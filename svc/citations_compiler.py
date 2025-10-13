# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Set, Tuple

from eyecite import clean_text, get_citations, resolve_citations
from eyecite.models import (
    CaseCitation,
    FullCaseCitation,
    FullCitation,
    FullJournalCitation,
    FullLawCitation,
    IdCitation,
    ReferenceCitation,
    ShortCaseCitation,
    SupraCitation,
)

from svc.secondary_citation_handler import (
    SecondaryCitation,
    SecondaryCitationDetector,
    SecondaryCitationResolver,
)
from svc.string_citation_handler import (
    CitationSegment,
    StringCitationDetector,
    StringCitationSplitter,
)
from utils.cleaner import clean_str
from utils.logger import get_logger
from utils.resource_resolver import get_journal_author_title
from utils.span_finder import get_span
from verifiers.case_verifier import get_case_name, verify_case_citation
from verifiers.federal_law_verifier import (
    classify_full_law_jurisdiction,
    verify_federal_law_citation,
)
from verifiers.journal_verifier import verify_journal_citation
from verifiers.state_law_verifier import verify_state_law_citation

logger = get_logger()

_AdjustedSpans = Dict[int, Tuple[int, int]]

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


# --- String citation processing helpers -----------------------------------

def _process_citation_segment(
    segment: CitationSegment,
    adjusted_spans: _AdjustedSpans,  # New parameter to collect adjusted spans
) -> Tuple[Dict[Any, Any], Dict[int, CitationSegment]]:
    """Process a single citation segment with eyecite.

    Args:
        segment: The citation segment to process.
        adjusted_spans: Dict to store adjusted span information (modified in place).

    Returns:
        Tuple of (resolutions dict, segment_metadata dict).
    """
    segment_text = segment.text
    cleaned = clean_text(segment_text, ["all_whitespace", "underscores"])

    try:
        citations = get_citations(cleaned)
    except Exception as exc:
        logger.error("eyecite.get_citations failed for segment: %s", exc)
        return {}, {}

    try:
        resolutions = resolve_citations(
            citations,
            resolve_full_citation=_bind_full_citation,
        )
    except Exception as exc:
        logger.error("eyecite.resolve_citations failed for segment: %s", exc)
        resolutions = {
            f"raw:{idx}": [citation]
            for idx, citation in enumerate(citations)
        }

    # Adjust spans to original document coordinates
    segment_metadata: Dict[int, CitationSegment] = {}

    for resource, resolved_cites in resolutions.items():
        for cite in resolved_cites:
            cite_span = get_span(cite)
            cite_index = _get_index(cite)

            if cite_span and cite_index is not None:
                seg_start, seg_end = cite_span
                # Calculate adjusted position in original document
                adjusted_start = segment.original_span[0] + seg_start
                adjusted_end = segment.original_span[0] + seg_end

                # Store adjusted span separately (don't modify eyecite object)
                adjusted_spans[cite_index] = (adjusted_start, adjusted_end)

            # Track segment metadata for this citation
            if cite_index is not None:
                segment_metadata[cite_index] = segment

    return resolutions, segment_metadata


def _get_adjusted_span(
    cite: Any,
    adjusted_spans: _AdjustedSpans,
) -> Tuple[int, int] | None:
    """Get the adjusted span for a citation.

    First checks the adjusted_spans dict for string citation corrections,
    then falls back to the citation's native span.

    Args:
        cite: Citation object.
        adjusted_spans: Dict of adjusted spans.

    Returns:
        Tuple of (start, end) or None if span unavailable.
    """
    cite_index = _get_index(cite)

    if cite_index is not None and cite_index in adjusted_spans:
        return adjusted_spans[cite_index]

    # Fallback to native span
    return get_span(cite)


def _merge_resolutions(
    target: Dict[str, Any],
    source: Dict[str, Any],
) -> None:
    """Merge source resolutions into target.

    Args:
        target: Target resolutions dict (modified in place).
        source: Source resolutions dict.
    """
    for resource_key, resolved_cites in source.items():
        if resource_key not in target:
            target[resource_key] = []
        target[resource_key].extend(resolved_cites)


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
            cite_idx = _get_index(cite)
            if cite_idx is None or cite_idx not in segment_metadata:
                continue

            segment = segment_metadata[cite_idx]
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
    for cite in full_citations:
        _add_secondary_to_db(cite, citation_db, is_full=True)
    
    # Process resolved short citations
    # Filter out Id. citations that refer to non-secondary sources
    for cite in resolved_shorts:
        if cite.source_type == "non_secondary":
            logger.info(
                "Skipping Id. at position %d - refers to non-secondary citation",
                cite.span[0],
            )
            continue
        _add_secondary_to_db(cite, citation_db, is_full=False)

def _add_secondary_to_db(
    cite: SecondaryCitation,
    citation_db: Dict[str, Dict[str, Any]],
    is_full: bool,
) -> None:
    """Add a secondary citation to the citation database.
    
    Args:
        cite: The SecondaryCitation to add.
        citation_db: Citation database to update (modified in place).
        is_full: Whether this is a full citation (vs short form).
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
    if resource_key in citation_db:
        # Add as additional occurrence
        citation_db[resource_key]["occurrences"].append(occurrence)
        logger.info(
            "Added occurrence to existing secondary citation: %s",
            cite.matched_text[:50],
        )
        return
    
    # Create new entry (only for full citations)
    if not is_full:
        # This is a short citation but no full was found
        logger.error(
            "Short citation %s at position %d has no antecedent in database",
            cite.matched_text,
            cite.span[0],
        )
        # Create a stub entry anyway
        resource_key = cite.to_resource_key()
    
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
    
    # Only verify full citations
    if is_full:
        from verifiers.secondary_sources_verifier import verify_secondary_citation
        status, substatus, verification_details = verify_secondary_citation(
            cite, normalized, resource_dict
        )
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
    
    logger.info(
        "Added new secondary citation to database: %s (status: %s)",
        cite.matched_text[:50],
        status,
    )



# --- Main compilation function --------------------------------------------

async def compile_citations(text: str) -> Dict[str, Any]:
    """Compile citations from the given text, handling string citations.

    This function:
    1. Detects string citations (multiple citations separated by semicolons)
    2. Splits string citations into individual segments
    3. Processes each segment with eyecite
    4. Resolves short citations to local antecedents within string groups
    5. Verifies citations against external sources

    Args:
        text: The document text to analyze.

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

    # Step 1: Detect and split string citations
    detector = StringCitationDetector()
    splitter = StringCitationSplitter()

    string_citation_spans = detector.detect_string_citations(text)
    logger.info("Detected %d potential string citation spans", len(string_citation_spans))

    # Step 2: Build list of all citation segments (string + standalone)
    all_segments: list[CitationSegment] = []
    covered_ranges: set[Tuple[int, int]] = set()
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
            except ValueError as exc:
                logger.error("Failed to split string citation: %s", exc)
                continue

    # Step 3: Process each segment with eyecite
    all_resolutions: Dict[Any, Any] = {}  # Changed from Dict[str, Any]
    all_segment_metadata: Dict[int, CitationSegment] = {}
    adjusted_spans: _AdjustedSpans = {}

    for segment in all_segments:
        seg_resolutions, seg_metadata = _process_citation_segment(
            segment,
            adjusted_spans
        )
        _merge_resolutions(all_resolutions, seg_resolutions)
        all_segment_metadata.update(seg_metadata)

    # Step 4: Process non-string portions (fallback to original eyecite flow)
    if not all_segments:
        logger.info("No string citations detected; using standard eyecite processing")
        cleaned_text = clean_text(text, ["all_whitespace", "underscores"])
        citations = get_citations(cleaned_text)

        logger.info(f"Detected {len(citations)} citations in text: {citations}")

        if not citations:
            logger.info("No citations detected in text; returning empty result set")
            return {}

        try:
            all_resolutions = resolve_citations(
                citations,
                resolve_full_citation=_bind_full_citation,
            )
        except Exception as exc:
            logger.error("eyecite resolve_citations failed: %s", exc)
            all_resolutions = {
                f"raw:{idx}": [citation]
                for idx, citation in enumerate(citations)
            }


    if not all_resolutions or not any(all_resolutions.values()):
        logger.info("No citations detected; returning empty result set")
        return {}

    # Step 5: Resolve short citations within string groups
    all_resolutions = _resolve_string_local_shorts(
        all_resolutions,
        all_segment_metadata,
    )

    # Step 6: Build citation database with verification
    citation_db: Dict[str, Dict[str, Any]] = {}
    state_tasks = []

    for resource, resolved_cites in all_resolutions.items():
        if not resolved_cites:
            continue

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
                status, substatus, verification_details = verify_federal_law_citation(
                    primary_full,
                    normalized_key,
                    resource_dict,
                    fallback_citation=fallback_value,
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
            status, substatus, verification_details = verify_journal_citation(
                primary_full,
                normalized_key,
                resource_dict,
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
            cite_idx = _get_index(cite)
            segment = all_segment_metadata.get(cite_idx) if cite_idx else None

            cite_span = _get_adjusted_span(cite, adjusted_spans)

            occurrence = {
                "citation_category": _citation_category(cite),
                "matched_text": _get_citation(cite),
                "span": cite_span,
                "index": cite_idx,
                "pin_cite": _get_pin_cite(cite),
                "citation_obj": cite,
                # New fields for string citation support
                "string_group_id": segment.string_group_id if segment else None,
                "position_in_string": segment.position_in_string if segment else None,
            }
            citation_db[resource_key]["occurrences"].append(occurrence)

    if state_tasks:
        for resource_key_task, status, substatus, verification_details in await asyncio.gather(*state_tasks):
            entry = citation_db.get(resource_key_task)
            if not entry:
                logger.error("State verification completed for unknown resource_key %s", resource_key_task)
                continue
            entry["status"] = status
            entry["substatus"] = substatus
            entry["verification_details"] = verification_details

    # Step 7: Process secondary citations
    _process_secondary_citations(text, citation_db)

    logger.info("Citation compilation complete: %d unique citations", len(citation_db))

    return citation_db
