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
    source?: string | null;
    unverified_fields?: string | string[] | null;
    returned_values?: Record<string, unknown> | null;
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
    badgeBackground: '#dcfce7',
    badgeBorder: '#86efac',
    badgeText: '#166534',
    pillBackground: '#dcfce7',
    pillText: '#166534',
    highlight: 'rgba(34, 197, 94, 0.25)',
    indicator: '#22c55e',
  },
  warning: {
    badgeBackground: '#fef3c7',
    badgeBorder: '#fcd34d',
    badgeText: '#92400e',
    pillBackground: '#fef3c7',
    pillText: '#92400e',
    highlight: 'rgba(250, 204, 21, 0.28)',
    indicator: '#f59e0b',
  },
  'no match': {
    badgeBackground: '#fee2e2',
    badgeBorder: '#fca5a5',
    badgeText: '#991b1b',
    pillBackground: '#fee2e2',
    pillText: '#991b1b',
    highlight: 'rgba(220, 38, 38, 0.25)',
    indicator: '#ef4444',
  },
  no_match: {
    badgeBackground: '#fee2e2',
    badgeBorder: '#fca5a5',
    badgeText: '#991b1b',
    pillBackground: '#fee2e2',
    pillText: '#991b1b',
    highlight: 'rgba(220, 38, 38, 0.25)',
    indicator: '#ef4444',
  },
  error: {
    badgeBackground: '#e2e8f0',
    badgeBorder: '#cbd5e1',
    badgeText: '#475569',
    pillBackground: '#e2e8f0',
    pillText: '#475569',
    highlight: 'rgba(148, 163, 184, 0.3)',
    indicator: '#64748b',
  },
  unknown: {
    badgeBackground: '#dbeafe',
    badgeBorder: '#93c5fd',
    badgeText: '#1e40af',
    pillBackground: '#dbeafe',
    pillText: '#1e40af',
    highlight: 'rgba(59, 130, 246, 0.25)',
    indicator: '#3b82f6',
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

const formatDetailValue = (input: unknown): string => {
  if (input === null || input === undefined) {
    return '—';
  }
  if (Array.isArray(input)) {
    return input.map((item) => String(item)).join(', ');
  }
  if (typeof input === 'object') {
    return JSON.stringify(input);
  }
  return String(input);
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
        background: 'linear-gradient(to bottom, #f8fafc 0%, #f1f5f9 100%)',
        padding: '2rem 1rem',
      }}
    >
      <div
        style={{
          maxWidth: '1200px',
          margin: '0 auto',
          backgroundColor: '#ffffff',
          borderRadius: '16px',
          padding: '2.5rem',
          boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)',
          border: '1px solid #e2e8f0',
        }}
      >
        <header style={{ marginBottom: '2rem' }}>
          <h1
            style={{
              fontSize: '2.25rem',
              fontWeight: 700,
              color: '#0f172a',
              marginBottom: '0.5rem',
              letterSpacing: '-0.025em',
            }}
          >
            Citation Verifier
          </h1>
          <p style={{ color: '#64748b', fontSize: '1rem', lineHeight: 1.6, maxWidth: '42rem' }}>
            Upload a legal brief or memo in PDF, DOCX, or TXT format. The verifier extracts the text,
            clusters short forms with their full citation, and surfaces verification details below.
          </p>
        </header>

        <form
          onSubmit={handleSubmit}
          style={{
            display: 'grid',
            gap: '1.5rem',
            padding: '1.5rem',
            border: '1px solid #e2e8f0',
            borderRadius: '12px',
            background: '#f8fafc',
          }}
        >
          <div>
            <label
              htmlFor="document"
              style={{
                display: 'block',
                fontWeight: 600,
                color: '#0f172a',
                marginBottom: '0.5rem',
                fontSize: '0.875rem',
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
                width: '100%',
                padding: '0.625rem 0.875rem',
                border: '1px solid #cbd5e1',
                borderRadius: '8px',
                backgroundColor: '#ffffff',
                fontSize: '0.875rem',
                cursor: isLoading ? 'not-allowed' : 'pointer',
              }}
              disabled={isLoading}
            />
            <p style={{ fontSize: '0.8125rem', color: '#64748b', marginTop: '0.5rem' }}>
              Maximum size 10MB. Supported formats: PDF, DOCX, TXT.
            </p>
          </div>

          <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center', flexWrap: 'wrap' }}>
            <button
              type="submit"
              disabled={isLoading}
              style={{
                background: isLoading ? '#94a3b8' : '#3b82f6',
                color: '#ffffff',
                fontWeight: 600,
                padding: '0.625rem 1.5rem',
                borderRadius: '8px',
                border: 'none',
                cursor: isLoading ? 'not-allowed' : 'pointer',
                fontSize: '0.875rem',
                transition: 'background-color 0.2s ease',
              }}
              onMouseOver={(e) => {
                if (!isLoading) e.currentTarget.style.background = '#2563eb';
              }}
              onMouseOut={(e) => {
                if (!isLoading) e.currentTarget.style.background = '#3b82f6';
              }}
            >
              {isLoading ? 'Processing...' : 'Verify citations'}
            </button>
            {citationCount > 0 && (
              <span style={{ color: '#475569', fontWeight: 500, fontSize: '0.875rem' }}>
                {citationCount} grouped citation{citationCount === 1 ? '' : 's'} found
              </span>
            )}
          </div>
        </form>

        {(error || successMessage) && (
          <div style={{ marginTop: '1.5rem', display: 'grid', gap: '0.75rem' }}>
            {error && (
              <div
                role="alert"
                style={{
                  backgroundColor: '#fee2e2',
                  border: '1px solid #fca5a5',
                  color: '#991b1b',
                  borderRadius: '8px',
                  padding: '0.75rem 1rem',
                  fontSize: '0.875rem',
                }}
              >
                {error}
              </div>
            )}
            {successMessage && !error && (
              <div
                role="status"
                style={{
                  backgroundColor: '#d1fae5',
                  border: '1px solid #6ee7b7',
                  color: '#065f46',
                  borderRadius: '8px',
                  padding: '0.75rem 1rem',
                  fontSize: '0.875rem',
                }}
              >
                {successMessage}
              </div>
            )}
          </div>
        )}

        {citationCount > 0 && (
          <section style={{ marginTop: '2.5rem' }}>
            <header style={{ marginBottom: '1.25rem' }}>
              <h2 style={{ fontSize: '1.5rem', fontWeight: 700, color: '#0f172a', marginBottom: '0.5rem', letterSpacing: '-0.025em' }}>
                Compiled citations
              </h2>
              {citationSummary.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem' }}>
                  {citationSummary.map(({ label, value }) => (
                    <span
                      key={label}
                      style={{
                        backgroundColor: '#f1f5f9',
                        color: '#475569',
                        borderRadius: '6px',
                        padding: '0.25rem 0.625rem',
                        fontSize: '0.8125rem',
                        fontWeight: 500,
                        border: '1px solid #e2e8f0',
                      }}
                    >
                      {label}: {value}
                    </span>
                  ))}
                </div>
              )}
            </header>

            <div style={{ display: 'grid', gap: '1.25rem' }}>
              {citations.map((citation, index) => {
                const theme = getStatusTheme(citation.status);
                const formattedStatus = formatIdentifier(citation.status) ?? 'Unknown';
                const formattedSubstatus = formatIdentifier(citation.substatus);
                const displayCitation = getDisplayCitation(citation);
                const occurrences = sortOccurrences(citation.occurrences);
                const isUnverifiedDetailsWarning =
                  normalizeKey(citation.status) === 'warning' &&
                  normalizeKey(citation.substatus) === 'unverified details';
                const unverifiedFields = citation.verification_details?.unverified_fields;
                const unverifiedFieldsDisplay = Array.isArray(unverifiedFields)
                  ? unverifiedFields.join(', ')
                  : unverifiedFields ?? null;
                const returnedValues = citation.verification_details?.returned_values;
                const returnedEntries =
                  returnedValues && typeof returnedValues === 'object'
                    ? Object.entries(returnedValues as Record<string, unknown>)
                    : [];
                const detailSourceRaw = citation.verification_details?.source ?? null;
                const formattedDetailSource = detailSourceRaw
                  ? formatIdentifier(detailSourceRaw) ?? detailSourceRaw
                  : null;
                const hasVerificationDetailContent =
                  Boolean(formattedDetailSource) || Boolean(unverifiedFieldsDisplay) || returnedEntries.length > 0;
                const showUnverifiedDetailBlock = isUnverifiedDetailsWarning && hasVerificationDetailContent;

                return (
                  <article
                    key={citation.resource_key}
                    style={{
                      border: '1px solid #e2e8f0',
                      borderRadius: '12px',
                      padding: '1.25rem',
                      background: '#ffffff',
                      boxShadow: '0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px 0 rgba(0, 0, 0, 0.06)',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'flex-start',
                        gap: '1rem',
                        marginBottom: '1rem',
                      }}
                    >
                      <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-start', flex: 1 }}>
                        <span
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            width: '2rem',
                            height: '2rem',
                            borderRadius: '6px',
                            fontWeight: 600,
                            color: theme.indicator,
                            backgroundColor: '#f8fafc',
                            border: '1px solid #e2e8f0',
                            fontSize: '0.875rem',
                            flexShrink: 0,
                          }}
                        >
                          #{index + 1}
                        </span>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <h3
                            style={{
                              fontSize: '1rem',
                              fontWeight: 600,
                              color: '#0f172a',
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
                              gap: '0.375rem',
                              fontSize: '0.8125rem',
                            }}
                          >
                            <span
                              style={{
                                backgroundColor: '#f1f5f9',
                                color: '#64748b',
                                borderRadius: '6px',
                                padding: '0.125rem 0.5rem',
                              }}
                            >
                              Type: {formatIdentifier(citation.type) ?? 'Unknown'}
                            </span>
                            {occurrences.length > 0 && (
                              <span
                                style={{
                                  backgroundColor: '#dbeafe',
                                  color: '#1e40af',
                                  borderRadius: '6px',
                                  padding: '0.125rem 0.5rem',
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
                          gap: '0.375rem',
                          flexShrink: 0,
                        }}
                      >
                        <span
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '0.375rem',
                            borderRadius: '6px',
                            padding: '0.375rem 0.75rem',
                            fontSize: '0.8125rem',
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
                              width: '0.5rem',
                              height: '0.5rem',
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
                              borderRadius: '6px',
                              padding: '0.25rem 0.5rem',
                              fontSize: '0.75rem',
                              fontWeight: 500,
                            }}
                          >
                            {formattedSubstatus}
                          </span>
                        )}
                      </div>
                    </div>

                    {showUnverifiedDetailBlock && (
                      <div
                        style={{
                          border: '1px solid #fcd34d',
                          backgroundColor: '#fef3c7',
                          borderRadius: '8px',
                          padding: '0.875rem',
                          marginBottom: '1rem',
                        }}
                      >
                        <p style={{ fontWeight: 600, marginBottom: '0.625rem', color: '#92400e', fontSize: '0.8125rem' }}>
                          Verification details
                        </p>
                        <div style={{ display: 'grid', gap: '0.5rem', fontSize: '0.8125rem', color: '#78350f' }}>
                          {formattedDetailSource && (
                            <div>
                              <strong style={{ display: 'block', marginBottom: '0.25rem', fontSize: '0.75rem' }}>Source</strong>
                              <div>{formattedDetailSource}</div>
                            </div>
                          )}
                          {unverifiedFieldsDisplay && (
                            <div>
                              <strong style={{ display: 'block', marginBottom: '0.25rem', fontSize: '0.75rem' }}>Unverified fields</strong>
                              <div>{unverifiedFieldsDisplay}</div>
                            </div>
                          )}
                          {returnedEntries.length > 0 && (
                            <div>
                              <strong style={{ display: 'block', marginBottom: '0.25rem', fontSize: '0.75rem' }}>Returned values</strong>
                              <div style={{ display: 'grid', gap: '0.25rem' }}>
                                {returnedEntries.map(([field, value]) => (
                                  <div key={`${citation.resource_key}-returned-${field}`}>
                                    <strong>{formatIdentifier(field) ?? field}</strong>: {formatDetailValue(value)}
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      </div>
                    )}

                    {citation.status === 'warning' &&
                      citation.verification_details?.mismatched_fields &&
                      citation.verification_details.mismatched_fields.length > 0 && (
                        <div
                          style={{
                            border: '1px solid #fcd34d',
                            backgroundColor: '#fef3c7',
                            borderRadius: '8px',
                            padding: '0.875rem',
                            marginBottom: '1rem',
                          }}
                        >
                          <p style={{ fontWeight: 600, marginBottom: '0.625rem', color: '#92400e', fontSize: '0.8125rem' }}>
                            Verification discrepancy details
                          </p>
                          <div style={{ display: 'grid', gap: '0.625rem' }}>
                            {citation.verification_details.mismatched_fields.map((field) => {
                              const label = formatIdentifier(field) ?? field;
                              const key = field as 'case_name' | 'year';
                              const extractedValue = citation.verification_details?.extracted?.[key];
                              const courtValue = citation.verification_details?.court_listener?.[key];

                              return (
                                <div key={`${citation.resource_key}-${field}`} style={{ fontSize: '0.8125rem', color: '#78350f' }}>
                                  <strong style={{ display: 'block', marginBottom: '0.25rem', fontSize: '0.75rem' }}>{label}</strong>
                                  <div>Document: {extractedValue ?? '—'}</div>
                                  <div>CourtListener: {courtValue ?? '—'}</div>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}

                    {occurrences.length > 0 ? (
                      <div style={{ display: 'grid', gap: '0.625rem' }}>
                        {occurrences.map((occurrence, occurrenceIndex) => (
                          <div
                            key={`${citation.resource_key}-${occurrenceIndex}`}
                            style={{
                              backgroundColor: '#f8fafc',
                              borderRadius: '8px',
                              border: '1px solid #e2e8f0',
                              padding: '0.75rem',
                              display: 'grid',
                              gap: '0.5rem',
                            }}
                          >
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
                              <p style={{ margin: 0, color: '#475569', fontWeight: 600, fontSize: '0.8125rem' }}>
                                Occurrence {occurrenceIndex + 1}{' '}
                                {occurrence.citation_category && `· ${formatIdentifier(occurrence.citation_category)}`}
                              </p>
                              <div style={{ display: 'flex', gap: '0.625rem', fontSize: '0.75rem', color: '#64748b' }}>
                                {occurrence.pin_cite && <span>Pin cite: {occurrence.pin_cite}</span>}
                                {occurrence.span && (
                                  <span>
                                    Span: {occurrence.span[0]} – {occurrence.span[1]}
                                  </span>
                                )}
                              </div>
                            </div>
                            {occurrence.matched_text && (
                              <p style={{ margin: 0, color: '#0f172a', lineHeight: 1.6, fontSize: '0.875rem' }}>
                                {occurrence.matched_text}
                              </p>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p style={{ color: '#64748b', fontSize: '0.875rem' }}>No occurrences found for this citation.</p>
                    )}
                  </article>
                );
              })}
            </div>
          </section>
        )}

        {extractedText && (
          <section style={{ marginTop: '2.5rem' }}>
            <div
              style={{
                borderRadius: '12px',
                background: '#1e293b',
                color: '#e2e8f0',
                padding: '1.25rem',
                boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '1rem', flexWrap: 'wrap' }}>
                <h3 style={{ margin: 0, fontSize: '1rem', fontWeight: 600, letterSpacing: '-0.025em' }}>Extracted document text</h3>
                <p style={{ margin: 0, fontSize: '0.75rem', color: '#cbd5e1' }}>
                  Highlight colors align with citation status and numbering above.
                </p>
              </div>
              <div
                style={{
                  marginTop: '1rem',
                  backgroundColor: '#0f172a',
                  borderRadius: '8px',
                  padding: '0.875rem',
                  maxHeight: '420px',
                  overflowY: 'auto',
                  border: '1px solid #334155',
                  fontFamily:
                    "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace",
                  fontSize: '0.8125rem',
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
                        borderRadius: '4px',
                        padding: '0.125rem 0.25rem',
                        position: 'relative',
                        display: 'inline-block',
                      }}
                    >
                      <span
                        style={{
                          position: 'absolute',
                          top: '-0.875rem',
                          right: '0',
                          fontSize: '0.625rem',
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
