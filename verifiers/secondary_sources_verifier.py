from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, Optional

import requests

LOC_SEARCH_URL = "https://www.loc.gov/search/"

def _clean(x: Optional[str]) -> str:
    return " ".join(str(x or "").strip().split())

def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def _join_nonempty(parts: Iterable[str]) -> str:
    return " ".join(p for p in parts if p)

def _authors_from(full: Any) -> str:
    # Eyecite may expose authors differently across citation types
    # Try list[dict]{name}, list[str], or single string
    a = getattr(full, "authors", None)
    if isinstance(a, list):
        if a and isinstance(a[0], dict) and "name" in a[0]:
            return ", ".join(_clean(x.get("name")) for x in a if x.get("name"))
        return ", ".join(_clean(str(x)) for x in a if x)
    return _clean(getattr(full, "author", ""))

def _title_from(full: Any) -> str:
    for attr in ("title", "case_name", "work", "article_title"):
        v = _clean(getattr(full, attr, None))
        if v:
            return v
    return ""

def _container_from(full: Any) -> str:
    for attr in ("container_title", "journal", "source", "publisher"):
        v = _clean(getattr(full, attr, None))
        if v:
            return v
    return ""

def _num(x: Any) -> str:
    try:
        return str(int(str(x).strip()))
    except Exception:
        return _clean(str(x or ""))

def _candidate_queries(full: Any) -> Iterable[str]:
    title = _title_from(full)
    authors = _authors_from(full)
    container = _container_from(full)
    year = _num(getattr(full, "year", ""))
    vol = _num(getattr(full, "volume", ""))
    issue = _num(getattr(full, "issue", ""))
    page = _num(getattr(full, "page", getattr(full, "first_page", "")))

    # Most precise to least precise
    q1 = _join_nonempty([title, authors, container, year])
    q2 = _join_nonempty([title, authors, year])
    q3 = _join_nonempty([title, container])
    q4 = _join_nonempty([title])
    q5 = _join_nonempty([container, vol, issue, page, year])  # useful for serials
    for q in (q1, q2, q3, q4, q5):
        if q:
            yield q

def _looks_like_match(item: Dict[str, Any], title: str, container: str, year: str) -> bool:
    ititle = _clean(item.get("title"))
    partof = _clean(item.get("partof"))
    date = _clean(item.get("date"))

    # Title similarity
    if title and ititle and _sim(title, ititle) >= 0.72:
        # Optional year check if present
        if year and date and (year in date or _sim(year, date) >= 0.6):
            return True
        return True

    # Container match for journals/series
    if container and (container.lower() in ititle.lower() or container.lower() in partof.lower()):
        if title:
            # If we have an article title, require some similarity too
            if ititle and _sim(title, ititle) >= 0.65:
                return True
        else:
            return True

    return False

def loc_has_secondary(fullcitation: Any, timeout: float = 10.0) -> bool:
    """
    Return True if Library of Congress API likely has a catalog record
    matching this Eyecite FullCitation (treatises, restatements, journals, etc.).
    Returns False on no match or on network/API errors.
    """
    title = _title_from(fullcitation)
    container = _container_from(fullcitation)
    year = _num(getattr(fullcitation, "year", ""))

    session = requests.Session()
    try:
        for q in _candidate_queries(fullcitation):
            params = {"q": q, "fo": "json"}
            r = session.get(LOC_SEARCH_URL, params=params, timeout=timeout)
            if r.status_code != 200:
                continue
            data = r.json() or {}
            results = data.get("results") or []
            for item in results:
                if _looks_like_match(item, title, container, year):
                    return True
        return False
    except Exception:
        return False
