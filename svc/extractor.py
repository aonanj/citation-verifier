from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple, Union, cast
import re
from pprint import pprint

from eyecite import get_citations, resolve_citations
from eyecite.models import FullJournalCitation
from eyecite.resolve import Resolutions

# --- Normalized outputs ------------------------------------------------------
@dataclass
class CaseNorm:
    kind: str            # "case"
    raw_citation: str | None  # optional original raw citation
    case_name: str | None
    case_name_short: str | None
    court: str | None
    reporter: str | None
    canonical_reporter: str | None
    volume: str | None
    page: str | None
    year: str | None
    pin_cite: str | None
    parenthetical: str | None
    span: Tuple[int, int]
    index: int | None
    raw: str | None

@dataclass
class LawNorm:
    kind: str            # "law"
    code: str | None
    section: str | None
    title: str | None
    year: str | None
    pin_cite: str | None
    parenthetical: str | None
    span: Tuple[int, int]
    index: int | None
    raw: str | None

@dataclass
class OtherNorm:
    kind: str            # "other"
    title: str | None
    author: str | None
    volume: str | None
    journal: str | None
    page: str | None
    year: str | None
    pin_cite: str | None
    parenthetical: str | None
    span: Tuple[int, int]
    index: int | None
    raw: str | None

Normalized = Union[CaseNorm, LawNorm, OtherNorm]

# --- helpers -----------------------------------------------------------------

_space_re = re.compile(r"\s+")

def _clean(s: str | None) -> str | None:
    if not s:
        return s
    v = str(s)
    v = _space_re.sub(" ", v).strip()
    return v if v else None

def _clean_join(v) -> str | None:
    if not v:
        return None
    if isinstance(v, (list, tuple)):
        v2 = " ".join(str(x) for x in v if x)
    else:
        v2 = str(v)
    v2 = _space_re.sub(" ", v2).strip()
    return v2 if v2 else None
   
def _ctype(obj: Any) -> str:
    return type(obj).__name__  # e.g., FullCaseCitation, ShortCaseCitation, IdCitation, SupraCitation, FullLawCitation...

def _coerce_span(obj: Any) -> Tuple[int, int]:
    s = getattr(obj, "span", None)
    if isinstance(s, tuple) and len(s) >= 2:
        a = 0 if s[0] is None else int(s[0])
        b = 0 if s[1] is None else int(s[1])
        return (a, b)
    return (0, 0)

def _get_journal_author_title(obj) -> dict[str | None, str | None] | None:
    if not hasattr(obj, "document") or not hasattr(obj.document, "plain_text"):
        return None
    if not hasattr(obj, "token") or not hasattr(obj.token, "data"):
        return None
    cs = f", {obj.token.data}"
    if obj.year is not None:
        cs += f" ({obj.year})."
    else:
        cs += "."
    text_block = obj.document.plain_text
    text_block = text_block.replace(cs, "").strip()
    sentences = re.split(r'([.!?])', text_block)
    sentences = [s.strip() for s in sentences if s.strip()]
    print(f"Journal citation text block sentences: {sentences}")

    raw_cite = None
    if len(sentences) >= 2:
        raw_cite = sentences[-1].strip()
    elif len(sentences) == 1:
        raw_cite = sentences[0].strip()
    else:
        return None
    
    parts = raw_cite.split(',')
    if len(parts) < 2:
        return None
    author = _clean(parts[0])
    title = _clean(parts[1])
    return {"author": author, "title": title}



# --- Per-class normalizers ---------------------------------------------------
def _norm_case(obj) -> CaseNorm:

    cn: CaseNorm = CaseNorm(
        kind = "case",
        raw_citation = None,
        case_name = None,
        case_name_short = None,
        court = None,
        reporter = None,
        canonical_reporter = None,
        volume = None,
        page = None,
        year = None,
        pin_cite = None,
        parenthetical = None,
        span = (0, 0),
        index = None,
        raw = ""
    )

    cn.raw_citation = obj.token.data if hasattr(obj, "token") and hasattr(obj.token, "data") else None

    if obj.groups is not None:
        g = obj.token.groups or {}
        cn.reporter = _clean(g.get("reporter", None))
        cn.volume = _clean(g.get("volume", None))
        cn.page = _clean(g.get("page", None))
    
    if obj.full_span_start is not None and obj.full_span_end is not None:
        cn.span = (int(obj.full_span_start), int(obj.full_span_end))

    if getattr(obj, "metadata", None) is not None:
        md = obj.metadata
        plaintiff = _clean(md.plaintiff or md.petitioner or None)
        defendant = _clean(md.defendant or md.respondent or None)
        if plaintiff is not None and defendant is not None:
            cn.case_name = f"{plaintiff} v. {defendant}"
        cn.case_name_short = _clean(md.resolved_case_name_short or None)
        cn.year = _clean(md.year or None)
        cn.pin_cite = _clean(md.pin_cite or None)
        cn.court = _clean(md.court or None)
        cn.parenthetical = _clean(md.parenthetical or None)

    if hasattr(obj, "document"):    
        cn.raw = _clean(obj.document.plain_text) or None

    if cn.year is None and hasattr(obj, "year"):
        cn.year = _clean(str(getattr(obj, "year", None)))

    return cn

def _norm_law(obj) -> LawNorm:

    ln: LawNorm = LawNorm(
        kind="law",
        code=None,
        section=None,
        title=None,
        year=None,
        pin_cite=None,
        parenthetical=None,
        span=(0, 0),
        index=obj.index if hasattr(obj, "index") else None,
        raw=""
    )

    if obj.groups is not None:
        ln.code = _clean(obj.groups.get("reporter", None)) or None
        ln.section = _clean(obj.groups.get("section", None)) or None
        ln.title = _clean(obj.groups.get("title", None)) or None

    if obj.document is not None:
        ln.raw = _clean(obj.document.plain_text) or None

    if obj.metadata is not None:
        md = obj.metadata
        ln.year = _clean(md.year or None)
        ln.pin_cite = _clean(md.pin_cite or None)
        ln.parenthetical = _clean(md.parenthetical or None)

    if ln.year is None and hasattr(obj, "year"):
        ln.year = _clean(str(getattr(obj, "year", None)))
    
    start = 0
    end = 0
    if obj.full_span_end is not None:
        end = int(obj.full_span_end)
    else:
        end = obj.token.end if hasattr(obj, "token") and hasattr(obj.token, "end") else 0
    if obj.full_span_start is not None:
        start = int(obj.full_span_start)
    else:
        start = obj.token.start if hasattr(obj, "token") and hasattr(obj.token, "start") else 0
    
    ln.span = (start, end)

    return ln

def _norm_other(obj) -> OtherNorm:

    on: OtherNorm = OtherNorm(
        kind="other",
        title=None,
        author=None,
        volume=None,
        journal=None,
        page=None,
        year=None,
        pin_cite=None,
        parenthetical=None,
        span=(0, 0),
        index=obj.index if hasattr(obj, "index") else None,
        raw=""
    )

    if obj.groups is not None:
        on.journal = _clean(obj.groups.get("reporter", None)) or None
        on.volume = _clean(obj.groups.get("volume", None)) or None
        on.page = _clean(obj.groups.get("page", None)) or None

    if obj.document is not None:
        on.raw = _clean(obj.document.plain_text) or None

    if obj.metadata is not None:
        md = obj.metadata
        on.year = _clean(md.year or None)
        on.pin_cite = _clean(md.pin_cite or None)
        on.parenthetical = _clean(md.parenthetical or None)

    if on.year is None and hasattr(obj, "year"):
        on.year = _clean(str(getattr(obj, "year", None)))
    
    start = 0
    end = 0
    if obj.full_span_end is not None:
        end = int(obj.full_span_end)
    else:
        end = obj.token.end if hasattr(obj, "token") and hasattr(obj.token, "end") else 0
    if obj.full_span_start is not None:
        start = int(obj.full_span_start)
    else:
        start = obj.token.start if hasattr(obj, "token") and hasattr(obj.token, "start") else 0
    
    on.span = (start, end)

    if isinstance(obj, FullJournalCitation):
        ja = _get_journal_author_title(obj)
        if ja is not None:
            on.author = ja.get("author", None)
            on.title = ja.get("title", None)

    return on

# --- Resource binding for resolver ------------------------------------------
@dataclass(frozen=True)
class ResourceKey:
    kind: str                # "case" | "law" | "other"
    id_tuple: Tuple[str, ...]  # stable tuple to represent the work

def _bind_full_citation(full_cite) -> Optional[ResourceKey]:
    """Return a stable key Eyecite will use as the 'resource' for short forms."""
    t = _ctype(full_cite)
    if t == "FullCaseCitation":
        name = (_clean(getattr(full_cite, "case_name", None) or getattr(full_cite, "normalized_case_name", None)) or "").lower()
        reporter = (_clean(getattr(full_cite, "reporter", None)) or "").lower()
        vol = _clean(getattr(full_cite, "volume", None)) or ""
        page = _clean(getattr(full_cite, "page", None)) or ""
        year = _clean(getattr(full_cite, "year", None)) or ""
        return ResourceKey("case", (name, reporter, vol, page, year))
    if t == "FullLawCitation":
        code = (_clean(getattr(full_cite, "code", None) or getattr(full_cite, "jurisdiction", None)) or "").lower()
        section = getattr(full_cite, "section", None) or getattr(full_cite, "sections", None)
        section = _clean_join(section) or ""
        year = _clean(getattr(full_cite, "year", None)) or ""
        return ResourceKey("law", (code, section, year))
    # Treat everything else as "other" so supra can still cluster journals, etc.
    title = (_clean(getattr(full_cite, "title", None)) or "").lower()
    author = (_clean(getattr(full_cite, "author", None)) or "").lower()
    journal = (_clean(getattr(full_cite, "reporter", None)) or "").lower()
    volume = _clean(getattr(full_cite, "volume", None)) or ""
    page = _clean(getattr(full_cite, "page", None)) or ""
    year = _clean(getattr(full_cite, "year", None)) or ""
    return ResourceKey("other", (title, author, journal, volume, page, year))

# --- End-to-end using Eyecite resolver --------------------------------------
def extract_and_resolve(text: str) -> Dict[str, Any]:
    """
    Output:
      {
        "groups": [
          {
            "resource": {"kind": "...", "id": [...]},
            "full": CaseNorm|LawNorm|OtherNorm,
            "mentions": [CaseNorm|LawNorm|OtherNorm],  # includes shorts, supra, id. normalized
          },
          ...
        ],
        "unresolved": [ {"type": "...", "raw": "...", "span": (s,e)} ],
      }
    """
    cites = sorted(get_citations(text), key=lambda c: _coerce_span(c)[0])

    groups_any: Resolutions = resolve_citations(
        cites,
        resolve_full_citation=_bind_full_citation  # called on every full citation
    )

    groups: Dict[ResourceKey, List[Any]] = {
        cast(ResourceKey, k): list(v) for k, v in groups_any.items()
    }

    out_groups: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []

    # Normalize each group
    for rkey, clist in groups.items():
        # Identify the canonical full cite in the cluster
        fulls = [c for c in clist if _ctype(c) in {"FullCaseCitation", "FullLawCitation", "FullJournalCitation"}]
        shorts = [c for c in clist if c not in fulls]

        # Fallback: no explicit full present (rare but possible)
        first = fulls[0] if fulls else clist[0]

        # Normalize the full by kind
        if rkey.kind == "case":
            full_norm: Normalized = _norm_case(first)
            mk_norm = lambda c: _norm_case(c)
        elif rkey.kind == "law":
            full_norm = _norm_law(first)
            mk_norm = lambda c: _norm_law(c)
        else:
            full_norm = _norm_other(first)
            mk_norm = lambda c: _norm_other(c)

        mentions = [mk_norm(c) for c in clist]  # include full + shorts

        out_groups.append({
            "resource": {"kind": rkey.kind, "id": list(rkey.id_tuple)},
            "full": asdict(full_norm),
            "mentions": [asdict(m) for m in mentions],
        })

    # Anything Eyecite left ungrouped but not classifiable
    for c in cites:
        # If resolver could not attach a short form, it remains alone in a group whose "full" may not exist.
        # Mark ambiguous short-forms for manual handling.
        t = _ctype(c)
        if t in {"IdCitation", "SupraCitation", "ShortCaseCitation", "SupraOrShortCaseCitation"}:
            # Check if it belongs to any produced group
            grouped_ids = {id(m) for glist in groups.values() for m in glist}
            in_any = id(c) in grouped_ids
            if not in_any:
                unresolved.append({"type": t, "raw": getattr(c, "matched_text", ""), "span": tuple(getattr(c, "span", (0, 0)))})

    return {"groups": out_groups, "unresolved": unresolved}

# --- Example -----------------------------------------------------------------
if __name__ == "__main__":
    #sample = "Brown v. Board of Education, 347 U.S. 483 (1954)."
    #sample = 'See also 28 U.S.C. § 1291 (2006)'
    # Id. at 490.
    # Smith v. Jones, 123 F.3d 45 (9th Cir. 1997).
    # Smith, supra, at 47.
    sample = """
    If possible, you should cite to the current official code or the supplement. Otherwise, cite a current unofficial code or its supplements. If these are unavailable, instead cite to (in order of decreasing preference) the official session laws, privately published session laws (like United States Code Congressional and Administrative News), a commercial electronic database (Westlaw, Lexis, etc.), a looseleaf service, an internet source, or even a newspaper. Alex Osterlind, Staking a Claim on the Building Blocks of Life, 75 Mo. L. Rev. 617 (2010)."""
    #sample = "Cal. Penal Code § 13777 (2024)."

    result = extract_and_resolve(sample)
    pprint(f"\nRESULT: {result}\n")
    for group in result.get("groups", []):
        print("\nRESOURCE:", group["resource"])
        print("\n FULL:", group["full"])
        print("\n MENTIONS:")
        for m in group["mentions"]:
            print("\n   -", m)
        print()
