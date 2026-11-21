"""PDF 合并（打印报销单）。

对标参考项目 piaoyihe的 ``pdf_handler.py::merge_pdfs``，继承其三个关键决策：

1. **分批合并 + 临时文件中转**（``batch_size``）
   一次性把几百页塞进单个 PyMuPDF Document 会耗尽内存甚至文件句柄耗尽。
   分批落盘再拼接是唯一稳妥做法。

2. **矢量 / 栅格双模式**
   ``vector`` 走 ``show_pdf_page``——体积小、文字可搜索；
   ``raster`` 走 ``get_pixmap``——能保住矢量模式会丢的图层内容。

3. **Stamp 注释（监制章）单独渲染贴回**
   这是参考项目踩过的坑：``show_pdf_page`` 只复制页面内容，
   **不复制注释对象**，导致增值税发票上的监制章在合并后消失。
   这里用 450dpi 裁剪渲染该区域再贴回。

默认值与参考项目对齐但做了修正：默认改用矢量模式（更常用、体积更小）。
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable

from . import pdf as pdfmod
from .config import Config

logger = logging.getLogger(__name__)

#: 页面尺寸（点，1pt = 1/72 inch）
A4_PORTRAIT = (595.0, 842.0)
A4_LANDSCAPE = (842.0, 595.0)

#: 支持的图片扩展名 —— 图片只参与排版，不做字段提取
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

MM_TO_PT = 72.0 / 25.4


def _round_to(value: int, multiple: int, *, up: bool = True) -> int:
    """把 ``value`` 调整到 ``multiple`` 的整数倍，且至少为 ``multiple``。

    用于把批次大小对齐到页边界——分批合并时批次不能在页中间断开。
    """
    multiple = max(1, multiple)
    if up:
        return max(multiple, math.ceil(value / multiple) * multiple)
    return max(multiple, (value // multiple) * multiple)


class MergeError(RuntimeError):
    """合并失败。"""


#: 纸张基准尺寸（点），按纵向记录；横向布局时宽高互换。
_PAGE_SIZES: dict[str, tuple[float, float]] = {
    "A4": A4_PORTRAIT,
    "A3": (842.0, 1191.0),
    "LETTER": (612.0, 792.0),
}


def resolve_page_size(
    page: str, orientation: str = "landscape"
) -> tuple[float, float]:
    """按纸张名与方向计算实际页面尺寸（点）。

    基准尺寸按纵向存于 :data:`_PAGE_SIZES`，横向且宽 < 高时互换宽高；
    未知名按 A4 处理。
    """
    width, height = _PAGE_SIZES.get(str(page).upper(), A4_PORTRAIT)
    if str(orientation).lower() == "landscape" and height > width:
        width, height = height, width
    return (width, height)


@dataclass
class MergeLayout:
    """合并布局配置。

    单元格 = 可用区域 ÷ (rows × cols)，每个文件占一格，超出则换页。
    """

    orientation: str = "landscape"  # landscape | portrait
    rows: int = 2
    cols: int = 2
    rotate: int = 0  # 0 | 90 | 180 | 270
    mode: str = "vector"  # vector | raster
    page_size: tuple[float, float] = A4_LANDSCAPE

    margin_top: float = 10.0  # mm
    margin_bottom: float = 10.0
    margin_left: float = 10.0
    margin_right: float = 10.0

    gap: float = 5.0  # 发票间隙（点）
    draw_dividers: bool = True
    show_crop_marks: bool = False
    crop_mark_left: float = 0.0  # mm
    crop_mark_right: float = 0.0
    batch_size: int = 50

    def __post_init__(self) -> None:
        self.orientation = self.orientation.lower()
        if self.orientation not in {"landscape", "portrait"}:
            raise ValueError(
                f"orientation 只能是 landscape 或 portrait，收到: {self.orientation}"
            )
        if self.mode.lower() not in {"vector", "raster"}:
            raise ValueError(f"mode 只能是 vector 或 raster，收到: {self.mode}")
        self.mode = self.mode.lower()

        if self.rows < 1 or self.cols < 1:
            raise ValueError(f"rows/cols 必须 >= 1，当前 {self.rows}x{self.cols}")

        # 方向决定页面尺寸（除非显式指定了自定义尺寸）
        if self.page_size == A4_LANDSCAPE:  # 仍是默认值时按方向调整
            self.page_size = (
                A4_LANDSCAPE if self.orientation == "landscape" else A4_PORTRAIT
            )

    @classmethod
    def from_config(cls, config: Config) -> MergeLayout:
        section = config.section("merge")
        orientation = str(section.get("orientation", "landscape"))
        return cls(
            orientation=orientation,
            rows=int(section.get("rows", 2)),
            cols=int(section.get("cols", 2)),
            rotate=int(section.get("rotate", 0)),
            mode=str(section.get("mode", "vector")),
            page_size=resolve_page_size(section.get("page", "A4"), orientation),
            margin_top=float(section.get("margin_top", 10)),
            margin_bottom=float(section.get("margin_bottom", 10)),
            margin_left=float(section.get("margin_left", 10)),
            margin_right=float(section.get("margin_right", 10)),
            gap=float(section.get("gap", 5)),
            draw_dividers=bool(section.get("draw_dividers", True)),
            show_crop_marks=bool(section.get("show_crop_marks", False)),
            crop_mark_left=float(section.get("crop_mark_left", 0)),
            crop_mark_right=float(section.get("crop_mark_right", 0)),
            batch_size=int(section.get("batch_size", 50)),
        )

    @property
    def cells_per_page(self) -> int:
        return self.rows * self.cols

    @property
    def page_width(self) -> float:
        return self.page_size[0]

    @property
    def page_height(self) -> float:
        return self.page_size[1]

    @property
    def available_width(self) -> float:
        return self.page_width - (self.margin_left + self.margin_right) * MM_TO_PT

    @property
    def available_height(self) -> float:
        return self.page_height - (self.margin_top + self.margin_bottom) * MM_TO_PT


# ---------------------------------------------------------------------------
# 预设布局
# ---------------------------------------------------------------------------

#: 常用预设，键为中文名。动态布局（见 ``_DYNAMIC_LAYOUT``）另有 1x1–10x10 全支持。
PRESET_LAYOUTS: dict[str, dict] = {
    "横向2x2": {"orientation": "landscape", "rows": 2, "cols": 2},
    "横向2x4": {"orientation": "landscape", "rows": 2, "cols": 4},
    "横向3x3": {"orientation": "landscape", "rows": 3, "cols": 3},
    "竖向1x2": {"orientation": "portrait", "rows": 2, "cols": 1},
    "竖向1x3": {"orientation": "portrait", "rows": 3, "cols": 1},
    "竖向2x4": {"orientation": "portrait", "rows": 4, "cols": 2},
}

#: 动态布局：从 piaoyihe 吸收的 1x1–10x10 自定义排版能力。
#: 接受 ``横向RxC`` / ``竖向RxC``（R=行 1–10，C=列 1–10），也兼容英文与带空格写法。
_DYNAMIC_LAYOUT = re.compile(
    r"^(?P<orient>横向|竖向|landscape|portrait)\s*(?P<rows>\d{1,2})\s*[xX×]\s*(?P<cols>\d{1,2})$"
)
_MAX_GRID = 10
_ORIENT_ALIAS = {"横向": "landscape", "竖向": "portrait"}


def _parse_dynamic_layout(spec: str) -> MergeLayout | None:
    """解析 ``横向3x5`` 形式的动态布局；不匹配返回 ``None``，越界抛 ``ValueError``。"""
    match = _DYNAMIC_LAYOUT.match(spec.strip())
    if not match:
        return None
    orient_raw = match.group("orient").lower()
    orientation = _ORIENT_ALIAS.get(orient_raw, orient_raw)
    rows, cols = int(match.group("rows")), int(match.group("cols"))
    if not (1 <= rows <= _MAX_GRID and 1 <= cols <= _MAX_GRID):
        raise ValueError(
            f"动态布局行列须在 1–{_MAX_GRID} 之间，收到 {rows}x{cols}"
        )
    return MergeLayout(orientation=orientation, rows=rows, cols=cols)


def resolve_layout(spec: str | dict | MergeLayout | None) -> MergeLayout:
    """把预设名 / 动态布局串 / 字典 / 对象统一解析为 :class:`MergeLayout`。

    动态布局格式：``横向RxC`` / ``竖向RxC``（R、C 均为 1–10），
    兼容 ``landscape3x3`` / ``竖向 2x4`` 等写法。
    """
    if isinstance(spec, MergeLayout):
        return spec
    if isinstance(spec, dict):
        return MergeLayout(**spec)
    if isinstance(spec, str):
        normalized = spec.replace(" ", "")
        if normalized in PRESET_LAYOUTS:
            return MergeLayout(**PRESET_LAYOUTS[normalized])
        dynamic = _parse_dynamic_layout(spec)
        if dynamic is not None:
            return dynamic
        raise ValueError(
            f"未知布局: {spec}；可用预设: {', '.join(PRESET_LAYOUTS)}；"
            f"或用 横向RxC / 竖向RxC（1–{_MAX_GRID}）自定义"
        )
    if spec is None:
        return MergeLayout()
    raise TypeError(f"无法解析布局类型: {type(spec).__name__}")


# ---------------------------------------------------------------------------
# 合并器
# ---------------------------------------------------------------------------


class Merger:
    """PDF / 图片合并器。"""

    def __init__(self, layout: MergeLayout | str | dict | None = None) -> None:
        self.layout = resolve_layout(layout)
        self._require_fitz()

    @staticmethod
    def _require_fitz() -> None:
        pdfmod.require_pymupdf()

    def merge(
        self,
        inputs: Iterable[str | Path],
        output_path: str | Path,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """把多个文件合并为一份 PDF。

        Args:
            inputs: 输入文件（PDF 或图片），顺序即排版顺序
            output_path: 输出路径
            progress: 进度回调 (current, total)

        Returns:
            输出文件路径

        Raises:
            MergeError: 无输入、路径无效、或合并过程失败
        """
        import fitz  # 延迟导入，缺依赖时报清晰错误

        files = [Path(p).expanduser() for p in inputs]
        files = [p for p in files if p.is_file()]
        if not files:
            raise MergeError("没有可合并的文件")

        target = Path(output_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)

        layout = self.layout
        temp_files: list[str] = []
        total_files = len(files)
        # 全局格号游标：跨批次延续
        next_slot = 0

        # 批次大小必须对齐到页边界。
        #
        # 分批合并用的是「独立临时文件 + insert_pdf 拼接」，PyMuPDF 没有
        # 「接着上一页继续写」的能力。若批次在页中间断开，拼接后就会多出
        # 半空页碎片（4 个文件 / 每页 4 格 / batch=1 -> 本该 1 页，
        # 实际得到 4 页，每页只放 1 个）。
        #
        # 做法：把有效批大小向上取整到 cells_per_page 的倍数，
        # 让每个临时文件都以整页收尾。
        effective_batch = _round_to(layout.batch_size, layout.cells_per_page, up=True)
        total_steps = total_files + math.ceil(total_files / effective_batch)

        try:
            # 阶段一：分批生成中间 PDF
            for start in range(0, total_files, effective_batch):
                batch = files[start : start + effective_batch]
                batch_doc = fitz.open()
                try:
                    next_slot = self._merge_batch(
                        batch, batch_doc, progress, start, total_steps, next_slot
                    )
                    fd, temp_path = tempfile.mkstemp(suffix=".pdf")
                    os.close(fd)
                    batch_doc.save(temp_path)
                    temp_files.append(temp_path)
                finally:
                    batch_doc.close()

            # 阶段二：拼接为最终输出
            output_doc = fitz.open()
            try:
                for index, temp_path in enumerate(temp_files):
                    with fitz.open(temp_path) as doc:
                        output_doc.insert_pdf(doc)
                    if progress:
                        progress(total_files + index + 1, total_steps)

                if output_doc.page_count == 0:
                    raise MergeError("合并结果为空")
                page_count = output_doc.page_count
                output_doc.save(str(target), garbage=3, deflate=True)
            finally:
                output_doc.close()

            logger.info(
                "合并完成: %d 个文件 -> %s（%d 页）",
                total_files,
                target.name,
                page_count,
            )
            return target

        except MergeError:
            raise
        except Exception as exc:
            logger.exception("合并失败")
            raise MergeError(f"合并失败: {exc}") from exc
        finally:
            for temp_path in temp_files:
                try:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                except OSError:
                    pass

    def _merge_batch(
        self,
        files: Sequence[Path],
        batch_doc,
        progress: Callable[[int, int], None] | None,
        progress_offset: int,
        total_steps: int,
        start_slot: int,
    ) -> int:
        """把一批文件排版进目标文档。

        Args:
            files: 本批文件
            batch_doc: 目标文档
            progress: 进度回调
            progress_offset: 进度基数
            total_steps: 总步数
            start_slot: 本批第一个文件应排入的**全局格号**

                分批合并的根本矛盾：PyMuPDF 的 Document 不能"接着上一个
                文档的页继续写"，每批都是全新文档。所以格号必须是全局的，
                否则批次拼接后会各自从页首重排。

                做法：``全局页号 = slot // cells_per_page``，
                ``页内偏移 = slot % cells_per_page``。
                批次内需要开几页由全局页号的跨度决定，
                这样批次之间能自然接上，不会产生半空页碎片。

        Returns:
            本批结束后的全局格号（下一批的起始格号）
        """
        layout = self.layout
        cells = layout.cells_per_page

        slot = start_slot
        # 本批文档的第一个页，对应全局页号 first_global_page
        first_global_page = slot // cells
        current_page = None
        current_global_page = first_global_page

        def new_page():
            page = batch_doc.new_page(width=layout.page_width, height=layout.page_height)
            if layout.draw_dividers and cells > 1:
                self._draw_dividers(page, layout)
            if layout.show_crop_marks:
                self._draw_crop_marks(page, layout)
            return page

        def place(slot_number: int):
            """把一个文件排入 ``slot_number`` 对应的格子。"""
            nonlocal current_page, current_global_page

            global_page = slot_number // cells
            in_page = slot_number % cells

            if current_page is None or global_page != current_global_page:
                current_page = new_page()
                current_global_page = global_page

            slot_box = self._slot_box(in_page)
            return current_page, slot_box

        for index, path in enumerate(files):
            try:
                if path.suffix.lower() in IMAGE_EXTENSIONS:
                    page, slot_box = place(slot)
                    self._place_image(page, path, *slot_box)
                    slot += 1
                else:
                    # 多页源文件需逐页占格，页数可能超出当前页容量
                    for page_offset in range(self._count_pages(path)):
                        page, slot_box = place(slot)
                        self._place_pdf(page, path, page_offset, *slot_box)
                        slot += 1
            except Exception as exc:
                # 单个文件失败不影响整批
                logger.warning("处理文件失败，已跳过 %s: %s", path.name, exc)

            if progress:
                progress(progress_offset + index + 1, total_steps)

        return slot

    def _slot_box(self, in_page_slot: int) -> tuple[float, float, float, float]:
        """计算**页内**格子的 (x, y, width, height)。

        Args:
            in_page_slot: 页内偏移，范围 [0, cells_per_page)。
                传全局格号会算出越界的行列——调用方需先做 ``% cells``。
        """
        layout = self.layout
        row, col = divmod(in_page_slot, layout.cols)
        cell_w = layout.available_width / layout.cols
        cell_h = layout.available_height / layout.rows
        x = layout.margin_left * MM_TO_PT + col * cell_w
        y = layout.margin_top * MM_TO_PT + row * cell_h
        return x, y, cell_w, cell_h

    @staticmethod
    def _count_pages(pdf_path: Path) -> int:
        """读取 PDF 页数，失败按 1 页计。"""
        import fitz

        try:
            with fitz.open(pdf_path) as doc:
                return max(1, doc.page_count)
        except Exception:
            return 1

    def _place_pdf(
        self,
        page,
        pdf_path: Path,
        page_index: int,
        x: float,
        y: float,
        cell_w: float,
        cell_h: float,
    ) -> None:
        """把 PDF 的第 ``page_index`` 页放进单元格。"""
        import fitz

        layout = self.layout
        with fitz.open(pdf_path) as doc:
            if page_index >= doc.page_count:
                return
            src = doc[page_index]
            src_rect = src.rect

            if layout.rotate == 90:
                scale = min(
                    (cell_w - layout.gap) / src_rect.height,
                    (cell_h - layout.gap) / src_rect.width,
                )
                w, h = src_rect.height * scale, src_rect.width * scale
            else:
                scale = min(
                    (cell_w - layout.gap) / src_rect.width,
                    (cell_h - layout.gap) / src_rect.height,
                )
                w, h = src_rect.width * scale, src_rect.height * scale

            offset_x = (cell_w - w) / 2
            offset_y = (cell_h - h) / 2
            target = fitz.Rect(
                x + offset_x, y + offset_y, x + offset_x + w, y + offset_y + h
            )

            if layout.mode == "raster":
                self._place_raster(page, src, target)
            else:
                page.show_pdf_page(target, doc, page_index, rotate=layout.rotate)
                # 关键：矢量模式不复制注释，必须单独贴回监制章
                self._copy_stamps(
                    page, src, doc, page_index, x, y, offset_x, offset_y, scale
                )

    def _place_raster(self, page, src_page, target) -> None:
        """栅格模式：渲染成图后插入。"""
        pixmap = src_page.get_pixmap(dpi=150)
        page.insert_image(target, pixmap=pixmap)

    def _place_image(
        self, page, image_path: Path, x: float, y: float, cell_w: float, cell_h: float
    ) -> None:
        """把图片放进单元格（等比缩放居中）。"""
        import fitz

        layout = self.layout
        try:
            pixmap = fitz.Pixmap(str(image_path))
        except Exception:
            from PIL import Image  # type: ignore[import-untyped]

            with Image.open(image_path) as img:
                buffer = BytesIO()
                img.convert("RGB").save(buffer, format="PNG")
                buffer.seek(0)
                pixmap = fitz.Pixmap(buffer)

        scale = min(
            (cell_w - layout.gap) / pixmap.width,
            (cell_h - layout.gap) / pixmap.height,
        )
        w, h = pixmap.width * scale, pixmap.height * scale
        target = fitz.Rect(
            x + (cell_w - w) / 2,
            y + (cell_h - h) / 2,
            x + (cell_w - w) / 2 + w,
            y + (cell_h - h) / 2 + h,
        )
        page.insert_image(target, pixmap=pixmap)

    def _copy_stamps(
        self,
        page,
        src_page,
        doc,
        page_index: int,
        x: float,
        y: float,
        offset_x: float,
        offset_y: float,
        scale: float,
    ) -> None:
        """复制 Stamp 类型注释（监制章）。

        ``show_pdf_page`` 只搬页面内容不搬注释，增值税发票的监制章
        会静默消失——这里用 450dpi 裁剪渲染该区域再贴回原位。
        """
        import fitz

        try:
            annots = src_page.annots()
            if not annots:
                return

            for annot in annots:
                # PyMuPDF: annot.type[0] == 13 即 Stamp
                if annot.type[0] != 13:
                    continue

                # 高 DPI 渲染保证印章放大后仍清晰
                pixmap = src_page.get_pixmap(dpi=450, clip=annot.rect)
                rect = annot.rect
                new_rect = fitz.Rect(
                    x + offset_x + rect.x0 * scale,
                    y + offset_y + rect.y0 * scale,
                    x + offset_x + rect.x1 * scale,
                    y + offset_y + rect.y1 * scale,
                )
                page.insert_image(new_rect, pixmap=pixmap)
                logger.debug("已复制监制章到 %s", new_rect)
        except Exception as exc:
            # 注释处理失败不应导致整个合并失败，但必须留下痕迹：
            # 这里曾是静默 except，导致 fitz 未导入的 bug 藏了很久
            logger.warning("复制监制章失败（%s）: %s", src_page.number + 1, exc)

    def _draw_dividers(self, page, layout: MergeLayout) -> None:
        """绘制单元格分割线（虚线）。"""
        import fitz

        left = layout.margin_left * MM_TO_PT
        top = layout.margin_top * MM_TO_PT
        cell_w = layout.available_width / layout.cols
        cell_h = layout.available_height / layout.rows

        for i in range(1, layout.rows):
            y = top + i * cell_h
            self._dashed(
                page, fitz.Point(left, y), fitz.Point(left + layout.available_width, y)
            )
        for i in range(1, layout.cols):
            x = left + i * cell_w
            self._dashed(
                page, fitz.Point(x, top), fitz.Point(x, top + layout.available_height)
            )

    def _draw_crop_marks(self, page, layout: MergeLayout) -> None:
        """绘制左右裁切线。"""
        import fitz

        if layout.crop_mark_left > 0:
            x = layout.crop_mark_left * MM_TO_PT
            self._dashed(page, fitz.Point(x, 0), fitz.Point(x, layout.page_height))
        if layout.crop_mark_right > 0:
            x = layout.page_width - layout.crop_mark_right * MM_TO_PT
            self._dashed(page, fitz.Point(x, 0), fitz.Point(x, layout.page_height))

    @staticmethod
    def _dashed(
        page,
        start,
        end,
        *,
        dash: float = 5.0,
        gap: float = 3.0,
        width: float = 0.5,
        color=(0.7, 0.7, 0.7),
    ) -> None:
        """画虚线。"""
        import fitz

        dx, dy = end.x - start.x, end.y - start.y
        length = math.hypot(dx, dy)
        if length == 0:
            return
        ux, uy = dx / length, dy / length

        position = 0.0
        is_dash = True
        while position < length:
            if is_dash:
                seg_end = min(position + dash, length)
                page.draw_line(
                    fitz.Point(start.x + ux * position, start.y + uy * position),
                    fitz.Point(start.x + ux * seg_end, start.y + uy * seg_end),
                    color=color,
                    width=width,
                )
                position = seg_end
            else:
                position = min(position + gap, length)
            is_dash = not is_dash
