# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Tuple

import httpx
from eyecite.models import FullCitation
from rapidfuzz import fuzz, process

from utils.cleaner import clean_str, normalize_case_name_for_compare
from utils.logger import get_logger
from utils.resource_resolver import resolve_case_name

logger = get_logger()

_COURT_LISTENER_LOOKUP_URL = "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
_COURT_LISTENER_TIMEOUT = httpx.Timeout(20.0, connect=10.0, read=10.0)
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

    if response.status_code == 401:
        return "error", "lookup_auth_failed", {}
    if response.status_code == 403:
        return "error", "lookup_forbidden", {}
    if response.status_code == 400:
        logger.error(
            "CourtListener lookup rejected payload volume=%s reporter=%s page=%s: %s",
            volume,
            reporter,
            page,
            response.text,
        )
        return "error", "lookup_bad_request", {}
    if response.status_code >= 500:
        return "error", "lookup_service_error", {}
    if response.status_code != 200:
        logger.error(
            "CourtListener lookup unexpected status %s for %s",
            response.status_code,
            request_payload,
        )
        return "error", "lookup_unexpected_status", {}

    try:
        payload = response.json()
    except ValueError:
        logger.error(
            "CourtListener lookup returned non-JSON response for %s",
            request_payload,
        )
        return "error", "lookup_invalid_payload", {}

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

    return "error", "lookup_unrecognized_payload", {}


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
    case_name = None
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
            case_name = clean_str(f"{plaintiff} v. {defendant}")

        if plaintiff is None and defendant is not None:
            case_name = f"In re {defendant}"

    return resolve_case_name(case_name, obj)

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
    if expected_name is None and primary_full is not None:
        metadata = getattr(primary_full, "metadata", None)
        if metadata is not None:
            expected_name = clean_str(getattr(metadata, "resolved_case_name", None))
            if not expected_name:
                expected_name = clean_str(getattr(metadata, "resolved_case_name_short", None))

    expected_year = None
    if primary_full is not None:
        expected_year = getattr(primary_full, "year", None)
        if not expected_year:
            metadata = getattr(primary_full, "metadata", None)
            if metadata is not None:
                expected_year = getattr(metadata, "year", None)
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
            if result is None:
                mismatches.append("case_name")
    elif expected_name_norm or actual_name_norm:
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
    "get_case_name",
    "verify_case_citation",
]
