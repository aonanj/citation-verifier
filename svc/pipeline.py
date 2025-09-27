# pipeline.py
import re
from typing import List, Optional, Dict, Any
from uuid import UUID, uuid4
import asyncio
from enum import Enum
from pydantic import BaseModel
from werkzeug.datastructures import FileStorage

# Parsing
from eyecite import get_citations, clean_text   
from eyecite.models import CitationBase       
# HTTP
import httpx
from .doc_processor import extract_text

# --- Models (POJOs for the pipeline) ---

class ResponseGroup(BaseModel):
    raw_citation: Optional[str]
    case_name: Optional[str]
    case_name_short: Optional[str]
    volume: Optional[str]
    reporter: Optional[str]
    page: Optional[str]
    pin_cite: Optional[str]
    court: Optional[str]
    year: Optional[str]
    vendor_cite: Optional[str]
    parenthetical: Optional[str]
    canonical_reporter: Optional[str]

class ExtractRespItem(BaseModel):
    raw: str
    idxStart: int
    idxEnd: int
    groups: ResponseGroup

class ExtractResp(BaseModel):
    citations: List[ExtractRespItem]

class Citation:
    def __init__(self, raw_text: str, start: int, end: int, normalized: Dict[str, Any]):
        self.id = uuid4()
        self.raw_text = raw_text
        self.start = start
        self.end = end
        self.normalized = normalized
        self.short_form_parent: Optional[UUID] = None

class InputCite:
    def __init__(
        self,
        case_name: str,
        case_name_full: Optional[str],
        reporter: str,
        volume: Optional[str],
        page: Optional[str],
        court: Optional[str],
        year: Optional[str],
        pin_cite: Optional[str],
        vendor_cite: Optional[str],
    ):
        self.case_name = case_name
        self.case_name_full = case_name_full
        self.reporter = reporter
        self.volume = volume
        self.page = page
        self.court = court
        self.year = year
        self.pin_cite = pin_cite
        self.vendor_cite = vendor_cite

class AuthorityHit:
    def __init__(self, provider: str, url: Optional[str], meta: Dict[str, Any], confidence: float):
        self.provider = provider
        self.url = url
        self.meta = meta
        self.confidence = confidence

class Reason(Enum):
    ERROR = "error"
    NO_MATCH = "no_match"
    TIMEOUT = "timeout"
    VERIFIED = "verified"
    WARNING_MISMATCH_CITATION = "warning_mismatch_citation"
    WARNING_MISMATCH_NAME = "warning_mismatch_name"
    WARNING_MISMATCH_YEAR = "warning_mismatch_year"
    
class VerifyResult:
  def __init__(self, input: InputCite):
      meta = {
        "case_name": input.case_name,
        "case_name_full": input.case_name_full,
        "reporter": input.reporter,
        "volume": input.volume,
        "page": input.page,
        "court": input.court,
        "year": input.year,
        "pin_cite": input.pin_cite,
        "vendor_cite": input.vendor_cite
      }
      self.reason: Reason = Reason.NO_MATCH
      self.evidence: Optional[Dict[str, Any]] = None
      self.normalized_case_name: Optional[str] = None
      self.normalized_citations: Optional[List[str]] = None
      self.filed_date: Optional[str] = None
      self.meta = meta

# --- helper functions ---

TOKEN_RE = re.compile(r"""
    (?P<single>[A-Za-z]\.)         # single-capital abbreviation like "F."
  | (?P<multi>[A-Za-z]{2,}\.)      # multi-letter abbreviation like "Supp."
  | (?P<ordinal>\d+(?:st|nd|rd|th|d))  # ordinals/series like "3d", "4th"
""", re.VERBOSE)

def normalize_reporter_abbrev(s: str) -> str:
    """
    Apply Bluebook spacing rules:
      - No spaces between single capitals, or a single capital and an ordinal.
      - Use single spaces between any abbreviation that has >1 letter.
    Examples:
      "U. S."      -> "U.S."
      "F. 3d"      -> "F.3d"
      "F.Supp.2d"  -> "F. Supp. 2d"
      "Cal.App.4th"-> "Cal. App. 4th"
      "S. Ct."     -> "S. Ct."
    """
    # Strip all whitespace to simplify, then re-tokenize.
    s_no_ws = re.sub(r"\s+", "", s)

    tokens = []
    for m in TOKEN_RE.finditer(s_no_ws):
        kind = "single" if m.lastgroup == "single" else "multi" if m.lastgroup == "multi" else "ordinal"
        tokens.append((kind, m.group(0)))

    if not tokens:
        return s.strip()

    out = [tokens[0][1]]
    prev_kind = tokens[0][0]

    for kind, tok in tokens[1:]:
        # Insert space iff at least one side is a multi-letter abbreviation,
        # EXCEPT when the left is a single-letter and right is an ordinal (no space).
        if not (prev_kind == "single" and kind == "ordinal"):
            if "multi" in (prev_kind, kind):
                out.append(" ")
        out.append(tok)
        prev_kind = kind

    return "".join(out)

# --- Citation extraction and normalization ---

def norm(c: CitationBase) -> ResponseGroup:
    # Guard missing attrs; EyeCite object types vary

    normalized: ResponseGroup = ResponseGroup(
        raw_citation = None,
        case_name = None,
        case_name_short = None,
        volume = None,
        reporter = None,
        page = None,
        pin_cite = None,
        court = None,
        year = None,
        vendor_cite = None,
        parenthetical = None,
        canonical_reporter = None
    )
    
    g = c.groups or {}
    if getattr(c.metadata, "resolved_case_name") is not None:
        normalized.case_name = c.metadata.resolved_case_name
    else:
        plaintiff = getattr(c.metadata, "plaintiff", None) or getattr(c.metadata, "petitioner", None)
        defendant = getattr(c.metadata, "defendant", None) or getattr(c.metadata, "respondent", None)
        if plaintiff and defendant:
            normalized.case_name = f"{plaintiff} v. {defendant}"
    if getattr(c.metadata, "resolved_case_name_short") is not None:
        normalized.case_name_short = c.metadata.resolved_case_name_short

    reporter = None
    raw_reporter = g.get("reporter")
    if raw_reporter is not None:
        reporter = normalize_reporter_abbrev(raw_reporter)
    normalized.reporter = reporter

    normalized.raw_citation = getattr(c, "data", None) or g.get("data", None) or None
    normalized.case_name = g.get("case_name") or g.get("short_name") or None
    normalized.volume = g.get("volume") or None
    normalized.reporter = reporter
    normalized.page = g.get("page") or None
    normalized.pin_cite = getattr(c.metadata, "pin_cite", None) or None
    normalized.court = getattr(c.metadata, "court", None) or None
    normalized.year = str(getattr(c, "year", None) or getattr(c.metadata, "year", None) or g.get("year", None) or None)
    normalized.parenthetical = getattr(c.metadata, "parenthetical", None) or g.get("parenthetical", None) or None
    normalized.canonical_reporter = getattr(c.metadata, "canonical_reporter", None) or g.get("canonical_reporter", None) or None
    normalized.vendor_cite = g.get("westlaw_cite") or g.get("lexis_cite")

    return normalized

async def extract_citations(file: FileStorage) -> ExtractResp:

    text = extract_text(file)
    cleaned_text = clean_text(text, ["all_whitespace", "underscores"])

    out = []
    for m in get_citations(cleaned_text or ""):
        s, e = m.span()
        out.append(ExtractRespItem(
            raw=m.matched_text(),
            idxStart=s,
            idxEnd=e,
            groups=norm(m)
        ))
    return ExtractResp(citations=out)

def authority_key(norm: Dict[str, Any]) -> str:
    # Canonical composite key
    parts = [
        norm.get("reporter") or "",
        norm.get("volume") or "",
        norm.get("page") or "",
        norm.get("court") or "",
        str(norm.get("year") or "")
    ]
    return "|".join(p.strip() for p in parts)

# --- Provider adapters ---

class Provider:
    name: str
    async def resolve(self, norm: Dict[str, Any]) -> List[AuthorityHit]:
        raise NotImplementedError

class CourtListenerProvider(Provider):
    name = "courtlistener"
    BASE = "https://www.courtlistener.com/api/rest/v4"
    CL_LOOKUP = f"{BASE}/citation-lookup/"
    CL_SEARCH = f"{BASE}/search/?q="


    async def resolve(self, norm: Dict[str, Any]) -> List[AuthorityHit]:
        # Strategy: search by reporter + volume + page or by case name + year
        hits: List[AuthorityHit] = []
        async with httpx.AsyncClient(timeout=20) as client:
            q = []
            if norm.get("reporter") and norm.get("volume") and norm.get("page"):
                q.append(f"cites__reporter={norm['reporter']}&cites__volume={norm['volume']}&cites__page={norm['page']}")
            if norm.get("case_name"):
                q.append(f"case_name={httpx.QueryParams({'case_name': norm['case_name']})['case_name']}")
            if not q:
                return hits
            url = self.BASE + "?" + "&".join(q)
            r = await client.get(url)
            if r.status_code != 200:
                return hits
            data = r.json()
            for res in data.get("results", []):
                # crude confidence heuristic; refine later
                conf = 0.8
                if str(res.get("year")) == str(norm.get("year")):
                    conf += 0.1
                hits.append(AuthorityHit(self.name, res.get("absolute_url"), res, conf))
        return hits

# --- Verification orchestration ---

async def verify_document_bytes(file: FileStorage, check_quotes: bool = False) -> Dict[str, Any]:


    cites = await extract_citations(file)

    providers: List[Provider] = [CourtListenerProvider()]
    results = []
    for c in cites.citations:
        provider_hits: List[AuthorityHit] = []
        # Try providers concurrently per citation
        hits_lists = await asyncio.gather(*[p.resolve(c.groups) for p in providers])
        for hl in hits_lists:
            provider_hits.extend(hl)

        best = sorted(provider_hits, key=lambda h: h.confidence, reverse=True)[0] if provider_hits else None

        status = "verified" if best else "not_found"
        confidence = best.confidence if best else 0.0
        details = {"provider": best.provider, "url": best.url} if best else {}

        # Optional: quote check placeholder
        if check_quotes and best:
            details["quote_check"] = "skipped"  # implement later

        results.append({
            "citation_id": str(c.id),
            "raw_text": c.,
            "normalized": c.normalized,
            "status": status,
            "confidence": confidence,
            "details": details
        })

    summary = {
        "total": len(results),
        "verified": sum(1 for r in results if r["status"] == "verified"),
        "not_found": sum(1 for r in results if r["status"] == "not_found")
    }
    return {"summary": summary, "citations": results}
