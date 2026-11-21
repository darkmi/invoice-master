"""LLM 兜底模块测试。

分两层：

1. **离线单测**（默认跑）—— 用假的 HTTP 响应验证请求构造、
   JSON 解析、交叉校验、熔断逻辑。不消耗任何 token。
2. **真实联调**（需环境变量）—— 设置 ``INVOICE_MASTER_TEST_LLM_BASE_URL``、
   ``..._MODEL``、``..._API_KEY`` 后自动启用，验证整条链路真的能跑通。

真实联调方式::

    export INVOICE_MASTER_TEST_LLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4
    export INVOICE_MASTER_TEST_LLM_MODEL=glm-4v-flash
    export INVOICE_MASTER_TEST_LLM_API_KEY=your-key
    pytest tests/test_llm.py -v -k real

运行::

    pytest tests/test_llm.py -v
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_master import load_config  # noqa: E402
from invoice_master.extractor import Extractor, ExtractResult  # noqa: E402
from invoice_master.llm import (  # noqa: E402
    CircuitBreaker,
    HybridExtractor,
    LLMDisabled,
    LLMExtractor,
)
from invoice_master.models import DocType, Invoice  # noqa: E402

SAMPLES = Path(__file__).parent / "samples"

ENV_BASE = "INVOICE_MASTER_TEST_LLM_BASE_URL"
ENV_MODEL = "INVOICE_MASTER_TEST_LLM_MODEL"
ENV_KEY = "INVOICE_MASTER_TEST_LLM_API_KEY"


def _llm_config(**overrides):
    cfg = load_config(search_parents=False)
    cfg.set("llm.enabled", True)
    cfg.set("llm.base_url", "https://example.invalid/v4")
    cfg.set("llm.model", "fake-model")
    cfg.set("llm.api_key", "fake-key")
    cfg.set("llm.max_retries", 0)
    for key, value in overrides.items():
        cfg.set(key, value)
    return cfg


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_starts_closed(self):
        breaker = CircuitBreaker(threshold=3)
        assert breaker.is_open is False

    def test_opens_after_threshold(self):
        breaker = CircuitBreaker(threshold=3)
        for _ in range(3):
            breaker.record_failure("boom")
        assert breaker.is_open is True
        assert breaker.failures == 3

    def test_auth_error_trips_immediately(self):
        """认证/额度错误重试无意义，必须一次就熔断。"""
        breaker = CircuitBreaker(threshold=5)
        breaker.record_failure("HTTP 401: unauthorized")
        assert breaker.is_open is True
        assert breaker.failures == 1

    def test_quota_error_trips_immediately(self):
        breaker = CircuitBreaker(threshold=5)
        breaker.record_failure("HTTP 402: insufficient")
        assert breaker.is_open is True

    def test_success_resets(self):
        breaker = CircuitBreaker(threshold=3)
        breaker.record_failure("x")
        breaker.record_failure("x")
        breaker.record_success()
        assert breaker.failures == 0
        assert breaker.is_open is False

    def test_half_open_after_reset_window(self):
        breaker = CircuitBreaker(threshold=1, reset_after=0.05)
        breaker.record_failure("x")
        assert breaker.is_open is True
        import time

        time.sleep(0.1)
        # 超过 reset_after 后允许试探
        assert breaker.is_open is False


# ---------------------------------------------------------------------------
# 可用性与状态
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_disabled_by_default(self):
        llm = LLMExtractor(load_config(search_parents=False))
        assert llm.is_available() is False
        assert llm.status == "未启用"

    def test_enabled_without_key(self):
        llm = LLMExtractor(_llm_config(**{"llm.api_key": ""}))
        assert llm.is_available() is False
        assert "API Key" in llm.status

    def test_call_limit_blocks(self):
        llm = LLMExtractor(_llm_config(**{"llm.max_calls_per_run": 5}))
        llm.calls_made = 5
        assert llm.is_available() is False
        assert "上限" in llm.status

    def test_breaker_blocks(self):
        llm = LLMExtractor(_llm_config())
        llm.breaker.record_failure("HTTP 401: bad key")
        assert llm.is_available() is False
        assert "熔断" in llm.status

    def test_ensure_ready_raises_when_disabled(self):
        llm = LLMExtractor(load_config(search_parents=False))
        with pytest.raises(LLMDisabled):
            llm.ensure_ready()


# ---------------------------------------------------------------------------
# 触发判断（决定何时花钱）
# ---------------------------------------------------------------------------


class TestNeedsLlm:
    @pytest.fixture
    def llm(self):
        return LLMExtractor(_llm_config())

    def test_disabled_blocks_even_for_scanned(self):
        """扫描件本该触发兜底，但 LLM 未启用时必须拒绝调用。"""
        disabled = LLMExtractor(load_config(search_parents=False))
        result = ExtractResult(path=Path("x.pdf"), invoice=None, reason="no_text_layer")
        need, reason = disabled.needs_llm("x.pdf", result)
        assert need is False
        assert "不可用" in reason

    def test_scanned_needs_llm(self, llm):
        result = ExtractResult(path=Path("x.pdf"), invoice=None, reason="no_text_layer")
        need, reason = llm.needs_llm("x.pdf", result)
        assert need is True
        assert "扫描件" in reason

    def test_missing_amount_needs_llm(self, llm):
        invoice = Invoice(
            path=Path("x.pdf"),
            doc_type=DocType.DIGITAL,
            amount=None,
            invoice_date=__import__("datetime").date(2026, 3, 15),
        )
        result = ExtractResult(path=Path("x.pdf"), invoice=invoice)
        need, reason = llm.needs_llm("x.pdf", result)
        assert need is True

    def test_confident_local_result_skips_llm(self, llm):
        """置信度足够时不该花钱调 API —— 这是省钱的关键。"""
        invoice = Invoice(
            path=Path("x.pdf"),
            doc_type=DocType.VAT_SPECIAL,
            amount=Decimal("1060.00"),
            invoice_date=__import__("datetime").date(2026, 3, 15),
            invoice_code="25377000000012345678",
            seller="某某公司",
        )
        invoice.confidence = 1.0
        result = ExtractResult(path=Path("x.pdf"), invoice=invoice)
        need, reason = llm.needs_llm("x.pdf", result)
        assert need is False
        assert "足够" in reason


# ---------------------------------------------------------------------------
# JSON 解析（模型输出不总是干净 JSON）
# ---------------------------------------------------------------------------


class TestResponseParsing:
    @pytest.mark.parametrize(
        "raw",
        [
            '{"amount": "1060.00"}',
            '```json\n{"amount": "1060.00"}\n```',
            '```\n{"amount": "1060.00"}\n```',
            '好的，结果如下：{"amount": "1060.00"} 以上。',
            '  {"amount": "1060.00"}  ',
        ],
    )
    def test_parse_variants(self, raw):
        parsed = LLMExtractor._parse_response(raw)
        assert parsed == {"amount": "1060.00"}

    @pytest.mark.parametrize("raw", ["", "完全不是 JSON", "{不合法}", None])
    def test_parse_failure(self, raw):
        assert LLMExtractor._parse_response(raw) is None

    def test_parse_array_rejected(self):
        """数组不是合法响应，不能误当字典用。"""
        assert LLMExtractor._parse_response("[1,2,3]") is None


# ---------------------------------------------------------------------------
# 交叉校验（本地优先）
# ---------------------------------------------------------------------------


class TestCrossValidation:
    def _local_invoice(self, **overrides):
        base = dict(
            path=Path("x.pdf"),
            doc_type=DocType.VAT_SPECIAL,
            amount=Decimal("1060.00"),
            invoice_date=__import__("datetime").date(2026, 3, 15),
            invoice_code="25377000000012345678",
            seller="本地识别的公司",
        )
        base.update(overrides)
        return Invoice(**base)

    @pytest.fixture
    def llm(self):
        return LLMExtractor(_llm_config())

    def test_local_amount_wins_on_conflict(self, llm):
        """本地与LLM 金额冲突时以本地为准 —— 正则对标准版面更可靠。"""
        from pathlib import Path as P

        local = self._local_invoice()
        llm_data = {
            "doc_type": "增值税普票",
            "amount": "999.00",
            "invoice_date": "2020-01-01",
            "seller": "LLM识别的公司",
            "purchaser": "",
            "invoice_code": "00000000000000000000",
        }
        invoice, warnings = llm._to_invoice(llm_data, P("x.pdf"), local)

        assert invoice.amount == Decimal("1060.00")  # 本地胜出
        assert any("金额不一致" in w for w in warnings)
        assert invoice.seller == "本地识别的公司"
        assert invoice.invoice_date == local.invoice_date

    def test_local_doc_type_wins(self, llm):
        from pathlib import Path as P

        local = self._local_invoice(doc_type=DocType.VAT_SPECIAL)
        llm_data = {"doc_type": "餐饮", "amount": "1060.00"}
        invoice, _ = llm._to_invoice(llm_data, P("x.pdf"), local)
        assert invoice.doc_type is DocType.VAT_SPECIAL

    def test_llm_fills_local_gaps(self, llm):
        """本地缺字段时，LLM 的结果应被采纳。"""
        from pathlib import Path as P

        local = self._local_invoice(amount=None, invoice_code="")
        llm_data = {
            "doc_type": "增值税专票",
            "amount": "1060.00",
            "invoice_code": "25377000000012345678",
            "seller": "某服务商",
        }
        invoice, _ = llm._to_invoice(llm_data, P("x.pdf"), local)
        assert invoice.amount == Decimal("1060.00")
        assert invoice.invoice_code == "25377000000012345678"

    def test_no_local_uses_llm_fully(self, llm):
        from pathlib import Path as P

        llm_data = {
            "doc_type": "住宿发票",
            "amount": "888.00",
            "invoice_date": "2026-02-10",
            "seller": "某酒店",
        }
        invoice, _ = llm._to_invoice(llm_data, P("x.pdf"), None)
        assert invoice.amount == Decimal("888.00")
        assert invoice.seller == "某酒店"
        assert invoice.doc_type is DocType.HOTEL_INVOICE

    def test_no_amount_at_all_returns_none(self, llm):
        from pathlib import Path as P

        invoice, warnings = llm._to_invoice(
            {"doc_type": "餐饮", "amount": "", "tax_amount": ""}, P("x.pdf"), None
        )
        assert invoice is None
        assert warnings

    def test_llm_confidence_is_capped(self, llm):
        """LLM 置信度固定中等，需人工抽检，不能标1.0。"""
        from pathlib import Path as P

        invoice, _ = llm._to_invoice(
            {"doc_type": "餐饮", "amount": "10.00"}, P("x.pdf"), None
        )
        assert invoice.confidence < 1.0


# ---------------------------------------------------------------------------
# HybridExtractor 集成（离线）
# ---------------------------------------------------------------------------


class TestHybridOffline:
    def test_local_result_passes_through(self):
        """LLM 未启用时，本地结果原样返回。"""
        cfg = load_config(search_parents=False)
        hybrid = HybridExtractor(cfg)
        result = hybrid.extract_one(SAMPLES / "test_vat.pdf")
        assert result.ok
        assert result.engine == "local"
        assert hybrid.llm.calls_made == 0

    def test_scanned_file_warns_without_llm(self, tmp_path):
        """未启用 LLM 时，扫描件应给出明确警告而非静默失败。"""
        pymupdf = pytest.importorskip("pymupdf")

        src = pymupdf.open(SAMPLES / "test_vat.pdf")
        scanned = tmp_path / "scanned.pdf"
        out = pymupdf.open()
        for i in range(src.page_count):
            page = src[i]
            new = out.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, pixmap=page.get_pixmap(dpi=100))
        out.save(scanned)
        out.close()
        src.close()

        cfg = load_config(search_parents=False)
        result = HybridExtractor(cfg).extract_one(scanned)

        assert not result.ok
        assert any("LLM" in w or "扫描件" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 真实 API 联调（需环境变量）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get(ENV_BASE) and os.environ.get(ENV_KEY)),
    reason=f"未设置 {ENV_BASE} / {ENV_KEY}，跳过真实 API 测试",
)
class TestRealAPI:
    """真实调用测试 —— 会消耗 token，默认跳过。"""

    @pytest.fixture
    def llm(self):
        cfg = load_config(search_parents=False)
        cfg.set("llm.enabled", True)
        cfg.set("llm.base_url", os.environ[ENV_BASE])
        cfg.set("llm.model", os.environ.get(ENV_MODEL, "glm-4v-flash"))
        cfg.set("llm.api_key", os.environ[ENV_KEY])
        cfg.set("llm.timeout", 90)
        cfg.set("llm.max_retries", 1)
        cfg.set("llm.max_image_px", 1400)
        return LLMExtractor(cfg)

    @pytest.fixture
    def scanned_pdf(self, tmp_path):
        pymupdf = pytest.importorskip("pymupdf")

        src = pymupdf.open(SAMPLES / "test_vat.pdf")
        path = tmp_path / "scanned.pdf"
        out = pymupdf.open()
        for i in range(src.page_count):
            page = src[i]
            new = out.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, pixmap=page.get_pixmap(dpi=150))
        out.save(path)
        out.close()
        src.close()
        return path

    def test_is_available(self, llm):
        assert llm.is_available(), f"LLM 不可用: {llm.status}"

    def test_extracts_fields_from_scanned(self, llm, scanned_pdf):
        """核心断言：扫描件能抽出与正则一致的字段。"""
        result = llm.extract(scanned_pdf)
        assert result.ok, f"抽取失败: {result.reason}"

        invoice = result.invoice
        # 与本地正则对同一张发票的抽取结果对照
        local = Extractor(fallback_to_mtime=False).extract(SAMPLES / "test_vat.pdf")
        assert local.ok
        expected = local.invoice

        assert invoice.amount == expected.amount, "金额应与正则一致"
        assert invoice.invoice_date == expected.invoice_date, "日期应与正则一致"
        assert invoice.invoice_code == expected.invoice_code, "号码应与正则一致"
        assert invoice.doc_type is expected.doc_type, "票种应与正则一致"

    def test_call_counter_increments(self, llm, scanned_pdf):
        before = llm.calls_made
        llm.extract(scanned_pdf)
        assert llm.calls_made == before + 1
        assert not llm.breaker.is_open

    def test_hybrid_uses_llm_for_scanned(self, scanned_pdf):
        """HybridExtractor 应识别扫描件并走LLM。"""
        cfg = load_config(search_parents=False)
        cfg.set("llm.enabled", True)
        cfg.set("llm.base_url", os.environ[ENV_BASE])
        cfg.set("llm.model", os.environ.get(ENV_MODEL, "glm-4v-flash"))
        cfg.set("llm.api_key", os.environ[ENV_KEY])
        cfg.set("llm.timeout", 90)

        hybrid = HybridExtractor(cfg)
        result = hybrid.extract_one(scanned_pdf)

        assert result.ok
        assert result.engine == "llm"
        assert hybrid.llm.calls_made == 1
