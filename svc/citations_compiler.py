# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any, Dict, Tuple

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

from utils.cleaner import clean_str
from utils.logger import get_logger
from utils.resource_resolver import get_journal_author_title
from utils.span_finder import get_span
from verifiers.case_verifier import get_case_name, verify_case_citation
from verifiers.federal_law_verifier import (
    classify_full_law_jurisdiction,
    verify_federal_law_citation,
)
from verifiers.state_law_verifier import verify_state_law_citation

logger = get_logger()

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
        return f"{volume}::{reporter}::{page}"
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
        year = clean_str(full_cite.year) or ""
        return ResourceKey("case", (name, reporter, vol, page, year))
    if t == "FullLawCitation":
        title = clean_str(full_cite.groups.get("title", None) or full_cite.groups.get("volume", None) or
                          full_cite.groups.get("chapter", None)) or  ""
        code = clean_str(full_cite.groups.get("reporter", None) or full_cite.groups.get("code", None)) or ""
        section = clean_str(full_cite.groups.get("section", None) or full_cite.groups.get("page", None)) or ""
        year = clean_str(getattr(full_cite, "year", None)) or ""
        return ResourceKey("law", (title, code, section, year))
    # Treat everything else as "other" so supra can still cluster journals, etc.
    if t == "FullJournalCitation":
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



async def compile_citations(text: str) -> Dict[str, Any]:
    """Compile citations from the given text.

    State-law verifications are dispatched to background threads so the main
    request handler is not blocked on long-running OpenAI calls.
    """

    cleaned_text = clean_text(text, ["all_whitespace", "underscores"])
    citations = get_citations(cleaned_text)

    try:
        resolutions = resolve_citations(
            citations,
            resolve_full_citation=_bind_full_citation,
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.error(f"eyecite resolve_citations failed; falling back to raw citations: {exc}")
        resolutions = {
            f"raw:{idx}": [citation]
            for idx, citation in enumerate(citations)
        }

    citation_db: Dict[str, Dict[str, Any]] = {}
    state_tasks = []

    for resource, resolved_cites in resolutions.items():
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

        status = "error"
        substatus = f"{entry_type}_verification_unsupported"
        verification_details = None

        fallback_value = _get_citation(primary_full)

        if entry_type == "case":
            status, substatus, verification_details = verify_case_citation(
                primary_full,
                normalized_key,
                resource_dict,
                fallback_citation=fallback_value,
            )
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

        for cite in resolved_cites:
            citation_db[resource_key]["occurrences"].append(
                {
                    "citation_category": _citation_category(cite),
                    "matched_text": _get_citation(cite),
                    "span": get_span(cite),
                    "index": _get_index(cite),
                    "pin_cite": _get_pin_cite(cite),
                    "citation_obj": cite,
                }
            )

    if state_tasks:
        for resource_key_task, status, substatus, verification_details in await asyncio.gather(*state_tasks):
            entry = citation_db.get(resource_key_task)
            if not entry:
                logger.warning("State verification completed for unknown resource_key %s", resource_key_task)
                continue
            entry["status"] = status
            entry["substatus"] = substatus
            entry["verification_details"] = verification_details

    return citation_db
