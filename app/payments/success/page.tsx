'use client';

// Import useCallback
import { useEffect, useState, Suspense, useCallback } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useAuth0 } from '@auth0/auth0-react';
import styles from '../../page.module.css';

const API_BASE_URL = process.env.BACKEND_URL ?? 'http://localhost:8000';

function SuccessContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // Get isLoading: auth0Loading
  const { isAuthenticated, getAccessTokenSilently, isLoading: auth0Loading } = useAuth0();
  const [countdown, setCountdown] = useState(3);
  const [credits, setCredits] = useState<number | null>(null);
  const sessionId = searchParams.get('session_id');

  // --- Wrap data fetching in useCallback ---
  // This makes the function stable and safe to use in useEffect
  const verifyAndFetchCredits = useCallback(async () => {
    let retryCount = 0;
    const maxRetries = 3;
    let verifyTimer: NodeJS.Timeout | undefined;

    // Define the async function that can be retried
    const attemptVerification = async () => {
      // Clear any previous retry timer
      if (verifyTimer) clearTimeout(verifyTimer);

      try {
        // This is the part that was hanging
        const token = await getAccessTokenSilently({
          authorizationParams: {
            audience: process.env.NEXT_PUBLIC_AUTH0_AUDIENCE,
          },
        });

        // First, verify the payment session
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
                message?: string;
              };
              console.log('Payment verification:', verifyData);

              if (verifyData.status === 'success' || verifyData.status === 'already_processed') {
                // Success! Use the new balance.
                if (verifyData.new_balance !== undefined) {
                  setCredits(verifyData.new_balance);
                }
                return; // Stop retrying
              } else if (verifyData.status === 'pending' && retryCount < maxRetries) {
                // Payment is pending, retry after a delay
                retryCount++;
                console.warn(`Payment pending. Retry ${retryCount}/${maxRetries}...`);
                verifyTimer = setTimeout(attemptVerification, 2000 * retryCount); // 2s, 4s, 6s
                return; // Wait for retry
              } else if (verifyData.status === 'pending') {
                console.error('Payment verification timed out (still pending).');
              }
            }
          } catch (verifyError) {
            console.error('Failed to verify payment session:', verifyError);
          }
        }

        // Fetch current balance (as a fallback or after verification)
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
        console.error('Failed to fetch credits (likely auth issue):', error);
        // Do not setCredits(null) here, let it just fail silently
        // The redirect timer will handle leaving the page.
      }
    };

    // Start the first attempt
    await attemptVerification();

    // Return a cleanup function for the retry timer
    return () => {
      if (verifyTimer) clearTimeout(verifyTimer);
    };
  }, [getAccessTokenSilently, sessionId]); // Dependencies for the callback


  // --- MODIFIED useEffect ---
  useEffect(() => {
    // 1. Wait for Auth0 to finish loading
    if (auth0Loading) {
      console.log("Auth0 is loading, waiting...");
      return;
    }

    // 2. If not logged in after load, go home
    if (!isAuthenticated) {
      console.log("Not authenticated, redirecting home.");
      router.push('/');
      return;
    }

    // 3. START THE COUNTDOWN TIMER IMMEDIATELY
    // This is no longer blocked by the async data fetching
    const countdownTimer = setInterval(() => {
      setCountdown((prev) => {
        if (prev <= 1) {
          clearInterval(countdownTimer);
          router.push('/');
          return 0;
        }
        return prev - 1;
      });
    }, 1000);

    // 4. START THE DATA FETCHING IN PARALLEL
    // We call the stable function returned by useCallback
    const cleanupVerifyPromise = verifyAndFetchCredits();

    // 5. Return cleanup for BOTH processes
    return () => {
      clearInterval(countdownTimer);
      // When component unmounts, call the cleanup function
      // that was returned by verifyAndFetchCredits
      cleanupVerifyPromise.then(cleanup => cleanup && cleanup());
    };
    
  // Add auth0Loading and the stable verifyAndFetchCredits
  }, [auth0Loading, isAuthenticated, router, verifyAndFetchCredits]);


  return (
    <main className={styles.main}>
      <div className={styles.container}>
        <div style={{ textAlign: 'center', padding: '2rem' }}>
          <div style={{ fontSize: '4rem', marginBottom: '1rem' }}>✅</div>
          <h1 style={{ color: '#10b981', marginBottom: '1rem' }}>Payment Successful</h1>
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
            {credits !== null ? (
              <p style={{ margin: '0.5rem 0 0 0', color: '#166534', fontWeight: 600 }}>
                Current balance: {credits} credits
              </p>
            ) : (
              <p style={{ margin: '0.5rem 0 0 0', color: '#166534', fontWeight: 600 }}>
                Updating credit balance...
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