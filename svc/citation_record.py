# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""The parsed fields of one full citation, in the form the verifiers read.

Both extractors hand the verifiers a CitationRecord (as `primary_full`), so no
verifier depends on eyecite's citation objects: the rules path builds records
with svc.eyecite_adapter, the LLM path (svc.llm_extractor) from the model's
grounded fields.

Field names by `type`:
  case:      case_name, volume, reporter, page, year, court
  law:       jurisdiction ("federal" | "state" | "unknown") plus the groups
             the law verifiers look up by name - title, volume, chapter, code,
             reporter, section, page, congress, lawnum, year
  journal:   author, title, journal_names (names to search the journal by),
             volume, page, year
  secondary: source_type, volume, title, section, page, year, edition,
             series, author
A missing field is None (or absent).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

LAW_FIELDS = ("title", "volume", "chapter", "code", "reporter", "section", "page", "congress", "lawnum", "year")
SECONDARY_FIELDS = ("source_type", "volume", "title", "section", "page", "year", "edition", "series", "author")


@dataclass
class CitationRecord:
    type: str  # "case" | "law" | "journal" | "secondary"
    matched_text: str | None = None
    fields: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str) -> Any:
        return self.fields.get(key)
