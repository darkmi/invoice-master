"""LLM 兜底引擎。

默认**关闭**。正则拿不准时才调用，且必须显式开启：

    llm:
      enabled: true
      api_key: ""      # 或用环境变量 INVOICE_MASTER_LLM_API_KEY

设计要点（对标参考项目 Invoice-Downloader 的 ``extraction_pipeline.py``）：

1. **熔断器（circuit breaker）**
   连续失败 ``breaker_threshold`` 次即停止后续调用。
   没有熔断的话，API 欠费时会把几百张发票全跑一遍失败，
   既烧时间又刷屏。遇到 401/402 直接立即熔断。

2. **本地优先，远端兜底**
   ``needs_llm()`` 只在本地置信度不足或无文本层时返回 True，
   不做无谓的API 调用。

3. **结果交叉校验**
   LLM 返回的字段与本地结果冲突时，以本地为准并记录警告——
   正则对标准化版面（增值税发票）比视觉模型更可靠。

说明：调用 LLM 时，当前正在处理的单张票面图片会发送到所配置的模型服务商；
模块不发送邮件原文。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from . import pdf as pdfmod
from .config import Config
from .models import DocType, Invoice, Route, is_valid, parse_amount, parse_date

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """LLM 调用异常。"""


class LLMDisabled(LLMError):
    """LLM 未启用。"""


class LLMCircuitOpen(LLMError):
    """熔断器已开启，停止调用。"""


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------


@dataclass
class CircuitBreaker:
    """熔断器。

    状态机：closed（正常）-> open（熔断）-> half_open（试探恢复）

    Attributes:
        threshold: 连续失败多少次后熔断
        reset_after: 熔断后等待多少秒允许试探
    """

    threshold: int = 3
    reset_after: float = 60.0
    failures: int = 0
    opened_at: float = 0.0
    last_reason: str = ""

    @property
    def is_open(self) -> bool:
        """是否处于熔断状态。"""
        if self.opened_at == 0.0:
            return False
        if time.monotonic() - self.opened_at > self.reset_after:
            # 半开：允许一次试探
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    def record_failure(self, reason: str) -> None:
        self.failures += 1
        self.last_reason = reason
        # 认证/额度错误立即熔断 —— 重试无意义
        if any(code in reason for code in ("401", "402", "403", "INSUFFICIENT")):
            self.opened_at = time.monotonic()
            logger.error("LLM 认证/额度异常，立即熔断: %s", reason)
        elif self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            logger.error("LLM 连续失败 %d 次，熔断 %ds", self.failures, self.reset_after)

    def trip(self, reason: str) -> None:
        """手动熔断。"""
        self.opened_at = time.monotonic()
        self.last_reason = reason


# ---------------------------------------------------------------------------
# 结果包装
# ---------------------------------------------------------------------------


@dataclass
class LLMResult:
    """LLM 抽取结果。"""

    invoice: Invoice | None
    raw: str = ""
    engine: str = "llm"
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.invoice is not None


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class LLMExtractor:
    """LLM 兜底抽取器。兼容 OpenAI 风格 chat completions 接口。"""

    #: 请求模板 —— 中文提示，明确要求只返回 JSON
    PROMPT_TEMPLATE = """你是一个中国电子发票信息抽取助手。请阅读这张{doctype}图片，抽取以下字段并以JSON返回。

字段说明：
- doc_type: 票据类型（火车票/机票/住宿发票/打车/行程单/餐饮/过路费/增值税普票/增值税专票/定额发票/其他）
- amount: 价税合计金额（纯数字，不要币种符号）
- tax_amount: 税额（没有则填 0）
- invoice_date: 开票日期，格式 YYYY-MM-DD（火车票填乘车日期）
- invoice_code: 发票号码（纯数字）
- invoice_number: 发票代码（纯数字，没有则填空字符串）
- purchaser: 购买方名称
- seller: 销售方名称
- product: 商品或服务名称
- tax_rate: 税率（如 6%，没有则填空字符串）
- origin: 出发地（火车票/机票/行程单填，其他填空）
- destination: 目的地
- is_invoice: 是否是发票本身（行程单、水单等凭证填 false）

严格要求：
1. 只返回 JSON，不要任何解释文字或 markdown 代码块
2. 无法确定的字段填空字符串或 0，不要猜测编造
3. 金额必须是图片上实际显示的数字

JSON 格式：
{{"doc_type":"","amount":"","tax_amount":"","invoice_date":"","invoice_code":"","invoice_number":"","purchaser":"","seller":"","product":"","tax_rate":"","origin":"","destination":"","is_invoice":true}}"""

    def __init__(self, config: Config) -> None:
        section = config.section("llm")
        self.enabled = bool(section.get("enabled", False))
        self.base_url = str(section.get("base_url", "")).rstrip("/")
        self.model = str(section.get("model", ""))
        self.api_key = str(section.get("api_key", "")).strip()
        self.timeout = int(section.get("timeout", 60))
        self.max_retries = int(section.get("max_retries", 2))
        self.max_image_px = int(section.get("max_image_px", 1600))
        self.breaker = CircuitBreaker(threshold=int(section.get("breaker_threshold", 3)))
        #: 单次运行已调用次数，防止意外烧钱
        self.calls_made = 0
        self.max_calls = int(section.get("max_calls_per_run", 200))

    def ensure_ready(self) -> None:
        """检查是否可用。"""
        if not self.enabled:
            raise LLMDisabled(
                "LLM 兜底未启用。如需启用，在配置中设置 llm.enabled: true 并提供 llm.api_key"
            )
        if not self.api_key:
            raise LLMDisabled(
                "缺少 API Key。请设置 llm.api_key 或环境变量 INVOICE_MASTER_LLM_API_KEY"
            )
        if not self.base_url or not self.model:
            raise LLMDisabled("缺少 llm.base_url 或 llm.model 配置")

    def is_available(self) -> bool:
        """是否可用（启用 + 有密钥 + 未熔断 + 未超额）。"""
        return (
            self.enabled
            and bool(self.api_key)
            and not self.breaker.is_open
            and self.calls_made < self.max_calls
        )

    @property
    def status(self) -> str:
        """当前状态描述，用于 CLI 展示。"""
        if not self.enabled:
            return "未启用"
        if not self.api_key:
            return "缺少 API Key"
        if self.breaker.is_open:
            return f"已熔断（{self.breaker.last_reason}）"
        if self.calls_made >= self.max_calls:
            return f"已达调用上限 {self.max_calls}"
        return f"就绪（已调用 {self.calls_made}/{self.max_calls}）"

    # -- 入口 --------------------------------------------------------------

    def needs_llm(self, path, local_result=None) -> tuple[bool, str]:
        """判断是否需要 LLM 兜底。

        Args:
            path: 文件路径
            local_result: 本地抽取结果（:class:`~invoice_master.extractor.ExtractResult`）

        Returns:
            (是否需要, 原因)
        """
        if not self.is_available():
            return False, f"LLM 不可用: {self.status}"

        # 本地无文本层（扫描件）—— 必须兜底
        if local_result is not None and local_result.invoice is None:
            if "no_text_layer" in local_result.reason:
                return True, "扫描件无文本层"
            return True, f"本地抽取失败: {local_result.reason}"

        # 本地置信度不足
        if local_result is not None and local_result.invoice is not None:
            invoice = local_result.invoice
            threshold = 0.6
            if invoice.confidence < threshold:
                return True, f"置信度不足({invoice.confidence:.2f} < {threshold})"
            if invoice.amount is None:
                return True, "金额未识别"
            if invoice.invoice_date is None:
                return True, "日期未识别"

        return False, "本地抽取已足够"

    def extract(self, path, local_invoice: Invoice | None = None) -> LLMResult:
        """调用 LLM 抽取字段。

        Args:
            path: 文件路径
            local_invoice: 本地已抽到的结果，用于交叉校验

        Returns:
            LLMResult

        Raises:
            LLMCircuitOpen: 熔断中
            LLMError: 调用失败
        """
        if self.breaker.is_open:
            raise LLMCircuitOpen(f"LLM 已熔断: {self.breaker.last_reason}")
        if self.calls_made >= self.max_calls:
            raise LLMError(f"已达单次运行调用上限 {self.max_calls}")

        self.ensure_ready()

        from pathlib import Path

        file_path = Path(path).expanduser()
        doctype = local_invoice.doc_type.value if local_invoice else "发票"

        try:
            image = pdfmod.render_page_image(file_path, dpi=150, max_px=self.max_image_px)
        except pdfmod.PdfError as exc:
            return LLMResult(invoice=None, reason=f"render_failed: {exc}")

        prompt = self.PROMPT_TEMPLATE.format(doctype=doctype)
        raw = self._call_api(prompt, image, doctype)

        parsed = self._parse_response(raw)
        if parsed is None:
            self.breaker.record_failure("PARSE_FAILED")
            return LLMResult(invoice=None, raw=raw, reason="无法解析 LLM 返回的 JSON")

        invoice, warnings = self._to_invoice(parsed, file_path, local_invoice)
        self.calls_made += 1
        self.breaker.record_success()

        return LLMResult(invoice=invoice, raw=raw, warnings=warnings)

    # -- 内部实现 ----------------------------------------------------------

    def _call_api(self, prompt: str, image: bytes, doctype: str) -> str:
        """调用 chat completions 接口。"""
        import base64

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(image).decode("ascii")
                            },
                        },
                    ],
                }
            ],
            "temperature": 0,
        }

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")[:200]
                last_error = f"HTTP {exc.code}: {detail}"
                # 认证/额度错误不重试
                if exc.code in (401, 402, 403):
                    self.breaker.record_failure(last_error)
                    raise LLMError(f"LLM 认证失败 {last_error}") from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                KeyError,
            ) as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < self.max_retries:
                wait = 2**attempt
                logger.warning("LLM 调用失败(%s)，%ds 后重试", last_error, wait)
                time.sleep(wait)

        self.breaker.record_failure(last_error)
        raise LLMError(f"LLM 调用失败: {last_error}")

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any] | None:
        """从模型返回中提取 JSON。

        模型常在 JSON 外面裹markdown 代码块，需剥离。
        """
        if not raw:
            return None
        text = raw.strip()

        # 剥离 ```json ... ```
        fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()

        # 直接解析
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass

        # 提取第一个 {...}
        match = re.search(r"\{.+?\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    def _to_invoice(
        self,
        data: dict[str, Any],
        path,
        local_invoice: Invoice | None,
        *,
        engine: str = "llm",
    ) -> tuple[Invoice | None, list[str]]:
        """把 LLM JSON 转成 :class:`Invoice`，并与本地结果交叉校验。

        ``engine`` 允许 agent 视觉识别复用同一套字段解析与校验逻辑，
        同时把结果标记为 ``agent`` 而非 ``llm``。
        """
        from pathlib import Path

        file_path = Path(path)

        invoice = Invoice(
            path=file_path,
            doc_type=DocType.normalize(data.get("doc_type", "")),
            amount=parse_amount(data.get("amount")),
            # 不能用 `or Decimal(0)` —— 那样 None 会被变成 0，
            # 导致下面"金额和税额都没抽到"的判断永远不成立，
            # 把彻底失败的抽取当成成功返回。
            tax_amount=parse_amount(data.get("tax_amount")),
            invoice_date=parse_date(data.get("invoice_date")),
            invoice_code=re.sub(r"\D", "", str(data.get("invoice_code", ""))),
            invoice_number=re.sub(r"\D", "", str(data.get("invoice_number", ""))),
            purchaser=str(data.get("purchaser") or "").strip(),
            seller=str(data.get("seller") or "").strip(),
            product=str(data.get("product") or "").strip(),
            tax_rate=str(data.get("tax_rate") or "").strip(),
            route=Route(
                origin=str(data.get("origin") or "").strip(),
                destination=str(data.get("destination") or "").strip(),
            ),
            is_invoice=bool(data.get("is_invoice", True)),
            engine=engine,
            confidence=0.7,  # LLM/agent 视觉识别置信度固定中等，需人工抽检
        )

        warnings: list[str] = []
        if invoice.amount is None and invoice.tax_amount is None:
            return None, ["模型未识别出金额"]

        # 交叉校验：本地有值时不覆盖
        if local_invoice is not None:
            if local_invoice.amount is not None:
                if not self._amounts_agree(local_invoice.amount, invoice.amount):
                    warnings.append(
                        f"金额不一致：本地 {local_invoice.amount} vs LLM {invoice.amount}，"
                        "以本地为准"
                    )
                    invoice.amount = local_invoice.amount
            if is_valid(local_invoice.invoice_date):
                invoice.invoice_date = local_invoice.invoice_date
            if is_valid(local_invoice.seller):
                invoice.seller = local_invoice.seller
            if is_valid(local_invoice.purchaser):
                invoice.purchaser = local_invoice.purchaser
            if is_valid(local_invoice.invoice_code):
                invoice.invoice_code = local_invoice.invoice_code
            # 本地明确识别出的票种更可信
            if local_invoice.doc_type not in (DocType.UNKNOWN, DocType.OTHER):
                invoice.doc_type = local_invoice.doc_type
            invoice.is_invoice = local_invoice.is_invoice

        return invoice, warnings

    @staticmethod
    def _amounts_agree(a: Decimal, b: Decimal | None) -> bool:
        """金额是否一致（容差 1 分）。"""
        if b is None:
            return False
        return abs(a - b) <= Decimal("0.01")


# ---------------------------------------------------------------------------
# 包装：本地优先的组合提取
# ---------------------------------------------------------------------------


class HybridExtractor:
    """本地正则优先 + LLM 兜底的组合提取器。

    用法::

        hybrid = HybridExtractor(config)
        results = hybrid.extract_many(paths)
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        from .extractor import Extractor

        section = config.section("extraction")
        self.local = Extractor(
            max_pages=int(section.get("max_pdf_pages", 20)),
            fallback_to_mtime=bool(section.get("fallback_to_mtime", True)),
        )
        self.llm = LLMExtractor(config)
        threshold = float(section.get("confidence_threshold", 0.6))
        self.threshold = threshold

    def extract_one(self, path):
        """提取单个文件，本地不够则尝试 LLM。"""
        result = self.local.extract(path)

        if self.llm.is_available():
            need, reason = self.llm.needs_llm(path, result)
            if need:
                logger.info("触发 LLM 兜底 %s: %s", result.path.name, reason)
                try:
                    llm_result = self.llm.extract(
                        path, result.invoice if result.invoice else None
                    )
                    if llm_result.invoice is not None:
                        llm_result.invoice.warnings.append(f"LLM 兜底原因: {reason}")
                        return type(result)(
                            path=result.path,
                            invoice=llm_result.invoice,
                            engine="llm",
                            warnings=result.warnings + llm_result.warnings,
                        )
                    result.warnings.append(f"LLM 兜底失败: {llm_result.reason}")
                except (LLMError, pdfmod.PdfError) as exc:
                    logger.warning("LLM 兜底失败 %s: %s", result.path.name, exc)
                    result.warnings.append(f"LLM 兜底失败: {exc}")
        elif not result.ok:
            result.warnings.append(
                f"未启用 LLM 兜底（{self.llm.status}），无法处理扫描件"
            )

        return result

    def extract_many(self, paths):
        """批量提取。"""
        return [self.extract_one(p) for p in paths]

    @property
    def llm_status(self) -> str:
        return self.llm.status
