import re
from typing import Any, Dict

from eyecite.models import FullCaseCitation, FullJournalCitation

from utils.cleaner import clean_str
from utils.logger import get_logger
from utils.span_finder import get_span

logger = get_logger()


def get_journal_author_title(obj) -> Dict[str, str | None] | None:
    """
    Extract first-listed author and article title for journal citations.

    Assumes canonical form:
        Author(s), Title, <vol> <Journal Abbrev> <page> (year)
    Uses obj.document.plain_text and a 2-int span where span[0] is the index
    of the first char of the volume and span[1] is the index of the second-to-last
    char of the page.
    """
    if not isinstance(obj, FullJournalCitation):
        return None

    # Access full document text and the (start, end) span
    document = getattr(obj, "document", None)
    text = getattr(document, "plain_text", None)
    if not text:
        return None

    span = get_span(obj)
    if not span:
        return None

    volume_span_start, _ = span  # start of the numeric volume
    if not (0 <= volume_span_start <= len(text)):
        return None

    text_before_volume = text[:volume_span_start]
    citation_start_pos = _find_citation_start(text_before_volume)

    # Find where the citation starts in the document
    text_before_volume = text[:volume_span_start]
    citation_start_pos = _find_citation_start(text_before_volume)
    
    # Extract just the citation text (from citation start to volume)
    citation_text = text[citation_start_pos:volume_span_start]
    
    # Find commas within the citation only
    comma_positions = [i for i, char in enumerate(citation_text) if char == ","]
    
    if len(comma_positions) < 2:
        return None
    
    # Last two commas separate author, title, volume
    title_comma_pos = comma_positions[-1]
    author_comma_pos = comma_positions[-2]
    
    # Extract segments (positions are relative to citation_text)
    raw_author_segment = citation_text[:author_comma_pos]
    title = citation_text[author_comma_pos + 1:title_comma_pos].strip()
    
    # Clean author segment
    author = _clean_author_segment(raw_author_segment)

    logger.info(f"Extracted author: {author}, title: {title}")

    return {"author": author, "title": title}

def _find_citation_start(text: str) -> int:
    """Find where the citation begins by looking for citation start markers.
    
    Excludes middle initial periods (e.g., "John Q. Smith") from being treated
    as sentence-ending periods.
    
    Args:
        text: Text up to (but not including) the volume.
    
    Returns:
        Character position where the citation starts.
    """
    # Define quote-based markers first (more specific)
    quote_markers = [
        ('." ', 3),
        ('"; ', 3),
    ]
    
    best_pos = -1
    skip_length = 0
    
    # Check quote-based markers
    for pattern, length in quote_markers:
        pos = text.rfind(pattern)
        if pos > best_pos:
            best_pos = pos
            skip_length = length
    
    # Find all ". " and "; " occurrences, filtering out middle initials
    # Middle initial pattern: space + single capital letter + period + space
    middle_initial_pattern = re.compile(r'\s[A-Z]\.\s')
    
    # Check for ". " (sentence citation marker)
    pos = len(text) - 1
    while pos >= 0:
        pos = text.rfind('. ', 0, pos)
        if pos == -1:
            break
        
        # Check if this is a middle initial (preceded by space + capital letter)
        # Look at character before the period
        if pos > 0 and text[pos - 1].isupper() and (pos == 1 or text[pos - 2] == ' '):
            # This is a middle initial, skip it
            pos -= 1
            continue
        
        # Valid sentence marker found
        if pos > best_pos:
            best_pos = pos
            skip_length = 2
        break
    
    # Check for "; " (string citation marker)
    pos = text.rfind('; ')
    if pos > best_pos:
        best_pos = pos
        skip_length = 2
    
    if best_pos == -1:
        # No marker found - citation starts at beginning
        return 0
    
    # Return position after the marker
    return best_pos + skip_length


def _clean_author_segment(segment: str) -> str:
    """Remove citation signals, prefixes, and handle author name cleanup.
    
    Args:
        segment: Raw text containing author name and possible signals/prefixes.
    
    Returns:
        Cleaned author name.
    """
    author_text = segment.strip()
    
    # Define signals to remove (order matters for multi-word signals)
    signals = [
        "see e.g., ",
        "see also ",
        "see cf.",
        "but see ",
        "but cf.",
        "but compare ",
        "e.g.,",
        "see ",
        "cf.",
        "cf ",
        "compare ",
        "but ",
        "accord ",
        "contra ",
    ]
    
    # Remove signals (case-insensitive)
    for signal in signals:
        pattern = re.escape(signal)
        author_text = re.sub(
            pattern, 
            "", 
            author_text, 
            count=1, 
            flags=re.IGNORECASE
        ).strip()
    
    # Remove "et al." and any following content
    author_text = re.sub(r"\s+et al\..*$", "", author_text, flags=re.IGNORECASE).strip()
    
    # Handle multiple authors separated by "&" or " and " - keep only the first
    if "&" in author_text:
        author_text = author_text.split("&")[0].strip()
    elif " and " in author_text.lower():
        # Case-insensitive split on " and "
        parts = re.split(r'\s+and\s+', author_text, maxsplit=1, flags=re.IGNORECASE)
        author_text = parts[0].strip()
    
    return author_text

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
    if not trimmed:
        return fallback

    base_word = r"[A-Z][\w.\-&'/]*,?"
    connectors = r"(?:of|the|and|for|in|on|at|et|al\.?|ex|rel\.?|&)"
    name_pattern = rf"{base_word}(?:\s+(?:{base_word}|{connectors}))*"

    pattern_specs = [
        (re.compile(rf"(In\s+re\s+{name_pattern})(?=[\s,;:.)]|$)"), True),
        (re.compile(rf"({name_pattern}\s+v\.\s+{name_pattern})(?=[\s,;:.)]|$)"), False),
    ]

    noise_single = {"see", "cf.", "cf", "compare", "but", "accord", "contra", "e.g.", "e.g"}
    noise_pairs = {
        ("see", "also"),
        ("see", "e.g."),
        ("but", "see"),
        ("but", "cf."),
        ("but", "compare"),
    }

    contexts: list[str] = []
    seen_contexts: set[str] = set()

    def add_context(segment: str, *, front: bool = False) -> None:
        segment = segment.strip()
        if not segment or segment in seen_contexts:
            return
        if front:
            contexts.insert(0, segment)
        else:
            contexts.append(segment)
        seen_contexts.add(segment)

    comma_idx = trimmed.rfind(",")
    if comma_idx != -1:
        case_segment = trimmed[:comma_idx].rstrip()
        if case_segment:
            add_context(case_segment)
            semicolon_within = case_segment.rfind(";")
            if semicolon_within != -1:
                add_context(case_segment[semicolon_within + 1 :], front=True)

    semicolon_idx = trimmed.rfind(";")
    if semicolon_idx != -1:
        add_context(trimmed[semicolon_idx + 1 :], front=True)

    period_idx = trimmed.rfind(".")
    if period_idx != -1:
        add_context(trimmed[period_idx + 1 :])

    add_context(trimmed[-300:])

    def extract_candidate(segment: str) -> str | None:
        context_window = segment[-300:]
        matches: list[tuple[int, re.Match[str], bool]] = []
        for pattern, is_in_re in pattern_specs:
            for match in pattern.finditer(context_window):
                matches.append((match.end(), match, is_in_re))

        if not matches:
            return None

        matches.sort(key=lambda item: item[0], reverse=True)

        for _, match, is_in_re in matches:
            candidate = clean_str(match.group(1))
            if not candidate:
                continue

            if is_in_re:
                return candidate.rstrip().removesuffix(",")

            if " v. " not in candidate:
                continue

            tokens = candidate.split()
            idx = 0
            while idx < len(tokens):
                current = tokens[idx].lower().strip(",").strip(";").strip(":")
                next_token = tokens[idx + 1].lower().strip(",").strip(";").strip(":") if idx + 1 < len(tokens) else None

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
            return cleaned_candidate.rstrip().removesuffix(",")
        return None

    for context in contexts:
        candidate = extract_candidate(context)
        if candidate:
            candidate = candidate.rstrip().removesuffix(",")
        if not candidate:
            continue
        if len(candidate) > len(fallback or ""):
            logger.info(f"Resolved case name: {candidate}")
            return candidate
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

