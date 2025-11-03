// Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

import type { Metadata } from 'next';
import { ReactNode } from 'react';
import './globals.css';
import { Providers } from './providers';

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? 'https://citation-verifier.vercel.app';
const normalizedSiteUrl = siteUrl.startsWith('http') ? siteUrl : `https://${siteUrl}`;
const metadataBase = new URL(normalizedSiteUrl);
const ogImageUrl = new URL('/images/CitationVerifierLogo.png', metadataBase).href;
const webApplicationLd = {
  '@type': 'WebApplication',
  name: 'VeriCite Citation Verification',
  url: normalizedSiteUrl,
  description:
    'VeriCite is a Bluebook-native legal citation verification service that helps attorneys, legal teams, and scholars confirm authorities before filing briefs, memos, and journal articles.',
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
    name: 'What types of citations does VeriCite verify?',
    text:
      'VeriCite validates Bluebook-formatted case law, statutes, regulations, and secondary sources drawn from federal and state reporters, journals, and treatises.',
  },
  {
    name: 'Can VeriCite catch AI-generated hallucinated authorities?',
    text:
      'Yes. Every citation is scored against trusted legal research services so that hallucinated or fabricated authorities are flagged with confidence indicators before you file.',
  },
  {
    name: 'How quickly will my verification report be ready?',
    text:
      'Most briefs process in just a few minutes. You receive structured verification results with citations grouped, annotated, and ready for edits the moment the review completes.',
  },
  {
    name: 'Is my document secure during the verification process?',
    text:
      'Documents are encrypted in transit, and you can keep sensitive filings on-premises with the optional Microsoft Word add-in that mirrors the VeriCite workflow.',
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
    default: 'VeriCite - Citation Verification for Legal Professionals',
    template: '%s | VeriCite Citation Verification',
  },
  description:
    'VeriCite is a Bluebook-native legal citation verification service that helps attorneys, legal teams, and scholars confirm authorities before filing briefs, memos, and journal articles.',
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
    title: 'VeriCite Citation Verification Platform',
    description:
      'Automated Bluebook citation verification for briefs, motions, and legal scholarship with instant validation reporting.',
    siteName: 'VeriCite',
    images: [
      {
        url: ogImageUrl,
        width: 1200,
        height: 630,
        alt: 'VeriCite citation verification web application interface',
      },
    ],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'VeriCite - Bluebook Citation Verification',
    description:
      'Verify legal citations before filing. VeriCite checks Bluebook authorities, pin cites, and references in minutes.',
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
        <Providers>{children}</Providers>
        <script
          type="application/ld+json"
          suppressHydrationWarning
          dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
        />
      </body>
    </html>
  );
}
