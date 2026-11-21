"""Agent 原生视觉识别桥接 —— 不配置远程 GLM API 的 LLM 路线。

为什么需要这个模块
------------------
本 skill 由 agent 执行，CLI 只是agent 调用的子进程。子进程**不能**
直接拿到 agent 的 LLM；让 CLI 去调 GLM/OpenAI 反而多此一举。
正确做法是两段式握手：

1. ``agent-prepare`` 把 PDF 渲染成 PNG，生成本地提取提示（hint）和
   统一提示词，写成 ``request.json``；
2. agent 用**自己的多模态能力**查看 ``request.json`` 里的每张图片，
   把字段 JSON 写入 ``answers.json``；
3. ``agent-apply`` 读取两份 JSON，复用 :class:`~invoice_master.llm.LLMExtractor`
   的解析与交叉校验逻辑，产出与 ``extract`` 完全相同的结果结构。

这不会绕过模型服务商：图片仍由当前 agent 所属的模型处理。
它省掉的是**额外配置一套 GLM/OpenAI API Key 和远程调用**。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from . import pdf as pdfmod
from .config import Config
from .extractor import ExtractResult, Extractor
from .llm import LLMExtractor
from .models import (
    DocType,
    Invoice,
    parse_amount,
    parse_date,
)

logger = logging.getLogger(__name__)

SCHEMA = "invoice-master/agent-extract/v1"

#: 答案字段白名单。用于区分「字段字典」与 ``{"id": ..., "path": ...}`` 这类仅含定位信息的对象。
FIELD_KEYS = frozenset(
    {
        "doc_type",
        "amount",
        "tax_amount",
        "invoice_date",
        "invoice_code",
        "invoice_number",
        "purchaser",
        "seller",
        "product",
        "tax_rate",
        "origin",
        "destination",
        "is_invoice",
    }
)

INSTRUCTIONS = (
    "请用你自己的多模态视觉能力逐张查看 tasks[*].image，按 prompt 抽取字段。"
    "不要调用任何远程 GLM/OpenAI API。把结果写成 answers.json，"
    "格式为 {\"answers\": [{\"id\": \"<task.id>\", \"fields\": {...}}]}。"
)


# ---------------------------------------------------------------------------
# JSON 读写
# ---------------------------------------------------------------------------


def _read_json(source: str | Path | dict | list) -> dict | list:
    """接受路径或已解析对象，统一返回 dict/list。"""
    if isinstance(source, (dict, list)):
        return source
    path = Path(source).expanduser()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败 ({path}): {exc}") from exc


def _write_json(target: str | Path, payload: Any) -> Path:
    path = Path(target).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def _task_id(path: Path) -> str:
    """用绝对路径生成稳定短 id，避免 agent 混淆同名文件。"""
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 阶段一：生成任务包
# ---------------------------------------------------------------------------


def build_agent_request(
    paths: Iterable[str | Path],
    *,
    config: Config,
    images_dir: str | Path,
    dpi: int = 150,
    max_px: int = 0,
) -> dict[str, Any]:
    """把 PDF 列表转成 agent 可消费的识别任务包。

    Args:
        paths: PDF 文件路径
        config: toolkit 配置（仅用于本地提示提取与图片尺寸）
        images_dir: 渲染图片保存目录
        dpi: 渲染精度
        max_px: 图片最长边上限；0 表示沿用 ``llm.max_image_px``

    Returns:
        可直接写入 JSON 的字典
    """
    image_dir = Path(images_dir).expanduser().resolve()
    image_dir.mkdir(parents=True, exist_ok=True)

    section = config.section("extraction")
    local = Extractor(
        max_pages=int(section.get("max_pdf_pages", 20)),
        fallback_to_mtime=bool(section.get("fallback_to_mtime", True)),
    )
    pixel_limit = int(max_px or config.get("llm.max_image_px", 1600) or 0)

    tasks: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        identifier = _task_id(path)
        task: dict[str, Any] = {
            "id": identifier,
            "path": str(path),
            "filename": path.name,
            "image": "",
        }

        try:
            png = pdfmod.render_page_image(path, dpi=dpi, max_px=pixel_limit)
            image_path = image_dir / f"{identifier}.png"
            image_path.write_bytes(png)
            task["image"] = str(image_path)
        except pdfmod.PdfError as exc:
            task["error"] = str(exc)

        local_result = local.extract(path)
        task["hint"] = {
            "reason": local_result.reason,
            "warnings": list(local_result.warnings),
            "invoice": local_result.invoice.to_row() if local_result.invoice else None,
        }
        task["doctype_hint"] = (
            local_result.invoice.doc_type.value if local_result.invoice else ""
        )
        tasks.append(task)

    return {
        "schema": SCHEMA,
        "instructions": INSTRUCTIONS,
        "prompt": LLMExtractor.PROMPT_TEMPLATE,
        "answer_format": {
            "answers": [
                {
                    "id": "<task.id>",
                    "fields": {
                        "doc_type": "",
                        "amount": "",
                        "tax_amount": "",
                        "invoice_date": "",
                        "invoice_code": "",
                        "invoice_number": "",
                        "purchaser": "",
                        "seller": "",
                        "product": "",
                        "tax_rate": "",
                        "origin": "",
                        "destination": "",
                        "is_invoice": True,
                    },
                }
            ]
        },
        "images_dir": str(image_dir),
        "tasks": tasks,
    }


def write_agent_request(
    paths: Iterable[str | Path],
    *,
    config: Config,
    request_path: str | Path,
    images_dir: str | Path | None = None,
    dpi: int = 150,
    max_px: int = 0,
) -> dict[str, Any]:
    """生成任务包并落盘。"""
    target = Path(request_path).expanduser().resolve()
    image_dir = Path(images_dir).expanduser() if images_dir else target.parent / "agent_images"
    request = build_agent_request(
        paths, config=config, images_dir=image_dir, dpi=dpi, max_px=max_px
    )
    _write_json(target, request)
    return request


# ---------------------------------------------------------------------------
# 阶段二：应用 agent 答案
# ---------------------------------------------------------------------------


def invoice_from_row(row: dict[str, Any] | None, path: Path) -> Invoice | None:
    """把 ``Invoice.to_row()`` 的中文列还原成 ``Invoice``。

    用于 ``report --from-extract`` 和 agent 任务中的本地提示。
    """
    if not row:
        return None
    return Invoice(
        path=path,
        doc_type=DocType.normalize(row.get("类型") or row.get("doc_type") or ""),
        amount=parse_amount(row.get("金额") if "金额" in row else row.get("amount")),
        tax_amount=parse_amount(
            row.get("税额") if "税额" in row else row.get("tax_amount")
        ),
        invoice_date=parse_date(
            row.get("开票日期") if "开票日期" in row else row.get("invoice_date")
        ),
        invoice_code=str(row.get("发票号码") or row.get("invoice_code") or ""),
        invoice_number=str(row.get("发票代码") or row.get("invoice_number") or ""),
        purchaser=str(row.get("购买方") or row.get("purchaser") or ""),
        seller=str(row.get("销售方") or row.get("seller") or ""),
        product=str(row.get("商品服务") or row.get("product") or ""),
        tax_rate=str(row.get("税率") or row.get("tax_rate") or ""),
        is_invoice=str(row.get("是否发票", "是")) != "否",
        confidence=float(row.get("置信度") or 0),
        engine=str(row.get("识别方式") or "local"),
    )


def index_answers(
    answers: str | Path | dict | list, tasks: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """把多种答案写法统一索引为 ``id/path/filename -> fields``。

    支持：

    * ``{"answers": [{"id": "...", "fields": {...}}]}``（推荐）
    * ``{"<task.id>": {...}}``
    * ``[{"path": "...", "fields": {...}}, ...]``
    * 与 ``tasks`` 等长的字段字典数组（按顺序配对）
    * 值为字符串时，自动剥离 markdown 代码块并解析 JSON
    """
    data = _read_json(answers)
    if isinstance(data, dict):
        items: list[Any] = data.get("answers") if isinstance(data.get("answers"), list) else []
        if not items:
            items = [
                {"id": key, "fields": value}
                for key, value in data.items()
                if isinstance(value, (dict, str))
            ]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("answers 根节点必须是对象或数组")

    indexed: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        aliases: list[str] = []
        fields: Any = None

        if isinstance(item, dict):
            for key in ("id", "path", "filename"):
                value = item.get(key)
                if value:
                    aliases.append(str(value))
            for key in ("fields", "answer", "data"):
                if key in item:
                    fields = item[key]
                    break
            if fields is None and any(key in item for key in FIELD_KEYS):
                fields = item
        elif isinstance(item, str):
            fields = item
        else:
            continue

        if isinstance(fields, str):
            fields = LLMExtractor._parse_response(fields)
        if not isinstance(fields, dict):
            continue

        if aliases:
            for alias in aliases:
                indexed[alias] = fields
        elif index < len(tasks):
            indexed[str(tasks[index].get("id", ""))] = fields

    return indexed


def _failure(path: Path, reason: str) -> ExtractResult:
    return ExtractResult(path=path, invoice=None, reason=reason)


def apply_agent_request(
    request: str | Path | dict,
    answers: str | Path | dict | list,
    config: Config,
) -> list[ExtractResult]:
    """把 agent 答案还原为 :class:`ExtractResult` 列表。

    本地提取结果始终优先：agent 只补本地没拿到的字段。
    """
    request_data = _read_json(request)
    if not isinstance(request_data, dict) or not isinstance(request_data.get("tasks"), list):
        raise ValueError("request 必须是包含 tasks 数组的 JSON 对象")

    tasks: list[dict[str, Any]] = request_data["tasks"]
    indexed = index_answers(answers, tasks)
    converter = LLMExtractor(config)
    results: list[ExtractResult] = []

    for task in tasks:
        raw_path = str(task.get("path") or "")
        if not raw_path:
            continue
        path = Path(raw_path)
        hint = task.get("hint") if isinstance(task.get("hint"), dict) else {}
        local = invoice_from_row(hint.get("invoice"), path)
        warnings = [str(w) for w in (hint.get("warnings") or [])]

        fields = (
            indexed.get(str(task.get("id", "")))
            or indexed.get(str(path))
            or indexed.get(path.name)
        )

        if fields is None:
            if task.get("error"):
                results.append(_failure(path, f"图片渲染失败: {task['error']}"))
            elif local is not None:
                local.warnings = warnings + ["agent 未提供答案，沿用本地结果"]
                results.append(
                    ExtractResult(
                        path=path, invoice=local, engine="local", warnings=local.warnings
                    )
                )
            else:
                results.append(_failure(path, "agent 未提供答案"))
            continue

        try:
            invoice, parse_warnings = converter._to_invoice(
                fields, path, local, engine="agent"
            )
        except (TypeError, ValueError) as exc:
            results.append(_failure(path, f"agent 答案字段格式错误: {exc}"))
            continue

        if invoice is None:
            if local is not None:
                local.warnings = warnings + parse_warnings + ["agent 未抽到有效金额"]
                results.append(
                    ExtractResult(path=path, invoice=local, warnings=local.warnings)
                )
            else:
                results.append(_failure(path, "; ".join(parse_warnings) or "agent 未抽到有效字段"))
            continue

        invoice.warnings = (
            warnings + parse_warnings + ["由 agent 多模态识别（未使用本地 GLM API）"]
        )
        results.append(
            ExtractResult(path=path, invoice=invoice, engine="agent", warnings=invoice.warnings)
        )

    return results
