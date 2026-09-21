# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""Local validation of the citations a model returns (plan section 11).

No external service and no second model is involved. For every returned citation the validator:

  1. checks the required fields and the enumerated citation kind;
  2. checks the returned source_id exists in the NormalizedDocument;
  3. finds the citation text in that block's raw_text (EXACT_MATCH), else in the conservative
     comparison form (NORMALIZED_MATCH, see svc.normalization.text);
  4. lets a match continue onto the next PDF page block (a citation split by a page break);
  5. drops exact duplicates and overlapping spans in the same block;
  6. reports where the text occurs more than once and the model gave no way to tell which
     (AMBIGUOUS_MATCH: accepted, at the first unclaimed occurrence);
  7. sorts what it accepts into document order.

A citation that cannot be found in its source block is NO_SOURCE_MATCH and is never repaired: the
caller fails closed. The validator never edits a citation string.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Sequence, Tuple

from svc.normalization.models import NormalizedDocument
from svc.normalization.text import (
    MatchText,
    find_occurrences,
    flexible_pattern,
    normalize_for_match,
    strip_inline_markup,
    unescape_delimiters,
)

ALLOWED_KINDS = frozenset({"case", "law", "journal", "secondary", "short_form"})
# A citation split by a page break continues on the next block of the same kind.
CROSS_BLOCK_KINDS = frozenset({"page"})


class ValidationStatus(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    NORMALIZED_MATCH = "NORMALIZED_MATCH"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    NO_SOURCE_MATCH = "NO_SOURCE_MATCH"
    DUPLICATE = "DUPLICATE"
    OVERLAPPING = "OVERLAPPING"


ACCEPTED_STATUSES = frozenset({
    ValidationStatus.EXACT_MATCH, ValidationStatus.NORMALIZED_MATCH, ValidationStatus.AMBIGUOUS_MATCH,
})


@dataclass
class ValidatedCitation:
    item: Dict[str, Any]
    status: ValidationStatus
    reason: str | None = None
    citation_text: str = ""
    source_id: str | None = None
    block_index: int | None = None
    raw_start: int | None = None  # in block_index's raw_text
    raw_end: int | None = None  # in end_block_index's raw_text
    end_block_index: int | None = None  # differs from block_index when the match crosses a page break
    match_kind: str | None = None  # "exact" | "normalized" | "cross_block"
    occurrences: int = 0  # how many times the text occurs in the source block

    @property
    def accepted(self) -> bool:
        return self.status in ACCEPTED_STATUSES

    @property
    def order_key(self) -> Tuple[int, int]:
        return (self.block_index if self.block_index is not None else 1 << 30, self.raw_start or 0)


@dataclass
class ValidationReport:
    results: List[ValidatedCitation]

    def accepted(self) -> List[ValidatedCitation]:
        """Accepted citations in document order (block order, then position in the block)."""
        return sorted((r for r in self.results if r.accepted), key=lambda r: r.order_key)

    def counts(self) -> Dict[str, int]:
        by_status = Counter(r.status.value for r in self.results)
        return {
            "validation_exact_matches": by_status[ValidationStatus.EXACT_MATCH.value],
            "validation_normalized_matches": by_status[ValidationStatus.NORMALIZED_MATCH.value],
            "validation_ambiguous": by_status[ValidationStatus.AMBIGUOUS_MATCH.value],
            "validation_unresolved": by_status[ValidationStatus.NO_SOURCE_MATCH.value],
            "validation_duplicates": by_status[ValidationStatus.DUPLICATE.value],
            "validation_overlapping": by_status[ValidationStatus.OVERLAPPING.value],
        }


@dataclass
class _Prepared:
    item: Dict[str, Any]
    text: str = ""
    source_id: str | None = None
    key: str = ""
    error: str | None = None


class CitationSpanValidator:
    def __init__(self, document: NormalizedDocument) -> None:
        self.document = document
        self._match: Dict[int, MatchText] = {}

    def _match_for(self, block_index: int) -> MatchText:
        cached = self._match.get(block_index)
        if cached is None:
            cached = normalize_for_match(self.document.blocks[block_index].raw_text)
            self._match[block_index] = cached
        return cached

    # --- step 1: schema -------------------------------------------------------------------

    @staticmethod
    def _prepare(item: Dict[str, Any]) -> _Prepared:
        prepared = _Prepared(item)
        raw = item.get("matched_text", item.get("citation_text"))
        if not isinstance(raw, str) or not raw.strip():
            prepared.error = "schema:citation_text"
            return prepared
        text = unescape_delimiters(strip_inline_markup(raw)[0]).strip()
        if not text:
            prepared.error = "schema:citation_text"
            return prepared
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            prepared.error = "schema:source_id"
            return prepared
        if item.get("kind") not in ALLOWED_KINDS:
            prepared.error = "schema:kind"
            return prepared
        # `page` is the cited work's own first page (an existing citation field); where the citation was FOUND is
        # `source_page`.
        page = item.get("source_page")
        if page is not None and (isinstance(page, bool) or not isinstance(page, int)):
            prepared.error = "schema:source_page"
            return prepared
        confidence = item.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1
        ):
            prepared.error = "schema:confidence"
            return prepared
        prepared.text = text
        prepared.source_id = source_id.strip()
        prepared.key = " ".join(normalize_for_match(text).text.split())
        return prepared

    # --- steps 2-6 ------------------------------------------------------------------------

    def validate(self, items: Sequence[Dict[str, Any]]) -> ValidationReport:
        prepared = [self._prepare(item) for item in items]
        wanted = Counter((p.source_id, p.key) for p in prepared if p.error is None)
        claims: Dict[int, List[Tuple[int, int, str]]] = {}
        return ValidationReport([self._validate_one(p, wanted, claims) for p in prepared])

    def _validate_one(
        self,
        prepared: _Prepared,
        wanted: Counter,
        claims: Dict[int, List[Tuple[int, int, str]]],
    ) -> ValidatedCitation:
        def reject(status: ValidationStatus, reason: str, **fields: Any) -> ValidatedCitation:
            return ValidatedCitation(prepared.item, status, reason, prepared.text, prepared.source_id, **fields)

        if prepared.error is not None:
            return reject(ValidationStatus.NO_SOURCE_MATCH, prepared.error)
        block_index = self.document.block_index(prepared.source_id or "")
        if block_index is None:
            return reject(ValidationStatus.NO_SOURCE_MATCH, "unknown_source_id")
        block = self.document.blocks[block_index]
        occurrences = find_occurrences(prepared.text, block.raw_text, self._match_for(block_index))

        if not occurrences:
            crossing = self._find_crossing(prepared.text, block_index)
            if crossing is None:
                return reject(ValidationStatus.NO_SOURCE_MATCH, "no_source_match", block_index=block_index)
            start, end = crossing
            end_index = block_index + 1
            if self._collides(claims, block_index, start, len(block.raw_text)) or self._collides(claims, end_index, 0, end):
                return reject(ValidationStatus.OVERLAPPING, "overlaps_earlier_citation", block_index=block_index)
            claims.setdefault(block_index, []).append((start, len(block.raw_text), prepared.key))
            claims.setdefault(end_index, []).append((0, end, prepared.key))
            return ValidatedCitation(
                prepared.item, ValidationStatus.NORMALIZED_MATCH, None, prepared.text, prepared.source_id,
                block_index, start, end, end_index, "cross_block", 1,
            )

        free = [o for o in occurrences if not self._collides(claims, block_index, o.start, o.end)]
        if not free:
            identical = any(
                key == prepared.key and any(o.start == s and o.end == e for o in occurrences)
                for s, e, key in claims.get(block_index, ())
            )
            if identical:
                return reject(ValidationStatus.DUPLICATE, "already_reported", block_index=block_index,
                              occurrences=len(occurrences))
            return reject(ValidationStatus.OVERLAPPING, "overlaps_earlier_citation", block_index=block_index,
                          occurrences=len(occurrences))
        chosen = free[0]
        claims.setdefault(block_index, []).append((chosen.start, chosen.end, prepared.key))
        # The text occurs more often than the model reported it: which occurrence it meant is a guess.
        if len(occurrences) > 1 and wanted[(prepared.source_id, prepared.key)] < len(occurrences):
            status = ValidationStatus.AMBIGUOUS_MATCH
        elif chosen.kind == "exact":
            status = ValidationStatus.EXACT_MATCH
        else:
            status = ValidationStatus.NORMALIZED_MATCH
        return ValidatedCitation(
            prepared.item, status, None, prepared.text, prepared.source_id, block_index,
            chosen.start, chosen.end, block_index, chosen.kind, len(occurrences),
        )

    @staticmethod
    def _collides(claims: Dict[int, List[Tuple[int, int, str]]], block_index: int, start: int, end: int) -> bool:
        return any(start < e and s < end for s, e, _ in claims.get(block_index, ()))

    def _find_crossing(self, text: str, block_index: int) -> Tuple[int, int] | None:
        """(raw start in this block, raw end in the next) of `text` continuing across a page break."""
        blocks = self.document.blocks
        block = blocks[block_index]
        if block.kind not in CROSS_BLOCK_KINDS or block_index + 1 >= len(blocks) or blocks[block_index + 1].kind != block.kind:
            return None
        pattern = flexible_pattern(normalize_for_match(text).text)
        if pattern is None:
            return None
        first, second = self._match_for(block_index), self._match_for(block_index + 1)
        joined = first.text + "\n" + second.text
        boundary = len(first.text)
        for m in pattern.finditer(joined):
            if m.start() < boundary and m.end() > boundary + 1:
                return first.starts[m.start()], second.ends[m.end() - boundary - 2]
        return None
