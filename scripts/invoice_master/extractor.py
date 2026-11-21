"""提取引擎：工厂 + 策略模式。

设计对照参考项目 piaoyihe 的做法（工厂 + 抽象基类 + 票种实现），
但修掉它两个问题：

1. 源码里塞满 ``print()`` 调试输出 —— 这里改用标准 ``logging``，
   默认静默，只有 ``--verbose`` 才输出。
2. 每个提取方法各自散落的正则没有统一收口 —— 这里把
   "关键词 + 优先级 + 兜底" 收敛到 :class:`FieldRule`，便于配置和测试。

用法::

    from invoice_master.extractor import Extractor
    extractor = Extractor(max_pages=20)
    result = extractor.extract(Path("发票.pdf"))

    if result.invoice:
        print(result.invoice.amount, result.invoice.doc_type)
"""

from __future__ import annotations

import logging
import re
from abc import ABC
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, ClassVar

from . import pdf as pdfmod
from .models import DocType, Invoice, Route, is_valid, parse_amount, parse_date

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 规则描述
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRule:
    """一条字段提取规则。

    Attributes:
        name: 字段名
        patterns: 正则列表，按顺序尝试，第一个命中即返回
        group: 取第几个捕获组；0 表示整个匹配
        transform: 命中后的后处理函数
    """

    name: str
    patterns: Sequence[str]
    group: int = 1
    transform: Callable[[str], object] | None = None

    def apply(self, text: str) -> tuple[object | None, str]:
        """在文本中应用规则。

        Returns:
            (值, 命中的原文)；未命中返回 (None, "")
        """
        for pattern in self.patterns:
            try:
                match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            except re.error:
                logger.warning("非法正则，已跳过: %r", pattern)
                continue
            if not match:
                continue
            try:
                raw = match.group(self.group)
            except IndexError:
                continue
            if not isinstance(raw, str):
                continue
            value = self.transform(raw) if self.transform else raw
            if value is None or (isinstance(value, str) and not is_valid(value)):
                continue
            return value, raw
        return None, ""


# ---------------------------------------------------------------------------
# 置信度
# ---------------------------------------------------------------------------

#: 关键字段命中时的置信度
_FIELD_SCORE = {
    "amount": 0.35,
    "date": 0.25,
    "code": 0.2,
    "seller": 0.1,
    "purchaser": 0.1,
}
_TYPE_SCORE = 0.3  # 类型明确识别


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class BaseExtractor(ABC):
    """票种提取器基类。

    子类必须提供 :attr:`keywords`（否则无法参与票种匹配），
    其余字段由基类的默认规则兜底；需要特殊逻辑时覆盖对应方法。
    """

    #: 用于识别本票种的关键词，**子类必须覆盖**。
    #: 匹配范围仅限票面顶部 120 字符（见 :meth:`matches`）。
    keywords: ClassVar[tuple[str, ...]]

    #: 识别出的票种
    type_name: ClassVar[DocType] = DocType.OTHER

    #: 优先级，数字越小越先匹配。特征明显的票种应给更小的值
    priority: ClassVar[int] = 100

    @classmethod
    def __init_subclass__(cls, **kwargs: Any) -> None:
        """子类定义时校验契约。

        只靠类型注解无法在运行时强制子类实现 ``keywords``——
        漏写时该提取器会静默永不匹配，表现为"某票种永远识别不出来"，
        排查成本很高。这里在类定义时就报错。

        必须加 ``@classmethod``：Python 3.9+ 中 ``__init_subclass__``
        隐式是类方法，无参 ``super()`` 会抛
        ``RuntimeError: super(): no arguments``。
        """
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "keywords", None):
            raise TypeError(
                f"{cls.__name__} 必须声明 keywords（票种识别关键词），"
                f"否则它永远不会被选中。例如:\n"
                f'    keywords = ("增值税专用发票",)'
            )

    def __init__(self, doc: pdfmod.PdfDoc, path: Path) -> None:
        self.doc = doc
        self.path = path
        self.text = doc.text
        self.words = list(doc.words)

    # -- 通用字段规则（子类可覆盖） ----------------------------------------

    amount_rules: ClassVar[tuple[FieldRule, ...]] = ()
    date_rules: ClassVar[tuple[FieldRule, ...]] = ()
    code_rules: ClassVar[tuple[FieldRule, ...]] = ()
    party_rules: ClassVar[tuple[FieldRule, ...]] = ()
    product_rules: ClassVar[tuple[FieldRule, ...]] = ()

    def extract_amount(self) -> Decimal | None:
        for rule in self.amount_rules:
            value, _ = rule.apply(self.text)
            if isinstance(value, (Decimal, float, int, str)):
                parsed = parse_amount(value)
                if parsed is not None:
                    return parsed
        return self._amount_fallback()

    def _amount_fallback(self) -> Decimal | None:
        """子类可覆盖的兜底提额逻辑。"""
        return None

    def extract_date(self):
        for rule in self.date_rules:
            value, _ = rule.apply(self.text)
            parsed = parse_date(value)
            if parsed is not None:
                return parsed
        return None

    def extract_code(self) -> str:
        for rule in self.code_rules:
            value, _ = rule.apply(self.text)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def extract_tax(self) -> Decimal | None:
        """提取税额。价税合计大写行是最可靠来源。"""
        # 策略1：合计行 "合计 ¥123.45 ¥12.34" —— 第二个金额即税额
        total_line = re.search(
            r"合\s*计.*?[¥￥]\s*(-?[\d,]+\.?\d*).*?[¥￥]\s*(-?[\d,]+\.?\d*)",
            self.text,
            re.DOTALL,
        )
        if total_line:
            tax = parse_amount(total_line.group(2))
            if tax is not None:
                return tax

        # 策略2：税率行 "13% 12.34"
        rate_rows = re.findall(
            r"(\d+(?:\.\d+)?)\s*%\s*(?:CNY\s*)?[¥￥]?\s*(-?[\d,]+\.?\d*)", self.text
        )
        if rate_rows:
            taxes = {t for t in (parse_amount(v) for _, v in rate_rows) if t is not None}
            if taxes:
                return sum(taxes)  # 多行发票时折扣行能自动抵消

        # 策略3：关键词附近
        near = re.search(r"税\s*额[:：]?\s*[¥￥]?\s*(-?[\d,]+\.?\d*)", self.text)
        if near:
            return parse_amount(near.group(1))
        return None

    def extract_parties(self) -> tuple[str, str, str]:
        """提取 (购买方, 销售方, 商品服务)。

        三级策略，从可靠到兜底：

        1. **坐标分栏** —— 增值税发票是左右分栏版面，PDF 文本流顺序不可信，
           必须按 x 坐标切分左右栏才能正确区分买卖方
        2. **标签定位** —— 用"购买方…名称:" / "销售方…名称:" 的邻近关系匹配
        3. **公司名兜底** —— 找带企业后缀的词组，按出现顺序假定买卖方

        策略 3 有固有歧义（无法判定哪个是买哪个是卖），
        故只在 1、2 都失败时启用。
        """
        purchaser = seller = ""

        # --- 策略 1：坐标分栏 ---
        left, right = pdfmod.split_columns(self.words)
        if left or right:
            seller_in_left = "销" in left
            seller_in_right = "销" in right
            if seller_in_left and not seller_in_right:
                seller_text, buyer_text = left, right
            elif seller_in_right and not seller_in_left:
                seller_text, buyer_text = right, left
            else:
                # 标签缺失时按国内发票惯例：左=购买方，右=销售方
                seller_text, buyer_text = right, left
            seller = self._first_company(seller_text)
            purchaser = self._first_company(buyer_text)

        # --- 策略 2：标签定位 ---
        if not seller or not purchaser:
            purchaser = purchaser or self._company_after_label("购买方")
            seller = seller or self._company_after_label("销售方")

        # --- 策略 3：整页兜底 ---
        if not seller and not purchaser:
            found = self._all_companies(self.text)
            if len(found) >= 2:
                purchaser, seller = found[0], found[1]
            elif found:
                seller = found[0]

        product = ""
        for rule in self.product_rules:
            value, _ = rule.apply(self.text)
            if isinstance(value, str) and value:
                product = value.replace("*", "").strip()[:40]
                break

        return purchaser, seller, product

    #: 公司名模式 —— 要求含企业后缀，避免把"名称:"之类误当公司名。
    #:
    #: **逐行匹配，绝不跨行**（:meth:`_match_company` 会做）。原因：PDF 文本里
    #: 公司名和标签常在同一行，跨行贪婪匹配会把上一行的名字连同下一行
    #: 的内容一起吞进来（实测把"名称: A公司" 和 "名称: B公司" 合成一个）。
    #:
    #: 分支 1 处理"XX有限公司"这类长后缀——用贪婪前缀吃到最远的合法后缀；
    #: 分支 2 处理"XX酒店/科技/银行"等短后缀——用惰性前缀避免吃多。
    COMPANY_PATTERN: ClassVar[str] = (
        r"([一-龥A-Za-z0-9（）()·]{2,40}(?:股份有限公司|有限责任公司|集团有限公司|有限公司))"
        r"|([一-龥A-Za-z0-9（）()·]{2,40}?(?:酒店|宾馆|旅馆|民宿|旅行社|银行|医院|"
        r"研究院|学校|事务所|合作社|集团|公司|企业|酒店管理|信息技术|科技|出行|用车))"
    )

    @classmethod
    def _match_company(cls, text: str) -> str:
        """从文本中取出第一个公司名（处理双捕获组 + 逐行限制）。

        逐行处理是关键：公司名与"名称:"标签在同一行，
        跨行匹配会把相邻字段的内容一起吞进来。
        """
        if not text:
            return ""
        for line in text.splitlines():
            match = re.search(cls.COMPANY_PATTERN, line)
            if match:
                # 分支 1（长后缀）优先
                name = (match.group(1) or match.group(2) or "").strip()
                if name:
                    return name
        return ""

    @classmethod
    def _first_company(cls, text: str) -> str:
        """取文本中第一个公司名。"""
        return cls._match_company(text)

    @classmethod
    def _all_companies(cls, text: str) -> list[str]:
        """取文本中所有公司名（去重保序）。"""
        seen: list[str] = []
        for line in (text or "").splitlines():
            for match in re.finditer(cls.COMPANY_PATTERN, line):
                name = (match.group(1) or match.group(2) or "").strip()
                if name and name not in seen:
                    seen.append(name)
        return seen

    def _company_after_label(self, label: str) -> str:
        """找 ``label`` 标签之后出现的第一个公司名。

        **逐行扫描**，不用 DOTALL 跨行匹配。增值税发票里
        "购买方"与"销售方"两个标签之间可能只有两行，
        跨行贪婪匹配会一路吞到下一个字段，把买方的名字当成卖方的。

        判定规则：从 ``label`` 所在行开始往后找，
        在遇到下一个对方标签前，取第一个"名称:"标签后的公司名。
        """
        lines = self.text.splitlines()
        try:
            start = next(i for i, line in enumerate(lines) if label in line)
        except StopIteration:
            return ""

        # 对方标签 —— 越过它就说明进入别人的区域了
        opposite = "销售方" if "购买" in label else "购买方"
        # "名称:" 标签之后才取公司名，避免抓到"纳税人识别号"等其他字段
        name_pending = False

        for line in lines[start + 1 : start + 8]:
            if opposite in line:
                break
            if "名" in line and "称" in line:
                name_pending = True
                company = self._match_company(line)
                if company:
                    return company
                continue
            if name_pending:
                # "名称:" 可能单独占一行，公司名在下一行
                company = self._match_company(line)
                if company:
                    return company

        return ""

    def extract_route(self) -> Route:
        """提取行程信息。默认无。"""
        return Route()

    def is_invoice(self) -> bool:
        """该文件本身是否为发票（凭证返回 False）。"""
        return True

    # -- 置信度 ------------------------------------------------------------

    def confidence(self, values: dict[str, object]) -> float:
        score = _TYPE_SCORE
        for key, weight in _FIELD_SCORE.items():
            value = values.get(key)
            if value not in (None, "", Decimal(0)):
                score += weight
        return min(round(score, 2), 1.0)

    # -- 入口 --------------------------------------------------------------

    def build(self) -> Invoice:
        """执行全部字段抽取并组装 :class:`Invoice`。"""
        amount = self.extract_amount()
        invoice_date = self.extract_date()
        code = self.extract_code()
        purchaser, seller, product = self.extract_parties()

        invoice = Invoice(
            path=self.path,
            doc_type=self.type_name,
            amount=amount,
            tax_amount=self.extract_tax(),
            invoice_date=invoice_date,
            invoice_code=code,
            purchaser=purchaser,
            seller=seller,
            product=product,
            route=self.extract_route(),
            is_invoice=self.is_invoice(),
            engine="local",
        )
        invoice.confidence = self.confidence(
            {
                "amount": amount,
                "date": invoice_date,
                "code": code,
                "seller": seller,
                "purchaser": purchaser,
            }
        )
        return invoice

    @classmethod
    def matches(cls, text: str) -> bool:
        """文本是否属于该票种。

        **只在票面标题区（开头 120 字符）匹配，不扫全文。**

        这是从实测踩出来的坑：增值税专用发票的商品明细写着
        ``*餐饮服务*餐费``。若范围放太宽，这张专票会被优先级更高的
        ``MealExtractor`` 抢走——票种一跑偏，金额还能蒙对，
        但日期和发票号码会全丢（餐饮提取器没定义这些规则）。

        120 字符覆盖了票种名称所在行（"XX增值税专用发票"/"电子发票(普通发票)"
        通常在第一行），又不会把中部的商品明细纳入。
        票种名称按规范一定印在票面顶部，所以这个范围既准确又够用。
        """
        head = re.sub(r"\s+", "", text[:120])
        return any(re.sub(r"\s+", "", kw) in head for kw in cls.keywords)


# ---------------------------------------------------------------------------
# 结果包装
# ---------------------------------------------------------------------------


@dataclass
class ExtractResult:
    """单文件抽取结果。"""

    path: Path
    invoice: Invoice | None
    engine: str = "local"
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.invoice is not None

    @property
    def needs_review(self) -> bool:
        """是否需要人工复核（置信度低或未识别）。"""
        if self.invoice is None:
            return True
        return self.invoice.doc_type is DocType.UNKNOWN or self.invoice.confidence < 0.6


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class Extractor:
    """字段抽取入口。

    流程：PDF 文本层 -> 票种识别 -> 对应提取器 -> :class:`Invoice`。
    文本层缺失（扫描件）时返回 ``reason='no_text_layer'``，
    由上层决定是否走 LLM/OCR。
    """

    def __init__(
        self,
        *,
        max_pages: int = 20,
        fallback_to_mtime: bool = True,
        registry: Sequence[type[BaseExtractor]] | None = None,
    ) -> None:
        self.max_pages = max_pages
        self.fallback_to_mtime = fallback_to_mtime
        self._registry = list(registry) if registry else None

    @property
    def registry(self) -> list[type[BaseExtractor]]:
        """已注册提取器，按优先级排序。"""
        if self._registry is None:
            from . import rules  # 延迟导入避免循环依赖

            self._registry = sorted(rules.ALL_EXTRACTORS, key=lambda c: c.priority)
        return self._registry

    def extract(self, path: str | Path) -> ExtractResult:
        """抽取单个 PDF 文件。"""
        target = Path(path).expanduser()

        try:
            doc = pdfmod.extract(target, max_pages=self.max_pages)
        except pdfmod.PdfError as exc:
            logger.warning("PDF 解析失败 %s: %s", target.name, exc)
            return ExtractResult(path=target, invoice=None, reason=f"pdf_error: {exc}")

        warnings: list[str] = []
        if doc.is_scanned:
            return ExtractResult(
                path=target,
                invoice=None,
                reason="no_text_layer",
                warnings=["疑似扫描件，无文本层，需 OCR 或 LLM 兜底"],
            )
        if doc.page_count > self.max_pages:
            warnings.append(f"文档共 {doc.page_count} 页，仅解析前 {self.max_pages} 页")

        extractor_cls = self.select(doc.text)
        if extractor_cls is None:
            # 无票种匹配 —— 仍用增值税兜底提取器试一次
            from .rules import VatInvoiceExtractor

            extractor_cls = VatInvoiceExtractor
            warnings.append("未匹配到特定票种，使用增值税发票兜底规则")

        try:
            invoice = extractor_cls(doc, target).build()
        except Exception as exc:  # 单个文件失败不应中断批量
            logger.exception("字段抽取异常 %s", target.name)
            return ExtractResult(
                path=target, invoice=None, reason=f"extract_error: {exc}"
            )

        if invoice.amount is None and self.fallback_to_mtime:
            import datetime as _dt

            try:
                invoice.invoice_date = (
                    invoice.invoice_date
                    or _dt.datetime.fromtimestamp(target.stat().st_mtime).date()
                )
                warnings.append("未能识别开票日期，已回退为文件修改时间")
            except OSError:
                pass

        invoice.warnings.extend(warnings)
        return ExtractResult(
            path=target, invoice=invoice, engine="local", warnings=warnings
        )

    def select(self, text: str) -> type[BaseExtractor] | None:
        """按文本内容选择票种提取器（优先级顺序，首个命中即返回）。"""
        for extractor_cls in self.registry:
            try:
                if extractor_cls.matches(text):
                    return extractor_cls
            except Exception:  # 单个规则出错不影响其他
                logger.debug("票种匹配失败: %s", extractor_cls.__name__, exc_info=True)
        return None

    def extract_many(self, paths: Iterable[str | Path]) -> list[ExtractResult]:
        """批量抽取。单个失败不影响其余文件。"""
        results: list[ExtractResult] = []
        for path in paths:
            results.append(self.extract(path))
            result = results[-1]
            if result.invoice is None:
                logger.info("抽取失败 %s: %s", Path(path).name, result.reason)
        return results


def register(extractor_cls: type[BaseExtractor], priority: int | None = None):
    """注册自定义提取器装饰器。

    用法::

        @register
        class MyInvoiceExtractor(BaseExtractor):
            keywords = ("我的票种",)
            type_name = DocType.OTHER
            priority = 10
    """
    if priority is not None:
        extractor_cls.priority = priority
    from . import rules

    rules.ALL_EXTRACTORS.append(extractor_cls)
    return extractor_cls
