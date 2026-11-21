"""字段提取与解析单元测试。

覆盖两类：
1. 纯函数（parse_amount / parse_date / 文件名生成）—— 无需 PDF
2. 真实 PDF 提取 —— 用 tests/samples/ 下的样本

运行::

    pytest tests/ -v
    python -m pytest tests/test_extractor.py -v
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_master import pdf as pdfmod  # noqa: E402
from invoice_master.extractor import Extractor  # noqa: E402
from invoice_master.models import (  # noqa: E402
    DocType,
    Invoice,  # noqa: E402
    companies_match,
    format_amount,
    parse_amount,
    parse_date,
)
from invoice_master.reporter import build_filename, sanitize_filename  # noqa: E402

SAMPLES = Path(__file__).parent / "samples"


# ---------------------------------------------------------------------------
# parse_amount —— 金额是本项目最容易出错的地方
# ---------------------------------------------------------------------------


class TestParseAmount:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1234.56", Decimal("1234.56")),
            ("¥1,234.56", Decimal("1234.56")),
            ("￥1,234.56 元", Decimal("1234.56")),
            ("CNY 100.00", Decimal("100.00")),
            ("RMB100", Decimal("100")),
            ("  88.80  ", Decimal("88.80")),
            ("(100.00)", Decimal("-100.00")),  # 会计负数
            ("１０２４.５６", Decimal("1024.56")),  # 全角
            ("6%", Decimal("6")),
            (Decimal("42.5"), Decimal("42.5")),
            (45.5, Decimal("45.5")),
        ],
    )
    def test_valid(self, raw, expected):
        assert parse_amount(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "  ",
            "未知",
            "N/A",
            "null",
            "-",
            "abc",
            "(100.00",
            "100.00)",
            "()",
            True,  # 布尔不该被当金额
            [1, 2],
        ],
    )
    def test_invalid(self, raw):
        assert parse_amount(raw) is None

    def test_never_returns_zero_for_unparseable(self):
        """0 和"没解析出来"必须可区分 —— 报销场景的核心要求。"""
        assert parse_amount("乱码") is None
        assert parse_amount("0") == Decimal("0")

    def test_no_float_precision_loss(self):
        """金额累加不能用 float —— 这是本项目不用 float 的根本原因。"""
        total = sum((parse_amount(v) or Decimal(0)) for v in ["0.1", "0.2"])
        assert total == Decimal("0.3")
        assert 0.1 + 0.2 != 0.3  # 展示 float 的问题


# ---------------------------------------------------------------------------
# parse_date
# ---------------------------------------------------------------------------


class TestParseDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-03-15", date(2026, 3, 15)),
            ("2026年3月15日", date(2026, 3, 15)),
            ("2026/03/15", date(2026, 3, 15)),
            ("2026.3.15", date(2026, 3, 15)),
            ("20260315", date(2026, 3, 15)),
            ("2026年03月15日", date(2026, 3, 15)),
        ],
    )
    def test_valid(self, raw, expected):
        assert parse_date(raw) == expected

    @pytest.mark.parametrize(
        "raw", [None, "", "未知日期", "2026-13-45", "abc", "2026-02-30"]
    )
    def test_invalid(self, raw):
        assert parse_date(raw) is None


# ---------------------------------------------------------------------------
# 公司名比较
# ---------------------------------------------------------------------------


class TestCompanyMatch:
    @pytest.mark.parametrize(
        "a,b,expected",
        [
            ("深圳某某科技有限公司", "深圳某某科技有限公司", True),
            ("深圳某某科技有限公司", "深圳某某科技", True),  # 后缀差异
            ("北京小桔科技有限公司", "北京小桔科技有限公司", True),
            ("广州A公司", "北京B公司", False),
            ("", "北京B公司", False),
        ],
    )
    def test_match(self, a, b, expected):
        assert companies_match(a, b) is expected


# ---------------------------------------------------------------------------
# DocType 归一
# ---------------------------------------------------------------------------


class TestDocTypeNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("铁路电子客票", DocType.TRAIN),
            ("航空运输电子客票行程单", DocType.FLIGHT),
            ("网约车服务费", DocType.RIDE),
            ("住宿服务", DocType.HOTEL_INVOICE),
            ("增值税专用发票", DocType.VAT_SPECIAL),
            ("增值税普通发票", DocType.VAT_NORMAL),
            ("通行费", DocType.TOLL),
            ("定额发票", DocType.QUOTA),
            ("完全无关的文本", DocType.OTHER),
        ],
    )
    def test_normalize(self, raw, expected):
        assert DocType.normalize(raw) is expected


# ---------------------------------------------------------------------------
# 文件名生成
# ---------------------------------------------------------------------------


class TestFilename:
    def test_sanitize_illegal_chars(self):
        assert "<" not in sanitize_filename("a<b>c:d")
        assert "/" not in sanitize_filename("a/b\\c")

    def test_sanitize_reserved_name(self):
        # Windows 保留设备名
        assert sanitize_filename("CON").upper().startswith("_")

    def test_sanitize_length(self):
        assert len(sanitize_filename("啊" * 300)) <= 120

    def _invoice(self, **kwargs):
        defaults = dict(
            path=Path("x.pdf"),
            doc_type=DocType.HOTEL_INVOICE,
            amount=Decimal("888.00"),
            invoice_date=date(2026, 3, 15),
            seller="深圳湾万怡酒店",
        )
        defaults.update(kwargs)
        return Invoice(**defaults)

    def test_basic_template(self):
        name = build_filename(
            self._invoice(), "{date}-{type}-{amount}元", extension=".pdf"
        )
        assert name == "2026-03-15-住宿发票-888.00元.pdf"

    def test_index_with_format_spec(self):
        """模板含 {index:03d} 时不能报格式错（曾是真bug）。"""
        name = build_filename(
            self._invoice(), "inv-{index:03d}", extension=".pdf", index=7
        )
        assert name == "inv-007.pdf"

    def test_unknown_placeholder_raises(self):
        from invoice_master.reporter import RenameError

        with pytest.raises(RenameError):
            build_filename(self._invoice(), "{nonexistent}", extension=".pdf")

    def test_missing_date_uses_placeholder(self):
        name = build_filename(
            self._invoice(invoice_date=None), "{date}", extension=".pdf"
        )
        assert "未知日期" in name


# ---------------------------------------------------------------------------
# 真实 PDF 提取
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not SAMPLES.exists(), reason="样本 PDF 不存在")
class TestExtraction:
    @pytest.fixture(scope="class")
    @classmethod
    def extractor(cls):
        return Extractor(max_pages=20, fallback_to_mtime=False)

    def _extract(self, extractor, name):
        result = extractor.extract(SAMPLES / name)
        assert result.ok, f"{name} 抽取失败: {result.reason}"
        return result.invoice

    def test_vat_special_invoice(self, extractor):
        """增值税专票 —— 字段最全，是引擎准确率的关键。"""
        inv = self._extract(extractor, "test_vat.pdf")
        assert inv.doc_type is DocType.VAT_SPECIAL
        assert inv.amount == Decimal("1060.00")
        assert inv.tax_amount == Decimal("60.00")
        assert inv.invoice_date == date(2026, 3, 15)
        assert inv.invoice_code == "25377000000012345678"
        assert inv.invoice_number == "4400193130"

    def test_vat_purchaser_and_seller_not_swapped(self, extractor):
        """买卖方不能反 —— 单栏页面曾把整页文本塞进 seller。"""
        inv = self._extract(extractor, "test_vat.pdf")
        assert "深圳某某科技有限公司" == inv.purchaser
        assert "广州某某商务服务有限公司" == inv.seller

    def test_company_name_not_truncated(self, extractor):
        """公司名不能被截断 —— '有限公司'曾被吃掉只剩'科技'。"""
        inv = self._extract(extractor, "test_vat.pdf")
        for name in (inv.purchaser, inv.seller):
            assert name
            assert name.endswith(("公司", "酒店", "集团", "企业")), name

    def test_invoice_number_not_treated_as_amount(self, extractor):
        """20 位号码不能被当成金额（曾出现 25377000000098765432.00）。"""
        inv = self._extract(extractor, "test_ride.pdf")
        assert inv.amount is not None
        assert inv.amount < Decimal("10000000")
        assert inv.amount == Decimal("45.50")

    def test_train_ticket(self, extractor):
        inv = self._extract(extractor, "test_train.pdf")
        assert inv.doc_type is DocType.TRAIN
        assert inv.amount == Decimal("553.00")
        assert inv.invoice_date == date(2026, 4, 1)
        assert inv.tax_amount is None  # 火车票无税额
        assert inv.route.origin == "北京南"
        assert inv.route.destination == "上海虹桥"

    def test_doc_type_not_polluted_by_line_items(self, extractor):
        """商品明细里的 '*餐饮服务*餐费' 曾把专票误判成餐饮票。"""
        inv = self._extract(extractor, "test_vat.pdf")
        assert inv.doc_type is DocType.VAT_SPECIAL

    def test_confidence(self, extractor):
        inv = self._extract(extractor, "test_vat.pdf")
        assert inv.confidence >= 0.9

    def test_batch_extraction(self, extractor):
        files = sorted(SAMPLES.glob("test_*.pdf"))
        results = extractor.extract_many(files)
        assert len(results) == len(files)
        assert all(r.ok for r in results)


# ---------------------------------------------------------------------------
# 列分栏
# ---------------------------------------------------------------------------


class TestSplitColumns:
    def test_single_column_returns_empty(self, extractor=None):
        """单栏页面必须返回空 —— 强行分栏会把整页塞进一栏。"""
        from invoice_master.pdf import Word

        words = [Word("发票", 50, 10, 90, 30), Word("金额", 50, 50, 90, 70)]
        left, right = pdfmod.split_columns(words)
        assert left == "" and right == ""

    def test_two_column_detected(self):
        from invoice_master.pdf import Word

        words = []
        for i in range(10):
            words.append(Word(f"左{i}", 50 + i * 5, 10 + i * 20, 90 + i * 5, 30 + i * 20))
            words.append(
                Word(f"右{i}", 400 + i * 5, 10 + i * 20, 440 + i * 5, 30 + i * 20)
            )
        left, right = pdfmod.split_columns(words)
        assert left and right
        assert "左" in left and "右" in right


# ---------------------------------------------------------------------------
# 格式输出
# ---------------------------------------------------------------------------


class TestFormat:
    def test_format_amount(self):
        assert format_amount(Decimal("1234.5")) == "1234.50"
        assert format_amount(None) == ""

    def test_format_amount_negative(self):
        assert format_amount(Decimal("-50")) == "-50.00"
