// Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

'use client';

import { FormEvent, useMemo, useState } from 'react';

const DEFAULT_API_BASE_URL = 'http://localhost:8000';

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? DEFAULT_API_BASE_URL;

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
    const counts = citations.reduce(
      (acc, entry) => {
        const key = entry.type ?? 'unknown';
        acc[key] = (acc[key] ?? 0) + 1;
        return acc;
      },
      {} as Record<string, number>,
    );

    return Object.entries(counts)
      .map(([kind, total]) => `${kind}: ${total}`)
      .join(' \u2022 ');
  }, [citations]);

  return (
    <main style={{ minHeight: '100vh', backgroundColor: '#f8fafc', padding: '2rem' }}>
      <div
        style={{
          maxWidth: '960px',
          margin: '0 auto',
          backgroundColor: '#ffffff',
          borderRadius: '16px',
          padding: '2.5rem',
          boxShadow: '0 24px 48px rgba(15, 23, 42, 0.08)',
        }}
      >
        <header style={{ marginBottom: '2rem' }}>
          <h1
            style={{
              fontSize: '2.5rem',
              fontWeight: 700,
              color: '#1e293b',
              marginBottom: '0.75rem',
            }}
          >
            Citation Verifier
          </h1>
          <p style={{ color: '#475569', fontSize: '1.05rem', lineHeight: 1.6 }}>
            Upload a legal brief or memo in PDF, DOCX, or TXT format. We will extract the document
            text, compile the citations it contains, and present the results below.
          </p>
        </header>

        <form onSubmit={handleSubmit} style={{ display: 'grid', gap: '1.5rem' }}>
          <div>
            <label
              htmlFor="document"
              style={{ display: 'block', fontWeight: 600, color: '#1e293b', marginBottom: '0.5rem' }}
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
                padding: '0.75rem 1rem',
                border: '1px solid #cbd5f5',
                borderRadius: '12px',
                backgroundColor: '#f8fafc',
              }}
              disabled={isLoading}
            />
            <p style={{ fontSize: '0.9rem', color: '#64748b', marginTop: '0.5rem' }}>
              Maximum size 10MB. Supported formats: PDF, DOCX, TXT.
            </p>
          </div>

          <div style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
            <button
              type="submit"
              disabled={isLoading}
              style={{
                backgroundColor: isLoading ? '#94a3b8' : '#2563eb',
                color: '#ffffff',
                fontWeight: 600,
                padding: '0.9rem 1.75rem',
                borderRadius: '999px',
                border: 'none',
                cursor: isLoading ? 'not-allowed' : 'pointer',
                transition: 'background-color 0.2s ease',
              }}
            >
              {isLoading ? 'Processing...' : 'Verify citations'}
            </button>
            {citationCount > 0 && (
              <span style={{ color: '#0f172a', fontWeight: 600 }}>
                {citationCount} citation{citationCount === 1 ? '' : 's'} found
              </span>
            )}
          </div>
        </form>

        {(error || successMessage) && (
          <div style={{ marginTop: '1.5rem' }}>
            {error && (
              <div
                role="alert"
                style={{
                  backgroundColor: '#fee2e2',
                  border: '1px solid #fca5a5',
                  color: '#991b1b',
                  borderRadius: '12px',
                  padding: '1rem 1.25rem',
                }}
              >
                {error}
              </div>
            )}
            {successMessage && !error && (
              <div
                role="status"
                style={{
                  backgroundColor: '#dcfce7',
                  border: '1px solid #86efac',
                  color: '#166534',
                  borderRadius: '12px',
                  padding: '1rem 1.25rem',
                }}
              >
                {successMessage}
              </div>
            )}
          </div>
        )}

        {citationCount > 0 && (
          <section style={{ marginTop: '2.5rem' }}>
            <header style={{ marginBottom: '1rem' }}>
              <h2 style={{ fontSize: '1.75rem', fontWeight: 700, color: '#0f172a' }}>
                Compiled citations
              </h2>
              {citationSummary && (
                <p style={{ color: '#475569', fontSize: '0.95rem' }}>{citationSummary}</p>
              )}
            </header>
            <div style={{ display: 'grid', gap: '1.25rem' }}>
              {citations.map((citation) => (
                <article
                  key={citation.resource_key}
                  style={{
                    border: '1px solid #e2e8f0',
                    borderRadius: '14px',
                    padding: '1.5rem',
                    backgroundColor: '#f8fafc',
                  }}
                >
                  <div style={{ marginBottom: '0.75rem' }}>
                    <h3 style={{ fontSize: '1.2rem', fontWeight: 700, color: '#1e293b' }}>
                      {citation.normalized_citation ?? citation.resource_key}
                    </h3>
                    <p style={{ color: '#475569', fontSize: '0.95rem' }}>
                      Type: <strong>{citation.type}</strong> • Status: {citation.status}
                      {citation.substatus && (
                        <>
                          {' '}• Substatus: {citation.substatus}
                        </>
                      )}
                    </p>

                    {citation.status === 'warning' &&
                      citation.verification_details?.mismatched_fields &&
                      citation.verification_details.mismatched_fields.length > 0 && (
                        <div
                          style={{
                            backgroundColor: '#fff7ed',
                            border: '1px solid #fdba74',
                            color: '#9a3412',
                            borderRadius: '10px',
                            padding: '0.9rem 1rem',
                            marginTop: '0.75rem',
                          }}
                        >
                          <p style={{ fontWeight: 600, marginBottom: '0.5rem' }}>
                            Verification discrepancy details
                          </p>
                          <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: '0.5rem' }}>
                            {citation.verification_details.mismatched_fields.map((field) => {
                              const label = field === 'case_name' ? 'Case name' : field === 'year' ? 'Year' : field;
                              const key = field as 'case_name' | 'year';
                              const extractedValue = citation.verification_details?.extracted?.[key];
                              const courtValue = citation.verification_details?.court_listener?.[key];

                              return (
                                <li key={`${citation.resource_key}-${field}`}>
                                  <div style={{ fontSize: '0.9rem', lineHeight: 1.4 }}>
                                    <div>
                                      <strong>{label}</strong>
                                    </div>
                                    <div>
                                      Document: {extractedValue ?? '—'}
                                    </div>
                                    <div>
                                      CourtListener: {courtValue ?? '—'}
                                    </div>
                                  </div>
                                </li>
                              );
                            })}
                          </ul>
                        </div>
                      )}
                  </div>

                  {citation.occurrences.length > 0 ? (
                    <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: '0.75rem' }}>
                      {citation.occurrences.map((occurrence, index) => (
                        <li
                          key={`${citation.resource_key}-${index}`}
                          style={{
                            backgroundColor: '#ffffff',
                            borderRadius: '12px',
                            border: '1px solid #dbeafe',
                            padding: '1rem 1.25rem',
                          }}
                        >
                          <p style={{ marginBottom: '0.5rem', color: '#1e293b', fontWeight: 600 }}>
                            {occurrence.citation_category ?? 'Occurrence'}
                          </p>
                          {occurrence.matched_text && (
                            <p style={{ color: '#1f2937', lineHeight: 1.5 }}>
                              {occurrence.matched_text}
                            </p>
                          )}
                          <div style={{ color: '#64748b', fontSize: '0.85rem', marginTop: '0.5rem' }}>
                            {occurrence.pin_cite && <span>Pin cite: {occurrence.pin_cite}</span>}
                            {occurrence.span && (
                              <span style={{ marginLeft: occurrence.pin_cite ? '1rem' : undefined }}>
                                Span: {occurrence.span.join(' – ')}
                              </span>
                            )}
                          </div>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p style={{ color: '#475569' }}>No occurrences found for this citation.</p>
                  )}
                </article>
              ))}
            </div>
          </section>
        )}

        {extractedText && (
          <section style={{ marginTop: '2.5rem' }}>
            <details
              style={{
                backgroundColor: '#0f172a',
                color: '#e2e8f0',
                borderRadius: '14px',
                padding: '1.25rem 1.5rem',
              }}
            >
              <summary style={{ cursor: 'pointer', fontWeight: 600, fontSize: '1.05rem' }}>
                View extracted text
              </summary>
              <pre
                style={{
                  marginTop: '1rem',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  fontFamily: 'ui-monospace, SFMono-Regular, SFMono-Regular, Menlo, Monaco, Consolas, \'Liberation Mono\', \'Courier New\', monospace',
                  fontSize: '0.9rem',
                }}
              >
                {extractedText}
              </pre>
            </details>
          </section>
        )}
      </div>
    </main>
  );
}
