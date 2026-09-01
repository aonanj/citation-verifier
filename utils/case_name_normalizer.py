# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

from __future__ import annotations

import functools
import re
from typing import Dict, List, Tuple

from reporters_db import CASE_NAME_ABBREVIATIONS, STATE_ABBREVIATIONS

from utils.logger import get_logger

logger = get_logger()

_APOSTROPHE_RE = re.compile(r"[’‘ʼ`´]")
_STRIP_RE = re.compile(r"[^a-z0-9&]+")
_DOTTED_TOKEN_RE = re.compile(r"[A-Za-z]\.[\"'),\]]*$")

_MANUAL_TOKEN_ALIASES = {"v": "v", "vs": "v", "versus": "v"}


def _lookup_key(token: str) -> str:
    lowered = token.lower()
    lowered = _APOSTROPHE_RE.sub("'", lowered).replace("'", "")
    return _STRIP_RE.sub("", lowered)


@functools.lru_cache(maxsize=1)
def _build_canonical_maps() -> Tuple[Dict[str, str], Dict[Tuple[str, ...], List[str]], int]:
    canon_of_abbr: Dict[str, str] = {}
    full_to_abbrs: Dict[str, set] = {}

    for abbr, fulls in CASE_NAME_ABBREVIATIONS.items():
        abbr_key = _lookup_key(abbr)
        if not abbr_key:
            continue
        canon_of_abbr[abbr_key] = abbr_key
        for full in fulls:
            full_key = _lookup_key(full)
            if not full_key:
                continue
            full_to_abbrs.setdefault(full_key, set()).add(abbr_key)

    for full_key, abbr_keys in full_to_abbrs.items():
        if len(abbr_keys) > 1:
            canon = min(abbr_keys)
            for abbr_key in abbr_keys:
                canon_of_abbr[abbr_key] = canon

    token_map: Dict[str, str] = {}
    phrase_map: Dict[Tuple[str, ...], List[str]] = {}

    for abbr, fulls in CASE_NAME_ABBREVIATIONS.items():
        abbr_key = _lookup_key(abbr)
        if not abbr_key:
            continue
        canon = canon_of_abbr.get(abbr_key, abbr_key)
        token_map[abbr_key] = canon
        for full in fulls:
            full_words = full.split()
            full_keys = tuple(_lookup_key(w) for w in full_words if _lookup_key(w))
            if not full_keys:
                continue
            if len(full_keys) == 1:
                token_map[full_keys[0]] = canon
            else:
                phrase_map[full_keys] = [canon]

    for abbr, full in STATE_ABBREVIATIONS.items():
        abbr_words = abbr.split()
        abbr_keys = tuple(_lookup_key(w) for w in abbr_words if _lookup_key(w))
        if not abbr_keys:
            continue
        canon_list = list(abbr_keys)

        full_words = full.split()
        full_keys = tuple(_lookup_key(w) for w in full_words if _lookup_key(w))

        if len(abbr_keys) == 1:
            token_map.setdefault(abbr_keys[0], canon_list[0])
        else:
            phrase_map.setdefault(abbr_keys, canon_list)

        if full_keys:
            if len(full_keys) == 1:
                token_map.setdefault(full_keys[0], canon_list[0] if len(canon_list) == 1 else canon_list[0])
            else:
                phrase_map.setdefault(full_keys, canon_list)

    for alias, canon in _MANUAL_TOKEN_ALIASES.items():
        token_map.setdefault(alias, canon)

    max_phrase_len = max((len(k) for k in phrase_map), default=1)

    return token_map, phrase_map, max_phrase_len


def _canonical_tokens(name: str) -> List[Tuple[str, bool]]:
    token_map, phrase_map, max_phrase_len = _build_canonical_maps()

    normalized = _APOSTROPHE_RE.sub("'", name)
    raw_tokens = normalized.split()

    records: List[Tuple[str, bool]] = []
    for raw in raw_tokens:
        key = _lookup_key(raw)
        if not key:
            continue
        dot_flag = bool(_DOTTED_TOKEN_RE.search(raw))
        records.append((key, dot_flag))

    result: List[Tuple[str, bool]] = []
    i = 0
    n = len(records)
    while i < n:
        matched = False
        max_window = min(max_phrase_len, n - i)
        for window in range(max_window, 1, -1):
            candidate = tuple(records[j][0] for j in range(i, i + window))
            if candidate in phrase_map:
                for canon in phrase_map[candidate]:
                    result.append((canon, False))
                i += window
                matched = True
                break
        if matched:
            continue

        key, dot_flag = records[i]
        if key in token_map:
            result.append((token_map[key], dot_flag))
        elif key.endswith("s") and key[:-1] in token_map:
            result.append((token_map[key[:-1]], dot_flag))
        else:
            result.append((key, dot_flag))
        i += 1

    return result


def canonical_case_name(name: str | None) -> str | None:
    if not name:
        return None
    tokens = _canonical_tokens(name)
    if not tokens:
        return None
    return " ".join(t for t, _ in tokens)


def _tokens_align(a: Tuple[str, bool], b: Tuple[str, bool]) -> bool:
    a_tok, a_dotted = a
    b_tok, b_dotted = b
    if a_tok == b_tok:
        return True

    a_singular = a_tok[:-1] if a_tok.endswith("s") else a_tok
    b_singular = b_tok[:-1] if b_tok.endswith("s") else b_tok

    if a_dotted and len(a_tok) >= 3 and b_singular.startswith(a_singular):
        return True
    if b_dotted and len(b_tok) >= 3 and a_singular.startswith(b_singular):
        return True
    return False


def case_names_equivalent(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False

    tokens_a = _canonical_tokens(a)
    tokens_b = _canonical_tokens(b)

    if not tokens_a or not tokens_b:
        return False

    if [t for t, _ in tokens_a] == [t for t, _ in tokens_b]:
        return True

    if len(tokens_a) != len(tokens_b):
        return False

    return all(_tokens_align(x, y) for x, y in zip(tokens_a, tokens_b))
