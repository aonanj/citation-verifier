# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""Builds CitationRecords from eyecite citations (the rules extractor).

Each record carries the values the verifiers used to read off the eyecite
object themselves, computed the same way, so the rules path verifies exactly
as before.
"""

from __future__ import annotations

import re
from typing import Any

from eyecite.models import FullCaseCitation, FullJournalCitation, FullLawCitation

from svc.citation_record import LAW_FIELDS, SECONDARY_FIELDS, CitationRecord
from utils.cleaner import clean_str
from utils.resource_resolver import get_journal_author_title, resolve_case_name
from verifiers.federal_law_verifier import classify_law_jurisdiction

# eyecite sometimes takes neighboring non-name text as a party ("Id. 141.",
# "7th Cir. 1910).", a glued footnote number "Reflect- 41.", a PDF page footer
# "... Repository, 2010"); such a party is discarded rather than reported.
# Numbered names like "Local 1199" or "One 1958 Plymouth Sedan" still pass.
_NON_NAME_PARTY_RE = re.compile(r"^(?:id|ibid|supra)\b|[()]|\s\d+\.$|,\s*\d{4}$", re.IGNORECASE)


def _name_party(value: Any) -> str | None:
    party = clean_str(value)
    if party and _NON_NAME_PARTY_RE.search(party):
        return None
    return party


def get_case_name(obj) -> str | None:
    if obj is None:
        return None
    case_name = None
    metadata = getattr(obj, "metadata", None)
    if metadata is not None:
        plaintiff = _name_party(
            getattr(metadata, "plaintiff", None)
            or getattr(metadata, "petitioner", None)
        )
        defendant = _name_party(
            getattr(metadata, "defendant", None)
            or getattr(metadata, "respondent", None)
        )
        if plaintiff and defendant:
            case_name = clean_str(f"{plaintiff} v. {defendant}")

        if plaintiff is None and defendant is not None:
            case_name = f"In re {defendant}"

    return resolve_case_name(case_name, obj)


def _sanitize_section(text: str | None) -> str | None:
    if not text:
        return None
    return clean_str(text.replace("§", "")) or None


def _law_text(cite) -> str:
    """The law citation's text, as classify_law_jurisdiction reads it.

    Defensive extraction. Works across eyecite versions.
    """
    parts = []
    cite_str = None

    val = getattr(cite, "full_cite", None) or getattr(cite.groups, "full_cite", None) or getattr(cite.token, "data", None)
    if val is not None:
        cite_str = _sanitize_section(val)
    else:
        for attr in ("title", "volume", "chapter"):
            part_one = getattr(cite, attr, None) or getattr(cite.groups, attr, None)
            if part_one is not None:
                s = _sanitize_section(str(part_one))
                if s:
                    parts.append(s)
                    break
        for attr in ("code", "reporter"):
            part_two = getattr(cite, attr, None) or getattr(cite.groups, attr, None)
            if part_two is not None:
                s = _sanitize_section(part_two)
                if s:
                    parts.append(s)
                    break
        for attr in ("section", "page"):
            part_three = getattr(cite, attr, None) or getattr(cite.groups, attr, None)
            if part_three is not None:
                s = _sanitize_section(str(part_three))
                if s:
                    parts.append(s)
                    break

        cite_str = " ".join(parts)
    if cite_str is None or cite_str == "":
        cite_str = str(cite)
    return cite_str


def _law_value(cite, key: str) -> str | None:
    groups = getattr(cite, "groups", {}) or {}
    if key in groups:
        value = clean_str(groups.get(key))
        if value:
            return value
    return clean_str(getattr(cite, key, None))


def _case_name(cite) -> str | None:
    name = get_case_name(cite)
    if name is None:
        metadata = getattr(cite, "metadata", None)
        if metadata is not None:
            name = clean_str(getattr(metadata, "resolved_case_name", None))
            if not name:
                name = clean_str(getattr(metadata, "resolved_case_name_short", None))
    return name


def _journal_names(cite) -> Any:
    names: Any = []
    editions = getattr(cite, "all_editions", None)
    if editions and len(editions) > 0:
        reporter = getattr(editions[0], "reporter", None)
        names = [getattr(reporter, "name", None)] if reporter else None
    if names == []:
        guess = getattr(cite, "edition_guess", None)
        if guess:
            guess_names = getattr(guess, "name", None)
            names = guess_names.split(";") if guess_names else []
    return names


def record_from_eyecite(cite) -> CitationRecord | None:
    """The CitationRecord for an eyecite full citation (None for other kinds)."""
    matched_text = cite.matched_text() if cite is not None else None
    if isinstance(cite, FullCaseCitation):
        groups = getattr(cite, "groups", {}) or {}
        return CitationRecord("case", matched_text, {
            "case_name": _case_name(cite),
            "volume": clean_str(groups.get("volume")) or clean_str(getattr(cite, "volume", None)),
            "reporter": clean_str(groups.get("reporter")),
            "page": clean_str(groups.get("page")) or clean_str(getattr(cite, "page", None)),
            "year": getattr(cite, "year", None) or getattr(cite.metadata, "year", None),
        })
    if isinstance(cite, FullLawCitation):
        fields = {key: _law_value(cite, key) for key in LAW_FIELDS}
        fields["jurisdiction"] = classify_law_jurisdiction(_law_text(cite))
        return CitationRecord("law", matched_text, fields)
    if isinstance(cite, FullJournalCitation):
        groups = getattr(cite, "groups", None)
        author_title = get_journal_author_title(cite)
        return CitationRecord("journal", matched_text, {
            "author": author_title.get("author") if author_title else None,
            "title": author_title.get("title") if author_title else None,
            "journal_names": _journal_names(cite),
            "volume": groups.get("volume") if groups else None,
            "page": groups.get("page") if groups else None,
            "year": getattr(cite, "year", None),
        })
    return None


def record_from_secondary(cite) -> CitationRecord:
    """The CitationRecord for a SecondaryCitation."""
    return CitationRecord(
        "secondary",
        cite.matched_text,
        {key: getattr(cite, key, None) for key in SECONDARY_FIELDS},
    )
