from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Tuple

import httpx
from eyecite.models import FullCitation

from utils.cleaner import clean_str, normalize_case_name_for_compare
from utils.logger import get_logger

logger = get_logger()

_COURT_LISTENER_LOOKUP_URL = "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
_COURT_LISTENER_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=5.0)
_COURT_LISTENER_TOKEN_ENV = "COURTLISTENER_API_TOKEN"

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
    if isinstance(value, str):
        match = re.search(r"(1[6-9]\d{2}|20\d{2}|2100)", value)
        if match:
            return match.group(0)
    return None


def _extract_lookup_case_name(payload: Dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None

    candidate_fields = (
        "case_name",
        "case_name_short",
        "case_name_full",
        "short_name",
        "caseName",
        "caseNameShort",
        "caseNameFull",
        "style_of_cause",
        "style_of_case",
        "name",
    )

    for field in candidate_fields:
        value = payload.get(field)
        cleaned = clean_str(value)
        if cleaned:
            return cleaned

    clusters = payload.get("clusters")
    if isinstance(clusters, list) and clusters:
        nested_case = clusters[0]
        if isinstance(nested_case, dict):
            return _extract_lookup_case_name(nested_case)

    return None


def _extract_lookup_case_year(payload: Dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None

    candidate_fields = (
        "year",
        "year_filed",
        "year_decided",
        "decision_date",
        "date_filed",
        "dateFiled",
    )

    for field in candidate_fields:
        value = payload.get(field)
        year = _extract_year_from_value(value)
        if year:
            return year

    clusters = payload.get("clusters")
    if isinstance(clusters, list) and clusters:
        nested_case = clusters[0]
        if isinstance(nested_case, dict):
            return _extract_lookup_case_year(nested_case)

    return None


def _lookup_case_citation(
    volume: str | None,
    reporter: str | None,
    page: str | None,
) -> Tuple[str, str | None, Dict[str, Any]]:
    if not volume or not reporter or not page:
        return "warning", "missing_lookup_fields", {}

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
        logger.warning(
            "CourtListener lookup failed for volume=%s reporter=%s page=%s: %s",
            volume,
            reporter,
            page,
            exc,
        )
        return "warning", "lookup_failed", {}

    if response.status_code == 401:
        return "warning", "lookup_auth_failed", {}
    if response.status_code == 403:
        return "warning", "lookup_forbidden", {}
    if response.status_code == 400:
        logger.warning(
            "CourtListener lookup rejected payload volume=%s reporter=%s page=%s: %s",
            volume,
            reporter,
            page,
            response.text,
        )
        return "warning", "lookup_bad_request", {}
    if response.status_code >= 500:
        return "warning", "lookup_service_error", {}
    if response.status_code != 200:
        logger.warning(
            "CourtListener lookup unexpected status %s for %s",
            response.status_code,
            request_payload,
        )
        return "warning", "lookup_unexpected_status", {}

    try:
        payload = response.json()
    except ValueError:
        logger.warning(
            "CourtListener lookup returned non-JSON response for %s",
            request_payload,
        )
        return "warning", "lookup_invalid_payload", {}

    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            first = results[0] if results else None
            if first is None:
                return "no match", None, payload
            return "ok", None, first
        if payload:
            return "ok", None, payload
        return "no match", None, {}

    if isinstance(payload, list):
        first = payload[0] if payload else None
        if first is None:
            return "no match", None, {}
        return "ok", None, first

    return "warning", "lookup_unrecognized_payload", {}


def _prepare_case_lookup_fields(
    primary_full: FullCitation | None,
    resource_dict: Dict[str, Any] | None,
    normalized_key: str | None,
) -> Tuple[str | None, str | None, str | None]:
    volume = None
    reporter = None
    page = None

    resource_dict = resource_dict or {}

    if primary_full is not None:
        groups = getattr(primary_full, "groups", {}) or {}
        volume = volume or clean_str(groups.get("volume"))
        reporter = reporter or clean_str(groups.get("reporter"))
        page = page or clean_str(groups.get("page"))

    if (not volume or not reporter or not page) and isinstance(primary_full, FullCitation):
        volume = volume or clean_str(getattr(primary_full, "volume", None))
        page = page or clean_str(getattr(primary_full, "page", None))

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

def get_case_name(obj) -> str | None:
    if obj is None:
        return None
    metadata = getattr(obj, "metadata", None)
    if metadata is not None:
        plaintiff = clean_str(
            getattr(metadata, "plaintiff", None)
            or getattr(metadata, "petitioner", None)
        )
        defendant = clean_str(
            getattr(metadata, "defendant", None)
            or getattr(metadata, "respondent", None)
        )
        if plaintiff and defendant:
            return clean_str(f"{plaintiff} v. {defendant}")
    return None

def verify_case_citation(
    primary_full: FullCitation | None,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None = None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    citation_text = clean_str(normalized_key) or clean_str(fallback_citation)

    volume, reporter, page = _prepare_case_lookup_fields(
        primary_full,
        resource_dict,
        citation_text,
    )

    lookup_status, lookup_substatus, lookup_payload = _lookup_case_citation(volume, reporter, page)

    if lookup_status != "ok":
        if lookup_status == "no match":
            return "no match", None, None
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
        return "no match", None, None

    expected_name = get_case_name(primary_full)
    if not expected_name and primary_full is not None:
        metadata = getattr(primary_full, "metadata", None)
        if metadata is not None:
            for attr in ("short_name", "case_name", "case_name_full"):
                value = clean_str(getattr(metadata, attr, None))
                if value:
                    expected_name = value
                    break
        if not expected_name:
            for attr in ("short_name", "case_name", "case_name_full"):
                value = clean_str(getattr(primary_full, attr, None))
                if value:
                    expected_name = value
                    break

    expected_year = None
    if primary_full is not None:
        expected_year = _extract_year_from_value(getattr(primary_full, "year", None))
        if not expected_year:
            metadata = getattr(primary_full, "metadata", None)
            if metadata is not None:
                for attr in ("decision_date", "date_filed"):
                    expected_year = _extract_year_from_value(getattr(metadata, attr, None))
                    if expected_year:
                        break
    if not expected_year:
        resource_dict = resource_dict or {}
        id_tuple = resource_dict.get("id_tuple")
        if isinstance(id_tuple, tuple) and len(id_tuple) >= 5:
            expected_year = _extract_year_from_value(id_tuple[4])

    actual_name = clean_str(_extract_lookup_case_name(lookup_payload))
    actual_year = _extract_year_from_value(_extract_lookup_case_year(lookup_payload))

    expected_name_norm = normalize_case_name_for_compare(expected_name)
    actual_name_norm = normalize_case_name_for_compare(actual_name)

    mismatches: List[str] = []

    if expected_name_norm and actual_name_norm:
        if expected_name_norm != actual_name_norm:
            mismatches.append("case_name")
    elif expected_name_norm or actual_name_norm:
        mismatches.append("case_name")

    if expected_year and actual_year:
        if expected_year != actual_year:
            mismatches.append("year")
    elif expected_year or actual_year:
        mismatches.append("year")

    if mismatches:
        substatus = "case_name_and_year_mismatch" if len(mismatches) == 2 else f"{mismatches[0]}_mismatch"
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
    "get_case_name",
    "verify_case_citation",
]
