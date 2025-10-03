'use client';

import { FormEvent, useMemo, useState } from 'react';

const DEFAULT_API_BASE_URL = 'http://localhost:8000';

const API_BASE_URL = process.env.BACKEND_URL ?? DEFAULT_API_BASE_URL;

type CitationOccurrence = {
  citation_category: string | null;
  matched_text: string | null;
  span: number[] | null;
  pin_cite: string | null;
};

type CitationEntry = {
  resource_key: string;
  type: string;
  status: string;
  substatus: string | null;
  normalized_citation: string | null;
  resource: Record<string, unknown>;
  occurrences: CitationOccurrence[];
  verification_details?: {
    source?: string;
    mismatched_fields?: string[];
    extracted?: {
      case_name?: string | null;
      year?: string | null;
    } | null;
    court_listener?: {
      case_name?: string | null;
      year?: string | null;
    } | null;
    lookup_request?: {
      volume?: string | null;
      reporter?: string | null;
      page?: string | null;
    } | null;
  } | null;
};

type VerificationResponse = {
  citations: CitationEntry[];
  extracted_text?: string | null;
};

type StatusTheme = {
  badgeBackground: string;
  badgeBorder: string;
  badgeText: string;
  pillBackground: string;
  pillText: string;
  highlight: string;
  indicator: string;
};

type HighlightSegment = {
  key: string;
  content: string;
  highlight?: {
    color: string;
    indicator: string;
    indicatorColor: string;
  };
};

type HighlightRange = {
  key: string;
  start: number;
  end: number;
  theme: StatusTheme;
  citationOrder: number;
};

const STATUS_THEMES: Record<string, StatusTheme> = {
  verified: {
    badgeBackground: 'rgba(22, 163, 74, 0.12)',
    badgeBorder: 'rgba(34, 197, 94, 0.35)',
    badgeText: '#166534',
    pillBackground: 'rgba(34, 197, 94, 0.14)',
    pillText: '#14532d',
    highlight: 'rgba(34, 197, 94, 0.25)',
    indicator: '#16a34a',
  },
  warning: {
    badgeBackground: 'rgba(234, 179, 8, 0.15)',
    badgeBorder: 'rgba(234, 179, 8, 0.35)',
    badgeText: '#92400e',
    pillBackground: 'rgba(250, 204, 21, 0.18)',
    pillText: '#854d0e',
    highlight: 'rgba(250, 204, 21, 0.28)',
    indicator: '#f59e0b',
  },
  'no match': {
    badgeBackground: 'rgba(248, 113, 113, 0.14)',
    badgeBorder: 'rgba(248, 113, 113, 0.35)',
    badgeText: '#991b1b',
    pillBackground: 'rgba(248, 113, 113, 0.16)',
    pillText: '#7f1d1d',
    highlight: 'rgba(220, 38, 38, 0.25)',
    indicator: '#dc2626',
  },
  no_match: {
    badgeBackground: 'rgba(248, 113, 113, 0.14)',
    badgeBorder: 'rgba(248, 113, 113, 0.35)',
    badgeText: '#991b1b',
    pillBackground: 'rgba(248, 113, 113, 0.16)',
    pillText: '#7f1d1d',
    highlight: 'rgba(220, 38, 38, 0.25)',
    indicator: '#dc2626',
  },
  error: {
    badgeBackground: 'rgba(148, 163, 184, 0.18)',
    badgeBorder: 'rgba(148, 163, 184, 0.38)',
    badgeText: '#1f2937',
    pillBackground: 'rgba(148, 163, 184, 0.18)',
    pillText: '#1f2937',
    highlight: 'rgba(148, 163, 184, 0.3)',
    indicator: '#475569',
  },
  unknown: {
    badgeBackground: 'rgba(59, 130, 246, 0.15)',
    badgeBorder: 'rgba(59, 130, 246, 0.32)',
    badgeText: '#1d4ed8',
    pillBackground: 'rgba(59, 130, 246, 0.16)',
    pillText: '#1e3a8a',
    highlight: 'rgba(59, 130, 246, 0.25)',
    indicator: '#2563eb',
  },
};

const normalizeKey = (value: string | null | undefined): string => {
  if (!value) {
    return 'unknown';
  }
  return value.trim().toLowerCase();
};

const getStatusTheme = (status: string): StatusTheme => {
  const theme = STATUS_THEMES[normalizeKey(status)];
  if (theme) {
    return theme;
  }
  return STATUS_THEMES.unknown;
};

const isFiniteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

const formatIdentifier = (value: string | null | undefined): string | null => {
  if (!value) {
    return null;
  }
  return value
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
    .join(' ');
};

const getDisplayCitation = (citation: CitationEntry): string => {
  const fullMatch = citation.occurrences.find(
    (occ) => occ.citation_category === 'full' && occ.matched_text,
  );

  if (fullMatch?.matched_text) {
    return fullMatch.matched_text;
  }

  const firstMatch = citation.occurrences.find((occ) => occ.matched_text);
  if (firstMatch?.matched_text) {
    return firstMatch.matched_text;
  }

  return citation.normalized_citation ?? citation.resource_key;
};

const sortOccurrences = (occurrences: CitationOccurrence[]): CitationOccurrence[] => {
  return [...occurrences].sort((a, b) => {
    const aStart = Array.isArray(a.span) ? a.span[0] ?? Number.MAX_SAFE_INTEGER : Number.MAX_SAFE_INTEGER;
    const bStart = Array.isArray(b.span) ? b.span[0] ?? Number.MAX_SAFE_INTEGER : Number.MAX_SAFE_INTEGER;
    return aStart - bStart;
  });
};

const HIGHLIGHT_BACKTRACK_WINDOW = 600;

const getApproximateSpanStart = (occurrence: CitationOccurrence): number | null => {
  if (!Array.isArray(occurrence.span) || occurrence.span.length === 0) {
    return null;
  }

  const [rawStart] = occurrence.span;
  if (!isFiniteNumber(rawStart)) {
    return null;
  }

  return Math.max(0, Math.floor(rawStart));
};

const escapeRegex = (value: string): string =>
  value.replace(/[.*+?^${}()|[\]\\]/g, '\$&');

const buildWhitespaceFlexibleRegExp = (value: string): RegExp | null => {
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }

  const parts = trimmed.split(/\s+/).map((segment) => escapeRegex(segment)).filter(Boolean);
  if (parts.length === 0) {
    return null;
  }

  const pattern = parts.join('\\s+');
  return new RegExp(pattern, 'gu');
};

const findRegexMatch = (text: string, regex: RegExp, startIndex: number): { start: number; end: number } | null => {
  const sliceStart = Math.max(0, Math.min(text.length, startIndex));
  const targetText = text.slice(sliceStart);
  regex.lastIndex = 0;
  const match = regex.exec(targetText);
  if (!match) {
    return null;
  }

  const start = sliceStart + match.index;
  return {
    start,
    end: Math.min(text.length, start + match[0].length),
  };
};

const findMatchInText = (
  text: string,
  needle: string,
  searchCursor: number,
  approxStart: number | null,
): { start: number; end: number } | null => {
  if (!needle) {
    return null;
  }

  const textLength = text.length;
  const uniqueNeedles = Array.from(new Set([needle, needle.trim()].filter((candidate) => candidate.length > 0)));

  const attemptExact = (candidate: string, fromIndex: number): { start: number; end: number } | null => {
    const index = text.indexOf(candidate, Math.max(0, fromIndex));
    if (index === -1) {
      return null;
    }
    return { start: index, end: Math.min(textLength, index + candidate.length) };
  };

  for (const candidate of uniqueNeedles) {
    const direct = attemptExact(candidate, searchCursor);
    if (direct) {
      return direct;
    }
  }

  if (approxStart !== null) {
    const approxWindowStart = Math.max(0, approxStart - HIGHLIGHT_BACKTRACK_WINDOW);
    for (const candidate of uniqueNeedles) {
      const nearby = attemptExact(candidate, approxWindowStart);
      if (nearby && nearby.end > searchCursor) {
        return nearby;
      }
    }
  }

  const regex = buildWhitespaceFlexibleRegExp(needle);
  if (regex) {
    const regexMatch = findRegexMatch(text, regex, searchCursor);
    if (regexMatch) {
      return regexMatch;
    }

    if (approxStart !== null) {
      const regexFallback = findRegexMatch(text, regex, Math.max(0, approxStart - HIGHLIGHT_BACKTRACK_WINDOW));
      if (regexFallback && regexFallback.end > searchCursor) {
        return regexFallback;
      }
    }
  }

  return null;
};

const calculateHighlightRanges = (text: string, citations: CitationEntry[]): HighlightRange[] => {
  if (!text) {
    return [];
  }

  const occurrenceContexts = citations.flatMap((citation, citationIndex) => {
    const theme = getStatusTheme(citation.status);
    const sortedOccurrences = sortOccurrences(citation.occurrences);

    return sortedOccurrences.map((occurrence, occurrenceIndex) => ({
      occurrence,
      citationIndex,
      occurrenceIndex,
      theme,
      resourceKey: citation.resource_key,
    }));
  });

  occurrenceContexts.sort((a, b) => {
    const aStart = getApproximateSpanStart(a.occurrence);
    const bStart = getApproximateSpanStart(b.occurrence);

    if (aStart !== null && bStart !== null && aStart !== bStart) {
      return aStart - bStart;
    }
    if (aStart !== null) {
      return -1;
    }
    if (bStart !== null) {
      return 1;
    }

    if (a.citationIndex !== b.citationIndex) {
      return a.citationIndex - b.citationIndex;
    }

    return a.occurrenceIndex - b.occurrenceIndex;
  });

  const ranges: HighlightRange[] = [];
  let searchCursor = 0;

  occurrenceContexts.forEach((context) => {
    const { occurrence, citationIndex, occurrenceIndex, theme, resourceKey } = context;
    const matchedText = occurrence.matched_text;

    if (!matchedText) {
      return;
    }

    const approxStart = getApproximateSpanStart(occurrence);
    const match = findMatchInText(text, matchedText, searchCursor, approxStart);

    if (!match) {
      return;
    }

    ranges.push({
      key: `${resourceKey}-${citationIndex}-${occurrenceIndex}-${match.start}`,
      start: match.start,
      end: match.end,
      theme,
      citationOrder: citationIndex + 1,
    });

    searchCursor = Math.max(searchCursor, match.end);
  });

  ranges.sort((a, b) => {
    if (a.start === b.start) {
      return a.end - b.end;
    }
    return a.start - b.start;
  });

  return ranges;
};

const buildHighlightSegments = (
  extractedText: string | null,
  citations: CitationEntry[],
): HighlightSegment[] => {
  if (!extractedText || extractedText.length === 0) {
    return [];
  }

  const textLength = extractedText.length;
  const ranges = calculateHighlightRanges(extractedText, citations);

  if (ranges.length === 0) {
    return [
      {
        key: 'plain-all',
        content: extractedText,
      },
    ];
  }

  const segments: HighlightSegment[] = [];
  let cursor = 0;

  ranges.forEach((range, index) => {
    if (range.start > cursor) {
      segments.push({
        key: `plain-${cursor}-${range.start}-${index}`,
        content: extractedText.slice(cursor, range.start),
      });
    }

    const highlightStart = Math.max(range.start, cursor);

    if (highlightStart >= range.end) {
      cursor = Math.max(cursor, range.end);
      return;
    }

    segments.push({
      key: `highlight-${range.key}-${index}`,
      content: extractedText.slice(highlightStart, range.end),
      highlight: {
        color: range.theme.highlight,
        indicator: `#${range.citationOrder}`,
        indicatorColor: range.theme.indicator,
      },
    });

    cursor = range.end;
  });

  if (cursor < textLength) {
    segments.push({
      key: `plain-${cursor}-end`,
      content: extractedText.slice(cursor),
    });
  }

  return segments;
};

export default function HomePage() {
  const [citations, setCitations] = useState<CitationEntry[]>([]);
  const [extractedText, setExtractedText] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);

  const citationCount = citations.length;

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setSuccessMessage(null);

    const form = event.currentTarget;
    const data = new FormData(form);
    const file = data.get('document');

    if (!(file instanceof File) || file.size === 0) {
      setError('Please choose a PDF, DOCX, or TXT document to upload.');
      return;
    }

    setIsLoading(true);

    try {
      const response = await fetch(`${API_BASE_URL}/api/verify`, {
        method: 'POST',
        body: data,
      });

      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        const message = detail?.detail ?? 'Unable to process the document. Please try again.';
        throw new Error(message);
      }

      const payload = (await response.json()) as VerificationResponse;
      setCitations(payload.citations ?? []);
      setExtractedText(payload.extracted_text ?? null);
      setSuccessMessage('Document processed successfully.');
      form.reset();
    } catch (fetchError) {
      const message = fetchError instanceof Error ? fetchError.message : 'Something went wrong.';
      setError(message);
      setCitations([]);
      setExtractedText(null);
    } finally {
      setIsLoading(false);
    }
  };

  const citationSummary = useMemo(() => {
    if (citations.length === 0) {
      return [] as Array<{ label: string; value: number }>;
    }

    const counts = citations.reduce(
      (acc, entry) => {
        const key = entry.type ?? 'unknown';
        acc[key] = (acc[key] ?? 0) + 1;
        return acc;
      },
      {} as Record<string, number>,
    );

    return Object.entries(counts).map(([kind, total]) => ({
      label: formatIdentifier(kind) ?? 'Unknown',
      value: total,
    }));
  }, [citations]);

  const highlightedExtractSegments = useMemo(
    () => buildHighlightSegments(extractedText, citations),
    [citations, extractedText],
  );

  return (
    <main
      style={{
        minHeight: '100vh',
        background: 'linear-gradient(145deg, #ebf1ff 0%, #f7f9fc 50%, #eef2ff 100%)',
        padding: '3rem 1.5rem',
      }}
    >
      <div
        style={{
          maxWidth: '1080px',
          margin: '0 auto',
          backgroundColor: '#ffffff',
          borderRadius: '20px',
          padding: '3rem',
          boxShadow: '0 30px 80px rgba(30, 64, 175, 0.12)',
          border: '1px solid rgba(15, 23, 42, 0.06)',
        }}
      >
        <header style={{ marginBottom: '2.5rem' }}>
          <h1
            style={{
              fontSize: '2.75rem',
              fontWeight: 700,
              color: '#0f172a',
              marginBottom: '0.75rem',
            }}
          >
            Citation Verifier
          </h1>
          <p style={{ color: '#475569', fontSize: '1.05rem', lineHeight: 1.7 }}>
            Upload a legal brief or memo in PDF, DOCX, or TXT format. The verifier extracts the text,
            clusters short forms with their full citation, and surfaces verification details below.
          </p>
        </header>

        <form
          onSubmit={handleSubmit}
          style={{
            display: 'grid',
            gap: '2rem',
            padding: '2rem',
            border: '1px solid rgba(59, 130, 246, 0.18)',
            borderRadius: '18px',
            background: 'linear-gradient(135deg, rgba(59, 130, 246, 0.06) 0%, rgba(59, 130, 246, 0.02) 100%)',
          }}
        >
          <div>
            <label
              htmlFor="document"
              style={{
                display: 'block',
                fontWeight: 600,
                color: '#1e293b',
                marginBottom: '0.65rem',
                fontSize: '1.05rem',
              }}
            >
              Document upload
            </label>
            <input
              id="document"
              name="document"
              type="file"
              accept=".pdf,.docx,.txt"
              style={{
                width: '75%',
                maxWidth: '100%',
                padding: '0.85rem 1.1rem',
                border: '1px solid rgba(37, 99, 235, 0.35)',
                borderRadius: '14px',
                backgroundColor: 'rgba(59, 130, 246, 0.12)',
                fontSize: '0.95rem',
              }}
              disabled={isLoading}
            />
            <p style={{ fontSize: '0.9rem', color: '#64748b', marginTop: '0.65rem' }}>
              Maximum size 10MB. Supported formats: PDF, DOCX, TXT.
            </p>
          </div>

          <div style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
            <button
              type="submit"
              disabled={isLoading}
              style={{
                background: isLoading
                  ? 'linear-gradient(135deg, #94a3b8 0%, #cbd5f5 100%)'
                  : 'linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%)',
                color: '#ffffff',
                fontWeight: 600,
                padding: '0.95rem 2.2rem',
                borderRadius: '999px',
                border: 'none',
                cursor: isLoading ? 'not-allowed' : 'pointer',
                boxShadow: isLoading
                  ? 'none'
                  : '0 16px 40px rgba(37, 99, 235, 0.22)',
                transition: 'transform 0.2s ease, box-shadow 0.2s ease',
              }}
            >
              {isLoading ? 'Processing...' : 'Verify citations'}
            </button>
            {citationCount > 0 && (
              <span style={{ color: '#0f172a', fontWeight: 600, fontSize: '1.05rem' }}>
                {citationCount} grouped citation{citationCount === 1 ? '' : 's'}
              </span>
            )}
          </div>
        </form>

        {(error || successMessage) && (
          <div style={{ marginTop: '1.75rem', display: 'grid', gap: '1rem' }}>
            {error && (
              <div
                role="alert"
                style={{
                  backgroundColor: 'rgba(254, 202, 202, 0.4)',
                  border: '1px solid rgba(248, 113, 113, 0.6)',
                  color: '#7f1d1d',
                  borderRadius: '14px',
                  padding: '1rem 1.25rem',
                  fontWeight: 500,
                }}
              >
                {error}
              </div>
            )}
            {successMessage && !error && (
              <div
                role="status"
                style={{
                  backgroundColor: 'rgba(187, 247, 208, 0.75)',
                  border: '1px solid rgba(34, 197, 94, 0.5)',
                  color: '#14532d',
                  borderRadius: '14px',
                  padding: '1rem 1.25rem',
                  fontWeight: 500,
                }}
              >
                {successMessage}
              </div>
            )}
          </div>
        )}

        {citationCount > 0 && (
          <section style={{ marginTop: '3rem' }}>
            <header style={{ marginBottom: '1.5rem' }}>
              <h2 style={{ fontSize: '1.85rem', fontWeight: 700, color: '#0f172a', marginBottom: '0.6rem' }}>
                Compiled citations
              </h2>
              {citationSummary.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem' }}>
                  {citationSummary.map(({ label, value }) => (
                    <span
                      key={label}
                      style={{
                        backgroundColor: 'rgba(15, 23, 42, 0.05)',
                        color: '#1e293b',
                        borderRadius: '999px',
                        padding: '0.35rem 0.85rem',
                        fontSize: '0.85rem',
                        fontWeight: 500,
                        border: '1px solid rgba(148, 163, 184, 0.35)',
                      }}
                    >
                      {label}: {value}
                    </span>
                  ))}
                </div>
              )}
            </header>

            <div style={{ display: 'grid', gap: '1.75rem' }}>
              {citations.map((citation, index) => {
                const theme = getStatusTheme(citation.status);
                const formattedStatus = formatIdentifier(citation.status) ?? 'Unknown';
                const formattedSubstatus = formatIdentifier(citation.substatus);
                const displayCitation = getDisplayCitation(citation);
                const occurrences = sortOccurrences(citation.occurrences);

                return (
                  <article
                    key={citation.resource_key}
                    style={{
                      border: '1px solid rgba(15, 23, 42, 0.08)',
                      borderRadius: '18px',
                      padding: '1.8rem',
                      background: 'linear-gradient(145deg, rgba(248, 250, 252, 0.95) 0%, #ffffff 70%)',
                      boxShadow: '0 24px 60px rgba(15, 23, 42, 0.08)',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'flex-start',
                        gap: '1rem',
                        marginBottom: '1.4rem',
                      }}
                    >
                      <div style={{ display: 'flex', gap: '1.05rem', alignItems: 'flex-start' }}>
                        <span
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            width: '2.5rem',
                            height: '2.5rem',
                            borderRadius: '12px',
                            fontWeight: 700,
                            color: theme.indicator,
                            backgroundColor: 'rgba(37, 99, 235, 0.06)',
                            border: '1px solid rgba(148, 163, 184, 0.2)',
                            fontSize: '1rem',
                          }}
                        >
                          #{index + 1}
                        </span>
                        <div>
                          <h3
                            style={{
                              fontSize: '1.25rem',
                              fontWeight: 700,
                              color: '#111827',
                              marginBottom: '0.5rem',
                              lineHeight: 1.5,
                            }}
                          >
                            {displayCitation}
                          </h3>
                          <div
                            style={{
                              display: 'flex',
                              flexWrap: 'wrap',
                              gap: '0.5rem',
                              color: '#475569',
                              fontSize: '0.9rem',
                            }}
                          >
                            <span
                              style={{
                                backgroundColor: 'rgba(148, 163, 184, 0.15)',
                                borderRadius: '999px',
                                padding: '0.25rem 0.7rem',
                              }}
                            >
                              Type: {formatIdentifier(citation.type) ?? 'Unknown'}
                            </span>
                            {occurrences.length > 0 && (
                              <span
                                style={{
                                  backgroundColor: 'rgba(59, 130, 246, 0.14)',
                                  color: '#1d4ed8',
                                  borderRadius: '999px',
                                  padding: '0.25rem 0.7rem',
                                }}
                              >
                                {occurrences.length} occurrence{occurrences.length === 1 ? '' : 's'}
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                      <div
                        style={{
                          display: 'flex',
                          flexDirection: 'column',
                          alignItems: 'flex-end',
                          gap: '0.5rem',
                        }}
                      >
                        <span
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '0.4rem',
                            borderRadius: '999px',
                            padding: '0.4rem 0.9rem',
                            fontSize: '0.9rem',
                            fontWeight: 600,
                            color: theme.badgeText,
                            backgroundColor: theme.badgeBackground,
                            border: `1px solid ${theme.badgeBorder}`,
                          }}
                        >
                          <span
                            aria-hidden
                            style={{
                              display: 'inline-block',
                              width: '0.65rem',
                              height: '0.65rem',
                              borderRadius: '50%',
                              backgroundColor: theme.indicator,
                            }}
                          />
                          {formattedStatus}
                        </span>
                        {formattedSubstatus && (
                          <span
                            style={{
                              backgroundColor: theme.pillBackground,
                              color: theme.pillText,
                              borderRadius: '8px',
                              padding: '0.35rem 0.65rem',
                              fontSize: '0.82rem',
                              fontWeight: 500,
                            }}
                          >
                            {formattedSubstatus}
                          </span>
                        )}
                      </div>
                    </div>

                    {citation.status === 'warning' &&
                      citation.verification_details?.mismatched_fields &&
                      citation.verification_details.mismatched_fields.length > 0 && (
                        <div
                          style={{
                            border: '1px solid rgba(234, 179, 8, 0.3)',
                            backgroundColor: 'rgba(250, 204, 21, 0.18)',
                            borderRadius: '12px',
                            padding: '1rem 1.1rem',
                            marginBottom: '1.25rem',
                          }}
                        >
                          <p style={{ fontWeight: 600, marginBottom: '0.75rem', color: '#92400e' }}>
                            Verification discrepancy details
                          </p>
                          <div style={{ display: 'grid', gap: '0.85rem' }}>
                            {citation.verification_details.mismatched_fields.map((field) => {
                              const label = formatIdentifier(field) ?? field;
                              const key = field as 'case_name' | 'year';
                              const extractedValue = citation.verification_details?.extracted?.[key];
                              const courtValue = citation.verification_details?.court_listener?.[key];

                              return (
                                <div key={`${citation.resource_key}-${field}`} style={{ fontSize: '0.92rem', color: '#78350f' }}>
                                  <strong style={{ display: 'block', marginBottom: '0.35rem' }}>{label}</strong>
                                  <div>Document: {extractedValue ?? '—'}</div>
                                  <div>CourtListener: {courtValue ?? '—'}</div>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}

                    {occurrences.length > 0 ? (
                      <div style={{ display: 'grid', gap: '0.9rem' }}>
                        {occurrences.map((occurrence, occurrenceIndex) => (
                          <div
                            key={`${citation.resource_key}-${occurrenceIndex}`}
                            style={{
                              backgroundColor: 'rgba(248, 250, 252, 0.9)',
                              borderRadius: '12px',
                              border: '1px solid rgba(148, 163, 184, 0.25)',
                              padding: '1rem 1.15rem',
                              display: 'grid',
                              gap: '0.6rem',
                            }}
                          >
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                              <p style={{ margin: 0, color: '#1f2937', fontWeight: 600 }}>
                                Occurrence {occurrenceIndex + 1}{' '}
                                {occurrence.citation_category && `· ${formatIdentifier(occurrence.citation_category)}`}
                              </p>
                              <div style={{ display: 'flex', gap: '0.75rem', fontSize: '0.8rem', color: '#475569' }}>
                                {occurrence.pin_cite && <span>Pin cite: {occurrence.pin_cite}</span>}
                                {occurrence.span && (
                                  <span>
                                    Span: {occurrence.span[0]} – {occurrence.span[1]}
                                  </span>
                                )}
                              </div>
                            </div>
                            {occurrence.matched_text && (
                              <p style={{ margin: 0, color: '#111827', lineHeight: 1.6 }}>
                                {occurrence.matched_text}
                              </p>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p style={{ color: '#475569' }}>No occurrences found for this citation.</p>
                    )}
                  </article>
                );
              })}
            </div>
          </section>
        )}

        {extractedText && (
          <section style={{ marginTop: '3rem' }}>
            <div
              style={{
                borderRadius: '18px',
                background: '#0f172a',
                color: '#e2e8f0',
                padding: '1.5rem',
                boxShadow: '0 20px 60px rgba(15, 23, 42, 0.45)',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h3 style={{ margin: 0, fontSize: '1.2rem', fontWeight: 600 }}>Extracted document text</h3>
                <p style={{ margin: 0, fontSize: '0.85rem', color: 'rgba(226, 232, 240, 0.7)' }}>
                  Highlight colors align with citation status and numbering above.
                </p>
              </div>
              <div
                style={{
                  marginTop: '1.25rem',
                  backgroundColor: 'rgba(15, 23, 42, 0.6)',
                  borderRadius: '12px',
                  padding: '1rem 1.25rem',
                  maxHeight: '420px',
                  overflowY: 'auto',
                  border: '1px solid rgba(226, 232, 240, 0.2)',
                  fontFamily:
                    "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace",
                  fontSize: '0.9rem',
                  lineHeight: 1.6,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {highlightedExtractSegments.map((segment) => {
                  if (!segment.highlight) {
                    return <span key={segment.key}>{segment.content}</span>;
                  }

                  return (
                    <span
                      key={segment.key}
                      style={{
                        backgroundColor: segment.highlight.color,
                        borderRadius: '6px',
                        padding: '0 0.2rem',
                        margin: '0 -0.05rem',
                        position: 'relative',
                        display: 'inline-block',
                      }}
                    >
                      <span
                        style={{
                          position: 'absolute',
                          top: '-1rem',
                          right: '0',
                          fontSize: '0.65rem',
                          fontWeight: 700,
                          color: segment.highlight.indicatorColor,
                        }}
                      >
                        {segment.highlight.indicator}
                      </span>
                      {segment.content}
                    </span>
                  );
                })}
              </div>
            </div>
          </section>
        )}
      </div>
    </main>
  );
}
