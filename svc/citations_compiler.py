from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Tuple

from eyecite import get_citations, resolve_citations, clean_text
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

from verifiers.case_verifier import get_case_name, verify_case_citation

# --- helper functions ------------------------------------------
_space_re = re.compile(r"\s+")

logger = logging.getLogger(__name__)

def _clean(s: str | None) -> str | None:
    if not s:
        return s
    v = str(s)
    v = _space_re.sub(" ", v).strip()
    return v if v else None

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
    return _clean(getattr(metadata, "pin_cite", None))


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


def _get_span(obj) -> Tuple[int, int] | None:
    start = getattr(obj, "full_span_start", None) or getattr(obj, "span_start", None)
    end = getattr(obj, "full_span_end", None) or getattr(obj, "span_end", None)
    if start is None:
        start = 0
    if end is None:
        end = 0
    return (start, end)

def _get_index(obj) -> int | None:
    index = getattr(obj, "index", None)
    if index is not None:
        return int(index)
    return None

def _resource_identifier(resource: Any) -> str:
    if isinstance(resource, ResourceKey):
        parts = [resource.kind, *resource.id_tuple]
        return "::".join(part for part in parts if part)
    return _clean(str(resource)) or _ctype(resource)
    
def _get_citation(obj) -> str | None:
    c = obj.token.data if hasattr(obj, "token") and hasattr(obj.token, "data") else None
    if c is not None:
        return _clean(c)
    c = obj.data if hasattr(obj, "data") else None
    if c is not None:
        return _clean(c)
    return None

def _get_journal_author_title(obj) -> dict[str | None, str | None] | None:
    if not hasattr(obj, "document") or not hasattr(obj.document, "plain_text"):
        return None
    if not hasattr(obj, "token") or not hasattr(obj.token, "data"):
        return None
    cs = f", {obj.token.data}"
    if obj.year is not None:
        cs += f" ({obj.year})."
    else:
        cs += "."
    text_block = obj.document.plain_text
    text_block = text_block.replace(cs, "").strip()
    sentences = re.split(r'([.!?])', text_block)
    sentences = [s.strip() for s in sentences if s.strip()]

    raw_cite = None
    if len(sentences) >= 2:
        raw_cite = sentences[-1].strip()
    elif len(sentences) == 1:
        raw_cite = sentences[0].strip()
    else:
        return None
    
    parts = raw_cite.split(',')
    if len(parts) < 2:
        return None
    author = _clean(parts[0])
    title = _clean(parts[1])
    return {"author": author, "title": title}

# --- Resource binding for resolver ------------------------------------------
@dataclass(frozen=True)
class ResourceKey:
    kind: str                # "case" | "law" | "other"
    id_tuple: Tuple[str, ...]  # stable tuple to represent the work

def _bind_full_citation(full_cite) -> ResourceKey | None:
    """Return a stable key Eyecite will use as the 'resource' for short forms."""
    t = _ctype(full_cite)
    if t == "FullCaseCitation":
        name = get_case_name(full_cite) or ""
        reporter = (_clean(full_cite.groups.get("reporter", None)) or "")
        vol = _clean(full_cite.groups.get("volume", None)) or ""
        page = _clean(full_cite.groups.get("page", None)) or ""
        year = _clean(full_cite.year) or ""
        return ResourceKey("case", (name, reporter, vol, page, year))
    if t == "FullLawCitation":
        title = (_clean(full_cite.groups.get("title", None)) or "")
        code = (_clean(full_cite.groups.get("reporter", None)) or "")
        section = (_clean(full_cite.groups.get("section", None)) or "")
        year = _clean(getattr(full_cite, "year", None)) or ""
        return ResourceKey("law", (title, code, section, year))
    # Treat everything else as "other" so supra can still cluster journals, etc.
    title = ""
    author = ""
    journal_info = _get_journal_author_title(full_cite)
    if journal_info is not None:
        title = journal_info.get("title", "") or ""
        author = journal_info.get("author", "") or ""
    journal = (_clean(full_cite.groups.get("reporter", None)) or "")
    volume = _clean(full_cite.groups.get("volume", None)) or ""
    page = _clean(full_cite.groups.get("page", None)) or ""
    year = _clean(full_cite.year) or ""
    return ResourceKey("other", (title, author, volume, journal, page, year))



def compile_citations(text: str) -> Dict[str, Any]:
    """Compile citations from the given text."""

    cleaned_text = clean_text(text, ["all_whitespace", "underscores"])
    citations = get_citations(cleaned_text)
    try:
        resolutions = resolve_citations(
            citations,
            resolve_full_citation=_bind_full_citation,
        )
    except Exception:  # pragma: no cover - defensive fallback
        logger.exception("eyecite resolve_citations failed; falling back to raw citations")
        resolutions = {
            f"raw:{idx}": [citation]
            for idx, citation in enumerate(citations)
        }

    citation_db: Dict[str, Dict[str, Any]] = {}

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

        status = "verified"
        substatus = None
        verification_details = None

        if entry_type == "case":
            status, substatus, verification_details = verify_case_citation(
                primary_full,
                normalized_key,
                resource_dict,
                fallback_citation=_get_citation(primary_full),
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

        for cite in resolved_cites:
            citation_db[resource_key]["occurrences"].append(
                {
                    "citation_category": _citation_category(cite),
                    "matched_text": _get_citation(cite),
                    "span": _get_span(cite),
                    "index": _get_index(cite),
                    "pin_cite": _get_pin_cite(cite),
                    "citation_obj": cite,
                }
            )

    return citation_db
