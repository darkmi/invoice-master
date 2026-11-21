"""PDF 合并测试。

重点验证从参考项目继承的关键细节：
* 分批合并不爆内存
* 矢量模式**保留监制章**（``show_pdf_page`` 不搬注释，必须单独贴回）
* 多页源文件正确换格
* 单栏/双栏版面计算

运行::

    pytest tests/test_merger.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_master.config import Config  # noqa: E402
from invoice_master.merger import (  # noqa: E402
    A4_LANDSCAPE,
    A4_PORTRAIT,
    MM_TO_PT,
    PRESET_LAYOUTS,
    MergeError,
    MergeLayout,
    Merger,
    resolve_layout,
    resolve_page_size,
)

pymupdf = pytest.importorskip("pymupdf")

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture
def simple_pdf(tmp_path):
    """生成一个指定页数与尺寸的简单 PDF。"""

    def _make(name: str, pages: int = 1, width: float = 595, height: float = 842):
        path = tmp_path / name
        doc = pymupdf.open()
        for i in range(pages):
            page = doc.new_page(width=width, height=height)
            page.insert_text(
                (72, 100 + i * 20),
                f"TEST PAGE {i + 1}",
                fontsize=18,
                fontname="china-s",
            )
        doc.save(path)
        doc.close()
        return path

    return _make


# ---------------------------------------------------------------------------
# 布局配置
# ---------------------------------------------------------------------------


class TestMergeLayout:
    def test_orientation_sets_page_size(self):
        landscape = MergeLayout(orientation="landscape")
        portrait = MergeLayout(orientation="portrait")
        assert landscape.page_width > landscape.page_height
        assert portrait.page_height > portrait.page_width

    def test_cells_per_page(self):
        assert MergeLayout(rows=2, cols=3).cells_per_page == 6

    def test_available_area_accounts_for_margins(self):
        layout = MergeLayout(
            orientation="landscape",
            margin_left=10,
            margin_right=10,
            margin_top=20,
            margin_bottom=5,
        )
        expected_w = A4_LANDSCAPE[0] - (10 + 10) * MM_TO_PT
        expected_h = A4_LANDSCAPE[1] - (20 + 5) * MM_TO_PT
        assert abs(layout.available_width - expected_w) < 0.01
        assert abs(layout.available_height - expected_h) < 0.01

    def test_invalid_orientation_raises(self):
        with pytest.raises(ValueError, match="orientation"):
            MergeLayout(orientation="diagonal")

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="mode"):
            MergeLayout(mode="magic")

    def test_zero_rows_rejected(self):
        """rows=0 会导致除零错误，必须提前拦。"""
        with pytest.raises(ValueError, match="rows"):
            MergeLayout(rows=0)

    def test_mode_case_insensitive(self):
        assert MergeLayout(mode="VECTOR").mode == "vector"

    def test_from_preset_landscape(self):
        layout = resolve_layout("横向2x2")
        assert layout.orientation == "landscape"
        assert layout.rows == 2 and layout.cols == 2
        assert layout.page_size == A4_LANDSCAPE

    def test_from_preset_portrait(self):
        layout = resolve_layout("竖向1x2")
        assert layout.orientation == "portrait"
        assert layout.page_size == A4_PORTRAIT

    def test_all_presets_resolvable(self):
        for name in PRESET_LAYOUTS:
            layout = resolve_layout(name)
            assert layout.cells_per_page >= 1

    def test_unknown_preset_raises_with_list(self):
        with pytest.raises(ValueError) as exc:
            resolve_layout("斜向9x9")
        assert "横向2x2" in str(exc.value)

    def test_preset_ignores_spaces(self):
        assert resolve_layout("横向 2x2").cells_per_page == 4

    # --- 动态布局：从 piaoyihe 吸收的 1x1–10x10 排版 ---
    def test_dynamic_layout_full_range(self):
        """1x1 到 10x10 全部可解析，排列数正确。"""
        for rows in range(1, 11):
            for cols in range(1, 11):
                layout = resolve_layout(f"横向{rows}x{cols}")
                assert layout.rows == rows
                assert layout.cols == cols
                assert layout.cells_per_page == rows * cols

    def test_dynamic_layout_portrait_and_aliases(self):
        assert resolve_layout("竖向10x10").orientation == "portrait"
        assert resolve_layout("landscape3x5").orientation == "landscape"
        assert resolve_layout("portrait 2x4").cells_per_page == 8
        assert resolve_layout("横向 9×9").cells_per_page == 81

    def test_dynamic_layout_out_of_range(self):
        with pytest.raises(ValueError, match="1–10"):
            resolve_layout("横向11x2")
        with pytest.raises(ValueError, match="1–10"):
            resolve_layout("竖向2x0")

    def test_resolve_from_dict(self):
        layout = resolve_layout({"orientation": "portrait", "rows": 1, "cols": 1})
        assert layout.cells_per_page == 1

    def test_resolve_passthrough_object(self):
        original = MergeLayout(rows=3, cols=3)
        assert resolve_layout(original) is original

    # ---- 纸张尺寸与配置读取 ----

    def test_page_size_follows_orientation(self):
        assert resolve_page_size("A4", "landscape") == A4_LANDSCAPE
        assert resolve_page_size("A4", "portrait") == A4_PORTRAIT

    def test_page_size_swaps_landscape(self):
        a3_l = resolve_page_size("A3", "landscape")
        a3_p = resolve_page_size("A3", "portrait")
        assert a3_l[0] > a3_l[1]
        assert a3_p[0] < a3_p[1]
        assert sorted(a3_l) == sorted(a3_p)

    def test_page_size_unknown_falls_back_to_a4(self):
        assert resolve_page_size("B5", "landscape") == A4_LANDSCAPE

    def test_from_config_reads_merge_section(self):
        config = Config(data={"merge": {"rows": 3, "cols": 3, "gap": 9}})
        layout = MergeLayout.from_config(config)
        assert (layout.rows, layout.cols) == (3, 3)
        assert layout.gap == 9

    def test_from_config_page_follows_orientation(self):
        portrait = MergeLayout.from_config(
            Config(data={"merge": {"orientation": "portrait", "page": "A4"}})
        )
        assert portrait.page_size == A4_PORTRAIT

        landscape_a3 = MergeLayout.from_config(
            Config(data={"merge": {"orientation": "landscape", "page": "A3"}})
        )
        assert landscape_a3.page_size[0] > landscape_a3.page_size[1]


# ---------------------------------------------------------------------------
# 合并功能
# ---------------------------------------------------------------------------


class TestMerge:
    def test_merge_two_pages(self, simple_pdf, tmp_path):
        """两张单页发票在 2x1 布局下应占同一页。"""
        a = simple_pdf("a.pdf", pages=1)
        b = simple_pdf("b.pdf", pages=1)
        out = tmp_path / "out.pdf"

        Merger("竖向1x2").merge([a, b], out)
        with pymupdf.open(out) as doc:
            assert doc.page_count == 1

    def test_merge_overflow_creates_new_page(self, simple_pdf, tmp_path):
        """3 张塞进 2x1 的第 2 格，第 3 张必须换页。"""
        files = [simple_pdf(f"{i}.pdf", pages=1) for i in range(3)]
        out = tmp_path / "out.pdf"

        Merger("竖向1x2").merge(files, out)
        with pymupdf.open(out) as doc:
            assert doc.page_count == 2

    def test_multipage_source_occupies_multiple_cells(self, simple_pdf, tmp_path):
        """多页 PDF 每页应占一格，而不是被塞进同一格。"""
        files = [
            simple_pdf("single.pdf", pages=1),
            simple_pdf("triple.pdf", pages=3),
        ]
        out = tmp_path / "out.pdf"

        # 2x1 = 每页 2 格；1 + 3 = 4 格 -> 需要 2 页
        Merger("竖向1x2").merge(files, out)
        with pymupdf.open(out) as doc:
            assert doc.page_count == 2

    def test_empty_input_raises(self, tmp_path):
        with pytest.raises(MergeError, match="没有可合并"):
            Merger("横向2x2").merge([], tmp_path / "out.pdf")

    def test_missing_files_skipped(self, simple_pdf, tmp_path):
        real = simple_pdf("real.pdf", pages=1)
        out = tmp_path / "out.pdf"

        Merger("横向2x2").merge([real, tmp_path / "ghost.pdf"], out)
        assert out.exists()

    def test_output_directory_created(self, simple_pdf, tmp_path):
        source = simple_pdf("a.pdf")
        out = tmp_path / "nested" / "deep" / "out.pdf"

        Merger("横向2x2").merge([source], out)
        assert out.exists()

    def test_all_files_skipped_raises(self, tmp_path):
        with pytest.raises(MergeError):
            Merger("横向2x2").merge([tmp_path / "ghost.pdf"], tmp_path / "o.pdf")

    def test_batch_processing(self, simple_pdf, tmp_path):
        """batch_size=1 强制走多批次，结果应与不分批一致。"""
        files = [simple_pdf(f"{i}.pdf", pages=1) for i in range(4)]
        out = tmp_path / "out.pdf"

        Merger({"orientation": "landscape", "rows": 1, "cols": 4, "batch_size": 1}).merge(
            files, out
        )
        with pymupdf.open(out) as doc:
            assert doc.page_count == 1

    def test_progress_callback(self, simple_pdf, tmp_path):
        files = [simple_pdf(f"{i}.pdf") for i in range(3)]
        calls: list[tuple[int, int]] = []

        Merger("竖向1x3").merge(
            files,
            tmp_path / "out.pdf",
            progress=lambda c, t: calls.append((c, t)),
        )
        assert calls
        # 进度单调不减
        assert [c for c, _ in calls] == sorted(c for c, _ in calls)

    def test_invalid_layout_name(self, tmp_path, simple_pdf):
        source = simple_pdf("a.pdf")
        with pytest.raises(ValueError):
            Merger("不存在布局").merge([source], tmp_path / "o.pdf")


# ---------------------------------------------------------------------------
# 监制章保留（关键回归）
# ---------------------------------------------------------------------------


class TestStampPreservation:
    """增值税发票的监制章是 PDF 注释对象。

    ``show_pdf_page``（矢量模式）**只搬页面内容不搬注释**，
    不做额外处理的话监制章会静默消失。参考项目踩过这个坑，
    这里用带 Stamp 注释的 PDF 做回归。
    """

    def _make_stamped_pdf(self, path: Path) -> Path:
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "增值税专用发票", fontsize=18, fontname="china-s")

        annot = page.add_stamp_annot(pymupdf.Rect(400, 700, 520, 780))
        annot.update()

        doc.save(path)
        doc.close()
        return path

    def test_source_has_stamp(self, tmp_path):
        source = self._make_stamped_pdf(tmp_path / "stamped.pdf")
        with pymupdf.open(source) as doc:
            page = doc[0]
            annots = list(page.annots())
            assert annots, "测试前提：源PDF 应含注释"
            # 必须在 with 块内访问 type —— 离开后 annot 失去页面绑定
            assert annots[0].type[0] == 13  # Stamp

    def test_vector_mode_preserves_stamp(self, tmp_path):
        """矢量模式下监制章必须以图片形式回到页面上。

        这条曾经因为 `fitz` 未导入被静默 except 吞掉，
        导致监制章无声消失 —— 必须守住。
        """
        source = self._make_stamped_pdf(tmp_path / "stamped.pdf")
        out = tmp_path / "out.pdf"

        Merger(
            {"orientation": "landscape", "rows": 1, "cols": 1, "mode": "vector"}
        ).merge([source], out)

        with pymupdf.open(out) as doc:
            page = doc[0]
            # 注释不会复制，但图片必须存在（渲染后贴回）
            assert len(page.get_images(full=True)) > 0, (
                "监制章丢失：矢量模式下应将其渲染为图片贴回页面"
            )

    def test_raster_mode_keeps_content(self, tmp_path):
        source = self._make_stamped_pdf(tmp_path / "stamped.pdf")
        out = tmp_path / "out.pdf"

        Merger(
            {"orientation": "landscape", "rows": 1, "cols": 1, "mode": "raster"}
        ).merge([source], out)

        with pymupdf.open(out) as doc:
            assert len(doc[0].get_images(full=True)) > 0

    def test_landscape_page_size_applied(self, tmp_path, simple_pdf):
        source = simple_pdf("a.pdf")
        out = tmp_path / "out.pdf"

        Merger("横向2x2").merge([source], out)
        with pymupdf.open(out) as doc:
            page = doc[0]
            assert page.rect.width > page.rect.height

    def test_portrait_page_size_applied(self, tmp_path, simple_pdf):
        source = simple_pdf("a.pdf")
        out = tmp_path / "out.pdf"

        Merger("竖向1x2").merge([source], out)
        with pymupdf.open(out) as doc:
            page = doc[0]
            assert page.rect.height > page.rect.width
