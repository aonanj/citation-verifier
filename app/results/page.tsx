'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';

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
    badgeBackground: '#1e5f3f',
    badgeBorder: '#2e8b57',
    badgeText: '#81c995',
    pillBackground: '#1e5f3f',
    pillText: '#81c995',
    highlight: 'rgba(34, 197, 94, 0.25)',
    indicator: '#22c55e',
  },
  warning: {
    badgeBackground: '#805b20',
    badgeBorder: '#b8860b',
    badgeText: '#ffd966',
    pillBackground: '#805b20',
    pillText: '#ffd966',
    highlight: 'rgba(250, 204, 21, 0.28)',
    indicator: '#f59e0b',
  },
  'no match': {
    badgeBackground: '#7a2e2e',
    badgeBorder: '#a94442',
    badgeText: '#f28b82',
    pillBackground: '#7a2e2e',
    pillText: '#f28b82',
    highlight: 'rgba(220, 38, 38, 0.25)',
    indicator: '#ef4444',
  },
  no_match: {
    badgeBackground: '#7a2e2e',
    badgeBorder: '#a94442',
    badgeText: '#f28b82',
    pillBackground: '#7a2e2e',
    pillText: '#f28b82',
    highlight: 'rgba(220, 38, 38, 0.25)',
    indicator: '#ef4444',
  },
  error: {
    badgeBackground: '#4a4a4a',
    badgeBorder: '#6a6a6a',
    badgeText: '#bdc1c6',
    pillBackground: '#4a4a4a',
    pillText: '#bdc1c6',
    highlight: 'rgba(148, 163, 184, 0.3)',
    indicator: '#9aa0a6',
  },
  unknown: {
    badgeBackground: '#2b4f7d',
    badgeBorder: '#4a90e2',
    badgeText: '#8ab4f8',
    pillBackground: '#2b4f7d',
    pillText: '#8ab4f8',
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
  value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

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

export default function ResultsPage() {
  const router = useRouter();
  const [citations, setCitations] = useState<CitationEntry[]>([]);
  const [extractedText, setExtractedText] = useState<string | null>(null);

  useEffect(() => {
    const resultsData = sessionStorage.getItem('verificationResults');
    if (!resultsData) {
      router.push('/');
      return;
    }

    const payload = JSON.parse(resultsData) as VerificationResponse;
    setCitations(payload.citations ?? []);
    setExtractedText(payload.extracted_text ?? null);
  }, [router]);

  const citationCount = citations.length;

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

  const handleNewVerification = () => {
    sessionStorage.removeItem('verificationResults');
    router.push('/');
  };

  const handleExportPDF = () => {
    // TODO: Implement PDF export
    alert('PDF export functionality will be implemented soon.');
  };

  return (
    <div style={{ minHeight: '100vh', background: 'linear-gradient(180deg, #0a2540 0%, #0d3a5f 100%)' }}>
      {/* Navbar */}
      <nav
        style={{
          backgroundColor: '#3d4043',
          padding: '1rem 2rem',
          boxShadow: '0 2px 10px rgba(0, 0, 0, 0.3)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <h1 style={{ margin: 0, fontSize: '1.5rem', fontWeight: 700, color: '#e8eaed' }}>
          VeriCite
        </h1>
        <div style={{ display: 'flex', gap: '1rem' }}>
          <button
            onClick={handleExportPDF}
            style={{
              background: '#5f6368',
              color: '#ffffff',
              fontWeight: 600,
              fontSize: '0.875rem',
              padding: '0.5rem 1rem',
              borderRadius: '6px',
              border: 'none',
              cursor: 'pointer',
              transition: 'background-color 0.2s ease',
            }}
            onMouseOver={(e) => {
              e.currentTarget.style.background = '#4d5256';
            }}
            onMouseOut={(e) => {
              e.currentTarget.style.background = '#5f6368';
            }}
          >
            Export PDF
          </button>
          <button
            onClick={handleNewVerification}
            style={{
              background: '#5f9ea0',
              color: '#ffffff',
              fontWeight: 600,
              fontSize: '0.875rem',
              padding: '0.5rem 1rem',
              borderRadius: '6px',
              border: 'none',
              cursor: 'pointer',
              transition: 'background-color 0.2s ease',
            }}
            onMouseOver={(e) => {
              e.currentTarget.style.background = '#4d8588';
            }}
            onMouseOut={(e) => {
              e.currentTarget.style.background = '#5f9ea0';
            }}
          >
            New Verification
          </button>
        </div>
      </nav>

      {/* Main Content */}
      <main style={{ padding: '2rem 1rem' }}>
        <div style={{ maxWidth: '1200px', margin: '0 auto' }}>
          {citationCount > 0 && (
            <section style={{ marginBottom: '2.5rem' }}>
              <header style={{ marginBottom: '1.25rem' }}>
                <h2
                  style={{
                    fontSize: '1.5rem',
                    fontWeight: 700,
                    color: '#e8eaed',
                    marginBottom: '0.75rem',
                    marginTop: 0,
                  }}
                >
                  Compiled Citations
                </h2>
                {citationSummary.length > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem' }}>
                    {citationSummary.map(({ label, value }) => (
                      <span
                        key={label}
                        style={{
                          backgroundColor: '#4a4a4a',
                          color: '#bdc1c6',
                          borderRadius: '6px',
                          padding: '0.25rem 0.625rem',
                          fontSize: '0.8125rem',
                          fontWeight: 500,
                          border: '1px solid #5f6368',
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
                        backgroundColor: '#3d4043',
                        border: '1px solid #5f6368',
                        borderRadius: '12px',
                        padding: '1.25rem',
                        boxShadow: '0 2px 8px rgba(0, 0, 0, 0.3)',
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
                              backgroundColor: '#2a2a2a',
                              border: '1px solid #5f6368',
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
                                color: '#e8eaed',
                                marginBottom: '0.5rem',
                                lineHeight: 1.5,
                                marginTop: 0,
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
                                  backgroundColor: '#4a4a4a',
                                  color: '#bdc1c6',
                                  borderRadius: '6px',
                                  padding: '0.125rem 0.5rem',
                                }}
                              >
                                Type: {formatIdentifier(citation.type) ?? 'Unknown'}
                              </span>
                              {occurrences.length > 0 && (
                                <span
                                  style={{
                                    backgroundColor: '#2b4f7d',
                                    color: '#8ab4f8',
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
                            border: '1px solid #b8860b',
                            backgroundColor: '#805b20',
                            borderRadius: '8px',
                            padding: '0.875rem',
                            marginBottom: '1rem',
                          }}
                        >
                          <p style={{ fontWeight: 600, marginBottom: '0.625rem', color: '#ffd966', fontSize: '0.8125rem', marginTop: 0 }}>
                            Verification details
                          </p>
                          <div style={{ display: 'grid', gap: '0.5rem', fontSize: '0.8125rem', color: '#f4e4c1' }}>
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
                              border: '1px solid #b8860b',
                              backgroundColor: '#805b20',
                              borderRadius: '8px',
                              padding: '0.875rem',
                              marginBottom: '1rem',
                            }}
                          >
                            <p style={{ fontWeight: 600, marginBottom: '0.625rem', color: '#ffd966', fontSize: '0.8125rem', marginTop: 0 }}>
                              Verification discrepancy details
                            </p>
                            <div style={{ display: 'grid', gap: '0.625rem' }}>
                              {citation.verification_details.mismatched_fields.map((field) => {
                                const label = formatIdentifier(field) ?? field;
                                const key = field as 'case_name' | 'year';
                                const extractedValue = citation.verification_details?.extracted?.[key];
                                const courtValue = citation.verification_details?.court_listener?.[key];

                                return (
                                  <div key={`${citation.resource_key}-${field}`} style={{ fontSize: '0.8125rem', color: '#f4e4c1' }}>
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
                                backgroundColor: '#2a2a2a',
                                borderRadius: '8px',
                                border: '1px solid #5f6368',
                                padding: '0.75rem',
                                display: 'grid',
                                gap: '0.5rem',
                              }}
                            >
                              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
                                <p style={{ margin: 0, color: '#bdc1c6', fontWeight: 600, fontSize: '0.8125rem' }}>
                                  Occurrence {occurrenceIndex + 1}{' '}
                                  {occurrence.citation_category && `· ${formatIdentifier(occurrence.citation_category)}`}
                                </p>
                                <div style={{ display: 'flex', gap: '0.625rem', fontSize: '0.75rem', color: '#9aa0a6' }}>
                                  {occurrence.pin_cite && <span>Pin cite: {occurrence.pin_cite}</span>}
                                  {occurrence.span && (
                                    <span>
                                      Span: {occurrence.span[0]} – {occurrence.span[1]}
                                    </span>
                                  )}
                                </div>
                              </div>
                              {occurrence.matched_text && (
                                <p style={{ margin: 0, color: '#e8eaed', lineHeight: 1.6, fontSize: '0.875rem' }}>
                                  {occurrence.matched_text}
                                </p>
                              )}
                            </div>
                          ))}
                        </div>
                      ) : (
                        <p style={{ color: '#9aa0a6', fontSize: '0.875rem', margin: 0 }}>No occurrences found for this citation.</p>
                      )}
                    </article>
                  );
                })}
              </div>
            </section>
          )}

          {extractedText && (
            <section style={{ marginBottom: '2rem' }}>
              <div
                style={{
                  borderRadius: '12px',
                  background: '#3d4043',
                  color: '#e2e8f0',
                  padding: '1.25rem',
                  boxShadow: '0 2px 8px rgba(0, 0, 0, 0.3)',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '1rem', flexWrap: 'wrap' }}>
                  <h3 style={{ margin: 0, fontSize: '1rem', fontWeight: 600, color: '#e8eaed' }}>Extracted document text</h3>
                  <p style={{ margin: 0, fontSize: '0.75rem', color: '#9aa0a6' }}>
                    Highlight colors align with citation status and numbering above.
                  </p>
                </div>
                <div
                  style={{
                    marginTop: '1rem',
                    backgroundColor: '#1a1a1a',
                    borderRadius: '8px',
                    padding: '0.875rem',
                    maxHeight: '420px',
                    overflowY: 'auto',
                    border: '1px solid #5f6368',
                    fontFamily:
                      "'Inter', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace",
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
    </div>
  );
}
