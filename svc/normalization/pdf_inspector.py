# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""PDF inspection (plan section 8.1-8.2): preflight, per-page metrics, page text and classification.

pypdf answers the questions asked of the file as a whole (encrypted? how many pages? digitally signed?).
pdfplumber measures each page; the components of every decision (character and word counts, character
quality, token shape, image coverage, a low-resolution "ink" render for pages with no text) are stored on
PageMetrics so the thresholds in PdfThresholds can be tuned from real files instead of guessed.

A page is one of NATIVE_TEXT_GOOD, NATIVE_TEXT_SUSPECT, IMAGE_ONLY, EXISTING_OCR_SUSPECT or
EMPTY_OR_DECORATIVE. A low character count is never a reason on its own: covers, separators and pages
holding only a short footnote legitimately carry little text.

Signals implemented: a page image with no meaningful text; visible marks with no text (vector-outlined
type); damaged characters (U+FFFD, control and private-use characters, "(cid:N)" tokens); text made of
isolated single letters or of vowel-less strings; unusable word coordinates; a full-page image with very
few words. Not implemented (they need a rendering-versus-text comparison): implausible reading order or
spacing, and footnote text missing from the bottom of a page that visibly has footnotes.

The page text is what a citation returned for that page is validated against, so it is assembled in reading
order: two-column pages are read column by column, with full-width lines (titles, footnote bands) kept
whole, so that a citation wrapping inside a column is contiguous.
"""

from __future__ import annotations

import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from svc.normalization.config import NormalizationConfig, PdfThresholds
from svc.normalization.models import (
    EMPTY_OR_DECORATIVE,
    EXISTING_OCR_SUSPECT,
    IMAGE_ONLY,
    NATIVE_TEXT_GOOD,
    NATIVE_TEXT_SUSPECT,
    PageMetrics,
    UnsupportedDocumentError,
)
from utils.logger import get_logger

logger = get_logger()

_VOWELS = set("aeiouyAEIOUY")
_CID_RE = re.compile(r"\(cid:\d+\)")
# One-letter words that are ordinary in legal text: "a", "I", "v." in a case name, Roman numerals.
_SINGLE_LETTER_WORDS = {"a", "A", "I", "v", "V", "X"}
_EDGE_PUNCTUATION = ".,;:()[]{}\"'“”‘’*†‡"


@dataclass
class PdfPreflight:
    page_count: int
    owner_only_encrypted: bool  # encrypted, but opens with an empty password
    signed: bool


@dataclass
class PageInspection:
    metrics: PageMetrics
    text: str


# --- preflight (pypdf) -----------------------------------------------------------------------


def _sig_in_fields(fields: Any, budget: List[int], depth: int = 0) -> bool:
    if depth > 8 or budget[0] <= 0:
        return False
    for ref in fields or []:
        budget[0] -= 1
        try:
            field = ref.get_object()
            if field.get("/FT") == "/Sig" and field.get("/V") is not None:
                return True
            if _sig_in_fields(field.get("/Kids"), budget, depth + 1):
                return True
        except Exception:
            continue
    return False


def _is_signed(reader: Any) -> bool:
    try:
        root = reader.trailer["/Root"]
        acro = root.get("/AcroForm")
        if acro is not None and _sig_in_fields(acro.get_object().get("/Fields"), [10_000]):
            return True
        perms = root.get("/Perms")
        return perms is not None and any(k in perms.get_object() for k in ("/DocMDP", "/UR3"))
    except Exception:
        return False


def preflight(path: str, config: NormalizationConfig) -> PdfPreflight:
    """Reject a PDF that cannot be processed; report the facts later stages need."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(path, strict=False)
        owner_only = False
        if reader.is_encrypted:
            if not reader.decrypt(""):
                raise UnsupportedDocumentError(
                    "pdf_encrypted", "Password-protected PDFs are not supported. Please upload an unprotected copy.")
            owner_only = True
        page_count = len(reader.pages)
        signed = _is_signed(reader)
    except UnsupportedDocumentError:
        raise
    except Exception as exc:
        raise UnsupportedDocumentError("pdf_unreadable", "This PDF could not be read; it may be damaged.") from exc
    if page_count == 0:
        raise UnsupportedDocumentError("pdf_empty", "This PDF has no pages.")
    if page_count > config.max_pdf_pages:
        raise UnsupportedDocumentError(
            "pdf_too_many_pages", f"This PDF has {page_count} pages; the limit is {config.max_pdf_pages}.")
    return PdfPreflight(page_count, owner_only, signed)


def decrypt_copy(path: str, dest: str) -> None:
    """A copy of an owner-password-only PDF with the restrictions removed (its user password is empty)."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(path, strict=False)
    reader.decrypt("")
    writer = PdfWriter(clone_from=reader)
    with open(dest, "wb") as handle:
        writer.write(handle)


# --- reading order ---------------------------------------------------------------------------


def _lines(words: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group words into lines by vertical overlap (a raised footnote marker stays with its line)."""
    lines: List[List[Dict[str, Any]]] = []
    bounds: List[Tuple[float, float]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        centre = (word["top"] + word["bottom"]) / 2
        if lines and bounds[-1][0] - 2 <= centre <= bounds[-1][1] + 2:
            lines[-1].append(word)
            bounds[-1] = (min(bounds[-1][0], word["top"]), max(bounds[-1][1], word["bottom"]))
        else:
            lines.append([word])
            bounds.append((word["top"], word["bottom"]))
    return lines


def _join(lines: Sequence[Sequence[Dict[str, Any]]]) -> str:
    return "\n".join(" ".join(w["text"] for w in sorted(line, key=lambda w: w["x0"])) for line in lines)


_MIN_GUTTER_GAP = 12.0  # points: the widest gap of a two-column line is at least this
_MIN_GUTTER_WIDTH = 8.0  # points: the strip every column line leaves empty


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _find_gutter(words: Sequence[Dict[str, Any]], lines: Sequence[Any], width: float) -> Tuple[float, float] | None:
    """(x0, x1) of the strip that every two-column line leaves empty, or None for a one-column page.

    Each line with words on both sides of the middle contributes the gap between them: from the last word
    of the left column to the first of the right. One edge of those gaps is aligned (usually the right
    column's left edge) and the other ragged. Lines whose aligned edge is off (a full-width line that happens
    to have a wide word gap) are ignored; the gutter runs from the ragged edge's furthest reach to the aligned
    edge, so no genuine column line touches it.
    """
    if len(words) < 60 or len(lines) < 8:
        return None
    pairs: List[Tuple[float, float]] = []
    for line in lines:
        ordered = sorted(line, key=lambda w: w["x0"])
        best: Tuple[float, float, float] | None = None
        for a, b in zip(ordered, ordered[1:]):
            gap = b["x0"] - a["x1"]
            if gap >= _MIN_GUTTER_GAP and 0.30 * width <= (a["x1"] + b["x0"]) / 2 <= 0.70 * width and (best is None or gap > best[0]):
                best = (gap, a["x1"], b["x0"])
        if best is not None:
            pairs.append((best[1], best[2]))
    if len(pairs) < max(5, 0.30 * len(lines)):
        return None
    lefts, rights = [p[0] for p in pairs], [p[1] for p in pairs]
    spread_left = _median([abs(x - _median(lefts)) for x in lefts])
    spread_right = _median([abs(x - _median(rights)) for x in rights])
    anchor_right = spread_right <= spread_left
    anchor = _median(rights if anchor_right else lefts)
    inliers = [p for p in pairs if abs((p[1] if anchor_right else p[0]) - anchor) <= 6]
    if len(inliers) < max(5, 0.30 * len(lines)):
        return None
    gutter = (max(p[0] for p in inliers), min(p[1] for p in inliers))
    if gutter[1] - gutter[0] < _MIN_GUTTER_WIDTH:
        return None
    centre = (gutter[0] + gutter[1]) / 2
    left = sum(1 for w in words if (w["x0"] + w["x1"]) / 2 < centre)
    if min(left, len(words) - left) < 0.2 * len(words):
        return None
    return gutter


def _crosses(line: Sequence[Dict[str, Any]], gutter: Tuple[float, float]) -> bool:
    """A full-width line: text runs across the gutter (a one-sided column line that merely reaches into it doesn't)."""
    centre = (gutter[0] + gutter[1]) / 2
    if not any(w["x0"] < gutter[1] and w["x1"] > gutter[0] for w in line):
        return False
    has_left = any((w["x0"] + w["x1"]) / 2 < centre for w in line)
    has_right = any((w["x0"] + w["x1"]) / 2 >= centre for w in line)
    return (has_left and has_right) or any(w["x0"] < centre < w["x1"] for w in line)


def _line_height(line: Sequence[Dict[str, Any]]) -> float:
    return _median([w["bottom"] - w["top"] for w in line])


def _notes_start(lines: Sequence[Sequence[Dict[str, Any]]], height: float) -> int:
    """Index of the first line of a bottom region set in clearly smaller type (footnotes), else len(lines).

    Read separately, after the body: a short footnote line under the left column would otherwise be placed
    between the two columns and split a body citation that runs from one into the other.
    """
    heights = [_line_height(line) for line in lines]
    body = _median([w["bottom"] - w["top"] for line in lines for w in line])
    k = len(lines)
    while k > 0 and heights[k - 1] <= 0.88 * body and min(w["top"] for w in lines[k - 1]) >= 0.4 * height:
        k -= 1
    return k


def page_text(words: Sequence[Dict[str, Any]], width: float, height: float | None = None) -> Tuple[str, int]:
    """(text in reading order, column count) for one page's words."""
    if not words:
        return "", 1
    lines = _lines(words)
    split = _notes_start(lines, height) if height else len(lines)
    if 0 < split < len(lines):
        parts = [_region_text(lines[:split], width), _region_text(lines[split:], width)]
        return "\n".join(text for text, _ in parts if text), max(cols for _, cols in parts)
    return _region_text(lines, width)


def _region_text(lines: Sequence[Sequence[Dict[str, Any]]], width: float) -> Tuple[str, int]:
    """One region's text: column by column when it has two columns, else line by line."""
    words = [w for line in lines for w in line]
    gutter = _find_gutter(words, lines, width)
    if gutter is None:
        return _join(lines), 1
    centre = (gutter[0] + gutter[1]) / 2
    pieces: List[str] = []
    run: List[List[Dict[str, Any]]] = []
    run_is_columns: bool | None = None
    used_columns = False

    def flush() -> None:
        nonlocal used_columns
        if not run:
            return
        if run_is_columns:
            flat = [w for line in run for w in line]
            left = [w for w in flat if (w["x0"] + w["x1"]) / 2 < centre]
            right = [w for w in flat if (w["x0"] + w["x1"]) / 2 >= centre]
            for column in (left, right):
                if column:
                    pieces.append(_join(_lines(column)))
            used_columns = used_columns or bool(left and right)
        else:
            pieces.append(_join(run))
        run.clear()

    for line in lines:
        line_is_columns = not _crosses(line, gutter)
        if run_is_columns is not None and line_is_columns != run_is_columns:
            flush()
        run_is_columns = line_is_columns
        run.append(line)
    flush()
    return "\n".join(pieces), 2 if used_columns else 1


# --- metrics and classification -----------------------------------------------------------------


def _text_quality(text: str) -> Tuple[float, float, float, float]:
    """(printable ratio, bad-character ratio, single-letter token ratio, vowel-less token ratio)."""
    length = max(len(text), 1)
    bad = 0
    printable = 0
    for ch in text:
        category = ord(ch)
        if ch in "\n\t\r" or ch.isprintable():
            printable += 1
        if ch == "�" or (category < 32 and ch not in "\n\t\r") or 0xE000 <= category <= 0xF8FF:
            bad += 1
    bad += 5 * len(_CID_RE.findall(text))
    # Whole whitespace-delimited tokens, so "U.S." or "C.F.R." never look like single letters.
    tokens = [t for t in (raw.strip(_EDGE_PUNCTUATION) for raw in text.split()) if t.isalpha()]
    single = sum(1 for t in tokens if len(t) == 1 and t not in _SINGLE_LETTER_WORDS)
    long_tokens = [t for t in tokens if len(t) >= 4 and t.isascii()]
    vowelless = sum(1 for t in long_tokens if not (set(t) & _VOWELS))
    return (
        printable / length,
        min(bad / length, 1.0),
        single / max(len(tokens), 1),
        vowelless / max(len(long_tokens), 1),
    )


def classify_page(m: PageMetrics, t: PdfThresholds) -> Tuple[str, List[str]]:
    reasons: List[str] = []
    big_image = m.image_coverage >= t.image_page_coverage
    full_image = m.image_coverage >= t.full_image_coverage
    if m.word_count == 0:
        if big_image:
            return IMAGE_ONLY, ["large_image_no_text"]
        if m.ink_ratio is not None and m.ink_ratio >= t.ink_visible_ratio:
            return IMAGE_ONLY, ["visible_marks_no_text"]
        return EMPTY_OR_DECORATIVE, ["no_text"]
    if m.word_count < t.min_words_for_text:
        if full_image:
            return EXISTING_OCR_SUSPECT, ["full_page_image_few_words"]
        return NATIVE_TEXT_GOOD, ["short_text"]
    if m.bad_char_ratio > t.max_bad_char_ratio:
        reasons.append("damaged_characters")
    if m.word_count >= t.min_tokens_for_ratios:
        if m.single_char_token_ratio > t.max_single_char_token_ratio:
            reasons.append("isolated_single_letters")
        if m.vowelless_token_ratio > t.max_vowelless_token_ratio:
            reasons.append("vowelless_tokens")
    if m.words_with_boxes_ratio < t.min_words_with_boxes_ratio:
        reasons.append("unusable_word_boxes")
    if full_image and m.word_count < t.full_image_min_words:
        reasons.append("full_page_image_few_words")
    if reasons:
        return (EXISTING_OCR_SUSPECT if big_image else NATIVE_TEXT_SUSPECT), reasons
    return NATIVE_TEXT_GOOD, ["clean_text"]


def _ink_ratio(pdfium_page: Any) -> float | None:
    try:
        bitmap = pdfium_page.render(scale=0.4)
        histogram = bitmap.to_pil().convert("L").histogram()
        total = sum(histogram)
        return sum(histogram[:128]) / total if total else None
    except Exception:
        return None


_SKEW_MIN = 0.004  # radians (~0.23 degrees): below this a page is treated as straight
_SKEW_MAX = 0.18  # radians (~10 degrees): beyond this the text is rotated on purpose, not skewed


def _page_words(page: Any) -> Tuple[List[Dict[str, Any]], float, float]:
    """(words, skew in radians, margin in points) for a page.

    A scan's OCR layer follows the scan's tilt: every character carries the same small rotation, the baselines
    slope, and a long line drifts past the vertical tolerance pdfplumber groups characters into lines by, so
    words and lines come out shredded. The median character angle is measured from the text matrices and the
    characters are sheared back (top and bottom shifted by tan(angle) x their horizontal position) before words
    are formed. Straight pages take pdfplumber's own path unchanged.
    """
    import math

    from pdfplumber.utils.text import extract_words

    chars = page.chars
    angles = sorted(math.atan2(c["matrix"][1], c["matrix"][0]) for c in chars if c.get("matrix"))
    theta = angles[len(angles) // 2] if angles else 0.0
    if not _SKEW_MIN < abs(theta) < _SKEW_MAX:
        return page.extract_words(x_tolerance=3, y_tolerance=3, keep_blank_chars=False, use_text_flow=False), 0.0, 5.0
    slope = math.tan(theta)
    sheared = []
    for c in chars:
        shift = slope * (c["x0"] + c["x1"]) / 2
        sheared.append({**c, "top": c["top"] + shift, "bottom": c["bottom"] + shift})
    words = extract_words(sheared, x_tolerance=3, y_tolerance=3, keep_blank_chars=False, use_text_flow=False)
    return words, theta, 5.0 + abs(slope) * float(page.width)


def inspect_pages(path: str, pages: Sequence[int], thresholds: PdfThresholds) -> List[PageInspection]:
    """Metrics, reading-order text and classification for the given 1-based pages of `path`."""
    import math

    import pdfplumber
    import pypdfium2

    out: List[PageInspection] = []
    pdfium = None
    with pdfplumber.open(path) as pdf:
        for number in pages:
            page = pdf.pages[number - 1]
            width, height = float(page.width), float(page.height)
            words, skew, margin = _page_words(page)
            text, columns = page_text(words, width, height)
            printable, bad, single, vowelless = _text_quality(text)
            area = max(width * height, 1.0)
            covered = 0.0
            for image in page.images:
                w = max(0.0, min(image["x1"], width) - max(image["x0"], 0.0))
                h = max(0.0, min(image["bottom"], height) - max(image["top"], 0.0))
                covered += w * h
            with_boxes = sum(
                1 for w in words
                if w["x1"] > w["x0"] and w["bottom"] > w["top"] and -margin <= w["x0"] and w["x1"] <= width + margin
                and -margin <= w["top"] and w["bottom"] <= height + margin
            )
            ink = None
            if len(words) <= thresholds.ink_test_max_words:
                if pdfium is None:
                    pdfium = pypdfium2.PdfDocument(path)
                ink = _ink_ratio(pdfium[number - 1])
            metrics = PageMetrics(
                page=number, classification="", reasons=[], char_count=len(page.chars),
                alpha_count=sum(1 for ch in text if ch.isalpha()), word_count=len(words),
                printable_ratio=round(printable, 4), bad_char_ratio=round(bad, 4),
                single_char_token_ratio=round(single, 4), vowelless_token_ratio=round(vowelless, 4),
                words_with_boxes_ratio=round(with_boxes / len(words), 4) if words else 1.0,
                image_count=len(page.images), image_coverage=round(min(covered / area, 1.0), 4),
                ink_ratio=None if ink is None else round(ink, 4), columns=columns,
                skew_degrees=round(math.degrees(skew), 2),
            )
            metrics.classification, metrics.reasons = classify_page(metrics, thresholds)
            out.append(PageInspection(metrics, text))
            page.flush_cache()
    if pdfium is not None:
        pdfium.close()
    return out


class PdfInspector:
    """The plan's PdfInspector: what to ask of a PDF before deciding what to do with it."""

    def __init__(self, config: NormalizationConfig) -> None:
        self.config = config

    def preflight(self, path: str) -> PdfPreflight:
        """Reject an unreadable, encrypted, empty or oversized PDF; report its page count and signature status."""
        return preflight(path, self.config)

    def inspect(self, path: str, page_count: int) -> List[PageInspection]:
        """Metrics, text and classification for every page."""
        return inspect_document(path, page_count, self.config)

    def inspect_pages(self, path: str, pages: Sequence[int]) -> List[PageInspection]:
        """The same for chosen 1-based pages (used to re-read pages after OCR)."""
        return inspect_pages(path, pages, self.config.pdf)


def inspect_document(path: str, page_count: int, config: NormalizationConfig) -> List[PageInspection]:
    """Inspect every page; large documents are split across worker processes (pdfminer is GIL-bound)."""
    numbers = list(range(1, page_count + 1))
    workers = config.inspect_workers
    if workers <= 1 or page_count < config.parallel_min_pages:
        return inspect_pages(path, numbers, config.pdf)
    size = -(-page_count // workers)
    ranges = [numbers[i:i + size] for i in range(0, page_count, size)]
    import multiprocessing

    with ProcessPoolExecutor(max_workers=len(ranges), mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(inspect_pages, path, chunk, config.pdf) for chunk in ranges]
        results = [f.result(timeout=config.total_timeout_s) for f in futures]
    return [inspection for chunk in results for inspection in chunk]
