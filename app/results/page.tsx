'use client';

import { useEffect, useMemo, useState } from 'react';
import type { CSSProperties } from 'react';
import { useRouter } from 'next/navigation';
import { jsPDF } from 'jspdf';
import styles from './page.module.css';

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
    badgeBackground: 'rgba(34, 197, 94, 0.18)',
    badgeBorder: 'rgba(34, 197, 94, 0.45)',
    badgeText: '#5ef2c3',
    pillBackground: 'rgba(34, 197, 94, 0.22)',
    pillText: '#5ef2c3',
    highlight: 'rgba(34, 197, 94, 0.22)',
    indicator: '#34d399',
  },
  warning: {
    badgeBackground: 'rgba(250, 204, 21, 0.18)',
    badgeBorder: 'rgba(250, 204, 21, 0.45)',
    badgeText: '#fde68a',
    pillBackground: 'rgba(250, 204, 21, 0.2)',
    pillText: '#fde68a',
    highlight: 'rgba(250, 204, 21, 0.28)',
    indicator: '#f59e0b',
  },
  'no match': {
    badgeBackground: 'rgba(248, 113, 113, 0.18)',
    badgeBorder: 'rgba(248, 113, 113, 0.45)',
    badgeText: '#fda4af',
    pillBackground: 'rgba(248, 113, 113, 0.2)',
    pillText: '#fda4af',
    highlight: 'rgba(248, 113, 113, 0.28)',
    indicator: '#f87171',
  },
  no_match: {
    badgeBackground: 'rgba(248, 113, 113, 0.18)',
    badgeBorder: 'rgba(248, 113, 113, 0.45)',
    badgeText: '#fda4af',
    pillBackground: 'rgba(248, 113, 113, 0.2)',
    pillText: '#fda4af',
    highlight: 'rgba(248, 113, 113, 0.28)',
    indicator: '#f87171',
  },
  error: {
    badgeBackground: 'rgba(148, 163, 184, 0.18)',
    badgeBorder: 'rgba(148, 163, 184, 0.4)',
    badgeText: '#d1d8e8',
    pillBackground: 'rgba(148, 163, 184, 0.22)',
    pillText: '#d1d8e8',
    highlight: 'rgba(148, 163, 184, 0.28)',
    indicator: '#94a3b8',
  },
  unknown: {
    badgeBackground: 'rgba(74, 144, 226, 0.18)',
    badgeBorder: 'rgba(74, 144, 226, 0.45)',
    badgeText: '#9ecaff',
    pillBackground: 'rgba(74, 144, 226, 0.22)',
    pillText: '#9ecaff',
    highlight: 'rgba(76, 196, 255, 0.26)',
    indicator: '#60a5fa',
  },
};

const UNVERIFIED_STATUSES = new Set(['warning', 'no match', 'no_match', 'error']);

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
  const [activeTab, setActiveTab] = useState<'list' | 'document'>('list');
  const [isExporting, setIsExporting] = useState(false);
  const [showUnverifiedOnly, setShowUnverifiedOnly] = useState(false);

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
  const annotatedCitations = useMemo(
    () => citations.map((citation, index) => ({ citation, originalIndex: index })),
    [citations],
  );
  const displayedCitations = useMemo(() => {
    if (!showUnverifiedOnly) {
      return annotatedCitations;
    }
    return annotatedCitations.filter(({ citation }) => UNVERIFIED_STATUSES.has(normalizeKey(citation.status)));
  }, [annotatedCitations, showUnverifiedOnly]);
  const displayedCitationCount = displayedCitations.length;

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

  const verificationSummary = useMemo(() => {
    if (citations.length === 0) {
      return [] as Array<{ label: string; value: number }>;
    }

    const counts = citations.reduce(
      (acc, entry) => {
        const key = normalizeKey(entry.status);
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
    if (isExporting) {
      return;
    }
    if (citations.length === 0 && !extractedText) {
      return;
    }

    setIsExporting(true);

    try {
      const doc = new jsPDF({
        orientation: 'portrait',
        unit: 'pt',
        format: 'letter',
      });
      const margin = 56;
      const pageWidth = doc.internal.pageSize.getWidth();
      const pageHeight = doc.internal.pageSize.getHeight();
      const contentWidth = pageWidth - margin * 2;
      let cursorY = margin;

      const addPage = () => {
        doc.addPage();
        cursorY = margin;
      };

      const ensureSpace = (lineHeight = 0) => {
        if (cursorY + lineHeight > pageHeight - margin) {
          addPage();
        }
      };

      const writeText = (
        text: string | string[],
        {
          fontSize = 11,
          fontStyle = 'normal' as 'normal' | 'bold' | 'italic' | 'bolditalic',
          lineHeight = 16,
          indent = 0,
        } = {},
      ) => {
        if (!text) {
          return;
        }

        doc.setFont('helvetica', fontStyle);
        doc.setFontSize(fontSize);
        const availableWidth = Math.max(contentWidth - indent, 1);
        const lines = Array.isArray(text) ? text : doc.splitTextToSize(text, availableWidth);
        lines.forEach((line) => {
          ensureSpace(lineHeight);
          doc.text(line, margin + indent, cursorY);
          cursorY += lineHeight;
        });
      };

      const addSectionHeading = (heading: string) => {
        doc.setFont('helvetica', 'bold');
        doc.setFontSize(14);
        ensureSpace(22);
        doc.text(heading, margin, cursorY);
        cursorY += 22;
      };

      doc.setFont('helvetica', 'bold');
      doc.setFontSize(18);
      doc.text('VeriCite Verification Report', margin, cursorY);
      cursorY += 26;

      writeText(`Generated: ${new Date().toLocaleString()}`);

      if (citationCount > 0) {
        writeText(`Total citations analyzed: ${citationCount}`);
      }

      if (verificationSummary.length > 0 || citationSummary.length > 0) {
        addSectionHeading('Summary');
      }

      if (verificationSummary.length > 0) {
        doc.setFont('helvetica', 'bold');
        doc.setFontSize(12);
        ensureSpace(18);
        doc.text('Verification outcomes', margin, cursorY);
        cursorY += 18;
        verificationSummary.forEach(({ label, value }) => {
          writeText(`• ${label}: ${value}`, { indent: 12, lineHeight: 16 });
        });
      }

      if (citationSummary.length > 0) {
        ensureSpace(22);
        doc.setFont('helvetica', 'bold');
        doc.setFontSize(12);
        doc.text('Citation types', margin, cursorY);
        cursorY += 18;
        citationSummary.forEach(({ label, value }) => {
          writeText(`• ${label}: ${value}`, { indent: 12, lineHeight: 16 });
        });
      }

      if (citations.length > 0) {
        addSectionHeading('Citation details');
      }

      citations.forEach((citation, index) => {
        const formattedStatus = formatIdentifier(citation.status) ?? 'Unknown';
        const formattedSubstatus = formatIdentifier(citation.substatus);
        const displayCitation = getDisplayCitation(citation);
        const occurrences = sortOccurrences(citation.occurrences);
        const isNoMatch =
          normalizeKey(citation.status) === 'no match' || normalizeKey(citation.status) === 'no_match';
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
        const extractedCaseName = citation.verification_details?.extracted?.case_name ?? '—';
        const extractedYear = citation.verification_details?.extracted?.year ?? '—';
        const referenceCaseName = citation.verification_details?.court_listener?.case_name ?? '—';
        const referenceYear = citation.verification_details?.court_listener?.year ?? '—';

        ensureSpace(24);
        doc.setFont('helvetica', 'bold');
        doc.setFontSize(12);
        const citationHeading = doc.splitTextToSize(`${index + 1}. ${displayCitation}`, contentWidth);
        citationHeading.forEach((line) => {
          ensureSpace(18);
          doc.text(line, margin, cursorY);
          cursorY += 18;
        });

        writeText(
          `Status: ${formattedStatus}${formattedSubstatus ? ` — ${formattedSubstatus}` : ''}`,
          {
            lineHeight: 14,
          },
        );
        writeText(`Occurrences: ${occurrences.length}`, { lineHeight: 14 });

        if (isNoMatch) {
          writeText(`Reference key: ${citation.resource_key}`, { lineHeight: 14 });
        }

        if (formattedDetailSource || unverifiedFieldsDisplay || returnedEntries.length > 0) {
          writeText('Verification notes:', { fontStyle: 'bold', lineHeight: 16 });
          if (formattedDetailSource) {
            writeText(`Source: ${formattedDetailSource}`, { indent: 12, lineHeight: 14 });
          }
          if (unverifiedFieldsDisplay) {
            writeText(`Unverified fields: ${unverifiedFieldsDisplay}`, { indent: 12, lineHeight: 14 });
          }
          if (formattedDetailSource || unverifiedFieldsDisplay) {
            writeText(`Document case: ${extractedCaseName} (${extractedYear})`, {
              indent: 12,
              lineHeight: 14,
            });
            writeText(`Reference case: ${referenceCaseName} (${referenceYear})`, {
              indent: 12,
              lineHeight: 14,
            });
          }
          if (returnedEntries.length > 0) {
            returnedEntries.forEach(([key, value]) => {
              writeText(`${formatIdentifier(key) ?? key}: ${formatDetailValue(value)}`, {
                indent: 12,
                lineHeight: 14,
              });
            });
          }
        }

        if (occurrences.length > 0) {
          writeText('Occurrences:', { fontStyle: 'bold', lineHeight: 16 });
          occurrences.forEach((occurrence, occurrenceIndex) => {
            const occurrenceLabelParts = [`${occurrenceIndex + 1}`];
            if (occurrence.citation_category) {
              occurrenceLabelParts.push(formatIdentifier(occurrence.citation_category) ?? '');
            }
            writeText(`• Occurrence ${occurrenceLabelParts.filter(Boolean).join(' ')}`, {
              indent: 12,
              lineHeight: 14,
            });
            if (occurrence.matched_text) {
              writeText(occurrence.matched_text, { indent: 24, lineHeight: 14, fontSize: 10 });
            }
            if (occurrence.span && occurrence.span.length === 2) {
              writeText(`Span: ${occurrence.span[0]} – ${occurrence.span[1]}`, {
                indent: 24,
                lineHeight: 14,
                fontSize: 10,
              });
            }
          });
        }

        cursorY += 6;
      });

      if (extractedText) {
        addSectionHeading('Extracted document text');
        doc.setFont('helvetica', 'normal');
        doc.setFontSize(10);
        const lines = doc.splitTextToSize(extractedText, contentWidth);
        lines.forEach((line) => {
          ensureSpace(14);
          doc.text(line, margin, cursorY);
          cursorY += 14;
        });
      }

      const timestampSlug = new Date().toISOString().replace(/[:.]/g, '-');
      doc.save(`vericite-report-${timestampSlug}.pdf`);
    } catch (error) {
      console.error('Failed to generate PDF export', error);
    } finally {
      setIsExporting(false);
    }
  };

  const hasSummaries = citationSummary.length > 0 || verificationSummary.length > 0;
  const listTabClassName = [
    styles.tabButton,
    activeTab === 'list' ? styles.tabButtonActive : '',
  ]
    .filter(Boolean)
    .join(' ');
  const documentTabClassName = [
    styles.tabButton,
    activeTab === 'document' ? styles.tabButtonActive : '',
  ]
    .filter(Boolean)
    .join(' ');
  const filterToggleClassName = [styles.filterToggle, showUnverifiedOnly ? styles.filterToggleActive : '']
    .filter(Boolean)
    .join(' ');
  const tabRow = (
    <div className={styles.tabRow}>
      <button type="button" className={listTabClassName} onClick={() => setActiveTab('list')}>
        Citation Status List
      </button>
      <button type="button" className={documentTabClassName} onClick={() => setActiveTab('document')}>
        Highlighted Document
      </button>
    </div>
  );

  return (
    <div className={styles.page}>
      <nav className={styles.nav}>
        <div className={styles.navBrand}>
          <span className={styles.navIcon}>
            <img src="/images/scales-of-justice.png" alt="" />
          </span>
          <div className={styles.navText}>
            <span className={styles.navEyebrow}>Verification Results</span>
            <h1 className={styles.navTitle}>Document Insights</h1>
          </div>
        </div>
        <div className={styles.navActions}>
          <button
            type="button"
            className={`${styles.button} ${styles.buttonGhost}`}
            onClick={handleNewVerification}
          >
            New Verification
          </button>
          <button
            type="button"
            className={`${styles.button} ${styles.buttonPrimary}`}
            onClick={handleExportPDF}
            disabled={isExporting || (citations.length === 0 && !extractedText)}
          >
            {isExporting ? 'Preparing PDF...' : 'Export PDF'}
          </button>
        </div>
      </nav>

      <main className={styles.main}>
        <section className={styles.summaryRow}>
          <div className={styles.summaryLead}>
            <div className={styles.summaryLeadHeader}>
              <span className={styles.summaryLeadTitle}>Total Citations Analyzed</span>
              <span className={styles.summaryLeadCount}>{citationCount}</span>
            </div>
            {verificationSummary.length > 0 && (
              <div className={styles.summaryPills}>
                {verificationSummary.map(({ label, value }) => (
                  <span key={label} className={styles.summaryPill}>
                    {label}: {value}
                  </span>
                ))}
              </div>
            )}
          </div>

        </section>

        {hasSummaries && (
          <div className={styles.tablesRow}>
            {citationSummary.length > 0 && (
              <div className={styles.tableCard}>
                <div className={styles.splitHeader}>
                  <h2>Citation Breakdown</h2>
                  <span>Grouped by Citation Type</span>
                </div>
                <table className={styles.dataTable}>
                  <thead>
                    <tr>
                      <th>Type</th>
                      <th>Count</th>
                    </tr>
                  </thead>
                  <tbody>
                    {citationSummary.map(({ label, value }) => (
                      <tr key={label}>
                        <td>{label}</td>
                        <td>{value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {verificationSummary.length > 0 && (
              <div className={styles.tableCard}>
                <div className={styles.splitHeader}>
                  <h2>Verification Status</h2>
                  <span>Outcome Distribution</span>
                </div>
                <table className={styles.dataTable}>
                  <thead>
                    <tr>
                      <th>Status</th>
                      <th>Count</th>
                    </tr>
                  </thead>
                  <tbody>
                    {verificationSummary.map(({ label, value }) => (
                      <tr key={label}>
                        <td>{label}</td>
                        <td>{value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {citationCount > 0 ? (
          <>
            {activeTab === 'list' && (
              <section className={styles.citationsSection}>
                {tabRow}
                <div className={styles.listToolbar}>
                  <span className={styles.listToolbarStatus}>
                    {showUnverifiedOnly
                      ? `Showing ${displayedCitationCount} unverified citation${displayedCitationCount === 1 ? '' : 's'}`
                      : `Showing all ${citationCount} citation${citationCount === 1 ? '' : 's'}`}
                  </span>
                  <button
                    type="button"
                    className={filterToggleClassName}
                    onClick={() => setShowUnverifiedOnly((prev) => !prev)}
                  >
                    {showUnverifiedOnly ? 'Show all citations' : 'Show unverified only'}
                  </button>
                </div>

                {displayedCitationCount === 0 ? (
                  <div className={styles.filterEmptyState}>
                    <p className={styles.emptyState}>No unverified citations found.</p>
                    <button
                      type="button"
                      className={styles.filterToggle}
                      onClick={() => setShowUnverifiedOnly(false)}
                    >
                      Show all citations
                    </button>
                  </div>
                ) : (
                  displayedCitations.map(({ citation, originalIndex }) => {
                    const theme = getStatusTheme(citation.status);
                    const formattedStatus = formatIdentifier(citation.status) ?? 'Unknown';
                    const formattedSubstatus = formatIdentifier(citation.substatus);
                    const hasSubstatus = Boolean(formattedSubstatus);
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
                    const formattedLookupSource = detailSourceRaw
                      ? formatIdentifier(detailSourceRaw) ?? detailSourceRaw
                      : null;
                    const hasVerificationDetailContent =
                      Boolean(formattedLookupSource) || Boolean(unverifiedFieldsDisplay) || returnedEntries.length > 0;
                    const showUnverifiedDetailBlock = isUnverifiedDetailsWarning && hasVerificationDetailContent;
                    const cardStyle = {
                      '--status-badge-bg': theme.badgeBackground,
                      '--status-border': theme.badgeBorder,
                      '--status-text': theme.badgeText,
                      '--status-pill-bg': theme.pillBackground,
                      '--status-pill-text': theme.pillText,
                      '--status-indicator': theme.indicator,
                    } as CSSProperties;
                    const mismatchedFieldsRaw = citation.verification_details?.mismatched_fields ?? [];
                    const mismatchedFields = Array.isArray(mismatchedFieldsRaw) ? mismatchedFieldsRaw : [];
                    const extractedDetails = (citation.verification_details?.extracted ?? {}) as Record<string, unknown>;
                    const referenceDetails = (citation.verification_details?.court_listener ?? {}) as Record<
                      string,
                      unknown
                    >;
                    const mismatchDetails = mismatchedFields.map((field) => {
                      const label = formatIdentifier(field) ?? field;
                      const citationValueRaw = Object.prototype.hasOwnProperty.call(extractedDetails, field)
                        ? extractedDetails[field]
                        : null;
                      const lookupValueRaw = Object.prototype.hasOwnProperty.call(referenceDetails, field)
                        ? referenceDetails[field]
                        : null;
                      return {
                        field,
                        label,
                        citationValue: formatDetailValue(citationValueRaw ?? null),
                        lookupValue: formatDetailValue(lookupValueRaw ?? null),
                      };
                    });
                    const mismatchedFieldsDisplay =
                      mismatchDetails.length > 0 ? mismatchDetails.map(({ label }) => label).join(', ') : null;
                    const showMismatchDetails =
                      normalizeKey(citation.status) === 'warning' && mismatchDetails.length > 0;
                    const lookupResultSourceDisplay = formattedLookupSource ?? 'Unspecified source';
                    const cardNumber = originalIndex + 1;

                    return (
                      <article key={citation.resource_key} className={styles.citationCard} style={cardStyle}>
                        <header className={styles.citationHeader}>
                          <div className={styles.citationHeaderInfo}>
                            <span className={styles.citationIndex}>{cardNumber}. 
                            <h3 className={styles.citationTitle}>{displayCitation}</h3>
                            </span>
                          </div>
                          <div className={styles.statusGroup}>
                            <span className={styles.statusBadge}>{formattedStatus}</span>
                            {hasSubstatus && <span className={styles.statusPill}>{formattedSubstatus}</span>}
                          </div>
                        </header>

                        <div className={styles.citationMeta}>
                          <span>
                            <strong>Type:</strong> {formatIdentifier(citation.type) ?? 'Unknown'}
                          </span>
                          <span>
                            <strong>Occurrences:</strong> {occurrences.length}
                          </span>
                        </div>

                        {showMismatchDetails && (
                          <div className={styles.mismatchDetails}>
                            <div className={styles.mismatchDetailsHeader}>
                              <strong>Mismatched Fields</strong> {mismatchedFieldsDisplay}
                              <span>compared to lookup result from {lookupResultSourceDisplay}</span>
                            </div>
                            <div className={styles.mismatchGrid}>
                              {mismatchDetails.map(({ field, label, citationValue, lookupValue }) => (
                                <div key={field} className={styles.mismatchItem}>
                                  <div className={styles.mismatchLabel}>{label}</div>
                                  <div className={styles.mismatchValuePair}>
                                    <span className={styles.mismatchValueKey}>Citation Value</span>
                                    <span className={styles.mismatchValue}>{citationValue}</span>
                                  </div>
                                  <div className={styles.mismatchValuePair}>
                                    <span className={styles.mismatchValueKey}>Lookup Result</span>
                                    <span className={styles.mismatchValue}>{lookupValue}</span>
                                  </div>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}

                        {showUnverifiedDetailBlock && (
                          <div className={styles.mismatchDetails}>
                            <div className={styles.mismatchDetailsHeader}>
                              <strong>Unverified Fields</strong> {unverifiedFieldsDisplay}
                              <span>reported from {lookupResultSourceDisplay}</span>
                            </div>
                            <div className={styles.mismatchGrid}>
                              {returnedEntries.length > 0 && (
                                <div className={styles.mismatchItem}>
                                  <div className={styles.mismatchLabel}>Lookup Result</div>
                                  <div className={styles.unverifiedLookupValues}>
                                    {returnedEntries.map(([key, value]) => (
                                      <div key={key} className={styles.mismatchValuePair}>
                                        <span className={styles.mismatchValueKey}>{formatIdentifier(key) ?? key}</span>
                                        <span className={styles.mismatchValue}>{formatDetailValue(value)}</span>
                                      </div>
                                    ))}
                                  </div>
                                </div>
                              )}
                            </div>
                          </div>
                        )}

                        {occurrences.length > 0 && (
                          <ul className={styles.occurrenceList}>
                            {occurrences.map((occurrence, occurrenceIndex) => (
                              <li
                                key={`${citation.resource_key}-${occurrenceIndex}`}
                                className={styles.occurrenceItem}
                              >
                                <div className={styles.occurrenceHeader}>
                                  <strong>Occurrence {occurrenceIndex + 1}</strong>
                                  {occurrence.citation_category &&
                                    ` - ${formatIdentifier(occurrence.citation_category)}`}
                                </div>
                                {occurrence.matched_text && (
                                  <p className={styles.occurrenceText}>{occurrence.matched_text}</p>
                                )}
                                {occurrence.span && (
                                  <div className={styles.occurrenceSpan}>
                                    Span: {occurrence.span[0]} – {occurrence.span[1]}
                                  </div>
                                )}
                              </li>
                            ))}
                          </ul>
                        )}
                      </article>
                    );
                })
                )}
              </section>
            )}

            {activeTab === 'document' && (
              <section className={styles.documentSection}>
                {tabRow}
                {extractedText ? (
                  <div className={styles.documentScroll}>
                    {highlightedExtractSegments.map((segment) => {
                      if (!segment.highlight) {
                        return <span key={segment.key}>{segment.content}</span>;
                      }

                      const highlightStyle = {
                        '--highlight-bg': segment.highlight.color,
                        '--highlight-indicator': segment.highlight.indicatorColor,
                        '--highlight-text': segment.highlight.indicatorColor,
                      } as CSSProperties;

                      return (
                        <span
                          key={segment.key}
                          className={styles.highlightSpan}
                          data-indicator={segment.highlight.indicator}
                          style={highlightStyle}
                        >
                          {segment.content}
                        </span>
                      );
                    })}
                  </div>
                ) : (
                  <p className={styles.emptyState}>No document text available for this verification.</p>
                )}
              </section>
            )}
          </>
        ) : (
          <section className={styles.citationsSection}>
            <p className={styles.emptyState}>
              No verification results are stored in this session. Start a new verification to populate this view.
            </p>
            <div className={styles.navActions}>
              <button
                type="button"
                className={`${styles.button} ${styles.buttonPrimary}`}
                onClick={handleNewVerification}
              >
                Start a new verification
              </button>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
