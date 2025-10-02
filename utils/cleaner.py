import re
from typing import Any

_space_re = re.compile(r"\s+")
_citation_marker_re = re.compile(r"\[\d+:")
_non_alphanum_re = re.compile(r"[^a-z0-9]+", re.IGNORECASE)


def clean_str(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    # Remove all footnote markers like "[3:" from anywhere in the string
    text = _citation_marker_re.sub("", text)
    text = _space_re.sub(" ", text).strip()
    return text or None

def normalize_case_name_for_compare(name: str | None) -> str | None:
    if not name:
        return None
    normalized = _non_alphanum_re.sub("", name.lower())
    return normalized or None