'use client';

import { ReactNode } from 'react';
import { UserProvider } from '@auth0/nextjs-auth0/client';

export default function AppProviders({ children }: { children: ReactNode }) {
  return <UserProvider>{children}</UserProvider>;
}
