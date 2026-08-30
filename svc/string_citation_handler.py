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

# Shared "volume + multi-word reporter" fragment: 1-3 capitalized chunks
# (each may contain internal periods, e.g. "Fed.", "Reg.", "F.2d") between
# the volume number and the page number, covering both single-word reporters
# ("U.S.", "F.2d") and multi-word ones ("Fed. Reg.", "F. Supp.").
_REPORTER = r'[A-Z][\w.]*(?:\s+[A-Z][\w.]*){0,2}'

# Semicolon boundary pattern with lookahead for next citation start
_SEMICOLON_BOUNDARY: Final = re.compile(
    r';\s*(?='
    r'(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+v\.|'  # Case name (e.g., "Brown v.")
    r'In\s+re\s+[A-Z]|'  # In re citation
    rf'\d+\s+{_REPORTER}\s+[A-Z0-9]|'  # Reporter (e.g., "347 U.S." or "66 Fed. Reg.")
    r'[A-Z][\w.]+\s*§|'  # Statute section
    r'\d+\s+[A-Z][\w.]*\s*§|'  # Numbered statute (e.g., "18 U.S.C. §")
    r"[A-Z][A-Za-z.']+(?:\s+[A-Z][A-Za-z.']+)*,\s*\d+\s+[A-Z]|"  # Title, ## Reporter
    r'id\.|supra|cf\.|see|compare|accord|contra|but)'  # Short forms/signals
    r')',
    re.MULTILINE | re.IGNORECASE
)

# Pattern to detect likely string citations. Alternatives are NOT anchored to
# a trailing ";" so the final citation in a string (which has no semicolon
# after it) still counts as an indicator.
_STRING_CITATION_INDICATORS: Final = re.compile(
    r'(?:'
    r'(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+v\.[^;]{10,80})|'  # Case citation
    rf'(?:\d+\s+{_REPORTER}\s+\d+[^;]{{0,80}})|'  # Reporter citation
    r'(?:[A-Z][\w.]+\s*§\s*[\d.]+[^;]{0,60})|'  # Statute citation
    rf"(?:[A-Z][A-Za-z.']+(?:\s+[A-Z][A-Za-z.']+)*,\s*\d+\s+{_REPORTER}\s+\d+[^;]{{0,80}})"  # Title-leading citation
    r')',
    re.MULTILINE
)

# Signal words that often precede string citations
_SIGNAL_WORDS: Final = frozenset({
    'see', 'see also', 'see, e.g.', 'see generally',
    'cf.', 'compare', 'but see', 'but cf.',
    'accord', 'contra', 'e.g.',
})

# Compiled signal-word matcher with letter-boundary guards, so "lessee"
# doesn't match the substring "see" (can't use \b adjacent to periods in
# words like "cf.", so boundaries are asserted explicitly).
_SIGNAL_RE: Final = re.compile(
    r'(?<![A-Za-z])(?:'
    + '|'.join(re.escape(s) for s in sorted(_SIGNAL_WORDS, key=len, reverse=True))
    + r')(?![A-Za-z])',
    re.IGNORECASE,
)

# Small, closed set of universal lowercase Latin/legal abbreviations that
# aren't proper-noun-shaped (so the sentence splitter's "starts uppercase"
# rule can't catch them) but must never be treated as ending a sentence.
# Deliberately NOT a list of party-name/journal abbreviations -- those are
# open-ended and are instead covered by the "starts uppercase" rule.
_LOWERCASE_ABBREVIATIONS: Final = frozenset({"cf", "etc", "vs"})

# Candidate sentence-split point: a period followed by one or more
# whitespace characters, then a capital letter or digit. Requiring actual
# whitespace (not \s*) keeps "Educ., 347" / "Co., 927" from ever being
# candidates, since a comma follows the period directly.
_SENTENCE_BOUNDARY: Final = re.compile(r'\.(\s+)(?=[A-Z0-9])')

# Word token immediately preceding a candidate split point.
_PRECEDING_TOKEN: Final = re.compile(r"([\w'’]+)$")

# Paragraph breaks are always genuine sentence/segment boundaries.
_PARAGRAPH_BREAK: Final = re.compile(r'\n\s*\n+')

# Defensive cap on candidate-segment length passed to is_likely_string_citation.
# With guarded splitting, real sentences stay far below this; it only guards
# against a pathological no-period document (e.g. OCR junk) being treated as
# one giant string-citation candidate.
_MAX_SEGMENT_LENGTH: Final = 1500

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

        if len(text_segment) > _MAX_SEGMENT_LENGTH:
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
        if semicolons >= 1 and _SIGNAL_RE.search(text_segment):
            # Signal word + at least one semicolon suggests string
            return True

        return False

    def _split_into_sentences(self, text: str) -> List[Tuple[int, int]]:
        """Split text into sentence-like spans for analysis.

        Focuses on citation-heavy regions rather than grammatical sentences.
        Deliberately biased toward keeping text together when uncertain:
        over-merging only risks a spurious string_group_id on harmless
        trailing text (still processed correctly by eyecite downstream),
        while over-fragmenting is what breaks string-citation detection
        entirely (the bug this heuristic exists to avoid). Never splits on
        ";" -- semicolons are the string-citation-internal delimiter and
        must stay inside one candidate span for is_likely_string_citation
        and StringCitationSplitter to see them.

        Args:
            text: Input text.

        Returns:
            List of (start, end) tuples marking sentence boundaries.
        """
        cut_points: List[int] = []

        for match in _PARAGRAPH_BREAK.finditer(text):
            cut_points.append(match.end())

        for match in _SENTENCE_BOUNDARY.finditer(text):
            lookahead_char = text[match.end()] if match.end() < len(text) else ""
            if lookahead_char.isdigit():
                # Bluebook abbreviations are routinely followed by numbers
                # ("ch. 5", "Cal. 3d", "Jan. 5", "no. 2"); a real sentence
                # starting with a bare digit is vanishingly rare here.
                continue

            preceding_text = text[:match.start()]
            token_match = _PRECEDING_TOKEN.search(preceding_text)
            token = token_match.group(1) if token_match else ""

            if token == "":
                # Period directly follows ")", '"', etc. -- a genuine
                # citation-ending boundary (e.g. "(1954).").
                cut_points.append(match.end())
                continue

            last_component = token.rstrip(".").split(".")[-1]
            if len(last_component) <= 1:
                # Single-letter component: "v.", the "S" in "U.S", a
                # personal initial "B." -- never a sentence boundary.
                continue
            if token[0].isupper():
                # Short Title-Case token before a period is reliably an
                # abbreviation in citation-dense legal text (party name,
                # institution, journal, reporter, court -- "Pharm.",
                # "Hous.", "Fed.", "Cir.", "Cal.", "Mass.", "Educ.", "Co.",
                # "Inc."), whereas a genuine sentence-final word is
                # essentially always lowercase ("review.", "applies.",
                # "controls."). Generalizes to any abbreviation without a
                # maintained whitelist.
                continue
            if token.lower() in _LOWERCASE_ABBREVIATIONS:
                continue

            cut_points.append(match.end())

        sentences: List[Tuple[int, int]] = []
        start = 0
        for end in sorted(set(cut_points)):
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
            logger.error(
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
