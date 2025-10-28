'use client';

import { FormEvent, useState } from 'react';
import { useRouter } from 'next/navigation';

const DEFAULT_API_BASE_URL = 'http://localhost:8000';
const API_BASE_URL = process.env.BACKEND_URL ?? DEFAULT_API_BASE_URL;

export default function HomePage() {
  const router = useRouter();
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  const handleDrag = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true);
    } else if (e.type === 'dragleave') {
      setDragActive(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);

    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      setSelectedFile(e.dataTransfer.files[0]);
      setError(null);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setSelectedFile(e.target.files[0]);
      setError(null);
    }
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);

    if (!selectedFile) {
      setError('Please choose a PDF, DOCX, or TXT document to upload.');
      return;
    }

    setIsLoading(true);

    try {
      const formData = new FormData();
      formData.append('document', selectedFile);

      const response = await fetch(`${API_BASE_URL}/api/verify`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        const message = detail?.detail ?? 'Unable to process the document. Please try again.';
        throw new Error(message);
      }

      const payload = await response.json();

      // Store results in sessionStorage and navigate to results page
      sessionStorage.setItem('verificationResults', JSON.stringify(payload));
      router.push('/results');
    } catch (fetchError) {
      const message = fetchError instanceof Error ? fetchError.message : 'Something went wrong.';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <main
      style={{
        minHeight: '100vh',
        background: 'linear-gradient(180deg, #0a2540 0%, #0d3a5f 100%)',
        padding: '2rem 1rem',
      }}
    >
      <div
        style={{
          maxWidth: '1000px',
          margin: '0 auto',
          display: 'flex',
          flexDirection: 'column',
          gap: '2rem',
        }}
      >
        {/* Title Card */}
        <div
          style={{
            backgroundColor: '#3d4043',
            borderRadius: '16px',
            padding: '3rem 2.5rem',
            boxShadow: '0 4px 20px rgba(0, 0, 0, 0.3)',
          }}
        >
          <h1
            style={{
              fontSize: '3rem',
              fontWeight: 700,
              color: '#e8eaed',
              marginBottom: '0.75rem',
              marginTop: 0,
            }}
          >
            VeriCite
          </h1>
          <p
            style={{
              fontSize: '1.25rem',
              color: '#bdc1c6',
              marginBottom: '1.5rem',
              marginTop: 0,
              fontStyle: 'italic',
            }}
          >
            Citation verification web service for briefs, memos, and other court filings and legal documents.
          </p>
          <div
            style={{
              fontSize: '0.95rem',
              color: '#c4c7c5',
              lineHeight: 1.7,
            }}
          >
            <p style={{ marginTop: 0 }}>
              VeriCite is a full-stack toolchain to verify legal citations in briefs, memos, legal journal articles, law review notes, and other documents citing primarily to case law, statutes, secondary legal sources, and most academic journals and periodicals.
            </p>
            <p>
              VeriCite is specifically configured to work with both inline and footnote citations, including string citations, following the Bluebook format. VeriCite is configured to verify full citations to (1) federal and state laws, (2) federal and state cases, (3) academic and legal journals, and (4) secondary legal sources (limited).
            </p>
            <p style={{ marginBottom: 0 }}>
              Reference citations, such as short case citations, <em>id.</em>, and <em>supra</em>, will be verified and grouped with their corresponding parent citations (note: <em>infra</em> is not recognized). Other sources, such as URLs and reference and text books, are not able to be verified at this time. Citations can include signals and parentheticals; however, block quotes will cause errors.
            </p>
          </div>
        </div>

        {/* Upload Card */}
        <div
          style={{
            backgroundColor: '#3d4043',
            borderRadius: '16px',
            padding: '3rem 2.5rem',
            boxShadow: '0 4px 20px rgba(0, 0, 0, 0.3)',
          }}
        >
          <h2
            style={{
              fontSize: '2rem',
              fontWeight: 600,
              color: '#e8eaed',
              marginBottom: '1rem',
              marginTop: 0,
              textAlign: 'center',
            }}
          >
            Document Upload
          </h2>
          <p
            style={{
              fontSize: '1rem',
              color: '#bdc1c6',
              marginBottom: '2rem',
              textAlign: 'center',
            }}
          >
            Upload a technology transactions document (pdf, txt, or docx only; max 10MB)
          </p>

          <form onSubmit={handleSubmit}>
            <div
              style={{
                position: 'relative',
                marginBottom: '1.5rem',
              }}
              onDragEnter={handleDrag}
              onDragLeave={handleDrag}
              onDragOver={handleDrag}
              onDrop={handleDrop}
            >
              <input
                id="document"
                name="document"
                type="file"
                accept=".pdf,.docx,.txt"
                onChange={handleFileChange}
                disabled={isLoading}
                style={{
                  position: 'absolute',
                  width: '100%',
                  height: '100%',
                  opacity: 0,
                  cursor: isLoading ? 'not-allowed' : 'pointer',
                  zIndex: 2,
                }}
              />
              <div
                style={{
                  border: `2px dashed ${dragActive ? '#5f9ea0' : '#80868b'}`,
                  borderRadius: '12px',
                  padding: '3rem 2rem',
                  textAlign: 'center',
                  backgroundColor: dragActive ? 'rgba(95, 158, 160, 0.05)' : 'transparent',
                  transition: 'all 0.2s ease',
                }}
              >
                <svg
                  style={{
                    width: '80px',
                    height: '80px',
                    margin: '0 auto 1.5rem',
                    display: 'block',
                  }}
                  fill="none"
                  stroke="#9aa0a6"
                  strokeWidth="2"
                  viewBox="0 0 24 24"
                  xmlns="http://www.w3.org/2000/svg"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"
                  />
                </svg>
                <p
                  style={{
                    fontSize: '1.125rem',
                    color: '#e8eaed',
                    marginBottom: '0.5rem',
                    fontWeight: 500,
                  }}
                >
                  {selectedFile ? selectedFile.name : 'Drag and drop file here'}
                </p>
                <p
                  style={{
                    fontSize: '0.875rem',
                    color: '#9aa0a6',
                    margin: 0,
                  }}
                >
                  or click to browse
                </p>
              </div>
            </div>

            {error && (
              <div
                role="alert"
                style={{
                  backgroundColor: 'rgba(242, 139, 130, 0.15)',
                  border: '1px solid rgba(242, 139, 130, 0.4)',
                  color: '#f28b82',
                  borderRadius: '8px',
                  padding: '0.75rem 1rem',
                  fontSize: '0.875rem',
                  marginBottom: '1.5rem',
                }}
              >
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={isLoading || !selectedFile}
              style={{
                width: '100%',
                background: isLoading || !selectedFile ? '#5f6368' : '#5f9ea0',
                color: '#ffffff',
                fontWeight: 600,
                fontSize: '1rem',
                padding: '1rem 2rem',
                borderRadius: '8px',
                border: 'none',
                cursor: isLoading || !selectedFile ? 'not-allowed' : 'pointer',
                transition: 'background-color 0.2s ease',
              }}
              onMouseOver={(e) => {
                if (!isLoading && selectedFile) {
                  e.currentTarget.style.background = '#4d8588';
                }
              }}
              onMouseOut={(e) => {
                if (!isLoading && selectedFile) {
                  e.currentTarget.style.background = '#5f9ea0';
                }
              }}
            >
              {isLoading ? 'Processing...' : 'Verify citations'}
            </button>
          </form>
        </div>

        {/* Important Notice */}
        <div
          style={{
            backgroundColor: '#3d4043',
            borderRadius: '16px',
            padding: '1.5rem 2rem',
            boxShadow: '0 4px 20px rgba(0, 0, 0, 0.3)',
          }}
        >
          <p
            style={{
              fontSize: '0.875rem',
              color: '#bdc1c6',
              margin: 0,
              lineHeight: 1.6,
              fontStyle: 'italic',
            }}
          >
            <strong style={{ color: '#f28b82' }}>IMPORTANT:</strong> Documents uploaded to this platform are publicly accessible. Do NOT upload any confidential information. This includes PDF documents with redactions. This service uses frontier-level AI models for plain text conversion, and redacted content can be inadvertently extracted. Plain text files are recommended for this reason.
          </p>
        </div>
      </div>
    </main>
  );
}
