# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""LLM citation extractor: an AI model (AI_MODEL) finds and parses the citations,
and deterministic code checks every answer against the document text.

extract_citations():
  1. _chunk_document splits the text at paragraph breaks and note boundaries
     (never inside a footnote/endnote when avoidable).
  2. Pass 1, one request per chunk, concurrently: the model lists the chunk's
     citations with a verbatim `matched_text` and structured fields.
  3. Grounding: code locates each `matched_text` in the document and computes
     the span itself (the model never supplies offsets); a citation it can't
     locate is dropped. Each field must be written in the citation (case
     names may also stand just before it); a field that isn't becomes None,
     so a value the model corrected or supplied is never verified as fact.
  4. Pass 2, one request: the model reads the ordered, compact list of all
     grounded citations and names the full citation each short form, Id. or
     supra refers to. Code checks the answer exists, comes earlier and is of
     a compatible type; an Id. after a string citation is flagged (Bluebook
     Rule 4.1) regardless of the model's answer.

Any failed request raises CitationExtractionError: a failure must never look
like a document with no citations. Requests use store=False, and nothing here
logs document text or model output.

The model is AI_MODEL and its key AI_API_KEY (utils/ai_model.py). Only OpenAI
models (names starting with "gpt") are implemented: the OpenAI SDK is imported
and called only for them, and any other AI_MODEL raises CitationExtractionError.

OpenAI models use a 'bluebook-citation-extraction' skill: 
{
  "id": "skill_6aaf69cd52388191b5808d2a2140bfe10b43bf1c1afe9ad1",
  "object": "skill",
  "created_at": 1789880781,
  "default_version": "1",
  "description": "Faithfully extract the supported Bluebook-style legal citations from supplied document text, or resolve extracted short forms to earlier full citations, using the strict JSON contracts expected by llm_extractor.py. Use for grounded transcription and antecedent resolution; do not use to verify, correct, normalize, or generate authorities.",
  "latest_version": "1",
  "name": "bluebook-citation-extraction"
}

{
  "id": "skill_6ab04c0010e08191bf01aa7c8a9468b10a8c3b342a85af55",
  "object": "skill",
  "created_at": 1789938688,
  "default_version": "1",
  "description": "Faithfully extract the supported Bluebook-style legal citations from supplied document text, or resolve extracted short forms to earlier full citations, using the strict JSON contracts expected by llm_extractor.py. Use for grounded transcription and antecedent resolution; do not use to verify, correct, normalize, or generate authorities.",
  "latest_version": "1",
  "name": "bluebook-citation-extraction"
}
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from bisect import bisect_right
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

import httpx2
import reporters_db

from svc.citation_record import CitationRecord
from utils.ai_model import ai_model
from utils.cleaner import clean_str
from utils.logger import get_logger

logger = get_logger()

DEFAULT_REASONING_EFFORT = "medium"

_CHUNK_CHARS = 6000
_MAX_CONCURRENCY = 8
_REQUEST_TIMEOUT = httpx2.Timeout(180.0, connect=10.0)
_EXTRACT_MAX_OUTPUT_TOKENS = 20000
_RESOLVE_MAX_OUTPUT_TOKENS = 40000
# A chunk whose answer is cut off at the output limit is split in half and
# retried, at most this many times.
_MAX_SPLIT_DEPTH = 2
# How far before a citation its name, author or title may stand ("In Roe v.
# Wade, the Court ... 410 U.S. 113"; a footnote citing a case the main text
# just named).
_NAME_WINDOW = 300

# Set by the eval harness to collect token usage; None in production.
USAGE: ContextVar[Dict[str, int] | None] = ContextVar("llm_extractor_usage", default=None)


class CitationExtractionError(RuntimeError):
    """The LLM extractor could not produce a result."""


# --- configuration ----------------------------------------------------------------

@dataclass(frozen=True)
class _Config:
    provider: str
    api_key: str
    model: str
    organization: str | None = None
    project_id: str | None = None
    reasoning: str | None = None
    skill_id: str | None = None
    skill_version: int | str | None = None


def _config() -> _Config:
    """Read at call time: .env is loaded after this module is imported."""
    model = ai_model()
    if not model:
        logger.error("AI_MODEL_ENV is not set.")
        raise CitationExtractionError("AI_MODEL_ENV is not set")
    api_key = model.get("ai_api_key")
    if not api_key:
        logger.error("AI_API_KEY_ENV is not set.")
        raise CitationExtractionError("AI_API_KEY_ENV is not set")
    return _Config(
        provider=model.get("provider") or "",
        api_key=api_key,
        model=model.get("model") or "",
        reasoning=(os.getenv("AI_EXTRACT_REASONING") or DEFAULT_REASONING_EFFORT).strip(),
        organization=model.get("organization"),
        project_id=model.get("project_id"),
        skill_id=model.get("skill_id"),
    )


# --- result type ------------------------------------------------------------------

FULL_KINDS = ("case", "law", "journal", "secondary")
SHORT_CATEGORIES = ("short", "id", "ibid", "supra", "reference")


@dataclass
class GroundedCitation:
    kind: str  # "case" | "law" | "journal" | "secondary" | "short_form"
    category: str  # "full" | "short" | "id" | "ibid" | "supra" | "reference"
    type_hint: str  # the citation's type ("unknown" for a short form of unclear type)
    matched_text: str  # the document's own text at span
    span: Tuple[int, int]
    fields: Dict[str, Any]
    string_group: int | None = None  # document-wide string citation number
    index: int = -1  # position in the document-ordered list
    refers_to: int | None = None  # index of the full citation a short form refers to
    id_error_group: int | None = None  # set on an Id. that refers back to this string group
    record: CitationRecord | None = None  # full citations only
    # Set only when the model read a NormalizedDocument (svc/llm_normalized.py): the block it reported the
    # citation in, the PDF page, and the validator's verdict on it.
    source_id: str | None = None
    source_page: int | None = None
    validation: str | None = None

    @property
    def is_full(self) -> bool:
        return self.category == "full"

    @property
    def pin_cite(self) -> str | None:
        return self.fields.get("pin_cite")


# --- JSON schemas ---------------------------------------------------------------------

_STR = {"type": ["string", "null"]}
_GROUP = {"type": ["integer", "null"]}


def _variant(kind: str, properties: Dict[str, Any]) -> Dict[str, Any]:
    props = {"kind": {"type": "string", "enum": [kind]}, "matched_text": {"type": "string"}, **properties}
    return {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}


_CITATIONS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["citations"],
    "properties": {
        "citations": {
            "type": "array",
            "items": {
                "anyOf": [
                    _variant("case", {
                        "core_text": _STR, "case_name": _STR, "volume": _STR, "reporter": _STR, "page": _STR,
                        "pin_cite": _STR, "court": _STR, "year": _STR, "string_group": _GROUP,
                    }),
                    _variant("law", {
                        "core_text": _STR,
                        "jurisdiction": {"type": "string", "enum": ["federal", "state", "unknown"]},
                        "reporter": _STR, "title_number": _STR, "section": _STR, "volume": _STR, "page": _STR,
                        "congress": _STR, "law_number": _STR, "pin_cite": _STR, "year": _STR,
                        "string_group": _GROUP,
                    }),
                    _variant("journal", {
                        "core_text": _STR, "author": _STR, "title": _STR, "journal": _STR, "volume": _STR,
                        "page": _STR, "pin_cite": _STR, "year": _STR, "string_group": _GROUP,
                    }),
                    _variant("secondary", {
                        "source_type": {"type": "string", "enum": ["cjs", "amjur", "alr", "restatement", "treatise"]},
                        "author": _STR, "title": _STR, "volume": _STR, "section": _STR, "page": _STR,
                        "edition": _STR, "series": _STR, "pin_cite": _STR, "year": _STR, "string_group": _GROUP,
                    }),
                    _variant("short_form", {
                        "category": {"type": "string", "enum": list(SHORT_CATEGORIES)},
                        "type_hint": {"type": "string", "enum": [*FULL_KINDS, "unknown"]},
                        "refers_to_name": _STR, "volume": _STR, "reporter": _STR, "pin_cite": _STR,
                        "note_reference": _STR, "string_group": _GROUP,
                    }),
                ]
            },
        }
    },
}

_RESOLUTIONS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resolutions"],
    "properties": {
        "resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "refers_to"],
                "properties": {"id": {"type": "integer"}, "refers_to": {"type": ["integer", "null"]}},
            },
        }
    },
}

_EXTRACT_INSTRUCTIONS = """\
Use the bluebook-citation-extraction skill in `citations` mode. \
You extract legal citations from one chunk of a legal document (a brief, memo, opinion or article). \
A separate system verifies each citation against legal databases, so your output must be a faithful \
transcription of what the document says.

FAITHFULNESS (most important)
- Copy every value exactly as the document writes it, including typos, wrong numbers, odd spacing and \
capitalization. Never correct, complete, standardize or look up anything. If the document cites Roe v. \
Wade as "411 U.S. 113", report volume "411". An error copied faithfully is what lets the verifier catch it.
- If the citation itself does not state a value, return null. Never supply a year, court, author, title \
or name from your own knowledge or from a different citation.
- matched_text must be copied character for character from the chunk: one contiguous stretch of the \
document text. Never assemble a citation from pieces in different places (a case named in the main text, \
its reporter citation in a footnote): extract the citation where it is written and put the name in \
case_name. Never write out a full citation the document does not contain: a short form whose full \
citation is not in the chunk is still a short_form.
- List each citation once, where it appears. A citation repeated later in the text is listed again \
at that later place. Every "Id." is a citation, including a bare "Id." that makes up a whole footnote: \
list each one.
- Citations inside a parenthetical ("(quoting Hartranft, 121 U.S. at 615)", "(citing 35 U.S.C. § 101)") \
are citations too: list them separately.

WHAT TO EXTRACT, in order of appearance, every citation to:
- case: court decisions ("Roe v. Wade, 410 U.S. 113, 115 (1973)", "In re Deuel, 51 F.3d 1552 (Fed. Cir. \
1995)", "Smith v. Jones, No. 12-cv-345, 2014 WL 123456, at *3 (D.D.C. Jan. 2, 2014)").
- law: constitutions, statutes, session laws, regulations and the Federal Register ("35 U.S.C. § 101 \
(2006)", "37 C.F.R. § 1.56", "Pub. L. No. 112-29, 125 Stat. 284 (2011)", "Cal. Civ. Proc. Code § 425.16 \
(West 2020)", "U.S. Const. art. I, § 8, cl. 8").
- journal: articles in law reviews and journals ("Lori B. Andrews, The Gene Patent Dilemma, 2 Hous. J. \
Health L. & Pol'y 65, 90 (2002)").
- secondary: treatises and other books, restatements, legal encyclopedias (C.J.S., Am. Jur.) and A.L.R. \
annotations.
- short_form: a later reference to an authority: a short-form case citation ("Roe, 410 U.S. at 115", \
"410 U.S. at 115"), "Id." or "id." (with or without a pin cite), "Ibid.", a supra reference ("Andrews, \
supra note 12, at 90"), or a case named with a pin cite but no reporter ("Roe at 115", category \
"reference").
Do not extract record citations ("Compl. ¶ 12", "Tr. 45"), docket entries, cross-references within the \
document ("see infra Part II"), section references that do not name the code ("under § 101"), case names \
or statute names mentioned without a citation ("the Copyright Act of 1976"), legislative history \
(committee reports such as "H.R. Rep. No. 1923", the Congressional Record, U.S.C.C.A.N.), or websites \
and news articles.

EXTENT OF matched_text
- Leave out introductory signals ("See", "See, e.g.,", "Cf.", "But see", "accord", "see also") and \
explanatory parentheticals ("(holding that ...)").
- A full citation runs from the first word of the case name, author or volume number through the closing \
parenthetical with the date (and court), or through the last page or section when there is none. If the \
case name is separated from the reporter citation by other words, start matched_text at the volume number \
and still report case_name.
- A parallel citation ("410 U.S. 113, 93 S. Ct. 705 (1973)") is ONE case citation covering all of it; \
report the first reporter's volume, reporter and page.
- Subsequent history ("aff'd, 5 F.4th 1 (2d Cir. 2021)") is a separate case citation.
- A short form's matched_text is the short form with its pin cite ("Roe, 410 U.S. at 115", "Id. at 629", \
"Andrews, supra note 12, at 90", "Id.").

FIELDS (null when the citation does not state it; digits exactly as written)
- core_text: the identifying core, copied from matched_text: "410 U.S. 113", "35 U.S.C. § 101", \
"125 Stat. 284", "2 Hous. J. Health L. & Pol'y 65".
- pin_cite: the specific page(s) or section cited, without "at" ("115", "115-16", "*3").
- case: case_name as written ("Roe v. Wade", "In re Deuel"); volume; reporter ("U.S.", "F.3d", "S. Ct."); \
page (the first page); court from the parenthetical ("Fed. Cir.", "D.D.C."); year.
- law: jurisdiction ("federal" for the U.S. Constitution, U.S.C., C.F.R., Stat., Pub. L. and Fed. Reg.; \
"state" for state constitutions, codes and regulations; otherwise "unknown"); reporter, the code or \
source ("U.S.C.", "C.F.R.", "Stat.", "Fed. Reg.", "Pub. L.", "Cal. Civ. Proc. Code", "U.S. Const."); \
title_number, the title number before the code ("35" in "35 U.S.C."; null when there is none, never an \
act's name); section, without the § sign ("101", "101-103", \
"1.56", "art. I, § 8, cl. 8" for a constitution); volume and page for Stat. and Fed. Reg. ("125", "284"); \
congress and law_number for a public law ("112" and "29" in "Pub. L. No. 112-29"); year.
- journal: author(s) as written; title of the article; journal as abbreviated in the citation; volume; \
page (the first page); year.
- secondary: source_type ("cjs", "amjur", "alr", "restatement", or "treatise" for treatises and other \
books); author; title (the book's title, the C.J.S./Am. Jur. topic such as "Contracts", or the \
Restatement subject such as "Torts"); volume; section; page; edition ("2d" in "Am. Jur. 2d", "Second" in \
"Restatement (Second)", "3d ed." for a book); series ("3d" in "A.L.R.3d"); year.
- short_form: category; type_hint (case, law, journal or secondary when the form itself shows it, \
otherwise unknown); refers_to_name, the name or short title used ("Roe", "Andrews"; null for Id. and \
Ibid.); volume and reporter when present ("410" and "U.S." in "410 U.S. at 115"); note_reference, the \
note number in "supra note 12".
- string_group: when several citations stand in one citation sentence separated by semicolons (a string \
citation), give them all the same number (1, 2, 3, ... per string in this chunk); otherwise null.

NOTES: footnote and endnote text appears inline, wrapped in <footnote n="..."> and <endnote n="..."> tags. \
The tags are not part of the document: never include them in matched_text or any field.

Return the citations in the order they appear, or an empty list if there are none."""

_RESOLVE_INSTRUCTIONS = """\
Use the bluebook-citation-extraction skill in `resolutions` mode. \
You resolve short-form legal citations. The input lists every citation found in a document, in document \
order, one per line:
[id] location | category | type | citation text
location is "main text" or the footnote/endnote holding the citation; "string N" marks citations in the \
same string citation.

For each citation whose category is short, id, ibid, supra or reference, return the id of the earlier \
FULL citation (category full) it refers to, following the Bluebook:
- "Id." and "ibid." refer to the immediately preceding cited authority: the citation just before, or the \
full citation that one refers to. If that preceding citation is part of a string citation citing several \
authorities, or (in footnotes) the preceding footnote cites more than one authority, id. has no valid \
antecedent: return null.
- A short-form case citation refers to the earlier full citation of the same case (same volume and \
reporter; the name may be shortened).
- A supra reference refers to the earlier full citation of the work by that author or short title; \
"supra note N" means that full citation appears in footnote N.
- A reference ("Roe at 115") refers to the earlier full citation of that case.
Return null when no earlier full citation fits; do not guess. Only the id of a full citation that comes \
earlier is a valid answer. Return one entry for every short, id, ibid, supra and reference citation."""


# --- chunking -------------------------------------------------------------------------

@dataclass(frozen=True)
class _Note:
    kind: str
    ordinal: int
    label: str
    start: int
    end: int


def _normalize_notes(note_spans: Sequence[Tuple[int, int]], notes: Sequence[Any] | None) -> List[_Note]:
    if notes:
        items = [_Note(n.kind, n.ordinal, n.label, n.start, n.end) for n in notes]
    else:
        items = [_Note("footnote", i, "", s, e) for i, (s, e) in enumerate(note_spans, start=1)]
    return sorted(items, key=lambda n: n.start)


def _note_name(note: _Note) -> str:
    return f"{note.kind} {note.label or note.ordinal}"


class _NoteIndex:
    def __init__(self, notes: Sequence[_Note]) -> None:
        self.notes = list(notes)
        self.starts = [n.start for n in self.notes]

    def containing(self, offset: int) -> _Note | None:
        """The note whose body contains `offset` (start inclusive, end exclusive)."""
        idx = bisect_right(self.starts, offset) - 1
        if idx >= 0 and self.notes[idx].start <= offset < self.notes[idx].end:
            return self.notes[idx]
        return None

    def strictly_inside(self, offset: int) -> bool:
        idx = bisect_right(self.starts, offset) - 1
        return idx >= 0 and self.notes[idx].start < offset < self.notes[idx].end

    def overlapping(self, start: int, end: int) -> List[_Note]:
        return [n for n in self.notes if n.start < end and start < n.end]

    def unit_start(self, offset: int) -> int:
        """Start of the main-text run or note body holding `offset`."""
        note = self.containing(offset)
        if note is not None:
            return note.start
        idx = bisect_right(self.starts, offset) - 1
        while idx >= 0 and self.notes[idx].end > offset:
            idx -= 1
        return self.notes[idx].end if idx >= 0 else 0


_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")
# A sentence end: a period after a word of 3+ lowercase letters, then a capital
# ("held. The") - never "U.S. 113", "v. Wade" or "Cir. 1995".
_SENTENCE_END_RE = re.compile(r"(?<=[a-z]{3}[.?!])\s+(?=[A-Z])")
_WHITESPACE_RE = re.compile(r"\s+")


def _cut_point(text: str, floor: int, limit: int, index: _NoteIndex) -> int:
    """The last good place in [floor, limit] to end a chunk.

    In order of preference: a paragraph break, a note boundary, a sentence
    end, any whitespace - outside note bodies, then (for a note longer than a
    chunk) inside one.
    """
    window = text[floor:limit]

    def last(pattern: re.Pattern[str], inside_notes: bool) -> int | None:
        cuts = [floor + m.end() for m in pattern.finditer(window)]
        cuts = [c for c in cuts if inside_notes or not index.strictly_inside(c)]
        return cuts[-1] if cuts else None

    note_boundaries = [b for n in index.notes for b in (n.start, n.end) if floor < b < limit]
    for candidate in (
        last(_PARAGRAPH_BREAK_RE, False),
        max(note_boundaries, default=None),
        last(_SENTENCE_END_RE, False),
        last(_WHITESPACE_RE, False),
        last(_SENTENCE_END_RE, True),
        last(_WHITESPACE_RE, True),
    ):
        if candidate is not None:
            return candidate
    return limit


def _chunk_document(text: str, index: _NoteIndex) -> List[Tuple[int, int]]:
    chunks: List[Tuple[int, int]] = []
    start = 0
    while start < len(text):
        if len(text) - start <= _CHUNK_CHARS:
            chunks.append((start, len(text)))
            break
        cut = _cut_point(text, start + _CHUNK_CHARS // 2, start + _CHUNK_CHARS, index)
        chunks.append((start, cut))
        start = cut
    return [(s, e) for s, e in chunks if text[s:e].strip()]


def _split_chunk(text: str, chunk: Tuple[int, int], index: _NoteIndex) -> List[Tuple[int, int]]:
    start, end = chunk
    if end - start < 200:
        return [chunk]
    middle = start + (end - start) // 2
    cut = _cut_point(text, start + (end - start) // 4, middle + (end - start) // 4, index)
    if not start < cut < end:
        cut = middle
    return [(start, cut), (cut, end)]


def _model_view(text: str, chunk: Tuple[int, int], index: _NoteIndex) -> str:
    """The chunk with its notes wrapped in <footnote n="..."> tags."""
    start, end = chunk
    pieces: List[str] = []
    position = start
    for note in index.overlapping(start, end):
        body_start, body_end = max(note.start, start), min(note.end, end)
        pieces.append(text[position:body_start])
        pieces.append(f'<{note.kind} n="{note.label or note.ordinal}">')
        pieces.append(text[body_start:body_end])
        pieces.append(f"</{note.kind}>")
        position = body_end
    pieces.append(text[position:end])
    return "".join(pieces)


# --- model calls ----------------------------------------------------------------------

def _add_usage(response: Any) -> None:
    usage = USAGE.get()
    if usage is None or response.usage is None:
        return
    details = getattr(response.usage, "input_tokens_details", None)
    output_details = getattr(response.usage, "output_tokens_details", None)
    for key, value in (
        ("requests", 1),
        ("input_tokens", response.usage.input_tokens),
        ("cached_tokens", getattr(details, "cached_tokens", 0) or 0),
        ("output_tokens", response.usage.output_tokens),
        ("reasoning_tokens", getattr(output_details, "reasoning_tokens", 0) or 0),
    ):
        usage[key] = usage.get(key, 0) + value


def _add_stat(key: str, count: int = 1) -> None:
    usage = USAGE.get()
    if usage is not None:
        usage[key] = usage.get(key, 0) + count


async def _structured_call(
    client: Any,
    config: _Config,
    semaphore: asyncio.Semaphore,
    instructions: str,
    input_text: str | List[Dict[str, Any]],
    schema_name: str,
    schema: Dict[str, Any],
    max_output_tokens: int,
) -> Dict[str, Any] | None:
    """One strict-JSON OpenAI request. Returns None when the answer hit max_output_tokens.

    
    Only reached for an OpenAI AI_MODEL (see extract_citations).
    """
    if config.provider == "openai":
        import openai

        request: Dict[str, Any] = {
            "model": config.model,
            "organization": config.organization,
            "project_id": config.project_id,
            "instructions": instructions,
            "input": input_text,
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
            "reasoning": {"effort": config.reasoning},
            "max_output_tokens": max_output_tokens,
            "store": False,
        }

        if config.skill_id:
            skill_ref = {
                "type": "skill_reference",
                "skill_id": config.skill_id,
            }

            request["tools"] = [{
                "type": "shell",
                "environment": {
                    "type": "container_auto",
                    "skills": [skill_ref],
                },
            }]
            logger.info("OpenAI skill id: %s", skill_ref["skill_id"])
        else:
            logger.info("OpenAI skill id not set")
        async with semaphore:
            try:
                response = await client.responses.create(**request)
            except openai.OpenAIError as exc:
                logger.error("OpenAI request failed: %s", exc)
                raise CitationExtractionError(f"{schema_name} request failed: {type(exc).__name__}") from exc
        _add_usage(response)

        if response.status == "incomplete":
            logger.warning("%s response incomplete: %s", schema_name, getattr(response.incomplete_details, "reason", None))
            reason = getattr(response.incomplete_details, "reason", None)
            if reason == "max_output_tokens":
                logger.warning("%s response hit max_output_tokens; returning None", schema_name)
                return None
            raise CitationExtractionError(f"{schema_name} response incomplete: {reason}")
        if response.status != "completed":
            raise CitationExtractionError(f"{schema_name} response status {response.status}")
        for item in response.output:
            for content in getattr(item, "content", None) or []:
                if getattr(content, "type", None) == "refusal":
                    raise CitationExtractionError(f"{schema_name} request refused")
        try:
            return json.loads(response.output_text)
        except (TypeError, ValueError) as exc:
            raise CitationExtractionError(f"{schema_name} response is not valid JSON") from exc


async def _extract_chunk(
    client: Any,
    config: _Config,
    semaphore: asyncio.Semaphore,
    text: str,
    index: _NoteIndex,
    chunk: Tuple[int, int],
    depth: int = 0,
) -> List[Tuple[Tuple[int, int], List[Dict[str, Any]]]]:
    """Pass 1 for one chunk: [(chunk, citations)] (several if the chunk was split)."""
    view = _model_view(text, chunk, index)
    data = await _structured_call(
        client, config, semaphore, _EXTRACT_INSTRUCTIONS, f"Document chunk:\n\n{view}",
        "citations", _CITATIONS_SCHEMA, _EXTRACT_MAX_OUTPUT_TOKENS,
    )
    if data is not None:
        return [(chunk, data.get("citations") or [])]
    parts = _split_chunk(text, chunk, index)
    if depth >= _MAX_SPLIT_DEPTH or len(parts) < 2:
        raise CitationExtractionError("citations response exceeded the output limit")
    logger.info("LLM extractor: chunk answer hit the output limit; splitting it in two")
    results = await asyncio.gather(
        *(_extract_chunk(client, config, semaphore, text, index, part, depth + 1) for part in parts)
    )
    return [item for result in results for item in result]


# --- grounding --------------------------------------------------------------------------

_QUOTE_CLASSES = {
    "'": "['’‘`]", "’": "['’‘`]", "‘": "['’‘`]", "`": "['’‘`]",
    '"': "[\"“”]", "“": "[\"“”]", "”": "[\"“”]",
    "-": "[-–—‑]", "–": "[-–—‑]", "—": "[-–—‑]", "‑": "[-–—‑]",
}


_DASHES = "-–—‑"
# A word broken across lines in a PDF: "Appropriat- ing".
_LINE_BREAK_HYPHEN = r"(?:[-‐\u00ad]\s+)?"
_LINE_BREAK_HYPHEN_RE = re.compile(r"(?<=\w)[-‐\u00ad]\s+(?=\w)")


def _flexible_pattern(needle: str) -> re.Pattern[str] | None:
    """Matches `needle` with any whitespace run, quote style or dash style, and
    with words broken by a line-break hyphen."""
    parts: List[str] = []
    for token in re.split(r"(\s+)", needle.strip()):
        if not token:
            continue
        if token.isspace():
            parts.append(r"\s+")
            continue
        for i, ch in enumerate(token):
            parts.append(_QUOTE_CLASSES.get(ch, re.escape(ch)))
            if ch in _DASHES:
                parts.append(r"\s*")
            elif ch.isalpha() and i + 1 < len(token) and token[i + 1].isalpha():
                parts.append(_LINE_BREAK_HYPHEN)
    if not parts:
        return None
    return re.compile("".join(parts))


def _normalize_for_compare(value: str) -> str:
    value = value.translate(str.maketrans({"’": "'", "‘": "'", "`": "'", "“": '"', "”": '"', "–": "-", "—": "-", "‑": "-"}))
    return _WHITESPACE_RE.sub(" ", value).strip().lower()


def _appears_in(value: str, haystack: str, ignore_spaces: bool = False) -> bool:
    """`value` is written in `haystack` (case/space/quote-insensitive, whole numbers).

    A number must match whole: "41" is not written in "410". A word broken by
    a line-break hyphen ("Appropriat- ing") counts as written either way.
    """
    needle = _normalize_for_compare(value)
    if ignore_spaces:
        needle = needle.replace(" ", "")
    if not needle:
        return False
    pattern = re.escape(needle)
    if needle[0].isdigit():
        pattern = r"(?<![0-9])" + pattern
    if needle[-1].isdigit():
        pattern += r"(?![0-9])"
    for variant in {haystack, _LINE_BREAK_HYPHEN_RE.sub("", haystack), _LINE_BREAK_HYPHEN_RE.sub("-", haystack)}:
        normalized = _normalize_for_compare(variant)
        if ignore_spaces:
            normalized = normalized.replace(" ", "")
        if re.search(pattern, normalized):
            return True
    return False


class _Claims:
    def __init__(self) -> None:
        self.spans: List[Tuple[int, int]] = []
        self.unplaced = 0  # citations the model reported that the text doesn't contain (for the normalized path)

    def free(self, start: int, end: int) -> bool:
        return all(end <= s or e <= start for s, e in self.spans)

    def add(self, span: Tuple[int, int]) -> None:
        self.spans.append(span)


class _ReservedClaims:
    """Claims that also treat some spans as taken without recording them: the note bodies inlined in the middle of
    a main-text block's window, which a main-text citation must not be located in."""

    def __init__(self, base: _Claims, reserved: Sequence[Tuple[int, int]]) -> None:
        self.base = base
        self.reserved = tuple(reserved)

    def free(self, start: int, end: int) -> bool:
        return self.base.free(start, end) and all(end <= s or e <= start for s, e in self.reserved)

    def add(self, span: Tuple[int, int]) -> None:
        self.base.add(span)


def _locate(text: str, needle: str, chunk: Tuple[int, int], cursor: int, claims: _Claims) -> Tuple[int, int] | None:
    """Where the chunk writes `needle`, preferring the first unclaimed match at/after `cursor`."""
    needle = needle.strip()
    if not needle:
        return None
    chunk_start, chunk_end = chunk
    for origin in (cursor, chunk_start):
        position = text.find(needle, origin, chunk_end)
        while position != -1:
            if claims.free(position, position + len(needle)):
                return position, position + len(needle)
            position = text.find(needle, position + 1, chunk_end)
    pattern = _flexible_pattern(needle)
    if pattern is None:
        return None
    for flags_pattern in (pattern, re.compile(pattern.pattern, re.IGNORECASE)):
        for origin in (cursor, chunk_start):
            for match in flags_pattern.finditer(text, origin, chunk_end):
                if claims.free(match.start(), match.end()):
                    return match.start(), match.end()
    return None


_FIELD_KEYS = {
    "case": ("core_text", "case_name", "volume", "reporter", "page", "pin_cite", "court", "year"),
    "law": ("core_text", "reporter", "title_number", "section", "volume", "page", "congress", "law_number", "pin_cite", "year"),
    "journal": ("core_text", "author", "title", "journal", "volume", "page", "pin_cite", "year"),
    "secondary": ("author", "title", "volume", "section", "page", "edition", "series", "pin_cite", "year"),
    "short_form": ("refers_to_name", "volume", "reporter", "pin_cite", "note_reference"),
}
_YEAR_RE = re.compile(r"^\d{4}$")
# Fields that may be written before the citation's own text.
_NAME_KEYS = {"case_name", "author", "title"}
_NAME_KINDS = {"case", "journal", "secondary"}


def _name_window(text: str, span: Tuple[int, int], index: _NoteIndex) -> str:
    """The citation plus up to _NAME_WINDOW characters before it: within its
    own main-text run, or for a citation in a note, reaching back past the
    note's start into the text the note is attached to."""
    if index.containing(span[0]) is not None:
        start = span[0] - _NAME_WINDOW
    else:
        start = max(index.unit_start(span[0]), span[0] - _NAME_WINDOW)
    return text[max(0, start):span[1]]


def _number_before(reporter: str | None, matched: str) -> str | None:
    """The number printed right before `reporter` ("37" in "37 C.F.R. § 5.10")."""
    if not reporter:
        return None
    pattern = _flexible_pattern(reporter)
    if pattern is None:
        return None
    match = re.search(r"(?<![\d.])(\d+)\s+" + pattern.pattern, matched, re.IGNORECASE)
    return match.group(1) if match else None


def _ground_fields(
    kind: str,
    item: Dict[str, Any],
    text: str,
    span: Tuple[int, int],
    index: _NoteIndex,
) -> Dict[str, Any]:
    matched = text[span[0]:span[1]]
    fields: Dict[str, Any] = {}
    for key in _FIELD_KEYS[kind]:
        value = clean_str(item.get(key))
        if value is None:
            fields[key] = None
            continue
        if key == "section":
            value = clean_str(re.sub(r"^§+\s*", "", value)) or value
        haystack = matched
        if key in _NAME_KEYS and kind in _NAME_KINDS:
            haystack = _name_window(text, span, index)
        # An abbreviation's spacing ("F.3d" / "F. 3d") is not a different value.
        ok = _appears_in(value, haystack, ignore_spaces=key in ("reporter", "journal")) and (
            key != "year" or _YEAR_RE.match(value) is not None
        )
        if not ok:
            _add_stat(f"ungrounded_field:{key}")
            value = None
        fields[key] = value
    for key in ("jurisdiction", "source_type", "type_hint"):
        if key in item:
            fields[key] = item[key]
    if kind == "law":
        if fields.get("title_number") and not any(ch.isdigit() for ch in fields["title_number"]):
            fields["title_number"] = None  # an act's name, not a code title
        if not fields.get("title_number") and not fields.get("volume"):
            # A U.S.C./C.F.R. title or a Stat./Fed. Reg. volume the model left
            # out, read from the document itself.
            number = _number_before(fields.get("reporter"), matched)
            if number is not None:
                is_volume = canonical_law_reporter(fields.get("reporter")) in ("Stat.", "Fed. Reg.")
                fields["volume" if is_volume else "title_number"] = number
    return fields


def _ground_chunk(
    text: str,
    chunk: Tuple[int, int],
    items: List[Dict[str, Any]],
    index: _NoteIndex,
    claims: _Claims,
    reserved: Sequence[Tuple[int, int]] = (),
) -> List[Tuple[GroundedCitation, int | None]]:
    """GroundedCitations for one chunk's answer, with each one's chunk-local string group."""
    grounded: List[Tuple[GroundedCitation, int | None]] = []
    cursor = chunk[0]
    counter = claims
    if reserved:
        claims = _ReservedClaims(claims, reserved)  # type: ignore[assignment]
    for item in items:
        kind = item.get("kind")
        if kind not in _FIELD_KEYS:
            continue
        matched_text = item.get("matched_text") or ""
        span = _locate(text, matched_text, chunk, cursor, claims)
        core_text = clean_str(item.get("core_text"))
        if span is None and core_text and core_text in matched_text:
            # A matched_text stitched together from separate places (a name in
            # the main text, the cite in its footnote): keep what the document
            # writes contiguously from the citation's core on.
            span = _locate(text, matched_text[matched_text.index(core_text):], chunk, cursor, claims)
            if span is not None:
                _add_stat("citations_located_by_core_text")
        if span is None:
            _add_stat("ungrounded_citations")
            counter.unplaced += 1
            continue
        if kind != "short_form" and not any(ch.isdigit() for ch in text[span[0]:span[1]]):
            # A full citation always has a number (volume, page, section,
            # docket or year); this is a name mentioned in prose.
            _add_stat("dropped_citations_without_numbers")
            continue
        claims.add(span)
        cursor = span[1]

        fields = _ground_fields(kind, item, text, span, index)
        if kind == "short_form":
            category = item.get("category") if item.get("category") in SHORT_CATEGORIES else "short"
            type_hint = item.get("type_hint") if item.get("type_hint") in FULL_KINDS else "unknown"
        else:
            category, type_hint = "full", kind
        if category and type_hint:
            grounded.append((
                GroundedCitation(
                    kind, category, type_hint, text[span[0]:span[1]], span, fields,
                    source_id=item.get("source_id"), source_page=item.get("source_page"), validation=item.get("_validation"),
                ),
                item.get("string_group"),
            ))
    return grounded


# --- records ------------------------------------------------------------------------------

_FEDERAL_REPORTERS = {
    "usc": "U.S.C.", "usca": "U.S.C.", "uscs": "U.S.C.",
    "cfr": "C.F.R.", "stat": "Stat.", "fedreg": "Fed. Reg.",
    "publ": "Pub. L.", "publno": "Pub. L. No.",
}
_JOURNAL_NAMES: Dict[str, str] | None = None


def canonical_law_reporter(reporter: str | None) -> str | None:
    """GovInfo's spelling of a federal reporter ("U. S. C." -> "U.S.C."); others unchanged."""
    if not reporter:
        return reporter
    return _FEDERAL_REPORTERS.get(re.sub(r"[\s.]", "", reporter).lower(), reporter)


def journal_names(abbreviation: str | None) -> List[str]:
    """Names to search a journal by: its reporters-db name if listed, else the abbreviation."""
    global _JOURNAL_NAMES
    if not abbreviation:
        return []
    if _JOURNAL_NAMES is None:
        names: Dict[str, str] = {}
        for key, editions in reporters_db.JOURNALS.items():
            name = editions[0].get("name") if editions else None
            if not name:
                continue
            for variant in [key, *(editions[0].get("variations") or [])]:
                names.setdefault(_normalize_for_compare(variant), name)
        _JOURNAL_NAMES = names
    name = _JOURNAL_NAMES.get(_normalize_for_compare(abbreviation))
    return [name] if name else [abbreviation]


def _record(citation: GroundedCitation) -> CitationRecord:
    f = citation.fields
    if citation.kind == "case":
        return CitationRecord("case", citation.matched_text, {
            "case_name": f.get("case_name"), "volume": f.get("volume"), "reporter": f.get("reporter"),
            "page": f.get("page"), "year": f.get("year"), "court": f.get("court"),
        })
    if citation.kind == "law":
        reporter = canonical_law_reporter(f.get("reporter"))
        jurisdiction = f.get("jurisdiction") or "unknown"
        if reporter in _FEDERAL_REPORTERS.values():
            jurisdiction = "federal"
        return CitationRecord("law", citation.matched_text, {
            "jurisdiction": jurisdiction, "reporter": reporter, "title": f.get("title_number"),
            "volume": f.get("volume"), "chapter": None, "code": None, "section": f.get("section"),
            "page": f.get("page"), "congress": f.get("congress"), "lawnum": f.get("law_number"),
            "year": f.get("year"),
        })
    if citation.kind == "journal":
        return CitationRecord("journal", citation.matched_text, {
            "author": f.get("author"), "title": f.get("title"), "journal": f.get("journal"),
            "journal_names": journal_names(f.get("journal")), "volume": f.get("volume"),
            "page": f.get("page"), "year": f.get("year"),
        })
    return CitationRecord("secondary", citation.matched_text, {
        "source_type": f.get("source_type"), "volume": f.get("volume"), "title": f.get("title"),
        "section": f.get("section"), "page": f.get("page"), "year": f.get("year"),
        "edition": f.get("edition"), "series": f.get("series"), "author": f.get("author"),
    })


# --- pass 2: short-form resolution -------------------------------------------------------

def _resolution_listing(citations: Sequence[GroundedCitation], index: _NoteIndex) -> str:
    lines = []
    for c in citations:
        note = index.containing(c.span[0])
        location = _note_name(note) if note else "main text"
        line = f"[{c.index}] {location} | {c.category} | {c.type_hint} | {c.matched_text}"
        if c.string_group is not None:
            line += f" | string {c.string_group}"
        lines.append(_WHITESPACE_RE.sub(" ", line))
    return "\n".join(lines)


def _same_volume_reporter(short: GroundedCitation, full: GroundedCitation) -> bool:
    volume, reporter = short.fields.get("volume"), short.fields.get("reporter")
    return (
        _normalize_for_compare(volume or "") == _normalize_for_compare(full.fields.get("volume") or "")
        and re.sub(r"[\s.]", "", reporter or "").lower() == re.sub(r"[\s.]", "", full.fields.get("reporter") or "").lower()
    )


def _compatible(short: GroundedCitation, full: GroundedCitation) -> bool:
    """A short form giving a volume and reporter ("447 U.S. at 309", "66 Fed.
    Reg. at 1093") must point at a citation with that volume and reporter."""
    if short.category == "short" and short.type_hint == "case" and full.kind != "case":
        return False
    if short.fields.get("volume") and short.fields.get("reporter"):
        return _same_volume_reporter(short, full)
    return True


def _latest_same_source(short: GroundedCitation, citations: Sequence[GroundedCitation]) -> int | None:
    """The latest earlier full citation with the short form's volume and
    reporter ("447 U.S. at 309" -> "447 U.S. 303"), if the short form has both."""
    if not (short.fields.get("volume") and short.fields.get("reporter")):
        return None
    for candidate in reversed(citations[:short.index]):
        if candidate.is_full and _same_volume_reporter(short, candidate):
            return candidate.index
    return None


async def _resolve_short_forms(
    client: Any,
    config: _Config,
    semaphore: asyncio.Semaphore,
    citations: List[GroundedCitation],
    index: _NoteIndex,
) -> None:
    shorts = [c for c in citations if not c.is_full]
    if not shorts:
        return
    data = await _structured_call(
        client, config, semaphore, _RESOLVE_INSTRUCTIONS, _resolution_listing(citations, index),
        "resolutions", _RESOLUTIONS_SCHEMA, _RESOLVE_MAX_OUTPUT_TOKENS,
    )
    if data is None:
        raise CitationExtractionError("resolutions response exceeded the output limit")
    answers = {
        r.get("id"): r.get("refers_to") for r in data.get("resolutions") or [] if isinstance(r.get("id"), int)
    }
    for short in shorts:
        target = answers.get(short.index)
        # An answer naming an earlier short form means what that one refers
        # to (shorts are resolved in document order).
        if isinstance(target, int) and 0 <= target < short.index and not citations[target].is_full:
            target = citations[target].refers_to
        if target is not None:
            if (
                isinstance(target, int)
                and 0 <= target < short.index
                and citations[target].is_full
                and _compatible(short, citations[target])
            ):
                short.refers_to = target
                continue
            _add_stat("rejected_resolutions")
        # A short form giving a volume and reporter names its source.
        fallback = _latest_same_source(short, citations)
        if fallback is not None:
            short.refers_to = fallback
            _add_stat("resolutions_by_volume_reporter")


def _flag_ids_after_strings(citations: Sequence[GroundedCitation]) -> None:
    """Bluebook Rule 4.1: an Id. can't refer back to a string citation.

    An Id. is flagged when the citation just before it lies in a string of
    >= 2 non-Id. authorities that the Id. itself is outside of; an Id. right
    after a flagged Id. is flagged too (see _flag_ids_after_string_citations
    in citations_compiler for the rules extractor's version).
    """
    authorities: Dict[int, int] = {}
    for c in citations:
        if c.string_group is not None and c.category not in ("id", "ibid"):
            authorities[c.string_group] = authorities.get(c.string_group, 0) + 1
    for i, c in enumerate(citations):
        if c.category not in ("id", "ibid") or i == 0:
            continue
        previous = citations[i - 1]
        if previous.id_error_group is not None:
            c.id_error_group = previous.id_error_group
        elif (
            previous.string_group is not None
            and c.string_group != previous.string_group
            and authorities.get(previous.string_group, 0) >= 2
        ):
            c.id_error_group = previous.string_group
        if c.id_error_group is not None:
            c.refers_to = None


# --- entry point ------------------------------------------------------------------------------

def _grounded_citations(
    text: str,
    answers: Sequence[Tuple[Any, ...]],
    index: _NoteIndex,
    unplaced: List[int] | None = None,
) -> List[GroundedCitation]:
    """Pass 1's answers, grounded, in document order.

    An answer is (chunk, citations); the normalized-input path (svc/llm_normalized.py) adds the text ranges that
    are off limits inside the chunk (other blocks' notes) and the key its string-group numbers are local to.
    """
    claims = _Claims()
    pending: List[Tuple[GroundedCitation, Any, int | None]] = []
    for answer in answers:
        chunk, items = answer[0], answer[1]
        reserved = answer[2] if len(answer) > 2 else ()
        key = answer[3] if len(answer) > 3 else chunk
        for citation, local_group in _ground_chunk(text, chunk, items, index, claims, reserved):
            pending.append((citation, key, local_group))
    if unplaced is not None:
        unplaced.append(claims.unplaced)
    pending.sort(key=lambda p: p[0].span)

    # Chunk-local string numbers -> document-wide ones; a "string" with a
    # single member is not a string citation.
    members: Dict[Tuple[Any, int], int] = {}
    for _, chunk, local_group in pending:
        if local_group is not None:
            members[(chunk, local_group)] = members.get((chunk, local_group), 0) + 1
    numbers: Dict[Tuple[Any, int], int] = {}
    citations: List[GroundedCitation] = []
    for citation, chunk, local_group in pending:
        key = (chunk, local_group)
        if local_group is not None and members[key] >= 2:
            citation.string_group = numbers.setdefault(key, len(numbers))
        citation.index = len(citations)
        if citation.is_full:
            citation.record = _record(citation)
        citations.append(citation)
    return citations


async def extract_citations(
    text: str,
    note_spans: Sequence[Tuple[int, int]] = (),
    notes: Sequence[Any] | None = None,
    normalized: Any | None = None,
) -> List[GroundedCitation]:
    """Every citation in `text`, grounded and resolved, in document order.

    `notes` (NoteSpans) label the footnotes/endnotes for the model; without
    them the `note_spans` ranges are numbered in order.

    `normalized` (a svc.normalization NormalizedDocument of the same upload) makes the model read the document's
    tagged text or PDF instead of chunks of `text`, and validates its answers against the document's blocks
    (svc/llm_normalized.py); `text` is still what the citations' spans index into. Pass 2 is unchanged.
    """
    config = _config()
    index = _NoteIndex(_normalize_notes(note_spans, notes))
    chunks = [] if normalized is not None else _chunk_document(text, index)
    if normalized is None and not chunks:
        return []
    started = time.monotonic()
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    if config.provider == "openai":
        from openai import AsyncOpenAI

        async with AsyncOpenAI(api_key=config.api_key, organization=config.organization, project=config.project_id, timeout=_REQUEST_TIMEOUT, max_retries=1) as client:
            if normalized is not None:
                from svc.llm_normalized import extract_pass_one

                answers = await extract_pass_one(client, config, semaphore, text, notes, normalized)
                unplaced: List[int] = []
                citations = _grounded_citations(text, answers, index, unplaced)
                if unplaced and unplaced[0]:
                    # Validated against the document but absent from its extracted text (a text box, say): it can't
                    # be highlighted or attributed, so it is left out - and the user is told.
                    normalized.telemetry["grounding_unplaced"] = unplaced[0]
                    normalized.warnings.append(f"citations_unplaced:{unplaced[0]}")
            else:
                chunk_answers = await asyncio.gather(
                    *(_extract_chunk(client, config, semaphore, text, index, chunk) for chunk in chunks)
                )
                citations = _grounded_citations(text, [pair for answer in chunk_answers for pair in answer], index)
            await _resolve_short_forms(client, config, semaphore, citations, index)
    else:
        raise CitationExtractionError(
            f"{config.model!r} is not supported: only OpenAI (\"gpt...\") models are implemented"
        )
    _flag_ids_after_strings(citations)

    logger.info(
        "LLM extractor: %d citation(s) (%d full) from %s in %.1fs",
        len(citations),
        sum(1 for c in citations if c.is_full),
        "the normalized document" if normalized is not None else f"{len(chunks)} chunk(s)",
        time.monotonic() - started,
    )
    return citations


__all__ = [
    "CitationExtractionError",
    "GroundedCitation",
    "USAGE",
    "canonical_law_reporter",
    "extract_citations",
    "journal_names",
]
