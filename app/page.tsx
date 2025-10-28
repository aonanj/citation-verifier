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
              fontSize: '3.1rem',
              fontWeight: 700,
              color: '#e8eaed',
              marginBottom: '0.75rem',
              marginTop: 0,
              textAlign: 'center',
              textDecoration: 'underline',
            }}
          >
            VeriCite: Citation Verification Service
          </h1>
          <p
            style={{
              fontSize: '1.2rem',
              color: '#bdc1c6',
              marginBottom: '1.5rem',
              marginTop: 0,
              fontStyle: 'italic',
              textAlign: 'center',
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
              VeriCite is a full-stack toolchain to verify legal citations in briefs, memos, legal journal articles, law review notes, and other documents citing primarily to US state and federal case law and statutes, academic and professional journals and periodicals, and secondary legal sources (limited).
            </p>
            <p style={{ marginTop: 1 }}>
              VeriCite is specifically configured to work with both inline and footnote citations, including string citations, that follow the Bluebook format. Accordingly, reference citations, such as short case citations, <em>id.</em>, and <em>supra</em>, are likewise verified and grouped with their corresponding parent citations. Other notes regarding this service:
            </p>
            <ul style={{ marginTop: 1 }}>
              <li>Web sites, reference and text books, and other sources not listed above will not be verified.</li>
              <li><em>infra</em> signals are not recognized as citations. The service ignores them.</li>
              <li>Incorrect or unexpected citations will introduce offsets in the citation numbering when footnotes are used.</li>
              <li>Block quotes will not be verified. Block quotes can also cause corrupted outputs due to the disruption in page formatting.</li>
            </ul>
          </div>
        </div>

        {/* Upload Card */}
        <div
          style={{
            backgroundColor: '#3d4043',
            borderRadius: '16px',
            padding: '3rem 2.5rem',
            boxShadow: '0 4px 20px rgba(0, 0, 0, 0.3)',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <h2
            style={{
              fontSize: '1.5rem',
              fontWeight: 600,
              color: '#e8eaed',
              marginBottom: '1rem',
              marginTop: 0,
              textAlign: 'center',
            }}
          >
            Upload Document
          </h2>
          <p
            style={{
              fontSize: '1rem',
              color: '#bdc1c6',
              marginBottom: '2rem',
              textAlign: 'center',
            }}
          >
            The service supports documents in PDF, DOCX, and TXT formats. Documents cannot exceed 10 MB. Processing times can vary substantially depending on the number of citations and types of sources being verified.
          </p>

          <form
            onSubmit={handleSubmit}
            style={{
              width: '100%',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
            }}
          >
            <div
              style={{
                position: 'relative',
                marginBottom: '1.5rem',
                width: '100%',
                display: 'flex',
                justifyContent: 'center',
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
                  top: 0,
                  left: 0,
                  width: '100%',
                  height: '100%',
                  opacity: 0,
                  cursor: isLoading ? 'not-allowed' : 'pointer',
                  gap: '25px',
                  alignContent: 'center',
                  alignItems: 'center',
                  justifyContent: 'center',
                  zIndex: 2,
                }}
              />
              <div
                style={{
                  border: `2px dashed ${dragActive ? '#5f9ea0' : '#80868b'}`,
                  borderRadius: '20px',
                  maxWidth: '400px',
                  width: '100%',
                  background: '#333',
                  boxShadow: '0 18px 50px rgba(0, 0, 0, 0.5)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexDirection: 'column',
                  cursor: isLoading ? 'not-allowed' : 'pointer',
                  gap: '25px',
                  padding: '40px',
                  backgroundColor: dragActive ? 'rgba(95, 158, 160, 0.05)' : 'transparent',
                  transition: 'all 0.2s ease',
                }}
              >
                <img
                  src="/images/folder-upload-icon.svg"
                  alt="Upload Icon"
                  style={{
                    width: '80px',
                    height: '80px',
                    margin: '0 auto 0.5rem',
                    display: 'block',
                    filter: 'invert(100%)',
                    color: '#ffffff'
                  }}
                />
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
                    fontSize: '1rem',
                    color: '#ffffff',
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
                  width: '100%',
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
                marginTop: '1rem',
                alignSelf: 'center',
                width: '35%',
                background: isLoading || !selectedFile ? '#5f6368' : '#5FA8D2',
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
            <strong style={{ color: '#F15F5C' }}>Note:</strong> Please do not upload any confidential or privileged information to this web service. A Word Add-In is also available with the same functionality, but all sensitive data remains on the local device. Contact <a href="mailto:support@phaethon.llc" style={{ color: '#9BC7FF' }}>support@phaethon.llc</a> for access.
          </p>
        </div>
      </div>
    </main>
  );
}
