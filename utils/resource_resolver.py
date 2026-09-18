import re
from typing import Any, Dict

from eyecite.helpers import get_year
from eyecite.models import CitationToken, FullCaseCitation, FullJournalCitation, IdToken
from reporters_db import CASE_NAME_ABBREVIATIONS, STATE_ABBREVIATIONS

from utils.cleaner import clean_str, normalize_case_name_for_compare
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


# --- Case-name search window and boundaries --------------------------------
#
# A case name must come from the text between this citation and the one before
# it, and can't run across a sentence end ("Benson. Gottschalk") or a comma
# that doesn't introduce an entity suffix ("Gottschalk, Parker").

# Words whose trailing period is an abbreviation, not a sentence end (Bluebook
# T6 case-name and T10 state abbreviations, plus a few T6 omits).
_NAME_ABBREVIATIONS = (
    set(CASE_NAME_ABBREVIATIONS)
    | set(STATE_ABBREVIATIONS)
    | {"Enters.", "Jr.", "Sr.", "St.", "Mt.", "Ft.", "v."}
)
# Words that may follow a comma inside a party name ("Amgen, Inc.").
_PARTY_SUFFIXES = {
    suffix.lower()
    for suffix in (
        {"Inc.", "Inc", "Co.", "Corp.", "Ltd.", "LLC", "L.L.C.", "LLP", "L.L.P.", "L.P.",
         "N.A.", "P.C.", "P.A.", "S.A.", "Jr.", "Sr.", "et"}
        | set(STATE_ABBREVIATIONS)
    )
}
# Replaces the whitespace at a boundary; neither \w nor \s matches it, so the
# name patterns in resolve_case_name can't cross it.
_NAME_BOUNDARY = "\x00"
_SENTENCE_END_RE = re.compile(r"(\S+)\.(\s+)(?=[A-Z])")
_NAME_COMMA_RE = re.compile(r",(\s+)(\S+)")
# Gap before a parallel citation ("410 U.S. 113, 93 S. Ct. 705") or after a
# subsequent-history phrase ("723 F.2d 195, 203 (2d Cir. 1983), rev'd, 471 U.S.
# 539") -- the same case, so the name search continues past that citation.
_PARALLEL_NAME_GAP_RE = re.compile(r"^[\s,]*(?:at\s+)?[\d\s,\-–]*$")
_HISTORY_NAME_GAP_RE = re.compile(
    r"^[^;]{0,120}?,\s*(?:aff['’]d|rev['’]d|vacated|modified|aff['’]g|rev['’]g|"
    r"cert\.\s+(?:denied|granted|dismissed))"
    r"(?:\s+(?:on\s+other\s+grounds|in\s+part|per\s+curiam))?,\s*$"
)


def is_name_abbreviation(word: str) -> bool:
    """True if `word` (without its trailing period) is an abbreviation or
    initial rather than a sentence-ending word: Bluebook T6/T10, a single
    capital, a dotted form, or the plural of a listed abbreviation. Also used
    by svc/secondary_citation_handler for treatise authors."""
    word = word.lstrip("(\"'“")
    if word + "." in _NAME_ABBREVIATIONS:
        return True
    # Initial ("H. K. Mulford") or dotted form ("U.S.", "J.E.M.").
    if re.fullmatch(r"[A-Z]", word) or "." in word:
        return True
    # Plural of a listed abbreviation ("Enters.", "Bros.").
    return word.endswith("s") and (word[:-1] + ".") in _NAME_ABBREVIATIONS


def _mark_name_boundaries(text: str) -> str:
    """Replace the whitespace after a sentence end or a non-suffix comma with
    _NAME_BOUNDARY (length-preserving)."""
    chars = list(text)

    def mark(start: int, end: int) -> None:
        chars[start:end] = _NAME_BOUNDARY * (end - start)

    for match in _SENTENCE_END_RE.finditer(text):
        if not is_name_abbreviation(match.group(1)):
            mark(match.start(2), match.end(2))
    for match in _NAME_COMMA_RE.finditer(text):
        if match.group(2).rstrip(",;:").lower() not in _PARTY_SUFFIXES:
            mark(match.start(1), match.end(1))
    return "".join(chars)


def _case_name_window_start(document, start: int) -> int:
    """Return where this citation's case-name search window begins: just after
    the nearest preceding citation or Id., skipping back over parallel
    citations and subsequent history of the same case."""
    text = document.plain_text
    tokens = sorted(
        (
            token
            for _, token in getattr(document, "citation_tokens", None) or []
            if isinstance(token, (CitationToken, IdToken)) and token.end <= start
        ),
        key=lambda token: token.end,
    )
    cursor = start
    while tokens:
        previous = tokens.pop()
        gap = text[previous.end:cursor]
        if isinstance(previous, CitationToken) and (
            _PARALLEL_NAME_GAP_RE.match(gap) or _HISTORY_NAME_GAP_RE.match(gap)
        ):
            cursor = previous.start
            continue
        return previous.end
    return 0


def _last_party(name: str) -> str:
    return name.split(" v. ", 1)[-1].removeprefix("In re ").strip()


def _name_in_window(name: str, window: str) -> bool:
    """True if the name's last party appears in the window (whitespace- and
    case-insensitive)."""
    party = re.sub(r"\s+", " ", _last_party(name)).lower()
    return bool(party) and party in re.sub(r"\s+", " ", window).lower()


def _defendants_agree(candidate: str, fallback: str) -> bool:
    """True unless both names have a defendant and neither normalized
    defendant is a suffix of the other."""
    candidate_key = normalize_case_name_for_compare(_last_party(candidate))
    fallback_key = normalize_case_name_for_compare(_last_party(fallback))
    if not candidate_key or not fallback_key:
        return True
    return candidate_key.endswith(fallback_key) or fallback_key.endswith(candidate_key)


def resolve_case_name(case_name: str | None, obj=None) -> str | None:
    """Resolve case name from the citation object if possible.

    `case_name` is an optional fallback sourced from citation metadata. The
    search covers only the text since the preceding citation (see
    _case_name_window_start) with sentence and comma boundaries marked (see
    _mark_name_boundaries); a fallback that doesn't occur in that window was
    borrowed from an earlier citation and is dropped.
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
    if start is None or end is None or start < 0 or end < 0:
        return fallback

    document = getattr(obj, "document", None)
    text_block = getattr(document, "plain_text", None)
    if not isinstance(text_block, str) or not text_block:
        return fallback

    window = text_block[_case_name_window_start(document, start):start]
    # eyecite's parallel-citation metadata copy can hand this citation an
    # earlier citation's parties ("(2009). 114. 447 U.S. 303" -> "Bilski v. Doll").
    if fallback and not _name_in_window(fallback, window):
        fallback = None

    preceding_text = _mark_name_boundaries(window)
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
            # eyecite's defendant sits right before the citation; a longer
            # candidate may extend the plaintiff ("Seed Co." -> "Funk Bros.
            # Seed Co.") but must end with the same defendant.
            if fallback and not _defendants_agree(candidate, fallback):
                continue
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
    if start is None or end is None or start < 0 or end < 0:
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


# Optional pin cite between a citation and its parenthetical: ", 460",
# ", 585-86", ", 115 & n.4", ", 726, n.*", ", at *3".
_PIN_CITE_PART = r"(?:,?\s*(?:at\s+)?[*¶]?\d[\d\s,\-–—&*]*(?:\s*nn?\.\s*[\d*][\d\-–*]*)?)?"
# The citation's own court/date parenthetical: any court text before the year
# ("6th Cir. ", "S.D.N.Y. Aug. 26, ", "Tex. App.—Houston [14th Dist.] "), which
# can't cross a paren; the year can't be glued to a preceding word ("09CV04515")
# and may be followed only by a comma phrase (", no pet.") before ")".
_POST_CITE_YEAR_RE = re.compile(
    rf"{_PIN_CITE_PART}\s*"
    r"\([^()]*?(?<![\w:])(?P<year>\d{4})(?:-\d{2,4})?(?:,[^()]*)?\)"
)
# Separator before a true parallel citation: "410 U.S. 113, 93 S. Ct. 705".
_PARALLEL_CITE_GAP_RE = re.compile(rf"{_PIN_CITE_PART},\s*$")
# California style: "People v. Anderson (1972) 6 Cal.3d 628".
_PRE_CITE_YEAR_RE = re.compile(r"\((?P<year>\d{4})\)\s*$")


def resolve_case_year(obj) -> str | None:
    """Return a full case citation's year, read only from its own text.

    The year comes from the first parenthetical directly after the citation
    (after an optional pin cite and any true parallel citations), else from a
    California-style "(1972)" directly before it, else None. eyecite's own
    year isn't trusted: its post-citation court group is an unbounded ".*?"
    that runs into the *next* citation's "(Fed. Cir. 2025)", and its
    pre-citation year scan and parallel-citation metadata copy reach across
    earlier citations.
    """
    if not isinstance(obj, FullCaseCitation):
        return None

    span = get_span(obj)
    document = getattr(obj, "document", None)
    text = getattr(document, "plain_text", None)
    if span is None or not isinstance(text, str):
        return None
    start, end = span

    citation_tokens = sorted(
        (token for _, token in getattr(document, "citation_tokens", None) or [] if isinstance(token, CitationToken)),
        key=lambda token: token.start,
    )
    for token in citation_tokens:
        if token.start < end:
            continue
        if not _PARALLEL_CITE_GAP_RE.match(text[end:token.start]):
            break
        end = token.end

    match = _POST_CITE_YEAR_RE.match(text[end:end + 250])
    if match and get_year(match["year"]):
        return match["year"]

    match = _PRE_CITE_YEAR_RE.search(text[max(0, start - 12):start])
    if match and get_year(match["year"]):
        return match["year"]

    return None

