# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

"""Verification for secondary legal sources using Library of Congress API.

This module verifies citations to secondary legal sources such as legal encyclopedias
(C.J.S., Am. Jur.), restatements, and treatises by querying
the Library of Congress Search API and performing fuzzy matching on results.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

import httpx
from rapidfuzz import fuzz

from utils.cleaner import clean_str, normalize_case_name_for_compare
from utils.logger import get_logger

logger = get_logger()

# Library of Congress Search API configuration
_LOC_SEARCH_URL = "https://www.loc.gov/search/"
_LOC_TIMEOUT = httpx.Timeout(15.0, connect=10.0, read=10.0)
_LOC_MAX_RETRIES = 3
_LOC_BACKOFF_FACTOR = 2.0

# Match thresholds for fuzzy string matching
_TITLE_MATCH_THRESHOLD = 72  # Minimum similarity score for title matches
_CONTAINER_MATCH_THRESHOLD = 65  # Minimum similarity score for container/series
_AUTHOR_MATCH_THRESHOLD = 70  # Minimum similarity score for author names

# Source type display names for logging and error messages
_SOURCE_TYPE_NAMES = {
    "cjs": "Corpus Juris Secundum",
    "amjur": "American Jurisprudence",
    "alr": "American Law Reports",
    "restatement": "Restatement",
    "treatise": "Treatise",
}


def _clean_value(value: Any) -> str:
    """Clean and normalize a value for comparison.
    
    Args:
        value: Value to clean.
        
    Returns:
        Cleaned string, or empty string if value is None/empty.
    """
    return clean_str(value) or ""


def _extract_year(text: str) -> str | None:
    """Extract a four-digit year from text.
    
    Args:
        text: Text potentially containing a year.
        
    Returns:
        Extracted year as string, or None if no year found.
    """
    if not text:
        return None
    
    import re
    match = re.search(r"\b(17|18|19|20)\d{2}\b", text)
    if match:
        return match.group(0)
    return None


def _similarity_score(a: str, b: str) -> float:
    """Calculate similarity between two strings using partial ratio.
    
    Uses RapidFuzz's partial_ratio which handles substring matches well.
    
    Args:
        a: First string.
        b: Second string.
        
    Returns:
        Similarity score from 0.0 to 100.0.
    """
    if not a or not b:
        return 0.0
    
    a_norm = normalize_case_name_for_compare(a) or a.lower()
    b_norm = normalize_case_name_for_compare(b) or b.lower()
    
    return fuzz.partial_ratio(a_norm, b_norm)


def _extract_citation_fields(
    cite: Any,
    resource_dict: Dict[str, Any] | None,
) -> Dict[str, str]:
    """Extract citation fields from citation object or resource dict.
    
    Args:
        cite: Citation object (could be SecondaryCitation or similar).
        resource_dict: Resource metadata dictionary.
        
    Returns:
        Dictionary with cleaned citation fields.
    """
    fields: Dict[str, str] = {}
    
    # Try to get fields from citation object first
    for field in ["source_type", "volume", "title", "section", "page", 
                  "year", "edition", "series", "author"]:
        value = getattr(cite, field, None)
        if value:
            fields[field] = _clean_value(value)
    
    # Fall back to resource_dict if needed
    if resource_dict:
        for field, value in resource_dict.items():
            if field not in fields and value:
                fields[field] = _clean_value(value)
        
        # Extract from id_tuple if present
        id_tuple = resource_dict.get("id_tuple")
        if isinstance(id_tuple, tuple):
            if len(id_tuple) > 0 and "volume" not in fields:
                fields["volume"] = _clean_value(id_tuple[0])
            if len(id_tuple) > 1 and "title" not in fields:
                fields["title"] = _clean_value(id_tuple[1])
            if len(id_tuple) > 2 and "section" not in fields:
                fields["section"] = _clean_value(id_tuple[2])
            if len(id_tuple) > 3 and "year" not in fields:
                fields["year"] = _clean_value(id_tuple[3])
    
    return fields


def _build_search_queries(fields: Dict[str, str]) -> List[str]:
    """Build search queries for Library of Congress API.
    
    Creates multiple query variants from most specific to least specific
    to maximize chance of finding a match.
    
    Args:
        fields: Dictionary of citation fields.
        
    Returns:
        List of search query strings, ordered by specificity.
    """
    queries: List[str] = []
    
    source_type = fields.get("source_type", "")
    title = fields.get("title", "")
    volume = fields.get("volume", "")
    section = fields.get("section", "")
    year = fields.get("year", "")
    author = fields.get("author", "")
    series = fields.get("series", "")
    
    # Get full name for source type
    source_name = _SOURCE_TYPE_NAMES.get(source_type, "")
    
    # Query 1: Most specific - full details
    if source_name and title and volume:
        parts = [source_name, title]
        if volume:
            parts.append(f"volume {volume}")
        if section:
            parts.append(f"section {section}")
        if year:
            parts.append(year)
        queries.append(" ".join(parts))
    
    # Query 2: Source name and title with year
    if source_name and title and year:
        queries.append(f"{source_name} {title} {year}")
    
    # Query 3: Source name and title (no year)
    if source_name and title:
        queries.append(f"{source_name} {title}")
    
    # Query 4: For treatises, use author
    if author and title:
        parts = [author, title]
        if year:
            parts.append(year)
        queries.append(" ".join(parts))
    
    # Query 5: Title only (least specific)
    if title:
        parts = [title]
        if volume:
            parts.append(f"volume {volume}")
        if year:
            parts.append(year)
        queries.append(" ".join(parts))
    
    # Query 6: For A.L.R., try series-specific search
    if source_type == "alr" and series:
        parts = [f"American Law Reports {series}"]
        if volume:
            parts.append(f"volume {volume}")
        queries.append(" ".join(parts))
    
    # Remove duplicates while preserving order
    seen = set()
    unique_queries = []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            unique_queries.append(q)
    
    return unique_queries


def _execute_loc_search(
    query: str,
    attempt: int = 0,
) -> Tuple[List[Dict[str, Any]], str | None]:
    """Execute a search against the Library of Congress API.
    
    Includes retry logic with exponential backoff for transient failures.
    
    Args:
        query: Search query string.
        attempt: Current retry attempt number (for backoff calculation).
        
    Returns:
        Tuple of (results list, error message). Error message is None on success.
    """
    params = {
        "q": query,
        "fo": "json",
        "at": "results,pagination",
    }
    
    try:
        with httpx.Client(timeout=_LOC_TIMEOUT) as client:
            response = client.get(_LOC_SEARCH_URL, params=params)
            response.raise_for_status()
            
        data = response.json()
        if not isinstance(data, dict):
            logger.error("LOC API returned non-dict response for query: %s", query)
            return [], "invalid_response_format"
        
        results = data.get("results", [])
        if not isinstance(results, list):
            logger.error("LOC API results is not a list for query: %s", query)
            return [], "invalid_results_format"
        
        logger.info("LOC API returned %d results for query: %s", len(results), query)
        return results, None
        
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429 and attempt < _LOC_MAX_RETRIES:
            # Rate limited - retry with exponential backoff
            backoff = _LOC_BACKOFF_FACTOR ** attempt
            logger.error(
                "LOC API rate limited (429), retrying in %.1f seconds (attempt %d/%d)",
                backoff,
                attempt + 1,
                _LOC_MAX_RETRIES,
            )
            time.sleep(backoff)
            return _execute_loc_search(query, attempt + 1)
        
        logger.error("LOC API HTTP error for query '%s': %s", query, exc)
        return [], f"http_error_{exc.response.status_code}"
        
    except httpx.RequestError as exc:
        if attempt < _LOC_MAX_RETRIES:
            backoff = _LOC_BACKOFF_FACTOR ** attempt
            logger.error(
                "LOC API request error, retrying in %.1f seconds (attempt %d/%d): %s",
                backoff,
                attempt + 1,
                _LOC_MAX_RETRIES,
                exc,
            )
            time.sleep(backoff)
            return _execute_loc_search(query, attempt + 1)
        
        logger.error("LOC API request failed for query '%s': %s", query, exc)
        return [], "request_failed"
        
    except Exception as exc:
        logger.error("Unexpected error during LOC search for '%s': %s", query, exc)
        return [], "unexpected_error"


def _match_result_to_citation(
    result: Dict[str, Any],
    fields: Dict[str, str],
) -> Tuple[bool, float, Dict[str, Any]]:
    """Determine if a LOC search result matches the citation.
    
    Performs fuzzy matching on title, container, author, and year fields.
    
    Args:
        result: Search result from LOC API.
        fields: Citation fields to match against.
        
    Returns:
        Tuple of (is_match, confidence_score, match_details).
        - is_match: True if result matches citation with sufficient confidence.
        - confidence_score: Float from 0.0 to 1.0 indicating match quality.
        - match_details: Dictionary with matching field details.
    """
    # Extract fields from result
    result_title = _clean_value(result.get("title"))
    result_partof = _clean_value(result.get("partof"))
    result_date = _clean_value(result.get("date"))
    result_contributors = result.get("contributors", [])
    
    # Extract fields from citation
    cite_title = fields.get("title", "")
    cite_author = fields.get("author", "")
    cite_year = fields.get("year", "")
    cite_source_type = fields.get("source_type", "")
    cite_volume = fields.get("volume", "")
    
    source_name = _SOURCE_TYPE_NAMES.get(cite_source_type, "")
    
    match_details: Dict[str, Any] = {
        "matched_fields": [],
        "scores": {},
    }
    
    scores: List[float] = []
    
    # Title matching
    title_score = 0.0
    if cite_title and result_title:
        title_score = _similarity_score(cite_title, result_title)
        match_details["scores"]["title"] = title_score
        
        if title_score >= _TITLE_MATCH_THRESHOLD:
            match_details["matched_fields"].append("title")
            scores.append(title_score / 100.0)
            logger.info(
                "Title match: '%s' ~ '%s' (score: %.1f)",
                cite_title,
                result_title,
                title_score,
            )
    
    # Container/Series matching (for encyclopedias and A.L.R.)
    container_score = 0.0
    if source_name and (result_title or result_partof):
        # Check if source name appears in result
        combined_result = f"{result_title} {result_partof}".lower()
        if source_name.lower() in combined_result:
            container_score = 100.0
            match_details["matched_fields"].append("source")
            scores.append(1.0)
            logger.info("Source name '%s' found in result", source_name)
        else:
            # Try fuzzy match on result_partof
            container_score = _similarity_score(source_name, result_partof)
            match_details["scores"]["container"] = container_score
            
            if container_score >= _CONTAINER_MATCH_THRESHOLD:
                match_details["matched_fields"].append("source")
                scores.append(container_score / 100.0)
                logger.info(
                    "Container match: '%s' ~ '%s' (score: %.1f)",
                    source_name,
                    result_partof,
                    container_score,
                )
    
    # Volume matching (if present in title or partof)
    if cite_volume:
        volume_patterns = [
            f"volume {cite_volume}",
            f"vol. {cite_volume}",
            f"v. {cite_volume}",
        ]
        combined_result = f"{result_title} {result_partof}".lower()
        
        for pattern in volume_patterns:
            if pattern in combined_result:
                match_details["matched_fields"].append("volume")
                scores.append(1.0)
                logger.info("Volume %s found in result", cite_volume)
                break
    
    # Author matching (for treatises)
    if cite_author and result_contributors:
        max_author_score = 0.0
        matched_contributor = None
        
        for contributor in result_contributors:
            if not isinstance(contributor, str):
                continue
            
            author_score = _similarity_score(cite_author, contributor)
            if author_score > max_author_score:
                max_author_score = author_score
                matched_contributor = contributor
        
        match_details["scores"]["author"] = max_author_score
        
        if max_author_score >= _AUTHOR_MATCH_THRESHOLD:
            match_details["matched_fields"].append("author")
            scores.append(max_author_score / 100.0)
            logger.info(
                "Author match: '%s' ~ '%s' (score: %.1f)",
                cite_author,
                matched_contributor,
                max_author_score,
            )
    
    # Year matching
    if cite_year and result_date:
        result_year = _extract_year(result_date)
        if result_year == cite_year:
            match_details["matched_fields"].append("year")
            scores.append(1.0)
            logger.info("Year match: %s", cite_year)
        elif result_year:
            # Allow close years (within 2 years for different editions)
            try:
                year_diff = abs(int(cite_year) - int(result_year))
                if year_diff <= 2:
                    match_details["matched_fields"].append("year_approximate")
                    scores.append(0.8)
                    logger.info(
                        "Approximate year match: %s ~ %s",
                        cite_year,
                        result_year,
                    )
            except ValueError:
                pass
    
    # Calculate overall confidence
    if not scores:
        return False, 0.0, match_details
    
    confidence = sum(scores) / len(scores)
    match_details["confidence"] = confidence
    match_details["result_title"] = result_title
    match_details["result_date"] = result_date
    
    # Require at least title OR (source + volume) match
    has_title_match = "title" in match_details["matched_fields"]
    has_source_match = "source" in match_details["matched_fields"]
    has_volume_match = "volume" in match_details["matched_fields"]
    
    is_match = (
        (has_title_match and confidence >= 0.7) or
        (has_source_match and has_volume_match and confidence >= 0.6) or
        (has_title_match and has_source_match and confidence >= 0.65)
    )
    
    return is_match, confidence, match_details


def verify_secondary_citation(
    cite: Any,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Verify a secondary source citation using Library of Congress API.
    
    Searches the LOC catalog for records matching the citation and performs
    fuzzy matching to determine if a valid match exists.
    
    Args:
        cite: Citation object (SecondaryCitation or similar).
        normalized_key: Normalized citation string.
        resource_dict: Resource metadata dictionary.
        
    Returns:
        Tuple of (status, substatus, verification_details) where:
        - status: "verified", "warning", "no_match", or "error"
        - substatus: Additional status information
        - verification_details: Dictionary with verification metadata
    """
    # Extract citation fields
    fields = _extract_citation_fields(cite, resource_dict)
    
    source_type = fields.get("source_type", "unknown")
    source_name = _SOURCE_TYPE_NAMES.get(source_type, "unknown secondary source")
    
    logger.info(
        "Verifying %s citation: %s",
        source_name,
        normalized_key or fields.get("title", "untitled"),
    )
    
    # Build search queries
    queries = _build_search_queries(fields)
    
    if not queries:
        logger.error(
            "Unable to build search queries for citation: insufficient fields"
        )
        return (
            "error",
            "insufficient_citation_data",
            {
                "source": "library_of_congress",
                "available_fields": list(fields.keys()),
            },
        )
    
    logger.info(
        "Built %d search queries for LOC API: %s",
        len(queries),
        queries[0] if queries else "none",
    )
    
    # Try each query until we find a match
    best_match: Tuple[Dict[str, Any], float, Dict[str, Any]] | None = None
    all_errors: List[str] = []
    
    for query_idx, query in enumerate(queries):
        logger.info(
            "Executing LOC search %d/%d: %s",
            query_idx + 1,
            len(queries),
            query,
        )
        
        results, error = _execute_loc_search(query)
        
        if error:
            all_errors.append(f"Query {query_idx + 1}: {error}")
            continue
        
        if not results:
            logger.info("No results for query: %s", query)
            continue
        
        # Check each result for a match
        for result_idx, result in enumerate(results):
            is_match, confidence, match_details = _match_result_to_citation(
                result, fields
            )
            
            if is_match:
                logger.info(
                    "Found match in result %d/%d (confidence: %.2f)",
                    result_idx + 1,
                    len(results),
                    confidence,
                )
                
                if best_match is None or confidence > best_match[1]:
                    best_match = (result, confidence, match_details)
                
                # If we found a high-confidence match, stop searching
                if confidence >= 0.85:
                    break
        
        # If we found a good match, stop trying other queries
        if best_match and best_match[1] >= 0.85:
            break
    
    # Determine final status based on best match
    if best_match is None:
        if all_errors:
            return (
                "error",
                "loc_search_failed",
                {
                    "source": "library_of_congress",
                    "errors": all_errors,
                    "queries_attempted": len(queries),
                },
            )
        
        return (
            "no_match",
            "Not found in Library of Congress",
            {
                "source": "library_of_congress",
                "queries_attempted": len(queries),
                "note": (
                    f"No matching record found in Library of Congress catalog "
                    f"for {source_name} citation"
                ),
            },
        )
    
    result, confidence, match_details = best_match
    
    # High confidence = verified
    if confidence >= 0.80:
        return (
            "verified",
            None,
            {
                "source": "library_of_congress",
                "confidence": round(confidence, 3),
                "matched_fields": match_details["matched_fields"],
                "loc_title": match_details.get("result_title"),
                "loc_date": match_details.get("result_date"),
            },
        )
    
    # Medium confidence = warning
    if confidence >= 0.60:
        return (
            "warning",
            "Unverified details",
            {
                "source": "library_of_congress",
                "confidence": round(confidence, 3),
                "matched_fields": match_details["matched_fields"],
                "loc_title": match_details.get("result_title"),
                "loc_date": match_details.get("result_date"),
                "note": (
                    "Found possible match but confidence is below verification "
                    "threshold. Manual review recommended."
                ),
            },
        )
    
    # Low confidence = no match
    return (
        "no_match",
        "insufficient_confidence",
        {
            "source": "library_of_congress",
            "confidence": round(confidence, 3),
            "matched_fields": match_details["matched_fields"],
            "note": (
                f"Found potential matches but confidence too low to verify. "
                f"Best match had {len(match_details['matched_fields'])} "
                f"matching fields."
            ),
        },
    )


__all__ = ["verify_secondary_citation"]
