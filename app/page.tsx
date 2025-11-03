'use client';

import { FormEvent, useEffect, useMemo, useState, StrictMode} from 'react';
import { useRouter } from 'next/navigation';
import { useAuth0, Auth0Provider } from "@auth0/auth0-react";
import styles from './page.module.css';

const DEFAULT_API_BASE_URL = 'http://localhost:8000';
const API_BASE_URL = process.env.BACKEND_URL ?? DEFAULT_API_BASE_URL;

const NEWS_ITEMS = [
  {
    title: 'California judge fines attorney as AI regulation debate escalates (CalMatters)',
    href: 'https://calmatters.org/economy/technology/2025/09/chatgpt-lawyer-fine-ai-regulation/',
  },
  {
    title: 'Massachusetts lawyer sanctioned for AI-generated fictitious cases (MSBA)',
    href: 'https://www.msba.org/site/site/content/News-and-Publications/News/General-News/Massachusetts_Lawyer-Sanctioned_for_AI_Generated-Fictitious_Cases.aspx',
  },
  {
    title: 'Federal court steps up scrutiny of ChatGPT research in filings (Esquire Solutions)',
    href: 'https://www.esquiresolutions.com/federal-court-turns-up-the-heat-on-attorneys-using-chatgpt-for-research/',
  },
  {
    title: 'Judge disqualifies Butler Snow attorneys over AI citations (Reuters)',
    href: 'https://www.reuters.com/legal/government/judge-disqualifies-three-butler-snow-attorneys-case-over-ai-citations-2025-07-24/',
  },
  {
    title: 'Judges cite AI hallucinations in growing sanctions inquiries (NatLawReview)',
    href: 'https://natlawreview.com/article/more-sanctions-inquiries-against-lawyers-judges-cite-hallucinations',
  },
];

export default function HomePage() {
  const router = useRouter();
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  const { isAuthenticated, user: authUser, error: auth0Error, isLoading: auth0Loading, loginWithRedirect, logout, getAccessTokenSilently } = useAuth0();
  const displayName = authUser?.name ?? authUser?.email ?? null;
  useEffect(() => {
    if (!isAuthenticated) {
      setSelectedFile(null);
    }
  }, [isAuthenticated]);

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

    if (!isAuthenticated) {
      setError('Please sign in to upload documents.');
      return;
    }

    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      setSelectedFile(e.dataTransfer.files[0]);
      setError(null);
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!isAuthenticated) {
      setError('Please sign in to upload documents.');
      return;
    }

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

    if (!isAuthenticated) {
      setError('Please sign in to verify citations.');
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
        isLoading || !isAuthenticated ? styles.dropzoneDisabled : '',
      ]
        .filter(Boolean)
        .join(' '),
    [dragActive, isAuthenticated, isLoading],
  );

  const dropzoneIconSrc = selectedFile ? '/images/doc_upload_fill.png' : '/images/doc_upload_empty.png';
  const dropzoneTitle = isAuthenticated
    ? selectedFile
      ? selectedFile.name.substring(0, selectedFile.name.lastIndexOf('.')) || selectedFile.name
      : 'Drag & drop your document'
    : 'Sign in to upload your document';
  const dropzoneSubtitle = isAuthenticated
    ? selectedFile
      ? `${selectedFile.name.substring(selectedFile.name.lastIndexOf('.') + 1)} / ${(selectedFile.size / 1024).toFixed(0)} KB`
      : 'or click to browse locally'
    : 'Authentication is required before uploading';
  const submitDisabled = isLoading || !selectedFile || !isAuthenticated;

  return (
    <main className={styles.page}>
      <div className={styles.content}>

        <section className={styles.newsTicker} aria-label="Attorney AI news">
          <span className={styles.newsTickerLabel}>AI litigation watch</span>
          <div className={styles.newsTickerViewport}>
            <ul className={styles.newsTickerTrack}>
              {[...NEWS_ITEMS, ...NEWS_ITEMS].map((item, index) => (
                <li className={styles.newsTickerItem} key={`${item.href}-${index}`}>
                  <a href={item.href} target="_blank" rel="noopener noreferrer">
                    {item.title}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        </section>

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
              This web service works with both inline and footnote citations that follow
              the Bluebook format, including string citations and citations preceded by introductory signals. Reference citations, such as short case citations, <em>id.</em>, and <em>supra</em>,
              are matched and grouped with their parent citations.
            </p>
          </div>
          <div className={styles.authActions}>
            {auth0Loading ? (
              <span className={styles.authStatus}>Checking session…</span>
            ) : isAuthenticated ? (
              <a className={`${styles.authButton} ${styles.authButtonGhost}`} onClick={() => logout({ logoutParams: { returnTo: typeof window !== "undefined" ? window.location.origin : undefined } })}>
                Log out
              </a>
            ) : (
              <a className={`${styles.authButton} ${styles.authButtonPrimary}`} onClick={() => loginWithRedirect()}>
                Log in
              </a>
            )}
          </div>
        </section>

        <section className={styles.uploadCard}>
          <h2 className={styles.uploadHeading}>Upload document</h2>
          <p className={styles.uploadDescription}>
            Upload a PDF, DOCX, or TXT document up to 10 MB. Processing time varies with document length and citation
            complexity, and results appear instantly once verification is complete.
          </p>

          {!isAuthenticated && !auth0Loading && (
            <div className={styles.authNotice} role="note">
              <h3 className={styles.authNoticeTitle}>Sign in required</h3>
              <p className={styles.authNoticeDescription}>
                You need to log in before we can verify citations. Auth0 keeps your session secure and lets you revisit
                past results quickly.
              </p>
            </div>
          )}

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
                disabled={isLoading || !isAuthenticated}
              />
              <div className={dropzoneClassName}>
                <img className={styles.dropzoneIcon} src={dropzoneIconSrc} alt="Document upload status" />
                <p className={styles.dropzoneTitle}>
                  {dropzoneTitle}
                </p>
                <p className={styles.dropzoneSubtitle}>
                  {dropzoneSubtitle}
                </p>
              </div>
            </div>

            {(error || auth0Error) && (
              <div className={styles.errorAlert} role="alert">
                {error ?? auth0Error?.message ?? 'Authentication error. Please try again.'}
              </div>
            )}

            <button className={styles.submitButton} type="submit" disabled={submitDisabled}>
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
