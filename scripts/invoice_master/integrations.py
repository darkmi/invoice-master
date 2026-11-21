"""统一提取适配层 —— 把两个参考项目的能力接入 toolkit 的单一入口。

背景
----
``scripts/invoice_master`` 物理整合了 InvoiceFlowAI（AI 路线）与
piaoyihe（本地排版/7 票种路线），但三套代码的提取结果格式不同：

==================  ==========================  =============================
来源                结果形态                    字段命名
==================  ==========================  =============================
invoice_master     ``Invoice`` dataclass       doc_type / amount / seller ...
InvoiceFlowAI       legacy ``dict``             Type / Amount / Seller / ...
piaoyihe            ``dict``                    invoice_type / amount / seller_name ...
==================  ==========================  =============================

本模块把所有来源统一映射到 toolkit 的 :class:`Invoice` / :class:`ExtractResult`，
让 CLI 的 ``extract`` / ``report`` 只需一个 ``--engine`` 参数即可切换，
而两侧参考代码**零改写**。

引擎
----
* ``local``    —— 纯本地正则（toolkit 原生，默认）
* ``hybrid``   —— 本地正则 + toolkit LLM 兜底
* ``dual``     —— InvoiceFlowAI 双引擎（OCR+LLM → Vision → 本地兜底）
* ``piaoyihe`` —— 票易合 7 票种工厂提取器

依赖全部延迟导入：选 ``local`` 时不需要 requests / fitz 之外的任何新库；
选 ``dual`` 而无 API key 时抛 :class:`EngineUnavailable`，由 CLI 给出清晰提示
并回退到 local，不静默失败。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable, Protocol

from .config import Config
from .extractor import ExtractResult
from .models import (
    DocType,
    Invoice,
    Route,
    parse_amount,
    parse_date,
)

logger = logging.getLogger(__name__)

ENGINES = ("local", "hybrid", "dual", "piaoyihe")


class EngineUnavailable(RuntimeError):
    """所选引擎缺少依赖或配置，无法使用。"""


class ExtractorLike(Protocol):
    """提取器统一协议。"""

    def extract_one(self, path: str | Path) -> ExtractResult:
        ...

    def extract_many(self, paths: Iterable[str | Path]) -> list[ExtractResult]:
        ...

    @property
    def llm_status(self) -> str:
        ...


class _SingleViaMany:
    """为只实现批量的引擎提供单文件便利方法（供 rename/pair 复用）。"""

    def extract_one(self, path: str | Path) -> ExtractResult:
        return self.extract_many([path])[0]


# ---------------------------------------------------------------------------
# 结果映射
# ---------------------------------------------------------------------------


def _digits(value: Any) -> str:
    """提取纯数字（发票号/代码），保留字母数字混合票号。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isalnum())
    return digits


def legacy_payload_to_invoice(payload: dict[str, Any], path: Path, engine: str) -> Invoice:
    """把 InvoiceFlowAI 的 legacy 结果字典映射为 toolkit ``Invoice``。

    InvoiceFlowAI 字段：``Date / Purchaser / Seller / Amount / InvoiceCode /
    InvoiceNumber / Type / Departure_* / Destination_*``。日期既有 ``YYYYMMDD``
    也有 ``YYYY-MM-DD``，复用 toolkit 的 :func:`parse_date` 统一处理。
    """
    doc_type = DocType.normalize(payload.get("Type") or payload.get("category") or "")
    route = Route(
        origin=str(payload.get("Departure_City") or "").strip(),
        destination=str(payload.get("Destination_City") or "").strip(),
    )
    if payload.get("Departure_Date"):
        route.travel_date = parse_date(payload.get("Departure_Date"))

    return Invoice(
        path=path,
        doc_type=doc_type,
        amount=parse_amount(payload.get("Amount")),
        tax_amount=parse_amount(payload.get("Tax") or payload.get("tax_amount")),
        invoice_date=parse_date(payload.get("Date")),
        invoice_code=_digits(payload.get("InvoiceCode")),
        invoice_number=_digits(payload.get("InvoiceNumber")),
        purchaser=str(payload.get("Purchaser") or "").strip(),
        seller=str(payload.get("Seller") or "").strip(),
        product=str(payload.get("Product") or payload.get("item_name") or "").strip(),
        route=route,
        is_invoice=bool(payload.get("is_invoice", True)),
        confidence=0.7,
        engine=engine,
        warnings=list(payload.get("warnings") or []),
    )


def piaoyihe_payload_to_invoice(
    payload: dict[str, Any], path: Path, extractor_name: str = ""
) -> Invoice | None:
    """把票易合 ``extract_all()`` 结果映射为 toolkit ``Invoice``。

    票易合字段：``amount / invoice_date / invoice_type / product_type /
    buyer_name / seller_name / invoice_code / tax_amount``。
    金额或日期都没取到时返回 ``None``（与 toolkit 的判定口径一致）。

    注意票易合的 ``extract_amount`` 失败时返回 ``0.0`` 而非 ``None``，
    这里把 0 视为"未取到"——报销场景不能用 0 冒充真实金额。
    """
    amount = parse_amount(payload.get("amount"))
    if amount is not None and amount == 0:
        amount = None
    tax = parse_amount(payload.get("tax_amount"))
    if tax is not None and tax == 0:
        tax = None
    inv_date = parse_date(payload.get("invoice_date"))
    if amount is None and inv_date is None and tax is None:
        return None

    doc_type = DocType.normalize(
        payload.get("invoice_type") or payload.get("product_type") or ""
    )
    return Invoice(
        path=path,
        doc_type=doc_type,
        amount=amount,
        tax_amount=tax,
        invoice_date=inv_date,
        invoice_code=_digits(payload.get("invoice_code")),
        purchaser=str(payload.get("buyer_name") or "").strip(),
        seller=str(payload.get("seller_name") or "").strip(),
        product=str(payload.get("product_type") or "").strip(),
        is_invoice=True,
        confidence=0.65,  # 票易合是关键词路由正则，置信度中等
        engine=f"piaoyihe:{extractor_name or 'auto'}",
    )


def _failure(path: Path, reason: str) -> ExtractResult:
    return ExtractResult(path=path, invoice=None, reason=reason)


# ---------------------------------------------------------------------------
# 引擎实现
# ---------------------------------------------------------------------------


class LocalExtractor(_SingleViaMany):
    """纯本地正则（toolkit 原生）。"""

    engine = "local"

    def __init__(self, config: Config) -> None:
        from .extractor import Extractor

        section = config.section("extraction")
        self._impl = Extractor(
            max_pages=int(section.get("max_pdf_pages", 20)),
            fallback_to_mtime=bool(section.get("fallback_to_mtime", True)),
        )

    def extract_many(self, paths: Iterable[str | Path]) -> list[ExtractResult]:
        return [self._impl.extract(p) for p in paths]

    @property
    def llm_status(self) -> str:
        return "未启用（local 引擎）"


class HybridEngine(_SingleViaMany):
    """本地正则 + toolkit LLM 兜底。"""

    engine = "hybrid"

    def __init__(self, config: Config) -> None:
        from .llm import HybridExtractor

        self._impl = HybridExtractor(config)

    def extract_one(self, path: str | Path) -> ExtractResult:
        return self._impl.extract_one(path)

    def extract_many(self, paths: Iterable[str | Path]) -> list[ExtractResult]:
        return self._impl.extract_many(paths)

    @property
    def llm_status(self) -> str:
        return self._impl.llm_status


class DualEngine(_SingleViaMany):
    """InvoiceFlowAI 双引擎：Track A(OCR+LLM) → Track B(Vision) → 本地兜底。

    使用 InvoiceFlowAI 自带的 ``InvoiceExtractor.extract_info_via_llm``，
    它内部同时跑两条识别路径，并处理本地快路径与结果适配。本类只做
    配置桥接与结果映射，不改参考项目源码。
    """

    engine = "dual"

    def __init__(self, config: Config) -> None:
        section = config.section("llm")
        api_key = os.environ.get("INVOICEFLOW_GLM_API_KEY") or os.environ.get(
            "INVOICE_MASTER_LLM_API_KEY"
        ) or str(section.get("api_key") or "")
        if not api_key:
            raise EngineUnavailable(
                "dual 引擎需要 GLM API key：设置环境变量 INVOICEFLOW_GLM_API_KEY "
                "或 INVOICE_MASTER_LLM_API_KEY"
            )
        self._api_key = api_key
        self._extractor = None  # 延迟创建（构造会建输出目录）

    def _get_extractor(self):
        if self._extractor is not None:
            return self._extractor
        try:
            from .invoiceflow.invoice_extractor import InvoiceExtractor
        except ImportError as exc:
            raise EngineUnavailable(f"dual 引擎依赖缺失: {exc}") from exc
        self._extractor = InvoiceExtractor(api_key=self._api_key, output_dir=".")
        return self._extractor

    def extract_many(self, paths: Iterable[str | Path]) -> list[ExtractResult]:
        results: list[ExtractResult] = []
        for raw in paths:
            path = Path(raw)
            try:
                extractor = self._get_extractor()
                # 先尝试本地确定性快路径（不消耗 API）
                probe = extractor.probe_local_only(str(path))
                payload = None
                engine_name = "dual"
                if getattr(probe, "status", "") == "resolved":
                    payload = probe.result
                    engine_name = "dual:local-probe"
                else:
                    images = extractor.pdf_to_base64_image(str(path))
                    if not images:
                        results.append(_failure(path, "无法把 PDF 渲染为图片"))
                        continue
                    payload = extractor.extract_info_via_llm(images, pdf_path=str(path))
                    trace = getattr(extractor, "last_extraction_trace", {}) or {}
                    engine_name = f"dual:{trace.get('engine') or 'unknown'}"
                if not payload:
                    results.append(_failure(path, "双引擎未识别出有效字段"))
                    continue
                invoice = legacy_payload_to_invoice(payload, path, engine_name)
                warning = getattr(probe, "reason_code", "")
                if warning and "local-probe" not in engine_name:
                    invoice.warnings.append(f"本地探测: {warning}")
                results.append(ExtractResult(path=path, invoice=invoice, engine=engine_name))
            except EngineUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - 参考项目异常类型不固定
                logger.warning("dual 引擎处理 %s 失败: %s", path.name, exc)
                results.append(_failure(path, f"dual 引擎失败: {exc}"))
        return results

    @property
    def llm_status(self) -> str:
        return f"dual（GLM 双引擎，{'已配置' if self._api_key else '未配置'} key）"


class PiaoyiheEngine(_SingleViaMany):
    """票易合 7 票种工厂提取器：火车 > 飞机 > 机动车 > 出租车 > 定额 > 通行费 > 普票。"""

    engine = "piaoyihe"

    def __init__(self, config: Config) -> None:
        # 校验依赖可用性，避免整批失败
        try:
            from .piaoyihe.core.invoice_extractors.factory import InvoiceExtractorFactory
        except ImportError as exc:
            raise EngineUnavailable(f"piaoyihe 引擎依赖缺失: {exc}") from exc
        self._factory = InvoiceExtractorFactory

    def extract_many(self, paths: Iterable[str | Path]) -> list[ExtractResult]:
        results: list[ExtractResult] = []
        for raw in paths:
            path = Path(raw)
            try:
                extractor = self._factory.get_extractor(str(path))
                if extractor is None:
                    results.append(_failure(path, "票易合工厂未匹配到票种"))
                    continue
                name = type(extractor).__name__
                with extractor:
                    payload = extractor.extract_all()
                invoice = piaoyihe_payload_to_invoice(payload, path, name)
                if invoice is None:
                    results.append(_failure(path, "票易合未提取到有效字段"))
                    continue
                results.append(
                    ExtractResult(path=path, invoice=invoice, engine=f"piaoyihe:{name}")
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("piaoyihe 引擎处理 %s 失败: %s", path.name, exc)
                results.append(_failure(path, f"piaoyihe 引擎失败: {exc}"))
        return results

    @property
    def llm_status(self) -> str:
        return "未启用（piaoyihe 为纯本地 7 票种提取）"


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def make_extractor(config: Config, engine: str = "local") -> ExtractorLike:
    """按名称构造提取引擎。

    Args:
        config: toolkit 配置
        engine: ``local`` / ``hybrid`` / ``dual`` / ``piaoyihe``

    Raises:
        ValueError: 引擎名未知
        EngineUnavailable: 引擎依赖或配置缺失
    """
    engine = (engine or "local").lower()
    factories: dict[str, Any] = {
        "local": LocalExtractor,
        "hybrid": HybridEngine,
        "dual": DualEngine,
        "piaoyihe": PiaoyiheEngine,
    }
    if engine not in factories:
        raise ValueError(f"未知引擎: {engine}；可用: {', '.join(ENGINES)}")
    return factories[engine](config)
