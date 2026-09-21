# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

import os
import re
import shutil
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
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
_FOOTNOTE_START_RE: Final = re.compile(r"^\s*([\d⁰¹²³⁴⁵⁶⁷⁸⁹]+)([\.\)])?\s*(.*)")

# PDF footnote region / marker detection (see _footnote_region and
# _span_marker_numbers). A footnote begins with its number followed by text
# ("12. Text", "12 Text"); a lone digit line - e.g. a raised marker that
# PyMuPDF put on its own line - is not a footnote start.
_FOOTNOTE_REGION_START_RE: Final = re.compile(r"^([\d⁰¹²³⁴⁵⁶⁷⁸⁹]{1,3})[\.\)]?\s+\S")
# Footnote-region blocks are set in type at most this fraction of the body size
# (law reviews run ~0.82-0.95; Word footnotes ~0.8).
_FOOTNOTE_REGION_MAX_SIZE_RATIO: Final = 0.96
# Blocks starting in the top/bottom band of the page are running headers and
# footers (page numbers, repository banners) - never footnotes.
_PAGE_HEADER_BAND: Final = 0.07
_PAGE_FOOTER_BAND: Final = 0.94
# A footnote reference mark is superscripted or set at most this fraction of
# the body size.
_MARKER_MAX_SIZE_RATIO: Final = 0.85
_MARKER_DIGITS_RE: Final = re.compile(r"\d{1,3}(?:,\s*\d{1,3})*")
# OCR'd text layers often glue a marker's leading digit(s) onto the preceding
# word ("respectively).1" + small "4" for marker 14).
_OCR_MARKER_PREFIX_RE: Final = re.compile(r"(?<!\d)(\d{1,2})$")

# Private-use-area sentinels used to wrap an inlined footnote/endnote body
# between the moment it's spliced into the running text and
# _strip_note_markers(), which runs after _normalize() and converts the
# wrapped bodies back into plain text plus a NoteSpan(kind, ordinal, label,
# start, end) per body. Chosen from the Unicode Private Use Area
# (U+E000-U+F8FF, general category "Co") specifically because \w, \s and
# every regex used in this module (_HYPHEN_WRAP_RE, _EXCESS_BREAKS_RE, the
# smart-quote/apostrophe subs, _needs_space_between's isalnum() check) do not
# match "Co" characters, so normalization sees these markers as inert
# punctuation and never corrupts them. _FN_FIELD is a fourth sentinel that
# separates the machine-readable "<kindcode><ordinal>" header from the
# free-text printed `label` (e.g. "5", "iv", "†") within the marker - both
# `label` and `body` are scrubbed of all four sentinel characters before
# wrapping (see _scrub_sentinels) so a stray sentinel already present in
# document content can never desynchronize the parser.
_FN_OPEN: Final = ""
_FN_SEP: Final = ""
_FN_CLOSE: Final = ""
_FN_FIELD: Final = ""
_FN_SENTINEL_RE: Final = re.compile("[\ue000-\ue003]")

_KIND_CODES: Final = {"footnote": "f", "endnote": "e"}
_KIND_BY_CODE: Final = {v: k for k, v in _KIND_CODES.items()}

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS = {"w": _W_NS}


@dataclass(frozen=True, kw_only=True)
class NoteSpan:
    """A footnote's or endnote's inlined body location within ExtractedDocument.text.

    `kind` is "footnote" or "endnote". `label` is the mark the source document
    itself prints ("5", "iv", "B", "†") and may be the empty string when the
    document's numFmt is "none" or for a custom mark this module can't decode
    - consumers must fall back to displaying `ordinal` in that case. `label`
    is deliberately NOT guaranteed unique: a document using numRestart or a
    cycling numFmt (upperLetter/lowerLetter/chicago) repeats labels, so `label`
    must never be used as a key or sort field. `ordinal` is the 1-based order
    of the reference within its own `kind`, counted across the whole document
    - it is the stable unique identity and the correct sort key. `start`/`end`
    index into the fully normalized, sentinel-stripped text (i.e. they are
    valid offsets into ExtractedDocument.text), not into any intermediate
    representation.
    """

    kind: str
    ordinal: int
    label: str
    start: int
    end: int


# Transitional alias - remove once nothing outside this module imports the
# old name.
FootnoteSpan = NoteSpan


@dataclass(frozen=True)
class ExtractedDocument:
    """Result of extracting a document: flat text plus footnote provenance."""

    text: str
    footnotes: Tuple[FootnoteSpan, ...]
    ocr_skipped_pages: Tuple[int, ...] = ()
    """1-based PDF page numbers that had no extractable text, contained an
    image, and were not OCR'd because Tesseract is unavailable on this host.
    Always empty for DOCX/TXT and for any PDF page that had text, had no
    image, or was successfully OCR'd."""


class _OcrUnavailable(Exception):
    """Raised internally when OCR is attempted but Tesseract can't run.

    Distinguishes "no OCR available" (caller should degrade gracefully) from a
    genuine extraction failure (caller should propagate as ValueError).
    """


def ocr_available() -> bool:
    """Return True if the Tesseract binary pytesseract would invoke exists.

    Checks `shutil.which` against `pytesseract.pytesseract.tesseract_cmd`
    (default `"tesseract"`), so a custom `tesseract_cmd` override is honored.
    Not cached: reflects the current process environment on every call.
    """
    return shutil.which(pytesseract.pytesseract.tesseract_cmd) is not None


def _ocr_page(page: "pymupdf.Page") -> str:
    """Render a PDF page to an image and OCR it with Tesseract.

    Raises `_OcrUnavailable` if Tesseract isn't installed/on PATH (wraps
    `pytesseract.TesseractNotFoundError`, a subclass of `OSError`). Any other
    exception propagates to the caller unchanged.
    """
    try:
        pix = page.get_pixmap()  # type: ignore[attr-defined]
        img = Image.frombytes(
            mode="RGB",
            size=(pix.width, pix.height),
            data=pix.samples,
        )
        return pytesseract.image_to_string(img)
    except pytesseract.TesseractNotFoundError as exc:
        raise _OcrUnavailable(str(exc)) from exc


def _scrub_sentinels(s: str) -> str:
    """Strip PUA sentinels from a value being embedded in a wrapped marker.

    Applied to both `label` and `body` at wrap time so that, between a
    matched _FN_OPEN and _FN_CLOSE, there is guaranteed to be exactly one
    _FN_FIELD and one _FN_SEP and no other sentinel character - the parser in
    _strip_note_markers relies on this to never have its forward find() calls
    truncated by a sentinel that leaked in from document content. Dropping a
    stray sentinel character is already this module's behavior for any
    unwrapped text (see _strip_note_markers' malformed-marker handling), so
    this introduces no new class of data loss.
    """
    return _FN_SENTINEL_RE.sub("", s)


def _wrap_note(kind: str, ordinal: int, label: str, body: str) -> str:
    """Wrap a footnote/endnote body in sentinel markers for later recovery.

    Wire format: OPEN + kindcode + decimal-ordinal + FIELD + label + SEP +
    body + CLOSE, e.g. "<OPEN>f12<FIELD>xii<SEP>body text<CLOSE>". `label` is
    whitespace-collapsed as well as sentinel-scrubbed so the header can never
    contain a newline or a ". "-like sequence that would let _normalize()'s
    _HYPHEN_WRAP_RE/_EXCESS_BREAKS_RE, or _para_with_inline_notes' stray-
    space-before-punctuation cleanup, reach into it.

    Must only be called with an already-stripped, non-empty `body` - an empty
    body would produce a marker with nothing between _FN_SEP and _FN_CLOSE,
    which _strip_note_markers still handles correctly (a zero-length
    NoteSpan) but which is never useful to a caller.
    """
    clean_label = " ".join(_scrub_sentinels(label).split())
    clean_body = _scrub_sentinels(body)
    return f"{_FN_OPEN}{_KIND_CODES[kind]}{ordinal}{_FN_FIELD}{clean_label}{_FN_SEP}{clean_body}{_FN_CLOSE}"


def _parse_note_header(head: str) -> Optional[Tuple[str, int]]:
    """Parse a wrapped marker's "<kindcode><decimal ordinal>" header.

    Returns (kind, ordinal), or None if `head` isn't a recognized kind code
    immediately followed by a positive integer.
    """
    if len(head) < 2:
        return None
    kind = _KIND_BY_CODE.get(head[0])
    if kind is None or not head[1:].isdigit():
        return None
    ordinal = int(head[1:])
    return (kind, ordinal) if ordinal >= 1 else None


def _strip_note_markers(text: str) -> Tuple[str, Tuple[NoteSpan, ...]]:
    """Remove _wrap_note() markers, returning plain text plus their spans.

    Single left-to-right pass so offsets are always consistent with the
    output text (no double-pass drift). A malformed marker (an _FN_OPEN with
    no matching _FN_FIELD/_FN_SEP/_FN_CLOSE, an unrecognized header, or a
    stray _FN_SEP/_FN_CLOSE/_FN_FIELD with no opener - not expected from this
    module's own writers, but guarded against defensively) has its sentinel
    character(s) dropped without emitting a NoteSpan, rather than corrupting
    the surrounding text.
    """
    notes: List[NoteSpan] = []
    out: List[str] = []
    out_len = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == _FN_OPEN:
            close_idx = text.find(_FN_CLOSE, i + 1)
            limit = close_idx if close_idx != -1 else n
            sep_idx = text.find(_FN_SEP, i + 1, limit)
            field_idx = text.find(_FN_FIELD, i + 1, sep_idx if sep_idx != -1 else limit)
            parsed = (
                _parse_note_header(text[i + 1:field_idx])
                if close_idx != -1 and sep_idx != -1 and field_idx != -1
                else None
            )
            if parsed is not None:
                kind, ordinal = parsed
                label = text[field_idx + 1:sep_idx]
                body = text[sep_idx + 1:close_idx]
                start = out_len
                out.append(body)
                out_len += len(body)
                notes.append(NoteSpan(kind=kind, ordinal=ordinal, label=label, start=start, end=out_len))
                i = close_idx + 1
                continue
            i += 1
            continue
        if ch in (_FN_SEP, _FN_CLOSE, _FN_FIELD):
            i += 1
            continue
        out.append(ch)
        out_len += 1
        i += 1
    return "".join(out), tuple(notes)


def note_for_offset(notes: Sequence[NoteSpan], offset: int) -> Optional[NoteSpan]:
    """Return the NoteSpan containing `offset` in ExtractedDocument.text, or None.

    Assumes `notes` is sorted by `start` ascending and non-overlapping, which
    _strip_note_markers guarantees (it emits spans in the order encountered
    scanning left-to-right) - footnote and endnote spans are interleaved in
    that single sequence, sorted purely by document position. Grouping
    endnotes after footnotes for display is a presentation-layer sort done by
    each consumer, not an extraction-order guarantee made here.
    """
    if not notes:
        return None
    starts = [f.start for f in notes]
    idx = bisect_right(starts, offset) - 1
    if idx < 0:
        return None
    span = notes[idx]
    return span if span.start <= offset < span.end else None


def _find_package_part(docx: DocxDocument, partname: str) -> Optional[Any]:
    """Locate an OPC part by its package-relative name (e.g. "/word/settings.xml")."""
    package_part = getattr(docx, "part", None)
    if package_part is None:
        return None
    package = getattr(package_part, "package", None)
    if package is None:
        return None
    for part in package.iter_parts():
        if str(part.partname) == partname:
            return part
    return None


_SKIP_NOTE_TYPES: Final = frozenset({"separator", "continuationSeparator", "continuationNotice"})


def _load_notes_map(docx: DocxDocument, partname: str, tag: str) -> Dict[int, str]:
    """Parse word/footnotes.xml or word/endnotes.xml and return {id: text}.

    `tag` is "w:footnote" or "w:endnote". A note is skipped when its `w:type`
    is separator/continuationSeparator/continuationNotice - verified against
    this repo's fixtures, both `footnotes.xml` and `endnotes.xml` carry this
    attribute explicitly on their id -1/0 entries - or, belt-and-braces, when
    its `w:id` is negative, for a producer that omits `w:type`. Checking
    `w:type` (rather than only `fid < 0`, the previous behavior) also closes
    a real gap: `w:id="0"` (continuationSeparator) is not negative, so the
    old `fid < 0` check alone let it through; it was inert only because its
    body happens to be empty.
    """
    mapping: Dict[int, str] = {}
    part = _find_package_part(docx, partname)
    if part is None:
        return mapping
    root = ET.fromstring(part.blob)
    for note in root.findall(tag, _NS):
        fid = int(note.get(f"{{{_W_NS}}}id", "-1"))
        note_type = note.get(f"{{{_W_NS}}}type")
        if fid < 0 or note_type in _SKIP_NOTE_TYPES:
            continue
        # Collect text paragraph-by-paragraph to preserve basic structure
        paras: List[str] = []
        for p in note.findall(".//w:p", _NS):
            runs = [t.text or "" for t in p.findall(".//w:t", _NS)]
            txt = "".join(runs).strip()
            if txt:
                paras.append(txt)
        if not paras:
            # Fallback: any text nodes
            paras = [t.text or "" for t in note.findall(".//w:t", _NS)]
        mapping[fid] = "\n".join([t for t in paras if t]).strip()
    return mapping


_DEFAULT_FOOTNOTE_FMT: Final = "decimal"
# Word's own default endnote numFmt is lowerRoman (i, ii, iii...); ECMA-376
# states the standard default is "decimal", but since the documents this
# module processes are Word files, matching what Word itself displays is
# what "honoring the document's numbering" means here.
_DEFAULT_ENDNOTE_FMT: Final = "lowerRoman"
_DEFAULT_NUM_START: Final = 1
_DEFAULT_NUM_RESTART: Final = "continuous"
_CHICAGO_SYMBOLS: Final = ("*", "†", "‡", "§")
_ROMAN_VALUES: Final = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)


@dataclass(frozen=True)
class _NoteProps:
    """Resolved w:footnotePr/w:endnotePr numbering properties for one scope

    (either the document-wide default from word/settings.xml, or a specific
    section's override).
    """

    num_fmt: str
    num_start: int
    num_restart: str  # "continuous" | "eachSect" | "eachPage"


def _to_roman(value: int) -> str:
    result: List[str] = []
    remaining = value
    for amount, numeral in _ROMAN_VALUES:
        count, remaining = divmod(remaining, amount)
        result.append(numeral * count)
    return "".join(result)


def _format_note_label(value: int, fmt: str) -> str:
    """Render an automatic footnote/endnote number in an OOXML ST_NumberFormat.

    Unknown or unimplemented formats fall back to decimal, so an unusual
    document degrades to the pre-numFmt-support behavior (a plain integer)
    rather than to a blank or wrong mark.
    """
    if value < 1:
        return str(value)
    if fmt == "decimal":
        return str(value)
    if fmt == "decimalZero":
        return f"{value:02d}"
    if fmt in ("upperRoman", "lowerRoman"):
        if value > 3999:
            return str(value)
        roman = _to_roman(value)
        return roman if fmt == "upperRoman" else roman.lower()
    if fmt in ("upperLetter", "lowerLetter"):
        # Word/LibreOffice repeat the alphabet rather than counting in base
        # 26: 1=A, ..., 26=Z, 27=AA, 28=BB, ... (verified against
        # LibreOffice's CHARS_UPPER_LETTER_N / lcl_formatChars1, the
        # reference reimplementation of Word's own behavior).
        letter = chr(ord("a") + (value - 1) % 26) * ((value - 1) // 26 + 1)
        return letter.upper() if fmt == "upperLetter" else letter
    if fmt == "chicago":
        # Cycles *, dagger, double-dagger, section-sign, then repeats each
        # doubled, tripled, etc. (verified against LibreOffice's
        # table_Chicago / lcl_formatChars1 - this is Word's own behavior,
        # not the broader "Chicago Manual of Style" footnote convention).
        symbol = _CHICAGO_SYMBOLS[(value - 1) % 4]
        return symbol * ((value - 1) // 4 + 1)
    if fmt == "none":
        return ""
    logger.debug("Unsupported footnote/endnote numFmt %r; falling back to decimal", fmt)
    return str(value)


def _parse_note_pr(pr: Optional[Any], base: "_NoteProps") -> "_NoteProps":
    """Apply the numbering children present on a w:footnotePr/w:endnotePr over `base`.

    Each child (w:numFmt, w:numStart, w:numRestart) is an independent
    override per ECMA-376 - a w:footnotePr/w:endnotePr carrying none of them
    (e.g. the separator-reference-only w:footnotePr/w:endnotePr present in
    every fixture's settings.xml) resolves to `base` unchanged. Works against
    both a stdlib ElementTree element (settings.xml, via part.blob) and a
    live lxml element (a section's w:sectPr) - both support
    .find(path, namespaces) and .get("{ns}attr") (verified directly against
    this repo's fixtures).
    """
    if pr is None:
        return base
    num_fmt = base.num_fmt
    num_start = base.num_start
    num_restart = base.num_restart
    fmt_el = pr.find("w:numFmt", _NS)
    if fmt_el is not None:
        val = fmt_el.get(f"{{{_W_NS}}}val")
        if val:
            num_fmt = val
    start_el = pr.find("w:numStart", _NS)
    if start_el is not None:
        val = start_el.get(f"{{{_W_NS}}}val")
        if val is not None:
            try:
                num_start = int(val)
            except ValueError:
                pass
    restart_el = pr.find("w:numRestart", _NS)
    if restart_el is not None:
        val = restart_el.get(f"{{{_W_NS}}}val")
        if val:
            num_restart = val
    return _NoteProps(num_fmt=num_fmt, num_start=num_start, num_restart=num_restart)


def _load_note_defaults(docx: DocxDocument) -> Tuple["_NoteProps", "_NoteProps"]:
    """Return (footnote_defaults, endnote_defaults) resolved from word/settings.xml."""
    footnote_base = _NoteProps(
        num_fmt=_DEFAULT_FOOTNOTE_FMT, num_start=_DEFAULT_NUM_START, num_restart=_DEFAULT_NUM_RESTART
    )
    endnote_base = _NoteProps(
        num_fmt=_DEFAULT_ENDNOTE_FMT, num_start=_DEFAULT_NUM_START, num_restart=_DEFAULT_NUM_RESTART
    )
    part = _find_package_part(docx, "/word/settings.xml")
    if part is None:
        return footnote_base, endnote_base
    root = ET.fromstring(part.blob)
    footnote_pr = root.find("w:footnotePr", _NS)
    endnote_pr = root.find("w:endnotePr", _NS)
    return _parse_note_pr(footnote_pr, footnote_base), _parse_note_pr(endnote_pr, endnote_base)


def _resolve_section_note_props(
    sect_pr: Optional[Any], fn_base: "_NoteProps", en_base: "_NoteProps"
) -> Tuple["_NoteProps", "_NoteProps"]:
    """Apply one w:sectPr's w:footnotePr/w:endnotePr override over the document defaults."""
    if sect_pr is None:
        return fn_base, en_base
    footnote_pr = sect_pr.find("w:footnotePr", _NS)
    endnote_pr = sect_pr.find("w:endnotePr", _NS)
    return _parse_note_pr(footnote_pr, fn_base), _parse_note_pr(endnote_pr, en_base)


@dataclass
class _NoteNumbering:
    """Per-document footnote/endnote numbering state for one DOCX body walk.

    `_counters` tracks the automatic-numbering counter per kind, reset to 0
    by begin_section() when that kind's new section properties specify
    numRestart="eachSect" - the label value is `num_start + counter - 1`, so
    a restart lands exactly on numStart. `_ordinals` tracks the 1-based
    document-order identity per kind and is NEVER reset; it is what
    NoteSpan.ordinal carries, and what group keys/sorts must use instead of
    `label` (labels repeat under numRestart or a cycling numFmt). `_seen`
    lets a repeated reference to the same (kind, w:id) reuse its first
    (ordinal, label) rather than advancing either counter again, mirroring
    the pre-existing `numbering.setdefault` behavior for a repeated w:id and
    merging both inlined bodies into one UI group.
    """

    footnote_props: "_NoteProps"
    endnote_props: "_NoteProps"
    _counters: Dict[str, int] = field(default_factory=lambda: {"footnote": 0, "endnote": 0})
    _ordinals: Dict[str, int] = field(default_factory=lambda: {"footnote": 0, "endnote": 0})
    _seen: Dict[Tuple[str, int], Tuple[int, str]] = field(default_factory=dict)
    _warned_each_page: bool = False

    def _props(self, kind: str) -> "_NoteProps":
        return self.footnote_props if kind == "footnote" else self.endnote_props

    def begin_section(self, props: Tuple["_NoteProps", "_NoteProps"]) -> None:
        """Advance to a new section's resolved footnote/endnote properties.

        Per kind independently, since footnote and endnote numbering restart
        independently per ECMA-376.
        """
        new_fn, new_en = props
        if new_fn.num_restart == "eachSect":
            self._counters["footnote"] = 0
        if new_en.num_restart == "eachSect":
            self._counters["endnote"] = 0
        self.footnote_props = new_fn
        self.endnote_props = new_en

    def next_label(self, kind: str, nid: int, custom_mark: Optional[str]) -> Tuple[int, str]:
        """Return (ordinal, label) for a reference to (kind, nid)."""
        key = (kind, nid)
        if key in self._seen:
            return self._seen[key]
        self._ordinals[kind] += 1
        ordinal = self._ordinals[kind]
        if custom_mark is not None:
            # A custom-marked note does not consume an automatic number.
            label = custom_mark
        else:
            props = self._props(kind)
            if props.num_restart == "eachPage" and not self._warned_each_page:
                logger.info(
                    'numRestart="eachPage" is not derivable from DOCX XML (page '
                    "boundaries are determined at Word's layout time, not stored "
                    "in the document); treating footnote/endnote numbering as continuous."
                )
                self._warned_each_page = True
            self._counters[kind] += 1
            value = props.num_start + self._counters[kind] - 1
            label = _format_note_label(value, props.num_fmt)
        result = (ordinal, label)
        self._seen[key] = result
        return result


def _custom_mark_text(ref: Any, run_el: Any) -> str:
    """Literal mark that follows a customMarkFollows footnote/endnote reference.

    Word emits the mark as the next sibling of the reference inside the same
    w:r - either a w:t (plain-text mark, decoded fully) or a w:sym (symbol-
    font mark whose w:char is a hex code point, normally in the F020-F0FF
    private-use range). For w:sym, only code points whose low byte is
    printable ASCII are decoded (covers w:char="F02A" -> "*"); anything else
    yields "" and the caller falls back to displaying the ordinal, because
    mapping Symbol/Wingdings code points to Unicode needs a font encoding
    table this module doesn't have and must not guess at.
    """
    siblings = list(run_el)
    try:
        ref_idx = siblings.index(ref)
    except ValueError:
        return ""
    for sibling in siblings[ref_idx + 1:]:
        if sibling.tag == qn("w:t"):
            return (sibling.text or "").strip()
        if sibling.tag == qn("w:sym"):
            char_attr = sibling.get(qn("w:char"))
            if char_attr:
                try:
                    code_point = int(char_attr, 16)
                except ValueError:
                    return ""
                low_byte = code_point & 0xFF
                if 0x20 <= low_byte <= 0x7E:
                    return chr(low_byte)
            return ""
    return ""


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


_FOOTNOTE_REF_QN: Final = qn("w:footnoteReference")
_ENDNOTE_REF_QN: Final = qn("w:endnoteReference")


def _para_with_inline_notes(
    p: Paragraph,
    footnotes: Dict[int, str],
    endnotes: Dict[int, str],
    numbering: "_NoteNumbering",
) -> str:
    """Inline footnote/endnote bodies at their reference markers.

    `numbering` is shared and mutated across every call for a given document
    so ordinals/labels stay consistent and sequential across the whole body
    walk (see _extract_docx_with_footnotes).

    Known limitation (documented, not fixed here): this walks `p.runs`, which
    python-docx defines as direct `w:r` children only
    (`CT_P.r_lst = ZeroOrMore("w:r")`). A footnote/endnote reference nested
    inside a `w:hyperlink`, `w:ins`, `w:smartTag`, `w:sdt`, or `w:fldSimple`
    is therefore silently skipped - its body is never inlined, no NoteSpan is
    produced, and any citations inside it are attributed to "Main text".
    """
    parts: List[str] = []
    for run in p.runs:
        r = run._r
        refs = [el for el in r.iter() if el.tag in (_FOOTNOTE_REF_QN, _ENDNOTE_REF_QN)]
        if refs:
            if run.text:
                parts.append(run.text)
            for ref in refs:
                kind = "footnote" if ref.tag == _FOOTNOTE_REF_QN else "endnote"
                nid_raw = ref.get(qn("w:id"))
                try:
                    nid = int(nid_raw)
                except (TypeError, ValueError):
                    continue
                body_map = footnotes if kind == "footnote" else endnotes
                ntxt = body_map.get(nid, "").strip()
                custom_mark = None
                if ref.get(qn("w:customMarkFollows")) in ("1", "true", "on"):
                    custom_mark = _custom_mark_text(ref, r)
                ordinal, label = numbering.next_label(kind, nid, custom_mark)
                if ntxt:
                    parts.append(f" {_wrap_note(kind, ordinal, label, ntxt)} ")
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


def _char_weighted_font_size(block: Dict[str, Any]) -> float:
    """Mean font size of the block's visible characters (0.0 if none).

    Weighted by character count, so a few small marker spans in a body
    paragraph can't make it look like footnote-size type.
    """
    total = 0.0
    chars = 0
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            count = len((span.get("text") or "").strip())
            size = float(span.get("size", 0.0) or 0.0)
            if count and size > 0:
                total += count * size
                chars += count
    return total / chars if chars else 0.0


def _first_text_line(block: Dict[str, Any]) -> str:
    """The block's first non-blank line (Word's PDF export starts footnote
    blocks with a blank separator line)."""
    for line in block.get("lines", []):
        text = _normalize_superscripts(_line_text_from_spans(line.get("spans", []))).strip()
        if text:
            return text
    return ""


def _starts_like_footnote(block: Dict[str, Any]) -> bool:
    return bool(_FOOTNOTE_REGION_START_RE.match(_first_text_line(block)))


def _is_footnote_block(
    block: Dict[str, Any],
    page_height: float,
    primary_font_size: float,
) -> bool:
    """Per-block footnote test, used only when _footnote_region finds no
    small-type region on the page (e.g. footnotes set in body-size type)."""
    lines = block.get("lines", [])
    if not lines:
        return False
    normalized_lines = [
        _normalize_superscripts(_line_text_from_spans(line.get("spans", []))).strip()
        for line in lines
    ]
    footnote_starts = [line for line in normalized_lines if _FOOTNOTE_REGION_START_RE.match(line)]
    if not footnote_starts:
        return False
    block_top = _block_top(block)
    block_size = _char_weighted_font_size(block)
    majority_threshold = max(1, len(lines) // 2)
    if page_height > 0 and block_top > page_height * 0.7:
        return True
    if primary_font_size > 0 and block_size > 0:
        if block_size <= primary_font_size * 0.85 and len(footnote_starts) >= majority_threshold:
            return True
    return False


def _footnote_region(
    blocks: Sequence[Dict[str, Any]],
    page_height: float,
    primary_font_size: float,
) -> Set[int]:
    """Return the indices (into `blocks`) of the page's footnote blocks.

    Footnotes sit at the bottom of the page in smaller type, so the region is
    the earliest block (by top) that starts like a footnote ("12. Text") such
    that it and every later block with text are set at most
    _FOOTNOTE_REGION_MAX_SIZE_RATIO of the body size - which also takes in
    unnumbered continuation blocks. Blocks in the running header/footer bands
    are never included. If there is no such run (e.g. footnotes in body-size
    type), falls back to the per-block _is_footnote_block test.
    """

    def in_page_body(block: Dict[str, Any]) -> bool:
        if page_height <= 0:
            return True
        top = _block_top(block)
        return page_height * _PAGE_HEADER_BAND <= top <= page_height * _PAGE_FOOTER_BAND

    ordered = sorted(
        (index for index, block in enumerate(blocks) if in_page_body(block)),
        key=lambda index: _block_top(blocks[index]),
    )
    if primary_font_size > 0:
        limit = primary_font_size * _FOOTNOTE_REGION_MAX_SIZE_RATIO
        sizes = {index: _char_weighted_font_size(blocks[index]) for index in ordered}
        for position, index in enumerate(ordered):
            if not _starts_like_footnote(blocks[index]):
                continue
            if all(sizes[later] <= limit for later in ordered[position:] if sizes[later] > 0):
                return set(ordered[position:])
    return {
        index for index in ordered if _is_footnote_block(blocks[index], page_height, primary_font_size)
    }


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


@dataclass
class _PdfNoteState:
    """Threaded through the PDF render helpers for one document.

    `used` answers "did this page already splice this printed number's body
    inline?" and `last` is the highest number spliced so far on the page;
    both are reset at the top of each page (see _extract_pdf_page_text) -
    printed footnote numbers can repeat across pages. `ordinal` is the
    document-global, never-reset 1-based order footnotes are spliced into
    the text, matching NoteSpan.ordinal's contract. Do not merge these
    lifetimes.
    """

    used: Set[int] = field(default_factory=set)
    last: int = 0
    ordinal: int = 0

    def next_ordinal(self) -> int:
        self.ordinal += 1
        return self.ordinal


def _span_marker_numbers(
    span: Dict[str, Any],
    primary_font_size: float,
) -> Optional[Tuple[List[int], str]]:
    """Return (numbers, digits) if the span looks like a footnote reference
    mark - digits only ("12", "3, 4") and superscripted or small - else None.

    Digits split by a synthesized space ("3 5", see _needs_space_between_chars)
    are rejoined. Looks only at the span itself: whether a number is really a
    reference to a footnote on this page is decided by
    _render_line_with_inline_footnotes.
    """
    text = span.get("text") or ""
    normalized = _normalize_superscripts(text).strip()
    if not normalized:
        return None
    digits = re.sub(r"(?<=\d) (?=\d)", "", normalized)
    if not _MARKER_DIGITS_RE.fullmatch(digits):
        return None
    font_size = float(span.get("size", 0.0) or 0.0)
    superscript = any(ch in _SUPERSCRIPT_CHARACTERS for ch in text) or bool(
        int(span.get("flags", 0) or 0) & pymupdf.TEXT_FONT_SUPERSCRIPT
    )
    small = (
        primary_font_size > 0
        and font_size > 0
        and font_size <= primary_font_size * _MARKER_MAX_SIZE_RATIO
    )
    if not superscript and not small:
        return None
    return [int(number) for number in re.findall(r"\d+", digits)], digits


def _footnote_available(number: int, footnotes: Dict[int, str], state: "_PdfNoteState") -> bool:
    """A marker number is accepted only if it's a footnote parsed on this page,
    not yet spliced, and above the page's last marker (markers ascend)."""
    return number in footnotes and number not in state.used and number > state.last


def _accept_marker(
    marker: Tuple[List[int], str],
    footnotes: Dict[int, str],
    state: "_PdfNoteState",
    parts: List[str],
) -> Optional[List[int]]:
    """Return the footnote numbers a marker span refers to, or None.

    For a single number that fails, tries an OCR split: the whole trailing
    digit run (1-2 digits, after a non-digit) of the last non-blank rendered
    part is the marker's leading digits ("respectively).1" + "4" -> 14). If
    that number is available, the prefix is removed from `parts` in place.
    """
    numbers, digits = marker
    if all(_footnote_available(number, footnotes, state) for number in numbers):
        return numbers
    if len(numbers) != 1:
        return None
    index = next((i for i in range(len(parts) - 1, -1, -1) if parts[i].strip()), None)
    if index is None:
        return None
    preceding = parts[index].rstrip()
    match = _OCR_MARKER_PREFIX_RE.search(preceding)
    if not match:
        return None
    number = int(match.group(1) + digits)
    if not _footnote_available(number, footnotes, state):
        return None
    parts[index] = preceding[: match.start()]
    return [number]


def _emit_footnote(number: int, footnotes: Dict[int, str], state: "_PdfNoteState") -> str:
    state.used.add(number)
    state.last = number
    body = _wrap_note("footnote", state.next_ordinal(), str(number), footnotes[number].strip())
    return f" {body} "


def _render_line_with_inline_footnotes(
    line: Dict[str, Any],
    footnotes: Dict[int, str],
    primary_font_size: float,
    state: "_PdfNoteState",
    detect_markers: bool = True,
) -> str:
    """Render a main-text line, splicing each footnote body in at its marker.

    Footnote bodies always come out in numeric order: before marker n, any
    still-unspliced page footnote numbered between the last marker and n
    (its marker wasn't recognized) is emitted first, so "Id." in footnote n
    follows footnote n-1 in the text.
    """
    spans = line.get("spans", [])
    joined: List[str] = []
    last_nonempty_span: Optional[Dict[str, Any]] = None
    last_nonempty_part = ""
    for span in spans:
        part = span.get("text") or ""
        marker = _span_marker_numbers(span, primary_font_size) if detect_markers and part else None
        numbers = _accept_marker(marker, footnotes, state, joined) if marker else None
        if numbers:
            chunks: List[str] = []
            for number in numbers:
                for pending in sorted(
                    n for n in footnotes if state.last < n < number and n not in state.used
                ):
                    chunks.append(_emit_footnote(pending, footnotes, state))
                chunks.append(_emit_footnote(number, footnotes, state))
            part = "".join(chunks)
        if _needs_space_between(last_nonempty_span, last_nonempty_part, span, part):
            joined.append(" ")
        joined.append(part)
        if part:
            last_nonempty_span = span
            last_nonempty_part = part
    line_text = "".join(joined)
    line_text = re.sub(r" {2,}", " ", line_text)
    return line_text.rstrip()


def _extract_pdf_page_text(page: pymupdf.Page, state: "_PdfNoteState") -> str:
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

    main_blocks: List[Dict[str, Any]] = []
    footnote_lines: List[str] = []

    text_blocks = [
        block
        for block in text_dict.get("blocks", [])
        if block.get("type") == 0 and block.get("lines")
    ]
    footnote_region = _footnote_region(text_blocks, page_height, primary_font_size)
    for index, block in enumerate(text_blocks):
        if index in footnote_region:
            for line in block["lines"]:
                footnote_lines.append(_line_text_from_spans(line.get("spans", [])))
        else:
            main_blocks.append(block)

    footnotes = _parse_footnote_lines(footnote_lines)
    state.used.clear()
    state.last = 0

    if not main_blocks:
        if footnotes:
            logger.info(
                "Detected footnotes without main text on page %s", getattr(page, "number", 0) + 1
            )
        return page.get_text("text") # type: ignore[attr-defined]

    lines_out: List[str] = []
    for block in main_blocks:
        # A footnote-looking block that still reached the main-text path is
        # never scanned for markers: its small citation digits ("85", "54")
        # must not be replaced by footnote bodies.
        detect_markers = not (
            _starts_like_footnote(block)
            and 0 < _char_weighted_font_size(block) <= primary_font_size * _FOOTNOTE_REGION_MAX_SIZE_RATIO
        )
        block_texts: List[str] = []
        for line in block["lines"]:
            block_texts.append(
                _render_line_with_inline_footnotes(
                    line, footnotes, primary_font_size, state, detect_markers
                )
            )
        if block_texts:
            if lines_out and lines_out[-1] != "":
                lines_out.append("")
            lines_out.extend(block_texts)

    unused = [num for num in sorted(footnotes) if num not in state.used and footnotes[num]]
    if unused:
        if lines_out:
            lines_out.append("")
        for num in unused:
            lines_out.append(_wrap_note("footnote", state.next_ordinal(), str(num), footnotes[num]))

    return "\n".join(lines_out)


def extract_pdf_text(file: FileStorage) -> ExtractedDocument:
    """Extract text from PDF file, inserting footnotes inline when present.

    Returns an ExtractedDocument: `text` is inline-footnote text with
    sentinel markers stripped, `footnotes` gives each footnote's number and
    character span within `text`. Footnotes recovered from pages that fell
    back to raw `page.get_text("text")` or OCR (no main-text blocks, or
    "rawdict" extraction failed) are not represented - those code paths never
    wrap a footnote body, matching pre-existing best-effort behavior for
    those pages. `ocr_skipped_pages` lists any image-only page (no text
    layer) that could not be OCR'd because Tesseract is unavailable; a
    genuinely blank page (no text and no image) is never OCR'd and never
    appears there. If every page has no text and at least one was an OCR
    candidate, raises ValueError instead of returning an empty document.
    """

    file.stream.seek(0)
    page_texts: List[str] = []
    ocr_skipped: List[int] = []
    any_page_has_text = False
    ocr_ok = ocr_available()
    note_state = _PdfNoteState()

    try:
        pdf_bytes = file.stream.read()
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                page_text = _extract_pdf_page_text(page, note_state)
                if not page_text.strip():
                    raw_text = page.get_text("text") # type: ignore[attr-defined]
                    if raw_text.strip():
                        page_text = raw_text
                    elif page.get_image_info(): # type: ignore[attr-defined]
                        # Page has an image but no text layer - an OCR
                        # candidate, as opposed to a genuinely blank page.
                        if ocr_ok:
                            try:
                                page_text = _ocr_page(page)
                            except _OcrUnavailable:
                                # `which` found a binary but it failed to
                                # execute; degrade for the rest of this
                                # document rather than retry every page.
                                ocr_ok = False
                                ocr_skipped.append(getattr(page, "number", 0) + 1)
                                page_text = ""
                        else:
                            ocr_skipped.append(getattr(page, "number", 0) + 1)
                            page_text = ""
                    else:
                        page_text = ""
                if page_text.strip():
                    any_page_has_text = True
                page_texts.append(page_text.strip())
    except Exception as exc:  # pragma: no cover - pass through for callers
        raise ValueError(f"Failed to extract text from PDF: {exc}") from exc

    if ocr_skipped and not any_page_has_text:
        raise ValueError(
            "This PDF appears to be a scanned image with no text layer, and "
            "OCR is not available on this server. Please upload a text-based "
            "PDF or a DOCX file."
        )
    if ocr_skipped:
        logger.warning(
            "OCR unavailable; skipped %d image-only page(s) with no text layer: %s",
            len(ocr_skipped),
            ocr_skipped,
        )

    combined = "\n\n\f\n\n".join(page_texts).strip()
    normalized = _normalize(combined)
    text, footnotes = _strip_note_markers(normalized)
    return ExtractedDocument(text=text, footnotes=footnotes, ocr_skipped_pages=tuple(ocr_skipped))

def _extract_docx_with_footnotes(doc: DocxDocument) -> str:
    """Return DOCX body text with footnotes/endnotes inlined at their references.

    Each note body is wrapped in sentinel markers (see _wrap_note) so the
    caller can later recover (kind, ordinal, label, span) via
    _strip_note_markers. Automatic numbering honors the document's
    w:footnotePr/w:endnotePr numFmt/numStart/numRestart, resolved from
    word/settings.xml and overridden per-section by any w:sectPr's own
    w:footnotePr/w:endnotePr (see _NoteNumbering/_resolve_section_note_props).
    A w:sectPr sits on the last paragraph of the section it ends (except the
    final section's, which is a direct w:body child) - so a section's
    resolved properties are applied via numbering.begin_section() *after*
    processing the paragraph that carries that sectPr, not to that paragraph
    itself, matching the spec's own section-boundary semantics.

    Args:
        doc: An opened python-docx Document.

    Returns:
        A single string containing paragraph text with inline footnotes/endnotes.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file does not have a .docx extension.
    """
    footnotes = _load_notes_map(doc, "/word/footnotes.xml", "w:footnote")
    endnotes = _load_notes_map(doc, "/word/endnotes.xml", "w:endnote")

    fn_base, en_base = _load_note_defaults(doc)
    sect_prs = doc.element.xpath("./w:body/w:p/w:pPr/w:sectPr | ./w:body/w:sectPr")
    resolved = [_resolve_section_note_props(sp, fn_base, en_base) for sp in sect_prs] or [(fn_base, en_base)]
    numbering = _NoteNumbering(*resolved[0])
    section_idx = 0

    lines: List[str] = []

    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            t = _para_with_inline_notes(block, footnotes, endnotes, numbering)
            if t:
                lines.append(t)
            if block._p.xpath("./w:pPr/w:sectPr"):
                section_idx += 1
                if section_idx < len(resolved):
                    numbering.begin_section(resolved[section_idx])
        else:
            for par in _iter_table_paragraphs(block):
                t = _para_with_inline_notes(par, footnotes, endnotes, numbering)
                if t:
                    lines.append(t)

    return "\n\n".join(lines)



def extract_docx_text(file: FileStorage) -> ExtractedDocument:
    """Extract text from DOCX file, including footnotes/endnotes inline.

    Args:
        file: FileStorage object containing DOCX data.

    Returns:
        ExtractedDocument with normalized text (footnotes/endnotes inline,
        sentinel markers stripped) and each note's kind/label/ordinal/span
        within that text.

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
    text, footnotes = _strip_note_markers(normalized)
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
