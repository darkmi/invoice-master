"""命令行入口。

子命令一览::

    scan     扫描邮箱收集发票附件
    extract        提取本地 PDF 字段
    agent-prepare  生成任务包，交给 agent 用自身视觉能力识别
    agent-apply    回填 agent 给出的字段 JSON
    merge          合并 PDF 为报销单
    rename   按模板批量重命名
    report   生成 Excel 报销汇总表
    pair     发票与凭证配对
    config   初始化/查看配置
    layouts  列出可用合并布局
    audit    严格真值审计（吸收自 InvoiceFlowAI）
    urlcheck URL 公网安全校验（SSRF 防护）
    verify   产物批次完整性 / SHA256 校验

全局选项::

    -c/--config PATH   指定配置文件
    -v/--verbose       输出调试日志
    --json             以JSON 输出（便于脚本消费）

设计原则：**破坏性操作默认 dry-run**。
``rename`` 与 ``archive`` 必须显式加 ``--execute`` 才会真正改动文件。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__, pdf as pdfmod
from .config import Config, load_config, redact, save_config
from .integrations import ENGINES, EngineUnavailable, make_extractor
from .merger import (
    PRESET_LAYOUTS,
    MergeError,
    Merger,
    MergeLayout,
    resolve_layout,
    resolve_page_size,
)
from .models import DocType, format_amount, format_date, pair_invoices

logger = logging.getLogger("invoice_master")


def build_parser() -> argparse.ArgumentParser:
    """构建参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="invoice-master",
        description="电子发票整理与报销准备工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", help="配置文件路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="以 JSON 输出"
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="禁用 LLM 兜底（覆盖配置文件中的开启设置）",
    )
    # 注意：--enable-llm 只挂在需要它的子命令上（extract/report/scan），
    # 不设全局版本——同名参数在子解析器里默认False，会把全局值覆盖掉。

    sub = parser.add_subparsers(dest="command", metavar="命令")

    # -- scan --
    scan = sub.add_parser("scan", help="扫描邮箱收集发票附件")
    scan.add_argument("-o", "--output", required=True, help="附件保存目录")
    scan.add_argument("--days", type=int, help="回溯天数（覆盖配置）")
    scan.add_argument("--mailbox-name", default="INBOX", help="邮箱文件夹")
    scan.add_argument("--limit", type=int, default=0, help="最多扫描邮件数，0=不限")
    scan.add_argument("--dry-run", action="store_true", help="只统计不保存附件")
    scan.add_argument(
        "--enable-llm",
        action="store_true",
        help="下载后用 LLM 识别字段（发票图片将发送至第三方服务）",
    )

    # -- extract --
    extract = sub.add_parser("extract", help="提取本地 PDF 发票字段")
    extract.add_argument("paths", nargs="+", help="PDF 文件或目录")
    extract.add_argument("-o", "--output", help="输出 JSON 文件路径")
    extract.add_argument("--recursive", "-r", action="store_true", help="递归扫描子目录")
    extract.add_argument(
        "--engine",
        choices=ENGINES,
        default="local",
        help="提取引擎：local 本地正则 / hybrid 本地+LLM兜底 / "
             "dual InvoiceFlowAI 双引擎 / piaoyihe 票易合7票种（默认 local）",
    )
    extract.add_argument(
        "--enable-llm",
        action="store_true",
        help="启用 LLM 兜底（等价于 --engine hybrid，保留向后兼容）",
    )

    # -- agent-prepare / agent-apply：用 agent 自身视觉能力替代远程 GLM API --
    agent_prepare = sub.add_parser(
        "agent-prepare", help="生成任务包，交给 agent 用自身视觉能力识别"
    )
    agent_prepare.add_argument("paths", nargs="+", help="PDF 文件或目录")
    agent_prepare.add_argument("-o", "--request", required=True, help="任务包 JSON 输出路径")
    agent_prepare.add_argument("--images-dir", help="渲染图片目录（默认在任务包同级）")
    agent_prepare.add_argument("--dpi", type=int, default=150, help="渲染精度（默认 150）")
    agent_prepare.add_argument("--max-px", type=int, default=0, help="图片最长边上限（0=沿用配置）")
    agent_prepare.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")

    agent_apply = sub.add_parser(
        "agent-apply", help="把 agent 回填的字段 JSON 转成标准提取结果"
    )
    agent_apply.add_argument("request", help="agent-prepare 生成的任务包 JSON")
    agent_apply.add_argument("answers", help="agent 写出的答案 JSON")
    agent_apply.add_argument("-o", "--output", help="提取结果 JSON 输出路径")

    # -- merge --
    merge = sub.add_parser("merge", help="合并 PDF 为报销单")
    merge.add_argument("paths", nargs="+", help="待合并的 PDF 或目录")
    merge.add_argument("-o", "--output", required=True, help="输出 PDF 路径")
    merge.add_argument(
        "-l", "--layout", default=None,
        help="布局预设名或 横向RxC/竖向RxC（行列各 1–10），如 横向3x5；"
        "省略时读配置 merge: 段，再省略则默认 横向2x2",
    )
    merge.add_argument("--mode", choices=["vector", "raster", "普通", "图像"],
                       help="合并模式（图像=栅格，兼容 piaoyihe 叫法）")
    merge.add_argument("--rows", type=int, help="行数（覆盖布局）")
    merge.add_argument("--cols", type=int, help="列数（覆盖布局）")
    merge.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], help="旋转角度")
    merge.add_argument("--page", choices=["A4", "A3", "LETTER"], help="纸张尺寸（默认 A4）")
    # 以下排版细节吸收自 piaoyihe PDFHandler，统一由 toolkit 的 Merger 执行
    merge.add_argument("--margin-top", type=float, help="上边距（mm）")
    merge.add_argument("--margin-bottom", type=float, help="下边距（mm）")
    merge.add_argument("--margin-left", type=float, help="左边距（mm）")
    merge.add_argument("--margin-right", type=float, help="右边距（mm）")
    merge.add_argument("--gap", type=float, help="发票间隙（点）")
    merge.add_argument("--batch-size", type=int, help="分批大小（默认 50）")
    merge.add_argument("--crop-marks", action="store_true", help="绘制裁切线")
    merge.add_argument(
        "--crop-mark-left", type=float, help="左侧裁切线偏移（mm）"
    )
    merge.add_argument(
        "--crop-mark-right", type=float, help="右侧裁切线偏移（mm）"
    )
    merge.add_argument("--no-dividers", action="store_true", help="不绘制分隔线")
    merge.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")

    # -- rename --
    rename = sub.add_parser("rename", help="按模板批量重命名")
    rename.add_argument("paths", nargs="+", help="PDF 文件或目录")
    rename.add_argument("-t", "--template", help="命名模板（覆盖配置）")
    rename.add_argument(
        "--execute",
        action="store_true",
        help="真正执行改名（不加此参数只预览）",
    )
    rename.add_argument("-r", "--recursive", action="store_true", help="递归扫描")

    # -- report --
    report = sub.add_parser("report", help="生成 Excel 报销汇总表")
    report.add_argument("paths", nargs="*", help="PDF 文件或目录（--from-extract 时可省略）")
    report.add_argument("-o", "--output", help="输出 xlsx 路径")
    report.add_argument("-r", "--recursive", action="store_true", help="递归扫描")
    report.add_argument("--title", default="发票汇总", help="报表标题")
    report.add_argument(
        "--engine",
        choices=ENGINES,
        default="local",
        help="提取引擎（同 extract 命令，默认 local）",
    )
    report.add_argument(
        "--enable-llm",
        action="store_true",
        help="启用 LLM 兜底（等价于 --engine hybrid，保留向后兼容）",
    )
    report.add_argument(
        "--from-extract",
        help="复用已有提取结果 JSON（如 agent-apply 输出），不再重新识别",
    )

    # -- pair --
    pair = sub.add_parser("pair", help="发票与凭证配对检查")
    pair.add_argument("paths", nargs="+", help="PDF 文件或目录")
    pair.add_argument("-r", "--recursive", action="store_true", help="递归扫描")

    # -- config --
    config_cmd = sub.add_parser("config", help="初始化或查看配置")
    config_cmd.add_argument("--init", help="生成默认配置文件到指定路径")
    config_cmd.add_argument(
        "--show", action="store_true", help="显示当前配置（密码脱敏）"
    )
    config_cmd.add_argument("--path", action="store_true", help="显示配置文件查找路径")

    # -- layouts --
    sub.add_parser("layouts", help="列出可用合并布局")

    # -- audit（吸收自 InvoiceFlowAI）--
    audit = sub.add_parser("audit", help="严格真值审计：对照归档结果")
    audit.add_argument("--truth-manifest", required=True, help="真值清单 JSON")
    audit.add_argument("--run-root", required=True, help="InvoiceFlowAI 运行目录")
    audit.add_argument("--output", help="审计结果输出路径")

    # -- urlcheck（吸收自 InvoiceFlowAI 的 SSRF 防护）--
    urlcheck = sub.add_parser("urlcheck", help="校验链接发票 URL 的公网安全性")
    urlcheck.add_argument("urls", nargs="+", help="待校验 URL")

    # -- verify（批次准入）--
    verify = sub.add_parser("verify", help="校验产物批次完整性 / SHA256")
    verify.add_argument("paths", nargs="+", help="产物文件或目录")
    verify.add_argument("--truth-manifest", help="真值清单 JSON（启用 SHA256 比对）")

    return parser


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def collect_pdfs(paths: Sequence[str], recursive: bool = False) -> list[Path]:
    """把路径参数展开为 PDF 文件列表。"""
    found: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            globber = path.rglob("*") if recursive else path.glob("*")
            found.extend(p for p in globber if p.is_file() and p.suffix.lower() == ".pdf")
        else:
            logger.warning("路径不存在: %s", raw)
    return sorted(set(found))


def _output(payload: Any, as_json: bool, text: str = "") -> None:
    """统一输出。"""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        if text:
            print(text)


def _make_extractor(
    config: Config,
    *,
    allow_llm: bool = False,
    engine: str = "local",
):
    """构造提取器。

    Args:
        config: 配置
        allow_llm: ``--enable-llm`` 传入，等价于切换到 hybrid 引擎（向后兼容）。
        engine: 提取引擎名，见 :data:`~invoice_master.integrations.ENGINES`。

    行为：
        * 默认 ``local`` —— 纯本地正则，不产生任何 API 调用。
        * ``--enable-llm`` 或 ``--engine hybrid`` —— 本地 + LLM 兜底。
        * ``dual`` / ``piaoyihe`` —— 分别接入 InvoiceFlowAI 双引擎与票易合提取器。
        * 所选引擎缺依赖/配置时降级到 local 并打印提示，不静默失败。
    """
    selected = "hybrid" if allow_llm else (engine or "local").lower()
    try:
        extractor = make_extractor(config, selected)
    except EngineUnavailable as exc:
        print(f"⚠ {exc}；已回退到 local 引擎", file=sys.stderr)
        logger.warning("引擎 %s 不可用，回退 local: %s", selected, exc)
        selected = "local"
        extractor = make_extractor(config, "local")
    logger.info("提取引擎: %s（%s）", selected, extractor.llm_status)
    return extractor


def _format_extraction_results(results) -> tuple[dict[str, Any], str]:
    """把抽取结果统一转成 CLI/JSON 输出。"""
    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    need_review = 0

    for result in results:
        if result.invoice is None:
            rows.append({"path": str(result.path), "ok": False, "reason": result.reason})
            lines.append(f"[FAIL] {result.path.name}: {result.reason}")
            need_review += 1
            continue

        invoice = result.invoice
        rows.append({"path": str(result.path), "ok": True, **invoice.to_row()})
        flag = " ⚠" if invoice.doc_type is DocType.UNKNOWN else ""
        lines.append(
            f"{invoice.doc_type.value:<10}{format_date(invoice.invoice_date):<12}"
            f"{format_amount(invoice.amount):>10}元  "
            f"{invoice.seller or '-'}  conf={invoice.confidence}{flag}"
        )

    payload = {
        "total": len(results),
        "success": len(results) - need_review,
        "need_review": need_review,
        "rows": rows,
    }
    header = f"共{len(results)} 个文件，成功 {len(results) - need_review}，需复核 {need_review}\n"
    return payload, header + "\n".join(lines)


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    """扫描邮箱。"""
    from .mailbox import ImapFetcher, MailboxError

    try:
        with ImapFetcher(config) as fetcher:
            attachments, stats = fetcher.scan(
                mailbox=args.mailbox_name,
                days_back=args.days,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            saved: list[Path] = []
            if not args.dry_run and attachments:
                saved = fetcher.save_all(attachments, args.output)

        result = {
            "scanned": stats.scanned,
            "matched": stats.matched,
            "attachments": stats.attachments,
            "skipped": stats.skipped,
            "errors": stats.errors,
            "reasons": stats.reasons,
            "saved": [str(p) for p in saved],
        }
        lines = [stats.as_text()]
        if saved:
            lines.append(f"已保存到{args.output}")
        elif args.dry_run:
            lines.append("（dry-run，未保存文件）")
        _output(result, args.as_json, "\n".join(lines))
        return 0 if stats.errors == 0 else 1
    except MailboxError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


def cmd_extract(args: argparse.Namespace, config: Config) -> int:
    """提取 PDF 字段。"""
    files = collect_pdfs(args.paths, args.recursive)
    if not files:
        print("未找到 PDF 文件", file=sys.stderr)
        return 2

    extractor = _make_extractor(config, allow_llm=args.enable_llm, engine=args.engine)
    results = extractor.extract_many(files)

    payload, text = _format_extraction_results(results)
    _output(payload, args.as_json, text)

    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\n已写入 {target}", file=sys.stderr)
    return 0


def cmd_agent_prepare(args: argparse.Namespace, config: Config) -> int:
    """生成 agent 视觉识别任务包。"""
    from .agent_bridge import write_agent_request

    files = collect_pdfs(args.paths, args.recursive)
    if not files:
        print("未找到 PDF 文件", file=sys.stderr)
        return 2

    target = Path(args.request).expanduser()
    images_dir = args.images_dir or str(target.resolve().parent / "agent_images")
    try:
        request = write_agent_request(
            files,
            config=config,
            request_path=target,
            images_dir=images_dir,
            dpi=args.dpi,
            max_px=args.max_px,
        )
    except pdfmod.PdfError as exc:
        print(f"生成任务包失败: {exc}", file=sys.stderr)
        return 2

    tasks = request["tasks"]
    failed = sum(1 for task in tasks if task.get("error"))
    payload = {
        "request": str(target.resolve()),
        "images_dir": request["images_dir"],
        "tasks": len(tasks),
        "render_failed": failed,
    }
    text = (
        f"已生成 {len(tasks)} 个 agent 识别任务 -> {payload['request']}\n"
        f"图片目录: {payload['images_dir']}\n"
        "请用你自己的多模态能力查看每个 image，按 request 中的 prompt 抽取字段，"
        "写入 answers.json（格式见 request.answer_format）。"
    )
    if failed:
        text += f"\n警告: {failed} 个文件渲染失败，详见 request.tasks[*].error"
    _output(payload, args.as_json, text)
    return 0


def cmd_agent_apply(args: argparse.Namespace, config: Config) -> int:
    """回填 agent 答案，产出与 extract 一致的结果。"""
    from .agent_bridge import apply_agent_request

    try:
        results = apply_agent_request(args.request, args.answers, config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"回填失败: {exc}", file=sys.stderr)
        return 2
    if not results:
        print("任务包中没有可处理的任务", file=sys.stderr)
        return 2

    payload, text = _format_extraction_results(results)
    _output(payload, args.as_json, text)

    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\n已写入 {target}", file=sys.stderr)
    return 0


def cmd_merge(args: argparse.Namespace, config: Config) -> int:
    """合并 PDF。"""
    files = collect_pdfs(args.paths, args.recursive)
    if not files:
        print("未找到 PDF 文件", file=sys.stderr)
        return 2

    # 布局优先级：命令行单项参数 > --layout 预设/动态 > 配置文件 merge: 段 > 内置默认
    layout: dict[str, Any] = dict(MergeLayout.from_config(config).__dict__)
    if args.layout:
        grid = resolve_layout(args.layout)
        layout["orientation"] = grid.orientation
        layout["rows"] = grid.rows
        layout["cols"] = grid.cols

    if args.mode:
        # 兼容 piaoyihe 的「普通 / 图像」叫法
        layout["mode"] = {"普通": "vector", "图像": "raster"}.get(args.mode, args.mode)
    if args.rows:
        layout["rows"] = args.rows
    if args.cols:
        layout["cols"] = args.cols
    if args.rotate is not None:
        layout["rotate"] = args.rotate

    # 排版细节（吸收自 piaoyihe）
    for arg_name, key in (
        ("margin_top", "margin_top"),
        ("margin_bottom", "margin_bottom"),
        ("margin_left", "margin_left"),
        ("margin_right", "margin_right"),
        ("gap", "gap"),
        ("batch_size", "batch_size"),
        ("crop_mark_left", "crop_mark_left"),
        ("crop_mark_right", "crop_mark_right"),
    ):
        value = getattr(args, arg_name, None)
        if value is not None:
            layout[key] = value
    if args.crop_marks:
        layout["show_crop_marks"] = True
    if args.no_dividers:
        layout["draw_dividers"] = False

    # 纸张最后定：--page > 配置 merge.page，按最终方向计算实际尺寸
    page_name = args.page or str(config.get("merge.page") or "A4")
    layout["page_size"] = resolve_page_size(
        page_name, str(layout.get("orientation", "landscape"))
    )

    try:
        merger = Merger(layout)
        total = len(files)

        def progress(current: int, steps: int) -> None:
            percent = current * 100 // steps if steps else 100
            print(f"\r合并中 {current}/{steps} ({percent}%)", end="", file=sys.stderr)

        output = merger.merge(files, args.output, progress=progress)
        print(file=sys.stderr)
        _output(
            {"output": str(output), "inputs": total},
            args.as_json,
            f"已合并 {total} 个文件 -> {output}",
        )
        return 0
    except MergeError as exc:
        print(f"合并失败: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"布局配置错误: {exc}", file=sys.stderr)
        return 2


def cmd_rename(args: argparse.Namespace, config: Config) -> int:
    """批量重命名。"""
    from .reporter import execute_rename, plan_rename

    files = collect_pdfs(args.paths, args.recursive)
    if not files:
        print("未找到 PDF 文件", file=sys.stderr)
        return 2

    template = args.template or str(config.get("archive.rename_template"))
    extractor = _make_extractor(config, allow_llm=False)

    invoices = []
    for path in files:
        result = extractor.extract_one(path)
        if result.invoice:
            invoices.append(result.invoice)

    if not invoices:
        print("没有成功识别的发票", file=sys.stderr)
        return 2

    use_index = "{index" in template
    plan = plan_rename(invoices, template=template, use_index=use_index)
    dry_run = not args.execute
    success, failed = execute_rename(plan, dry_run=dry_run)

    result = {
        "total": len(invoices),
        "renamed": success,
        "failed": failed,
        "dry_run": dry_run,
        "items": [{"from": str(old), "to": str(new)} for old, new in plan.items],
        "skipped": [{"path": str(p), "reason": r} for p, r in plan.skipped],
    }
    note = "\n（预览模式，未实际改名。确认无误后加 --execute）" if dry_run else ""
    _output(result, args.as_json, plan.as_text() + note)
    return 0 if failed == 0 else 1


def cmd_report(args: argparse.Namespace, config: Config) -> int:
    """生成 Excel 报表。"""
    from .reporter import Reporter

    files = collect_pdfs(args.paths, args.recursive)
    if not files and not args.from_extract:
        print("未找到 PDF 文件", file=sys.stderr)
        return 2

    if args.from_extract:
        from .agent_bridge import invoice_from_row

        try:
            data = json.loads(Path(args.from_extract).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"读取提取结果失败: {exc}", file=sys.stderr)
            return 2
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            print("提取结果格式错误：需要包含 rows 数组", file=sys.stderr)
            return 2
        invoices = []
        for row in data.get("rows", []):
            if not row.get("ok"):
                continue
            invoice = invoice_from_row(row, Path(row.get("path") or row.get("路径") or ""))
            if invoice is not None:
                invoices.append(invoice)
    else:
        extractor = _make_extractor(config, allow_llm=args.enable_llm, engine=args.engine)
        invoices = [r.invoice for r in extractor.extract_many(files) if r.invoice]
    if not invoices:
        print("没有成功识别的发票", file=sys.stderr)
        return 2

    output = args.output or str(config.get("report.filename"))
    target = Path(output).expanduser()
    if target.is_dir():
        target = target / str(config.get("report.filename"))

    try:
        path = Reporter(config).build(invoices, target, title=args.title)
    except RuntimeError as exc:
        print(f"生成失败: {exc}", file=sys.stderr)
        return 2

    total = sum((i.amount or 0) for i in invoices if i.is_invoice)
    _output(
        {"output": str(path), "count": len(invoices), "total": str(total)},
        args.as_json,
        f"已生成报表: {path}\n发票 {len(invoices)} 张，合计 ¥{total}",
    )
    return 0


def cmd_pair(args: argparse.Namespace, config: Config) -> int:
    """配对检查。"""
    files = collect_pdfs(args.paths, args.recursive)
    if not files:
        print("未找到 PDF 文件", file=sys.stderr)
        return 2

    extractor = _make_extractor(config, allow_llm=False)
    invoices = [r.invoice for r in extractor.extract_many(files) if r.invoice]
    result = pair_invoices(invoices)

    lines = [
        f"共{result.total_invoices} 张发票，凭证 {len(result.unmatched_companions)} 张",
        f"成功配对 {len(result.pairs)} 组（覆盖率 {result.coverage:.0%}）",
        "",
        "已配对：",
    ]
    for pair in result.pairs:
        lines.append(
            f"  {pair.invoice.doc_type.value} <-> {pair.companion.doc_type.value}"
            f"  {format_amount(pair.invoice.amount)}元  score={pair.score}"
        )
    if result.unmatched_invoices:
        lines.append("\n缺少凭证：")
        for invoice in result.unmatched_invoices:
            wanted = invoice.companion_type.value if invoice.companion_type else "-"
            lines.append(f"  {invoice.path.name}  需要 {wanted}")
    if result.unmatched_companions:
        lines.append("\n未匹配的凭证：")
        for companion in result.unmatched_companions:
            lines.append(f"  {companion.path.name}")

    payload = {
        "pairs": [
            {
                "invoice": str(p.invoice.path),
                "companion": str(p.companion.path),
                "score": p.score,
            }
            for p in result.pairs
        ],
        "unmatched_invoices": [str(i.path) for i in result.unmatched_invoices],
        "unmatched_companions": [str(c.path) for c in result.unmatched_companions],
        "coverage": result.coverage,
    }
    _output(payload, args.as_json, "\n".join(lines))
    return 0


def cmd_config(args: argparse.Namespace, config: Config) -> int:
    """配置管理。"""
    if args.init:
        target = save_config(config, args.init)
        _output(
            {"path": str(target)},
            args.as_json,
            f"已生成默认配置: {target}\n请按需修改后使用 -c 指定。",
        )
        return 0

    if args.path:
        _output(
            {"config_path": str(config.source_path) if config.source_path else None},
            args.as_json,
            f"配置文件: {config.source_path or '未找到（使用内置默认值）'}",
        )
        return 0

    safe = redact(config)
    _output(
        safe,
        args.as_json,
        json.dumps(safe, ensure_ascii=False, indent=2)
        + "\n\n提示：密码与 API Key 已脱敏显示。",
    )
    return 0


def cmd_layouts(_args: argparse.Namespace, _config: Config) -> int:
    """列出可用布局。"""
    rows = []
    for name, spec in PRESET_LAYOUTS.items():
        resolved = resolve_layout(name)
        rows.append(
            {
                "name": name,
                "orientation": spec["orientation"],
                "rows": spec["rows"],
                "cols": spec["cols"],
                "cells": spec["rows"] * spec["cols"],
                "page": f"{resolved.page_width:.0f}x{resolved.page_height:.0f}pt",
            }
        )
    lines = ["可用布局预设：", ""]
    for row in rows:
        lines.append(
            f"  {row['name']:<10} {row['orientation']:<10} "
            f"{row['rows']}x{row['cols']} = 每页{row['cells']}张  {row['page']}"
        )
    lines.append("")
    lines.append("自定义布局：横向RxC / 竖向RxC（行列各 1–10），如 横向3x5、竖向10x10")
    lines.append("例：invoice-master merge *.pdf -o 报销单.pdf -l 横向3x5")
    lines.append("也可用 dict 形式（JSON 字符串）传入完整排版参数")
    _output(rows, _args.as_json, "\n".join(lines))
    return 0


def cmd_audit(args: argparse.Namespace, config: Config) -> int:
    """严格真值审计。"""
    from .bridges import BridgeUnavailable, audit_truth

    try:
        result = audit_truth(args.truth_manifest, args.run_root, args.output)
    except BridgeUnavailable as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    counts = {
        "p0": result["p0_conclusion"]["count"],
        "p1": result["user_p1_conclusion"]["count"],
        "p2": result["p2_conclusion"]["count"],
    }
    lines = [
        f"P0: {counts['p0']}  P1: {counts['p1']}  P2: {counts['p2']}",
        f"结果已写入: {result['output']}",
    ]
    _output(result, args.as_json, "\n".join(lines))
    return int(result["exit_code"])


def cmd_urlcheck(args: argparse.Namespace, config: Config) -> int:
    """校验链接发票 URL 的公网安全性（SSRF 防护）。"""
    from .bridges import BridgeUnavailable, validate_invoice_url

    rows = []
    for url in args.urls:
        try:
            rows.append(validate_invoice_url(url))
        except BridgeUnavailable as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
    lines = [f"{'✓' if r['ok'] else '✗'} {r['url']}" + (f"  {r['reason']}" if not r["ok"] else "") for r in rows]
    _output(rows if len(rows) > 1 else rows[0], args.as_json, "\n".join(lines))
    return 0 if all(r["ok"] for r in rows) else 1


def cmd_verify(args: argparse.Namespace, config: Config) -> int:
    """校验产物批次完整性。"""
    from .bridges import BridgeUnavailable, verify_batch

    try:
        result = verify_batch(args.paths, args.truth_manifest)
    except BridgeUnavailable as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    lines = [
        f"产物 {result['total']} 个，缺失 {len(result['missing'])} 个",
        f"校验: {'通过' if result['ok'] else '未通过'}",
    ]
    if "unverified" in result:
        lines.append(f"SHA256 未命中真值: {len(result['unverified'])} 个")
    _output(result, args.as_json, "\n".join(lines))
    return 0 if result["ok"] else 1


COMMANDS = {
    "scan": cmd_scan,
    "extract": cmd_extract,
    "agent-prepare": cmd_agent_prepare,
    "agent-apply": cmd_agent_apply,
    "merge": cmd_merge,
    "rename": cmd_rename,
    "report": cmd_report,
    "pair": cmd_pair,
    "config": cmd_config,
    "layouts": cmd_layouts,
    "audit": cmd_audit,
    "urlcheck": cmd_urlcheck,
    "verify": cmd_verify,
}


def _hoist_global_flags(argv: Sequence[str]) -> list[str]:
    """把误写在子命令后的全局参数提到前面。

    argparse 要求全局选项在前（``prog -v extract``），
    但用户自然会写在后面（``prog extract -v``），
    后者会报"unrecognized arguments"，很难自查。

    这里做一次前置转发，让两种写法都能用。子命令自己声明的同名
    参数（如 ``--enable-llm``）不受影响—— 只转发全局那几个。
    """
    raw = list(argv)
    global_flags = {"-v", "--verbose", "--json", "--no-llm"}

    subcommand_pos = len(raw)
    for index, token in enumerate(raw):
        if not token.startswith("-"):
            subcommand_pos = index
            break

    head, tail = raw[:subcommand_pos], raw[subcommand_pos:]
    hoisted = [t for t in tail if t in global_flags]
    rest = [t for t in tail if t not in global_flags]
    return hoisted + head + rest


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 主入口。"""
    parser = build_parser()
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(_hoist_global_flags(raw_argv))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.command:
        parser.print_help()
        return 0

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    if args.no_llm:
        config.set("llm.enabled", False)

    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2

    try:
        return handler(args, config)
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.exception("命令执行失败")
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
