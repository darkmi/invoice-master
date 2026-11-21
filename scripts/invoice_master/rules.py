"""各票种的具体提取规则。

这里是对参考项目 piaoyihe``invoice_extractors/`` 的重写：
保留了工厂/策略的架构与各票种的实战正则，但做了三处改动：

1. 去掉散落的 ``print()``，改为结构化日志
2. 把重复的"关键词 + 多级正则 + 兜底"收敛为 :class:`FieldRule`
3. 补上参考项目缺失的出租车电子发票、网约车行程单、增值税专票区分

优先级约定：数字越小越先匹配。特征明显的票种（火车票有"铁路电子客票"、
机票有"航空运输")必须排在前面，否则会被增值税兜底规则抢走。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import ClassVar

from .extractor import BaseExtractor, FieldRule
from .models import DocType, Route, parse_amount


# 金额归一为Decimal
def _amount(value: str) -> Decimal | None:
    return parse_amount(value)


def _code(value: str) -> str:
    """发票号码：只保留数字。"""
    return re.sub(r"\D", "", value)


# ---------------------------------------------------------------------------
# 铁路电子客票 / 火车票
# ---------------------------------------------------------------------------


class TrainTicketExtractor(BaseExtractor):
    """火车票 / 铁路电子客票。

    特殊性：没有购买方/销售方概念（旅客姓名代替），
    日期是乘车日期而非开票日期，金额是票价且无税额。
    """

    priority: ClassVar[int] = 10
    keywords: ClassVar[tuple[str, ...]] = (
        "铁路电子客票",
        "火车票",
        "列车票",
        "高铁",
        "动车",
    )
    type_name: ClassVar[DocType] = DocType.TRAIN

    amount_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("票价", (r"票价[:：]?\s*[¥￥]?\s*([\d,]+\.?\d*)",)),
        FieldRule("票价", (r"[¥￥]\s*([\d,]+\.?\d*)",)),
    )

    date_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("乘车日期", (r"乘车日期[:：]?\s*(\d{4}[-年.]\d{1,2}[-月.]\d{1,2})",)),
        FieldRule("乘车日期", (r"(\d{4}年\d{1,2}月\d{1,2}日)",)),
    )

    code_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("号码", (r"发票号码[:：]?\s*(\d{20})", r"电子客票号[:：]?\s*(\w+)")),
    )

    def extract_tax(self) -> Decimal | None:
        return None  # 火车票无税额

    def extract_parties(self) -> tuple[str, str, str]:
        """购买方位置填旅客姓名，销售方固定为中国铁路。"""
        passenger = ""
        # 电子客票里身份证号后跟姓名
        match = re.search(r"\d{6}\*{4,8}\d{2,4}[\s\t]+([一-龥]{2,4})", self.text)
        if not match:
            match = re.search(r"(?:旅客|乘客)[:：]?\s*([一-龥]{2,4})", self.text)
        if match:
            passenger = match.group(1)
        return passenger, "中国铁路", "铁路旅客运输"

    def extract_route(self) -> Route:
        origin = destination = ""
        # "北京南" -> "上海虹桥"
        match = re.search(r"([一-龥]{2,6})\s*→\s*([一-龥]{2,6})", self.text)
        if not match:
            match = re.search(r"([一-龥]{2,6})\s*-\s*([一-龥]{2,6})", self.text)
        if match:
            origin, destination = match.group(1), match.group(2)
        return Route(
            origin=origin, destination=destination, travel_date=self.extract_date()
        )


# ---------------------------------------------------------------------------
# 航空运输电子客票行程单
# ---------------------------------------------------------------------------


class FlightTicketExtractor(BaseExtractor):
    """机票 / 航空运输电子客票行程单。

    注意：行程单和"电子客票"是两回事。前者用于报销，后者是乘机凭证。
    两者都归到本提取器，用 ``is_invoice`` 区分。
    """

    priority: ClassVar[int] = 20
    keywords: ClassVar[tuple[str, ...]] = (
        "航空运输电子客票",
        "电子客票行程单",
        "机票",
        "航班",
    )
    type_name: ClassVar[DocType] = DocType.FLIGHT

    amount_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("票价", (r"票价[:：]?\s*[¥￥]?\s*([\d,]+\.?\d*)",)),
        FieldRule("合计", (r"合计[:：]?\s*[¥￥]?\s*([\d,]+\.?\d*)",)),
        FieldRule("金额", (r"[¥￥]\s*([\d,]+\.?\d*)",)),
    )

    date_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("日期", (r"开票日期[:：]?\s*(\d{4}[-年.]\d{1,2}[-月.]\d{1,2})",)),
        FieldRule("日期", (r"(\d{4}年\d{1,2}月\d{1,2}日)",)),
    )

    code_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule(
            "号码", (r"发票号码[:：]?\s*(\d{8,20})", r"票号[:：]?\s*(\d{10,13}[-–]?\d?)")
        ),
    )

    def extract_parties(self) -> tuple[str, str, str]:
        # 航空行程单里承运人字段是航空公司
        airline = ""
        for pattern in (
            r"(?:承运人|航空公司|销售航空公司)[:：]?\s*([一-龥A-Za-z（）()]{2,30})",
            r"([一-龥]{2,10}(?:航空|航空公司))",
        ):
            match = re.search(pattern, self.text)
            if match:
                airline = match.group(1).strip()
                break
        return "", airline or "航空公司", "航空运输服务"

    def extract_route(self) -> Route:
        origin = destination = ""
        match = re.search(r"([一-龥]{2,6})\s*[-–—→]+\s*([一-龥]{2,6})", self.text)
        if match:
            origin, destination = match.group(1), match.group(2)
        return Route(
            origin=origin, destination=destination, travel_date=self.extract_date()
        )


# ---------------------------------------------------------------------------
# 网约车 / 出租车发票
# ---------------------------------------------------------------------------


class RideInvoiceExtractor(BaseExtractor):
    """网约车、出租车发票。

    金额有两个坑：一是发票含税而行程单未税，二是平台常在多处重复金额。
    优先取"价税合计"。
    """

    priority: ClassVar[int] = 30
    keywords: ClassVar[tuple[str, ...]] = (
        "网约车",
        "滴滴",
        "出租车",
        "出行",
        "T3出行",
        "曹操出行",
    )
    type_name: ClassVar[DocType] = DocType.RIDE

    amount_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("价税合计", (r"价税合计[^\d¥￥]*[¥￥]?\s*([\d,]+\.?\d*)",)),
        FieldRule("合计", (r"合\s*计[^\d¥￥]*[¥￥]?\s*([\d,]+\.?\d*)",)),
    )

    def is_invoice(self) -> bool:
        """行程单不是发票。"""
        return not re.search(r"行程单|行程报销单", self.text[:2000])

    def extract_parties(self) -> tuple[str, str, str]:
        platform = ""
        for pattern in (
            r"(?:销售方名称|收款方)[:：]?\s*([一-龥A-Za-z（）()]{4,40}?公司)",
            r"([一-龥]{2,8}(?:出行|用车|科技|信息服务)?有限公司)",
        ):
            match = re.search(pattern, self.text)
            if match:
                platform = match.group(1).strip()
                break
        return "", platform, "网约车服务"


# ---------------------------------------------------------------------------
# 网约车 / 出租车行程单（凭证）
# ---------------------------------------------------------------------------


class RideItineraryExtractor(BaseExtractor):
    """网约车行程单 / 报销凭证。"""

    priority: ClassVar[int] = 31
    keywords: ClassVar[tuple[str, ...]] = ("行程单", "行程报销单", "用车明细")
    type_name: ClassVar[DocType] = DocType.RIDE_ITINERARY

    amount_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("合计", (r"(?:总金额|合计金额|合计)[:：]?\s*[¥￥]?\s*([\d,]+\.?\d*)",)),
    )

    def is_invoice(self) -> bool:
        return False  # 凭证


# ---------------------------------------------------------------------------
# 住宿发票 / 酒店水单
# ---------------------------------------------------------------------------


class HotelInvoiceExtractor(BaseExtractor):
    """住宿发票。"""

    priority: ClassVar[int] = 40
    keywords: ClassVar[tuple[str, ...]] = (
        "住宿服务",
        "住宿费",
        "酒店",
        "宾馆",
        "民宿",
        "旅馆",
    )
    type_name: ClassVar[DocType] = DocType.HOTEL_INVOICE

    amount_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("价税合计", (r"价税合计[^\d¥￥]*[¥￥]?\s*([\d,]+\.?\d*)",)),
        FieldRule("合计", (r"合\s*计[^\d¥￥]*[¥￥]?\s*([\d,]+\.?\d*)",)),
    )

    product_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("项目", (r"(?:货物或应税劳务|项目)名\s*称[:：]?\s*([^\n]{2,40})",)),
        FieldRule("项目", (r"\*住宿服务\*",)),
    )

    def extract_parties(self) -> tuple[str, str, str]:
        hotel = ""
        match = re.search(
            r"([一-龥A-Za-z（）()]{4,40}?(?:酒店|宾馆|民宿|旅馆|大酒店))", self.text
        )
        if match:
            hotel = match.group(1).strip()
        product = ""
        for rule in self.product_rules:
            value, _ = rule.apply(self.text)
            if isinstance(value, str) and value:
                product = value.replace("*", "").strip()[:40]
                break
        return "", hotel, product


class HotelFolioExtractor(BaseExtractor):
    """住宿水单 / 账单 / 结账单。"""

    priority: ClassVar[int] = 41
    keywords: ClassVar[tuple[str, ...]] = (
        "水单",
        "结账单",
        "住宿明细",
        "消费账单",
        "folio",
    )
    type_name: ClassVar[DocType] = DocType.HOTEL_FOLIO

    amount_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule(
            "合计",
            (r"(?:总金额|消费合计|合计金额|合计)[:：]?\s*[¥￥]?\s*([\d,]+\.?\d*)",),
        ),
    )

    def is_invoice(self) -> bool:
        return False  # 凭证


# ---------------------------------------------------------------------------
# 餐饮
# ---------------------------------------------------------------------------


class MealExtractor(BaseExtractor):
    """餐饮发票。"""

    priority: ClassVar[int] = 50
    keywords: ClassVar[tuple[str, ...]] = ("餐饮服务", "餐费", "饭店", "餐厅", "美食")
    type_name: ClassVar[DocType] = DocType.MEAL

    amount_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("价税合计", (r"价税合计[^\d¥￥]*[¥￥]?\s*([\d,]+\.?\d*)",)),
        FieldRule("合计", (r"合\s*计[^\d¥￥]*[¥￥]?\s*([\d,]+\.?\d*)",)),
    )


# ---------------------------------------------------------------------------
# 过路费/ 通行费
# ---------------------------------------------------------------------------


class TollExtractor(BaseExtractor):
    """高速公路通行费电子发票。"""

    priority: ClassVar[int] = 60
    keywords: ClassVar[tuple[str, ...]] = ("通行费", "高速公路", "过路费", "路桥")
    type_name: ClassVar[DocType] = DocType.TOLL

    amount_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("价税合计", (r"价税合计[^\d¥￥]*[¥￥]?\s*([\d,]+\.?\d*)",)),
        FieldRule("合计", (r"合\s*计[^\d¥￥]*[¥￥]?\s*([\d,]+\.?\d*)",)),
    )


# ---------------------------------------------------------------------------
# 定额发票
# ---------------------------------------------------------------------------


class QuotaInvoiceExtractor(BaseExtractor):
    """定额发票（通用机打发票）。"""

    priority: ClassVar[int] = 70
    keywords: ClassVar[tuple[str, ...]] = ("定额发票", "通用机打发票")
    type_name: ClassVar[DocType] = DocType.QUOTA

    amount_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("金额", (r"(?:价税合计|合计)[:：]?\s*[¥￥]?\s*([\d,]+\.?\d*)",)),
    )

    def extract_parties(self) -> tuple[str, str, str]:
        # 定额发票通常无购方名称
        seller = ""
        match = re.search(r"([一-龥]{2,20}(?:财政局|税务局|定额发票))", self.text)
        if match:
            seller = match.group(1)
        return "", seller, ""


# ---------------------------------------------------------------------------
# 增值税发票（兜底）
# ---------------------------------------------------------------------------


class VatInvoiceExtractor(BaseExtractor):
    """增值税普通/专用发票 —— 也是全场兜底提取器。

    这是整个引擎准确率的关键：增值税发票版面高度标准化，
    但 PDF 文本流顺序不可信，所以买卖方必须靠坐标分栏而非正则顺序。

    优先级定为最高（5）：增值税发票版式国家统一、字段最全
    （金额/税额/日期/号码/代码都有），先由它尝试，
    实在不是增值税票再交给业务分类票种，最后由它兜底。

    专票与普票靠"专用发票"/"普通发票"关键词区分。
    """

    priority: ClassVar[int] = 5
    keywords: ClassVar[tuple[str, ...]] = (
        "增值税",
        "电子发票",
        "数电票",
        "普通发票",
        "专用发票",
    )
    type_name: ClassVar[DocType] = DocType.DIGITAL

    amount_rules: ClassVar[tuple[FieldRule, ...]] = (
        # 价税合计最可靠
        FieldRule("价税合计", (r"价税合计[^\d¥￥]*[¥￥]?\s*([\d,]+\.?\d*)",)),
        FieldRule("小写", (r"[（(]\s*小\s*写\s*[)）]\s*[¥￥]?\s*([\d,]+\.?\d*)",)),
        FieldRule("合计", (r"合\s*计[^\d¥￥]*[¥￥]?\s*([\d,]+\.?\d*)",)),
    )

    date_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule(
            "开票日期", (r"开票日期[:：]?\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)",)
        ),
        FieldRule("开票日期", (r"开票日期[:：]?\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})",)),
    )

    code_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule("发票号码", (r"发\s*票\s*号\s*码[:：]?\s*(\d{8,20})",)),
        FieldRule("号码", (r"(\d{20})",)),  # 数电票号码固定20 位
    )

    product_rules: ClassVar[tuple[FieldRule, ...]] = (
        FieldRule(
            "项目名称", (r"(?:货物或应税劳务|劳务或服务)名\s*称[:：]?\s*([^\n]{2,40})",)
        ),
        FieldRule("星号项", (r"\*([^*\s][^*]{1,30})\*",)),
    )

    def __init__(self, doc, path) -> None:
        super().__init__(doc, path)
        # 专票/普票区分
        if re.search(r"专\s*用\s*发\s*票|增值税专用发票", self.text):
            self.type_name = DocType.VAT_SPECIAL
        elif re.search(r"普\s*通\s*发\s*票|增值税普通发票", self.text):
            self.type_name = DocType.VAT_NORMAL

    def _amount_fallback(self) -> Decimal | None:
        """价税合计解析失败时，取文本中最大的合理金额。

        三重防御（都是实测踩出来的坑）：

        1. 排除长数字串—— 20 位数字是发票号码，不是金额。
           不排除会得到 ``25377000000098765432.00`` 这种荒谬金额。
        2. 金额必须有小数位 —— 税额、金额、单价都带两位小数，
           裸整数多半是号码或代码。
        3. 合理区间 —— 不超过 1000 万，避免把纳税人识别号当金额。
        """
        candidates = []
        for raw in re.findall(r"[¥￥]\s*([\d,]+\.\d{2})", self.text):
            # 纯数字部分去掉分隔符后过长 -> 是号码/代码
            digits = raw.replace(",", "")
            if len(digits.replace(".", "")) > 12:
                continue
            value = parse_amount(raw)
            if value is not None and Decimal(0) < value < Decimal("10000000"):
                candidates.append(value)

        if candidates:
            return max(candidates)

        # 最后兜底：明确的"小写"金额行（无 ¥ 符号）
        match = re.search(r"[（(]\s*小\s*写\s*[)）]\s*([\d,]+\.\d{2})", self.text)
        if match:
            return parse_amount(match.group(1))
        return None

    def extract_number(self) -> str:
        """提取发票代码（12 位）。数电票没有此字段。"""
        match = re.search(r"发\s*票\s*代\s*码[:：]?\s*(\d{10,12})", self.text)
        return match.group(1) if match else ""

    def build_vat(self):
        """构建时补充发票代码。"""
        invoice = super().build()
        invoice.invoice_number = self.extract_number()
        # 增值税税率
        rate = re.search(r"(\d{1,2}(?:\.\d)?)\s*%", self.text)
        if rate:
            invoice.tax_rate = f"{rate.group(1)}%"
        return invoice

    def build(self):
        return self.build_vat()


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

ALL_EXTRACTORS: list[type[BaseExtractor]] = [
    TrainTicketExtractor,
    FlightTicketExtractor,
    RideInvoiceExtractor,
    RideItineraryExtractor,
    HotelInvoiceExtractor,
    HotelFolioExtractor,
    MealExtractor,
    TollExtractor,
    QuotaInvoiceExtractor,
    VatInvoiceExtractor,
]


def get_extractor(text: str) -> type[BaseExtractor] | None:
    """按文本内容选择提取器（优先级顺序，首个命中即返回）。

    委托给各提取器自己的 :meth:`~invoice_master.extractor.BaseExtractor.matches`，
    该方法只看票面标题区，避免商品明细污染票种判定。
    """
    for extractor_cls in sorted(ALL_EXTRACTORS, key=lambda c: c.priority):
        try:
            if extractor_cls.matches(text):
                return extractor_cls
        except Exception:  # 单个规则出错不影响其他
            continue
    return None
