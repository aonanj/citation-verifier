# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.
"""Where each block of a NormalizedDocument sits in the display text.

The results page highlights citations by offset into the text svc.doc_processor extracts, and attributes them to
footnotes through that extractor's NoteSpans. The normalized blocks come from another parser, so a citation
validated in a block has to be located in the display text again. Searching the whole text for a short string
("Id.") would land on the wrong occurrence as soon as the model missed one, so each block is first given a
window in the display text and the citation is searched only inside it:

  footnote / endnote   the NoteSpan whose body reads the same (order-preserving, so identical notes pair up in order);
  paragraph / cell     found sequentially in the text with the notes taken out (a note's body is inlined in the middle
                       of the paragraph that refers to it), the window running to the start of the next block;
  page                 found sequentially in the text, the window running to the start of the next page.

Comparison is on the conservative normalized form (svc.normalization.text) with its offset map, so nonbreaking spaces,
ligatures and line breaks do not defeat it. A block that can't be found simply gets no window and its citations are
searched for across the whole text, in document order, as the text extractor does today.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from rapidfuzz import fuzz

from svc.normalization.models import NormalizedDocument
from svc.normalization.text import MatchText, flexible_pattern, normalize_for_match

_ANCHOR_CHARS = 80
_MAIN_KINDS = ("paragraph", "table_cell")
_NOTE_KINDS = ("footnote", "endnote")


@dataclass(frozen=True)
class BlockWindow:
    start: int  # offsets into the display text
    end: int
    # Note bodies inlined inside [start, end): a main-text citation must not be found in one of them.
    reserved: Tuple[Tuple[int, int], ...] = ()


def _anchor(text: str, tail: bool = False) -> str:
    if len(text) <= _ANCHOR_CHARS:
        return text
    piece = text[-_ANCHOR_CHARS:] if tail else text[:_ANCHOR_CHARS]
    words = piece.split(" ")
    if len(words) > 2:
        words = words[1:] if tail else words[:-1]  # drop the token the cut may have split
    return " ".join(words)


class _Stream:
    """A normalized string with some index ranges removed, and the way back to normalized indices."""

    def __init__(self, text: str, removed: Sequence[Tuple[int, int]]) -> None:
        pieces: List[str] = []
        self.origins: List[Tuple[int, int]] = []  # (start in this stream, start in `text`)
        position = 0
        length = 0
        for start, end in sorted(removed):
            if start > position:
                self.origins.append((length, position))
                pieces.append(text[position:start])
                length += start - position
            position = max(position, end)
        if position < len(text):
            self.origins.append((length, position))
            pieces.append(text[position:])
        self.text = "".join(pieces)
        self._starts = [o[0] for o in self.origins]

    def to_source(self, index: int) -> int:
        piece = max(bisect_left(self._starts, index + 1) - 1, 0)
        stream_start, source_start = self.origins[piece]
        return source_start + (index - stream_start)


def _find_sequence(stream_text: str, blocks: Sequence[Tuple[int, str]]) -> Dict[int, Tuple[int, int]]:
    """{block index: (start, end) in stream_text} for the blocks found in order."""
    found: Dict[int, Tuple[int, int]] = {}
    cursor = 0
    for index, match_text in blocks:
        if not match_text:
            continue
        head = flexible_pattern(_anchor(match_text))
        if head is None:
            continue
        m = head.search(stream_text, cursor)
        if m is None:
            continue
        end = m.end()
        tail = flexible_pattern(_anchor(match_text, tail=True))
        t = tail.search(stream_text, m.start()) if tail is not None else None
        if t is not None and t.end() >= end:
            end = t.end()
        found[index] = (m.start(), end)
        cursor = end
    return found


def block_windows(document: NormalizedDocument, text: str, notes: Sequence[Any] | None) -> Dict[int, BlockWindow]:
    """{block index: BlockWindow} for every block that could be placed in `text`."""
    match: MatchText = normalize_for_match(text)
    note_spans = sorted(((n.start, n.end, n.kind) for n in notes or ()), key=lambda s: s[0])
    windows: Dict[int, BlockWindow] = {}

    def raw(start: int, end: int) -> Tuple[int, int]:
        return match.starts[start], match.ends[end - 1]

    # --- notes: the NoteSpan that reads the same, in order ---
    candidates: Dict[str, List[Tuple[int, int, str]]] = {kind: [] for kind in _NOTE_KINDS}
    for start, end, kind in note_spans:
        if kind in candidates:
            candidates[kind].append((start, end, normalize_for_match(text[start:end]).text))
    pointers = {kind: 0 for kind in _NOTE_KINDS}
    for i, block in enumerate(document.blocks):
        if block.kind not in _NOTE_KINDS:
            continue
        options, first = candidates[block.kind], pointers[block.kind]
        chosen = next((j for j in range(first, min(first + 6, len(options))) if options[j][2] == block.match_text), None)
        if chosen is None:
            scored = [(fuzz.ratio(options[j][2], block.match_text), j) for j in range(first, min(first + 3, len(options)))]
            best = max(scored, default=(0.0, None))
            chosen = best[1] if best[0] >= 90 else None
        if chosen is not None:
            windows[i] = BlockWindow(options[chosen][0], options[chosen][1])
            pointers[block.kind] = chosen + 1

    # --- main text: the text with the notes taken out ---
    if any(b.kind in _MAIN_KINDS for b in document.blocks) and match.text:
        removed = [(bisect_left(match.starts, s), bisect_left(match.starts, e)) for s, e, _ in note_spans]
        stream = _Stream(match.text, removed)
        wanted = [(i, b.match_text) for i, b in enumerate(document.blocks) if b.kind in _MAIN_KINDS]
        placed = _find_sequence(stream.text, wanted)
        for i, (start, end) in placed.items():
            if end <= start:
                continue
            raw_start, raw_end = raw(stream.to_source(start), stream.to_source(end - 1) + 1)
            reserved = tuple((s, e) for s, e, _ in note_spans if raw_start <= s and e <= raw_end)
            windows[i] = BlockWindow(raw_start, raw_end, reserved)

    # --- pages: consecutive stretches of the text ---
    page_blocks = [(i, b.match_text) for i, b in enumerate(document.blocks) if b.kind == "page"]
    if page_blocks and match.text:
        placed = _find_sequence(match.text, page_blocks)
        ordered = sorted(placed.items(), key=lambda item: item[1][0])
        for n, (i, (start, _)) in enumerate(ordered):
            end = ordered[n + 1][1][0] if n + 1 < len(ordered) else len(match.text)
            raw_start, raw_end = raw(start, max(end, start + 1))
            windows[i] = BlockWindow(raw_start, len(text) if n + 1 == len(ordered) else raw_end)
    return windows
