from __future__ import annotations

import os
from typing import Any, Dict, Tuple

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

_SEMANTIC_SCHOLAR_API_KEY = "SEMANTIC_SCHOLAR_API_KEY"


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

def _verify_with_semantic_scholar(primary_full: FullCitation | None, resource_dict: Dict[str, Any] | None
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

    return "not_match", "not found in Semantic Scholar", None

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
    
    validation = _verify_with_semantic_scholar(
        primary_full=primary_full,
        resource_dict=resource_dict,
    )
    if validation[0] == "verified":
        logger.info(f"Journal citation verified by Semantic Scholar: {primary_full}")
        return validation

    return "no_match", "not found in OpenAlex", None
