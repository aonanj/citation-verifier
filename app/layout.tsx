// Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

import type { Metadata } from 'next';
import { ReactNode } from 'react';
import './globals.css';
import { Providers } from './providers';

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? 'https://www.jurischeck.com';
const normalizedSiteUrl = siteUrl.startsWith('http') ? siteUrl : `https://${siteUrl}`;
const metadataBase = new URL(normalizedSiteUrl);
const ogImageUrl = new URL('/images/CitationVerifierLogo.png', metadataBase).href;
const currentYear = new Date().getFullYear();
const webApplicationLd = {
  '@type': 'WebApplication',
  name: 'JurisCheck Citation Verification',
  url: normalizedSiteUrl,
  description:
    'JurisCheck is a Bluebook-native legal citation verification service that catches AI-generated hallucinations and other inaccuracies in legal citations.',
  applicationCategory: 'BusinessApplication',
  operatingSystem: 'Web',
  inLanguage: 'en-US',
  publisher: {
    '@type': 'Organization',
    name: 'Phaethon Order LLC',
  },
  audience: {
    '@type': 'Audience',
    audienceType: ['Attorneys', 'Law firms', 'Legal scholars'],
  },
  featureList: [
    'Automated verification of Bluebook citations and string cites',
    'Clustering of short-form references with full citations',
    'Instant verification reports with confidence indicators',
  ],
  image: ogImageUrl,
  potentialAction: {
    '@type': 'Action',
    name: 'Verify legal citations',
    target: normalizedSiteUrl,
  },
};

const faqEntriesLd = [
  {
    name: 'What is JurisCheck?',
    text:
      'JurisCheck is a service to verify that legal citations exist and are accurate. In view of the increasing use of AI in law and the increasingly serve consequences levied against attorneys that file legal documents with AI hallucinations (see "AI Litigation Watch" above), JurisCheck helps ensure that all citations are authentic and properly cited. JurisCheck is also a perfect tool for attorney review work, law review editors, law school professors and TAs, and anyone else who needs to verify the accuracy of legal citations in any document.',
  },
  {
    name: 'What types of citations does JurisCheck verify?',
    text:
      'JurisCheck validates citations to state and federal cases, state and federal laws, law reviews, academic and professional journals and periodicals, and secondary sources of law.',
  },
  {
    name: 'Can JurisCheck catch AI-generated hallucinated authorities?',
    text:
      'Yes. Every citation is scored against at least one reputable and authoritative source, such as Court Listener GovInfo.gov, Semantic Scholar, FindLaw, Justia, and OpenAlex. Multiple fields are independently checked for each citation to ensure verification results are accurate and comprehensive. Any missing or inaccurate fields are flagged with a warning, so even subtle hallucinations and incomplete citations are caught.',
  },
  {
    name: 'What makes JurisCheck better than other citation verification tools, such as CiteSure.com?',
    text:
      'JurisCheck allows you to upload an entire document and verify all citations in one go, rather than having to manually check each citation individually. Furthermore, JurisCheck verifies multiple fields for each citation, such as case name, title, author, reporter or journal, and year, and provides details on any mismatched fields, rather than just a pass/fail result.',
  },
  {
    name: 'How quickly will my verification report be ready?',
    text:
      'Most documents are processed in just a few minutes or less. Document length and the number and type of citations may increase processing time. State law citations may require a longer processing time due to the complexity of querying state law sources.',
  },
  {
    name: 'Is my document secure during the verification process?',
    text:
      'Documents are encrypted for upload to JurisCheck, and are deleted after processing. Documents can also be locally processed so only citation data is sent to JurisCheck through our Microsoft Word Add-In -- please contact support@phaethon.llc for access.',
  },
  {
    name: 'Are verification results retained for later review?',
    text:
      'In the interest of user privacy, JurisCheck does not retain uploaded documents or verification results. Users are encouraged to save or download their verification reports immediately after processing completes (an export pdf option is provided on the results page).',
  },
  {
    name: 'What can I do if I am not satisfied with the verification results?',
    text:
      'Export the verification results as a pdf. Send an email to support@phaethon.llc with the exported pdf and a description of the issue, including approximate date and time. Your issue will be reviewed and we will follow up with you to discuss next steps within three business days.',
  },
];

const jsonLd = {
  '@context': 'https://schema.org',
  '@graph': [
    webApplicationLd,
    {
      '@type': 'FAQPage',
      url: `${normalizedSiteUrl}#faq`,
      mainEntity: faqEntriesLd.map((entry) => ({
        '@type': 'Question',
        name: entry.name,
        acceptedAnswer: {
          '@type': 'Answer',
          text: entry.text,
        },
      })),
    },
  ],
};

export const metadata: Metadata = {
  metadataBase,
  title: {
    default: 'JurisCheck - Citation Verification for Legal Documents',
    template: '%s | JurisCheck Citation Verification',
  },
  description:
    'JurisCheck is a Bluebook-native legal citation verification service that helps attorneys, legal teams, and scholars confirm authorities before filing briefs, memos, and journal articles.',
  keywords: [
    'legal citation verification',
    'Bluebook compliance',
    'AI legal research',
    'law firm technology',
    'brief checker',
    'court filing preparation',
    'citation checker',
    'authenticating legal authorities',
  ],
  category: 'business',
  creator: 'Phaethon Order LLC',
  authors: [{ name: 'Phaethon Order LLC' }],
  publisher: 'Phaethon Order LLC',
  alternates: {
    canonical: '/',
  },
  openGraph: {
    type: 'website',
    url: metadataBase,
    title: 'JurisCheck Citation Verification Platform',
    description:
      'Automated Bluebook citation verification for briefs, motions, and legal scholarship with instant validation reporting.',
    siteName: 'JurisCheck',
    images: [
      {
        url: ogImageUrl,
        width: 1200,
        height: 630,
        alt: 'JurisCheck citation verification web application interface',
      },
    ],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'JurisCheck - Bluebook Citation Verification',
    description:
      'Verify legal citations before filing. JurisCheck checks Bluebook authorities, pin cites, and references in minutes.',
    images: [ogImageUrl],
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      'max-snippet': -1,
      'max-image-preview': 'large',
      'max-video-preview': -1,
    },
  },
  icons: {
    icon: '/favicon.ico',
    shortcut: '/favicon.ico',
    apple: '/apple-touch-icon.png',
    other: [
      { rel: 'icon', url: '/favicon-192x192.png', type: 'image/png' },
      { rel: 'icon', url: '/favicon-512x512.png', type: 'image/png' },
    ],
  },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>
          <div className="app-shell">
            <div className="app-shell__content">{children}</div>
            <footer className="site-footer" role="contentinfo">
              <div className="site-footer__content">
                <div className="site-footer__brand">
                  <span className="site-footer__logo">
                    <img src="/images/scales-of-justice.png" alt="" aria-hidden="true" />
                  </span>
                  <div>
                    <span className="site-footer__label">JurisCheck</span>
                    <span className="site-footer__tagline">Citation confidence for legal documents.</span>
                  </div>
                </div>
                <div className="site-footer__links">
                  <a href="/terms-of-use">Terms of Use</a>
                  <a href="mailto:support@phaethon.llc">Contact Support</a>
                  <a href="https://www.phaethonorder.com" target="_blank" rel="noopener noreferrer">
                    Visit phaethonorder.com
                  </a>
                </div>
                <p className="site-footer__copyright">© {currentYear} Phaethon Order LLC. All rights reserved.</p>
              </div>
            </footer>
          </div>
        </Providers>
        <script
          type="application/ld+json"
          suppressHydrationWarning
          dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
        />
      </body>
    </html>
  );
}
