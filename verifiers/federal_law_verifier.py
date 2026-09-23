# Copyright © 2026 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

from __future__ import annotations

import base64
import json
import os
import re
from datetime import date
from typing import Any, Dict, Final, List, Literal, Tuple

import httpx2

import verifiers.state_law_verifier as state_law_verifier
from svc.citation_record import CitationRecord
from utils.ai_model import ai_model
from utils.cleaner import clean_str
from utils.logger import get_logger

logger = get_logger()

GOVINFO_BASE_URL = "https://www.govinfo.gov/link/"
GOVINFO_API_KEY = "GOVINFO_API_KEY"
GOVINFO_TIMEOUT = httpx2.Timeout(60.0, connect=45.0, read=60.0)

GOVINFO_REPORTER_MAP = {
    "U.S.C.": "uscode", # /uscode/{title}/{section}
    "C.F.R.": "cfr", # /cfr/{title}/{part}?sectionnum={section}
    "Stat.": "statute", # /statute/{volume}/{page}
    "Weekly Comp. Pres. Doc.": "cpd", # /cpd/{doctype}/{docnum}
    "Daily Comp. Pres. Doc.": "cpd", # /cpd/{doctype}/{docnum}
    "S.": "bills", # /bills/{congress}/{billtype}/{bill_number}
    "H.R.": "bills", # /bills/{congress}/{billtype}/{bill_number}
    "Pub. L.": "plaw", # /plaw/{congress}/{lawtype}/{lawnum} or /plaw/{statutecitation} or .../{associatedbillnum}
    "Pub. L. No.": "plaw", # /plaw/{congress}/{lawtype}/{lawnum} or .../{statutecitation} or .../{associatedbillnum}
    "Fed. Reg.": "fr", # /fr/{volume}/{page}
}

FEDERAL_PATTERNS = [
    re.compile(r"\bU\.?\s*S\.?\s*C\.?\b", re.I),        # 35 U.S.C. § 101
    re.compile(r"\bC\.?\s*F\.?\s*R\.?\b", re.I),        # 37 C.F.R. § 1.775
    re.compile(r"\bU\.?\s*S\.?\s*Const\.?\b", re.I),    # U.S. Const. art. I
    re.compile(r"\bPub\.?\s*L\.?\b", re.I),             # Pub. L. No. 107-155
    re.compile(r"\bPublic\s+Law\b", re.I),
    re.compile(r"^\d+\s+Stat\.\s+\d+$", re.I),        # 116 Stat. 81  (avoid e.g., state's Stat. Ann.)
    re.compile(r"\bStat\.?\b", re.I),              # 28 Stat. 509
    re.compile(r"\bFed\.?\s*Reg\.?\b", re.I),           # 88 Fed. Reg. 12345
]

# Common state markers seen in Bluebook-style code cites
STATE_MARKERS = [
    # Full names
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut","delaware",
    "florida","georgia","hawaii","idaho","illinois","indiana","iowa","kansas","kentucky","louisiana",
    "maine","maryland","massachusetts","michigan","minnesota","mississippi","missouri","montana",
    "nebraska","nevada","new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island","south carolina",
    "south dakota","tennessee","texas","utah","vermont","virginia","washington","west virginia",
    "wisconsin","wyoming",
    # Common Bluebook abbreviations in codes/regs
    r"ala\.", "alaska", r"ariz\.", r"ark\.", r"cal\.", r"colo\.", r"conn\.", r"del\.", r"fla\.", r"ga\.", r"haw\.", # type: ignore
    "idaho", r"ill\.", r"ind\.", "iowa", r"kan\.", r"ky\.", r"la\.", r"me\.", r"md\.", r"mass\.", r"mich\.", r"minn\.", # type: ignore  # noqa: W605
    r"miss\.", r"mo\.", r"mont\.", r"neb\.", r"nev\.", r"n\. ?h\.", r"n\. ?j\.", r"n\. ?m\.", r"n\. ?y\.", r"n\. ?c\.", # type: ignore
    r"n\. ?d\.", "ohio", r"okla\.", r"or\.", r"pa\.", r"r\. ?i\.", r"s\. ?c\.", r"s\. ?d\.", r"tenn\.", r"tex\.", "utah", # type: ignore
    r"vt\.", r"va\.", r"wash\.", r"w\. ?va\.", r"wis\.", r"wyo\.", # type: ignore
    # Generic state code words that often appear with a state marker
    r"rev\.? ?stat\.?", r"gen\.? ?stat\.?", r"ann\.?", "code", r"comp\.? ?laws", r"stat\.? ann\.?" # type: ignore
]
STATE_REGEX = re.compile(r"\b(" + "|".join(STATE_MARKERS) + r")\b", re.I)

_PUB_LAW_RE = re.compile(
    r"pub\.?\s*l\.?\s*(?:no\.?\s*)?(?P<congress>\d+)[-–](?P<lawnum>\d+)",
    re.IGNORECASE,
)

_CFR_PART_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)(?:\.(\d+))?$")

# Multi-section range support (e.g. "35 U.S.C. §§ 101-103", "29 C.F.R. §§ 1910.1-1910.10").
# GovInfo's link service only resolves a single section per request, so a range that 400s
# on the literal lookup is expanded into one request per section, capped to keep worst-case
# fan-out bounded.
MAX_RANGE_EXPANSION: Final[int] = 10
_USC_RANGE_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)-(\d+)$")
_CFR_PART_SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+)\.(\d+)$")
_PURE_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"^\d+$")

# A GovInfo C.F.R. section lookup ("cfr/37/1?sectionnum=47[&year=2010]") -> part, section.
_CFR_SECTION_ENDPOINT_RE: Final[re.Pattern[str]] = re.compile(r"^cfr/\d+/(\d+)\?sectionnum=([^&]+)")
# The package a GovInfo link resolved to (".../content/pkg/CFR-2025-title37-vol1/pdf/...").
_CFR_PACKAGE_RE: Final[re.Pattern[str]] = re.compile(r"/CFR-(\d{4})-title(\d+)-vol(\d+)/")

# Official sources for material newer than GovInfo's latest published edition/volume.
GOVINFO_API_URL = "https://api.govinfo.gov/packages/{package_id}/summary"
ECFR_VERSIONS_URL = "https://www.ecfr.gov/api/versioner/v1/versions/title-{title}.json"
OLRC_SECTION_URL = "https://uscode.house.gov/view.xhtml"
_USER_AGENT: Final[str] = "JurisCheck citation verifier (+https://www.jurischeck.com)"
_ECFR_FIRST_YEAR: Final[int] = 2017  # eCFR's point-in-time history starts 2017-01-01
_ECFR_SECTION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^\d+\.[0-9A-Za-z-]+$")
_HTML_TITLE_RE: Final[re.Pattern[str]] = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)

def classify_law_jurisdiction(
    text: str,
) -> Literal["federal", "state", "unknown"]:
    """
    Heuristic classifier for a law citation's text:
      - 'federal' if it matches federal code/reg/constitution/statutes-at-large markers
      - 'state' if it contains a state name/abbreviation + code/reg words
      - 'unknown' if neither is detected

    Returns: 'federal' | 'state' | 'unknown'
    """

    # Federal first: strong signals
    for pat in FEDERAL_PATTERNS:
        if pat.search(text) or len(pat.findall(text)) > 0:
            return "federal"

    # State signals: look for a state marker anywhere
    if STATE_REGEX.search(text) or len(STATE_REGEX.findall(text)) > 0:
        return "state"

    # If code is present without explicit markers, try minimal structural hints
    # e.g., "§ 101" alone cannot be classified
    return "unknown"

def _clean_value(value: Any) -> str | None:
    return clean_str(value)


def _get_law_group(
    cite: CitationRecord | None,
    resource_dict: Dict[str, Any] | None,
    key: str,
) -> str | None:
    if cite is not None:
        value = _clean_value(cite.get(key))
        if value:
            return value

    resource_dict = resource_dict or {}
    id_tuple = resource_dict.get("id_tuple")
    if isinstance(id_tuple, tuple):
        mapping = {
            "title": 0,
            "volume": 0,
            "chapter": 0,
            "code": 1,
            "reporter": 1,
            "section": 2,
            "page": 2,
            "year": 3,
        }
        idx = mapping.get(key)
        if idx is not None and len(id_tuple) > idx:
            value = _clean_value(id_tuple[idx])
            if value:
                return value

    return None


def _sanitize_section(text: str | None) -> str | None:
    if not text:
        return None
    sanitized = text.replace("§", "")
    sanitized = clean_str(sanitized)
    return sanitized or None


def _extract_cfr_part(section: str | None) -> Dict[str, str | None] | None:
    if not section:
        return None

    match = _CFR_PART_RE.match(section)
    if not match:
        return None
    part = match.group(1)
    section_num = match.group(2) if match.group(2) else None
    return {"part": part, "section_num": section_num}


def _extract_cfr_range_literal(section: str) -> Dict[str, str | None] | None:
    """Build a literal (unexpanded) sectionnum query for a hyphenated C.F.R. section.

    Tried before any range expansion so genuinely hyphenated sections (e.g. an EPA
    section like "86.1803-01") get a real GovInfo lookup instead of being assumed
    to be a range.
    """
    if section.count("-") != 1:
        return None

    left, right = section.split("-", 1)
    left_match = _CFR_PART_SECTION_RE.match(left)
    if left_match:
        part, section_num = left_match.group(1), left_match.group(2)
        return {"part": part, "section_num": f"{section_num}-{right}"}

    if _PURE_DIGITS_RE.match(left) and _PURE_DIGITS_RE.match(right):
        return {"part": section, "section_num": None}

    return None


def _build_uscode_endpoint(
    cite: CitationRecord,
    resource_dict: Dict[str, Any] | None,
) -> Tuple[str | None, Dict[str, str] | None, Tuple[str, Dict[str, Any]] | None]:
    title = _get_law_group(cite, resource_dict, "title")
    section = _sanitize_section(_get_law_group(cite, resource_dict, "section"))

    if not title or not section:
        return (
            None,
            None,
            (
                "insufficient_citation_data",
                {"required_fields": ["title", "section"], "source": "govinfo"},
            ),
        )

    endpoint = f"{GOVINFO_REPORTER_MAP['U.S.C.']}/{title}/{section}"
    return endpoint, None, None


def _build_cfr_endpoint(
    cite: CitationRecord,
    resource_dict: Dict[str, Any] | None,
) -> Tuple[str | None, Dict[str, str] | None, Tuple[str, Dict[str, Any]] | None]:
    title = (_get_law_group(cite, resource_dict, "title") or _get_law_group(cite, resource_dict, "volume")
             or _get_law_group(cite, resource_dict, "chapter"))
    section = _sanitize_section(_get_law_group(cite, resource_dict, "section")
                                or _get_law_group(cite, resource_dict, "page"))

    part = None
    section_num = None
    if section is not None:
        cfr_dict = _extract_cfr_part(section) or _extract_cfr_range_literal(section)
        if cfr_dict is not None:
            part = cfr_dict.get("part", None)
            section_num = cfr_dict.get("section_num", None)

    if not title or not section or not part:
        return (
            None,
            None,
            (
                "insufficient_citation_data",
                {
                    "required_fields": ["title", "section"],
                    "source": "govinfo",
                },
            ),
        )

    endpoint = f"{GOVINFO_REPORTER_MAP['C.F.R.']}/{title}/{part}"
    if section_num is not None:
        endpoint += f"?sectionnum={section_num}"

    return endpoint, None, None


def _build_stat_endpoint(
    cite: CitationRecord,
    resource_dict: Dict[str, Any] | None,
) -> Tuple[str | None, Dict[str, str] | None, Tuple[str, Dict[str, Any]] | None]:
    volume = _get_law_group(cite, resource_dict, "volume") or _get_law_group(cite, resource_dict, "title")
    page = _sanitize_section(_get_law_group(cite, resource_dict, "page") or _get_law_group(cite, resource_dict, "section"))

    if not volume or not page:
        return (
            None,
            None,
            (
                "insufficient_citation_data",
                {
                    "required_fields": ["volume", "page"],
                    "source": "govinfo",
                },
            ),
        )

    endpoint = f"{GOVINFO_REPORTER_MAP['Stat.']}/{volume}/{page}"
    return endpoint, None, None


def _build_fr_endpoint(
    cite: CitationRecord,
    resource_dict: Dict[str, Any] | None,
) -> Tuple[str | None, Dict[str, str] | None, Tuple[str, Dict[str, Any]] | None]:
    volume = _get_law_group(cite, resource_dict, "volume") or _get_law_group(cite, resource_dict, "title")
    page = _sanitize_section(_get_law_group(cite, resource_dict, "page") or _get_law_group(cite, resource_dict, "section"))

    if not volume or not page:
        return (
            None,
            None,
            (
                "insufficient_citation_data",
                {
                    "required_fields": ["volume", "page"],
                    "source": "govinfo",
                },
            ),
        )

    endpoint = f"{GOVINFO_REPORTER_MAP['Fed. Reg.']}/{volume}/{page}"
    return endpoint, None, None


def _build_plaw_endpoint(
    cite: CitationRecord,
    resource_dict: Dict[str, Any] | None,
    citation_text: str | None,
) -> Tuple[str | None, Dict[str, str] | None, Tuple[str, Dict[str, Any]] | None]:
    congress = _get_law_group(cite, resource_dict, "congress")
    lawnum = _get_law_group(cite, resource_dict, "lawnum")

    if (not congress or not lawnum) and citation_text:
        match = _PUB_LAW_RE.search(citation_text)
        if match:
            congress = congress or match.group("congress")
            lawnum = lawnum or match.group("lawnum")

    if not congress or not lawnum:
        return (
            None,
            None,
            (
                "insufficient_citation_data",
                {
                    "required_fields": ["congress", "lawnum"],
                    "source": "govinfo",
                },
            ),
        )

    if congress.isdigit():
        congress_num = int(congress)
        if congress_num < 104:
            return (
                "error",
                None,
                (
                    "citation predates earliest available data",
                    {"congress": congress, "source": "govinfo"},
                ),
            )

    endpoint = f"{GOVINFO_REPORTER_MAP['Pub. L.']}/{congress}/public/{lawnum}"
    return endpoint, None, None


_REPORTER_BUILDERS = {
    "U.S.C.": lambda cite, resource, citation_text=None: _build_uscode_endpoint(cite, resource),
    "C.F.R.": lambda cite, resource, citation_text=None: _build_cfr_endpoint(cite, resource),
    "Stat.": lambda cite, resource, citation_text=None: _build_stat_endpoint(cite, resource),
    "Fed. Reg.": lambda cite, resource, citation_text=None: _build_fr_endpoint(cite, resource),
    "Pub. L.": lambda cite, resource, citation_text=None: _build_plaw_endpoint(cite, resource, citation_text),
    "Pub. L. No.": lambda cite, resource, citation_text=None: _build_plaw_endpoint(cite, resource, citation_text),
}


def _build_govinfo_request(
    cite: CitationRecord,
    resource_dict: Dict[str, Any] | None,
    citation_text: str | None,
) -> Tuple[str | None, Dict[str, str] | None, Tuple[str, Dict[str, Any]] | None]:
    reporter = _get_law_group(cite, resource_dict, "reporter")
    if not reporter:
        return (
            "error",
            None,
            (
                "missing_reporter",
                {"source": "govinfo"},
            ),
        )

    reporter = reporter.strip()
    if (
        reporter not in GOVINFO_REPORTER_MAP
        or reporter not in _REPORTER_BUILDERS
    ):
        return (
            "error",
            None,
            (
                "unsupported_reporter",
                {"reporter": reporter, "source": "govinfo"},
            ),
        )

    builder = _REPORTER_BUILDERS[reporter]
    endpoint, params, error = builder(cite, resource_dict, citation_text)
    if error:
        return None, None, error

    return endpoint, params, None


def _splice_short_form(start_str: str, end_str: str) -> str:
    """Splice a Bluebook short-form range end onto the start's prefix.

    E.g. ("101", "03") -> "103"; ("1910", "26") -> "1926". Leaves end_str
    unchanged when it isn't shorter than start_str.
    """
    if len(end_str) < len(start_str):
        return start_str[: len(start_str) - len(end_str)] + end_str
    return end_str


def _expand_bluebook_range(start_str: str, end_str: str) -> Tuple[List[str], int] | None:
    """Expand a numeric range, handling Bluebook short forms (e.g. "101-03").

    Returns (labels, total_count). When total_count exceeds MAX_RANGE_EXPANSION,
    labels contains only the start and end values so the interior is never
    materialized for pathological ranges (e.g. "1-9999"). Returns None when the
    range isn't purely numeric or doesn't strictly increase.
    """
    end_str = _splice_short_form(start_str, end_str)
    if not (start_str.isdigit() and end_str.isdigit()):
        return None

    start, end = int(start_str), int(end_str)
    if end <= start:
        return None

    total_count = end - start + 1
    if total_count > MAX_RANGE_EXPANSION:
        return [str(start), str(end)], total_count

    return [str(n) for n in range(start, end + 1)], total_count


def _plan_uscode_range(
    cite: CitationRecord,
    resource_dict: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    title = _get_law_group(cite, resource_dict, "title")
    section = _sanitize_section(_get_law_group(cite, resource_dict, "section"))
    if not title or not section:
        return None

    match = _USC_RANGE_RE.match(section)
    if not match:
        return None

    expanded = _expand_bluebook_range(match.group(1), match.group(2))
    if not expanded:
        return None
    labels, total_count = expanded

    requests = [(f"{GOVINFO_REPORTER_MAP['U.S.C.']}/{title}/{label}", None) for label in labels]

    return {
        "range": section,
        "labels": labels,
        "requests": requests,
        "total_count": total_count,
        "capped": total_count > MAX_RANGE_EXPANSION,
        "part_range": False,
    }


def _plan_cfr_range(
    cite: CitationRecord,
    resource_dict: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    title = (_get_law_group(cite, resource_dict, "title") or _get_law_group(cite, resource_dict, "volume")
             or _get_law_group(cite, resource_dict, "chapter"))
    section = _sanitize_section(_get_law_group(cite, resource_dict, "section")
                                or _get_law_group(cite, resource_dict, "page"))
    if not title or not section or section.count("-") != 1:
        return None

    left, right = section.split("-", 1)
    left_match = _CFR_PART_SECTION_RE.match(left)
    right_match = _CFR_PART_SECTION_RE.match(right)
    cfr_reporter = GOVINFO_REPORTER_MAP["C.F.R."]

    if left_match and right_match:
        left_part, left_num = left_match.group(1), left_match.group(2)
        right_part, right_num = right_match.group(1), right_match.group(2)

        if left_part == right_part:
            # Same-part section-number range, e.g. "1910.1-1910.10".
            expanded = _expand_bluebook_range(left_num, right_num)
            if not expanded:
                return None
            labels, total_count = expanded
            requests = [(f"{cfr_reporter}/{title}/{left_part}?sectionnum={label}", None) for label in labels]
            return {
                "range": section,
                "labels": labels,
                "requests": requests,
                "total_count": total_count,
                "capped": total_count > MAX_RANGE_EXPANSION,
                "part_range": False,
            }

        # Cross-part range, e.g. "1910.5-1926.1": interior sections are unknowable,
        # so only the two boundary sections are checked.
        labels = [left, right]
        requests = [
            (f"{cfr_reporter}/{title}/{left_part}?sectionnum={left_num}", None),
            (f"{cfr_reporter}/{title}/{right_part}?sectionnum={right_num}", None),
        ]
        return {
            "range": section,
            "labels": labels,
            "requests": requests,
            "total_count": 2,
            "capped": False,
            "part_range": True,
        }

    if left_match and _PURE_DIGITS_RE.match(right):
        # Dotted-left, bare-digit-right short form, e.g. "1910.1-5".
        part, left_num = left_match.group(1), left_match.group(2)
        expanded = _expand_bluebook_range(left_num, right)
        if not expanded:
            return None
        labels, total_count = expanded
        requests = [(f"{cfr_reporter}/{title}/{part}?sectionnum={label}", None) for label in labels]
        return {
            "range": section,
            "labels": labels,
            "requests": requests,
            "total_count": total_count,
            "capped": total_count > MAX_RANGE_EXPANSION,
            "part_range": False,
        }

    if _PURE_DIGITS_RE.match(left) and _PURE_DIGITS_RE.match(right):
        # Bare part range, e.g. "1910-1926". C.F.R. part numbering is sparse by
        # design, so only the two boundary parts are checked, never the interior.
        expanded = _expand_bluebook_range(left, right)
        if not expanded:
            return None
        _, total_count = expanded
        right_full = _splice_short_form(left, right)
        labels = [left, right_full]
        requests = [
            (f"{cfr_reporter}/{title}/{left}", None),
            (f"{cfr_reporter}/{title}/{right_full}", None),
        ]
        return {
            "range": section,
            "labels": labels,
            "requests": requests,
            "total_count": total_count,
            "capped": False,
            "part_range": True,
        }

    return None


def _plan_range_requests(
    cite: CitationRecord,
    resource_dict: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    """Plan per-section GovInfo requests for a multi-section range citation.

    Only meaningful after the literal section lookup has already returned a
    GovInfo 400 for the citation's un-split section string. Returns None when
    the reporter isn't range-aware (Stat./Pub. L./Fed. Reg.) or the section
    isn't a valid range.
    """
    reporter = _get_law_group(cite, resource_dict, "reporter")
    if not reporter:
        return None
    reporter = reporter.strip()

    if reporter == "U.S.C.":
        return _plan_uscode_range(cite, resource_dict)
    if reporter == "C.F.R.":
        return _plan_cfr_range(cite, resource_dict)
    return None


def _execute_govinfo_lookup(
    endpoint: str,
    params: Dict[str, str] | None,
    api_key: bytes,
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    url = f"{GOVINFO_BASE_URL}{endpoint}"
    logger.info(f"federal_law_verifier.verify_federal_law_citation: Built GovInfo URL: {url}")

    try:
        response = httpx2.get(
            url,
            params=params,
            auth=httpx2.BasicAuth(api_key, ""),
            headers={"accept": "*/*"},
            follow_redirects=True,
            timeout=GOVINFO_TIMEOUT,
        )
    except httpx2.HTTPError as exc:
        logger.error("GovInfo lookup failed for %s: %s", url, exc)
        return "error", "lookup_failed", {
            "source": "govinfo",
            "endpoint": endpoint,
            "params": params,
        }
    except Exception as exc:  # pragma: no cover - unexpected failure
        logger.error("Unexpected error during GovInfo lookup for %s: %s", url, exc)
        return "error", "lookup_error", {
            "source": "govinfo",
            "endpoint": endpoint,
            "params": params,
        }

    details: Dict[str, Any] = {
        "source": "govinfo",
        "endpoint": endpoint,
        "params": params,
        "status_code": response.status_code,
    }

    if response.status_code == 400:
        logger.error(f"GovInfo lookup failed for {url}: 400")
        return "no_match", "Not found in GovInfo", details
    elif response.status_code == 401:
        logger.error(f"GovInfo lookup failed for {url}: 401")
        return "error", "lookup_auth_failed", details
    elif response.status_code == 403:
        logger.error(f"GovInfo lookup failed for {url}: 403")
        return "error", "lookup_forbidden", details
    elif response.status_code == 429:
        logger.error(f"GovInfo lookup failed for {url}: 429")
        return "error", "lookup_rate_limited", details
    elif response.status_code >= 500:
        logger.error(f"GovInfo lookup failed for {url}: {response.status_code}")
        return "error", "lookup_service_error", details
    elif response.status_code != 200:
        return "no_match", None, details

    content_type = (response.headers.get("content-type") or "").lower()
    body = response.content or b""

    if content_type.find("pdf") == -1 or not body.startswith(b"%PDF"):
        logger.error(f"GovInfo lookup failed for {url}: invalid content type")
        details["content_type"] = response.headers.get("content-type")
        return "no_match", "Not found in GovInfo", details

    if not body:
        logger.error(f"GovInfo lookup failed for {url}: empty content")
        details["content_length"] = 0
        return "no_match", "Not found in GovInfo", details

    cfr_section = _CFR_SECTION_ENDPOINT_RE.match(endpoint)
    if cfr_section and _cfr_pdf_reserved(body, f"{cfr_section.group(1)}.{cfr_section.group(2)}"):
        logger.info(f"GovInfo shows a reserved C.F.R. section for {url}")
        details["reserved"] = True
        return "no_match", "Section reserved in the C.F.R.", details

    # Without a year, the link service serves the latest edition that still has the
    # section (e.g. the 1997 edition for 37 C.F.R. § 1.107, reserved since): a newer
    # edition of that volume means the section was removed or reserved after it.
    served = _CFR_PACKAGE_RE.search(str(response.url)) if cfr_section and "year=" not in endpoint else None
    if served and _cfr_edition_superseded(int(served.group(1)), served.group(2), served.group(3)):
        logger.info(f"GovInfo's latest edition with {url} is {served.group(1)}")
        details["last_edition_year"] = int(served.group(1))
        return "no_match", "Not in the current C.F.R. edition", details

    return "verified", None, None


def _cfr_edition_superseded(year: int, title: str, volume: str) -> bool:
    """Whether GovInfo has published an edition of this C.F.R. volume newer than `year`.

    Only asked for an edition older than last year: C.F.R. titles are revised annually,
    so a recent edition is taken as current without an extra request.
    """
    if year >= date.today().year - 1:
        return False
    return _govinfo_package_published(f"CFR-{year + 1}-title{title}-vol{volume}") is True


def _cfr_pdf_reserved(body: bytes, section_id: str) -> bool:
    """Whether GovInfo's PDF of a C.F.R. section shows that section as "[Reserved]".

    Matched on the section's own entry ("§ 1.47 [Reserved]", or a reserved range
    "§§ 1.106–1.108 [Reserved]" covering it): the page also carries a running header
    ("§ 1.47") and neighboring sections. A PDF that can't be read counts as not reserved.
    """
    try:
        import pymupdf

        with pymupdf.open(stream=body, filetype="pdf") as document:
            text = " ".join(page.get_text() for page in document)
    except Exception:
        return False

    if re.search(rf"§\s*{re.escape(section_id)}\s*\[Reserved\]", text):
        return True
    part, _, number = section_id.partition(".")
    if not number.isdigit():
        return False
    reserved_range = re.compile(
        rf"§§\s*{re.escape(part)}\.(\d+)\s*[-–]\s*(?:{re.escape(part)}\.)?(\d+)\s*\[Reserved\]"
    )
    return any(int(m.group(1)) <= int(number) <= int(m.group(2)) for m in reserved_range.finditer(text))


def _verify_section_range(
    plan: Dict[str, Any],
    api_key: bytes,
    literal_result: Tuple[str, str | None, Dict[str, Any] | None],
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Verify each section/part in a range plan and aggregate the results.

    Any error-class result (auth/rate-limit/5xx/network) aborts expansion and is
    returned immediately, unmodified. Otherwise: all verified -> verified; some
    found -> warning naming the missing sections/parts; none found -> the
    original literal 400 result, unchanged.
    """
    is_part_range = bool(plan.get("part_range"))
    unit = "Parts" if is_part_range else "Sections"

    logger.info(
        "Expanding GovInfo range lookup for %s into %d request(s)",
        plan["range"],
        len(plan["requests"]),
    )

    sections_verified: List[str] = []
    sections_not_found: List[str] = []

    for label, (endpoint, params) in zip(plan["labels"], plan["requests"]):
        status, substatus, details = _execute_govinfo_lookup(endpoint, params, api_key)
        if status == "verified":
            sections_verified.append(label)
        elif status in ("no_match", "no match"):
            sections_not_found.append(label)
        else:
            return status, substatus, details

    result_details: Dict[str, Any] = {
        "source": "govinfo",
        "range": plan["range"],
        "sections_verified": sections_verified,
        "sections_not_found": sections_not_found,
    }
    if plan.get("capped"):
        result_details["range_capped"] = True
        result_details["sections_in_range"] = plan["total_count"]
        result_details["sections_unchecked_count"] = plan["total_count"] - len(plan["labels"])
    if is_part_range:
        result_details["part_range"] = True

    if not sections_not_found:
        return "verified", None, result_details

    if sections_verified:
        return "warning", f"{unit} not found: {', '.join(sections_not_found)}", result_details

    return literal_result


# AI web-search recheck of a citation GovInfo can't confirm (e.g. "35 U.S.C. § 154(a)(1)-(2)",
# whose subsections the link service rejects with a 400), modeled on state_law_verifier.
AI_FALLBACK_PROMPT = """
Below is a citation to a U.S. federal legal authority: a United States Code (U.S.C.) section, a Code of Federal
Regulations (C.F.R.) section or part, a Statutes at Large (Stat.) page, a Federal Register (Fed. Reg.) page, a public
law (Pub. L.), the U.S. Constitution (U.S. Const.), or a similar federal provision.
You must verify the existence and accuracy of the citation. Check each part of it: the title or volume number, the
code or reporter, the section (including every subsection, paragraph, or clause the citation names, e.g. "(a)(1)-(2)")
or page, and the year.
A year in parentheses names the edition or year of the code or publication cited: a provision that existed in that
edition is valid even if it has since been amended, renumbered, or repealed. If you cannot confirm that the provision
existed in the cited year, adjust your confidence score downward.
Your primary goal is to verify whether the citation you are provided corresponds to an actual federal law citation.
You should use only the information explicitly provided to you when generating a response.
Because accuracy is the paramount concern, responses indicating you don't have sufficient information to provide an answer or you are unable to locate a source
corresponding to a citation are acceptable. Moreover, you should provide a confidence score between 0.0 and
1.0 indicating confidence for a citation verification.
If you are unable to verify all parts of a citation, you should adjust your confidence score downward to reflect this uncertainty.
Provide your response as a JSON object, according to this format:
    {
        "status": "verified" if citation is verified (confidence score >= 0.90), "warning" if confidence is low (0.70 <= confidence < 0.90),
            "no_match" if no matching citation is found (confidence score < 0.70), or "error" if an error occurred,
        "citation": the standardized Bluebook citation string closest to the provided citation (if "verified" this may be the same as the provided
            citation; if "no_match" or "error" this should be null),
        "confidence": confidence score as a float between 0.0 and 1.0, indicating how confident you are that the citation is valid
    }
Do not return any text or other characters apart from the JSON object. Do not include any text or other characters outside of the JSON object.\n\n
"""

AI_FALLBACK_DOMAINS: Final[List[str]] = [
    "law.justia.com",
    "law.cornell.edu",
    "codes.findlaw.com",
    "govregs.com",
]

AI_FALLBACK_TIMEOUT: Final[float] = 120.0

# Pre-request GovInfo errors meaning the link service can't look this citation up
# (as opposed to a configuration fault or a service outage, which are not rechecked).
_AI_RECHECK_ERROR_SUBSTATUSES: Final[frozenset[str]] = frozenset({
    "unsupported_reporter",
    "missing_reporter",
    "insufficient_citation_data",
    "invalid_endpoint",
    "citation predates earliest available data",
})

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def _needs_ai_recheck(result: Tuple[str, str | None, Dict[str, Any] | None]) -> bool:
    status, substatus, _ = result
    if status in ("no_match", "no match"):
        return True
    return status == "error" and substatus in _AI_RECHECK_ERROR_SUBSTATUSES


def _ai_citation_text(
    record: CitationRecord,
    normalized_key: str | None,
    fallback_citation: str | None,
) -> str | None:
    """The citation as written (plus its year when the text lacks it), for the AI recheck."""
    text = None
    for candidate in (record.matched_text, normalized_key, fallback_citation):
        cleaned = _WHITESPACE_RE.sub(" ", candidate or "").strip()
        if cleaned:
            text = cleaned
            break
    if not text:
        return None

    year = _clean_value(record.get("year"))
    if year and year not in text:
        text += f" ({year})"
    return text


def _last_message_text(response: Any) -> str | None:
    text = None
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        parts = [
            content.text
            for content in (getattr(item, "content", None) or [])
            if getattr(content, "type", None) == "output_text" and getattr(content, "text", None)
        ]
        if parts:
            text = "".join(parts)
    return text


def _blank_to_none(value: Any) -> Any:
    if value is None or (isinstance(value, str) and value.strip().lower() in {"null", "none", ""}):
        return None
    return value


def _recheck_with_ai(
    citation_text: str,
    govinfo_result: Tuple[str, str | None, Dict[str, Any] | None],
    base_confirmed: Tuple[str, str] | None = None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Recheck a citation GovInfo couldn't confirm with AI_MODEL + web search.

    base_confirmed is (section, source) for the section an official source did find
    (the citation minus its subdivisions), so the model is asked about the
    subdivisions only. The returned status is the more conservative of the model's
    and its confidence band (state_law_verifier.confidence_band_status).

    Any failure (no usable AI_MODEL, request error or timeout, unparseable or
    unexpected answer) returns govinfo_result unchanged.
    """
    context = ""
    if base_confirmed:
        context = (
            f"{base_confirmed[1]} confirms that {base_confirmed[0]} exists. Verify only whether the subdivisions the "
            "citation names (e.g. \"(a)(1)-(2)\") exist in that section; if you cannot find them, answer \"no_match\".\n"
        )
    try:
        model = ai_model()
        client = state_law_verifier._get_ai_client(model)
        response = client.responses.create(
            model=model.get("model"),
            input=AI_FALLBACK_PROMPT + context + f"**Citation to verify**: `{citation_text}`",
            tools=[{
                "type": "web_search",
                "filters": {"allowed_domains": AI_FALLBACK_DOMAINS},
            }],
            tool_choice="auto",
            text={"verbosity": "low"},
            reasoning={"effort": "medium"},
            store=False,
            timeout=AI_FALLBACK_TIMEOUT,
        )
        data = json.loads(state_law_verifier._clean_json_response(_last_message_text(response) or ""))
    except Exception as exc:
        logger.error("AI recheck of a federal law citation failed: %s", exc)
        return govinfo_result

    raw_status = data.get("status") if isinstance(data, dict) else None
    confidence = _blank_to_none(data.get("confidence")) if isinstance(data, dict) else None
    status = state_law_verifier.confidence_band_status(raw_status, confidence)
    if status is None:
        logger.error(
            "AI recheck of a federal law citation returned an unusable answer: status=%r, confidence=%r",
            raw_status,
            confidence,
        )
        return govinfo_result

    closest_match = _blank_to_none(data.get("citation"))
    logger.info("AI recheck of a federal law citation: status=%s (model: %s), confidence=%s", status, raw_status, confidence)

    govinfo_status, govinfo_substatus, govinfo_details = govinfo_result
    details: Dict[str, Any] = {
        "source": "ai_web_search",
        "model": model.get("model"),
        "citation_checked": citation_text,
        "closest_match": closest_match,
        "confidence": confidence,
        "govinfo": {
            "status": govinfo_status,
            "substatus": govinfo_substatus,
            "details": govinfo_details,
        },
    }
    model_status = str(raw_status).strip().lower().replace(" ", "_")
    if model_status != status:
        details["model_status"] = model_status
    if base_confirmed:
        details["base_section_confirmed"] = base_confirmed[0]
        details["base_confirmed_by"] = base_confirmed[1]
    if status == "no_match":
        return "no_match", "Not found in GovInfo or by AI web search", details
    return status, f"AI web search closest_match: {closest_match}, confidence: {confidence}", details


# GovInfo's first U.S.C./C.F.R. edition. A year GovInfo has no edition for also answers
# 400, so GovInfo's "not found" is final only for a year it covers (or no year at all).
_GOVINFO_FIRST_EDITION: Final[Dict[str, int]] = {"U.S.C.": 1994, "C.F.R.": 1997}

# The part of a U.S.C./C.F.R. section the link service can look up; the rest
# ("(a)(1)-(2)", "et seq.") only the AI recheck can check.
_SECTION_BASE_RES: Final[Dict[str, re.Pattern[str]]] = {
    "U.S.C.": re.compile(r"\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?"),
    "C.F.R.": re.compile(r"\d+(?:\.\d+[A-Za-z]*)?(?:-\d+(?:\.\d+)?[A-Za-z]*)?"),
}

_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}$")


def _split_section(reporter: str, section: str | None) -> Tuple[str | None, str]:
    """(base, rest): "154(a)(1)-(2)" -> ("154", "(a)(1)-(2)"); "5.10" -> ("5.10", "")."""
    if not section:
        return None, ""
    match = _SECTION_BASE_RES[reporter].match(section)
    if not match:
        return None, section
    return match.group(0), section[match.end():].strip()


def _lookup_outcome(result: Tuple[str, str | None, Dict[str, Any] | None]) -> str:
    status, substatus, _ = result
    if status in ("verified", "warning"):
        return "found"
    if status in ("no_match", "no match"):
        return "not_found"
    if substatus in _AI_RECHECK_ERROR_SUBSTATUSES:
        return "unservable"
    return "undecided"


def _find_in_editions(
    record: CitationRecord,
    section: str,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None,
    year: int | None,
    current: Tuple[str, str | None, Dict[str, Any] | None] | None = None,
) -> Tuple[str, Tuple[str, str | None, Dict[str, Any] | None], int | None]:
    """Look `section` up in GovInfo's most recent edition (`current`, when that lookup
    already ran), then in `year`'s edition if it isn't found there.

    Returns (outcome, result, edition_year): outcome is "found", "not_found",
    "unservable" (GovInfo can't look it up) or "undecided" (a lookup error).
    """
    probe = CitationRecord(record.type, record.matched_text, {**record.fields, "section": section})
    result = current or _verify_with_govinfo(probe, normalized_key, resource_dict, fallback_citation)
    outcome = _lookup_outcome(result)
    if outcome != "not_found" or year is None:
        return outcome, result, None
    year_result = _verify_with_govinfo(probe, normalized_key, resource_dict, fallback_citation, year=str(year))
    return _lookup_outcome(year_result), year_result, year


def _govinfo_package_published(package_id: str) -> bool | None:
    """Whether GovInfo has published a package ("USCODE-2024-title35", "STATUTE-137"),
    per the GovInfo API: True (200), False (404), or None when the API can't tell."""
    try:
        response = httpx2.get(
            GOVINFO_API_URL.format(package_id=package_id),
            headers={"X-Api-Key": (os.getenv(GOVINFO_API_KEY) or "").strip(), "accept": "application/json"},
            follow_redirects=True,
            timeout=GOVINFO_TIMEOUT,
        )
    except httpx2.HTTPError as exc:
        logger.error("GovInfo API lookup failed for %s: %s", package_id, exc)
        return None
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    logger.error("GovInfo API lookup for %s returned %s", package_id, response.status_code)
    return None


def _ecfr_section_exists(title: str, section_id: str, year: int | None) -> bool | None:
    """Whether eCFR has `title` C.F.R. § `section_id` in force (not removed, not
    "[Reserved]"): now when `year` is None, else at any point during `year` (2017 on,
    eCFR's history). None when eCFR can't tell."""
    try:
        response = httpx2.get(
            ECFR_VERSIONS_URL.format(title=title),
            params={"section": section_id},
            headers={"accept": "application/json", "User-Agent": _USER_AGENT},
            follow_redirects=True,
            timeout=GOVINFO_TIMEOUT,
        )
        payload = response.json() if response.status_code == 200 else None
    except (httpx2.HTTPError, ValueError) as exc:
        logger.error("eCFR lookup failed for %s C.F.R. § %s: %s", title, section_id, exc)
        return None
    versions = payload.get("content_versions") if isinstance(payload, dict) else None
    if not isinstance(versions, list):
        logger.error("eCFR lookup for %s C.F.R. § %s returned %s", title, section_id, response.status_code)
        return None

    versions = sorted(
        (v for v in versions if isinstance(v, dict) and isinstance(v.get("date"), str)),
        key=lambda v: v["date"],
    )
    if not versions:
        return False

    def in_force(version: Dict[str, Any]) -> bool:
        return not version.get("removed") and "[Reserved]" not in str(version.get("name") or "")

    if year is None:
        return in_force(versions[-1])
    start, end = f"{year:04d}-01-01", f"{year:04d}-12-31"
    in_year = [v for v in versions if v["date"] <= start][-1:] + [v for v in versions if start < v["date"] <= end]
    return any(in_force(v) for v in in_year)


def _olrc_section_exists(title: str, section: str) -> bool | None:
    """Whether the current U.S. Code of the Office of the Law Revision Counsel has
    `title` U.S.C. § `section`: its page title reads "35 USC 154: ..." or "Document
    not Found". None when the page can't tell."""
    try:
        response = httpx2.get(
            OLRC_SECTION_URL,
            params={"req": f"granuleid:USC-prelim-title{title}-section{section}", "num": "0", "edition": "prelim"},
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
            timeout=GOVINFO_TIMEOUT,
        )
    except httpx2.HTTPError as exc:
        logger.error("OLRC lookup failed for %s U.S.C. § %s: %s", title, section, exc)
        return None
    match = _HTML_TITLE_RE.search(response.text) if response.status_code == 200 else None
    page_title = _WHITESPACE_RE.sub(" ", match.group(1)).strip() if match else ""
    if page_title.lower() == "document not found":
        return False
    if re.match(rf"{re.escape(title)}\s+USC\s+{re.escape(section)}\s*:", page_title, re.IGNORECASE):
        return True
    logger.error("Unexpected OLRC page for %s U.S.C. § %s (HTTP %s)", title, section, response.status_code)
    return None


def _current_source_check(
    reporter: str,
    title: str | None,
    base: str,
    year: int | None,
    first_edition: int,
) -> Tuple[bool | None, str] | None:
    """Look up a section GovInfo doesn't have in the current official source, for
    material newer than GovInfo's latest edition: eCFR (C.F.R., no year or 2017 on) or
    the OLRC U.S. Code (no year, or a year whose GovInfo edition isn't published yet).

    Returns (found, source), found None when the source can't tell, or None when no
    source applies (GovInfo's edition for the cited year decides, or the year predates
    what the source covers).
    """
    if not title or not title.isdigit():
        return None
    if reporter == "C.F.R.":
        if not _ECFR_SECTION_ID_RE.match(base) or (year is not None and year < _ECFR_FIRST_YEAR):
            return None
        return _ecfr_section_exists(title, base, year), "ecfr"
    if year is not None:
        if year < first_edition:
            return None
        published = _govinfo_package_published(f"USCODE-{year}-title{title}")
        if published is None:
            return None, "uscode.house.gov"
        if published:
            return None
    return _olrc_section_exists(title, base), "uscode.house.gov"


_SOURCE_LABELS: Final[Dict[str, str]] = {"ecfr": "eCFR", "uscode.house.gov": "uscode.house.gov"}


def _check_with_govinfo_editions(
    record: CitationRecord,
    result: Tuple[str, str | None, Dict[str, Any] | None],
    reporter: str,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None,
) -> Tuple[Tuple[str, str | None, Dict[str, Any] | None] | None, Tuple[str, str] | None]:
    """Decide a U.S.C./C.F.R. citation GovInfo didn't find in its most recent edition.

    GovInfo decides the section it can look up, in the most recent or the cited year's
    edition; the current official source (eCFR / uscode.house.gov) decides material
    newer than GovInfo's latest edition; the AI recheck is left only the subdivisions of
    a section one of them found, years before their coverage, and sections they can't
    look up. Returns (final_result, None) when a source decides, else
    (None, base_confirmed), base_confirmed = (section, source) for the section found, if any.
    """
    section = _get_law_group(record, resource_dict, "section")
    if reporter == "C.F.R.":
        section = section or _get_law_group(record, resource_dict, "page")
    base, rest = _split_section(reporter, _sanitize_section(section))
    if base is None:
        return None, None

    title = _get_law_group(record, resource_dict, "title") or _get_law_group(record, resource_dict, "volume")
    first_edition = _GOVINFO_FIRST_EDITION[reporter]
    year_text = _get_law_group(record, resource_dict, "year")
    year = int(year_text) if year_text and _YEAR_RE.match(year_text) else None
    covered_year = year if year is not None and first_edition <= year <= date.today().year else None

    outcome, lookup, edition = _find_in_editions(
        record, base, normalized_key, resource_dict, fallback_citation, covered_year,
        current=None if rest else result,
    )
    if outcome == "undecided":
        return result, None
    if outcome == "unservable":
        return None, None
    if outcome == "found":
        if rest:
            confirmed = f"{title} {reporter} § {base}"
            if edition:
                confirmed += f" (in the {edition} edition)"
            return None, (confirmed, "GovInfo")
        status, substatus, details = lookup
        return (status, substatus, {**(details or {"source": "govinfo"}), "edition_year": edition}), None

    current = _current_source_check(reporter, title, base, year, first_edition)
    if current is not None:
        found, source = current
        if found is None:
            return result, None
        if found:
            if rest:
                return None, (f"{title} {reporter} § {base}", _SOURCE_LABELS[source])
            details = {"source": source}
            if year is not None:
                details["year_checked"] = year
            return ("verified", None, details), None

    if year is not None and year < first_edition:
        return None, None  # before GovInfo's editions: only the AI recheck can tell

    status, substatus, details = lookup
    extra: Dict[str, Any] = {}
    if rest:
        extra["section_checked"] = base
    if edition:
        extra["edition_year_checked"] = edition
    return (status, substatus, {**(details or {}), **extra} if extra else details), None


def verify_federal_law_citation(
    primary_full: CitationRecord | None,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None = None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    if primary_full is None or primary_full.type != "law":
        logger.error("Primary full citation is not a law citation.")
        return "error", "unsupported_citation_type", None

    jurisdiction = primary_full.get("jurisdiction")
    if jurisdiction != "federal":
        logger.error(f"Unsupported jurisdiction: {jurisdiction}")
        return "error", "unsupported_jurisdiction", None

    result = _verify_with_govinfo(primary_full, normalized_key, resource_dict, fallback_citation)
    if not _needs_ai_recheck(result):
        return result

    reporter = (_get_law_group(primary_full, resource_dict, "reporter") or "").strip()
    if result[0] in ("no_match", "no match"):
        if reporter == "Fed. Reg.":
            return result  # GovInfo has every Federal Register volume and resolves pin pages
        if reporter == "Stat.":
            volume = _get_law_group(primary_full, resource_dict, "volume") or _get_law_group(primary_full, resource_dict, "title")
            published = _govinfo_package_published(f"STATUTE-{volume}") if volume and volume.isdigit() else None
            if published is not False:
                return result  # GovInfo has the volume (or can't tell): its answer stands

    base_confirmed = None
    if reporter in _GOVINFO_FIRST_EDITION:
        final, base_confirmed = _check_with_govinfo_editions(
            primary_full, result, reporter, normalized_key, resource_dict, fallback_citation
        )
        if final is not None:
            return final

    citation_text = _ai_citation_text(primary_full, normalized_key, fallback_citation)
    if not citation_text:
        return result
    return _recheck_with_ai(citation_text, result, base_confirmed)


def _with_year(endpoint: str, year: str) -> str:
    """The endpoint for GovInfo's `year` edition (C.F.R. endpoints already carry a query)."""
    return f"{endpoint}{'&' if '?' in endpoint else '?'}year={year}"


def _verify_with_govinfo(
    primary_full: CitationRecord,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None,
    year: str | None = None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    """Look the citation up with GovInfo's link service: the most recent edition, or
    `year`'s edition when given (U.S.C./C.F.R. only)."""
    govinfo_env = os.getenv(GOVINFO_API_KEY) or ""
    api_key = base64.b64encode(govinfo_env.encode("utf-8"))
    if not api_key:
        logger.error("Missing API key for GovInfo.")
        return "error", "missing_api_key", {"source": "govinfo"}

    citation_text = _clean_value(normalized_key) or _clean_value(fallback_citation)

    endpoint, params, error = _build_govinfo_request(primary_full, resource_dict, citation_text)
    if error:
        substatus, details = error
        details = details or {}
        if citation_text:
            details.setdefault("citation", citation_text)
        logger.error(f"Failed to build GovInfo endpoint: {substatus}. Details: {details}")
        return "error", substatus, details

    if not endpoint:
        logger.error(f"Failed to build GovInfo endpoint using {citation_text}.")
        return "error", "invalid_endpoint", {"source": "govinfo"}

    if year:
        endpoint = _with_year(endpoint, year)

    status, substatus, details = _execute_govinfo_lookup(endpoint, params, api_key)

    if status == "no_match" and details is not None and details.get("status_code") == 400:
        plan = _plan_range_requests(primary_full, resource_dict)
        if plan:
            if year:
                plan["requests"] = [(_with_year(e, year), p) for e, p in plan["requests"]]
            return _verify_section_range(plan, api_key, literal_result=(status, substatus, details))

    return status, substatus, details


__all__ = [
    "classify_law_jurisdiction",
    "verify_federal_law_citation",
]
