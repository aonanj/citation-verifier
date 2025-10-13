from __future__ import annotations

import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from eyecite.models import FullCitation, FullJournalCitation
from rapidfuzz import fuzz, process

from utils.cleaner import clean_str, normalize_case_name_for_compare
from utils.logger import get_logger
from utils.resource_resolver import get_journal_author_title

logger = get_logger()

_OPENALEX_WORKS_URL = "https://api.openalex.org/works"
_OPENALEX_SOURCE_URL = "https://api.openalex.org/sources"
_OPENALEX_TIMEOUT = httpx.Timeout(15.0, connect=10.0, read=10.0)
_OPENALEX_MAILTO_ENV = "OPENALEX_MAILTO"

_SEMANTIC_SCHOLAR_BASE_URL = "https://api.semanticscholar.org/graph/v1"
_SEMANTIC_SCHOLAR_TIMEOUT = httpx.Timeout(10.0, connect=10.0, read=10.0)
_SEMANTIC_SCHOLAR_API_KEY = "SEMANTIC_SCHOLAR_API_KEY"
_SEMANTIC_SCHOLAR_MAX_SEARCH = 50
_DEFAULT_FIELDS_BASE = [
    "title",
    "year",
    "venue",
    "authors.name",
    "url",
    "isOpenAccess",
    "publicationTypes",
    "externalIds",
]
_DEFAULT_FIELDS_BASIC = ",".join(_DEFAULT_FIELDS_BASE)
_DEFAULT_FIELDS_AUTH = ",".join(_DEFAULT_FIELDS_BASE + ["tldr"])
_FIELDS = ",".join([
    "title","year","venue","authors.name","url","externalIds"
])

def _sleep_min_interval(last_ts):
    now = time.time()
    wait = max(0.0, 1.2 - (now - last_ts))
    if wait:
        time.sleep(wait)
    return time.time()


def _result_matches_citation(
    result: Dict[str, Any], citation_author: str | None, citation_title: str | None
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Check if an OpenAlex result matches the citation author and title.

    Args:
        result: The OpenAlex work result object.
        citation_author: The author from the citation.
        citation_title: The title from the citation.

    Returns:
        True if both author and title match, False otherwise.
    """
    # Normalize citation author and title for comparison
    normalized_citation_author = normalize_case_name_for_compare(citation_author)
    normalized_citation_title = normalize_case_name_for_compare(citation_title)

    # Check title match
    result_title = result.get("title")
    normalized_result_title = normalize_case_name_for_compare(result_title)

    title_matches = (
        normalized_citation_title
        and normalized_result_title
        and (
            normalized_citation_title in normalized_result_title
            or normalized_result_title in normalized_citation_title
        )
    )

    if not title_matches:
        logger.info(
            f"Title mismatch: citation='{citation_title}' vs result='{result_title}'"
        )
        details = {
            "source": "openalex",
            "unverified_fields": "title",
            "returned_values": {
                "title": citation_title
            },
        }
        return "warning", "Unverified details", details

    # Check author match - look through authorships
    if not normalized_citation_author:
        # If no author provided, just match on title
        logger.info("No citation author provided, matching on title only")
        details = {
            "source": "openalex",
            "unverified_fields": "author",
            "returned_values": {
                "author": citation_author
            },
        }
        return "warning", "Unverified details", details

    authorships = result.get("authorships", [])
    for authorship in authorships:
        author_obj = authorship.get("author", {})
        display_name = author_obj.get("display_name")
        normalized_display_name = normalize_case_name_for_compare(display_name)

        if normalized_display_name and (
            normalized_citation_author in normalized_display_name
            or normalized_display_name in normalized_citation_author
        ):
            logger.info(f"Author match found: {display_name}")
            return "verified", None, {"source": "openalex", "matched_author": display_name}

    logger.info(f"No author match found for: {citation_author}")
    details = {
            "source": "openalex",
            "unverified_fields": "author",
            "returned_values": {
                "author": citation_author
            },
        }
    return "warning", "Unverified details", details

def _verify_author_title_with_openalex(
    citation: FullCitation | None, resource_dict: Dict[str, Any] | None
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Verify a citation using the OpenAlex API with targeted, quoted field filters.

    Strategy:
      - Use specific `filter` parameters for author and title for more accuracy.
      - Wrap search values in double quotes to handle spaces and prevent 403 errors.
      - Example: `filter=title.search:"mapping the landscape",author.search:"smith"`
      - Then, post-filter results with `_result_matches_citation` to confirm.
    """
    if not isinstance(citation, FullJournalCitation):
        return "no_match", "Not a journal citation", None

    # Choose search fields: prefer provided values; fall back to parsed citation.
    search_author = clean_str(resource_dict.get("author")) if resource_dict else None
    search_title = clean_str(resource_dict.get("title")) if resource_dict else None
    if not search_author or not search_title:
        ji = get_journal_author_title(citation)
        if not search_author and ji:
            search_author = clean_str(ji.get("author"))
        if not search_title and ji:
            search_title = clean_str(ji.get("title"))

    if not search_author and not search_title:
        return _verify_journal_citation_with_openalex(citation, resource_dict)

    # Build a filter string, wrapping values with spaces in quotes
    filter_parts = []
    if search_title:
        # Add quotes around the title to treat it as a single search phrase
        filter_parts.append(f'title.search:"{search_title}"')
#    if search_author:
        # Add quotes around the author's name#
#        filter_parts.append(f'author.search:"{search_author}"')

    if not filter_parts:
        return "error", "could not build filter from citation data", None

    filter_str = ",".join(filter_parts)

    # Build the request parameters
    mailto = os.environ.get(_OPENALEX_MAILTO_ENV)
    params: Dict[str, Any] = {"filter": filter_str, "per-page": 25, "cursor": "*"}
    if mailto:
        params["mailto"] = mailto

    logger.info(f"Querying OpenAlex with params: {params}")

    try:
        with httpx.Client(timeout=_OPENALEX_TIMEOUT) as client:
            response = client.get(_OPENALEX_WORKS_URL, params=params)
            response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as e:
        logger.error(f"OpenAlex HTTP error: {e} for filter: {filter_str}")
        return "error", f"openalex http error: {e}", None
    except Exception as e:
        logger.error(f"OpenAlex unknown error: {e} for filter: {filter_str}")
        return "error", f"openalex error: {e}", None

    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        logger.info(f"No OpenAlex results for filter='{filter_str}'")
        return "no_match", "Not found in OpenAlex", {"not found": "title", "source": "openalex"}

    # The rest of the function remains the same, as post-filtering is still valuable
    citation_author = search_author
    citation_title  = search_title
    if not (citation_author and citation_title):
        ji = get_journal_author_title(citation)
        if not citation_author and ji:
            citation_author = clean_str(ji.get("author"))
        if not citation_title and ji:
            citation_title = clean_str(ji.get("title"))

    for idx, result in enumerate(results):
        if _result_matches_citation(result, citation_author, citation_title):
            result, _, _ = _result_matches_citation(result, citation_author, citation_title)
            if result == "verified":
                logger.info(f"OpenAlex result matched author+title on result index {idx}")
                return "verified", None, {"source": "openalex", "data": f"{citation_author}, {citation_title}"}

    logger.info("No OpenAlex result matched author+title after filter search")
    return "no_match", "Not found in OpenAlex", {"source": "openalex"}

def _verify_journal_citation_with_openalex(
  primary_full: FullCitation | None, resource_dict: Dict[str, Any] | None
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Verify a citation using the OpenAlex API with targeted, quoted field filters.

    Args:
        citation: The journal citation to verify.

    Returns:
        A tuple of (status, error_message, data) where:
        - status is "verified" if found, "no_match" if not found, or "error" on failure
        - error_message is None on success or an error description on failure
        - data is the OpenAlex work data if verified, otherwise None
    """
    if not isinstance(primary_full, FullJournalCitation):
        return "no_match", "Not a journal citation", None
    
    reporter_full_name = []
    data = {}
    logger.info(f"Verifying journal citation with OpenAlex: {primary_full}")
    groups = getattr(primary_full, 'groups', None)
    logger.info(f"Primary full groups: {groups}")
    volume = groups.get('volume') if groups else None
    logger.info(f"Primary full volume: {volume}")
    page = groups.get('page') if groups else None
    logger.info(f"Primary full page: {page}")

    reporter_editions = getattr(primary_full, 'all_editions', None)
    if reporter_editions and len(reporter_editions) > 0:
        reporter_name = getattr(reporter_editions[0], 'reporter', None)
        reporter_full_name = [getattr(reporter_name, 'name', None)] if reporter_name else None

    if reporter_full_name == []:
        edition_guess = getattr(primary_full, 'edition_guess', None)
        if edition_guess:
            guess_names = getattr(edition_guess, 'name', None)
            reporter_full_name = guess_names.split(";") if guess_names else []

    logger.info(f"OpenAlex source search for reporter_full_name={reporter_full_name},volume={str(volume)},page={str(page)}")
    source_id = None
    mailto = os.environ.get(_OPENALEX_MAILTO_ENV, "admin@phaethon.llc")
    params: Dict[str, Any] = {"filter": "", "per-page": 100, "cursor": "*", "mailto": mailto}
    for name in reporter_full_name if reporter_full_name else []:
        name = clean_str(str(name))
        params["filter"] = f"display_name.search:{name}"
        try:
            with httpx.Client(timeout=_OPENALEX_TIMEOUT) as client:
                response = client.get(_OPENALEX_SOURCE_URL, params=params)
                response.raise_for_status()
            data = response.json()
            if data['results']:
                first_result = data['results'][0]
                if first_result and "display_name" in first_result:
                    returned_name = first_result["display_name"]
                    similarity = process.extractOne(name, returned_name, scorer=fuzz.partial_ratio, score_cutoff=75)
                    if similarity:
                        logger.info(f"OpenAlex source match found: {returned_name} for name='{name}' with similarity={similarity}")
                        source_url = first_result["id"]
                        source_id = source_url.rsplit("/", 1)[-1] 
                        logger.info(f"OpenAlex source ID found: {source_id}")
                        break


        except httpx.HTTPError as e:
            logger.error(f"OpenAlex HTTP error: {e} for filter: {name}")
            return "error", f"openalex http error: {e}", None
        except Exception as e:
            logger.error(f"OpenAlex unknown error: {e} for filter: {name}")
            return "error", f"openalex error: {e}", None

    
    logger.info(f"OpenAlex source search results reporter_full_name='{reporter_full_name}: Source ID={source_id}'")

    filter = f"primary_location.source.id:{source_id},biblio.volume:{str(volume)},biblio.first_page:{str(page)}"

    params_works: Dict[str, Any] = {"filter": filter, "per-page": 100, "cursor": "*", "mailto": mailto}
    try:
        with httpx.Client(timeout=_OPENALEX_TIMEOUT) as client:
            response = client.get(_OPENALEX_WORKS_URL, params=params_works)
            response.raise_for_status()
        data_works = response.json()
        logger.info(f"OpenAlex works search response data: {data_works}")
        if data_works is not None and 'results' in data_works:
            results = data_works['results']
            for result in results:
                extracted_volume = result.get("biblio", {}).get("volume")
                logger.info(f"OpenAlex work volume: {extracted_volume}")
                extracted_page = result.get("biblio", {}).get("first_page")
                logger.info(f"OpenAlex work first_page: {extracted_page}")
                if (volume and extracted_volume and str(volume) == str(extracted_volume)) and (page and extracted_page and str(page) == str(extracted_page)):
                    logger.info(f"OpenAlex work match found for volume={volume} and page={page}")
                    return "verified", None, {"source": "openalex", "data": f"volume={volume}, page={page}"}

    except httpx.HTTPError as e:
        logger.error(f"OpenAlex HTTP error: {e} for filter search on {source_id}")
        return "error", f"openalex http error: {e}", None
    except Exception as e:
        logger.error(f"OpenAlex unknown error: {e} for filter search on {source_id}")
        return "error", f"openalex error: {e}", None
    results_works = data_works.get("results", []) if isinstance(data_works, dict) else []
    if not results_works:
        logger.info(f"No OpenAlex results for filter search on {source_id}")
        return "no_match", "Not found in OpenAlex", {"not found": "title", "source": "openalex"}
    
    return "no_match", "Not found in OpenAlex", None

def _verify_title_with_semantic_scholar(
    primary_full: FullCitation | None,
    resource_dict: Dict[str, Any] | None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Verify a citation via Semantic Scholar by exact/near title match with optional author check."""
    if not isinstance(primary_full, FullJournalCitation):
        return "no_match", "Not a journal citation", None

    # ---- inputs ----
    search_author = clean_str(resource_dict.get("author")) if resource_dict else None
    search_title = clean_str(resource_dict.get("title")) if resource_dict else None
    if not (search_author and search_title):
        ji = get_journal_author_title(primary_full)
        if not search_author and ji:
            search_author = clean_str(ji.get("author"))
        if not search_title and ji:
            search_title = clean_str(ji.get("title"))
    if search_author is None and search_title is None:
        return _verify_citation_with_semantic_scholar(primary_full, resource_dict)

    # ---- headers ----
    headers: Dict[str, str] = {"Accept": "application/json"}
    api_key = os.environ.get(_SEMANTIC_SCHOLAR_API_KEY)  # keep project constant
    if api_key:
        headers["x-api-key"] = api_key

    # ---- params ----
    fields = _DEFAULT_FIELDS_AUTH if api_key else _DEFAULT_FIELDS_BASIC  # comma-separated string
    params_search = {
        "query": f"\"{search_title}\"",
        "limit": str(_SEMANTIC_SCHOLAR_MAX_SEARCH),
        "fields": fields,
    }

    # ---- normalizers ----
    search_title_norm = normalize_case_name_for_compare(search_title)
    search_author_norm = normalize_case_name_for_compare(search_author)

    # ---- rate + retry ----
    import time, random
    BASE_INTERVAL = 1.2           # keep <= 1 RPS
    MAX_RETRIES = 5

    def _min_interval_sleep(last_ts: float) -> float:
        now = time.time()
        wait = max(0.0, BASE_INTERVAL - (now - last_ts))
        if wait:
            time.sleep(wait)
        return time.time()

    last_call = 0.0
    last_client_error: Optional[str] = None

    with httpx.Client(
        timeout=_SEMANTIC_SCHOLAR_TIMEOUT,
        headers=headers,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
    ) as client:
        attempt = 0
        while True:
            last_call = _min_interval_sleep(last_call)

            try:
                resp = client.get(f"{_SEMANTIC_SCHOLAR_BASE_URL}/paper/search", params=params_search)
            except httpx.HTTPError as e:
                logger.error("Semantic Scholar HTTP error for title '%s': %s", search_title, e)
                return "error", f"semantic scholar http error: {e}", None

            if resp.status_code == 200:
                try:
                    data = resp.json() or {}
                except Exception as e:
                    logger.error("Semantic Scholar JSON decode error: %s", e)
                    return "error", f"semantic scholar json error: {e}", None
                break  # proceed to evaluate results

            if resp.status_code == 429 and attempt < MAX_RETRIES:
                # Respect Retry-After if provided, else exponential backoff with jitter
                ra = resp.headers.get("Retry-After")
                try:
                    sleep_s = float(ra) if ra is not None else 0.0
                except ValueError:
                    sleep_s = 0.0
                backoff = max(sleep_s, 2 ** attempt) + random.uniform(0, 0.5)
                logger.error("Semantic Scholar 429 for title '%s'; sleeping %.2fs", search_title, backoff)
                time.sleep(backoff)
                attempt += 1
                continue

            if 400 <= resp.status_code < 500:
                last_client_error = f"semantic scholar query rejected ({resp.status_code})"
                logger.error("Semantic Scholar %s for title '%s': %s", resp.status_code, search_title, resp.text)
                data = {}
                break

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error("Semantic Scholar server error for title '%s': %s", search_title, e)
                return "error", f"semantic scholar http error: {e}", None

    papers = data.get("data", []) if isinstance(data, dict) else []
    if not papers:
        if last_client_error:
            return "error", last_client_error, None
        logger.info("Semantic Scholar no match for title='%s'", search_title)
        return "no_match", "Not found in Semantic Scholar", None

    # ---- scoring ----
    for paper in papers:
        paper_title = paper.get("title")
        paper_title_norm = normalize_case_name_for_compare(paper_title)
        if not (search_title_norm and paper_title_norm):
            continue

        title_match = (
            search_title_norm == paper_title_norm
            or search_title_norm in paper_title_norm
            or paper_title_norm in search_title_norm
        )
        if not title_match:
            continue

        if search_author and search_author_norm:
            authors = paper.get("authors") or []
            if not isinstance(authors, list):
                authors = []
            # exact/substring match
            match_found = False
            for author in authors:
                a_name = author.get("name")
                a_norm = normalize_case_name_for_compare(a_name)
                if a_norm and (
                    a_norm == search_author_norm
                    or search_author_norm in a_norm
                    or a_norm in search_author_norm
                ):
                    match_found = True
                    break
                if a_name:
                    sim = process.extractOne(search_author, [a_name], scorer=fuzz.partial_ratio, score_cutoff=75)
                    if sim:
                        match_found = True
                        break
            if not match_found:
                logger.info("Semantic Scholar no author match for '%s'", search_author)
                continue

        logger.info("Semantic Scholar verified by title%s",
                    f" + author '{search_author}'" if search_author else "")
        return "verified", None, {"source": "semantic_scholar", "data": paper}

    return "no_match", "No author match for title match", {"author": search_author, "source": "semantic_scholar"}

def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    # lowercase, remove punctuation, collapse spaces
    s2 = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s2).strip()

def _first_page(pages: Optional[str]) -> Optional[int]:
    if not pages:
        return None
    # handles "123", "123-130", "123–130", "S12-S30"
    m = re.match(r"^[A-Za-z]*\s*(\d+)", pages.strip())
    return int(m.group(1)) if m else None

def _journal_name(paper: Dict[str, Any]) -> str:
    # S2 sometimes uses journal.name, sometimes venue
    jname = None
    j = paper.get("journal")
    if isinstance(j, dict):
        jname = j.get("name")
    return jname or paper.get("venue") or ""

def _escape_semantic_scholar_term(term: str) -> str:
    if not term:
        return ""
    # Escape Lucene special characters so the query is always parseable
    return re.sub(r"([+\-=&|!(){}\[\]^\"~*?:\\\/])", r"\\\1", term)

def _verify_citation_with_semantic_scholar(
    primary_full: FullCitation | None, 
    resource_dict: Dict[str, Any] | None
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """
    Search Semantic Scholar by Journal + Volume + First Page (+ optional year).

    Returns a list of candidate papers filtered to match:
      - normalized journal name equals or contains the provided journal name, or vice versa
      - volume exact string match (after stripping)
      - first page equals `page`

    Results are sorted with exact journal equality first, then year proximity if provided.
    """
    if not isinstance(primary_full, FullJournalCitation):
        return "no_match", "not a journal citation", None
    
    reporter_full_name = []
    data = {}
    logger.info(f"Verifying journal citation with OpenAlex: {primary_full}")
    groups = getattr(primary_full, 'groups', None)
    logger.info(f"Primary full groups: {groups}")
    volume = groups.get('volume') if groups else None
    logger.info(f"Primary full volume: {volume}")
    page = groups.get('page') if groups else None
    logger.info(f"Primary full page: {page}")
    year = getattr(primary_full, 'year', None)
    if year is None:
        year = resource_dict.get('year') if resource_dict else None
    logger.info(f"Primary full year: {year}")

    reporter_editions = getattr(primary_full, 'all_editions', None)
    if reporter_editions and len(reporter_editions) > 0:
        reporter_name = getattr(reporter_editions[0], 'reporter', None)
        reporter_full_name = [getattr(reporter_name, 'name', None)] if reporter_name else None

    if reporter_full_name == []:
        edition_guess = getattr(primary_full, 'edition_guess', None)
        if edition_guess:
            guess_names = getattr(edition_guess, 'name', None)
            reporter_full_name = guess_names.split(";") if guess_names else []

    headers = {"Accept": "application/json"}
    api_key = os.environ.get(_SEMANTIC_SCHOLAR_API_KEY)
    if api_key:
        headers["x-api-key"] = api_key

    logger.info(f"Semantic Scholar search for reporter_full_name={reporter_full_name},volume={str(volume)},page={str(page)}")
    journal = clean_str(reporter_full_name[0] if reporter_full_name else None)
    if not (journal and volume and page):
        return "no_match", "insufficient citation data for search", None

    vol_s = str(volume).strip()
    page_s = str(page).strip()
    year_str = str(year).strip()

    queries: List[str] = []

    def _add_query(parts: List[Optional[str]]) -> None:
        query = " ".join(part for part in parts if part)
        if query and query not in queries:
            queries.append(query)

    journal_variants: List[str] = []
    if journal:
        base_phrase = journal.strip()
        if base_phrase:
            journal_variants.append(base_phrase)
            # Some Semantic Scholar queries choke on raw '&'; try common substitutions.
            amp_as_word = re.sub(r"&", " and ", base_phrase)
            amp_removed = re.sub(r"&", " ", base_phrase)
            for variant in (amp_as_word, amp_removed):
                normalized_variant = re.sub(r"\s+", " ", variant).strip()
                if normalized_variant and normalized_variant not in journal_variants:
                    journal_variants.append(normalized_variant)

    for phrase in journal_variants:
        # For quoted queries, don't escape - the quotes handle special characters
        quoted_phrase = f"\"{phrase}\"" if phrase else ""
        if quoted_phrase:
            _add_query([quoted_phrase, vol_s, page_s, year_str])
        # For unquoted queries, escape special characters
        escaped_phrase = _escape_semantic_scholar_term(phrase)
        if escaped_phrase:
            _add_query([escaped_phrase, vol_s, page_s, year_str])


    last_client_error: Optional[str] = None
    last_call = 0.0

    with httpx.Client(timeout=_SEMANTIC_SCHOLAR_TIMEOUT, headers=headers, limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as client:
        for q in queries:
            params = {
                "query": q,
                "limit": _SEMANTIC_SCHOLAR_MAX_SEARCH,
                "fields": _FIELDS,
                "year": year_str
            }
            attempt = 0
            while True:
                last_call = _sleep_min_interval(last_call)

                r = client.get(f"{_SEMANTIC_SCHOLAR_BASE_URL}/paper/search", params=params)
                if r.status_code == 200:
                    data = r.json() or {}
                    items = data.get("data", []) if isinstance(data, dict) else []
                    if items and len(items) > 0:
                        logger.info(f"Semantic Scholar match found first result: {items[0]}")
                        returned_title = items[0].get("title")
                        returned_authors = []
                        authorship = items[0].get("authors") or []
                        for author in authorship:
                            a_name = author.get("name")
                            if a_name:
                                returned_authors.append(a_name)
                        details = {
                            "source": "semantic_scholar",
                            "unverified_fields": "title, author",
                            "returned_values": {
                                "title": returned_title,
                                "author": ", ".join(returned_authors)
                            },
                        }
                        return "warning", "Unverified details", details
                    break  # try next query

                if r.status_code == 429 and attempt < 3:
                    ra = r.headers.get("Retry-After")
                    try:
                        sleep_s = float(ra) if ra is not None else 0.0
                    except ValueError:
                        sleep_s = 0.0
                    # exponential backoff with jitter
                    backoff = max(sleep_s, (2 ** attempt)) + random.uniform(0, 0.5)
                    time.sleep(backoff)
                    attempt += 1
                    continue

                if 400 <= r.status_code < 500:
                    last_client_error = f"semantic scholar query rejected ({r.status_code})"
                    logger.info(f"Semantic Scholar Search failed: {last_client_error}")
                    break  # try next query

                r.raise_for_status()



    # Nothing matched strictly; as a fallback, return top unfiltered candidates from the last search
    if last_client_error:
        return "error", last_client_error, None
    return "no_match", "not found in Semantic Scholar", None


def verify_journal_citation(
  primary_full: FullCitation | None,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None = None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Verify a journal citation using OpenAlex.

    Args:
        citation: The journal citation to verify.

    Returns:
        A tuple of (status, error_message, data) where:
        - status is "verified" if found, "no_match" if not found, or "error" on failure
        - error_message is None on success or an error description on failure
        - data is the OpenAlex work data if verified, otherwise None
    """

    validation = _verify_author_title_with_openalex(
        citation=primary_full,
        resource_dict=resource_dict,
    )

    if validation[0] == "verified":
        logger.info(f"Journal citation verified by OpenAlex: {primary_full}")
        return validation

    validation = _verify_title_with_semantic_scholar(
        primary_full=primary_full,
        resource_dict=resource_dict,
    )
    if validation[0] == "verified" or validation[0] == "warning":
        logger.info(f"Journal citation verified by Semantic Scholar: {primary_full}")
        return validation

    return "no_match", "Not found in OpenAlex or Semantic Scholar", None
