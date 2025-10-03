# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

from __future__ import annotations

import base64
import os
import re
from typing import Any, Dict, Final, Literal, Tuple

import httpx
from eyecite.models import FullCitation, FullLawCitation

from utils.cleaner import clean_str
from utils.logger import get_logger

logger = get_logger()

GOVINFO_BASE_URL = "https://www.govinfo.gov/link/"
GOVINFO_API_KEY = "GOVINFO_API_KEY"
GOVINFO_TIMEOUT = httpx.Timeout(20.0, connect=10.0, read=10.0)

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

def _text_from_eyecite(cite) -> str:
    """
    Defensive extraction. Works across eyecite versions.
    Tries fields commonly present on Law citations.
    """
    parts = []
    cite_str = None

    val = getattr(cite, "full_cite", None) or getattr(cite.groups, "full_cite", None) or getattr(cite.token, "data", None)
    if val is not None:
        cite_str = _sanitize_section(val)
    else:
        for attr in ("title", "volume", "chapter"):
            part_one = getattr(cite, attr, None) or getattr(cite.groups, attr, None)
            if part_one is not None:
                s = _sanitize_section(str(part_one))
                if s:
                    parts.append(s)
                    break
        for attr in ("code", "reporter"):
            part_two = getattr(cite, attr, None) or getattr(cite.groups, attr, None)
            if part_two is not None:
                s = _sanitize_section(part_two)
                if s:
                    parts.append(s)
                    break
        for attr in ("section", "page"):
            part_three = getattr(cite, attr, None) or getattr(cite.groups, attr, None)
            if part_three is not None:
                s = _sanitize_section(str(part_three))
                if s:
                    parts.append(s)
                    break

        cite_str = " ".join(parts)
    if cite_str is None or cite_str == "":
        cite_str = str(cite)
    return cite_str

def classify_full_law_jurisdiction(
    cite  # eyecite.citations.FullLawCitation
) -> Literal["federal", "state", "unknown"]:
    """
    Heuristic classifier for an eyecite FullLawCitation:
      - 'federal' if it matches federal code/reg/constitution/statutes-at-large markers
      - 'state' if it contains a state name/abbreviation + code/reg words
      - 'unknown' if neither is detected

    Returns: 'federal' | 'state' | 'unknown'
    """
    text = _text_from_eyecite(cite)

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
    cite: FullCitation | None,
    resource_dict: Dict[str, Any] | None,
    key: str,
) -> str | None:
    if isinstance(cite, FullCitation):
        groups = getattr(cite, "groups", {}) or {}
        if key in groups:
            value = _clean_value(groups.get(key))
            if value:
                return value

    if isinstance(cite, FullCitation):
        direct_value = _clean_value(getattr(cite, key, None))
        if direct_value is not None:
            return direct_value

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



def _build_uscode_endpoint(
    cite: FullLawCitation,
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
    cite: FullLawCitation,
    resource_dict: Dict[str, Any] | None,
) -> Tuple[str | None, Dict[str, str] | None, Tuple[str, Dict[str, Any]] | None]:
    title = (_get_law_group(cite, resource_dict, "title") or _get_law_group(cite, resource_dict, "volume")
             or _get_law_group(cite, resource_dict, "chapter"))
    section = _sanitize_section(_get_law_group(cite, resource_dict, "section")
                                or _get_law_group(cite, resource_dict, "page"))

    part = None
    section_num = None
    cfr_dict = _extract_cfr_part(section)
    if section is not None:
        cfr_dict = _extract_cfr_part(section)
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
    cite: FullLawCitation,
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
    cite: FullLawCitation,
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
    cite: FullLawCitation,
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
    cite: FullLawCitation,
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


def verify_federal_law_citation(
    primary_full: FullCitation | None,
    normalized_key: str | None,
    resource_dict: Dict[str, Any] | None,
    fallback_citation: str | None = None,
) -> Tuple[str, str | None, Dict[str, Any] | None]:
    if not isinstance(primary_full, FullLawCitation):
        logger.error("Primary full citation is not a FullLawCitation.")
        return "error", "unsupported_citation_type", None

    jurisdiction = classify_full_law_jurisdiction(primary_full)
    if jurisdiction != "federal":
        logger.error(f"Unsupported jurisdiction: {jurisdiction}")
        return "error", "unsupported_jurisdiction", None

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


    url = f"{GOVINFO_BASE_URL}{endpoint}"
    logger.info(f"federal_law_verifier.verify_federal_law_citation: Built GovInfo URL: {url}")

    try:
        response = httpx.get(
            url,
            params=params,
            auth=httpx.BasicAuth(api_key, ""),
            headers={"accept": "*/*"},
            follow_redirects=True,
            timeout=GOVINFO_TIMEOUT,
        )
    except httpx.HTTPError as exc:
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
        return "no_match", "no_federal_law", details
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
        return "no match", None, details

    content_type = (response.headers.get("content-type") or "").lower()
    body = response.content or b""

    if content_type.find("pdf") == -1 or not body.startswith(b"%PDF"):
        logger.error(f"GovInfo lookup failed for {url}: invalid content type")
        details["content_type"] = response.headers.get("content-type")
        return "no match", None, details

    if not body:
        logger.error(f"GovInfo lookup failed for {url}: empty content")
        details["content_length"] = 0
        return "no match", None, details

    return "verified", None, None


__all__ = [
    "classify_full_law_jurisdiction",
    "verify_federal_law_citation",
]
