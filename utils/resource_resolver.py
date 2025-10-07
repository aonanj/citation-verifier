import re
from typing import Any

from eyecite.models import FullCaseCitation, FullJournalCitation

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

def resolve_case_name(case_name: str | None, obj=None) -> str | None:
    """Resolve case name from the citation object if possible.

    `case_name` is an optional fallback sourced from citation metadata.
    """

    fallback = clean_str(case_name)

    if obj is None and isinstance(case_name, FullCaseCitation):
        obj = case_name
        fallback = None

    if not isinstance(obj, FullCaseCitation):
        return fallback

    span = get_span(obj)
    if span is None:
        return fallback
    start, end = span
    if start is None or end is None or start <= 0 or end <= 0:
        return fallback

    document = getattr(obj, "document", None)
    text_block = getattr(document, "plain_text", None)
    if not isinstance(text_block, str) or not text_block:
        return fallback

    preceding_text = text_block[:start]
    if not preceding_text:
        return fallback

    trimmed = preceding_text.rstrip()
    comma_idx = trimmed.rfind(",")
    if comma_idx == -1:
        return fallback

    case_segment = trimmed[:comma_idx].rstrip()
    if not case_segment:
        return fallback

    context_window = case_segment[-300:]

    base_word = r"[A-Z][\w.\-&'/]*,?"
    connectors = r"(?:of|the|and|for|in|on|at|et|al\.?|ex|rel\.?|&)"
    name_pattern = rf"{base_word}(?:\s+(?:{base_word}|{connectors}))*"

    patterns = [
        rf"(In\s+re\s+{name_pattern})\s*$",
        rf"({name_pattern}\s+v\.\s+{name_pattern})\s*$",
    ]

    noise_single = {"see", "cf.", "cf", "compare", "but", "accord", "contra", "e.g.", "e.g"}
    noise_pairs = {
        ("see", "also"),
        ("see", "e.g."),
        ("but", "see"),
        ("but", "cf."),
        ("but", "compare"),
    }

    for pattern in patterns:
        match = re.search(pattern, context_window)
        if not match:
            continue

        candidate = clean_str(match.group(1))
        if not candidate:
            continue

        if candidate.lower().startswith("in re "):
            return candidate

        if " v. " not in candidate:
            continue

        tokens = candidate.split()
        idx = 0
        while idx < len(tokens):
            current = tokens[idx].lower().strip(",;:")
            next_token = tokens[idx + 1].lower().strip(",;:") if idx + 1 < len(tokens) else None

            if next_token and (current, next_token) in noise_pairs:
                idx += 2
                continue

            if current in noise_single:
                idx += 1
                continue

            break

        if idx >= len(tokens):
            continue

        stripped_tokens = tokens[idx:]
        cleaned_candidate = clean_str(" ".join(stripped_tokens))
        if not cleaned_candidate or " v. " not in cleaned_candidate:
            continue

        left, _, right = cleaned_candidate.partition(" v. ")
        if not left or not right:
            continue
        if not any(ch.isalpha() and ch.isupper() for ch in left):
            continue
        if not any(ch.isalpha() and ch.isupper() for ch in right):
            continue
        if len(cleaned_candidate) > len(fallback or ""):
            logger.info(f"Resolved case name: {cleaned_candidate}")
            return cleaned_candidate
    logger.info("Could not resolve case name; using fallback: %s", fallback)
    return fallback


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
