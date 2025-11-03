'use client';

import { useEffect, useState, Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useAuth0 } from '@auth0/auth0-react';
import styles from '../../page.module.css';

const API_BASE_URL = process.env.BACKEND_URL ?? 'http://localhost:8000';

function SuccessContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { isAuthenticated, getAccessTokenSilently } = useAuth0();
  const [countdown, setCountdown] = useState(5);
  const [credits, setCredits] = useState<number | null>(null);
  const sessionId = searchParams.get('session_id');

  useEffect(() => {
    if (!isAuthenticated) {
      router.push('/');
      return;
    }

    // Verify payment and fetch updated credit balance
    const verifyAndFetchCredits = async () => {
      try {
        const token = await getAccessTokenSilently({
          authorizationParams: {
            audience: process.env.NEXT_PUBLIC_AUTH0_AUDIENCE,
          },
        });

        // First, verify the payment session (in case webhook didn't fire)
        if (sessionId) {
          try {
            const verifyResponse = await fetch(`${API_BASE_URL}/api/payments/verify-session`, {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                Authorization: `Bearer ${token}`,
              },
              body: JSON.stringify({ session_id: sessionId }),
            });

            if (verifyResponse.ok) {
              const verifyData = (await verifyResponse.json()) as { 
                status: string; 
                new_balance?: number;
                credits?: number;
              };
              console.log('Payment verification:', verifyData);
              
              // If we got the new balance from verification, use it
              if (verifyData.new_balance !== undefined) {
                setCredits(verifyData.new_balance);
                return;
              }
            }
          } catch (verifyError) {
            console.error('Failed to verify payment session:', verifyError);
            // Continue to fetch balance normally
          }
        }

        // Fetch current balance
        const response = await fetch(`${API_BASE_URL}/api/user/me`, {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        });

        if (response.ok) {
          const data = (await response.json()) as { credits?: number };
          setCredits(data.credits ?? 0);
        }
      } catch (error) {
        console.error('Failed to fetch credits:', error);
      }
    };

    void verifyAndFetchCredits();

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
  }, [isAuthenticated, router, getAccessTokenSilently]);

  return (
    <main className={styles.main}>
      <div className={styles.container}>
        <div style={{ textAlign: 'center', padding: '2rem' }}>
          <div style={{ fontSize: '4rem', marginBottom: '1rem' }}>✅</div>
          <h1 style={{ color: '#10b981', marginBottom: '1rem' }}>Payment Successful!</h1>
          <p style={{ fontSize: '1.1rem', marginBottom: '0.5rem' }}>
            Your credits have been added to your account.
          </p>
          {sessionId && (
            <p style={{ fontSize: '0.9rem', color: '#666', marginBottom: '2rem' }}>
              Transaction ID: {sessionId.substring(0, 20)}...
            </p>
          )}
          <div
            style={{
              background: '#f0fdf4',
              border: '1px solid #86efac',
              borderRadius: '8px',
              padding: '1.5rem',
              marginBottom: '2rem',
            }}
          >
            <p style={{ margin: 0, color: '#166534' }}>
              🎉 You can now verify citations with your new credits!
            </p>
            {credits !== null && (
              <p style={{ margin: '0.5rem 0 0 0', color: '#166534', fontWeight: 600 }}>
                Your balance: {credits} credits
              </p>
            )}
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

export default function PaymentSuccess() {
  return (
    <Suspense
      fallback={
        <main className={styles.main}>
          <div className={styles.container}>
            <div style={{ textAlign: 'center', padding: '2rem' }}>
              <p>Loading...</p>
            </div>
          </div>
        </main>
      }
    >
      <SuccessContent />
    </Suspense>
  );
}
