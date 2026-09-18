# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterator, List, Sequence, Set, Tuple

from eyecite import get_citations, resolve_citations
from eyecite.models import (
    CaseCitation,
    CitationBase,
    CitationToken,
    Edition,
    FullCaseCitation,
    FullCitation,
    FullJournalCitation,
    FullLawCitation,
    IdCitation,
    ReferenceCitation,
    Reporter,
    ShortCaseCitation,
    SupraCitation,
    Token,
)
from eyecite.regexes import nonalphanum_boundaries_re
from eyecite.tokenizers import TokenExtractor, Tokenizer, default_tokenizer

from svc.secondary_citation_handler import (
    SecondaryCitation,
    SecondaryCitationDetector,
    SecondaryCitationResolver,
)
from svc.string_citation_handler import (
    CitationSegment,
    StringCitationDetector,
    StringCitationSplitter,
    create_standalone_segment,
)
from utils.cleaner import clean_str
from utils.logger import get_logger
from utils.resource_resolver import get_journal_author_title, resolve_case_year
from utils.span_finder import get_span
from verifiers.case_verifier import get_case_name, verify_case_citation
from verifiers.federal_law_verifier import (
    classify_full_law_jurisdiction,
    verify_federal_law_citation,
)
from verifiers.journal_verifier import verify_journal_citation
from verifiers.secondary_sources_verifier import verify_secondary_citation
from verifiers.state_law_verifier import verify_state_law_citation

logger = get_logger()

_AdjustedSpans = Dict[int, Tuple[int, int]]

# Journal verification hits OpenAlex/Semantic Scholar sequentially today via
# time.sleep-based rate limiting inside the verifier; serialize the async
# tasks with this semaphore so moving to asyncio.to_thread doesn't fan out
# concurrent requests against Semantic Scholar's ~1 RPS limit.
_journal_verification_semaphore = asyncio.Semaphore(1)

# --- async helpers -------------------------------------------------

async def _verify_state_async(
    resource_key: str,
    primary_full: Any,
    normalized_key: str | None,
    resource_dict: Dict[str, Any],
    fallback_citation: str | None,
) -> Tuple[str, str, str | None, Dict[str, Any] | None]:
    """Run the state law verifier off the main event loop."""
    try:
        status, substatus, details = await asyncio.to_thread(
            verify_state_law_citation,
            primary_full,
            normalized_key,
            resource_dict,
            fallback_citation=fallback_citation,
        )
    except Exception as exc:  # pragma: no cover - defensive safeguard
        logger.exception("State law verification task failed for %s: %s", resource_key, exc)
        status, substatus, details = "error", "state_law_async_failed", None
    return resource_key, status, substatus, details


async def _verify_federal_async(
    resource_key: str,
    primary_full: Any,
    normalized_key: str | None,
    resource_dict: Dict[str, Any],
    fallback_citation: str | None,
) -> Tuple[str, str, str | None, Dict[str, Any] | None]:
    """Run the federal law verifier off the main event loop."""
    try:
        status, substatus, details = await asyncio.to_thread(
            verify_federal_law_citation,
            primary_full,
            normalized_key,
            resource_dict,
            fallback_citation=fallback_citation,
        )
    except Exception as exc:  # pragma: no cover - defensive safeguard
        logger.exception("Federal law verification task failed for %s: %s", resource_key, exc)
        status, substatus, details = "error", "federal_law_async_failed", None
    return resource_key, status, substatus, details


async def _verify_journal_async(
    resource_key: str,
    primary_full: Any,
    normalized_key: str | None,
    resource_dict: Dict[str, Any],
) -> Tuple[str, str, str | None, Dict[str, Any] | None]:
    """Run the journal verifier off the main event loop."""
    try:
        async with _journal_verification_semaphore:
            status, substatus, details = await asyncio.to_thread(
                verify_journal_citation,
                primary_full,
                normalized_key,
                resource_dict,
            )
    except Exception as exc:  # pragma: no cover - defensive safeguard
        logger.exception("Journal verification task failed for %s: %s", resource_key, exc)
        status, substatus, details = "error", "journal_verification_async_failed", None
    return resource_key, status, substatus, details


async def _verify_secondary_async(
    resource_key: str,
    cite: Any,
    normalized: str | None,
    resource_dict: Dict[str, Any],
) -> Tuple[str, str, str | None, Dict[str, Any] | None]:
    """Run the secondary-source (Library of Congress) verifier off the main event loop."""
    try:
        status, substatus, details = await asyncio.to_thread(
            verify_secondary_citation, cite, normalized, resource_dict
        )
    except Exception as exc:  # pragma: no cover - defensive safeguard
        logger.exception("Secondary source verification task failed for %s: %s", resource_key, exc)
        status, substatus, details = "error", "secondary_verification_async_failed", None
    logger.info(
        "Added new secondary citation to database: %s (status: %s)",
        cite.matched_text[:50],
        status,
    )
    return resource_key, status, substatus, details

# --- helper functions ------------------------------------------

def _ctype(obj: Any) -> str:
    return type(obj).__name__

def _normalized_key(citation_obj) -> str:
    """Generate a normalized key for a citation object."""

    if isinstance(citation_obj, FullCaseCitation) or isinstance(citation_obj, FullLawCitation):
        return citation_obj.corrected_citation()
    elif isinstance(citation_obj, FullJournalCitation):
        volume = citation_obj.groups.get("volume", "")
        reporter = citation_obj.groups.get("reporter", "")
        page = citation_obj.groups.get("page", "")
        year = citation_obj.year
        if year:
            return f"{volume} {reporter} {page} ({year})"
        return f"{volume} {reporter} {page}"
    else:
        return citation_obj.matched_text()

def _get_citation_type(citation_obj) -> str:
    """Determine the type of citation."""

    if isinstance(citation_obj, (FullCaseCitation, CaseCitation, ShortCaseCitation)):
        return "case"
    elif isinstance(citation_obj, FullLawCitation):
        return "law"
    elif isinstance(citation_obj, FullJournalCitation):
        return "journal"
    else:
        return "unknown"

def _get_pin_cite(obj) -> str | None:
    metadata = getattr(obj, "metadata", None)
    if metadata is None:
        return None
    return clean_str(getattr(metadata, "pin_cite", None))


def _citation_category(obj) -> str:
    if isinstance(obj, FullCitation):
        return "full"
    if isinstance(obj, ShortCaseCitation):
        return "short"
    if isinstance(obj, SupraCitation):
        return "supra"
    if isinstance(obj, ReferenceCitation):
        return "reference"
    if isinstance(obj, IdCitation):
        return "id"
    return _ctype(obj)

def _get_index(obj) -> int | None:
    index = getattr(obj, "index", None)
    if index is not None:
        return int(index)
    return None

def _resource_identifier(resource: Any) -> str:
    if isinstance(resource, ResourceKey):
        parts = [resource.kind, *resource.id_tuple]
        return "::".join(part for part in parts if part)
    return clean_str(str(resource)) or _ctype(resource)

def _get_citation(obj) -> str | None:
    c = obj.token.data if hasattr(obj, "token") and hasattr(obj.token, "data") else None
    if c is not None:
        return clean_str(c)
    c = obj.data if hasattr(obj, "data") else None
    if c is not None:
        return clean_str(c)
    return None

# --- Resource binding for resolver ------------------------------------------
@dataclass(frozen=True)
class ResourceKey:
    kind: str                # "case" | "law" | "other"
    id_tuple: Tuple[str, ...]  # stable tuple to represent the work

def _bind_full_citation(full_cite) -> ResourceKey | None:
    """Return a stable key Eyecite will use as the 'resource' for short forms."""
    t = _ctype(full_cite)
    if t == "FullCaseCitation":
        name = clean_str(get_case_name(full_cite)) or ""
        reporter = (clean_str(full_cite.groups.get("reporter", None)) or "")
        vol = clean_str(full_cite.groups.get("volume", None)) or ""
        page = clean_str(full_cite.groups.get("page", None)) or ""
        year = clean_str(full_cite.year) or clean_str(full_cite.metadata.year) or ""
        return ResourceKey("case", (name, reporter, vol, page, year))
    elif t == "FullLawCitation":
        title = clean_str(full_cite.groups.get("title", None) or full_cite.groups.get("volume", None) or
                          full_cite.groups.get("chapter", None)) or  ""
        code = clean_str(full_cite.groups.get("reporter", None) or full_cite.groups.get("code", None)) or ""
        section = clean_str(full_cite.groups.get("section", None) or full_cite.groups.get("page", None)) or ""
        year = clean_str(getattr(full_cite, "year", None)) or ""
        return ResourceKey("law", (title, code, section, year))
    elif t == "FullJournalCitation":
        title = ""
        author = ""
        journal_info = get_journal_author_title(full_cite)
        if journal_info is not None:
            title = journal_info.get("title", "") or ""
            author = journal_info.get("author", "") or ""
        journal = (clean_str(full_cite.groups.get("reporter", None)) or "")
        volume = clean_str(full_cite.groups.get("volume", None)) or ""
        page = clean_str(full_cite.groups.get("page", None)) or ""
        year = clean_str(full_cite.year) or ""
        return ResourceKey("journal", (author, title, volume, journal, page, year))
    else:
        logger.info(f"Unsupported full citation type for resource binding: {full_cite}")


# --- Exact-span cleaning helpers -------------------------------------------

_CLEAN_RUN_RE = re.compile(r"[​\s]+|__+")


def _clean_with_offset_map(text: str) -> Tuple[str, List[int]]:
    """Apply eyecite's "all_whitespace" + "underscores" cleaners in one pass
    while recording, for each output character, the index it came from in
    the original `text`.

    Equivalent to clean_text(text, ["all_whitespace", "underscores"]):
      - all_whitespace: collapse a run of zero-width-space/whitespace
        characters to a single " ".
      - underscores: delete a run of 2+ underscores.
    The two character classes are disjoint (whitespace is never "_") and the
    underscore step only deletes (never re-collapses whitespace), so a single
    left-to-right pass over the union pattern is equivalent to running the
    two cleaners in sequence.

    Returns (cleaned_text, offsets) where offsets[i] is the original-text
    index cleaned_text[i] came from, plus a trailing sentinel
    offsets[len(cleaned_text)] == len(text) so an end-exclusive span can be
    mapped without a bounds special case (see _map_cleaned_span).
    """
    out_chunks: List[str] = []
    offsets: List[int] = []
    pos = 0
    for match in _CLEAN_RUN_RE.finditer(text):
        if match.start() > pos:
            out_chunks.append(text[pos:match.start()])
            offsets.extend(range(pos, match.start()))
        run = match.group(0)
        if run[0] != "_":
            out_chunks.append(" ")
            offsets.append(match.start())
        pos = match.end()
    if pos < len(text):
        out_chunks.append(text[pos:])
        offsets.extend(range(pos, len(text)))
    offsets.append(len(text))
    return "".join(out_chunks), offsets


def _map_cleaned_span(span: Tuple[int, int], offsets: List[int]) -> Tuple[int, int] | None:
    """Map a (start, end) span in _clean_with_offset_map's cleaned text back
    to the original text, using the offsets it returned."""
    start, end = span
    if end <= start or start < 0 or end > len(offsets) - 1:
        return None
    return (offsets[start], offsets[end - 1] + 1)


# --- Unlisted-journal fallback tokenizer -----------------------------------
#
# eyecite only recognizes journals listed in reporters_db.JOURNALS (~800
# entries) while Bluebook T13 lists thousands, so any other law review (e.g.
# "2 Hous. J. Health L. & Pol'y 65") produced no citation at all. This
# fallback matches a Bluebook-style journal abbreviation -- capitalized
# abbreviated words including at least one T13 journal marker word --
# between a volume and a page. Tuning knobs: _JOURNAL_WORD, _JOURNAL_MARKER,
# and the volume/page digit widths.

_JOURNAL_WORD = r"(?:[A-Z][A-Za-z]*(?:['’][a-z]+)?\.?|&|[A-Z]\.(?:[A-Z]\.)+)"
_JOURNAL_MARKER = r"(?:L\.J\.|L\.Q\.|L\.|J\.|Rev\.|Q\.|Pol['’]y|F\.)"
_UNLISTED_JOURNAL_RE = nonalphanum_boundaries_re(
    rf"(?P<volume>\d{{1,4}}) "
    rf"(?P<reporter>(?:{_JOURNAL_WORD} )*?{_JOURNAL_MARKER}(?: {_JOURNAL_WORD})*?) "
    rf"(?P<page>\d{{1,5}})"
)
# A real abbreviation has a multi-letter word ending in a period ("Hous.",
# "Rev."), an apostrophe contraction ("Int'l", "Pol'y"), compact initials
# ("L.J."), or "&" -- a bare initial plus a name ("5 J. Smith 12") has none.
_JOURNAL_ABBREVIATION_EVIDENCE_RE = re.compile(r"[A-Za-z]{2,}\.|['’]|[A-Z]\.[A-Z]\.|&")


def _unlisted_journal_token(m: re.Match, extra: Dict[str, Any], offset: int = 0) -> Token:
    """Build a CitationToken for an unlisted-journal match.

    source="journals" makes eyecite emit a FullJournalCitation (pin cite, year,
    and document back-reference included). The per-match Reporter carries the
    matched abbreviation as its name because journal_verifier sends
    all_editions[0].reporter.name to its OpenAlex/Semantic Scholar source
    lookups -- a placeholder name would poison those queries.
    """
    abbreviation = m["reporter"]
    reporter = Reporter(
        short_name=abbreviation,
        name=abbreviation,
        cite_type="journal",
        source="journals",
    )
    edition = Edition(short_name=abbreviation, reporter=reporter, start=None, end=None)
    return CitationToken.from_match(
        m,
        {"exact_editions": [edition], "variation_editions": [], "short": False},
        offset,
    )


_UNLISTED_JOURNAL_EXTRACTOR = TokenExtractor(_UNLISTED_JOURNAL_RE, _unlisted_journal_token)


class _JournalFallbackTokenizer(Tokenizer):
    """eyecite's default tokenizer plus the unlisted-journal fallback.

    Wraps default_tokenizer (rather than subclassing AhocorasickTokenizer) so
    eyecite's extractor automaton isn't built a second time. Fallback matches
    overlapping any known citation token are dropped, so reporters_db entries
    (e.g. "F. Supp.", "L. Ed.") always win, as are matches whose reporter
    shows no abbreviation evidence (see _JOURNAL_ABBREVIATION_EVIDENCE_RE).
    """

    def extract_tokens(self, text: str) -> Iterator[Token]:
        known = list(default_tokenizer.extract_tokens(text))
        yield from known
        known_spans = [(t.start, t.end) for t in known if isinstance(t, CitationToken)]
        for match in _UNLISTED_JOURNAL_EXTRACTOR.get_matches(text):
            if not _JOURNAL_ABBREVIATION_EVIDENCE_RE.search(match["reporter"]):
                continue
            token = _UNLISTED_JOURNAL_EXTRACTOR.get_token(match)
            if not any(token.start < end and start < token.end for start, end in known_spans):
                yield token


_JOURNAL_FALLBACK_TOKENIZER = _JournalFallbackTokenizer(extractors=[])


def _repair_case_years(citations: List[CitationBase]) -> None:
    """Replace each full case citation's eyecite year with resolve_case_year().

    eyecite can assign a year taken from a *different* citation (e.g. "43 Cal.
    4th 757 (2008); ... 134 F.4th 1205 (Fed. Cir. 2025)" gives the first cite
    2025), which then flows into the resource key and the verifier's year
    check. Must run before resolution (_bind_full_citation reads the year).
    """
    for cite in citations:
        if not isinstance(cite, FullCaseCitation):
            continue
        year = resolve_case_year(cite)
        if year != cite.metadata.year:
            logger.info(
                "Corrected year for %s: %s -> %s",
                cite.matched_text(),
                cite.metadata.year,
                year,
            )
        cite.metadata.year = year
        cite.year = int(year) if year else None


# --- String citation processing helpers -----------------------------------

def _process_citation_segment(
    segment: CitationSegment,
    adjusted_spans: _AdjustedSpans,  # New parameter to collect adjusted spans
) -> Tuple[List[CitationBase], Dict[int, CitationSegment]]:
    """Run eyecite detection (not resolution) on a single citation segment.

    Resolution happens once, globally, over every segment's citations in
    document order (see _resolve_all_citations) -- eyecite drops any
    short/id/supra citation it cannot resolve, so resolving per segment
    silently lost citations whose full antecedent lived in a different
    segment.

    Args:
        segment: The citation segment to process.
        adjusted_spans: Dict to store adjusted span information (modified in place).

    Returns:
        Tuple of (citations list, segment_metadata dict).
    """
    segment_text = segment.text
    cleaned, offsets = _clean_with_offset_map(segment_text)

    try:
        citations = get_citations(cleaned, tokenizer=_JOURNAL_FALLBACK_TOKENIZER)
    except Exception as exc:
        logger.error("eyecite.get_citations failed for segment: %s", exc)
        return [], {}
    _repair_case_years(citations)

    # Adjust spans to original document coordinates
    segment_metadata: Dict[int, CitationSegment] = {}

    for cite in citations:
        cite_span = get_span(cite)

        if cite_span:
            mapped = _map_cleaned_span(cite_span, offsets)
            if mapped is not None:
                # Store adjusted span separately (don't modify eyecite
                # object). Keyed by object identity rather than eyecite's
                # per-call token index: each segment's get_citations() call
                # restarts indexing from 0, so index-keyed storage collides
                # once more than one segment is processed.
                adjusted_spans[id(cite)] = (
                    segment.original_span[0] + mapped[0],
                    segment.original_span[0] + mapped[1],
                )

        # Track segment metadata for this citation (same identity keying).
        segment_metadata[id(cite)] = segment

    return citations, segment_metadata


def _resolve_all_citations(citations: List[CitationBase]) -> Dict[Any, Any]:
    """Resolve a flat, document-ordered list of citations in a single pass.

    Must be called once over every segment's citations together (rather than
    per segment) since eyecite's resolve_citations resolves short/id/supra
    forms only against full citations it has already seen in the same call.

    Args:
        citations: Citations in document order.

    Returns:
        Resolutions dict (resource -> list of resolved citations).
    """
    try:
        return resolve_citations(
            citations,
            resolve_full_citation=_bind_full_citation,
        )
    except Exception as exc:
        logger.error("eyecite.resolve_citations failed: %s", exc)
        return {
            f"raw:{idx}": [citation]
            for idx, citation in enumerate(citations)
        }


def _get_adjusted_span(
    cite: Any,
    adjusted_spans: _AdjustedSpans,
) -> Tuple[int, int] | None:
    """Get the adjusted span for a citation.

    First checks the adjusted_spans dict for string citation corrections,
    then falls back to the citation's native span.

    Args:
        cite: Citation object.
        adjusted_spans: Dict of adjusted spans, keyed by id(cite).

    Returns:
        Tuple of (start, end) or None if span unavailable.
    """
    adjusted = adjusted_spans.get(id(cite))
    if adjusted is not None:
        return adjusted

    # Fallback to native span
    return get_span(cite)


def _compute_gap_ranges(
    covered_ranges: Set[Tuple[int, int]],
    text_length: int,
) -> List[Tuple[int, int]]:
    """Compute the complement of covered_ranges over [0, text_length).

    Used to find text not covered by any detected string citation, so that
    text can still be scanned by eyecite as standalone segments instead of
    being silently skipped.

    Args:
        covered_ranges: Set of (start, end) ranges already covered by
            detected string citations.
        text_length: Length of the full document text.

    Returns:
        List of (start, end) gap ranges, sorted by position.
    """
    if not covered_ranges:
        return [(0, text_length)] if text_length > 0 else []

    merged: List[Tuple[int, int]] = []
    for start, end in sorted(covered_ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    gaps: List[Tuple[int, int]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < text_length:
        gaps.append((cursor, text_length))

    return gaps


def _resolve_string_local_shorts(
    resolutions: Dict[str, Any],
    segment_metadata: Dict[int, CitationSegment],
) -> Dict[Any, Any]:
    """Resolve short citations to antecedents within the same string group.

    This handles cases where a short citation appears in the same string
    citation as its full antecedent:
        "Brown v. Board, 347 U.S. 483 (1954); Brown, 347 U.S. at 495"

    Args:
        resolutions: Citation resolutions from eyecite.
        segment_metadata: Mapping of citation index to segment info.

    Returns:
        Updated resolutions dict with corrected short form assignments.
    """
    # Group citations by string_group_id
    string_groups: Dict[str, list] = {}

    for resource_key, cites in resolutions.items():
        for cite in cites:
            segment = segment_metadata.get(id(cite))
            if segment is None:
                continue

            group_id = segment.string_group_id

            if group_id is None:
                # Standalone citation, no string-local resolution needed
                continue

            if group_id not in string_groups:
                string_groups[group_id] = []

            string_groups[group_id].append({
                'resource_key': resource_key,
                'cite': cite,
                'segment': segment,
            })

    # Within each group, build local antecedent registry
    for group_id, group_items in string_groups.items():
        # Sort by position in string
        sorted_items = sorted(
            group_items,
            key=lambda x: x['segment'].position_in_string or 0
        )

        # Build lookup of full citations appearing before each position
        local_fulls: Dict[int, Dict[str, Any]] = {}

        for i, item in enumerate(sorted_items):
            cite = item['cite']

            if isinstance(cite, FullCitation):
                # Register this as potential antecedent for later shorts
                for j in range(i + 1, len(sorted_items)):
                    if j not in local_fulls:
                        local_fulls[j] = {}
                    # Store by normalized case name or statute identifier
                    lookup_key = _make_short_lookup_key(cite)
                    if lookup_key:
                        local_fulls[j][lookup_key] = item

        # Now check shorts and reassign if better local match exists
        for i, item in enumerate(sorted_items):
            cite = item['cite']

            if isinstance(cite, (ShortCaseCitation, IdCitation, SupraCitation)):
                lookup_key = _make_short_lookup_key(cite)

                if lookup_key and i in local_fulls:
                    potential_antecedent = local_fulls[i].get(lookup_key)

                    if potential_antecedent:
                        # Found a better (local) antecedent
                        correct_resource_key = potential_antecedent['resource_key']

                        # Log the correction
                        logger.info(
                            "String-local resolution: reassigning short citation "
                            "from %s to %s (group_id=%s)",
                            item['resource_key'],
                            correct_resource_key,
                            group_id,
                        )

                        # Move citation to correct resource
                        # (eyecite may have grouped it incorrectly)
                        if correct_resource_key != item['resource_key']:
                            # Remove from current resource
                            if item['resource_key'] in resolutions:
                                try:
                                    resolutions[item['resource_key']].remove(cite)
                                except ValueError:
                                    pass

                            # Add to correct resource
                            if correct_resource_key not in resolutions:
                                resolutions[correct_resource_key] = []
                            resolutions[correct_resource_key].append(cite)

    return resolutions


def _make_short_lookup_key(cite: Any) -> str | None:
    """Create a normalized key for matching shorts to fulls.

    Args:
        cite: Citation object (Full or Short).

    Returns:
        Normalized lookup key, or None if key cannot be extracted.
    """
    if isinstance(cite, FullCaseCitation):
        name = get_case_name(cite) or ""
        from utils.cleaner import normalize_case_name_for_compare
        normalized = normalize_case_name_for_compare(name)
        if normalized and "v" in normalized:
            first_party = normalized.split("v")[0].strip()
            return first_party
        return normalized

    elif isinstance(cite, ShortCaseCitation):
        # Extract the short name
        metadata = getattr(cite, "metadata", None)
        if metadata:
            from utils.cleaner import normalize_case_name_for_compare
            plaintiff = clean_str(
                getattr(metadata, "plaintiff", None)
                or getattr(metadata, "antecedent_guess", None)
            )
            if plaintiff:
                return normalize_case_name_for_compare(plaintiff)

    elif isinstance(cite, (IdCitation, SupraCitation)):
        # These reference the immediately preceding citation
        # For string-local resolution, we need special handling
        return "__ID_OR_SUPRA__"

    elif isinstance(cite, FullLawCitation):
        # Build key from statute identifier
        reporter = clean_str(getattr(cite, "reporter", None))
        section = clean_str(getattr(cite, "section", None))
        if reporter and section:
            return f"{reporter}::{section}"

    return None


# --- Bluebook "Id." after a string citation --------------------------------

_ID_AFTER_STRING_SUBSTATUS = "id_refers_to_string_citation"


def _flag_ids_after_string_citations(
    citations: List[CitationBase],
    adjusted_spans: _AdjustedSpans,
    secondary_citations: List[SecondaryCitation],
    string_group_ranges: Dict[str, Tuple[int, int]],
) -> List[Tuple[str, List[IdCitation]]]:
    """Find "Id." citations that refer back to a string citation.

    Bluebook Rule 4.1 bars "id." from referring to a string citation (a
    citation clause with more than one authority), so such an Id. has no
    valid antecedent. eyecite would instead bind it to the string's last
    authority or, when its pin cite doesn't fit that authority, silently
    drop it.

    An eyecite Id. is flagged when the immediately preceding citation lies
    inside a string-citation range holding >= 2 non-Id. authorities and the
    Id. itself lies outside that range; an Id. whose immediately preceding
    citation is a flagged Id. inherits the error. Secondary-source citations
    count as preceding citations (an Id. after "A; B. Restatement ... ."
    refers to the Restatement), except their Id./Ibid. forms, which duplicate
    eyecite's own Id. spans.

    Args:
        citations: Every eyecite citation (including ones eyecite's resolver
            dropped), in document order.
        adjusted_spans: Document-coordinate spans, keyed by id(cite).
        secondary_citations: Detected secondary-source citations.
        string_group_ranges: string_group_id -> (start, end) of the string.

    Returns:
        One (string_group_id, [Id. citations]) chain per error entry, in
        document order.
    """
    if not string_group_ranges:
        return []

    # (start, citation, is_id) for every citation, in document order.
    events: List[Tuple[int, Any, bool]] = []
    for cite in citations:
        span = adjusted_spans.get(id(cite))
        if span is not None:
            events.append((span[0], cite, isinstance(cite, IdCitation)))
    for cite in secondary_citations:
        if cite.citation_category not in ("id", "ibid"):
            events.append((cite.span[0], cite, False))
    events.sort(key=lambda event: event[0])

    def group_at(position: int) -> str | None:
        for group_id, (start, end) in string_group_ranges.items():
            if start <= position < end:
                return group_id
        return None

    authority_counts: Dict[str, int] = {}
    for position, _cite, is_id in events:
        group_id = group_at(position)
        if group_id is not None and not is_id:
            authority_counts[group_id] = authority_counts.get(group_id, 0) + 1

    chains: List[Tuple[str, List[IdCitation]]] = []
    chain_by_cite: Dict[int, Tuple[str, List[IdCitation]]] = {}
    for i, (position, cite, is_id) in enumerate(events):
        if not is_id or i == 0:
            continue
        preceding_position, preceding, _ = events[i - 1]
        chain = chain_by_cite.get(id(preceding))
        if chain is None:
            group_id = group_at(preceding_position)
            if (
                group_id is None
                or group_at(position) == group_id
                or authority_counts.get(group_id, 0) < 2
            ):
                continue
            chain = (group_id, [])
            chains.append(chain)
        chain[1].append(cite)
        chain_by_cite[id(cite)] = chain

    return chains


def _eyecite_occurrence(
    cite: Any,
    adjusted_spans: _AdjustedSpans,
    segment_metadata: Dict[int, CitationSegment],
) -> Dict[str, Any]:
    """Build the occurrence dict for an eyecite citation."""
    segment = segment_metadata.get(id(cite))
    return {
        "citation_category": _citation_category(cite),
        "matched_text": _get_citation(cite),
        "span": _get_adjusted_span(cite, adjusted_spans),
        "index": _get_index(cite),
        "pin_cite": _get_pin_cite(cite),
        "citation_obj": cite,
        "string_group_id": segment.string_group_id if segment else None,
        "position_in_string": segment.position_in_string if segment else None,
    }

def _process_secondary_citations(
    text: str,
    citation_db: Dict[str, Dict[str, Any]],
) -> None:
    """Detect and add secondary source citations to the citation database.
    
    This function:
    1. Collects spans from eyecite-detected citations to avoid duplicates
    2. Detects full secondary citations
    3. Detects short form secondary citations (Id., supra, etc.)
    4. Resolves short forms to their antecedents (considering ALL citations)
    5. Adds all citations to the citation database with verification
    
    Args:
        text: The document text.
        citation_db: Citation database to update (modified in place).
    """
    # Collect all eyecite citation spans to avoid duplicate detection
    eyecite_spans: Set[Tuple[int, int]] = set()
    all_citation_spans: List[Tuple[int, int, str, Any]] = []
    
    for resource_key, entry in citation_db.items():
        citation_type = entry.get("type", "unknown")
        for occurrence in entry.get("occurrences", []):
            span = occurrence.get("span")
            if span and isinstance(span, (tuple, list)) and len(span) == 2:
                span_tuple = (span[0], span[1])
                eyecite_spans.add(span_tuple)
                # Track all citations with their type for Id. resolution
                # Use "case", "law", "journal" for non-secondary
                all_citation_spans.append((span[0], span[1], citation_type, None))
    
    # Sort all citation spans by position
    all_citation_spans.sort(key=lambda s: s[0])
    
    # Detect citations
    detector = SecondaryCitationDetector()
    full_citations, short_citations = detector.detect_secondary_citations(
        text, eyecite_spans
    )
    
    if not full_citations and not short_citations:
        return
    
    logger.info(
        "Detected %d full and %d short secondary source citations",
        len(full_citations),
        len(short_citations),
    )
    
    # Add full secondary citations to all_citation_spans for Id. resolution
    for cite in full_citations:
        all_citation_spans.append((cite.span[0], cite.span[1], "secondary", cite))
    
    # Re-sort after adding secondaries
    all_citation_spans.sort(key=lambda s: s[0])
    
    # Resolve short citations to their antecedents
    resolver = SecondaryCitationResolver()
    resolved_shorts = resolver.resolve_short_citations(
        full_citations, short_citations, all_citation_spans
    )
    
    # Process full citations first
    secondary_tasks: List[asyncio.Task] = []
    for cite in full_citations:
        _add_secondary_to_db(cite, citation_db, is_full=True, secondary_tasks=secondary_tasks)

    # Process resolved short citations
    # Filter out Id. citations that refer to non-secondary sources
    for cite in resolved_shorts:
        if cite.source_type == "non_secondary":
            logger.info(
                "Skipping Id. at position %d - refers to non-secondary citation",
                cite.span[0],
            )
            continue
        _add_secondary_to_db(cite, citation_db, is_full=False, secondary_tasks=secondary_tasks)

def _append_if_existing(
    resource_key: str,
    citation_db: Dict[str, Dict[str, Any]],
    occurrence: Dict[str, Any],
    matched_text: str,
) -> bool:
    """Append occurrence to an existing citation_db entry if present.

    Returns True if an existing entry was found and updated, False if
    resource_key is not (yet) present in citation_db.
    """
    if resource_key not in citation_db:
        return False
    citation_db[resource_key]["occurrences"].append(occurrence)
    logger.info(
        "Added occurrence to existing secondary citation: %s",
        matched_text[:50],
    )
    return True


def _add_secondary_to_db(
    cite: SecondaryCitation,
    citation_db: Dict[str, Dict[str, Any]],
    is_full: bool,
    secondary_tasks: List[asyncio.Task],
) -> None:
    """Add a secondary citation to the citation database.

    Full citations that require Library of Congress verification are inserted
    with a "pending" status and their verification is scheduled as an async
    task (appended to secondary_tasks) so the blocking LOC API call runs off
    the main event loop instead of stalling the whole request.

    Args:
        cite: The SecondaryCitation to add.
        citation_db: Citation database to update (modified in place).
        is_full: Whether this is a full citation (vs short form).
        secondary_tasks: List to append a verification asyncio.Task to, for
            full citations that need LOC verification.
    """
    # Determine resource key
    if cite.antecedent_key and not is_full:
        # Short form - use antecedent's resource key
        resource_key = cite.antecedent_key
    else:
        # Full citation - use its own resource key
        resource_key = cite.to_resource_key()
    
    occurrence = {
        "citation_category": cite.citation_category,
        "matched_text": cite.matched_text,
        "span": cite.span,
        "index": None,
        "pin_cite": cite.pin_cite,
        "citation_obj": cite,
        "string_group_id": None,
        "position_in_string": None,
    }
    
    # Check if entry already exists
    if _append_if_existing(resource_key, citation_db, occurrence, cite.matched_text):
        return

    # Create new entry (only for full citations)
    if not is_full:
        # This is a short citation but no full was found
        logger.error(
            "Short citation %s at position %d has no antecedent in database",
            cite.matched_text,
            cite.span[0],
        )
        # Create a stub entry anyway, unless the fallback key happens to
        # already name an existing entry (e.g. another sparse short-form
        # citation) - in that case merge into it instead of clobbering it.
        resource_key = cite.to_resource_key()
        if _append_if_existing(resource_key, citation_db, occurrence, cite.matched_text):
            return
    
    # Verify the citation
    normalized = cite.to_normalized_citation()
    resource_dict = {
        "kind": "secondary",
        "source_type": cite.source_type,
        "id_tuple": (
            cite.volume or "",
            cite.title or "",
            cite.section or cite.page or "",
            cite.year or "",
        ),
    }
    
    # Only verify full citations; schedule the (blocking) LOC lookup as a
    # background task instead of calling it inline so it doesn't stall the
    # event loop or hold this request's DB session idle for minutes.
    if is_full:
        status = "pending"
        substatus = "secondary_verification_pending"
        verification_details = None
    else:
        # Short forms inherit verification status from their antecedent
        status = "warning"
        substatus = "short_form_unresolved"
        verification_details = {
            "note": "Short form citation without resolved antecedent",
        }

    # Create new entry
    citation_db[resource_key] = {
        "type": "secondary",
        "resource": resource_dict,
        "status": status,
        "substatus": substatus,
        "verification_details": verification_details,
        "normalized_citation": normalized,
        "full_citation_obj": cite,
        "occurrences": [occurrence],
    }

    if is_full:
        secondary_tasks.append(
            asyncio.create_task(
                _verify_secondary_async(resource_key, cite, normalized, resource_dict)
            )
        )
    else:
        logger.info(
            "Added new secondary citation to database: %s (status: %s)",
            cite.matched_text[:50],
            status,
        )



# --- Main compilation function --------------------------------------------

async def compile_citations(
    text: str,
    note_spans: Sequence[Tuple[int, int]] = (),
) -> Dict[str, Any]:
    """Compile citations from the given text, handling string citations.

    This function:
    1. Detects string citations (multiple citations separated by semicolons)
    2. Splits string citations into individual segments
    3. Processes each segment with eyecite
    4. Resolves short citations to local antecedents within string groups
    5. Detects and resolves secondary source citations
    6. Sorts all citations by document position
    7. Verifies citations against external sources

    Args:
        text: The document text to analyze.
        note_spans: (start, end) of each footnote/endnote body inlined in
            `text` (ExtractedDocument.footnotes); keeps secondary-source
            citations from straddling a note boundary.

    Returns:
        Dict mapping resource keys to citation metadata, including:
        - type, status, substatus
        - normalized_citation
        - occurrences (with string_group_id and position_in_string)
        - verification_details

    Raises:
        Exception: If critical errors occur during processing.
    """
    logger.info("Starting citation compilation (text length: %d chars)", len(text))

    # Steps 1-5: String detection, eyecite processing (unchanged)
    detector = StringCitationDetector()
    splitter = StringCitationSplitter()

    string_citation_spans = detector.detect_string_citations(text)
    logger.info("Detected %d potential string citation spans", len(string_citation_spans))

    all_segments: list[CitationSegment] = []
    covered_ranges: set[Tuple[int, int]] = set()
    string_group_ranges: Dict[str, Tuple[int, int]] = {}
    string_group_counter = 0

    for start, end, is_string in string_citation_spans:
        if is_string:
            string_text = text[start:end]
            group_id = f"string_group_{string_group_counter}"
            string_group_counter += 1

            try:
                segments = splitter.split_string_citation(
                    string_text,
                    start,
                    group_id,
                )
                all_segments.extend(segments)
                covered_ranges.add((start, end))
                string_group_ranges[group_id] = (start, end)
            except ValueError as exc:
                logger.error("Failed to split string citation: %s", exc)
                continue

    # Text outside the detected string-citation spans still needs to be
    # scanned by eyecite. Without this, full/short citations outside the
    # detected string sentences are silently dropped whenever at least one
    # string citation exists elsewhere in the document.
    gap_segment_count = 0
    if all_segments:
        for gap_start, gap_end in _compute_gap_ranges(covered_ranges, len(text)):
            gap_text = text[gap_start:gap_end]
            stripped_gap_text = gap_text.strip()
            if not stripped_gap_text:
                continue
            # Preserve the offset of the stripped text within the gap so
            # spans still line up with the original document (mirrors how
            # StringCitationSplitter locates part_stripped within its part).
            leading_ws = len(gap_text) - len(gap_text.lstrip())
            segment_start = gap_start + leading_ws
            segment_end = segment_start + len(stripped_gap_text)
            all_segments.append(
                create_standalone_segment(stripped_gap_text, segment_start, segment_end)
            )
            gap_segment_count += 1

        logger.info(
            "Assembled %d string segment(s) and %d gap segment(s) covering "
            "the remaining document text",
            len(all_segments) - gap_segment_count,
            gap_segment_count,
        )

    all_resolutions: Dict[Any, Any] = {}
    all_segment_metadata: Dict[int, CitationSegment] = {}
    adjusted_spans: _AdjustedSpans = {}
    all_citations: List[CitationBase] = []

    # Segments must be visited in document order: resolve_citations resolves
    # short/id/supra forms only against full citations already seen earlier
    # in the same call, and all_segments holds string segments followed by
    # gap segments (not necessarily in document order).
    for segment in sorted(all_segments, key=lambda seg: seg.original_span[0]):
        seg_citations, seg_metadata = _process_citation_segment(
            segment,
            adjusted_spans
        )
        all_citations.extend(seg_citations)
        all_segment_metadata.update(seg_metadata)

    if all_segments:
        # Resolve once over the document-ordered list: eyecite drops any
        # short form it cannot resolve, so per-segment resolution silently
        # lost shorts whose antecedent lived in another segment.
        all_resolutions = _resolve_all_citations(all_citations)

    if not all_segments:
        logger.info("No string citations detected; using standard eyecite processing")
        cleaned_text, whole_doc_offsets = _clean_with_offset_map(text)
        citations = get_citations(cleaned_text, tokenizer=_JOURNAL_FALLBACK_TOKENIZER)
        _repair_case_years(citations)

        logger.info(f"Detected {len(citations)} citations in text: {citations}")

        if not citations:
            logger.info("No citations detected in text; returning empty result set")
            return {}

        for cite in citations:
            cite_span = get_span(cite)
            if cite_span:
                mapped = _map_cleaned_span(cite_span, whole_doc_offsets)
                if mapped is not None:
                    adjusted_spans[id(cite)] = mapped

        all_resolutions = _resolve_all_citations(citations)

    if not all_resolutions or not any(all_resolutions.values()):
        logger.info("No citations detected; returning empty result set")
        return {}

    all_resolutions = _resolve_string_local_shorts(
        all_resolutions,
        all_segment_metadata,
    )

    # Step 6: Detect and resolve secondary citations
    eyecite_spans: Set[Tuple[int, int]] = set()
    all_citation_spans_for_id_resolution: List[Tuple[int, int, str, Any]] = []
    
    # Collect eyecite spans for secondary detection and Id. resolution
    for resource, resolved_cites in all_resolutions.items():
        resource_key = _resource_identifier(resource)
        entry_type = _get_citation_type(
            next((c for c in resolved_cites if isinstance(c, FullCitation)), None)
        ) if resolved_cites else "unknown"
        
        for cite in resolved_cites:
            cite_span = _get_adjusted_span(cite, adjusted_spans)
            if cite_span:
                eyecite_spans.add(cite_span)
                all_citation_spans_for_id_resolution.append(
                    (cite_span[0], cite_span[1], entry_type, None)
                )
    
    # Sort for Id. resolution
    all_citation_spans_for_id_resolution.sort(key=lambda s: s[0])
    
    # Detect secondary citations
    secondary_detector = SecondaryCitationDetector()
    full_secondary_citations, short_secondary_citations = secondary_detector.detect_secondary_citations(
        text, eyecite_spans, note_spans
    )
    
    if full_secondary_citations or short_secondary_citations:
        logger.info(
            "Detected %d full and %d short secondary source citations",
            len(full_secondary_citations),
            len(short_secondary_citations),
        )
        
        # Add full secondary citations to all_citation_spans for Id. resolution
        for cite in full_secondary_citations:
            all_citation_spans_for_id_resolution.append(
                (cite.span[0], cite.span[1], "secondary", cite)
            )
        
        # Re-sort after adding secondaries
        all_citation_spans_for_id_resolution.sort(key=lambda s: s[0])
        
        # Resolve short citations to their antecedents
        secondary_resolver = SecondaryCitationResolver()
        resolved_short_secondary = secondary_resolver.resolve_short_citations(
            full_secondary_citations,
            short_secondary_citations,
            all_citation_spans_for_id_resolution,
        )
        
        # Filter out Id. citations that refer to non-secondary sources
        resolved_short_secondary = [
            cite for cite in resolved_short_secondary
            if cite.source_type != "non_secondary"
        ]
        
        if resolved_short_secondary:
            logger.info(
                "Filtered to %d short secondary citations (removed non-secondary refs)",
                len(resolved_short_secondary),
            )
    else:
        full_secondary_citations = []
        resolved_short_secondary = []

    # Step 6b: Bluebook Rule 4.1 -- an "Id." referring back to a string
    # citation has no valid antecedent. Report it as an error instead of
    # letting eyecite bind it to the string's last authority or drop it.
    id_string_errors = _flag_ids_after_string_citations(
        all_citations,
        adjusted_spans,
        full_secondary_citations + short_secondary_citations,
        string_group_ranges,
    )
    if id_string_errors:
        flagged_ids = {id(cite) for _, chain in id_string_errors for cite in chain}
        for resolved_cites in all_resolutions.values():
            resolved_cites[:] = [cite for cite in resolved_cites if id(cite) not in flagged_ids]
        flagged_spans = [adjusted_spans[cite_id] for cite_id in flagged_ids]
        resolved_short_secondary = [
            cite for cite in resolved_short_secondary
            if not any(cite.span[0] < end and start < cite.span[1] for start, end in flagged_spans)
        ]
        logger.info(
            "Flagged %d Id. citation(s) referring back to a string citation",
            len(flagged_ids),
        )

    # Step 7: Create unified list of all citations sorted by position
    citation_entries: List[Dict[str, Any]] = []
    
    # Add eyecite citations with their first occurrence position
    for resource, resolved_cites in all_resolutions.items():
        if not resolved_cites:
            continue
        
        # Find first occurrence position
        first_position = float('inf')
        for cite in resolved_cites:
            cite_span = _get_adjusted_span(cite, adjusted_spans)
            if cite_span and cite_span[0] < first_position:
                first_position = cite_span[0]
        
        citation_entries.append({
            'type': 'eyecite',
            'position': first_position,
            'resource': resource,
            'resolved_cites': resolved_cites,
        })
    
    # Add secondary citations with their positions
    for cite in full_secondary_citations:
        citation_entries.append({
            'type': 'secondary_full',
            'position': cite.span[0],
            'citation': cite,
        })
    
    for cite in resolved_short_secondary:
        citation_entries.append({
            'type': 'secondary_short',
            'position': cite.span[0],
            'citation': cite,
        })

    for group_id, chain in id_string_errors:
        citation_entries.append({
            'type': 'id_string_error',
            'position': adjusted_spans[id(chain[0])][0],
            'group_id': group_id,
            'cites': chain,
        })

    # Sort by position to maintain document order
    citation_entries.sort(key=lambda x: x['position'])
    
    logger.info(
        "Built unified citation list with %d entries in document order",
        len(citation_entries)
    )

    # Step 8: Build citation database in sorted order
    citation_db: Dict[str, Dict[str, Any]] = {}
    state_tasks = []
    secondary_tasks: List[asyncio.Task] = []
    journal_tasks: List[asyncio.Task] = []
    federal_tasks: List[asyncio.Task] = []

    for entry in citation_entries:
        if entry['type'] == 'eyecite':
            # Process eyecite citation (existing logic)
            resource = entry['resource']
            resolved_cites = entry['resolved_cites']
            
            resource_key = _resource_identifier(resource)
            if isinstance(resource, ResourceKey):
                resource_dict = asdict(resource)
                resource_kind = resource.kind
            else:
                resource_dict = {
                    "kind": _ctype(resource),
                    "id_tuple": (str(resource),),
                }
                resource_kind = resource_dict["kind"]

            primary_full = next(
                (cite for cite in resolved_cites if isinstance(cite, FullCitation)),
                None,
            )

            representative = primary_full or resolved_cites[0]
            normalized_key = _normalized_key(representative) or resource_key

            entry_type = _get_citation_type(primary_full) if primary_full else resource_kind
            logger.info("Entry type: %s", entry_type)

            status = "error"
            substatus = f"{entry_type}_verification_unsupported"
            verification_details = None

            fallback_value = _get_citation(primary_full)

            # Verification logic
            if entry_type == "case":
                status, substatus, verification_details = verify_case_citation(
                    primary_full,
                    normalized_key,
                    resource_dict,
                    fallback_citation=fallback_value,
                )
                logger.info(f"Verifying case citation: {normalized_key}: status={status}, substatus={substatus}")

            elif entry_type == "law":
                jurisdiction = None
                if isinstance(primary_full, FullLawCitation):
                    jurisdiction = classify_full_law_jurisdiction(primary_full)

                if jurisdiction == "federal":
                    status = "pending"
                    substatus = "federal_law_verification_pending"
                    verification_details = None
                    federal_tasks.append(
                        asyncio.create_task(
                            _verify_federal_async(
                                resource_key,
                                primary_full,
                                normalized_key,
                                resource_dict,
                                fallback_value,
                            )
                        )
                    )
                elif jurisdiction == "state":
                    status = "pending"
                    substatus = "state_law_verification_pending"
                    verification_details = None
                    state_tasks.append(
                        asyncio.create_task(
                            _verify_state_async(
                                resource_key,
                                primary_full,
                                normalized_key,
                                resource_dict,
                                fallback_value,
                            )
                        )
                    )
                else:
                    logger.info(f"Unsupported jurisdiction for resource_key: {resource_key}")
                    status = "error"
                    substatus = "unsupported_jurisdiction"
                    verification_details = {
                        "jurisdiction": jurisdiction or "unknown",
                    }

            elif entry_type == "journal":
                status = "pending"
                substatus = "journal_verification_pending"
                verification_details = None
                journal_tasks.append(
                    asyncio.create_task(
                        _verify_journal_async(
                            resource_key,
                            primary_full,
                            normalized_key,
                            resource_dict,
                        )
                    )
                )

            citation_db[resource_key] = {
                "type": entry_type,
                "resource": resource_dict,
                "status": status,
                "substatus": substatus,
                "verification_details": verification_details,
                "normalized_citation": normalized_key,
                "full_citation_obj": primary_full,
                "occurrences": [],
            }

            # Add occurrences with string group metadata
            for cite in resolved_cites:
                citation_db[resource_key]["occurrences"].append(
                    _eyecite_occurrence(cite, adjusted_spans, all_segment_metadata)
                )

        elif entry['type'] == 'id_string_error':
            # Bluebook Rule 4.1 violation (see Step 6b): no antecedent to verify.
            chain = entry['cites']
            error_key = ResourceKey("unknown", ("id", str(entry['position'])))
            group_start, group_end = string_group_ranges[entry['group_id']]
            citation_db[_resource_identifier(error_key)] = {
                "type": "unknown",
                "resource": asdict(error_key),
                "status": "error",
                "substatus": _ID_AFTER_STRING_SUBSTATUS,
                "verification_details": {
                    "reason": 'Bluebook Rule 4.1: "id." cannot refer to a string citation',
                    "string_citation": text[group_start:group_end].strip(),
                },
                "normalized_citation": " ".join(
                    part for part in (_get_citation(chain[0]), _get_pin_cite(chain[0])) if part
                ),
                "full_citation_obj": None,
                "occurrences": [
                    _eyecite_occurrence(cite, adjusted_spans, all_segment_metadata)
                    for cite in chain
                ],
            }

        elif entry['type'] in ('secondary_full', 'secondary_short'):
            # Process secondary citation
            cite = entry['citation']
            is_full = (entry['type'] == 'secondary_full')
            _add_secondary_to_db(cite, citation_db, is_full, secondary_tasks=secondary_tasks)

    # Complete async verifications (state law + secondary sources + journals) concurrently,
    # off the main event loop, so slow external lookups don't block the request.
    pending_tasks = state_tasks + secondary_tasks + journal_tasks + federal_tasks
    if pending_tasks:
        for resource_key_task, status, substatus, verification_details in await asyncio.gather(*pending_tasks):
            entry = citation_db.get(resource_key_task)
            if not entry:
                logger.error("Async verification completed for unknown resource_key %s", resource_key_task)
                continue
            entry["status"] = status
            entry["substatus"] = substatus
            entry["verification_details"] = verification_details

    logger.info("Citation compilation complete: %d unique citations", len(citation_db))

    return citation_db
