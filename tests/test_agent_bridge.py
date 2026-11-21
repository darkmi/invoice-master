"""Agent 原生视觉识别桥接测试。

重点验证：无需 GLM API Key 也能完成「准备 → 回填 → 标准结果」闭环，
且本地正则结果始终优先。全部离线，不调用任何模型。
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from invoice_master import agent_bridge  # noqa: E402
from invoice_master.config import Config  # noqa: E402
from invoice_master.extractor import ExtractResult  # noqa: E402
from invoice_master.models import DocType, Invoice  # noqa: E402


@pytest.fixture
def fake_pdf(monkeypatch, tmp_path):
    """绕过 PyMuPDF，用假渲染/假本地提取验证握手协议。"""
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr(
        agent_bridge.pdfmod,
        "render_page_image",
        lambda *args, **kwargs: b"\x89PNG\r\n\x1a\nfake",
    )
    monkeypatch.setattr(
        agent_bridge.Extractor,
        "extract",
        lambda self, p: ExtractResult(path=Path(p), invoice=None, reason="no_text_layer"),
    )
    return path


def _request(fake_pdf, tmp_path):
    return agent_bridge.build_agent_request(
        [fake_pdf],
        config=Config(),
        images_dir=tmp_path / "images",
        max_px=800,
    )


class TestBuildRequest:
    def test_request_has_prompt_image_and_id(self, fake_pdf, tmp_path):
        request = _request(fake_pdf, tmp_path)
        assert request["schema"] == agent_bridge.SCHEMA
        assert "不要调用任何远程" in request["instructions"]
        assert "JSON" in request["prompt"]

        task = request["tasks"][0]
        assert task["id"]
        assert task["path"] == str(fake_pdf.resolve())
        assert Path(task["image"]).is_file()
        assert task["hint"]["reason"] == "no_text_layer"
        assert task["hint"]["invoice"] is None

    def test_never_embeds_api_key(self, fake_pdf, tmp_path):
        cfg = Config()
        cfg.set("llm.api_key", "sk-should-not-leak")
        request = agent_bridge.build_agent_request(
            [fake_pdf], config=cfg, images_dir=tmp_path / "images"
        )
        assert "sk-should-not-leak" not in str(request)


class TestIndexAnswers:
    def test_indexes_by_id_path_and_filename(self, fake_pdf, tmp_path):
        request = _request(fake_pdf, tmp_path)
        task = request["tasks"][0]
        answers = {
            "answers": [
                {
                    "id": task["id"],
                    "path": task["path"],
                    "filename": task["filename"],
                    "fields": {"amount": "1060.00"},
                }
            ]
        }
        indexed = agent_bridge.index_answers(answers, request["tasks"])
        assert indexed[task["id"]]["amount"] == "1060.00"
        assert indexed[task["path"]]["amount"] == "1060.00"
        assert indexed[task["filename"]]["amount"] == "1060.00"

    def test_accepts_dict_and_markdown_json(self, fake_pdf, tmp_path):
        request = _request(fake_pdf, tmp_path)
        task_id = request["tasks"][0]["id"]
        answers = {task_id: '```json\n{"amount": "88.80"}\n```'}
        indexed = agent_bridge.index_answers(answers, request["tasks"])
        assert indexed[task_id]["amount"] == "88.80"

    def test_positional_list_maps_by_order(self, fake_pdf, tmp_path):
        request = _request(fake_pdf, tmp_path)
        indexed = agent_bridge.index_answers(
            [{"amount": "12.00"}], request["tasks"]
        )
        assert indexed[request["tasks"][0]["id"]]["amount"] == "12.00"


class TestApplyRequest:
    def test_applies_agent_fields_without_api_key(self, fake_pdf, tmp_path):
        request = _request(fake_pdf, tmp_path)
        task_id = request["tasks"][0]["id"]
        answers = {
            "answers": [
                {
                    "id": task_id,
                    "fields": {
                        "doc_type": "增值税专票",
                        "amount": "1060.00",
                        "seller": "某服务商",
                    },
                }
            ]
        }
        results = agent_bridge.apply_agent_request(request, answers, Config())

        assert len(results) == 1
        assert results[0].ok
        assert results[0].engine == "agent"
        assert results[0].invoice.amount == Decimal("1060.00")
        assert results[0].invoice.doc_type is DocType.VAT_SPECIAL
        assert any("agent" in w for w in results[0].invoice.warnings)

    def test_local_hint_wins_on_conflict(self, fake_pdf, tmp_path):
        request = _request(fake_pdf, tmp_path)
        task = request["tasks"][0]
        local = Invoice(
            path=fake_pdf,
            doc_type=DocType.VAT_SPECIAL,
            amount=Decimal("999.00"),
            seller="本地识别的公司",
            confidence=1.0,
        )
        task["hint"]["invoice"] = local.to_row()

        results = agent_bridge.apply_agent_request(
            request,
            {"answers": [{"id": task["id"], "fields": {"amount": "1060.00"}}]},
            Config(),
        )
        assert results[0].invoice.amount == Decimal("999.00")
        assert any("金额不一致" in w for w in results[0].invoice.warnings)

    def test_missing_answer_falls_back_to_local(self, fake_pdf, tmp_path):
        request = _request(fake_pdf, tmp_path)
        task = request["tasks"][0]
        local = Invoice(path=fake_pdf, amount=Decimal("50.00"))
        task["hint"]["invoice"] = local.to_row()

        results = agent_bridge.apply_agent_request(request, {"answers": []}, Config())
        assert results[0].ok
        assert results[0].engine == "local"
        assert any("未提供答案" in w for w in results[0].invoice.warnings)

    def test_missing_answer_without_local_is_failure(self, fake_pdf, tmp_path):
        request = _request(fake_pdf, tmp_path)
        results = agent_bridge.apply_agent_request(request, {"answers": []}, Config())
        assert not results[0].ok
        assert "未提供答案" in results[0].reason
