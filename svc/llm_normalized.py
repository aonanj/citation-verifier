# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""Pass 1 of the LLM citation extractor for a NormalizedDocument (plan sections 9-11).

svc.llm_extractor.extract_citations calls extract_pass_one() instead of chunking the display text when it is given a
NormalizedDocument (DOCUMENT_NORMALIZATION=on). What changes is only where the model reads the document and how
its answer is checked; the model, the instructions' rules of faithfulness, pass 2 (short-form resolution) and the
citation records are the extractor's own.

  DOCX -> the tagged text, chunked at block boundaries (a paragraph stays with its notes);
  PDF  -> the PDF itself, in windows of LLM_PDF_PAGES_PER_REQUEST pages (default 8) plus one page of lookahead so a
          citation that runs over a page break is seen whole. A window is only asked about citations that BEGIN on its
          own pages. Pages travel as inline base64 `input_file` parts with detail="high" (small footnote type) and
          store=False; nothing is uploaded to a file store.

Every citation the model returns carries a source_id (and a page for a PDF). The answer is then validated locally
(svc.normalization.validator: the text must be in that source block, exactly or after conservative normalization;
duplicates and overlaps are dropped; nothing is repaired) and only what passes is located in the display text
(svc.normalization.alignment) and grounded field by field exactly as on the text path.

Nothing here logs document text or model output.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from svc import llm_extractor as core
from svc.normalization.alignment import BlockWindow, block_windows
from svc.normalization.models import NormalizedDocument
from svc.normalization.text import strip_inline_markup, unescape_delimiters
from svc.normalization.validator import CitationSpanValidator, ValidatedCitation
from utils.logger import get_logger

logger = get_logger()

DEFAULT_PDF_PAGES_PER_REQUEST = 8
# OpenAI accepts at most 50 MB of files per request; stay well under it (base64 adds a third).
_MAX_UNIT_PDF_BYTES = 30 * 1024 * 1024
# Values that name a category, not text written in the document: never markup-cleaned.
_ENUM_KEYS = {"kind", "category", "type_hint", "source_type", "jurisdiction", "source_id"}

_TOP_LEVEL_RE = re.compile(r"\n(?=<(?:paragraph|footnote|endnote|textbox|table|header|footer)\b)")
_ATTACHED_RE = re.compile(r"<(?:footnote|endnote|textbox)\b")
_ID_RE = re.compile(r'\bid="([^"]+)"')
_PAGE_ID_RE = re.compile(r"^pg-(\d+)$")


# --- schema and instructions --------------------------------------------------------------------

def _build_schema() -> Dict[str, Any]:
    """The extractor's citation schema plus where each citation was found.

    The plan's `page` is called `source_page` here: `page` already exists in the schema (the cited work's own first
    page, "63" in "409 U.S. 63") and must keep meaning that.
    """
    schema = copy.deepcopy(core._CITATIONS_SCHEMA)
    for variant in schema["properties"]["citations"]["items"]["anyOf"]:
        props = variant["properties"]
        props["source_id"] = {"type": "string"}
        props["source_page"] = {"type": ["integer", "null"]}
        props["confidence"] = {"type": "number"}
        variant["required"] = list(props)
    return schema


_SCHEMA: Dict[str, Any] | None = None


def citations_schema() -> Dict[str, Any]:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = _build_schema()
    return _SCHEMA


_TAGGED_FORMAT = """\
DOCUMENT FORMAT: the chunk is tagged text. <paragraph id="p-0001"> is a paragraph, <cell id="tc-0001-0002-0001"> a table \
cell, <textbox id="tx-0001" anchor="p-0002"> a text box, and <footnote id="fn-3" anchor="p-0001"> / <endnote id="en-1" \
anchor="p-0001"> a note; a note's text follows the paragraph it is attached to, and <footnote-ref id="fn-3" /> marks \
where the paragraph refers to it. <i>, <b>, <u>, <sup>, <sub> and <sc> mark italic, bold, underlined, superscript, \
subscript and small-capital text. All tags and their attributes are markup, not document text: never include them in \
matched_text or any field, and copy the text between them exactly (a literal "<" or ">" in the document is written \
&lt; or &gt;).
SOURCE: every citation carries source_id, the id of the innermost <paragraph>, <cell>, <textbox>, <footnote> or <endnote> \
element that contains the FIRST character of matched_text. Copy the id exactly; never invent one. source_page is null."""

_PDF_FORMAT = """\
DOCUMENT FORMAT: you are given pages of a PDF (its text and its page images). Read the body text and the footnotes at \
the bottom of each page: a footnote is text like any other, so extract the citations in it where they are written. A \
line break inside a citation is written as a single space in matched_text.
COPYING: matched_text is checked against the text layer that accompanies the page images. Copy it character for \
character from that extracted text, including the hyphens and dashes it contains, even where the image shows a \
different character (a scan's text layer often reads an en dash as a hyphen). Use the images for what the text \
cannot tell you, such as which page a citation is on.
PAGES: page numbers are positions in the file you were given (the first page is 1); ignore the page numbers printed on \
the pages. {scope}
SOURCE: every citation carries source_page, the page of the file where matched_text begins (not the cited work's own \
page, which stays in `page`), and source_id, exactly "pg-" followed by that number (source_page 3 -> "pg-3")."""


def instructions_for(kind: str, primary: int = 0, lookahead: bool = False) -> str:
    """The extractor's instructions with the notes paragraph replaced by this input's format and source rules."""
    if kind == "tagged_text":
        block = _TAGGED_FORMAT
    else:
        scope = f"Extract only citations that BEGIN on pages 1 to {primary}."
        if lookahead:
            scope += (f" Page {primary + 1} is context only: a citation may continue onto it, but do not extract"
                      " citations that begin on it.")
        block = _PDF_FORMAT.format(scope=scope)
    head, marker, tail = core._EXTRACT_INSTRUCTIONS.partition("\nNOTES:")
    if not marker:
        return core._EXTRACT_INSTRUCTIONS + "\n\n" + block
    _, closing_marker, closing = tail.partition("\n\nReturn the citations")
    return head + "\n" + block + (closing_marker + closing if closing_marker else "")


# --- units of work -------------------------------------------------------------------------------

@dataclass
class _TextUnit:
    groups: List[List[str]]  # fragments, grouped: a paragraph (or table) with the notes attached to it

    @property
    def text(self) -> str:
        return '<document>\n' + "\n".join(f for g in self.groups for f in g) + "\n</document>"

    @property
    def ids(self) -> frozenset:
        return frozenset(i for g in self.groups for f in g for i in _ID_RE.findall(f))

    def split(self) -> List["_TextUnit"]:
        if len(self.groups) < 2:
            return [self]
        middle = len(self.groups) // 2
        return [_TextUnit(self.groups[:middle]), _TextUnit(self.groups[middle:])]


@dataclass
class _PdfUnit:
    first: int  # first page (1-based) in the whole document
    primary: int  # pages first .. first + primary - 1 are asked about
    lookahead: bool  # the following page is included as context

    @property
    def pages(self) -> int:
        return self.primary + (1 if self.lookahead else 0)

    def split(self) -> List["_PdfUnit"]:
        if self.primary < 2:
            return [self]
        half = self.primary // 2
        return [_PdfUnit(self.first, half, True), _PdfUnit(self.first + half, self.primary - half, self.lookahead)]


def _text_units(document: NormalizedDocument) -> List[_TextUnit]:
    tagged = document.model_input_text or ""
    body = re.sub(r"^<document[^>]*>\n", "", tagged)
    body = body[:-len("\n</document>")] if body.endswith("\n</document>") else body
    groups: List[List[str]] = []
    for fragment in (f for f in _TOP_LEVEL_RE.split(body) if f.strip()):
        if groups and _ATTACHED_RE.match(fragment):
            groups[-1].append(fragment)
        else:
            groups.append([fragment])
    units: List[_TextUnit] = []
    current: List[List[str]] = []
    size = 0
    for group in groups:
        length = sum(len(f) for f in group)
        if current and size + length > core._CHUNK_CHARS:
            units.append(_TextUnit(current))
            current, size = [], 0
        current.append(group)
        size += length
    if current:
        units.append(_TextUnit(current))
    return units


def _pdf_units(document: NormalizedDocument) -> List[_PdfUnit]:
    pages = [b for b in document.blocks if b.kind == "page"]
    per = max(int(os.getenv("LLM_PDF_PAGES_PER_REQUEST") or DEFAULT_PDF_PAGES_PER_REQUEST), 1)
    units = []
    for first in range(1, len(pages) + 1, per):
        primary = min(per, len(pages) - first + 1)
        # A window whose pages have no text at all has nothing an answer could be validated against.
        if any(pages[p - 1].raw_text.strip() for p in range(first, first + primary)):
            units.append(_PdfUnit(first, primary, first + primary <= len(pages)))
    return units


def _pdf_bytes(path: str, unit: _PdfUnit) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(path, strict=False)
    writer = PdfWriter()
    for index in range(unit.first - 1, unit.first - 1 + unit.pages):
        writer.add_page(reader.pages[index])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _pdf_input(pdf: bytes, primary: int) -> List[Dict[str, Any]]:
    encoded = base64.b64encode(pdf).decode("ascii")
    return [{
        "role": "user",
        "content": [
            {"type": "input_file", "filename": "document.pdf", "file_data": f"data:application/pdf;base64,{encoded}", "detail": "high"},
            {"type": "input_text", "text": f"Extract the citations that begin on pages 1 to {primary} of this PDF."},
        ],
    }]


# --- pass 1 requests -----------------------------------------------------------------------------

@dataclass
class _Answer:
    items: List[Dict[str, Any]]
    rejected: int  # items refused before validation (a source_id outside the request, a context page)
    mismatched_pages: int


async def _ask_text_unit(
    client: Any, config: Any, semaphore: asyncio.Semaphore, unit: _TextUnit, depth: int = 0,
) -> List[_Answer]:
    data = await core._structured_call(
        client, config, semaphore, instructions_for("tagged_text"), f"Document chunk:\n\n{unit.text}",
        "citations", citations_schema(), core._EXTRACT_MAX_OUTPUT_TOKENS,
    )
    if data is None:
        halves = unit.split()
        if depth >= core._MAX_SPLIT_DEPTH or len(halves) < 2:
            raise core.CitationExtractionError("citations response exceeded the output limit")
        logger.info("LLM extractor: a chunk's answer hit the output limit; splitting it in two")
        results = await asyncio.gather(*(_ask_text_unit(client, config, semaphore, h, depth + 1) for h in halves))
        return [a for r in results for a in r]
    ids = unit.ids
    kept, rejected = [], 0
    for item in data.get("citations") or []:
        if item.get("source_id") in ids:
            kept.append(item)
        else:
            rejected += 1
    return [_Answer(kept, rejected, 0)]


async def _ask_pdf_unit(
    client: Any, config: Any, semaphore: asyncio.Semaphore, path: str, unit: _PdfUnit, depth: int = 0,
) -> List[_Answer]:
    pdf = await asyncio.to_thread(_pdf_bytes, path, unit)
    if len(pdf) > _MAX_UNIT_PDF_BYTES:
        halves = unit.split()
        if len(halves) < 2:
            raise core.CitationExtractionError("a PDF page is too large to send")
        results = await asyncio.gather(*(_ask_pdf_unit(client, config, semaphore, path, h, depth) for h in halves))
        return [a for r in results for a in r]
    data = await core._structured_call(
        client, config, semaphore, instructions_for("pdf", unit.primary, unit.lookahead), _pdf_input(pdf, unit.primary),
        "citations", citations_schema(), core._EXTRACT_MAX_OUTPUT_TOKENS,
    )
    if data is None:
        halves = unit.split()
        if depth >= core._MAX_SPLIT_DEPTH or len(halves) < 2:
            raise core.CitationExtractionError("citations response exceeded the output limit")
        logger.info("LLM extractor: a PDF window's answer hit the output limit; splitting it in two")
        results = await asyncio.gather(*(_ask_pdf_unit(client, config, semaphore, path, h, depth + 1) for h in halves))
        return [a for r in results for a in r]
    kept, rejected, mismatched = [], 0, 0
    for item in data.get("citations") or []:
        match = _PAGE_ID_RE.match(str(item.get("source_id") or ""))
        local = int(match.group(1)) if match else 0
        if not 1 <= local <= unit.primary:
            rejected += 1  # a page that isn't one of this request's own (or no page at all)
            continue
        if item.get("source_page") not in (None, local):
            mismatched += 1
        item = dict(item)
        item["source_id"] = f"pg-{unit.first + local - 1}"
        item["source_page"] = unit.first + local - 1
        kept.append(item)
    return [_Answer(kept, rejected, mismatched)]


# --- validate, locate, hand over ------------------------------------------------------------------

def _clean_item(vc: ValidatedCitation) -> Dict[str, Any]:
    """The model's item with markup it copied from the tagged text removed, ready for grounding."""
    item: Dict[str, Any] = {}
    for key, value in vc.item.items():
        if isinstance(value, str) and key not in _ENUM_KEYS:
            value = unescape_delimiters(strip_inline_markup(value)[0])
        item[key] = value
    item["matched_text"] = vc.citation_text
    item["_validation"] = vc.status.value
    return item


def _answers_for_grounding(
    accepted: Sequence[ValidatedCitation],
    windows: Dict[int, BlockWindow],
    text: str,
) -> List[Tuple[Tuple[int, int], List[Dict[str, Any]], Tuple[Tuple[int, int], ...], Any]]:
    """[(window, items, reserved spans, string-group key)] in document order, one entry per source block."""
    by_block: Dict[int, List[ValidatedCitation]] = {}
    for vc in accepted:
        by_block.setdefault(vc.block_index if vc.block_index is not None else -1, []).append(vc)
    answers = []
    for block_index in sorted(by_block):
        group = by_block[block_index]
        window = windows.get(block_index)
        if window is None:
            span, reserved = (0, len(text)), ()
        else:
            end = window.end
            for vc in group:  # a citation crossing a page break continues in the next block
                if vc.end_block_index not in (None, block_index) and vc.end_block_index in windows:
                    end = max(end, windows[vc.end_block_index].end)
            span, reserved = (window.start, end), window.reserved
        answers.append((span, [_clean_item(vc) for vc in group], reserved, ("block", block_index)))
    return answers


async def extract_pass_one(
    client: Any,
    config: Any,
    semaphore: asyncio.Semaphore,
    text: str,
    notes: Sequence[Any] | None,
    document: NormalizedDocument,
) -> List[Tuple[Tuple[int, int], List[Dict[str, Any]], Tuple[Tuple[int, int], ...], Any]]:
    """Ask the model, validate its answers against the document's blocks, and return them located in `text` as
    `answers` for llm_extractor._grounded_citations. Records timings and counts on document.telemetry."""
    started = time.monotonic()
    if document.model_input_kind == "tagged_text":
        units: List[Any] = _text_units(document)
        answers_by_unit = await asyncio.gather(*(_ask_text_unit(client, config, semaphore, u) for u in units))
    elif document.model_input_kind == "pdf" and document.model_input_path:
        units = _pdf_units(document)
        answers_by_unit = await asyncio.gather(
            *(_ask_pdf_unit(client, config, semaphore, document.model_input_path, u) for u in units))
    else:
        raise core.CitationExtractionError("the normalized document has no input for the model")
    model_ms = int((time.monotonic() - started) * 1000)

    answers = [a for group in answers_by_unit for a in group]
    items = [item for a in answers for item in a.items]
    rejected = sum(a.rejected for a in answers)

    started = time.monotonic()

    def validate_and_locate() -> Tuple[Any, Any]:
        report = CitationSpanValidator(document).validate(items)
        return report, block_windows(document, text, notes)

    report, windows = await asyncio.to_thread(validate_and_locate)
    accepted = report.accepted()
    counts = report.counts()
    counts["validation_unresolved"] += rejected
    validation_ms = int((time.monotonic() - started) * 1000)

    for key, value in counts.items():
        core._add_stat(key, value)
    document.telemetry.update({
        **counts,
        "llm_units": len(units),
        "llm_citations_returned": len(items) + rejected,
        "llm_page_source_mismatches": sum(a.mismatched_pages for a in answers),
        "blocks_aligned": len(windows),
        "model_duration_ms": model_ms,
        "validation_duration_ms": validation_ms,
    })
    if counts["validation_unresolved"]:
        document.warnings.append(f"citations_unmatched:{counts['validation_unresolved']}")
    logger.info(
        "LLM normalized extraction: %d unit(s), %d returned, %d accepted (%d exact, %d normalized, %d ambiguous), "
        "%d unresolved, %d duplicate, %d overlapping; model %d ms, validation %d ms",
        len(units), len(items) + rejected, len(accepted), counts["validation_exact_matches"],
        counts["validation_normalized_matches"], counts["validation_ambiguous"], counts["validation_unresolved"],
        counts["validation_duplicates"], counts["validation_overlapping"], model_ms, validation_ms,
    )
    return _answers_for_grounding(accepted, windows, text)
