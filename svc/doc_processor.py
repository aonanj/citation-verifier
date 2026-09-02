# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

import os
import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, Final, Iterable, List, Optional, Sequence, Set, Tuple
from xml.etree import ElementTree as ET

import pymupdf
import pytesseract
from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl  # type: ignore
from docx.oxml.text.paragraph import CT_P  # type: ignore
from docx.table import Table, _Cell  # type: ignore
from docx.text.paragraph import Paragraph
from PIL import Image
from werkzeug.datastructures import FileStorage

from utils.logger import get_logger

logger = get_logger()

_HYPHEN_WRAP_RE: Final = re.compile(r"(\w)-\n(\w)")
_EXCESS_BREAKS_RE: Final = re.compile(r"\n{3,}")
_SMART_QUOTES_RE: Final = re.compile("[\u201c\u201d]")
_SMART_APOSTROPHES_RE: Final = re.compile("[\u2018\u2019]")
_SUPERSCRIPT_TRANSLATION: Final = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_SUPERSCRIPT_CHARACTERS: Final = frozenset("⁰¹²³⁴⁵⁶⁷⁸⁹")
_FOOTNOTE_LINE_RE: Final = re.compile(r"^\s*([\d⁰¹²³⁴⁵⁶⁷⁸⁹]+)[\.\)]?\s*(.*)")
_FOOTNOTE_START_RE: Final = re.compile(r"^\s*([\d⁰¹²³⁴⁵⁶⁷⁸⁹]+)([\.\)])?\s*(.*)")

# Private-use-area sentinels used to wrap an inlined footnote body between the
# moment it's spliced into the running text and _strip_footnote_markers(),
# which runs after _normalize() and converts the wrapped bodies back into
# plain text plus a FootnoteSpan(number, start, end) per body. Chosen from the
# Unicode Private Use Area (U+E000-U+F8FF, general category "Co") specifically
# because \w, \s and every regex used in this module (_HYPHEN_WRAP_RE,
# _EXCESS_BREAKS_RE, the smart-quote/apostrophe subs, _needs_space_between's
# isalnum() check) do not match "Co" characters, so normalization sees these
# markers as inert punctuation and never corrupts them.
_FN_OPEN: Final = ""
_FN_SEP: Final = ""
_FN_CLOSE: Final = ""

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS = {"w": _W_NS}


@dataclass(frozen=True)
class FootnoteSpan:
    """A footnote's location within an ExtractedDocument's `text`.

    `start`/`end` index into the fully normalized, sentinel-stripped text
    (i.e. they are valid offsets into ExtractedDocument.text), not into any
    intermediate representation.
    """

    number: int
    start: int
    end: int


@dataclass(frozen=True)
class ExtractedDocument:
    """Result of extracting a document: flat text plus footnote provenance."""

    text: str
    footnotes: Tuple[FootnoteSpan, ...]


def _wrap_footnote(number: int, body: str) -> str:
    """Wrap a footnote body in sentinel markers for later recovery.

    Must only be called with an already-stripped, non-empty `body` - an empty
    body would produce a marker with nothing between _FN_SEP and _FN_CLOSE,
    which _strip_footnote_markers still handles correctly (a zero-length
    FootnoteSpan) but which is never useful to a caller.
    """
    return f"{_FN_OPEN}{number}{_FN_SEP}{body}{_FN_CLOSE}"


def _strip_footnote_markers(text: str) -> Tuple[str, Tuple[FootnoteSpan, ...]]:
    """Remove _wrap_footnote() markers, returning plain text plus their spans.

    Single left-to-right pass so offsets are always consistent with the
    output text (no double-pass drift). A malformed marker (an _FN_OPEN with
    no matching _FN_SEP/_FN_CLOSE, or a stray _FN_SEP/_FN_CLOSE with no
    opener - not expected from this module's own writers, but guarded against
    defensively) has its sentinel character(s) dropped without emitting a
    FootnoteSpan, rather than corrupting the surrounding text.
    """
    footnotes: List[FootnoteSpan] = []
    out: List[str] = []
    out_len = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == _FN_OPEN:
            close_idx = text.find(_FN_CLOSE, i + 1)
            sep_idx = text.find(_FN_SEP, i + 1, close_idx if close_idx != -1 else n)
            number_str = text[i + 1:sep_idx] if sep_idx != -1 else ""
            if close_idx != -1 and sep_idx != -1 and number_str.isdigit():
                body = text[sep_idx + 1:close_idx]
                start = out_len
                out.append(body)
                out_len += len(body)
                footnotes.append(FootnoteSpan(number=int(number_str), start=start, end=out_len))
                i = close_idx + 1
                continue
            i += 1
            continue
        if ch == _FN_SEP or ch == _FN_CLOSE:
            i += 1
            continue
        out.append(ch)
        out_len += 1
        i += 1
    return "".join(out), tuple(footnotes)


def footnote_number_for_offset(footnotes: Sequence[FootnoteSpan], offset: int) -> Optional[int]:
    """Return the footnote number containing `offset` in ExtractedDocument.text, or None.

    Assumes `footnotes` is sorted by `start` ascending and non-overlapping,
    which _strip_footnote_markers guarantees (it emits spans in the order
    encountered scanning left-to-right).
    """
    if not footnotes:
        return None
    starts = [f.start for f in footnotes]
    idx = bisect_right(starts, offset) - 1
    if idx < 0:
        return None
    span = footnotes[idx]
    return span.number if span.start <= offset < span.end else None


def _load_footnotes_map(docx: DocxDocument) -> Dict[int, str]:
    """Parse word/footnotes.xml and return {footnote_id: text}."""
    mapping: Dict[int, str] = {}
    package_part = getattr(docx, "part", None)
    if package_part is None:
        return mapping
    package = getattr(package_part, "package", None)
    if package is None:
        return mapping
    footnotes_part = None
    for part in package.iter_parts():
        if str(part.partname) == "/word/footnotes.xml":
            footnotes_part = part
            break
    if footnotes_part is None:
        return mapping
    root = ET.fromstring(footnotes_part.blob)
    for fn in root.findall("w:footnote", _NS):
        fid = int(fn.get(f"{{{_W_NS}}}id", "-1"))
        if fid < 0:
            continue  # skip separators/continuation
        # Collect text paragraph-by-paragraph to preserve basic structure
        paras: List[str] = []
        for p in fn.findall(".//w:p", _NS):
            runs = [t.text or "" for t in p.findall(".//w:t", _NS)]
            txt = "".join(runs).strip()
            if txt:
                paras.append(txt)
        if not paras:
            # Fallback: any text nodes
            paras = [t.text or "" for t in fn.findall(".//w:t", _NS)]
        mapping[fid] = "\n".join([t for t in paras if t]).strip()
    return mapping

def _iter_table_paragraphs(tbl: Table) -> Iterable[Paragraph]:
    for row in tbl.rows:
        for cell in row.cells:
            yield from _iter_cell_paragraphs(cell)


def _iter_cell_paragraphs(cell: _Cell) -> Iterable[Paragraph]:
    for p in cell.paragraphs:
        yield p
    for t in cell.tables:
        yield from _iter_table_paragraphs(t)


def _iter_block_items(container: DocxDocument | _Cell) -> Iterable[Paragraph | Table]:
    if isinstance(container, DocxDocument):
        parent_elm = container.element.body  # type: ignore[union-attr]
    elif isinstance(container, _Cell):
        parent_elm = container._tc
    else:
        parent_elm = container._element  # type: ignore[attr-defined]
    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, container)  # type: ignore[arg-type]
        elif isinstance(child, CT_Tbl):
            yield Table(child, container)  # type: ignore[arg-type]


def _para_with_inline_footnotes(
    p: Paragraph, footnotes: Dict[int, str], numbering: Dict[int, int]
) -> str:
    """Inline footnote bodies at their reference markers.

    `numbering` maps a footnote's XML `w:id` (not the displayed number - ids
    can have gaps, e.g. separator/continuation ids are skipped in
    _load_footnotes_map) to its 1-based display number, assigned the first
    time that id is seen across the whole document body walk (see
    _extract_docx_with_footnotes). It is shared and mutated across every call
    for a given document so numbering is consistent and sequential.
    """
    parts: List[str] = []
    for run in p.runs:
        r = run._r
        refs = list(r.iter(qn("w:footnoteReference")))
        if refs:
            if run.text:
                parts.append(run.text)
            for ref in refs:
                fid = int(ref.get(qn("w:id")))
                ftxt = footnotes.get(fid, "").strip()
                display_number = numbering.setdefault(fid, len(numbering) + 1)
                if ftxt:
                    parts.append(f" {_wrap_footnote(display_number, ftxt)} ")
                else:
                    parts.append("  ")
            continue
        if run.text:
            parts.append(run.text)
    text = "".join(parts)
    # Remove stray spaces inserted before closing punctuation such as periods.
    text = re.sub(r"[ \t]+([.,;:?!])", r"\1", text)
    return text.strip()

def _normalize(text: str) -> str:
    """Normalize text by removing artifacts and standardizing formatting.

    Args:
        text: Raw extracted text.

    Returns:
        Normalized text with consistent line breaks and punctuation.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SMART_QUOTES_RE.sub('"', text)
    text = _SMART_APOSTROPHES_RE.sub("'", text)
    text = _HYPHEN_WRAP_RE.sub(r"\1\2", text)
    text = _EXCESS_BREAKS_RE.sub("\n\n", text)
    return text.strip()


def _normalize_superscripts(text: str) -> str:
    return text.translate(_SUPERSCRIPT_TRANSLATION)


def _primary_font_size(text_dict: Dict[str, Any]) -> float:
    sizes: Counter[float] = Counter()
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                raw_text = (span.get("text") or "").strip()
                if not raw_text:
                    continue
                size = float(span.get("size", 0.0) or 0.0)
                if size > 0:
                    sizes[round(size, 1)] += 1
    if not sizes:
        return 0.0
    return max(sizes.items(), key=lambda item: item[1])[0]


def _line_text_from_spans(spans: Iterable[Dict[str, Any]]) -> str:
    parts: List[str] = []
    prev_span: Optional[Dict[str, Any]] = None
    prev_text = ""
    for span in spans:
        text = str(span.get("text") or "")
        if _needs_space_between(prev_span, prev_text, span, text):
            parts.append(" ")
        parts.append(text)
        if text:
            prev_span = span
            prev_text = text
    return "".join(parts)


def _normalize_footnote_token(token: str) -> str:
    if not token:
        return token
    start = 0
    end = len(token)
    while start < end and not token[start].isalpha():
        start += 1
    while end > start and not token[end - 1].isalpha():
        end -= 1
    if start >= end:
        return token
    leading = token[:start]
    core = token[start:end]
    trailing = token[end:]
    if not core.isupper():
        return token
    if len(core) < 2:
        return token
    if not core.isalpha():
        return token
    if len(core) < 4 and not trailing.startswith('.'):
        return token
    converted = core[0] + core[1:].lower()
    return f"{leading}{converted}{trailing}"


def _normalize_footnote_case(text: str) -> str:
    if not text:
        return text
    parts = re.split(r"(\s+)", text)
    normalized_parts: List[str] = []
    for part in parts:
        if not part:
            continue
        if part.isspace():
            normalized_parts.append(part)
        else:
            normalized_parts.append(_normalize_footnote_token(part))
    return "".join(normalized_parts)


def _span_bbox(span: Dict[str, Any]) -> Sequence[float]:
    bbox = span.get("bbox")
    if isinstance(bbox, Sequence) and len(bbox) >= 4:
        return bbox
    return (0.0, 0.0, 0.0, 0.0)


def _needs_space_between(
    prev_span: Optional[Dict[str, Any]],
    prev_text: str,
    next_span: Optional[Dict[str, Any]],
    next_text: str,
) -> bool:
    """Detect a lost inter-word space between two adjacent PyMuPDF spans.

    PyMuPDF only synthesizes a space glyph when the horizontal gap between
    spans exceeds roughly 0.6 space-widths; tightly-tracked/kerned law-review
    PDFs routinely fall below that, leaving two words glued together
    ("Bill" + "Rights" -> "BillRights") with no whitespace character at all
    for any later normalization step to recover.
    """
    if not prev_span or not next_span or not prev_text or not next_text:
        return False
    if not prev_text[-1].isalnum() or not next_text[0].isalnum():
        return False
    if len(prev_text.strip()) == 1 and len(next_text.strip()) == 1:
        return False
    prev_bbox = _span_bbox(prev_span)
    next_bbox = _span_bbox(next_span)
    gap = float(next_bbox[0]) - float(prev_bbox[2])
    prev_size = float(prev_span.get("size", 0.0) or 0.0)
    next_size = float(next_span.get("size", 0.0) or 0.0)
    candidates = [size for size in (prev_size, next_size) if size > 0]
    font_size = min(candidates) if candidates else 10.0
    return gap >= 0.05 * font_size


def _needs_space_between_chars(
    prev_bbox: Optional[Sequence[float]],
    prev_char: str,
    next_bbox: Optional[Sequence[float]],
    next_char: str,
    font_size: float,
) -> bool:
    """Detect a lost inter-word space between two adjacent glyphs in a PDF.

    PyMuPDF's own text-extraction decides whether to synthesize a space
    character between glyphs *before* spans are ever exposed to Python -
    two words separated only by a tight coordinate gap (kerned/justified
    law-review text, no literal space glyph) can end up concatenated inside
    a single span's text with no whitespace character anywhere for later
    normalization to recover. This inspects the raw per-character bboxes
    (`page.get_text("rawdict")`) to catch that case directly.

    Unlike the span-level `_needs_space_between`, there is no reliable
    single-glyph guard against letterspaced/tracked text at this
    granularity - every comparison here is inherently glyph-to-glyph.
    """
    if not prev_bbox or not next_bbox or not prev_char or not next_char:
        return False
    if not prev_char.isalnum() or not next_char.isalnum():
        return False
    gap = float(next_bbox[0]) - float(prev_bbox[2])
    size = font_size if font_size > 0 else 10.0
    return gap >= 0.08 * size


def _reconstruct_span_text(span: Dict[str, Any]) -> str:
    """Rebuild a rawdict span's text from its characters, inserting spaces
    lost to PyMuPDF's own glyph-gap heuristic (see `_needs_space_between_chars`).
    """
    chars = span.get("chars")
    if not chars:
        return str(span.get("text") or "")
    font_size = float(span.get("size", 0.0) or 0.0)
    parts: List[str] = []
    prev_bbox: Optional[Sequence[float]] = None
    prev_char = ""
    for ch in chars:
        c = str(ch.get("c") or "")
        if not c:
            continue
        bbox = _span_bbox(ch)
        if _needs_space_between_chars(prev_bbox, prev_char, bbox, c, font_size):
            parts.append(" ")
        parts.append(c)
        prev_bbox = bbox
        prev_char = c
    return "".join(parts)


def _block_top(block: Dict[str, Any]) -> float:
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            bbox = _span_bbox(span)
            return float(bbox[1])
    return float("inf")


def _block_average_font_size(block: Dict[str, Any]) -> float:
    sizes: List[float] = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            size_val = float(span.get("size", 0.0) or 0.0)
            if size_val > 0:
                sizes.append(size_val)
    if not sizes:
        return 0.0
    return sum(sizes) / len(sizes)


def _is_footnote_block(
    block: Dict[str, Any],
    page_height: float,
    primary_font_size: float,
) -> bool:
    lines = block.get("lines", [])
    if not lines:
        return False
    normalized_lines = [
        _normalize_superscripts(_line_text_from_spans(line.get("spans", []))).strip()
        for line in lines
    ]
    footnote_starts = [line for line in normalized_lines if _FOOTNOTE_LINE_RE.match(line)]
    if not footnote_starts:
        return False
    block_top = _block_top(block)
    block_avg_size = _block_average_font_size(block)
    majority_threshold = max(1, len(lines) // 2)
    if page_height > 0 and block_top > page_height * 0.7:
        return True
    if primary_font_size > 0 and block_avg_size > 0:
        if block_avg_size <= primary_font_size * 0.85 and len(footnote_starts) >= majority_threshold:
            return True
    return False


def _parse_footnote_lines(lines: Iterable[str]) -> Dict[int, str]:
    footnotes: Dict[int, str] = {}
    current_number: Optional[int] = None
    buffer: List[str] = []

    def flush() -> None:
        nonlocal buffer
        nonlocal current_number
        if current_number is None:
            buffer = []
            return
        text = " ".join(part for part in buffer if part).strip()
        if text:
            footnotes[current_number] = _normalize_footnote_case(text)
        buffer = []

    for raw_line in lines:
        raw_stripped = raw_line.strip() if raw_line else ""
        normalized = _normalize_superscripts(raw_line)
        stripped = normalized.strip()
        if not stripped:
            if buffer:
                buffer.append("")
            continue
        match = _FOOTNOTE_START_RE.match(stripped)
        if not match:
            if current_number is not None:
                buffer.append(stripped)
            continue

        number_str = match.group(1)
        digits = number_str if number_str.isdigit() else _normalize_superscripts(number_str)
        try:
            parsed_number = int(digits)
        except (TypeError, ValueError):
            parsed_number = None

        has_punct = match.group(2) is not None
        superscript_start = bool(raw_stripped) and raw_stripped[0] in _SUPERSCRIPT_CHARACTERS
        # Many real documents number footnotes with a bare digit and no
        # trailing "." or ")" at all (confirmed against a real fixture), so
        # punctuation can't be required for the common case. Instead, treat
        # a line as a genuine new footnote only when its number is exactly
        # one more than the current footnote - footnote numbering is always
        # strictly sequential, whereas a wrapped citation fragment (e.g. a
        # volume number landing at the start of a line) essentially never
        # coincides with that exact value. A punctuated "1" is additionally
        # accepted as a numbering restart (e.g. a new article/section).
        is_new_footnote = (
            current_number is None
            or superscript_start
            or (parsed_number is not None and parsed_number == current_number + 1)
            or (has_punct and parsed_number == 1)
        )

        if is_new_footnote:
            flush()
            current_number = parsed_number
            remainder = match.group(3).strip()
            buffer = [remainder] if remainder else []
        else:
            buffer.append(stripped)

    if buffer:
        flush()

    return footnotes


def _ends_with_whitespace(parts: List[str]) -> bool:
    for part in reversed(parts):
        if not part:
            continue
        return part[-1].isspace()
    return False


def _render_span_with_inline_footnotes(
    span: Dict[str, Any],
    footnotes: Dict[int, str],
    primary_font_size: float,
    used: Set[int],
) -> str:
    text = span.get("text") or ""
    if not text:
        return ""
    normalized = _normalize_superscripts(text)
    matches = list(re.finditer(r"(?<!\d)(\d{1,3})(?!\d)", normalized))
    if not matches:
        return text
    font_size = float(span.get("size", 0.0) or 0.0)
    has_superscript = any(ch in _SUPERSCRIPT_CHARACTERS for ch in text)
    is_small = primary_font_size > 0 and font_size > 0 and font_size <= primary_font_size * 0.85
    if not has_superscript and not is_small:
        return text
    residual = re.sub(r"(?<!\d)\d{1,3}(?!\d)", "", normalized)
    if residual.strip():
        return text
    result: List[str] = []
    last_idx = 0
    for match in matches:
        result.append(text[last_idx:match.start()])
        num_val = int(normalized[match.start():match.end()])
        footnote_text = footnotes.get(num_val)
        if footnote_text:
            if not _ends_with_whitespace(result):
                result.append(" ")
            clean_text = footnote_text.strip()
            if clean_text:
                result.append(_wrap_footnote(num_val, clean_text))
                result.append(" ")
                used.add(num_val)
        else:
            result.append(text[match.start():match.end()])
        last_idx = match.end()
    result.append(text[last_idx:])
    return "".join(result)


def _render_line_with_inline_footnotes(
    line: Dict[str, Any],
    footnotes: Dict[int, str],
    primary_font_size: float,
    used: Set[int],
) -> str:
    spans = line.get("spans", [])
    joined: List[str] = []
    last_nonempty_span: Optional[Dict[str, Any]] = None
    last_nonempty_part = ""
    for span in spans:
        part = _render_span_with_inline_footnotes(span, footnotes, primary_font_size, used)
        if _needs_space_between(last_nonempty_span, last_nonempty_part, span, part):
            joined.append(" ")
        joined.append(part)
        if part:
            last_nonempty_span = span
            last_nonempty_part = part
    line_text = "".join(joined)
    line_text = re.sub(r" {2,}", " ", line_text)
    return line_text.rstrip()


def _extract_pdf_page_text(page: pymupdf.Page) -> str:
    try:
        # "rawdict" exposes per-character bboxes (unlike "dict", which only
        # gives pre-joined span text) - needed to detect inter-word spaces
        # PyMuPDF's own extraction silently dropped. See
        # _needs_space_between_chars for why.
        text_dict: Any = page.get_text("rawdict") # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        return page.get_text("text") # type: ignore[attr-defined]
    if not isinstance(text_dict, dict):
        return page.get_text("text") # type: ignore[attr-defined]

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span["text"] = _reconstruct_span_text(span)

    primary_font_size = _primary_font_size(text_dict)
    page_height = float(page.rect.height)

    main_blocks: List[List[Dict[str, Any]]] = []
    footnote_lines: List[str] = []

    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines = block.get("lines", [])
        if not lines:
            continue
        if _is_footnote_block(block, page_height, primary_font_size):
            for line in lines:
                footnote_lines.append(_line_text_from_spans(line.get("spans", [])))
        else:
            main_blocks.append(lines)

    footnotes = _parse_footnote_lines(footnote_lines)
    used_footnotes: Set[int] = set()

    if not main_blocks:
        if footnotes:
            logger.info(
                "Detected footnotes without main text on page %s", getattr(page, "number", 0) + 1
            )
        return page.get_text("text") # type: ignore[attr-defined]

    lines_out: List[str] = []
    for block_lines in main_blocks:
        block_texts: List[str] = []
        for line in block_lines:
            block_texts.append(
                _render_line_with_inline_footnotes(line, footnotes, primary_font_size, used_footnotes)
            )
        if block_texts:
            if lines_out and lines_out[-1] != "":
                lines_out.append("")
            lines_out.extend(block_texts)

    unused = [num for num in sorted(footnotes) if num not in used_footnotes and footnotes[num]]
    if unused:
        if lines_out:
            lines_out.append("")
        for num in unused:
            lines_out.append(_wrap_footnote(num, footnotes[num]))

    return "\n".join(lines_out)


def extract_pdf_text(file: FileStorage) -> ExtractedDocument:
    """Extract text from PDF file, inserting footnotes inline when present.

    Returns an ExtractedDocument: `text` is inline-footnote text with
    sentinel markers stripped, `footnotes` gives each footnote's number and
    character span within `text`. Footnotes recovered from pages that fell
    back to raw `page.get_text("text")` or OCR (no main-text blocks, or
    "rawdict" extraction failed) are not represented - those code paths never
    wrap a footnote body, matching pre-existing best-effort behavior for
    those pages.
    """

    file.stream.seek(0)
    page_texts: List[str] = []

    try:
        pdf_bytes = file.stream.read()
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                page_text = _extract_pdf_page_text(page)
                if not page_text.strip():
                    raw_text = page.get_text("text") # type: ignore[attr-defined]
                    if raw_text.strip():
                        page_text = raw_text
                    else:
                        pix = page.get_pixmap() # type: ignore[attr-defined]
                        img = Image.frombytes(
                            mode="RGB",
                            size=(pix.width, pix.height),
                            data=pix.samples,
                        )
                        page_text = pytesseract.image_to_string(img)
                page_texts.append(page_text.strip())
    except Exception as exc:  # pragma: no cover - pass through for callers
        raise ValueError(f"Failed to extract text from PDF: {exc}") from exc

    combined = "\n\n\f\n\n".join(page_texts).strip()
    normalized = _normalize(combined)
    text, footnotes = _strip_footnote_markers(normalized)
    return ExtractedDocument(text=text, footnotes=footnotes)

def _extract_docx_with_footnotes(doc: DocxDocument) -> str:
    """Return DOCX body text with footnotes inlined at their references.

    Each footnote body is wrapped in sentinel markers (see _wrap_footnote) so
    the caller can later recover (display_number, span) via
    _strip_footnote_markers. The display number is the 1-based order in which
    each footnote's `w:id` is first encountered walking the document body
    (paragraphs and nested table paragraphs, in document order) - this
    matches Word's own displayed numbering, which is positional rather than
    tied to the `w:id` values in word/footnotes.xml.

    Args:
        doc: An opened python-docx Document.

    Returns:
        A single string containing paragraph text with inline footnotes.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file does not have a .docx extension.
    """
    footnotes = _load_footnotes_map(doc)
    numbering: Dict[int, int] = {}

    lines: List[str] = []

    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            t = _para_with_inline_footnotes(block, footnotes, numbering)
            if t:
                lines.append(t)
        else:
            for par in _iter_table_paragraphs(block):
                t = _para_with_inline_footnotes(par, footnotes, numbering)
                if t:
                    lines.append(t)

    return "\n\n".join(lines)



def extract_docx_text(file: FileStorage) -> ExtractedDocument:
    """Extract text from DOCX file, including footnotes inline.

    Args:
        file: FileStorage object containing DOCX data.

    Returns:
        ExtractedDocument with normalized text (footnotes inline, sentinel
        markers stripped) and each footnote's number/span within that text.

    Raises:
        ValueError: If DOCX cannot be opened or processed.
    """
    file.stream.seek(0)

    try:
        doc = Document(file.stream)
    except Exception as exc:
        raise ValueError(f"Failed to open DOCX file: {exc}") from exc

    full_text = _extract_docx_with_footnotes(doc)
    normalized = _normalize(full_text)
    text, footnotes = _strip_footnote_markers(normalized)
    return ExtractedDocument(text=text, footnotes=footnotes)


def extract_document(file: FileStorage) -> ExtractedDocument:
    """Extract text and footnote provenance from an uploaded file.

    Supports PDF, DOCX, and TXT files. Includes footnote extraction for PDF
    and DOCX formats; TXT files never have footnotes.

    Args:
        file: FileStorage object containing the uploaded file.

    Returns:
        ExtractedDocument with normalized text and footnote spans.

    Raises:
        ValueError: If file format is unsupported or extraction fails.
    """
    filename = file.filename or ""
    file.stream.seek(0)
    _, ext = os.path.splitext(filename.lower())

    if ext == ".txt":
        try:
            raw_text = file.stream.read().decode("utf-8")
            return ExtractedDocument(text=_normalize(raw_text), footnotes=())
        except Exception as exc:
            raise ValueError(f"Failed to read TXT file: {exc}") from exc
    elif ext == ".pdf":
        return extract_pdf_text(file)
    elif ext == ".docx":
        return extract_docx_text(file)
    else:
        raise ValueError(
            f"Unsupported file format: {ext}. Supported formats: .pdf, .docx, .txt"
        )


def extract_text(file: FileStorage) -> str:
    """Extract text from uploaded file based on file extension.

    Thin compatibility wrapper around extract_document() for callers that
    only need the flat text and not footnote provenance.

    Args:
        file: FileStorage object containing the uploaded file.

    Returns:
        Extracted and normalized text content.

    Raises:
        ValueError: If file format is unsupported or extraction fails.
    """
    return extract_document(file).text
