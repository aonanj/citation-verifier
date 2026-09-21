# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import httpx
from rapidfuzz import fuzz, process

from svc.citation_record import CitationRecord
from utils.case_name_normalizer import case_names_equivalent
from utils.cleaner import clean_str, normalize_case_name_for_compare
from utils.logger import get_logger

logger = get_logger()

_COURT_LISTENER_LOOKUP_URL = "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
_COURT_LISTENER_TIMEOUT = httpx.Timeout(20.0, connect=10.0, read=10.0)
_COURT_LISTENER_TOKEN_ENV = "COURTLISTENER_API_TOKEN"

# The text lookup looks up at most 250 citations per request; any past that
# come back with a per-citation status of 429. The API throttles at 60 valid
# citations per minute, but a request sent while under that budget is served
# in full, so one text request replaces up to 250 volume/reporter/page ones.
_COURT_LISTENER_BATCH_LIMIT = 250
_COURT_LISTENER_BATCH_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_BATCH_SEPARATOR = "; "

CaseTriad = Tuple[str, str, str]
LookupResult = Tuple[str, str | None, Dict[str, Any]]

def _courtlistener_headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    token = os.getenv(_COURT_LISTENER_TOKEN_ENV)
    if token:
        headers["Authorization"] = f"Token {token.strip()}"
    return headers

def _extract_year_from_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        if 1000 <= value <= 9999:
            return str(value)
        return None
    if value.isdigit() and len(value) == 4:
        return value
    if isinstance(value, str):
        match = re.search(r"(1[6-9]\d{2}|20\d{2}|2100)", value)
        if match:
            return match.group(0)
    return None


def _extract_lookup_case_name(payload: Dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None

    clusters = payload.get("clusters", None)
    if clusters is not None and isinstance(clusters, list) and len(clusters) > 0:
        cluster = clusters[0]
        if isinstance(cluster, dict):
            cn = clean_str(cluster.get("case_name", None))
            if cn is not None:
                return cn
            elif cluster.get("case_name_short", None) is not None:
                return clean_str(cluster.get("case_name_short", None))
            elif cluster.get("case_name_full", None) is not None:
                return clean_str(cluster.get("case_name_full", None))

    return None


def _extract_lookup_case_year(payload: Dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None

    clusters = payload.get("clusters", None)
    if clusters is not None and isinstance(clusters, list) and len(clusters) > 0:
        cluster = clusters[0]
        if isinstance(cluster, dict):
            decision_date = cluster.get("date_filed", None)
            year = _extract_year_from_value(decision_date)
            if year is not None:
                return year

    return None


def _lookup_response_json(response: httpx.Response, request: Any) -> Tuple[str, str | None, Any]:
    """Return ("ok", None, parsed JSON) for a 200 lookup response, else the error triple."""
    if response.status_code == 401:
        return "error", "lookup_auth_failed", {}
    if response.status_code == 403:
        return "error", "lookup_forbidden", {}
    if response.status_code == 400:
        logger.error("CourtListener lookup rejected payload %s: %s", request, response.text)
        return "error", "lookup_bad_request", {}
    if response.status_code >= 500:
        return "error", "lookup_service_error", {}
    if response.status_code != 200:
        logger.error(
            "CourtListener lookup unexpected status %s for %s",
            response.status_code,
            request,
        )
        return "error", "lookup_unexpected_status", {}

    try:
        return "ok", None, response.json()
    except ValueError:
        logger.error("CourtListener lookup returned non-JSON response for %s", request)
        return "error", "lookup_invalid_payload", {}


def _lookup_case_citation(
    volume: str | None,
    reporter: str | None,
    page: str | None,
) -> Tuple[str, str | None, Dict[str, Any]]:
    if not volume or not reporter or not page:
        return "error", "missing_lookup_fields", {}

    request_payload = {
        "volume": volume,
        "reporter": reporter,
        "page": page,
    }

    try:
        response = httpx.post(
            _COURT_LISTENER_LOOKUP_URL,
            json=request_payload,
            headers=_courtlistener_headers(),
            timeout=_COURT_LISTENER_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        logger.error(
            "CourtListener lookup failed for volume=%s reporter=%s page=%s: %s",
            volume,
            reporter,
            page,
            exc,
        )
        return "error", "lookup_failed", {}

    status, substatus, payload = _lookup_response_json(response, request_payload)
    if status != "ok":
        return status, substatus, payload

    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            first = results[0] if results else None
            if first is None:
                return "no_match", None, payload
            return "ok", None, first
        if payload:
            return "ok", None, payload
        return "no_match", None, {}

    if isinstance(payload, list):
        first = payload[0] if payload else None
        if first is None:
            return "no_match", None, {}
        return "ok", None, first

    return "error", "lookup_unrecognized_payload", {}


def _lookup_case_citation_chunk(chunk: Sequence[CaseTriad]) -> Dict[CaseTriad, LookupResult]:
    """Look up up to _COURT_LISTENER_BATCH_LIMIT triads with one text request.

    Each result is mapped back to its triad by start_index. Triads missing
    from the result are left out of the returned dict.
    """
    offsets: List[Tuple[int, int, CaseTriad]] = []
    position = 0
    for triad in chunk:
        citation_length = len(" ".join(triad))
        offsets.append((position, position + citation_length, triad))
        position += citation_length + len(_BATCH_SEPARATOR)
    text = _BATCH_SEPARATOR.join(" ".join(triad) for triad in chunk)
    request = f"text lookup of {len(chunk)} citations"

    try:
        response = httpx.post(
            _COURT_LISTENER_LOOKUP_URL,
            json={"text": text},
            headers=_courtlistener_headers(),
            timeout=_COURT_LISTENER_BATCH_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        logger.error("CourtListener %s failed: %s", request, exc)
        return {triad: ("error", "lookup_failed", {}) for triad in chunk}

    status, substatus, payload = _lookup_response_json(response, request)
    if status != "ok":
        return {triad: (status, substatus, {}) for triad in chunk}
    if not isinstance(payload, list):
        logger.error("CourtListener %s returned an unrecognized payload", request)
        return {}

    results: Dict[CaseTriad, LookupResult] = {}
    for item in payload:
        start = item.get("start_index") if isinstance(item, dict) else None
        if not isinstance(start, int):
            continue
        triad = next((t for s, e, t in offsets if s <= start < e), None)
        if triad is None or triad in results:
            continue
        if item.get("status") == 429:
            # Past the per-request limit: parsed but not looked up.
            results[triad] = ("error", "lookup_unexpected_status", {})
        else:
            results[triad] = ("ok", None, item)
    return results


def lookup_case_citations_batch(
    triads: Iterable[Tuple[str | None, str | None, str | None]],
) -> Dict[CaseTriad, LookupResult]:
    """Look up volume/reporter/page triads with as few requests as possible.

    Returns the same (status, substatus, payload) per triad as
    _lookup_case_citation. A triad the text lookup doesn't return falls back to
    its own triad request; incomplete triads are skipped (verify_case_citation
    reports them as missing_lookup_fields without a request).
    """
    unique: List[CaseTriad] = list(dict.fromkeys(t for t in triads if all(t)))
    results: Dict[CaseTriad, LookupResult] = {}
    for chunk_start in range(0, len(unique), _COURT_LISTENER_BATCH_LIMIT):
        results.update(
            _lookup_case_citation_chunk(unique[chunk_start:chunk_start + _COURT_LISTENER_BATCH_LIMIT])
        )
    missing = [triad for triad in unique if triad not in results]
    if missing:
        logger.info("CourtListener text lookup missed %d citation(s); looking them up singly", len(missing))
    for triad in missing:
        results[triad] = _lookup_case_citation(*triad)
    return results


def _prepare_case_lookup_fields(
    primary_full: CitationRecord | None,
    resource_dict: Dict[str, Any] | None,
    normalized_key: str | None,
) -> Tuple[str | None, str | None, str | None]:
    volume = None
    reporter = None
    page = None

    resource_dict = resource_dict or {}

    if primary_full is not None:
        volume = clean_str(primary_full.get("volume"))
        reporter = clean_str(primary_full.get("reporter"))
        page = clean_str(primary_full.get("page"))

    id_tuple = resource_dict.get("id_tuple")
    if isinstance(id_tuple, tuple):
        if len(id_tuple) >= 3:
            reporter = reporter or clean_str(id_tuple[1])
            volume = volume or clean_str(id_tuple[2])
        if len(id_tuple) >= 4:
            page = page or clean_str(id_tuple[3])

    if (not volume or not reporter or not page) and normalized_key:
        match = re.search(
            r"(?P<volume>\d+)\s+(?P<reporter>[\w\.'-]+(?:\s[\w\.'-]+)*)\s+(?P<page>\d+)",
            normalized_key,
        )
        if match:
            volume = volume or clean_str(match.group("volume"))
            reporter = reporter or clean_str(match.group("reporter"))
            page = page or clean_str(match.group("page"))

    return volume, reporter, page


def case_lookup_triad(
    primary_full: CitationRecord | None,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None = None,
) -> Tuple[str | None, str | None, str | None]:
    """The (volume, reporter, page) verify_case_citation looks up."""
    citation_text = clean_str(normalized_key) or clean_str(fallback_citation)
    return _prepare_case_lookup_fields(primary_full, resource_dict, citation_text)

def verify_case_citation(
    primary_full: CitationRecord | None,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None = None,
    lookup: LookupResult | None = None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Verify a case citation against CourtListener.

    `lookup` is this citation's result from lookup_case_citations_batch; when
    omitted, the citation is looked up on its own.
    """
    volume, reporter, page = case_lookup_triad(
        primary_full,
        normalized_key,
        resource_dict,
        fallback_citation,
    )

    if lookup is None:
        lookup = _lookup_case_citation(volume, reporter, page)
    lookup_status, lookup_substatus, lookup_payload = lookup

    if lookup_status != "ok":
        if lookup_status == "no_match" or lookup_substatus == "no match":
            return "no_match", None, None
        details = None
        if lookup_substatus == "missing_lookup_fields":
            details = {
                "source": "courtlistener",
                "lookup_request": {
                    "volume": volume,
                    "reporter": reporter,
                    "page": page,
                },
            }
        return lookup_status, lookup_substatus, details

    if not lookup_payload:
        return "no_match", None, None

    expected_name = primary_full.get("case_name") if primary_full is not None else None
    expected_year = primary_full.get("year") if primary_full is not None else None
    if not expected_year:
        resource_dict = resource_dict or {}
        id_tuple = resource_dict.get("id_tuple")
        if isinstance(id_tuple, tuple) and len(id_tuple) >= 4:
            expected_year = _extract_year_from_value(id_tuple[-1])

    actual_name = clean_str(_extract_lookup_case_name(lookup_payload))
    actual_year = _extract_year_from_value(_extract_lookup_case_year(lookup_payload))

    expected_name_norm = normalize_case_name_for_compare(expected_name)
    actual_name_norm = normalize_case_name_for_compare(actual_name)

    mismatches: List[str] = []

    if expected_name_norm and actual_name_norm:
        if expected_name_norm != actual_name_norm:
            result = process.extractOne(
                expected_name_norm,
                [actual_name_norm],
                scorer=fuzz.partial_ratio,
                score_cutoff=75
            )
            if result is None and not case_names_equivalent(expected_name, actual_name):
                mismatches.append("case_name")
    elif expected_name_norm:
        # The document names the case but CourtListener returned no name. When
        # the document gives no name (e.g. a bare "447 U.S. 303 (1980)"), there
        # is nothing to compare, so the name check is skipped.
        mismatches.append("case_name")

    if expected_year is not None and actual_year is not None:
        cleaned_expected_year = int(clean_str(str(expected_year)) or expected_year)
        cleaned_actual_year = int(clean_str(str(actual_year)) or actual_year)

        if cleaned_expected_year != cleaned_actual_year:
            mismatches.append("year")
    elif expected_year is None and actual_year is not None:
        mismatches.append("year")
    elif expected_year is not None and actual_year is None:
        mismatches.append("year")


    if len(mismatches) > 0:
        substatus = "Mismatch at "
        substatus += " (1) case name, (2) year" if len(mismatches) == 2 else f"{mismatches[0]}"
        details = {
            "source": "courtlistener",
            "mismatched_fields": mismatches,
            "extracted": {
                "case_name": expected_name,
                "year": expected_year,
            },
            "court_listener": {
                "case_name": actual_name,
                "year": actual_year,
            },
            "lookup_request": {
                "volume": volume,
                "reporter": reporter,
                "page": page,
            },
        }
        return "warning", substatus, details

    return "verified", None, None


__all__ = [
    "case_lookup_triad",
    "lookup_case_citations_batch",
    "verify_case_citation",
]
