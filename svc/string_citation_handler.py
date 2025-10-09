# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

"""String citation detection and splitting for legal documents.

This module handles the decomposition of string citations (multiple citations
separated by semicolons) into individual citation segments that can be processed
independently by eyecite while maintaining their relationship and accurate spans.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, List, Tuple

from utils.logger import get_logger

logger = get_logger()

# Semicolon boundary pattern with lookahead for next citation start
_SEMICOLON_BOUNDARY: Final = re.compile(
    r';\s*(?='
    r'(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+v\.|'  # Case name (e.g., "Brown v.")
    r'In\s+re\s+[A-Z]|'  # In re citation
    r'\d+\s+[A-Z][\w.]+\s+[A-Z]|'  # Reporter (e.g., "347 U.S.")
    r'[A-Z][\w.]+\s*§|'  # Statute section
    r'id\.|supra|cf\.|see|compare|accord|contra|but)'  # Short forms/signals
    r')',
    re.MULTILINE | re.IGNORECASE
)

# Pattern to detect likely string citations
_STRING_CITATION_INDICATORS: Final = re.compile(
    r'(?:'
    r'(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+v\.[^;]{10,80};)|'  # Case + semicolon
    r'(?:\d+\s+[A-Z][\w.]+\s+\d+[^;]{0,80};)|'  # Reporter cite + semicolon
    r'(?:[A-Z][\w.]+\s*§\s*[\d.]+[^;]{0,60};)'  # Statute + semicolon
    r')',
    re.MULTILINE
)

# Signal words that often precede string citations
_SIGNAL_WORDS: Final = frozenset({
    'see', 'see also', 'see, e.g.', 'see generally',
    'cf.', 'compare', 'but see', 'but cf.',
    'accord', 'contra', 'e.g.',
})

# Patterns that should NOT be split (inside parentheticals)
_PROTECTED_CONTEXTS: Final = re.compile(
    r'\([^)]{0,150}\)',  # Content within parentheses
    re.MULTILINE
)


@dataclass(frozen=True)
class CitationSegment:
    """Represents a single citation extracted from a string citation.

    Attributes:
        text: The citation text content.
        original_span: Tuple of (start, end) positions in original document.
        string_group_id: Identifier linking citations from the same string.
                        None for standalone citations.
        position_in_string: Order within the string (0-indexed).
                           None for standalone citations.
        has_semicolon_boundary: Whether this segment was separated by semicolon.
    """

    text: str
    original_span: Tuple[int, int]
    string_group_id: str | None
    position_in_string: int | None
    has_semicolon_boundary: bool


class StringCitationDetector:
    """Identifies string citations in legal text.

    A string citation is multiple related citations separated by semicolons,
    typically appearing in a single sentence or clause.

    Example:
        "Brown v. Board, 347 U.S. 483 (1954); Roe v. Wade, 410 U.S. 113 (1973)."
    """

    def __init__(self, min_semicolons: int = 1) -> None:
        """Initialize the detector.

        Args:
            min_semicolons: Minimum number of citation-separating semicolons
                           required to classify as a string citation.
        """
        self._min_semicolons = min_semicolons

    def detect_string_citations(
        self, text: str
    ) -> List[Tuple[int, int, bool]]:
        """Identify spans containing string citations in the text.

        Args:
            text: The document text to analyze.

        Returns:
            List of (start_pos, end_pos, is_string) tuples where is_string
            indicates whether the span contains a string citation.
        """
        if not text or len(text.strip()) == 0:
            return []

        # Find all potential string citation candidates
        candidates: List[Tuple[int, int]] = []

        # Look for sentence-level spans with multiple semicolons
        sentences = self._split_into_sentences(text)

        for sentence_start, sentence_end in sentences:
            sentence_text = text[sentence_start:sentence_end]

            if self.is_likely_string_citation(sentence_text):
                candidates.append((sentence_start, sentence_end))

        # Return spans with classification
        results: List[Tuple[int, int, bool]] = []
        for start, end in candidates:
            results.append((start, end, True))

        return results

    def is_likely_string_citation(self, text_segment: str) -> bool:
        """Heuristic check if segment contains a string citation.

        Checks for:
        - Multiple semicolons with citation-like patterns around them
        - Signal words followed by multiple citations
        - At least two distinct citation patterns

        Args:
            text_segment: Text segment to analyze.

        Returns:
            True if the segment likely contains a string citation.
        """
        if not text_segment or len(text_segment.strip()) < 20:
            return False

        # Count semicolons that are citation boundaries (not in parentheticals)
        protected_ranges = self._get_protected_ranges(text_segment)
        semicolons = self._count_boundary_semicolons(
            text_segment, protected_ranges
        )

        if semicolons < self._min_semicolons:
            return False

        # Look for citation patterns around semicolons
        indicators = _STRING_CITATION_INDICATORS.findall(text_segment)

        if len(indicators) >= 2:
            return True

        # Check for signal words followed by multiple citations
        lower_text = text_segment.lower()
        for signal in _SIGNAL_WORDS:
            if signal in lower_text and semicolons >= 1:
                # Signal word + at least one semicolon suggests string
                return True

        return False

    def _split_into_sentences(self, text: str) -> List[Tuple[int, int]]:
        """Split text into sentence-like spans for analysis.

        Focuses on citation-heavy regions rather than grammatical sentences.

        Args:
            text: Input text.

        Returns:
            List of (start, end) tuples marking sentence boundaries.
        """
        # Simple sentence boundary detection
        # Period/semicolon followed by capital or end-of-text
        sentence_pattern = re.compile(
            r'[.;]\s*(?=[A-Z]|\s*$)', re.MULTILINE
        )

        sentences: List[Tuple[int, int]] = []
        start = 0

        for match in sentence_pattern.finditer(text):
            end = match.end()
            if end > start:
                sentences.append((start, end))
            start = end

        # Add final segment if exists
        if start < len(text):
            sentences.append((start, len(text)))

        return sentences

    def _get_protected_ranges(self, text: str) -> List[Tuple[int, int]]:
        """Find ranges that should not be split (e.g., parentheticals).

        Args:
            text: Text to analyze.

        Returns:
            List of (start, end) tuples marking protected ranges.
        """
        protected: List[Tuple[int, int]] = []

        for match in _PROTECTED_CONTEXTS.finditer(text):
            protected.append((match.start(), match.end()))

        return protected

    def _count_boundary_semicolons(
        self, text: str, protected_ranges: List[Tuple[int, int]]
    ) -> int:
        """Count semicolons that serve as citation boundaries.

        Excludes semicolons within parentheticals.

        Args:
            text: Text to analyze.
            protected_ranges: Ranges to exclude from counting.

        Returns:
            Number of boundary semicolons.
        """
        count = 0

        for match in _SEMICOLON_BOUNDARY.finditer(text):
            pos = match.start()

            # Check if this semicolon is in a protected range
            is_protected = any(
                start <= pos < end for start, end in protected_ranges
            )

            if not is_protected:
                count += 1

        return count


class StringCitationSplitter:
    """Splits string citations into individual citation segments.

    Handles edge cases like:
    - Parallel citations (same case, multiple reporters)
    - Parentheticals with semicolons
    - Pin cites and page ranges
    - Short forms (id., supra) within strings
    """

    def split_string_citation(
        self,
        text: str,
        original_start_offset: int,
        string_group_id: str,
    ) -> List[CitationSegment]:
        """Split a string citation into individual citations.

        Args:
            text: The string citation text to split.
            original_start_offset: Position of this text in the original document.
            string_group_id: Unique identifier for this string group.

        Returns:
            List of CitationSegment objects representing individual citations.

        Raises:
            ValueError: If text is empty or invalid.
        """
        if not text or len(text.strip()) == 0:
            logger.error("Cannot split empty string citation text")
            raise ValueError("String citation text cannot be empty")

        # Get protected ranges (parentheticals, etc.)
        protected_ranges = self._get_protected_ranges(text)

        # Split on semicolons that are citation boundaries
        parts = self._smart_split_on_semicolons(text, protected_ranges)

        if not parts:
            logger.warning(
                "No citation parts found in text: %s", text[:100]
            )
            return []

        segments: List[CitationSegment] = []
        current_offset = 0

        for i, part in enumerate(parts):
            part_stripped = part.strip()

            if not part_stripped:
                # Empty segment, skip but account for length
                current_offset += len(part)
                continue

            # Find where this part starts in the original text
            part_start = text.find(part_stripped, current_offset)

            if part_start == -1:
                # Fallback: use current offset
                part_start = current_offset

            part_end = part_start + len(part_stripped)

            # Calculate absolute position in document
            absolute_start = original_start_offset + part_start
            absolute_end = original_start_offset + part_end

            segments.append(
                CitationSegment(
                    text=part_stripped,
                    original_span=(absolute_start, absolute_end),
                    string_group_id=string_group_id,
                    position_in_string=i,
                    has_semicolon_boundary=True,
                )
            )

            # Move offset to end of this part
            current_offset = part_end

        logger.info(
            "Split string citation into %d segments (group_id=%s)",
            len(segments),
            string_group_id,
        )

        return segments

    def _smart_split_on_semicolons(
        self, text: str, protected_ranges: List[Tuple[int, int]]
    ) -> List[str]:
        """Split text on semicolons, respecting protected contexts.

        Args:
            text: Text to split.
            protected_ranges: Ranges that should not be split.

        Returns:
            List of text segments.
        """
        parts: List[str] = []
        current_start = 0

        for match in _SEMICOLON_BOUNDARY.finditer(text):
            semicolon_pos = match.start()

            # Check if semicolon is protected
            is_protected = any(
                start <= semicolon_pos < end
                for start, end in protected_ranges
            )

            if is_protected:
                continue

            # Extract segment up to semicolon
            segment = text[current_start:semicolon_pos]
            parts.append(segment)

            # Move past the semicolon and any whitespace
            current_start = match.end()

        # Add final segment
        if current_start < len(text):
            final_segment = text[current_start:]
            if final_segment.strip():
                parts.append(final_segment)

        return parts

    def _get_protected_ranges(self, text: str) -> List[Tuple[int, int]]:
        """Find ranges that should not be split.

        Args:
            text: Text to analyze.

        Returns:
            List of (start, end) tuples.
        """
        protected: List[Tuple[int, int]] = []

        # Protect parenthetical content
        paren_depth = 0
        paren_start = -1

        for i, char in enumerate(text):
            if char == '(':
                if paren_depth == 0:
                    paren_start = i
                paren_depth += 1
            elif char == ')':
                paren_depth -= 1
                if paren_depth == 0 and paren_start >= 0:
                    protected.append((paren_start, i + 1))
                    paren_start = -1

        return protected


def create_standalone_segment(
    text: str, start: int, end: int
) -> CitationSegment:
    """Create a CitationSegment for a non-string citation.

    Args:
        text: Citation text.
        start: Start position in document.
        end: End position in document.

    Returns:
        CitationSegment with no string grouping.
    """
    return CitationSegment(
        text=text,
        original_span=(start, end),
        string_group_id=None,
        position_in_string=None,
        has_semicolon_boundary=False,
    )


__all__ = [
    'CitationSegment',
    'StringCitationDetector',
    'StringCitationSplitter',
    'create_standalone_segment',
]