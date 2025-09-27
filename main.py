# main.py
from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional, Dict
from eyecite import get_citations


app = FastAPI(title="eyecite-extractor", version="0.1.0")

class ExtractReq(BaseModel):
    text: str

class ExtractRespItem(BaseModel):
    raw: str
    idxStart: int
    idxEnd: int
    groups: Dict[str, Optional[str]]

class ExtractResp(BaseModel):
    citations: List[ExtractRespItem]

def norm(c):
    # Guard missing attrs; EyeCite object types vary
    
    g = c.groups or {}
    plaintiff = getattr(c.metadata, "plaintiff", None) or getattr(c.metadata, "petitioner", None)
    defendant = getattr(c.metadata, "defendant", None) or getattr(c.metadata, "respondent", None)
    if plaintiff and defendant and "case_name" not in g:
        g["case_name"] = f"{plaintiff} v. {defendant}"
    return {
        "case_name": g.get("case_name") or g.get("short_name") or None,
        "volume": g.get("volume") or None,
        "reporter": g.get("reporter") or None,
        "page": g.get("page") or None,
        "pin": getattr(c.metadata, "pin_cite", None) or None,
        "court": getattr(c.metadata, "court", None) or None,
        "year": str(getattr(c, "year", None) or getattr(c.metadata, "year", "") or ""),
        "vendor_cite": g.get("westlaw_cite") or g.get("lexis_cite"),
    }

@app.post("/extract", response_model=ExtractResp)
def extract(req: ExtractReq):
    out = []
    for m in get_citations(req.text or ""):
        s, e = m.span()
        out.append(ExtractRespItem(
            raw=m.matched_text(),
            idxStart=s,
            idxEnd=e,
            groups=norm(m)
        ))
    return ExtractResp(citations=out)
