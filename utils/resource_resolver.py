import re
from typing import Any

from eyecite.models import FullJournalCitation

from utils.cleaner import clean_str
from utils.logger import get_logger
from utils.span_finder import get_span

logger = get_logger()


def get_journal_author_title(obj) -> dict[str | None, str | None] | None:
    """Extract author and title from a FullJournalCitation object."""
    if not isinstance(obj, FullJournalCitation):
        return None

    cite_span = get_span(obj)
    if not cite_span:
        return None

    start, _ = cite_span
    if start is None or start <= 0:
        return None

    document = getattr(obj, "document", None)
    text_block = getattr(document, "plain_text", None)
    if not text_block or not isinstance(text_block, str):
        return None

    preceding_text = text_block[:start]

    first_comma = preceding_text.rfind(",")
    if first_comma == -1:
        return None

    second_comma = preceding_text.rfind(",", 0, first_comma)
    if second_comma == -1:
        return None

    raw_title = preceding_text[second_comma + 1 : first_comma]
    raw_title = raw_title.replace('"', "").replace("'", "")
    title = clean_str(raw_title)

    period_idx = preceding_text.rfind(".", 0, second_comma)
    author_start = period_idx + 1 if period_idx != -1 else 0
    raw_author = preceding_text[author_start:second_comma]
    raw_author = raw_author.replace('"', "").replace("'", "")
    author = clean_str(raw_author)

    if title is None and author is None:
        return None

    return {"author": author, "title": title}

def resolve_case_name(case_name: str | None, obj) -> str | None:
    """Resolve case name from the citation object if possible."""

    span = get_span(obj)
    start, end = span if span is not None else (None, None)
    if start is None or end is None or start <= 0 or end <= 0:
        return case_name

    document = getattr(obj, "document", None)
    text_block = getattr(document, "plain_text", None)
    if not text_block or not isinstance(text_block, str):
        return case_name

    preceding_text = text_block[:start]
    if not preceding_text:
        return case_name

    first_comma = preceding_text.rfind(",")
    if first_comma == -1:
        return case_name

    v = preceding_text.lower().rfind("v.", 0, first_comma)
    if v == -1:
        in_re = preceding_text.lower().rfind("in re", 0, first_comma)
        if in_re == -1:
            return case_name
        raw_case_name = f"In re {preceding_text[in_re:first_comma]}"
        raw_case_name = raw_case_name.replace('"', "").replace("'", "")
        raw_case_name = clean_str(raw_case_name)
        if len(raw_case_name or "") < len(case_name if case_name else ""):
            return case_name
        return raw_case_name

    raw_defendent = preceding_text[v:first_comma]
    period_idx = preceding_text.rfind(".", 0, v)
    raw_plaintiff_start = period_idx + 1 if period_idx != -1 else 0
    raw_plaintiff = preceding_text[raw_plaintiff_start:v]
    raw_case_name = f"{raw_plaintiff} v. {raw_defendent}"
    raw_case_name = raw_case_name.replace('"', "").replace("'", "")
    raw_case_name = clean_str(raw_case_name)
    if len(raw_case_name or "") < len(case_name if case_name else ""):
        return case_name
    return raw_case_name

def resolve_case_court_year(case_year: str | None, obj) -> dict[str | Any | None, str | Any | None] | None:
    """Resolve case year and court from the citation object if possible."""
    fallback = {"year": case_year, "court": None}
    if case_year is not None:
        return fallback

    span = get_span(obj)
    start, end = span if span is not None else (None, None)
    if start is None or end is None or start <= 0 or end <= 0:
        return fallback

    document = getattr(obj, "document", None)
    text_block = getattr(document, "plain_text", None)
    if not text_block or not isinstance(text_block, str):
        return fallback

    following_text = text_block[end:]
    if not following_text:
        return fallback

    open_paren = following_text.find("(")
    if open_paren == -1:
        return fallback
    close_paren = following_text.find(")", open_paren)
    if close_paren == -1:
        return fallback

    raw_year_segment = following_text[open_paren + 1 : close_paren]
    match_year = re.search(r"\b(17|18|19|20)\d{2}\b", raw_year_segment)
    if not match_year:
        return fallback

    raw_year = clean_str(match_year.group(0))

    if len(raw_year or "") != 4:
        return fallback
    if case_year is not None and raw_year != case_year:
        return fallback
    court_candidate = raw_year_segment[: match_year.start()]
    raw_court = clean_str(court_candidate)
    if raw_court:
        raw_court = clean_str(raw_court.rstrip(",;"))
        if raw_court and len(raw_court) > 22:
            return fallback

    return {"year": raw_year or case_year, "court": raw_court}
