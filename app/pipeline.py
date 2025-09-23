# pipeline.py
from pathlib import Path
from typing import List, Optional, Dict, Any
from uuid import UUID, uuid4
import asyncio

# Parsing
from eyecite import get_citations 
# DOCX/PDF extraction
from docx import Document as DocxDocument 
from pypdf import PdfReader                
# HTTP
import httpx

# --- Models (POJOs for the pipeline) ---

class Citation:
    def __init__(self, raw_text: str, start: int, end: int, normalized: Dict[str, Any]):
        self.id = uuid4()
        self.raw_text = raw_text
        self.start = start
        self.end = end
        self.normalized = normalized
        self.short_form_parent: Optional[UUID] = None

class AuthorityHit:
    def __init__(self, provider: str, url: Optional[str], meta: Dict[str, Any], confidence: float):
        self.provider = provider
        self.url = url
        self.meta = meta
        self.confidence = confidence

# --- Text extraction ---

def read_docx_bytes(data: bytes) -> str:
    tmp = Path("/tmp/tmp.docx")
    tmp.write_bytes(data)
    doc = DocxDocument(str(tmp))
    return "\n".join(p.text for p in doc.paragraphs)

def read_pdf_bytes(data: bytes) -> str:
    tmp = Path("/tmp/tmp.pdf")
    tmp.write_bytes(data)
    reader = PdfReader(str(tmp))
    return "\n".join(page.extract_text() or "" for page in reader.pages)

def sniff_and_extract_text(filename: str, data: bytes) -> str:
    if filename.lower().endswith(".docx"):
        return read_docx_bytes(data)
    if filename.lower().endswith(".pdf"):
        return read_pdf_bytes(data)
    raise ValueError("Unsupported file type")

# --- Citation extraction and normalization ---

REPORTER_MAP = {
    # expand as needed
    "F.3d": "F.3d",
    "F.2d": "F.2d",
    "U.S.": "U.S.",
    "S. Ct.": "S. Ct."
}

def normalize_eyecite(cite_obj) -> Dict[str, Any]:
    # eyecite returns structured pieces; map to canonical dict
    return {
        "reporter": cite_obj.reporter or None,
        "volume": cite_obj.volume or None,
        "page": cite_obj.page or None,
        "court": getattr(cite_obj, "court", None),
        "year": getattr(cite_obj, "year", None),
        "pin_cite": getattr(cite_obj, "pin_cite", None),
        "case_name": getattr(cite_obj, "case_name", None),
        "vendor_cite": getattr(cite_obj, "westlaw_cite", None) or getattr(cite_obj, "lexis_cite", None)
    }

def extract_citations(text: str) -> List[Citation]:
    cites = []
    for match in get_citations(text):
        norm = normalize_eyecite(match)
        cites.append(Citation(match.matched_text(), match.span()[0], match.span()[1], norm))
    # TODO: short-form/id./supra linking via a local graph
    return cites

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
    BASE = "https://www.courtlistener.com/api/rest/v3/opinions/"

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

async def verify_document_bytes(filename: str, data: bytes, check_quotes: bool = False) -> Dict[str, Any]:
    text = sniff_and_extract_text(filename, data)
    cites = extract_citations(text)

    providers: List[Provider] = [CourtListenerProvider()]
    results = []
    for c in cites:
        provider_hits: List[AuthorityHit] = []
        # Try providers concurrently per citation
        hits_lists = await asyncio.gather(*[p.resolve(c.normalized) for p in providers])
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
            "raw_text": c.raw_text,
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
