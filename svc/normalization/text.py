# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""Conservative text normalization and matching (plan section 6).

`normalize_for_match` is a separate, deterministic comparison form; it never replaces a block's
raw_text. Exactly these operations are applied, in this order:

  1. line endings become "\\n";
  2. Unicode NFKC (which also turns nonbreaking and other exotic spaces into ordinary spaces);
  3. soft hyphens (U+00AD) are removed;
  4. runs of horizontal whitespace collapse to one space;
  5. whitespace at the block boundaries is trimmed.

Nothing else is touched: section and paragraph signs, periods in abbreviations, en dashes, quotation
marks and apostrophes, brackets, citation signals and visible hyphens all survive, and words hyphenated
at a PDF line end are not rejoined. Every output character keeps the raw span it came from, so a match
found in the comparison form maps back to exact raw offsets.

Matching adds one documented tolerance: any whitespace run in the needle (including a line break)
matches any whitespace run in the text. A match that needed it or the normalization above is a
NORMALIZED_MATCH; a verbatim substring of raw_text is an EXACT_MATCH.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple

_SOFT_HYPHEN = "­"
_LINE_SEPARATORS = {" ", " ", "\x85"}
_COMBINING = ("Mn", "Mc", "Me")


@dataclass(frozen=True)
class MatchText:
    """The comparison form of a raw string, with the raw span behind every character."""

    text: str
    starts: List[int]
    ends: List[int]

    def raw_span(self, start: int, end: int) -> Tuple[int, int]:
        """Raw offsets covering the normalized range [start, end)."""
        return self.starts[start], self.ends[end - 1]


def normalize_for_match(raw: str) -> MatchText:
    chars: List[str] = []
    starts: List[int] = []
    ends: List[int] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "\r":
            j = i + 2 if raw[i + 1:i + 2] == "\n" else i + 1
            chars.append("\n"), starts.append(i), ends.append(j)
            i = j
            continue
        if c in _LINE_SEPARATORS:
            chars.append("\n"), starts.append(i), ends.append(i + 1)
            i += 1
            continue
        j = i + 1
        while j < n and unicodedata.category(raw[j]) in _COMBINING:
            j += 1
        for ch in unicodedata.normalize("NFKC", raw[i:j]):
            if ch != _SOFT_HYPHEN:
                chars.append(ch), starts.append(i), ends.append(j)
        i = j

    out_chars: List[str] = []
    out_starts: List[int] = []
    out_ends: List[int] = []
    previous_space = False
    for ch, start, end in zip(chars, starts, ends):
        if ch != "\n" and ch.isspace():
            if previous_space:
                out_ends[-1] = end
                continue
            out_chars.append(" "), out_starts.append(start), out_ends.append(end)
            previous_space = True
        else:
            out_chars.append(ch), out_starts.append(start), out_ends.append(end)
            previous_space = False

    lo, hi = 0, len(out_chars)
    while lo < hi and out_chars[lo].isspace():
        lo += 1
    while hi > lo and out_chars[hi - 1].isspace():
        hi -= 1
    return MatchText("".join(out_chars[lo:hi]), out_starts[lo:hi], out_ends[lo:hi])


def flexible_pattern(needle: str) -> re.Pattern[str] | None:
    """`needle` with every whitespace run matching any whitespace run (line breaks included)."""
    tokens = needle.split()
    if not tokens:
        return None
    return re.compile(r"\s+".join(re.escape(token) for token in tokens))


@dataclass(frozen=True)
class Occurrence:
    start: int  # raw offsets
    end: int
    kind: str  # "exact" | "normalized"


def find_occurrences(needle: str, raw: str, match: MatchText) -> List[Occurrence]:
    """Where `needle` occurs in a block: verbatim in raw_text first, else in the comparison form."""
    needle = needle.strip()
    if not needle:
        return []
    found: List[Occurrence] = []
    position = raw.find(needle)
    while position != -1:
        found.append(Occurrence(position, position + len(needle), "exact"))
        position = raw.find(needle, position + 1)
    if found:
        return found
    pattern = flexible_pattern(normalize_for_match(needle).text)
    if pattern is None:
        return []
    for m in pattern.finditer(match.text):
        start, end = match.raw_span(m.start(), m.end())
        found.append(Occurrence(start, end, "normalized"))
    return found


# --- markup the model may copy from the tagged text ----------------------------------------

_MARKUP_RE = re.compile(
    r"</?(?:i|b|u|sup|sub|sc|footnote-ref|endnote-ref|footnote|endnote|paragraph|cell|row|table|textbox|document)\b[^>]*>",
    re.IGNORECASE,
)
_ESCAPE_AMPERSAND = re.compile(r"&(?=(?:lt|gt|amp);)")


def strip_inline_markup(value: str) -> Tuple[str, bool]:
    """`value` without the tagged text's tags (the model is told not to copy them); and whether any were there."""
    stripped = _MARKUP_RE.sub("", value)
    return stripped, stripped != value


def escape_delimiters(text: str) -> str:
    """Escape "<" and ">" in document text so they can't be mistaken for the tags around it.

    "&" is escaped only where it would make the round trip ambiguous ("&lt;" written in the document).
    """
    return _ESCAPE_AMPERSAND.sub("&amp;", text).replace("<", "&lt;").replace(">", "&gt;")


def unescape_delimiters(text: str) -> str:
    """Inverse of escape_delimiters, applied to strings the model returns."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
