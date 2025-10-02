# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

import re
from typing import Any

_space_re = re.compile(r"\s+")
_non_alphanum_re = re.compile(r"[^a-z0-9]+", re.IGNORECASE)


def clean_str(value: Any) -> str | None:
    if not value:
        return None
    s = str(value)
    s = re.sub(r"\s+", " ", s).strip()
    s = _space_re.sub(" ", s).strip()
    return s or None

def normalize_case_name_for_compare(name: str | None) -> str | None:
    if not name:
        return None
    normalized = _non_alphanum_re.sub("", name.lower())
    return normalized or None
