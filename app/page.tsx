'use client';

import { FormEvent, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import styles from './page.module.css';

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

  const dropzoneClassName = useMemo(
    () =>
      [
        styles.dropzone,
        dragActive ? styles.dropzoneActive : '',
        isLoading ? styles.dropzoneDisabled : '',
      ]
        .filter(Boolean)
        .join(' '),
    [dragActive, isLoading],
  );

  const dropzoneIconSrc = selectedFile ? '/images/doc_upload_fill.png' : '/images/doc_upload_empty.png';

  return (
    <main className={styles.page}>
      <div className={styles.content}>
        <section className={styles.heroCard}>
          <div className={styles.heroHeader}>
            <span className={styles.heroMark}>
              <img src="/images/scales-of-justice.png" alt="VeriCite crest" />
            </span>
            <div>
              <span className={styles.heroEyebrow}>Bluebook-native verification</span>
              <h1 className={styles.heroTitle}>
                VeriCite
                <span className={styles.heroAccent}>Citation confidence for legal documents.</span>
              </h1>
              <p className={styles.heroSubtitle}>
                Citation verification web service for briefs, memos, and other court filings, legal documents, and journal articles.
              </p>
            </div>
          </div>

          <div className={styles.heroBody}>
            <p>
              VeriCite is a full-stack toolchain to verify legal citations in briefs, memos, legal journal articles,
              law review notes, and other documents citing primarily to US state and federal case law and statutes,
              academic and professional journals and periodicals, and secondary legal sources (limited).
            </p>
            <p>
              The platform understands inline and footnote citations (including string citations) that follow
              the Bluebook format. Reference citations, such as short case citations, <em>id.</em>, and <em>supra</em>,
              are matched and grouped with their parent citations.
            </p>
          </div>
        </section>

        <section className={styles.uploadCard}>
          <h2 className={styles.uploadHeading}>Upload document</h2>
          <p className={styles.uploadDescription}>
            Upload a PDF, DOCX, or TXT document up to 10 MB. Processing time varies with document length and citation
            complexity, and results appear instantly once verification is complete.
          </p>

          <form className={styles.form} onSubmit={handleSubmit}>
            <div
              className={styles.dropzoneWrapper}
              onDragEnter={handleDrag}
              onDragLeave={handleDrag}
              onDragOver={handleDrag}
              onDrop={handleDrop}
            >
              <input
                id="document"
                name="document"
                className={styles.dropzoneInput}
                type="file"
                accept=".pdf,.docx,.txt"
                onChange={handleFileChange}
                disabled={isLoading}
              />
              <div className={dropzoneClassName}>
                <img className={styles.dropzoneIcon} src={dropzoneIconSrc} alt="Document upload status" />
                <p className={styles.dropzoneTitle}>
                  {selectedFile ? selectedFile.name.substring(0, selectedFile.name.lastIndexOf('.')) : 'Drag & drop your document'}
                </p>
                <p className={styles.dropzoneSubtitle}>
                  {selectedFile ? selectedFile.name.substring(selectedFile.name.lastIndexOf('.') + 1) + ' / ' + (selectedFile.size / 1024 / 1024).toFixed(0) + ' MB' : 'or click to browse locally'}
                </p>
              </div>
            </div>

            {error && (
              <div className={styles.errorAlert} role="alert">
                {error}
              </div>
            )}

            <button className={styles.submitButton} type="submit" disabled={isLoading || !selectedFile}>
              {isLoading ? 'Processing...' : 'Verify citations'}
            </button>
          </form>
        </section>

        <section className={styles.noticeCard}>
          <p>Note that not all citation formats are recognized or verified.</p>
          <ul className={styles.noticeList}>
            <li className={styles.noticeListItem}>Web sites, textbooks, and unsupported sources are ignored.</li>
            <li className={styles.noticeListItem}>
              <em>infra</em> signals are omitted from verification.
            </li>
            <li className={styles.noticeListItem}>Irregular formats can offset automatic footnote numbering.</li>
            <li className={styles.noticeListItem}>Block quotes are not supported.</li>
          </ul>
          <p>
            <strong>Important:</strong> Please avoid uploading confidential or privileged information. A Microsoft Word
            add-in with identical functionality keeps data on-device. Contact{' '}
            <a href="mailto:support@phaethon.llc">support@phaethon.llc</a> for access.
          </p>
        </section>
      </div>
    </main>
  );
}
