"""领域模型与基础工具。

两个关键设计决策（来自生产踩坑）：

1. **金额一律用 ``Decimal``，禁止 float。**
   发票金额要参与求和比对，float 的二进制表示误差会让
   ``0.1 + 0.2 != 0.3`` 变成静默的错误对账结果。

2. **日期统一钉死 Asia/Shanghai。**
   服务器时区不确定，跨时区会把开票日期算错一天。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo

    BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 缺少时区库时退化为固定偏移
    BUSINESS_TZ = None  # type: ignore[assignment]


# 视为"没有值"的占位文本。模型抽取失败时经常返回这些而非空串。
_NULL_TEXTS = {
    "",
    "-",
    "--",
    "none",
    "null",
    "nan",
    "n/a",
    "na",
    "unknown",
    "undefined",
    "无",
    "未知",
    "暂无",
    "未填写",
    "不详",
    "不适用",
}


# ---------------------------------------------------------------------------
# 票据类型
# ---------------------------------------------------------------------------


class DocType(str, Enum):
    """支持的票据类型。

    继承 ``str`` 便于直接与配置、JSON 互操作。
    """

    TRAIN = "火车票"
    FLIGHT = "机票"
    FLIGHT_ITINERARY = "航班行程单"
    HOTEL_INVOICE = "住宿发票"
    HOTEL_FOLIO = "住宿水单"
    RIDE = "打车"
    RIDE_ITINERARY = "行程单"
    MEAL = "餐饮"
    TOLL = "过路费"
    QUOTA = "定额发票"
    FUEL = "加油票"
    PARKING = "停车费"
    VAT_NORMAL = "增值税普票"
    VAT_SPECIAL = "增值税专票"
    DIGITAL = "电子发票"
    OTHER = "其他"
    UNKNOWN = "未识别"

    @classmethod
    def normalize(cls, value: object) -> DocType:
        """把任意文本归一到已知类型。

        顺序敏感：更具体的规则放前面，避免 "机票行程单" 被误判为普通行程单。
        """
        text = _clean(value).replace(" ", "")
        if not text:
            return cls.UNKNOWN

        # 精确匹配
        for member in cls:
            if text == member.value:
                return member

        rules: tuple[tuple[tuple[str, ...], DocType], ...] = (
            (("火车票", "铁路电子客票", "高铁", "动车", "列车票", "train"), cls.TRAIN),
            (("航班行程单", "航空行程单"), cls.FLIGHT_ITINERARY),
            (("机票", "航空", "flight", "airline"), cls.FLIGHT),
            (
                ("水单", "账单", "结账单", "住宿明细", "folio", "consumption"),
                cls.HOTEL_FOLIO,
            ),
            (("住宿确认单", "确认单"), cls.HOTEL_FOLIO),
            (("住宿", "酒店", "宾馆", "民宿", "hotel"), cls.HOTEL_INVOICE),
            (
                (
                    "打车",
                    "滴滴",
                    "出租",
                    "高德",
                    "网约车",
                    "曹操",
                    "T3出行",
                    "ride",
                    "taxi",
                ),
                cls.RIDE,
            ),
            (("行程单", "报销单", "itinerary", "trip"), cls.RIDE_ITINERARY),
            (("餐饮", "餐费", "美食", "饭店", "restaurant"), cls.MEAL),
            (("过路", "高速", "通行费", "toll"), cls.TOLL),
            (("定额", "quota"), cls.QUOTA),
            (("加油", "石油", "石化", "fuel", "gas"), cls.FUEL),
            (("停车", "parking"), cls.PARKING),
            (("专用发票", "专票", "special"), cls.VAT_SPECIAL),
            (("普通发票", "普票", "normal"), cls.VAT_NORMAL),
            (("增值税", "电子发票", "数电票", "invoice"), cls.DIGITAL),
        )
        for keywords, target in rules:
            if any(keyword in text for keyword in keywords):
                return target
        return cls.OTHER


#: 需要伴随凭证（行程单/水单）才构成完整报销材料的类型
COMPANION_REQUIRED: dict[DocType, DocType] = {
    DocType.RIDE: DocType.RIDE_ITINERARY,
    DocType.HOTEL_INVOICE: DocType.HOTEL_FOLIO,
}

#: 本身不是发票的附件类型，仅作为凭证参与配对
COMPANION_ONLY: frozenset[DocType] = frozenset(
    {DocType.RIDE_ITINERARY, DocType.HOTEL_FOLIO}
)


def is_valid(value: object) -> bool:
    """判断文本是否为有效值（排除占位词）。"""
    return _clean(value) not in _NULL_TEXTS


def _clean(value: object) -> str:
    """清洗文本：全角转半角、去空白、归一化。"""
    if value is None:
        return ""
    text = str(value)
    # 全角字符转半角
    text = unicodedata.normalize("NFKC", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_amount(value: Any) -> Decimal | None:
    """解析金额为 ``Decimal``。

    支持的输入形态：
      - ``1234.56`` / ``"1234.56"`` / ``Decimal``
      - ``"¥1,234.56"`` / ``"￥1,234.56 元"``
      - ``"CNY 100.00"`` / ``"RMB 100.00"``
      - 会计负数 ``"(100.00)"`` -> ``-100.00``
      - 百分号税率 ``"6%"`` -> ``Decimal("6")``

    无法解析时返回 ``None``（而不是 0）。0 和"没解析出来"是两回事，
    报销场景必须能区分。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, float)):
        # float 先转字符串，避免二进制表示误差传导
        text = repr(value) if isinstance(value, float) else str(value)
    elif isinstance(value, str):
        text = value
    else:
        return None

    normalized = _clean(text)
    if normalized.lower().replace(" ", "") in _NULL_TEXTS:
        return None

    # 剥离货币单位与币种。\b 对中文/紧邻数字失效，
    # 所以 RMB100 这类无分隔写法必须用非贪婪前缀匹配，
    # 否则 \b(cny|rmb)\b 会因 RMB 与 100 之间没有边界而匹配不到。
    normalized = re.sub(r"(?i)(cny|rmb|人民币)", "", normalized)
    normalized = normalized.replace("元", "")
    normalized = re.sub(r"[¥￥%,，$\s]", "", normalized)

    if not normalized:
        return None

    # 会计负数：括号包裹
    has_open = normalized.startswith("(")
    has_close = normalized.endswith(")")
    if has_open or has_close:
        if not (has_open and has_close):
            return None  # 括号不配对，视为格式错误
        inner = normalized[1:-1]
        if not inner or inner.startswith(("+", "-")):
            return None
        normalized = f"-{inner}"
    elif "(" in normalized or ")" in normalized:
        return None

    # 全角数字统一处理
    normalized = _to_halfwidth_digits(normalized)
    if not normalized:
        return None

    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def _to_halfwidth_digits(text: str) -> str:
    """把中文/全角数字转成ASCII 数字。"""
    table = str.maketrans("０１２３４５６７８９．－", "0123456789.-")
    return text.translate(table)


def parse_amount_safe(value: Any, default: Decimal | None = None) -> Decimal | None:
    """``parse_amount`` 的容错版本，解析失败返回 default。"""
    try:
        return parse_amount(value)
    except Exception:
        return default


def parse_date(value: Any) -> date | None:
    """解析日期为 ``date``。

    支持 ``2026-03-15`` / ``2026年3月15日`` / ``2026/03/15`` / ``2026.3.15``
    / ``20260315`` / ``datetime`` / Unix 时间戳。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.astimezone(BUSINESS_TZ).date() if BUSINESS_TZ else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).astimezone(BUSINESS_TZ).date()
        except (OverflowError, OSError, ValueError):
            return None

    text = _clean(value)
    if text.lower().replace(" ", "") in _NULL_TEXTS:
        return None

    match = re.fullmatch(
        r"(\d{4})\s*(?:年|[-/.])?\s*(\d{1,2})\s*(?:月|[-/.])?\s*(\d{1,2})\s*日?",
        text,
    )
    if not match:
        digits = re.sub(r"\D", "", text)
        match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", digits) if digits else None
    if not match:
        return None

    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def format_amount(amount: Decimal | None, precision: int = 2) -> str:
    """格式化金额为字符串。"""
    if amount is None:
        return ""
    quantized = amount.quantize(Decimal(1).scaleb(-precision))
    return f"{quantized:f}"


def format_date(value: date | None, pattern: str = "%Y-%m-%d") -> str:
    """格式化日期为字符串。"""
    if value is None:
        return ""
    return value.strftime(pattern)


# ---------------------------------------------------------------------------
# 记录模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """行程信息（火车票/机票/网约车通用）。"""

    origin: str = ""
    destination: str = ""
    travel_date: date | None = None

    def is_complete(self) -> bool:
        return bool(self.origin and self.destination)


@dataclass
class Invoice:
    """一张发票/凭证的抽取结果。

    注意 ``amount`` 是 ``Decimal | None`` —— ``None`` 表示未取到，
    不可用 0 代替。
    """

    path: Path
    doc_type: DocType = DocType.UNKNOWN

    amount: Decimal | None = None
    tax_amount: Decimal | None = None
    invoice_date: date | None = None

    invoice_code: str = ""  # 发票号码（20 位）
    invoice_number: str = ""  # 发票代码（12 位），部分票种没有
    purchaser: str = ""  # 购买方
    seller: str = ""  # 销售方
    product: str = ""  # 商品/服务名称
    tax_rate: str = ""  # 税率

    route: Route = field(default_factory=Route)
    is_invoice: bool = True  # False 表示这是凭证（行程单/水单）

    confidence: float = 0.0  # 抽取置信度 0~1
    engine: str = ""  # 抽取来源：local / llm / fallback
    warnings: list[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def companion_type(self) -> DocType | None:
        """若本票需要配套凭证，返回凭证类型。"""
        return COMPANION_REQUIRED.get(self.doc_type)

    @property
    def needs_companion(self) -> bool:
        return self.companion_type is not None and self.is_invoice

    def key_fields(self) -> tuple[str, ...]:
        """配对时用于判定是否同一笔业务的关键字段。"""
        return (
            _normalize_company(self.seller),
            format_date(self.invoice_date),
            _normalize_company(self.purchaser),
        )

    def to_row(self) -> dict[str, Any]:
        """转成报表行。"""
        return {
            "文件名": self.filename,
            "类型": self.doc_type.value,
            "开票日期": format_date(self.invoice_date),
            "金额": format_amount(self.amount),
            "税额": format_amount(self.tax_amount),
            "发票号码": self.invoice_code,
            "发票代码": self.invoice_number,
            "购买方": self.purchaser,
            "销售方": self.seller,
            "商品服务": self.product,
            "税率": self.tax_rate,
            "是否发票": "是" if self.is_invoice else "否",
            "置信度": round(self.confidence, 2),
            "识别方式": self.engine,
            "路径": str(self.path),
        }


_COMPANY_SUFFIX_RE = re.compile(
    r"(股份有限公司|有限责任公司|有限公司|集团有限公司|集团|公司|企业|"
    r"合作社|事务所|研究院|学校|医院|银行)"
)


def _normalize_company(value: object) -> str:
    """归一化公司名用于比较：去空格、全角转半角、去后缀。"""
    text = _clean(value).replace(" ", "").replace("　", "")
    return _COMPANY_SUFFIX_RE.sub("", text)


def companies_match(a: object, b: object) -> bool:
    """判断两个公司名是否指向同一主体（去后缀后比较）。"""
    left = _normalize_company(a)
    right = _normalize_company(b)
    if not left or not right:
        return False
    return left == right or left in right or right in left


# ---------------------------------------------------------------------------
# 配对
# ---------------------------------------------------------------------------


@dataclass
class Pair:
    """一组配对结果：发票 + 其配套凭证。"""

    invoice: Invoice
    companion: Invoice
    score: int = 0

    @property
    def paths(self) -> list[Path]:
        return [self.invoice.path, self.companion.path]


@dataclass
class PairingResult:
    """配对汇总。"""

    pairs: list[Pair] = field(default_factory=list)
    unmatched_invoices: list[Invoice] = field(default_factory=list)
    unmatched_companions: list[Invoice] = field(default_factory=list)

    @property
    def total_invoices(self) -> int:
        return len(self.pairs) + len(self.unmatched_invoices)

    @property
    def coverage(self) -> float:
        """配对覆盖率 0~1。"""
        total = self.total_invoices
        return len(self.pairs) / total if total else 0.0


def pair_invoices(invoices: Iterable[Invoice]) -> PairingResult:
    """按业务字段把发票与凭证配对。

    比对 piaoyihe 的做法：金额相等是硬约束，供应商/日期/商户词做打分，
    一对多时取最高分。打车票存在 3% 税率系数，故放宽金额容差。

    Args:
        invoices: 全部票据（发票与凭证混在一起）

    Returns:
        PairingResult
    """
    items = list(invoices)
    invoice_side = [i for i in items if i.is_invoice]
    companion_side = [i for i in items if not i.is_invoice]

    result = PairingResult()
    used: set[int] = set()

    for invoice in invoice_side:
        wanted = invoice.companion_type
        if wanted is None:
            continue

        best_index: int | None = None
        best_score = 0
        for index, companion in enumerate(companion_side):
            if index in used:
                continue
            if companion.doc_type is not wanted:
                continue
            score = _pair_score(invoice, companion)
            if score <= 0:
                continue
            if score > best_score:
                best_score, best_index = score, index

        if best_index is not None and best_score >= _PAIR_MIN_SCORE:
            used.add(best_index)
            result.pairs.append(Pair(invoice, companion_side[best_index], best_score))
        else:
            result.unmatched_invoices.append(invoice)

    result.unmatched_companions = [
        c for i, c in enumerate(companion_side) if i not in used
    ]
    # 没有需要配对凭证的发票（火车票等）不算缺失
    result.unmatched_invoices = [
        i for i in result.unmatched_invoices if i.needs_companion
    ]
    return result


#: 低于该分数不建立配对 —— 宁可留待人工，也不要错配
_PAIR_MIN_SCORE = 40

#: 打车发票含税，行程单金额常为不含税，容忍 3% 系数误差
_RIDE_TAX_FACTOR = Decimal("1.03")
_RIDE_TOLERANCE = Decimal("0.50")
_CENT = Decimal("0.01")


def _amounts_compatible(invoice: Invoice, companion: Invoice) -> bool:
    """金额是否可配对。"""
    if invoice.amount is None or companion.amount is None:
        return False
    if abs(invoice.amount - companion.amount) < _CENT:
        return True
    if invoice.doc_type is DocType.RIDE:
        # 行程单可能是未税价，发票是含税价
        return (
            abs(invoice.amount * _RIDE_TAX_FACTOR - companion.amount) < _RIDE_TOLERANCE
            or abs(companion.amount * _RIDE_TAX_FACTOR - invoice.amount) < _RIDE_TOLERANCE
        )
    return False


def _pair_score(invoice: Invoice, companion: Invoice) -> int:
    """配对打分，0 表示不兼容。"""
    if not _amounts_compatible(invoice, companion):
        return 0

    score = 40  # 金额一致的基础分

    if companies_match(invoice.seller, companion.seller):
        score += 60

    if invoice.tax_amount is not None and companion.tax_amount is not None:
        if abs(invoice.tax_amount - companion.tax_amount) < _CENT:
            score += 30

    if invoice.invoice_date and companion.invoice_date:
        days = abs((invoice.invoice_date - companion.invoice_date).days)
        if days == 0:
            score += 30
        elif days <= 3:
            score += max(0, 20 - days * 6)

    # 同目录或同邮件来源是强信号
    if invoice.path.parent == companion.path.parent:
        score += 20

    return score
