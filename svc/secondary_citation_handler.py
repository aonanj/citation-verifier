# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

"""Handler for secondary source citations (treatises, encyclopedias, restatements, etc.).

This module detects both full and short form citations to secondary legal sources,
and resolves short forms to their antecedent full citations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Dict, Final, List, Set, Tuple

from utils.cleaner import clean_str
from utils.logger import get_logger

logger = get_logger()

# Regex patterns for common secondary sources (FULL CITATIONS)
_SECONDARY_PATTERNS: Final = {
    "cjs": re.compile(
        r"(?P<volume>\d+)\s+"
        r"C\.J\.S\.\s+"
        r"(?P<title>[A-Z][A-Za-z\s&]+?)\s+"
        r"§+\s*(?P<section>[\d.]+)"
        r"(?:\s*\((?P<year>\d{4})\))?",
        re.IGNORECASE
    ),
    "amjur": re.compile(
        r"(?P<volume>\d+)\s+"
        r"Am\.\s*Jur\.\s*(?P<edition>\d+d)?\s+"
        r"(?P<title>[A-Z][A-Za-z\s&]+?)\s+"
        r"§+\s*(?P<section>[\d.]+)"
        r"(?:\s*\((?P<year>\d{4})\))?",
        re.IGNORECASE
    ),
    "alr": re.compile(
        r"(?P<volume>\d+)\s+"
        r"A\.L\.R\.(?:\s*(?P<series>\d+[a-z]{2}))?\s+"
        r"(?P<page>\d+)"
        r"(?:\s*\((?P<year>\d{4})\))?",
        re.IGNORECASE
    ),
    "restatement": re.compile(
        r"Restatement\s+"
        r"\((?P<edition>First|Second|Third|Fourth)\)\s+"
        r"(?:of\s+)?(?P<subject>[A-Z][A-Za-z\s&]+?)\s+"
        r"§+\s*(?P<section>[\d.]+)"
        r"(?:\s*\((?P<year>\d{4})\))?",
        re.IGNORECASE
    ),
    "treatise": re.compile(
        r"(?P<author>[A-Z][A-Za-z\s.,'&]+?),\s+"
        r"(?P<title>[A-Z][A-Za-z\s:]+?)\s+"
        r"§+\s*(?P<section>[\d.:]+)"
        r"(?:\s*\((?P<edition>\d+(?:st|nd|rd|th)\s+ed\.)?\s*(?P<year>\d{4})\))?",
        re.IGNORECASE
    ),
}

# Regex patterns for SHORT FORM citations
# These patterns are now more restrictive to avoid false positives
_SHORT_FORM_PATTERNS: Final = {
    # Id. with optional pin cite - MUST be preceded by sentence boundary or start
    "id": re.compile(
        r"(?:^|(?<=\.)\s+|(?<=;)\s+)"  # Sentence boundary or start
        r"\*?Id\.\*?(?:\s+at\s+(?P<pin_cite>[\d.]+))?"
        r"(?:\s*\((?P<parenthetical>[^)]+)\))?",
        re.IGNORECASE
    ),
    
    # Ibid. (less common in modern legal writing)
    "ibid": re.compile(
        r"(?:^|(?<=\.)\s+|(?<=;)\s+)"  # Sentence boundary
        r"Ibid\.(?:\s+at\s+(?P<pin_cite>[\d.]+))?"
        r"(?:\s*\((?P<parenthetical>[^)]+)\))?",
        re.IGNORECASE
    ),
    
    # Supra note reference - must have explicit "supra" keyword
    # Exclude common false positives like "See supra" without proper context
    "supra": re.compile(
        r"(?P<title_fragment>[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}),\s+"
        r"supra\s+note\s+(?P<note_num>\d+)"
        r"(?:,?\s+at\s+(?P<pin_cite>[\d.]+))?"
        r"(?:\s*\((?P<parenthetical>[^)]+)\))?",
        re.IGNORECASE
    ),
    
    # Short form with volume/section but no title (e.g., "88 C.J.S. § 195")
    # MUST have C.J.S. explicitly (not U.S.C. or other codes)
    "cjs_short": re.compile(
        r"(?P<volume>\d+)\s+"
        r"C\.J\.S\.\s+"
        r"§+\s*(?P<section>[\d.]+)"
        r"(?:\s+at\s+(?P<page>[\d.]+))?",
        re.IGNORECASE
    ),
    
    "amjur_short": re.compile(
        r"(?P<volume>\d+)\s+"
        r"Am\.\s*Jur\.\s*(?P<edition>\d+d)?\s+"
        r"§+\s*(?P<section>[\d.]+)"
        r"(?:\s+at\s+(?P<page>[\d.]+))?",
        re.IGNORECASE
    ),
    
    "alr_short": re.compile(
        r"(?P<volume>\d+)\s+"
        r"A\.L\.R\.(?:\s*(?P<series>\d+[a-z]{2}))?\s+"
        r"at\s+(?P<page>\d+)",
        re.IGNORECASE
    ),
    
    "restatement_short": re.compile(
        r"Restatement\s+"
        r"(?:\((?P<edition>First|Second|Third|Fourth)\)\s+)?"
        r"§+\s*(?P<section>[\d.]+)",
        re.IGNORECASE
    ),
}

# Patterns to exclude from short form detection (known false positives)
_EXCLUSION_PATTERNS: Final = [
    re.compile(r"\bU\.S\.C\.\s+§", re.IGNORECASE),  # U.S. Code
    re.compile(r"\b[A-Z][a-z]+\.\s+(?:Civ\.|Penal|Bus\.|Fam\.|Health|Gov't)\s+Code\s+§", re.IGNORECASE),  # State codes
    re.compile(r"\bC\.F\.R\.\s+§", re.IGNORECASE),  # Code of Federal Regulations
    re.compile(r"\bStat\.\s+\d+", re.IGNORECASE),  # Statutes at Large
    re.compile(r"\bPub\.\s*L\.", re.IGNORECASE),  # Public Law
    re.compile(r"\bComp\.\s+Laws", re.IGNORECASE),  # Compiled Laws
    re.compile(r"\bRev\.\s+Stat", re.IGNORECASE),  # Revised Statutes
    re.compile(r"\bGen\.\s+Stat", re.IGNORECASE),  # General Statutes
    re.compile(r"\bAnn\.\s+Code", re.IGNORECASE),  # Annotated Code
]


@dataclass(frozen=True)
class SecondaryCitation:
    """Represents a citation to a secondary legal source.
    
    Attributes:
        source_type: Type of secondary source (e.g., 'cjs', 'amjur', 'alr').
        citation_category: Category: 'full', 'short', 'id', 'ibid', 'supra'.
        matched_text: The full citation text as it appeared in the document.
        span: Tuple of (start, end) positions in the document.
        volume: Volume number.
        title: Title or subject matter.
        section: Section number.
        page: Page number (for ALR and similar).
        pin_cite: Pin cite for specific page/section reference.
        year: Publication year.
        edition: Edition information.
        series: Series information (e.g., for ALR).
        author: Author name (for treatises).
        antecedent_key: Resource key of the full citation this short refers to.
    """
    source_type: str
    citation_category: str
    matched_text: str
    span: Tuple[int, int]
    volume: str | None = None
    title: str | None = None
    section: str | None = None
    page: str | None = None
    pin_cite: str | None = None
    year: str | None = None
    edition: str | None = None
    series: str | None = None
    author: str | None = None
    antecedent_key: str | None = None

    def to_normalized_citation(self) -> str:
        """Generate a normalized Bluebook-style citation string."""
        parts = []
        
        if self.source_type == "cjs":
            parts.append(f"{self.volume} C.J.S.")
            if self.title:
                parts.append(self.title)
            if self.section:
                parts.append(f"§ {self.section}")
            if self.year:
                parts.append(f"({self.year})")
                
        elif self.source_type == "amjur":
            parts.append(f"{self.volume} Am. Jur.")
            if self.edition:
                parts.append(self.edition)
            if self.title:
                parts.append(self.title)
            if self.section:
                parts.append(f"§ {self.section}")
            if self.year:
                parts.append(f"({self.year})")
                
        elif self.source_type == "alr":
            parts.append(f"{self.volume} A.L.R.")
            if self.series:
                parts.append(self.series)
            if self.page:
                parts.append(self.page)
            if self.year:
                parts.append(f"({self.year})")
                
        elif self.source_type == "restatement":
            parts.append("Restatement")
            if self.edition:
                parts.append(f"({self.edition})")
            if self.title:
                parts.append(f"of {self.title}")
            if self.section:
                parts.append(f"§ {self.section}")
            if self.year:
                parts.append(f"({self.year})")
                
        elif self.source_type == "treatise":
            if self.author:
                parts.append(f"{self.author},")
            if self.title:
                parts.append(self.title)
            if self.section:
                parts.append(f"§ {self.section}")
            edition_parts = []
            if self.edition:
                edition_parts.append(self.edition)
            if self.year:
                edition_parts.append(self.year)
            if edition_parts:
                parts.append(f"({' '.join(edition_parts)})")
        
        # Add pin cite if present
        if self.pin_cite and self.citation_category != "full":
            parts.append(f"at {self.pin_cite}")
        
        return " ".join(parts)

    def to_resource_key(self) -> str:
        """Generate a unique resource key for grouping."""
        key_parts = [
            "secondary",
            self.source_type,
            self.volume or "",
            self.title or self.author or "",
            self.section or self.page or "",
        ]
        return "::".join(clean_str(p) or "unknown" for p in key_parts)


class SecondaryCitationDetector:
    """Detects citations to secondary legal sources in text."""

    def detect_secondary_citations(
        self, text: str, eyecite_spans: Set[Tuple[int, int]] | None = None
    ) -> Tuple[List[SecondaryCitation], List[SecondaryCitation]]:
        """Detect all secondary source citations in the given text.
        
        Args:
            text: The document text to analyze.
            eyecite_spans: Set of (start, end) spans already detected by eyecite
                          to avoid duplicate detection.
            
        Returns:
            Tuple of (full_citations, short_citations) where:
            - full_citations: List of full SecondaryCitation objects
            - short_citations: List of short form SecondaryCitation objects
        """
        eyecite_spans = eyecite_spans or set()
        
        full_citations: List[SecondaryCitation] = []
        short_citations: List[SecondaryCitation] = []
        
        # Detect full citations first
        for source_type, pattern in _SECONDARY_PATTERNS.items():
            for match in pattern.finditer(text):
                match_span = (match.start(), match.end())
                
                # Skip if overlaps with eyecite detection
                if self._overlaps_with_eyecite(match_span, eyecite_spans):
                    logger.info(
                        "Skipping secondary detection at %s - already detected by eyecite",
                        match_span,
                    )
                    continue
                
                # Check for exclusion patterns
                if self._matches_exclusion(match.group(0)):
                    logger.info(
                        "Skipping false positive: %s",
                        match.group(0)[:50],
                    )
                    continue
                
                try:
                    citation = self._create_full_citation(
                        source_type, match, text
                    )
                    if citation:
                        full_citations.append(citation)
                        logger.info(
                            "Detected full %s citation: %s at span %s",
                            source_type,
                            citation.matched_text[:50],
                            citation.span,
                        )
                except Exception as exc:
                    logger.error(
                        "Failed to parse %s citation at position %d: %s",
                        source_type,
                        match.start(),
                        exc,
                    )
        
        # Detect short form citations
        for short_type, pattern in _SHORT_FORM_PATTERNS.items():
            for match in pattern.finditer(text):
                match_span = (match.start(), match.end())
                
                # Skip if overlaps with eyecite detection
                if self._overlaps_with_eyecite(match_span, eyecite_spans):
                    continue
                
                # Skip if overlaps with a full secondary citation
                if self._overlaps_with_full(match_span, full_citations):
                    continue
                
                # Check for exclusion patterns
                if self._matches_exclusion(match.group(0)):
                    continue
                
                try:
                    citation = self._create_short_citation(
                        short_type, match, text
                    )
                    if citation:
                        short_citations.append(citation)
                        logger.info(
                            "Detected short %s citation: %s at span %s",
                            short_type,
                            citation.matched_text,
                            citation.span,
                        )
                except Exception as exc:
                    logger.error(
                        "Failed to parse short %s citation at position %d: %s",
                        short_type,
                        match.start(),
                        exc,
                    )
        
        # Sort by position in document
        full_citations.sort(key=lambda c: c.span[0])
        short_citations.sort(key=lambda c: c.span[0])
        
        return full_citations, short_citations

    def _matches_exclusion(self, text: str) -> bool:
        """Check if text matches any exclusion pattern."""
        for pattern in _EXCLUSION_PATTERNS:
            if pattern.search(text):
                return True
        return False

    def _overlaps_with_eyecite(
        self, span: Tuple[int, int], eyecite_spans: Set[Tuple[int, int]]
    ) -> bool:
        """Check if a span overlaps with any eyecite-detected citation."""
        start, end = span
        for eyecite_start, eyecite_end in eyecite_spans:
            # Check for any overlap
            if not (end <= eyecite_start or start >= eyecite_end):
                return True
        return False

    def _overlaps_with_full(
        self, span: Tuple[int, int], full_citations: List[SecondaryCitation]
    ) -> bool:
        """Check if a span overlaps with any full citation."""
        start, end = span
        for full in full_citations:
            full_start, full_end = full.span
            if not (end <= full_start or start >= full_end):
                return True
        return False

    def _create_full_citation(
        self, source_type: str, match: re.Match, text: str
    ) -> SecondaryCitation | None:
        """Create a SecondaryCitation from a full citation regex match."""
        groups = match.groupdict()
        
        # Clean all extracted values
        cleaned = {
            key: clean_str(value) for key, value in groups.items()
        }
        
        return SecondaryCitation(
            source_type=source_type,
            citation_category="full",
            matched_text=match.group(0),
            span=(match.start(), match.end()),
            volume=cleaned.get("volume"),
            title=cleaned.get("title") or cleaned.get("subject"),
            section=cleaned.get("section"),
            page=cleaned.get("page"),
            pin_cite=None,  # Full citations don't have pin cites
            year=cleaned.get("year"),
            edition=cleaned.get("edition"),
            series=cleaned.get("series"),
            author=cleaned.get("author"),
            antecedent_key=None,
        )

    def _create_short_citation(
        self, short_type: str, match: re.Match, text: str
    ) -> SecondaryCitation | None:
        """Create a SecondaryCitation from a short form regex match."""
        groups = match.groupdict()
        
        # Clean all extracted values
        cleaned = {
            key: clean_str(value) for key, value in groups.items()
        }
        
        # Determine source type and category from short_type
        category_map = {
            "id": "id",
            "ibid": "ibid",
            "supra": "supra",
            "cjs_short": "short",
            "amjur_short": "short",
            "alr_short": "short",
            "restatement_short": "short",
        }
        
        source_type_map = {
            "id": "unknown",  # Will be resolved later
            "ibid": "unknown",
            "supra": "unknown",
            "cjs_short": "cjs",
            "amjur_short": "amjur",
            "alr_short": "alr",
            "restatement_short": "restatement",
        }
        
        citation_category = category_map.get(short_type, "short")
        source_type = source_type_map.get(short_type, "unknown")
        
        return SecondaryCitation(
            source_type=source_type,
            citation_category=citation_category,
            matched_text=match.group(0),
            span=(match.start(), match.end()),
            volume=cleaned.get("volume"),
            title=cleaned.get("title_fragment"),
            section=cleaned.get("section"),
            page=cleaned.get("page"),
            pin_cite=cleaned.get("pin_cite"),
            year=None,
            edition=cleaned.get("edition"),
            series=cleaned.get("series"),
            author=cleaned.get("author"),
            antecedent_key=None,  # Will be resolved in next step
        )


# Around line 275, update the SecondaryCitationResolver class:

class SecondaryCitationResolver:
    """Resolves short form citations to their antecedent full citations."""

    def resolve_short_citations(
        self,
        full_citations: List[SecondaryCitation],
        short_citations: List[SecondaryCitation],
        all_citation_spans: List[Tuple[int, int, str, Any]] | None = None,
    ) -> List[SecondaryCitation]:
        """Resolve short citations to their full citation antecedents.
        
        Args:
            full_citations: List of full SecondaryCitation objects.
            short_citations: List of short SecondaryCitation objects.
            all_citation_spans: List of (start, end, type, cite_obj) tuples for ALL
                               citations in the document (including eyecite citations)
                               to properly resolve Id. citations. The cite_obj is the
                               SecondaryCitation object for secondary sources, or None
                               for other citation types.
            
        Returns:
            Updated list of short citations with antecedent_key populated.
        """
        if not short_citations:
            return short_citations
        
        all_citation_spans = all_citation_spans or []
        
        # Build index of full citations by resource key
        full_by_key: Dict[str, SecondaryCitation] = {}
        for full in full_citations:
            key = full.to_resource_key()
            full_by_key[key] = full
        
        resolved_shorts: List[SecondaryCitation] = []
        
        for short in short_citations:
            resolved = self._resolve_short(
                short,
                full_citations,
                full_by_key,
                all_citation_spans,
            )
            resolved_shorts.append(resolved)
        
        return resolved_shorts

    def _resolve_short(
        self,
        short: SecondaryCitation,
        full_citations: List[SecondaryCitation],
        full_by_key: Dict[str, SecondaryCitation],
        all_citation_spans: List[Tuple[int, int, str, Any]],
    ) -> SecondaryCitation:
        """Resolve a single short citation to its antecedent.
        
        Args:
            short: The short citation to resolve.
            full_citations: List of all full secondary citations.
            full_by_key: Dict mapping resource keys to full citations.
            all_citation_spans: List of (start, end, type, cite_obj) tuples for
                               ALL citations.
            
        Returns:
            Updated SecondaryCitation with antecedent_key set.
        """
        # Id. and Ibid. refer to the immediately preceding citation
        if short.citation_category in ("id", "ibid"):
            # Find the immediately preceding citation of ANY type
            preceding = self._find_preceding_citation(
                short.span[0], all_citation_spans, full_citations
            )
            
            if preceding and preceding[2] == "secondary":
                # The preceding citation is a secondary source - resolve to it
                secondary_cite = preceding[3]
                if isinstance(secondary_cite, SecondaryCitation):
                    return replace(
                        short,
                        antecedent_key=secondary_cite.to_resource_key(),
                        source_type=secondary_cite.source_type,
                    )
            else:
                # The preceding citation is NOT a secondary source
                # Don't resolve - this Id. refers to a case/statute/journal
                logger.info(
                    "Id. at position %d refers to non-secondary citation, skipping",
                    short.span[0],
                )
                return replace(short, source_type="non_secondary")
            
            logger.error(
                "Found %s at position %d but no preceding citation",
                short.citation_category,
                short.span[0],
            )
            return short
        
        # Supra references need title matching
        if short.citation_category == "supra":
            antecedent = self._find_supra_antecedent(
                short, full_citations, full_by_key
            )
            if antecedent:
                return replace(
                    short,
                    antecedent_key=antecedent.to_resource_key(),
                    source_type=antecedent.source_type,
                )
            else:
                logger.error(
                    "Could not resolve supra citation at position %d: %s",
                    short.span[0],
                    short.matched_text,
                )
                return short
        
        # Short forms with volume/section - match to most recent compatible full
        if short.citation_category == "short":
            antecedent = self._find_short_antecedent(
                short, full_citations, full_by_key
            )
            if antecedent:
                # Merge information from antecedent
                return replace(
                    short,
                    antecedent_key=antecedent.to_resource_key(),
                    source_type=antecedent.source_type,
                    title=short.title or antecedent.title,
                    year=short.year or antecedent.year,
                    edition=short.edition or antecedent.edition,
                )
            else:
                logger.error(
                    "Could not resolve short citation at position %d: %s",
                    short.span[0],
                    short.matched_text,
                )
                return short
        
        return short

    def _find_preceding_citation(
        self,
        position: int,
        all_citation_spans: List[Tuple[int, int, str, Any]],
        full_citations: List[SecondaryCitation],
    ) -> Tuple[int, int, str, Any] | None:
        """Find the immediately preceding citation of any type.
        
        Args:
            position: Position of the Id. citation.
            all_citation_spans: List of (start, end, type, cite_obj) tuples where
                               cite_obj is the SecondaryCitation for secondary sources
                               or None for other types.
            full_citations: List of full secondary citations (unused, kept for
                           compatibility).
            
        Returns:
            Tuple of (start, end, type, cite_obj) for preceding citation, or None.
        """
        # Filter to citations before this position
        preceding = [
            span for span in all_citation_spans
            if span[1] <= position  # end <= position
        ]
        
        if not preceding:
            return None
        
        # Sort by end position and get the most recent
        preceding.sort(key=lambda s: s[1], reverse=True)
        return preceding[0]

    def _find_supra_antecedent(
        self,
        short: SecondaryCitation,
        full_citations: List[SecondaryCitation],
        full_by_key: Dict[str, SecondaryCitation],
    ) -> SecondaryCitation | None:
        """Find antecedent for supra citation by title matching."""
        if not short.title:
            return None
        
        # Normalize title for comparison
        from utils.cleaner import normalize_case_name_for_compare
        short_title_norm = normalize_case_name_for_compare(short.title)
        
        # Search backwards from short citation position
        candidates = [
            f for f in full_citations
            if f.span[0] < short.span[0]
        ]
        candidates.sort(key=lambda c: c.span[0], reverse=True)
        
        for candidate in candidates:
            if not candidate.title:
                continue
            
            candidate_title_norm = normalize_case_name_for_compare(
                candidate.title
            )
            
            if not (short_title_norm and candidate_title_norm):
                continue
            
            # Check for substring match (handles partial titles in supra)
            if (short_title_norm in candidate_title_norm or
                candidate_title_norm in short_title_norm):
                return candidate
        
        return None

    def _find_short_antecedent(
        self,
        short: SecondaryCitation,
        full_citations: List[SecondaryCitation],
        full_by_key: Dict[str, SecondaryCitation],
    ) -> SecondaryCitation | None:
        """Find antecedent for short citation by matching fields."""
        # Search backwards from short citation position
        candidates = [
            f for f in full_citations
            if f.span[0] < short.span[0]
            and f.source_type == short.source_type
        ]
        candidates.sort(key=lambda c: c.span[0], reverse=True)
        
        # Match on volume and compatible section/page
        for candidate in candidates:
            # Volume must match
            if short.volume and candidate.volume:
                if short.volume != candidate.volume:
                    continue
            
            # For same-volume cites, assume it's a reference
            # Additional logic could check section proximity
            return candidate
        
        return None


__all__ = [
    "SecondaryCitation",
    "SecondaryCitationDetector",
    "SecondaryCitationResolver",
]
