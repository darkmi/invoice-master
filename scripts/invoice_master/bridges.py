"""能力桥接层 —— 暴露两个参考项目中 toolkit 原本没有的独有能力。

本模块把参考项目里带 CLI/GUI 入口的逻辑包装成纯函数，供 toolkit 的统一
CLI 调用；参考代码本身**零改写**。

暴露的能力
----------
* ``audit_truth``       —— InvoiceFlowAI 真值审计（严格对照归档结果）
* ``validate_invoice_url`` —— InvoiceFlowAI 的 SSRF 防护 URL 校验
* ``recover_invoice_links`` —— InvoiceFlowAI 的链接发票恢复（邮件正文下载链接）
* ``run_mcp_server``    —— piaoyihe 的 FastMCP Server（9 个 tool）
* ``verify_batch``      —— InvoiceFlowAI 的批次准入校验

所有第三方依赖延迟导入，缺依赖时抛 :class:`BridgeUnavailable`。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .integrations import EngineUnavailable

logger = logging.getLogger(__name__)


class BridgeUnavailable(RuntimeError):
    """桥接能力缺少依赖或输入不完整。"""


# ---------------------------------------------------------------------------
# 真值审计
# ---------------------------------------------------------------------------


def audit_truth(
    truth_manifest: str | Path,
    run_root: str | Path,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """对照真值清单审计一次运行的归档结果。

    Args:
        truth_manifest: JSON 真值清单（含 ``included`` 条目）
        run_root: InvoiceFlowAI 运行根目录（含 artifacts）
        output: 结果 JSON 路径，默认写在真值清单同目录

    Returns:
        审计结果 dict，含 ``p0_conclusion`` / ``user_p1_conclusion`` /
        ``p2_conclusion`` / ``exit_code``。
    """
    try:
        from .invoiceflow.strict_truth_audit import (
            audit_run_id,
            candidate_revision,
            compare,
            strict_exit_code,
            validate_truth_ids,
            write_json_atomic,
            write_markdown,
        )
        from .invoiceflow.truth_contracts import TruthContractError, TruthManifest
    except ImportError as exc:
        raise BridgeUnavailable(f"真值审计依赖缺失: {exc}") from exc

    manifest_path = Path(truth_manifest).expanduser()
    run_path = Path(run_root).expanduser()
    if not manifest_path.is_file():
        raise BridgeUnavailable(f"真值清单不存在: {manifest_path}")
    if not run_path.is_dir():
        raise BridgeUnavailable(f"运行目录不存在: {run_path}")

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_truth_ids(raw.get("included", []))
        manifest = TruthManifest.from_mapping(raw).to_mapping()
    except (OSError, json.JSONDecodeError, TruthContractError, ValueError) as exc:
        raise BridgeUnavailable(f"真值清单无效: {exc}") from exc

    result = compare(manifest, run_path)
    target = Path(output).expanduser() if output else manifest_path.with_name(
        "strict_truth_audit_result.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    counts = {
        "p0": result["p0_conclusion"]["count"],
        "p1": result["user_p1_conclusion"]["count"],
        "p2": result["p2_conclusion"]["count"],
        "manual": len(result["manual_check_rows"]),
    }
    result["generated_at_utc"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    result["run_id"] = audit_run_id(run_path)
    result["candidate_revision"] = candidate_revision(run_path)
    result["exit_code"] = (
        strict_exit_code(counts)
        if result.get("audit_authority", {}).get("authoritative")
        else 1
    )
    write_json_atomic(target, result)
    write_markdown(result, target.with_suffix(".md"))
    result["output"] = str(target)
    return result


# ---------------------------------------------------------------------------
# URL 安全校验（SSRF 防护）
# ---------------------------------------------------------------------------


def validate_invoice_url(url: str) -> dict[str, Any]:
    """用 InvoiceFlowAI 的公网单播策略校验链接发票 URL。

    拒绝 localhost / 内网地址 / 非标准端口 / 带凭据的 URL，防止 SSRF。
    校验结果含脱敏后的 URL，可安全写入日志。
    """
    try:
        from .invoiceflow.url_security import PublicUrlPolicy, PublicUrlPolicyError
    except ImportError as exc:
        raise BridgeUnavailable(f"URL 安全校验依赖缺失: {exc}") from exc

    policy = PublicUrlPolicy()
    try:
        validated = policy.validate(url)
    except PublicUrlPolicyError as exc:
        return {
            "ok": False,
            "url": PublicUrlPolicy.sanitize(str(url)),
            "reason": str(exc),
        }
    return {
        "ok": True,
        "url": PublicUrlPolicy.sanitize(str(url)),
        "host": getattr(validated, "host", ""),
        "port": getattr(validated, "port", None),
        "transport": getattr(validated, "transport_mode", lambda: "")(),
    }


def recover_invoice_links(
    text_content: str,
    subject: str = "",
    email_id: str = "",
    staging_dir: str | Path = "staging",
    *,
    candidate_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从邮件正文里恢复链接发票（绕过邮件客户端只给下载链接的情况）。

    内部走 InvoiceFlowAI 的 ``BoundedUrlRecoveryClient``：先做 URL 安全预检，
    再按 provider（百望云 / 税务平台 / 通用网页）分级处理，带超时与子进程隔离。
    """
    try:
        from .invoiceflow.bounded_url_recovery import BoundedUrlRecoveryClient
    except ImportError as exc:
        raise BridgeUnavailable(f"链接发票恢复依赖缺失: {exc}") from exc

    client = BoundedUrlRecoveryClient(staging_dir=str(staging_dir))
    return client.process_invoice_links(
        text_content=text_content,
        subject=subject,
        email_id=email_id,
        return_metadata=True,
        candidate_info=candidate_info,
    )


# ---------------------------------------------------------------------------
# 批次准入校验
# ---------------------------------------------------------------------------


def verify_batch(
    artifacts: Iterable[str | Path],
    truth_manifest: str | Path | None = None,
) -> dict[str, Any]:
    """校验一批产物是否完整、可归档（批次准入）。

    无真值清单时：检查文件存在性与 SHA256 可读性。
    有真值清单时：逐项比对 SHA256，未命中者进 ``unverified`` 列表待人工复核。
    SHA256 是 InvoiceFlowAI ``artifact_verifier`` 的首要准入依据
    （``verification_mode=unchanged_sha256``）。
    """
    import hashlib

    paths = [Path(p).expanduser() for p in artifacts]
    missing = [str(p) for p in paths if not p.is_file()]
    hashes: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[str(path)] = digest.hexdigest()
        except OSError as exc:
            logger.warning("读取产物失败 %s: %s", path, exc)

    result: dict[str, Any] = {
        "ok": not missing and len(hashes) == len(paths),
        "total": len(paths),
        "missing": missing,
        "hashes": hashes,
    }
    if truth_manifest:
        manifest = Path(truth_manifest).expanduser()
        if not manifest.is_file():
            raise BridgeUnavailable(f"真值清单不存在: {manifest}")
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeUnavailable(f"真值清单无效: {exc}") from exc
        expected = {
            str(row.get("sha256", "")).lower()
            for row in (raw.get("included") or [])
            if isinstance(row, dict)
        }
        verified = {p: h for p, h in hashes.items() if h.lower() in expected}
        result.update(
            {
                "truth_manifest": str(manifest),
                "verified": verified,
                "unverified": {p: h for p, h in hashes.items() if p not in verified},
                "ok": result["ok"] and len(verified) == len(hashes),
            }
        )
    return result


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------


def run_mcp_server(
    transport: str = "stdio",
    port: int = 8765,
    host: str = "127.0.0.1",
    cors_origins: list[str] | None = None,
) -> None:
    """启动 piaoyihe 的 FastMCP Server（9 个发票处理 tool）。

    这是阻塞调用；在 CLI 里作为独立子命令使用。
    """
    try:
        from .piaoyihe.mcp_server.config import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TRANSPORT
        from .piaoyihe.mcp_server.server import run_server
    except ImportError as exc:
        raise BridgeUnavailable(f"MCP Server 依赖缺失（需安装 [piaoyihe] 可选组）: {exc}") from exc

    logger.info("启动 MCP Server: transport=%s host=%s port=%s", transport, host, port)
    run_server(
        transport=transport or DEFAULT_TRANSPORT,
        port=port or DEFAULT_PORT,
        host=host or DEFAULT_HOST,
        cors_origins=cors_origins,
    )
