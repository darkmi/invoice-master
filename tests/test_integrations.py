"""统一提取适配层测试（InvoiceFlowAI / piaoyihe → toolkit Invoice 映射）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_master.integrations import (
    ENGINES,
    EngineUnavailable,
    LocalExtractor,
    legacy_payload_to_invoice,
    make_extractor,
    piaoyihe_payload_to_invoice,
)
from invoice_master.models import DocType


SAMPLE = Path("/tmp/invoice.pdf")


class TestLegacyPayloadMapping:
    """InvoiceFlowAI legacy dict → toolkit Invoice。"""

    def test_full_mapping(self):
        inv = legacy_payload_to_invoice(
            {
                "Date": "20260315",
                "Amount": "¥1,234.56",
                "Tax": "78.00",
                "Seller": "某某公司",
                "Purchaser": "我司",
                "Type": "火车票",
                "InvoiceCode": "12345",
                "InvoiceNumber": "12345678901234567890",
                "Departure_City": "北京",
                "Destination_City": "上海",
                "Departure_Date": "2026/03/14",
            },
            SAMPLE,
            "dual:track_a",
        )
        assert inv.amount == Decimal("1234.56")
        assert inv.tax_amount == Decimal("78.00")
        assert inv.invoice_date == date(2026, 3, 15)
        assert inv.doc_type is DocType.TRAIN
        assert inv.route.origin == "北京" and inv.route.destination == "上海"
        assert inv.route.travel_date == date(2026, 3, 14)
        assert inv.engine == "dual:track_a"

    def test_iso_date_and_invoice_number(self):
        inv = legacy_payload_to_invoice(
            {"Date": "2026-04-01", "Amount": 100, "InvoiceNumber": "AB-123"},
            SAMPLE,
            "dual:track_b",
        )
        assert inv.invoice_date == date(2026, 4, 1)
        assert inv.invoice_number == "AB123"
        assert inv.doc_type is DocType.UNKNOWN


class TestPiaoyiheMapping:
    """票易合 extract_all() dict → toolkit Invoice。"""

    def test_taxi_invoice(self):
        inv = piaoyihe_payload_to_invoice(
            {
                "amount": 99.9,
                "tax_amount": "6.00",
                "invoice_date": "2026年3月16日",
                "invoice_type": "出租车发票",
                "buyer_name": "我司",
                "seller_name": "出租公司",
                "invoice_code": "12345",
            },
            SAMPLE,
            "TaxiInvoiceExtractor",
        )
        assert inv is not None
        assert inv.amount == Decimal("99.9")
        assert inv.invoice_date == date(2026, 3, 16)
        assert inv.doc_type is DocType.RIDE
        assert inv.engine == "piaoyihe:TaxiInvoiceExtractor"

    def test_zero_amount_means_missing(self):
        """票易合用 0.0 表示提取失败，不能当成真实金额。"""
        assert piaoyihe_payload_to_invoice({"amount": 0, "invoice_date": ""}, SAMPLE) is None
        inv = piaoyihe_payload_to_invoice(
            {"amount": 0, "invoice_date": "20260316", "seller_name": "某公司"}, SAMPLE
        )
        assert inv is not None and inv.amount is None


class TestEngineFactory:
    """引擎工厂与降级。"""

    def test_engines_constant(self):
        assert ENGINES == ("local", "hybrid", "dual", "piaoyihe")

    def test_local_engine(self):
        from invoice_master.config import Config

        engine = make_extractor(Config(), "local")
        assert isinstance(engine, LocalExtractor)
        assert hasattr(engine, "extract_one")
        assert hasattr(engine, "extract_many")

    def test_unknown_engine(self):
        from invoice_master.config import Config

        with pytest.raises(ValueError, match="未知引擎"):
            make_extractor(Config(), "nope")

    def test_dual_without_key_raises(self):
        import os
        from invoice_master.config import Config

        config = Config()
        saved = {k: os.environ.get(k) for k in ("INVOICEFLOW_GLM_API_KEY", "INVOICE_MASTER_LLM_API_KEY")}
        try:
            os.environ.pop("INVOICEFLOW_GLM_API_KEY", None)
            os.environ.pop("INVOICE_MASTER_LLM_API_KEY", None)
            config.set("llm.api_key", "")
            with pytest.raises(EngineUnavailable, match="GLM API key"):
                make_extractor(config, "dual")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
