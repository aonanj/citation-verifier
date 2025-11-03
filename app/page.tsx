'use client';

import { FormEvent, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useAuth0 } from "@auth0/auth0-react";
import styles from './page.module.css';

const DEFAULT_API_BASE_URL = 'http://localhost:8000';
const API_BASE_URL = process.env.BACKEND_URL ?? DEFAULT_API_BASE_URL;

type PaymentPackage = {
  key: string;
  name: string;
  credits: number;
  amount_cents: number;
};

const formatCurrency = (amountCents: number): string => {
  return (amountCents / 100).toFixed(2);
};

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

function HomePageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [infoMessage, setInfoMessage] = useState<string | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [accountCredits, setAccountCredits] = useState<number | null>(null);
  const [packages, setPackages] = useState<PaymentPackage[]>([]);
  const [isLoadingPackages, setIsLoadingPackages] = useState(false);
  const [isCheckoutOpening, setIsCheckoutOpening] = useState(false);

  const { isAuthenticated, user: authUser, error: auth0Error, isLoading: auth0Loading, loginWithRedirect, logout, getAccessTokenSilently } = useAuth0();
  const displayName = authUser?.name ?? authUser?.email ?? null;
  useEffect(() => {
    if (!isAuthenticated) {
      setSelectedFile(null);
      setAccountCredits(null);
      setInfoMessage(null);
    }
  }, [isAuthenticated]);

  useEffect(() => {
    if (auth0Error) {
      setError(auth0Error.message ?? 'Authentication error. Please try again.');
    }
  }, [auth0Error]);

  const loadBalance = useCallback(async () => {
    if (!isAuthenticated) {
      setAccountCredits(null);
      return;
    }

    try {
      const token = await getAccessTokenSilently({
        authorizationParams: {
          audience: process.env.NEXT_PUBLIC_AUTH0_AUDIENCE,
        },
      });

      const response = await fetch(`${API_BASE_URL}/api/user/me`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });

      if (!response.ok) {
        throw new Error('Failed to fetch account balance.');
      }

      const data = (await response.json()) as { credits?: number | null };
      setAccountCredits(typeof data.credits === 'number' ? data.credits : 0);
    } catch (loadError) {
      console.error('Failed to load account balance', loadError);
      if (isAuthenticated) {
        setError((previous) => previous ?? 'Unable to load account balance. Please try again.');
      }
    }
  }, [API_BASE_URL, getAccessTokenSilently, isAuthenticated]);

  useEffect(() => {
    void loadBalance();
  }, [loadBalance]);

  useEffect(() => {
    let isMounted = true;

    const fetchPackages = async () => {
      setIsLoadingPackages(true);
      try {
        const response = await fetch(`${API_BASE_URL}/api/payments/packages`);
        if (!response.ok) {
          throw new Error('Failed to fetch payment packages.');
        }

        const data = (await response.json()) as PaymentPackage[];
        if (isMounted) {
          setPackages(data);
        }
      } catch (packagesError) {
        console.error('Failed to load payment packages', packagesError);
        if (isMounted) {
          setError((previous) => previous ?? 'Unable to load pricing options. Please refresh the page.');
        }
      } finally {
        if (isMounted) {
          setIsLoadingPackages(false);
        }
      }
    };

    void fetchPackages();

    return () => {
      isMounted = false;
    };
  }, [API_BASE_URL]);

  const checkoutStatus = searchParams?.get('checkout');

  useEffect(() => {
    if (checkoutStatus === 'success') {
      setInfoMessage('Payment successful. Credits will update shortly.');
      setError(null);
      void loadBalance();
    } else if (checkoutStatus === 'cancelled') {
      setInfoMessage(null);
      setError('Checkout was canceled. No charges were made.');
    }
  }, [checkoutStatus, loadBalance]);

  const handlePurchase = useCallback(
    async (packageKey: string) => {
      if (isCheckoutOpening) {
        return;
      }

      if (!isAuthenticated) {
        setError('Please sign in to purchase credits.');
        await loginWithRedirect();
        return;
      }

      setError(null);
      setInfoMessage(null);
      setIsCheckoutOpening(true);

      try {
        const token = await getAccessTokenSilently({
          authorizationParams: {
            audience: process.env.NEXT_PUBLIC_AUTH0_AUDIENCE,
          },
        });

        const response = await fetch(`${API_BASE_URL}/api/payments/checkout`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({ package_key: packageKey }),
        });

        if (!response.ok) {
          const detail = await response.json().catch(() => null);
          const message = detail?.detail ?? 'Unable to start Stripe checkout. Please try again.';
          throw new Error(message);
        }

        const data = (await response.json()) as { checkout_url?: string };
        if (!data.checkout_url) {
          throw new Error('Checkout URL was not returned by the server.');
        }

        window.location.href = data.checkout_url;
      } catch (purchaseError) {
        const message =
          purchaseError instanceof Error ? purchaseError.message : 'Unable to start checkout. Please try again.';
        setError(message);
        setIsCheckoutOpening(false);
      }
    },
    [API_BASE_URL, getAccessTokenSilently, isAuthenticated, isCheckoutOpening, loginWithRedirect],
  );

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

    if (!hasCreditsAvailable) {
      setError('Purchase credits to upload documents.');
      return;
    }

    if (!hasCreditsAvailable) {
      setError('Purchase credits to upload documents.');
      return;
    }

    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      setSelectedFile(e.dataTransfer.files[0]);
      setError(null);
      setInfoMessage(null);
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
      setInfoMessage(null);
    }
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setInfoMessage(null);

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

      const token = await getAccessTokenSilently({
        authorizationParams: {
          audience: process.env.NEXT_PUBLIC_AUTH0_AUDIENCE,
        },
      });

      const response = await fetch(`${API_BASE_URL}/api/verify`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${token}`,
        },
        body: formData,
      });

      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        if (response.status === 402) {
          setAccountCredits(0);
          const message =
            detail?.detail ?? 'Insufficient credits. Purchase document verifications to continue.';
          throw new Error(message);
        }

        const message = detail?.detail ?? 'Unable to process the document. Please try again.';
        throw new Error(message);
      }

      const payload = await response.json() as { remaining_credits?: number };

      if (typeof payload.remaining_credits === 'number') {
        setAccountCredits(payload.remaining_credits);
      } else {
        void loadBalance();
      }

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

  const dropzoneIconSrc = selectedFile ? '/images/doc_upload_fill.png' : '/images/doc_upload_empty.png';
  const dropzoneTitle = isAuthenticated
    ? selectedFile
      ? selectedFile.name.substring(0, selectedFile.name.lastIndexOf('.')) || selectedFile.name
      : 'Drag & drop a document'
    : 'Sign in to upload a document';
  const dropzoneSubtitle = isAuthenticated
    ? selectedFile
      ? `${selectedFile.name.substring(selectedFile.name.lastIndexOf('.') + 1)} / ${(selectedFile.size / 1024).toFixed(0)} KB`
      : accountCredits === null
        ? 'Loading account credits…'
        : accountCredits === 0
          ? 'No credits remaining. Purchase additional credits to continue.'
          : 'or click to browse locally'
    : 'Authentication is required before uploading';
  const hasCreditsAvailable = accountCredits === null || accountCredits > 0;
  const submitDisabled = isLoading || !selectedFile || !isAuthenticated || !hasCreditsAvailable;
  const balanceDisplay = !isAuthenticated ? '—' : accountCredits === null ? '…' : accountCredits;
  const balanceSubtitle = isAuthenticated
    ? accountCredits === 0
      ? 'No credits remaining'
      : 'Documents remaining'
    : 'Sign in to view your credits';

  const dropzoneClassName = useMemo(
    () =>
      [
        styles.dropzone,
        dragActive ? styles.dropzoneActive : '',
        isLoading || !isAuthenticated || !hasCreditsAvailable ? styles.dropzoneDisabled : '',
      ]
        .filter(Boolean)
        .join(' '),
    [dragActive, hasCreditsAvailable, isAuthenticated, isLoading],
  );

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

          {infoMessage && (
            <div className={styles.infoAlert} role="status">
              {infoMessage}
            </div>
          )}

          <div className={styles.balanceCard}>
            <div className={styles.balanceHeader}>
              <div className={styles.balanceCount}>
                <span className={styles.balanceTitle}>Account balance</span>
                <span className={styles.balanceCountValue}>{balanceDisplay}</span>
                <span className={styles.balanceCountLabel}>{balanceSubtitle}</span>
              </div>
            </div>

            <div className={styles.packages}>
              <h3 className={styles.packagesHeading}>Purchase credits</h3>
              {isLoadingPackages ? (
                <span className={styles.authStatus}>Loading packages…</span>
              ) : packages.length > 0 ? (
                <div className={styles.packagesGrid}>
                  {packages.map((pkg) => (
                    <div key={pkg.key} className={styles.packageCard}>
                      <p className={styles.packageTitle}>{pkg.name}</p>
                      <p className={styles.packagePrice}>${formatCurrency(pkg.amount_cents)}</p>
                      <p className={styles.packageCredits}>
                        {pkg.credits} {pkg.credits === 1 ? 'document credit' : 'document credits'}
                      </p>
                      <button
                        type="button"
                        className={styles.packageAction}
                        onClick={() => handlePurchase(pkg.key)}
                        disabled={isCheckoutOpening}
                      >
                        {isCheckoutOpening ? 'Redirecting…' : isAuthenticated ? 'Buy credits' : 'Log in to buy'}
                      </button>
                    </div>
                  ))}
                </div>
              ) : (
                <span className={styles.authStatus}>No purchase options are currently available.</span>
              )}
            </div>
          </div>

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
                disabled={isLoading || !isAuthenticated || !hasCreditsAvailable}
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

            {error && (
              <div className={styles.errorAlert} role="alert">
                {error}
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

export default function HomePage() {
  return (
    <Suspense fallback={null}>
      <HomePageContent />
    </Suspense>
  );
}
