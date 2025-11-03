'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth0 } from '@auth0/auth0-react';
import styles from '../../page.module.css';

export default function PaymentCancelled() {
  const router = useRouter();
  const { isAuthenticated } = useAuth0();
  const [countdown, setCountdown] = useState(5);

  useEffect(() => {
    if (!isAuthenticated) {
      router.push('/');
      return;
    }

    // Countdown timer
    const timer = setInterval(() => {
      setCountdown((prev) => {
        if (prev <= 1) {
          clearInterval(timer);
          router.push('/');
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    return () => clearInterval(timer);
  }, [isAuthenticated, router]);

  return (
    <main className={styles.main}>
      <div className={styles.container}>
        <div style={{ textAlign: 'center', padding: '2rem' }}>
          <div style={{ fontSize: '4rem', marginBottom: '1rem' }}>❌</div>
          <h1 style={{ color: '#ef4444', marginBottom: '1rem' }}>Payment Cancelled</h1>
          <p style={{ fontSize: '1.1rem', marginBottom: '2rem', color: '#666' }}>
            Your payment was cancelled. No charges were made.
          </p>
          <div
            style={{
              background: '#fef2f2',
              border: '1px solid #fecaca',
              borderRadius: '8px',
              padding: '1.5rem',
              marginBottom: '2rem',
            }}
          >
            <p style={{ margin: 0, color: '#991b1b' }}>
              If you encountered an issue, please try again or contact support.
            </p>
          </div>
          <p style={{ color: '#666', marginBottom: '1rem' }}>
            Redirecting to home page in <strong>{countdown}</strong> seconds...
          </p>
          <button
            onClick={() => router.push('/')}
            style={{
              background: '#3b82f6',
              color: 'white',
              border: 'none',
              borderRadius: '6px',
              padding: '0.75rem 1.5rem',
              fontSize: '1rem',
              cursor: 'pointer',
              fontWeight: 500,
            }}
          >
            Return to Home Now
          </button>
        </div>
      </div>
    </main>
  );
}
