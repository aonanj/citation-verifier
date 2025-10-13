from __future__ import annotations

import os
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
_SEMANTIC_SCHOLAR_TIMEOUT = httpx.Timeout(15.0, connect=10.0, read=10.0)
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
    "title","year","venue","journal.name","volume","pages",
    "authors.name","url","externalIds"
])


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
        return "warning", None, {"title": citation_title, "source": "openalex"}

    # Check author match - look through authorships
    if not normalized_citation_author:
        # If no author provided, just match on title
        logger.info("No citation author provided, matching on title only")
        return "warning", "No author match for title match", {"author": citation_author, "source": "openalex"}

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
    return "warning", "No author match for title match", {"author": citation_author, "source": "openalex"}

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
        return "no_match", "not a journal citation", None

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
        return "no_match", "could not build filter from citation data", None

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
        return "no_match", None, {"not found": "title", "source": "openalex"}

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
    return "no_match", "no matching result in openalex search results", {"source": "openalex"}

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
        return "no_match", None, {"not found": "title", "source": "openalex"}
    
    return "not_match", "not found in OpenAlex", None

def _verify_title_with_semantic_scholar(primary_full: FullCitation | None, resource_dict: Dict[str, Any] | None
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Verify a citation using the Semantic Scholar API with targeted, quoted field filters.

    Args:
        citation: The journal citation to verify.

    Returns:
        A tuple of (status, error_message, data) where:
        - status is "verified" if found, "no_match" if not found, or "error" on failure
        - error_message is None on success or an error description on failure
        - data is the OpenAlex work data if verified, otherwise None
    """
    if not isinstance(primary_full, FullJournalCitation):
        return "no_match", "not a journal citation", None
    
    data = None
    
    search_author = clean_str(resource_dict.get("author")) if resource_dict else None
    search_title = clean_str(resource_dict.get("title")) if resource_dict else None
    if not search_author or not search_title:
        ji = get_journal_author_title(primary_full)
        if not search_author and ji:
            search_author = clean_str(ji.get("author"))
        if not search_title and ji:
            search_title = clean_str(ji.get("title"))
    
    if search_author is None and search_title is None:
        return _verify_citation_with_semantic_scholar(primary_full, resource_dict)

    headers: Dict[str, str] = {"Accept": "application/json"}
    api_key = os.environ.get(_SEMANTIC_SCHOLAR_API_KEY)
    if api_key:
        headers["x-api-key"] = api_key

    fields = _DEFAULT_FIELDS_AUTH if api_key else _DEFAULT_FIELDS_BASIC
    params_search = {
        "query": f"\"{search_title}\"",
        "limit": str(_SEMANTIC_SCHOLAR_MAX_SEARCH),
        "fields": fields,
    }

    search_title_norm = normalize_case_name_for_compare(search_title)
    search_author_norm = normalize_case_name_for_compare(search_author)

    with httpx.Client(timeout=_SEMANTIC_SCHOLAR_TIMEOUT, headers=headers) as client:
        for attempt in range(2):
            try:
                response = client.get(
                    f"{_SEMANTIC_SCHOLAR_BASE_URL}/paper/search",
                    params=params_search,
                )
                response.raise_for_status()
                data = response.json()
                break
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code if e.response else None
                if status_code == 429 and attempt == 0:
                    logger.warning(
                        "Semantic Scholar rate limited (429) for title search '%s'; retrying after 1s",
                        search_title,
                    )
                    time.sleep(1)
                    continue
                response_text = ""
                error_response = getattr(e, "response", None)
                if error_response is not None:
                    try:
                        response_text = error_response.text
                    except Exception:
                        response_text = "<response text unavailable>"
                logger.error(
                    f"Semantic Scholar HTTP error: {e} for title search '{search_title}'. Response: {response_text}"
                )
                return "error", f"semantic scholar http error: {e}", None
            except httpx.HTTPError as e:
                response_text = ""
                error_response = getattr(e, "response", None)
                if error_response is not None:
                    try:
                        response_text = error_response.text
                    except Exception:
                        response_text = "<response text unavailable>"
                logger.error(
                    f"Semantic Scholar HTTP error: {e} for title search '{search_title}'. Response: {response_text}"
                )
                return "error", f"semantic scholar http error: {e}", None
            except Exception as e:
                logger.error(f"Semantic Scholar unknown error: {e} for title search: {search_title}")
                return "error", f"semantic scholar error: {e}", None

    papers = data.get("data", []) if data else []
    if not papers:
        logger.info(f"Semantic Scholar no match found for title='{search_title}'")
        return "not_match", "not found in Semantic Scholar", None

    for paper in papers:
        paper_title = paper.get("title")
        paper_title_norm = normalize_case_name_for_compare(paper_title)
        if not (search_title_norm and paper_title_norm):
            continue
        title_matches = (
            search_title_norm == paper_title_norm
            or search_title_norm in paper_title_norm
            or paper_title_norm in search_title_norm
        )
        if not title_matches:
            continue

        if search_author and search_author_norm:
            authors = paper.get("authors") or []
            if not isinstance(authors, list):
                authors = []
            for author in authors:
                author_name = author.get("name")
                author_norm = normalize_case_name_for_compare(author_name)
                if author_norm and (
                    search_author_norm == author_norm
                    or search_author_norm in author_norm
                    or author_norm in search_author_norm
                ):
                    logger.info(f"Semantic Scholar author match found: {author_name}")
                    return "verified", None, {"source": "semantic_scholar", "data": paper}

                if author_name:
                    similarity = process.extractOne(
                        search_author,
                        [author_name],
                        scorer=fuzz.partial_ratio,
                        score_cutoff=75,
                    )
                    if similarity:
                        logger.info(f"Semantic Scholar author similarity found: {similarity}")
                        return "verified", None, {"source": "semantic_scholar", "data": paper}
            logger.info(f"Semantic Scholar no author match found for: {search_author}")
            continue

        logger.info(f"Semantic Scholar paper match found for title='{search_title}'")
        return "verified", None, {"source": "semantic_scholar", "data": paper}

    return "not_match", "no author match for title match", {"author": search_author, "source": "semantic_scholar"}

    return "not_match", "not found in Semantic Scholar", None

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

    norm_j = _norm(journal) if journal else ""
    vol_s = str(volume).strip()
    page_s = str(page).strip()
    year_str = str(year) if year is not None else None

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
        escaped_phrase = _escape_semantic_scholar_term(phrase)
        quoted_phrase = f"\"{escaped_phrase}\"" if escaped_phrase else ""
        if quoted_phrase:
            if year_str:
                _add_query([quoted_phrase, vol_s, page_s, year_str])
            _add_query([quoted_phrase, vol_s, page_s])
        if phrase:
            if year_str:
                _add_query([phrase, vol_s, page_s, year_str])
            _add_query([phrase, vol_s, page_s])

    def filter_and_rank(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for p in items:
            pj = _journal_name(p)
            pv = (p.get("volume") or "").strip()
            fp = _first_page(p.get("pages"))
            if not pv or fp is None:
                continue
            # journal match: equality or substring either direction after normalization
            nj = _norm(pj)
            j_ok = (nj == norm_j) or (norm_j and norm_j in nj) or (nj and nj in norm_j)
            if not j_ok:
                continue
            if pv != vol_s:
                continue
            if fp != int(page):
                continue
            out.append(p)

        # Rank: exact journal equality first, then year closeness if provided
        def score(p):
            pj = _journal_name(p)
            nj = _norm(pj)
            exact = 1 if nj == norm_j else 0
            yr_pen = 0
            if year and isinstance(p.get("year"), int):
                yr_pen = abs(p["year"] - year)
            return (-exact, yr_pen)

        return sorted(out, key=score)

    last_client_error: Optional[str] = None

    with httpx.Client(timeout=_SEMANTIC_SCHOLAR_TIMEOUT, headers=headers) as client:
        for q in queries:
            params = {
                "query": q,
                "limit": _SEMANTIC_SCHOLAR_MAX_SEARCH,
                "fields": _FIELDS,
            }
            try:
                r = client.get(
                    f"{_SEMANTIC_SCHOLAR_BASE_URL}/paper/search",
                    params=params,
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code if e.response else None
                logger.warning(
                    "Semantic Scholar returned %s for query='%s': %s",
                    status_code,
                    q,
                    e,
                )
                if status_code and 400 <= status_code < 500:
                    last_client_error = f"semantic scholar query rejected ({status_code})"
                    continue
                return "error", f"semantic scholar http error: {e}", None
            except httpx.HTTPError as e:
                logger.error(f"Semantic Scholar HTTP error for query='{q}': {e}")
                return "error", f"semantic scholar http error: {e}", None

            data = r.json() or {}
            items = data.get("data", []) if isinstance(data, dict) else []
            filtered = filter_and_rank(items)
            if filtered:
                return "verified", None, {"source": "semantic_scholar", "data": filtered[0]}

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
    if validation[0] == "verified":
        logger.info(f"Journal citation verified by Semantic Scholar: {primary_full}")
        return validation

    return "no_match", "not found in OpenAlex or Semantic Scholar", None
