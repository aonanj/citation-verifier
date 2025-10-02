from __future__ import annotations

import os
import re
import base64
from typing import Any, Dict, Literal, Tuple

import httpx
from eyecite.models import FullCitation, FullLawCitation
from utils.cleaner import clean_str
from utils.logger import get_logger

logger = get_logger()

GOVINFO_BASE_URL = "https://www.govinfo.gov/link/"
GOVINFO_API_KEY = os.getenv("GOVINFO_API_KEY", "")
GOVINFO_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=5.0)

GOVINFO_REPORTER_MAP = {
    "U.S.C.": "uscode", # /uscode/{title}/{section}
    "C.F.R.": "cfr", # /cfr/{title}/{part}?sectionnum={section}
    "Stat.": "statute", # /statute/{volume}/{page}
    "Weekly Comp. Pres. Doc.": "cpd", # /cpd/{doctype}/{docnum}
    "Daily Comp. Pres. Doc.": "cpd", # /cpd/{doctype}/{docnum}
    "S.": "bills", # /bills/{congress}/{billtype}/{bill_number}
    "H.R.": "bills", # /bills/{congress}/{billtype}/{bill_number}
    "Pub. L.": "plaw", # /plaw/{congress}/{lawtype}/{lawnum} or /plaw/{statutecitation} or /plaw/{congress}/{associatedbillnum}
    "Pub. L. No.": "plaw", # /plaw/{congress}/{lawtype}/{lawnum} or /plaw/{statutecitation} or /plaw/{congress}/{associatedbillnum}
    "Fed. Reg.": "fr", # /fr/{volume}/{page}
}

FEDERAL_PATTERNS = [
    re.compile(r"\bU\.?\s*S\.?\s*C\.?\b", re.I),        # 35 U.S.C. § 101
    re.compile(r"\bC\.?\s*F\.?\s*R\.?\b", re.I),        # 37 C.F.R. § 1.775
    re.compile(r"\bU\.?\s*S\.?\s*Const\.?\b", re.I),    # U.S. Const. art. I
    re.compile(r"\bPub\.?\s*L\.?\b", re.I),             # Pub. L. No. 107-155
    re.compile(r"\bPublic\s+Law\b", re.I),
    re.compile(r"\bStat\.\b(?!\s*Ann\.)", re.I),        # 116 Stat. 81  (avoid e.g., state's Stat. Ann.)
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
    "ala\.", "alaska", "ariz\.", "ark\.", "cal\.", "colo\.", "conn\.", "del\.", "fla\.", "ga\.", "haw\.", # type: ignore
    "idaho", "ill\.", "ind\.", "iowa", "kan\.", "ky\.", "la\.", "me\.", "md\.", "mass\.", "mich\.", "minn\.", # type: ignore
    "miss\.", "mo\.", "mont\.", "neb\.", "nev\.", "n\. ?h\.", "n\. ?j\.", "n\. ?m\.", "n\. ?y\.", "n\. ?c\.", # type: ignore
    "n\. ?d\.", "ohio", "okla\.", "or\.", "pa\.", "r\. ?i\.", "s\. ?c\.", "s\. ?d\.", "tenn\.", "tex\.", "utah", # type: ignore
    "vt\.", "va\.", "wash\.", "w\. ?va\.", "wis\.", "wyo\.", # type: ignore
    # Generic state code words that often appear with a state marker
    "rev\.? ?stat\.?", "gen\.? ?stat\.?", "ann\.?", "code", "comp\.? ?laws", "stat\.? ann\.?" # type: ignore
]
STATE_REGEX = re.compile(r"\b(" + "|".join(STATE_MARKERS) + r")\b", re.I)

_PUB_LAW_RE = re.compile(
    r"pub\.?\s*l\.?\s*(?:no\.?\s*)?(?P<congress>\d+)[-–](?P<lawnum>\d+)",
    re.IGNORECASE,
)

def _text_from_eyecite(cite) -> str:
    """
    Defensive extraction. Works across eyecite versions.
    Tries fields commonly present on Law citations.
    """
    parts = []

    for attr in ("code", "volume", "title", "full_cite", "short", "reporter", "edition", "section", "page"):
        val = getattr(cite.groups, attr, None)
        if isinstance(val, (str, int)):
            parts.append(str(val))
    s = " ".join(parts).strip()
    if not s:
        s = str(cite)
    return s

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
        if pat.search(text):
            return "federal"

    # State signals: look for a state marker anywhere
    if STATE_REGEX.search(text):
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
        if direct_value:
            return direct_value

    resource_dict = resource_dict or {}
    id_tuple = resource_dict.get("id_tuple")
    if isinstance(id_tuple, tuple):
        mapping = {
            "title": 0,
            "code": 1,
            "reporter": 1,
            "section": 2,
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


def _extract_cfr_part(section: str | None) -> str | None:
    if not section:
        return None
    match = re.match(r"(\d+)", section)
    if match:
        return match.group(1)
    return None


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
    return endpoint, {"format": "pdf"}, None


def _build_cfr_endpoint(
    cite: FullLawCitation,
    resource_dict: Dict[str, Any] | None,
) -> Tuple[str | None, Dict[str, str] | None, Tuple[str, Dict[str, Any]] | None]:
    title = _get_law_group(cite, resource_dict, "title")
    section = _sanitize_section(_get_law_group(cite, resource_dict, "section"))

    part = _extract_cfr_part(section)
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
    params = {"sectionnum": section, "format": "pdf"}
    return endpoint, params, None


def _build_stat_endpoint(
    cite: FullLawCitation,
    resource_dict: Dict[str, Any] | None,
) -> Tuple[str | None, Dict[str, str] | None, Tuple[str, Dict[str, Any]] | None]:
    volume = _get_law_group(cite, resource_dict, "volume")
    page = _get_law_group(cite, resource_dict, "page")

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
    return endpoint, {"format": "pdf"}, None


def _build_fr_endpoint(
    cite: FullLawCitation,
    resource_dict: Dict[str, Any] | None,
) -> Tuple[str | None, Dict[str, str] | None, Tuple[str, Dict[str, Any]] | None]:
    volume = _get_law_group(cite, resource_dict, "volume")
    page = _get_law_group(cite, resource_dict, "page")

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
    return endpoint, {"format": "pdf"}, None


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

    endpoint = f"{GOVINFO_REPORTER_MAP['Pub. L.']}/{congress}/publaw/{lawnum}"
    return endpoint, {"format": "pdf"}, None


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
            None,
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
            None,
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
        logger.debug("Primary full citation is not a FullLawCitation.")
        return "warning", "unsupported_citation_type", None

    jurisdiction = classify_full_law_jurisdiction(primary_full)
    if jurisdiction != "federal":
        logger.debug(f"Unsupported jurisdiction: {jurisdiction}")
        return "warning", "unsupported_jurisdiction", None

    api_key = base64.b64encode(GOVINFO_API_KEY.encode("utf-8"))
    if not api_key:
        logger.debug("Missing API key for GovInfo.")
        return "warning", "missing_api_key", {"source": "govinfo"}

    citation_text = _clean_value(normalized_key) or _clean_value(fallback_citation)

    endpoint, params, error = _build_govinfo_request(primary_full, resource_dict, citation_text)
    if error:
        substatus, details = error
        details = details or {}
        if citation_text:
            details.setdefault("citation", citation_text)
        logger.debug(f"Failed to build GovInfo endpoint: {substatus}. Details: {details}")
        return "warning", substatus, details

    if not endpoint:
        logger.debug(f"Failed to build GovInfo endpoint using {citation_text}.")
        return "warning", "invalid_endpoint", {"source": "govinfo"}

    params = params or {"format": "pdf"}
    params.setdefault("format", "pdf")

    url = f"{GOVINFO_BASE_URL}{endpoint}"
    logger.debug(f"federal_law_verifier.verify_federal_law_citation: Built GovInfo URL: {url}")

    try:
        response = httpx.get(
            url,
            params=params,
            auth=httpx.BasicAuth(api_key, ""),
            headers={"Accept": "application/pdf"},
            timeout=GOVINFO_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        logger.error("GovInfo lookup failed for %s: %s", url, exc)
        return "warning", "lookup_failed", {
            "source": "govinfo",
            "endpoint": endpoint,
            "params": params,
        }
    except Exception as exc:  # pragma: no cover - unexpected failure
        logger.error("Unexpected error during GovInfo lookup for %s: %s", url, exc)
        return "warning", "lookup_error", {
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

    if response.status_code == 401:
        logger.error(f"GovInfo lookup failed for {url}: 401 - {response.text}")
        return "warning", "lookup_auth_failed", details
    if response.status_code == 403:
        logger.error(f"GovInfo lookup failed for {url}: 403 - {response.text}")
        return "warning", "lookup_forbidden", details
    if response.status_code == 429:
        logger.error(f"GovInfo lookup failed for {url}: 429 - {response.text}")
        return "warning", "lookup_rate_limited", details
    if response.status_code >= 500:
        logger.error(f"GovInfo lookup failed for {url}: {response.status_code} - {response.text}")
        return "warning", "lookup_service_error", details
    if response.status_code != 200:
        return "no match", None, details

    content_type = (response.headers.get("Content-Type") or "").lower()
    body = response.content or b""

    if "application/pdf" not in content_type and not body.startswith(b"%PDF"):
        logger.error(f"GovInfo lookup failed for {url}: invalid content type")
        details["content_type"] = response.headers.get("Content-Type")
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
