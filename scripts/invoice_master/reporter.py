"""文件重命名与归档 + Excel 报表。

重命名规则用模板字符串，占位符取自 :class:`~invoice_master.models.Invoice`::

    {date}      开票日期 2026-03-15
    {date_compact}  20260315
    {type}      票据类型 如「住宿发票」
    {amount}    金额 888.00
    {seller}    销售方
    {purchaser} 购买方
    {code}      发票号码（后8 位，无则空）
    {index}     序号，自动补零（配合 {index} 需要开启序号模式）

安全设计：**默认 dry-run**。
批量改名是最容易造成不可逆损失的操作（几百个文件一次性改名，
改错了没法逐个还原），所以 ``rename_files`` 签名为 ``dry_run=True``，
必须显式传 ``dry_run=False`` 才真正执行。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .config import Config
from .models import DocType, Invoice, format_amount, format_date

logger = logging.getLogger(__name__)

#: Windows 文件名非法字符
ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Windows 保留设备名
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_FILENAME_LENGTH = 120


class RenameError(RuntimeError):
    """重命名失败。"""


@dataclass
class RenamePlan:
    """一次重命名的完整计划。"""

    items: list[tuple[Path, Path]] = field(default_factory=list)  # (原路径, 新路径)
    skipped: list[tuple[Path, str]] = field(default_factory=list)  # (路径, 原因)
    conflicts: list[tuple[Path, Path]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    def as_text(self, limit: int = 20) -> str:
        """人类可读的计划摘要。"""
        lines = [f"将重命名 {self.count} 个文件"]
        for old, new in self.items[:limit]:
            lines.append(f"  {old.name}\n    -> {new.name}")
        if self.count > limit:
            lines.append(f"  ... 还有 {self.count - limit} 个")
        if self.skipped:
            lines.append(f"跳过 {len(self.skipped)} 个：")
            for path, reason in self.skipped[:5]:
                lines.append(f"  {path.name} — {reason}")
        if self.conflicts:
            lines.append(f"⚠目标名冲突 {len(self.conflicts)} 个：")
            for old, new in self.conflicts[:5]:
                lines.append(f"  {old.name} -> {new.name}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 文件名生成
# ---------------------------------------------------------------------------


def sanitize_filename(name: str, *, replacement: str = "_") -> str:
    """清洗文件名：去非法字符、处理保留名、限长。"""
    cleaned = ILLEGAL_CHARS.sub(replacement, name).strip().rstrip(". ")
    if not cleaned:
        cleaned = "unnamed"

    stem = cleaned.split(".")[0].upper()
    if stem in RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    if len(cleaned) > MAX_FILENAME_LENGTH:
        # 只有确实含扩展名时才拆分。注意 rpartition 对无 "." 的字符串
        # 会返回 ("", "", 整个串)，直接用会让 stem 变空、长度计算失效。
        base, dot, ext = cleaned.rpartition(".")
        if dot:
            keep = MAX_FILENAME_LENGTH - len(ext) - 1
            cleaned = base[:keep] + f".{ext}"
        else:
            cleaned = cleaned[:MAX_FILENAME_LENGTH]

    return cleaned


def build_filename(
    invoice: Invoice,
    template: str,
    *,
    extension: str = ".pdf",
    index: int | None = None,
) -> str:
    """按模板生成新文件名。

    Args:
        invoice: 发票记录
        template: 模板字符串
        extension: 目标扩展名（不含点）
        index: 序号（用于同一模板下多文件去重）

    Returns:
        清洗后的文件名
    """
    # 序号占位符需要知道总位数才能补零，故先扫描模板
    values = {
        "date": format_date(invoice.invoice_date) or "未知日期",
        "date_compact": format_date(invoice.invoice_date, "%Y%m%d") or "",
        "type": invoice.doc_type.value,
        "amount": format_amount(invoice.amount) if invoice.amount is not None else "",
        "tax_amount": format_amount(invoice.tax_amount)
        if invoice.tax_amount is not None
        else "",
        "seller": invoice.seller or "",
        "purchaser": invoice.purchaser or "",
        "code": invoice.invoice_code[-8:] if invoice.invoice_code else "",
        "product": invoice.product[:20] if invoice.product else "",
    }

    # 序号占位符需预先格式化（values 里的值都是字符串，
    # 模板若写 {index:03d}，format_map 会对 str套数字格式符而报错）。
    # 做法：先检测 index 占位符并填充带前导零的字符串，
    # 再把模板里的格式说明符剥掉，只留 {index}。
    index_value: str | None = None
    if "{index" in template:
        match = re.search(r"\{index(?::0(\d+)d)?\}", template)
        width = int(match.group(1)) if match and match.group(1) else 3
        index_value = f"{(index or 0):0{width}d}"
        template = re.sub(r"\{index(?::0\d+d)?\}", "{index}", template)

    values["index"] = index_value if index_value is not None else ""

    # 用 format_map 一次性填充；未知占位符保留原样以便发现拼写错误
    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        rendered = template.format_map(_SafeDict(values))
    except (ValueError, IndexError) as exc:
        raise RenameError(f"重命名模板语法错误: {template} ({exc})") from exc

    if "{" in rendered and "}" in rendered:
        unknown = re.findall(r"\{(\w+)\}", rendered)
        if unknown:
            raise RenameError(
                f"模板包含未知占位符: {', '.join(unknown)}；"
                f"可用占位符: {', '.join(sorted(values))}, index"
            )

    return sanitize_filename(rendered) + extension


# ---------------------------------------------------------------------------
# 重命名执行
# ---------------------------------------------------------------------------


def plan_rename(
    invoices: Sequence[Invoice],
    *,
    template: str,
    use_index: bool = False,
    suffix_if_exists: str = "_2",
) -> RenamePlan:
    """生成重命名计划，不执行任何文件操作。

    Args:
        invoices: 发票记录
        template: 命名模板
        use_index: 是否在文件名加序号（防同名覆盖）
        suffix_if_exists: 目标已存在时的后缀策略

    Returns:
        RenamePlan
    """
    plan = RenamePlan()
    used: set[str] = set()

    for position, invoice in enumerate(invoices, start=1):
        extension = invoice.path.suffix or ".pdf"
        try:
            name = build_filename(
                invoice,
                template,
                extension=extension,
                index=position if use_index else None,
            )
        except RenameError as exc:
            plan.skipped.append((invoice.path, str(exc)))
            continue

        target = invoice.path.parent / name
        key = target.name.lower()

        if target == invoice.path:
            continue  # 名字没变，无需处理

        if key in used:
            # 同批内重名 —— 加序号后缀
            stem, _, ext = name.rpartition(".")
            stem = f"{stem}{suffix_if_exists}" if stem else f"{name}{suffix_if_exists}"
            target = invoice.path.parent / (f"{stem}.{ext}" if ext else stem)
            plan.conflicts.append((invoice.path, target))

        if target.exists():
            plan.conflicts.append((invoice.path, target))
            # 目标已存在于磁盘，追加后缀避免覆盖
            stem, _, ext = target.name.rpartition(".")
            counter = 2
            while True:
                candidate = invoice.path.parent / (
                    f"{stem}_{counter}.{ext}" if ext else f"{stem}_{counter}"
                )
                if not candidate.exists():
                    target = candidate
                    break
                counter += 1
                if counter > 999:
                    plan.skipped.append((invoice.path, "无法生成唯一文件名"))
                    target = None
                    break
            if target is None:
                continue

        used.add(target.name.lower())
        plan.items.append((invoice.path, target))

    return plan


def execute_rename(plan: RenamePlan, *, dry_run: bool = True) -> tuple[int, int]:
    """执行重命名计划。

    Args:
        plan: 计划
        dry_run: True 时只统计不执行

    Returns:
        (成功数, 失败数)
    """
    if dry_run:
        logger.info("[dry-run] 将重命名 %d 个文件", len(plan.items))
        return len(plan.items), len(plan.skipped)

    success = failed = 0
    for old, new in plan.items:
        if not old.exists():
            logger.warning("源文件不存在，跳过: %s", old)
            failed += 1
            continue
        try:
            old.rename(new)
            success += 1
        except OSError as exc:
            logger.error("重命名失败 %s -> %s: %s", old.name, new.name, exc)
            failed += 1

    logger.info("重命名完成：成功 %d，失败 %d", success, failed)
    return success, failed


# ---------------------------------------------------------------------------
# 归档（按类型分目录）
# ---------------------------------------------------------------------------


@dataclass
class ArchiveResult:
    """归档结果。"""

    moved: list[tuple[Path, Path]] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def moved_count(self) -> int:
        return len(self.moved)


def archive_by_type(
    invoices: Sequence[Invoice],
    root: str | Path,
    *,
    dry_run: bool = False,
    company_filter: str = "",
) -> ArchiveResult:
    """按票据类型归档到子目录。

    Args:
        invoices: 发票记录
        root: 归档根目录
        dry_run: 只统计不移动
        company_filter: 目标公司名；不匹配的发票隔离到「非目标公司发票」

    Returns:
        ArchiveResult
    """
    result = ArchiveResult()
    base = Path(root).expanduser()

    for invoice in invoices:
        # 人工复核队列：无法可靠识别的
        if invoice.doc_type is DocType.UNKNOWN:
            folder = "待人工复核"
        elif company_filter and invoice.purchaser:
            from .models import companies_match

            if not companies_match(invoice.purchaser, company_filter):
                folder = "非目标公司发票"
            else:
                folder = invoice.doc_type.value
        else:
            folder = invoice.doc_type.value

        target_dir = base / folder
        target = target_dir / invoice.path.name

        if target.resolve() == invoice.path.resolve():
            continue

        if not dry_run:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                invoice.path.rename(target)
            except OSError as exc:
                logger.error("归档失败 %s: %s", invoice.path.name, exc)
                result.skipped.append((invoice.path, str(exc)))
                continue
            invoice.path = target

        result.moved.append((invoice.path, target))

    return result


# ---------------------------------------------------------------------------
# Excel 报表
# ---------------------------------------------------------------------------


class Reporter:
    """生成 Excel 报销汇总表。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.precision = int(config.get("report.amount_precision", 2))
        self.highlight_duplicates = bool(config.get("report.highlight_duplicates", True))
        self.duplicate_fill = str(config.get("report.duplicate_fill", "FFF2CC"))

    def build(
        self,
        invoices: Sequence[Invoice],
        output_path: str | Path,
        *,
        title: str = "发票汇总",
    ) -> Path:
        """生成报表。

        表结构：
          - 汇总区：按类型分组小计+ 总计
          - 明细区：逐张发票字段
          - 重复发票号码高亮标黄

        Args:
            invoices: 发票记录
            output_path: 输出路径（.xlsx）
            title:报表标题

        Returns:
            输出文件路径
        """
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
            from openpyxl.utils import get_column_letter
        except ImportError as exc:
            raise RuntimeError(
                "生成 Excel 需要 openpyxl，请执行: pip install openpyxl"
            ) from exc

        target = Path(output_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)

        workbook = Workbook()
        detail = workbook.active
        detail.title = "发票明细"

        headers = [
            "文件名",
            "类型",
            "开票日期",
            "金额",
            "税额",
            "发票号码",
            "发票代码",
            "购买方",
            "销售方",
            "商品服务",
            "是否发票",
            "置信度",
            "识别方式",
            "路径",
        ]

        # -- 样式 --
        header_font = Font(bold=True, size=11, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="4472C4")
        header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
        thin = Side(style="thin", color="D0D0D0")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        money_format = f"#,##0.{'0' * self.precision}"

        # -- 表头 --
        detail.append(headers)
        for cell in detail[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = border
        detail.freeze_panes = "A2"
        detail.row_dimensions[1].height = 28

        # -- 明细行 --
        duplicate_codes = self._find_duplicates(invoices)
        total_amount = Decimal(0)
        total_tax = Decimal(0)
        by_type: dict[str, list[Invoice]] = {}

        for invoice in invoices:
            row = invoice.to_row()
            detail.append([row.get(h, "") for h in headers])

            row_idx = detail.max_row
            # 金额列右对齐 + 数字格式
            for col_name in ("金额", "税额"):
                col_idx = headers.index(col_name) + 1
                cell = detail.cell(row=row_idx, column=col_idx)
                cell.alignment = Alignment(horizontal="right")
                if isinstance(cell.value, str) and cell.value:
                    cell.number_format = money_format

            for col_idx in range(1, len(headers) + 1):
                detail.cell(row=row_idx, column=col_idx).border = border

            # 重复发票号码标黄
            if self.highlight_duplicates and invoice.invoice_code in duplicate_codes:
                fill = PatternFill("solid", fgColor=self.duplicate_fill)
                for col_idx in range(1, len(headers) + 1):
                    detail.cell(row=row_idx, column=col_idx).fill = fill

            if invoice.is_invoice:
                total_amount += invoice.amount or Decimal(0)
                total_tax += invoice.tax_amount or Decimal(0)
            by_type.setdefault(invoice.doc_type.value, []).append(invoice)

        # -- 列宽 --
        widths = {
            "文件名": 32,
            "类型": 14,
            "开票日期": 12,
            "金额": 12,
            "税额": 10,
            "发票号码": 22,
            "发票代码": 14,
            "购买方": 24,
            "销售方": 24,
            "商品服务": 20,
            "是否发票": 9,
            "置信度": 8,
            "识别方式": 9,
            "路径": 40,
        }
        for index, name in enumerate(headers, start=1):
            detail.column_dimensions[get_column_letter(index)].width = widths.get(
                name, 14
            )

        detail.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{detail.max_row}"

        # -- 汇总表 --
        summary = workbook.create_sheet("分类汇总", 0)
        summary.append([f"{title}（共 {len(invoices)} 张）"])
        summary["A1"].font = Font(bold=True, size=14)
        summary.append([])

        summary.append(["类型", "张数", "金额合计", "税额合计"])
        for cell in summary[3]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = border

        for type_name, items in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
            amount_sum = sum((i.amount or Decimal(0)) for i in items if i.is_invoice)
            tax_sum = sum((i.tax_amount or Decimal(0)) for i in items if i.is_invoice)
            summary.append([type_name, len(items), float(amount_sum), float(tax_sum)])
            row_idx = summary.max_row
            summary.cell(row=row_idx, column=3).number_format = money_format
            summary.cell(row=row_idx, column=4).number_format = money_format
            for col_idx in range(1, 5):
                summary.cell(row=row_idx, column=col_idx).border = border

        summary.append([])
        total_row = summary.max_row + 1
        summary.cell(row=total_row, column=1, value="总计").font = Font(bold=True)
        summary.cell(row=total_row, column=2, value=len(invoices)).font = Font(bold=True)
        amount_cell = summary.cell(row=total_row, column=3, value=float(total_amount))
        amount_cell.font = Font(bold=True)
        amount_cell.number_format = money_format
        tax_cell = summary.cell(row=total_row, column=4, value=float(total_tax))
        tax_cell.font = Font(bold=True)
        tax_cell.number_format = money_format

        summary.column_dimensions["A"].width = 20
        summary.column_dimensions["B"].width = 10
        summary.column_dimensions["C"].width = 16
        summary.column_dimensions["D"].width = 16

        if duplicate_codes:
            note_row = total_row + 2
            summary.cell(
                row=note_row,
                column=1,
                value=f"⚠ 检测到 {len(duplicate_codes)} 个重复发票号码，"
                "已在明细表标黄，请人工核对",
            )
            summary.cell(row=note_row, column=1).font = Font(color="C00000", bold=True)

        workbook.save(target)
        logger.info("报表已生成: %s", target)
        return target

    @staticmethod
    def _find_duplicates(invoices: Iterable[Invoice]) -> set[str]:
        """找出重复出现的发票号码。"""
        seen: dict[str, int] = {}
        for invoice in invoices:
            if invoice.is_invoice and invoice.invoice_code:
                seen[invoice.invoice_code] = seen.get(invoice.invoice_code, 0) + 1
        return {code for code, count in seen.items() if count > 1}
