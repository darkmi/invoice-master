"""PDF 文本层访问。

集中封装 PyMuPDF 的使用，向上层暴露"纯文本 + 词坐标"两种视图：

- :func:`extract_text` 返回整篇文本，供正则匹配
- :func:`extract_words` 返回带坐标的词，供按版面分栏（增值税发票左右分栏）

为什么不直接把 PyMuPDF 泄漏到提取器里：提取器需要能被单元测试
（喂假文本），而不该每个测试都造一个真 PDF。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# PyMuPDF 缺失时给出明确提示，而不是在 import 时抛裸 ImportError
try:
    import fitz  # type: ignore[import-untyped]

    HAS_PYMUPDF = True
    PYMUPDF_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover
    fitz = None  # type: ignore[assignment]
    HAS_PYMUPDF = False
    PYMUPDF_IMPORT_ERROR = str(exc)


class PdfError(RuntimeError):
    """PDF 解析异常。"""


def require_pymupdf() -> None:
    """确保 PyMuPDF 可用，否则抛出带安装指引的异常。"""
    if not HAS_PYMUPDF:
        raise PdfError(
            "缺少 PyMuPDF 依赖，无法处理 PDF 文件。\n"
            "请执行: pip install pymupdf\n"
            f"原始错误: {PYMUPDF_IMPORT_ERROR}"
        )


@dataclass(frozen=True)
class Word:
    """带坐标的词。"""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        """中心点 x 坐标。"""
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        """中心点 y 坐标。"""
        return (self.y0 + self.y1) / 2


@dataclass(frozen=True)
class PdfDoc:
    """PDF 文本提取结果。"""

    text: str
    words: tuple[Word, ...]
    page_count: int
    page_size: tuple[float, float]  # (width, height) of first page
    has_text_layer: bool

    @property
    def is_scanned(self) -> bool:
        """是否疑似扫描件（无文本层，需 OCR）。"""
        return not self.has_text_layer


def extract(
    pdf_path: str | Path,
    *,
    max_pages: int = 20,
    need_words: bool = True,
) -> PdfDoc:
    """提取 PDF 文本与词坐标。

    Args:
        pdf_path: PDF 路径
        max_pages: 最多解析多少页，防止超大文件卡死
        need_words: 是否提取词坐标（版面分析需要）

    Returns:
        PdfDoc

    Raises:
        PdfError: 文件不存在、加密、损坏
    """
    require_pymupdf()
    path = Path(pdf_path).expanduser()

    if not path.is_file():
        raise PdfError(f"文件不存在: {path}")

    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise PdfError(f"无法打开 PDF ({path.name}): {exc}") from exc

    try:
        if doc.needs_pass:
            raise PdfError(f"PDF 已加密，无法解析: {path.name}")

        page_count = doc.page_count
        limit = min(page_count, max_pages)

        text_parts: list[str] = []
        all_words: list[Word] = []
        page_size = (0.0, 0.0)

        for index in range(limit):
            page = doc[index]
            if index == 0:
                page_size = (float(page.rect.width), float(page.rect.height))

            # sort=True 让输出接近视觉阅读顺序，对正则友好
            text_parts.append(page.get_text("text", sort=True))

            if need_words:
                for raw in page.get_text("words"):
                    # words 元组结构: (x0, y0, x1, y1, word, block_no, line_no, word_no)
                    all_words.append(
                        Word(
                            text=str(raw[4]),
                            x0=float(raw[0]),
                            y0=float(raw[1]),
                            x1=float(raw[2]),
                            y1=float(raw[3]),
                        )
                    )

        full_text = "\n".join(text_parts)
        # 有效文本层判定：去掉空白后至少有实际字符。
        # 扫描件常残留页码/页眉，故取一个宽松阈值。
        meaningful = "".join(full_text.split())
        has_layer = len(meaningful) >= 32

        return PdfDoc(
            text=full_text,
            words=tuple(all_words),
            page_count=page_count,
            page_size=page_size,
            has_text_layer=has_layer,
        )
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError(f"PDF 解析失败 ({path.name}): {exc}") from exc
    finally:
        doc.close()


def render_page_image(
    pdf_path: str | Path,
    *,
    page_index: int = 0,
    dpi: int = 150,
    max_px: int = 0,
) -> bytes:
    """把指定页渲染为 PNG 字节流，供 LLM 视觉兜底使用。

    Args:
        pdf_path: PDF 路径
        page_index: 页码，从 0 开始
        dpi: 渲染精度
        max_px: 最长边上限像素，超过则等比缩放（控制 LLM token 消耗）

    Returns:
        PNG 格式的字节数据
    """
    require_pymupdf()
    path = Path(pdf_path).expanduser()

    try:
        with fitz.open(path) as doc:
            if doc.needs_pass:
                raise PdfError(f"PDF 已加密: {path.name}")
            page = doc[min(page_index, doc.page_count - 1)]

            # 先按 dpi 渲染，再按需缩小。
            # 缩小用「降低 dpi 重新渲染」而非 Pixmap 变换——
            # 后者需要构造 Matrix，且不同 PyMuPDF 版本 API 有差异，
            # 实测 fitz.Pixmap(pixmap, 0) 会抛
            # "isinstance() arg 2 must be a type"（第二个参数期望 Matrix）。
            pixmap = page.get_pixmap(dpi=dpi)

            longest = max(pixmap.width, pixmap.height)
            if max_px and longest > max_px:
                scale = max_px / longest
                target_dpi = max(36, int(dpi * scale))  # 下限 36，避免过小
                pixmap = page.get_pixmap(dpi=target_dpi)

            return pixmap.tobytes("png")
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError(f"页面渲染失败 ({path.name}): {exc}") from exc


def split_columns(words: list[Word], page_height: float = 842.0) -> tuple[str, str]:
    """按页面中线把词分成左右两栏文本。

    增值税发票是典型的左右分栏版面：左边购买方，右边销售方。
    直接按文本流顺序提``名称:`` 会把买卖方搞反——因为 PDF 的
    文本绘制顺序与视觉顺序不一定一致。必须用坐标分栏。

    **单栏页面返回空字符串**：很多发票（尤其数电票、定额票）
    是单栏排版，此时强行分栏会把整页文本塞进左栏或右栏，
    导致买卖方提取完全错乱。调用方必须检查返回值是否为空。

    Args:
        words: 词列表
        page_height: 页高（保留参数，便于后续按高度分区）

    Returns:
        (左栏文本, 右栏文本)；单栏或无词时返回 ("", "")
    """
    if len(words) < 4:
        return "", ""

    min_x = min(w.x0 for w in words)
    max_x = max(w.x1 for w in words)
    span = max_x - min_x
    if span <= 0:
        return "", ""

    mid_x = (min_x + max_x) / 2

    left = [w for w in words if w.cx < mid_x]
    right = [w for w in words if w.cx > mid_x]

    # 单栏判定：中线一侧几乎没词，说明实际是单栏排版
    # 阈值取 15%——真实的左右分栏版面两侧都会有实质内容
    if len(left) < len(words) * 0.15 or len(right) < len(words) * 0.15:
        return "", ""

    return _join(left), _join(right)


def _join(words: list[Word]) -> str:
    """按 (y, x) 排序后拼接成行文本。"""
    ordered = sorted(words, key=lambda w: (round(w.y0, 1), w.x0))
    lines: list[str] = []
    current: list[str] = []
    last_y: float | None = None

    for word in ordered:
        # 同一行判定：垂直中心偏差小于一个字高
        if last_y is not None and abs(word.cy - last_y) > 4:
            lines.append(" ".join(current))
            current = []
        current.append(word.text)
        last_y = word.cy if last_y is None else last_y

    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


def detect_encoding_hint(data: bytes) -> str | None:
    """从 XML/OFD 字节流猜测是否含发票 XML。

    数电发票的 XML 以 ``<?xml`` 开头，内含 ``<InvoiceData>`` 根元素。
    """
    head = data[:2048].lstrip()
    if not head.startswith(b"<?xml") and not head.startswith(b"<"):
        return None
    try:
        decoded = head.decode("utf-8", errors="ignore")
    except Exception:
        return None
    if "InvoiceData" in decoded or "invoice" in decoded.lower():
        return "xml"
    return None
