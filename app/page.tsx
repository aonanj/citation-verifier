'use client';

import { ChangeEvent, FormEvent, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
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
  {
    title: 'Maryland Appellate Court Refers Attorney to Attorney Grievance Commission for AI Misuse (MD Courts)',
    href: 'https://www.mdcourts.gov/data/opinions/cosa/2025/0361s25.pdf',
  },
];

const FAQ_ITEMS = [
  {
    question: 'What is VeriCite?',
    answer:
      'VeriCite is a service to verify that legal citations exist and are accurate. In view of the increasing use of AI in law and the increasingly serve consequences levied against attorneys that file legal documents with AI hallucinations (see "AI Litigation Watch" above), VeriCite helps ensure that all citations are authentic and properly cited. VeriCite is also a perfect tool for attorney review work, law review editors, law school professors and TAs, and anyone else who needs to verify the accuracy of legal citations in any document.',
  },
  {
    question: 'What types of citations does VeriCite verify?',
    answer:
      'VeriCite validates citations to state and federal cases, state and federal laws, law reviews, academic and professional journals and periodicals, and secondary sources of law.',
  },
  {
    question: 'Can VeriCite catch AI-generated hallucinated authorities?',
    answer:
      'Yes. Every citation is scored against at least one reputable and authoritative source, such as Court Listener GovInfo.gov, Semantic Scholar, FindLaw, Justia, and OpenAlex. Multiple fields are independently checked for each citation to ensure verification results are accurate and comprehensive. Any missing or inaccurate fields are flagged with a warning, so even subtle hallucinations and incomplete citations are caught.',
  },
  {
    question: 'How quickly will my verification report be ready?',
    answer:
      'Most documents are processed in just a few minutes or less. Document length and the number and type of citations may increase processing time. State law citations may require a longer processing time due to the complexity of querying state law sources.',
  },
  {
    question: 'Is my document secure during the verification process?',
    answer:
      'Documents are encrypted for upload to VeriCite, and are deleted after processing. Documents can also be locally processed so only citation data is sent to VeriCite through our Microsoft Word Add-In -- please contact support@phaethon.llc for access.',
  },
  {
    question: 'Are verification results retained for later review?',
    answer:
      'In the interest of user privacy, VeriCite does not retain uploaded documents or verification results. Users are encouraged to save or download their verification reports immediately after processing completes (an export pdf option is provided on the results page).',
  },
  {
    question: 'What can I do if I am not satisfied with the verification results?',
    answer:
      'Export the verification results as a pdf. Send an email to support@phaethon.llc with the exported pdf and a description of the issue, including approximate date and time. Your issue will be reviewed and we will follow up with you to discuss next steps within three business days.',
  },
];

const PROGRESS_STEPS = [
  { threshold: 0, label: 'Preparing document for verification…' },
  { threshold: 20, label: 'Uploading document…' },
  { threshold: 45, label: 'Extracting citations…' },
  { threshold: 70, label: 'Verifying authorities…' },
  { threshold: 90, label: 'Preparing verification report…' },
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
  const [selectedPackageKey, setSelectedPackageKey] = useState<string | null>(null);
  const [isLoadingPackages, setIsLoadingPackages] = useState(false);
  const [isCheckoutOpening, setIsCheckoutOpening] = useState(false);
  const [progressPercent, setProgressPercent] = useState(0);
  const [isFaqExpanded, setIsFaqExpanded] = useState(false);

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
          setSelectedPackageKey((current) => {
            if (current && data.some((pkg) => pkg.key === current)) {
              return current;
            }
            return data.length > 0 ? data[0].key : null;
          });
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

  const toggleFaq = useCallback(() => {
    setIsFaqExpanded((previous) => !previous);
  }, []);

  const handlePackageChange = useCallback((event: ChangeEvent<HTMLSelectElement>) => {
    const { value } = event.target;
    setSelectedPackageKey(value || null);
    setInfoMessage(null);
    setError(null);
  }, []);

  const handlePurchase = useCallback(
    async () => {
      if (isCheckoutOpening) {
        return;
      }

      if (!selectedPackageKey) {
        setError('Please choose a verification package to purchase.');
        return;
      }

      const selection = packages.find((pkg) => pkg.key === selectedPackageKey);
      if (!selection) {
        setSelectedPackageKey(packages[0]?.key ?? null);
        setError('Selected package is unavailable. Please choose another option.');
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
          body: JSON.stringify({ package_key: selection.key }),
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
      } finally {
        setIsCheckoutOpening(false);
      }
    },
    [
      API_BASE_URL,
      getAccessTokenSilently,
      isAuthenticated,
      isCheckoutOpening,
      loginWithRedirect,
      packages,
      selectedPackageKey,
    ],
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
    setProgressPercent(10);

    try {
      const formData = new FormData();
      formData.append('document', selectedFile);
      setProgressPercent((current) => Math.max(current, 20));

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
      setProgressPercent((current) => Math.max(current, 60));

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
      setProgressPercent((current) => Math.max(current, 90));

      if (typeof payload.remaining_credits === 'number') {
        setAccountCredits(payload.remaining_credits);
      } else {
        void loadBalance();
      }

      // Store results in sessionStorage and navigate to results page
      sessionStorage.setItem('verificationResults', JSON.stringify(payload));
      setProgressPercent(100);
      router.push('/results');
    } catch (fetchError) {
      setProgressPercent(100);
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
  const balanceDisplay = !isAuthenticated ? 'Sign in to view' : accountCredits === null ? '0' : accountCredits;
  const balanceSubtitle = isAuthenticated
    ? accountCredits === null
      ? 'Loading credits…'
      : accountCredits === 0
        ? 'No credits remaining'
        : 'Credits available'
    : 'Sign in to view';

  const selectedPackage = useMemo(() => {
    if (!selectedPackageKey) {
      return null;
    }
    return packages.find((pkg) => pkg.key === selectedPackageKey) ?? null;
  }, [packages, selectedPackageKey]);

  const purchasePriceDisplay = selectedPackage ? `$${formatCurrency(selectedPackage.amount_cents)}` : '—';
  const purchaseButtonLabel = isCheckoutOpening
    ? 'Redirecting…'
    : isAuthenticated
      ? 'Purchase'
      : 'Log in to purchase';
  const isPurchaseDisabled = isCheckoutOpening || isLoadingPackages || !selectedPackage;

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

  const faqContentClassName = useMemo(
    () =>
      [styles.faqContent, !isFaqExpanded ? styles.faqContentCollapsed : ''].filter(Boolean).join(' '),
    [isFaqExpanded],
  );

  useEffect(() => {
    if (!isLoading) {
      setProgressPercent(0);
      return;
    }

    setProgressPercent((current) => (current > 0 ? current : 6));

    if (typeof window === 'undefined') {
      return;
    }

    const intervalId = window.setInterval(() => {
      setProgressPercent((current) => {
        if (current >= 96) {
          return current;
        }
        const increment = Math.random() * 5 + 2;
        return Math.min(current + increment, 96);
      });
    }, 1200);

    return () => {
      window.clearInterval(intervalId);
    };
  }, [isLoading]);

  const activeProgressStage = useMemo(() => {
    return PROGRESS_STEPS.reduce((active, step) => {
      return progressPercent >= step.threshold ? step : active;
    }, PROGRESS_STEPS[0]);
  }, [progressPercent]);

  return (
    <main className={styles.page}>
      {isLoading && (
        <div className={styles.verificationOverlay} role="presentation">
          <div className={styles.verificationDialog} role="status" aria-live="polite" aria-busy="true">
            <h2 className={styles.verificationTitle}>Verifying citations…</h2>
            <div className={styles.progressSection}>
              <div className={styles.progressTrack} aria-hidden="true">
                <div
                  className={styles.progressIndicator}
                  style={{ width: `${Math.min(100, Math.round(progressPercent))}%` }}
                />
              </div>
              <div className={styles.progressMeta}>
                <span className={styles.progressPercent}>{Math.min(100, Math.round(progressPercent))}%</span>
                <span className={styles.progressLabel}>{activeProgressStage.label}</span>
              </div>
            </div>
            <p className={styles.verificationNotice}>
              Please allow the verification process to complete. Navigating away from this page or closing your browser will cause your verification results to be lost.
            </p>
          </div>
        </div>
      )}
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
              academic and professional journals and periodicals, and secondary legal sources.
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

        <section className={styles.featureHighlights} aria-labelledby="feature-highlights-heading">
          <div className={styles.featureHighlightsHeader}>
            <h2 id="feature-highlights-heading" className={styles.sectionTitle}>
              Fact Check Citations Before Filing
            </h2>
            <p className={styles.sectionSubtitle}>
              Automate verification of citations in legal briefs and documents. Submit documents with confidence in their accuracy.
            </p>
          </div>
          <div className={styles.featureGrid}>
            <article className={styles.featureCard}>
              <h3 className={styles.featureCardTitle}>Bluebook-native citation checks</h3>
              <p className={styles.featureCardCopy}>
                Accurately identifies most citations in Bluebook format, including string citations and citations with introductory signals. Short-form citations are matched to their parent citations.
              </p>
            </article>
            <article className={styles.featureCard}>
              <h3 className={styles.featureCardTitle}>Comprehensive authority coverage</h3>
              <p className={styles.featureCardCopy}>
                Validates citations against authoritative databases and APIs, including Court Listener, GovInfo.gov, Semantic Scholar, FindLaw, and Justia.
              </p>
            </article>
            <article className={styles.featureCard}>
              <h3 className={styles.featureCardTitle}>Verification confirmations across multiple fields</h3>
              <p className={styles.featureCardCopy}>
                Citations are verified against multiple fields, such as case name, source (e.g., reporter, journal, etc.), volume, page, year, etc. Get warnings for missing or inaccurate fields.
              </p>
            </article>
            <article className={styles.featureCard}>
              <h3 className={styles.featureCardTitle}>Compatible with multiple formats</h3>
              <p className={styles.featureCardCopy}>
                Works with docx, pdf, and txt files, including pdf image files. Support for both inline and footnote citations.
              </p>
            </article>
          </div>
        </section>

        <section className={styles.workspaceColumns}>
          <article className={styles.uploadCard}>
            <h2 className={styles.uploadHeading}>Upload document</h2>
            <p className={styles.uploadDescription}>
              Upload a pdf, docx, or txt file (10 MB max). Verification results are displayed after processing completes. Processing time varies with document length and complexity.
            </p>

            {infoMessage && (
              <div className={styles.infoAlert} role="status">
                {infoMessage}
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
          </article>

          <aside className={styles.balanceCard} aria-label="Purchase verification reports">
            <h3 className={styles.balanceCardTitle}>Purchase credits</h3>
            <label className={styles.purchaseLabel} htmlFor="purchase-package">
              Purchase Verification Reports:
            </label>
            <select
              id="purchase-package"
              className={styles.purchaseSelect}
              value={selectedPackageKey ?? ''}
              onChange={handlePackageChange}
              disabled={isLoadingPackages || packages.length === 0}
            >
              {(!selectedPackageKey || packages.length === 0) && (
                <option value="" disabled>
                  {isLoadingPackages ? 'Loading packages…' : 'Select an option'}
                </option>
              )}
              {packages.map((pkg) => (
                <option key={pkg.key} value={pkg.key}>
                  {pkg.credits === 1 ? '1 document' : `${pkg.credits} documents`}
                </option>
              ))}
            </select>
            <div className={styles.purchasePrice}>
              Price: <span className={styles.purchasePriceValue}>{purchasePriceDisplay}</span>
            </div>
            <button
              type="button"
              className={styles.purchaseButton}
              onClick={handlePurchase}
              disabled={isPurchaseDisabled}
            >
              {purchaseButtonLabel}
            </button>
            {!isLoadingPackages && packages.length === 0 && (
              <span className={styles.purchaseEmpty}>No purchase options are currently available.</span>
            )}
            <br />
            <div className={styles.inlineBalanceCard}>
              <span className={styles.inlineBalanceLabel}>Verification Report Credits:</span>
              <span className={styles.inlineBalanceValue}>{balanceDisplay}</span>

              <p className={styles.creditDescription}>
                One credit = One verification report (citation verification for one document). Credits do not expire. 
              </p>
            </div>
          </aside>
        </section>

        <section className={styles.faqSection} id="faq" aria-labelledby="faq-heading">
          <h2 id="faq-heading" className={styles.sectionTitle}>
            <button
              type="button"
              className={styles.faqToggle}
              onClick={toggleFaq}
              aria-expanded={isFaqExpanded}
              aria-controls="faq-content"
            >
              <span className={styles.faqArrow} aria-hidden="true">{isFaqExpanded ? '▼' : '▶︎'}</span>
              <span className={styles.faqToggleLabel}>Frequently asked questions</span>
            </button>
          </h2>
          <div
            id="faq-content"
            className={faqContentClassName}
            aria-hidden={!isFaqExpanded}
          >
            <dl className={styles.faqList}>
              {FAQ_ITEMS.map((item) => (
                <div className={styles.faqItem} key={item.question}>
                  <dt className={styles.faqQuestion}>{item.question}</dt>
                  <dd className={styles.faqAnswer}>{item.answer}</dd>
                </div>
              ))}
            </dl>
          </div>
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
