// app/api/verify/route.ts
import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

// ===== Types =====
type InputCite = { citation: string; case_name?: string | null; year?: number | null };
type RawCite = {
  raw: string;
  groups: Partial<{
    case_name: string;
    volume: string;
    reporter: string;
    page: string;
    year: string;
  }>;
};
type VerifyResult = {
  input: InputCite;
  citation_match: boolean;
  name_match: boolean;
  year_match: boolean;
  verified: boolean;
  reason?:
    | "no_match"
    | "timeout"
    | "partial_mismatch_citation"
    | "partial_mismatch_name"
    | "partial_mismatch_year"
    | "error";
  evidence?: {
    via: "citation-lookup" | "search";
    url?: string;
    normalized_case_name?: string;
    normalized_citations?: string[];
    filed_date?: string | null;
  };
};

// ===== Config =====
const CL_BASE = "https://www.courtlistener.com";
const CL_LOOKUP = `${CL_BASE}/api/rest/v4/citation-lookup/`; // POST form-encoded
const CL_SEARCH = `${CL_BASE}/api/rest/v4/search/`; // GET

const EXTRACTOR_BASE_URL = process.env.EXTRACTOR_BASE_URL || "http://127.0.0.1:8000";
const EXTRACT_ENDPOINT = `${EXTRACTOR_BASE_URL}/extract`;

const CL_TOKEN = process.env.COURTLISTENER_TOKEN || "";
const DEFAULT_TIMEOUT_MS = Number(process.env.FETCH_TIMEOUT_MS || 15000);
const RETRIES = Number(process.env.FETCH_RETRIES || 2);

// ===== Utils =====
const normSpaces = (s: string) => s.replace(/\s+/g, " ").trim();
const normCite = (s: string) => normSpaces(s).replace(/\.\s+/g, ". ").toUpperCase();
const normName = (s: string) =>
  normSpaces(
    s
      .toUpperCase()
      .replace(/\bet\s+al\.?/gi, "")
      .replace(/[^A-Z0-9\s.'-]/g, "")
  );
const yearOfDate = (date?: string | null) => {
  if (!date) return undefined;
  const m = String(date).match(/(\d{4})/);
  return m ? Number(m[1]) : undefined;
};
function splitReporter(s: string) {
  const m = /^\s*(\d+)\s+([A-Z.]+)\s+(\d+)\s*$/.exec(s.toUpperCase());
  return m ? { vol: m[1], rep: m[2], page: m[3] } : null;
}
function extractCitesFromSearchResult(r: any): string[] {
  return Array.isArray(r?.citation) ? r.citation.map(String).filter(Boolean) : [];
}

async function fetchWithTimeout(url: string, init: RequestInit, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(id);
  }
}

async function fetchJsonWithRetry(url: string, init: RequestInit, retries = RETRIES) {
  let lastErr: any;
  for (let i = 0; i <= retries; i++) {
    try {
      const res = await fetchWithTimeout(url, init);
      const ct = res.headers.get("content-type") || "";
      if (!res.ok) {
        const head = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status} ${res.statusText} head="${head.slice(0, 140)}"`);
      }
      if (!/application\/json/i.test(ct)) {
        const head = await res.text().catch(() => "");
        throw new Error(`Non-JSON response head="${head.slice(0, 140)}"`);
      }
      return await res.json();
    } catch (e) {
      lastErr = e;
      if (i < retries) await new Promise(r => setTimeout(r, 500 * Math.pow(2, i)));
    }
  }
  throw lastErr;
}

function fromEyecite(rc: RawCite): InputCite {
  const citation = `${rc.groups.volume ?? ""} ${rc.groups.reporter ?? ""} ${rc.groups.page ?? ""}`.trim();
  return {
    citation,
    year: rc.groups.year ? Number(rc.groups.year) : null,
    case_name: rc.groups.case_name ?? null,
  };
}

function compareMatches(
  input: InputCite,
  normalized_case_name?: string | null,
  normalized_citations?: string[],
  filed_date?: string | null
) {
  const wantCite = normCite(input.citation);
  const wantName = input.case_name ? normName(input.case_name) : "";
  const wantYear = input.year ?? undefined;

  const hasCite = (normalized_citations || []).some(c => normCite(c) === wantCite);
  const hasName = normalized_case_name ? normName(normalized_case_name) === wantName : false;
  const gotYear = yearOfDate(filed_date);
  const hasYear = wantYear ? gotYear === wantYear : false;

  let reason: VerifyResult["reason"] | undefined;
  const verified = hasCite && hasName && (wantYear ? hasYear : true);

  if (!verified) {
    if (!hasCite) reason = "partial_mismatch_citation";
    else if (!hasName) reason = "partial_mismatch_name";
    else if (wantYear && !hasYear) reason = "partial_mismatch_year";
  }
  return { hasCite, hasName, hasYear, verified, reason };
}

function authHeaders() {
  const h: Record<string, string> = { Accept: "application/json" };
  if (CL_TOKEN) h["Authorization"] = `Token ${CL_TOKEN}`;
  return h;
}

async function lookupByCitation(citationText: string) {
  const body = new URLSearchParams({ text: citationText });
  const data = await fetchJsonWithRetry(
    CL_LOOKUP,
    {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/x-www-form-urlencoded" },
      body,
      next: { revalidate: 0 },
    }
  );
  const hit = Array.isArray(data)
    ? data.find((h: any) => Number(h?.status) === 200 && (h?.clusters?.length ?? 0) > 0)
    : null;
  if (!hit) return null;
  const cluster = hit.clusters?.[0];
  return {
    normalized_citations: hit.normalized_citations as string[] | undefined,
    normalized_case_name: cluster?.case_name as string | undefined,
    filed_date: cluster?.date_filed as string | undefined,
    url: cluster?.absolute_url ? `${CL_BASE}${cluster.absolute_url}` : undefined,
  };
}

async function searchFallback(input: { citation: string; case_name?: string | null }) {
  const q = [`"${input.citation}"`, input.case_name ? `"${input.case_name}"` : null]
    .filter(Boolean)
    .join(" ");
  const url = `${CL_SEARCH}?&q=${encodeURIComponent(q)}`;

  const data = await fetchJsonWithRetry(url, { method: "GET", headers: authHeaders(), next: { revalidate: 0 } });
  const results: any[] = Array.isArray(data?.results) ? data.results : [];
  if (!results.length) return null;

  const target = normCite(input.citation);
  const targetParts = splitReporter(target);

  // 1) exact citation match within any result
  for (const r of results) {
    const cites = extractCitesFromSearchResult(r).map(normCite);
    if (cites.includes(target)) {
      return {
        normalized_citations: cites,
        normalized_case_name: r.caseName,
        filed_date: r.dateFiled,
        url: r.absolute_url ? `${CL_BASE}${r.absolute_url}` : undefined,
      };
    }
  }

  // 2) loose match on reporter tuple if exact failed
  if (targetParts) {
    for (const r of results) {
      const cites = extractCitesFromSearchResult(r);
      const hasLoose = cites.some((c: string) => {
        const p = splitReporter(c);
        return p && p.vol === targetParts.vol && p.rep === targetParts.rep && p.page === targetParts.page;
      });
      if (hasLoose) {
        return {
          normalized_citations: cites.map(normCite),
          normalized_case_name: r.caseName,
          filed_date: r.dateFiled, 
          url: r.absolute_url ? `${CL_BASE}${r.absolute_url}` : undefined,
        };
      }
    }
  }

  // 3) nothing usable
  return null;
}


// ===== Local text extraction for DOCX/PDF =====
function sniffExtFromName(name?: string | null) {
  if (!name) return "";
  const m = name.toLowerCase().match(/\.(\w+)$/);
  return m ? m[1] : "";
}

async function extractTextFromUpload(file: File): Promise<string> {
  const mime = file.type || "";
  const ext = sniffExtFromName(file.name);
  const buf = Buffer.from(await file.arrayBuffer());

  // DOCX
  if (
    mime === "application/vnd.openxmlformats-officedocument.wordprocessingml.document" ||
    ext === "docx"
  ) {
    const mammoth = await import("mammoth");
    const { value: html } = await mammoth.convertToHtml({ buffer: buf });
    return htmlToText(html);
    
  }

  // PDF
  if (mime === "application/pdf" || ext === "pdf") {
    const mod = await import("pdf-parse/lib/pdf-parse.js");
    const pdfParse = (mod.default ?? mod) as (b: Buffer | Uint8Array) => Promise<{ text: string }>;

    if (typeof pdfParse !== "function") {
      throw new Error("pdf-parse export not a function");
    }

    if (!buf.length) throw new Error("empty PDF buffer");

    const { text } = await pdfParse(buf);
    const t = (text || "").trim();
    if (t) return t;
  }


  // Plain text fallback
  try {
    const utf8 = buf.toString("utf8").trim();
    if (utf8) return utf8;
  } catch {
    // ignore
  }

  throw new Error("unable to extract text from upload");
}

function htmlToText(html: string): string {
  return html
    .replace(/<head[\s\S]*?<\/head>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/\u00A0/g, " ")
    .trim();
}

// ===== Route =====
export async function POST(req: NextRequest) {
  try {
    const ct = req.headers.get("content-type") || "";
    if (!/multipart\/form-data/i.test(ct)) {
      return NextResponse.json({ error: "expected multipart/form-data with 'file'" }, { status: 400 });
    }

    const form = await req.formData();
    const file = form.get("file");
    if (!(file instanceof File)) {
      return NextResponse.json({ error: "file missing" }, { status: 400 });
    }

    // 1) Extract text locally
    let text: string;
    try {
      text = await extractTextFromUpload(file);
      text = text.replace(/[ \t]+/g, " ").replace(/\n+/g, "\n").trim();
    } catch (e: any) {
      return NextResponse.json({ error: `text extraction failed: ${e?.message ?? "unknown"}` }, { status: 422 });
    }

    // 2) Call /extract with JSON { text }
    let rawCites: RawCite[] = [];
    try {
      const data = await fetchJsonWithRetry(
        EXTRACT_ENDPOINT,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ text }),
          next: { revalidate: 0 },
        }
      );
      rawCites = (data?.citations ?? []) as RawCite[];
      console.log("Extracted data:", data);
      console.log("Extracted cites:", data?.citations);
    } catch (e: any) {
      return NextResponse.json({ error: `extract service error: ${e?.message ?? "unknown"}` }, { status: 502 });
    }

    // 3) Normalize extracted citations
    const inputs: InputCite[] = rawCites
      .map(fromEyecite)
      .filter(c => c.citation && /\d/.test(c.citation));

    // 4) Verify each citation against CourtListener
    const results: VerifyResult[] = [];
    for (const input of inputs) {
      try {
        const viaLookup = await lookupByCitation(input.citation);
        if (viaLookup) {
          const cmp = compareMatches(
            input,
            viaLookup.normalized_case_name,
            viaLookup.normalized_citations,
            viaLookup.filed_date
          );
          results.push({
            input,
            citation_match: !!cmp.hasCite,
            name_match: !!cmp.hasName,
            year_match: !!cmp.hasYear,
            verified: cmp.verified,
            reason: cmp.verified ? undefined : cmp.reason,
            evidence: { via: "citation-lookup", ...viaLookup },
          });
          continue;
        }
      } catch {
        // fall through
      }

      try {
        const viaSearch = await searchFallback(input);
        if (viaSearch) {
          const cmp = compareMatches(
            input,
            viaSearch.normalized_case_name,
            viaSearch.normalized_citations,
            viaSearch.filed_date
          );
          results.push({
            input,
            citation_match: !!cmp.hasCite,
            name_match: !!cmp.hasName,
            year_match: !!cmp.hasYear,
            verified: cmp.verified,
            reason: cmp.verified ? undefined : cmp.reason,
            evidence: { via: "search", ...viaSearch },
          });
        } else {
          results.push({
            input,
            citation_match: false,
            name_match: false,
            year_match: false,
            verified: false,
            reason: "no_match",
          });
        }
      } catch {
        results.push({
          input,
          citation_match: false,
          name_match: false,
          year_match: false,
          verified: false,
          reason: "timeout",
        });
      }
    }

    const summary = {
      allVerified: results.length > 0 && results.every(r => r.verified),
      counts: {
        total: results.length,
        verified: results.filter(r => r.verified).length,
        partial: results.filter(r => !r.verified && r.reason?.startsWith("partial_")).length,
        timeout: results.filter(r => r.reason === "timeout").length,
        no_match: results.filter(r => r.reason === "no_match").length,
      },
      results,
    };

    return NextResponse.json(summary, { status: 200 });
  } catch (e: any) {
    return NextResponse.json({ error: e?.message ?? "unknown" }, { status: 500 });
  }
}
