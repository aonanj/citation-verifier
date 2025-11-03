// Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

import type { Metadata } from 'next';
import { ReactNode } from 'react';
import './globals.css';
import AppProviders from './providers';

export const metadata: Metadata = {
  title: 'VeriCite - Citation Verification',
  description: 'Citation verification web service for briefs, memos, and other court filings and legal documents.',
  icons: '/favicon.ico',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AppProviders>{children}</AppProviders>
      </body>
    </html>
  );
}
