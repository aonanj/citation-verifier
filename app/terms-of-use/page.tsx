import Link from 'next/link';
import type { Metadata } from 'next';
import styles from './page.module.css';

export const metadata: Metadata = {
  title: 'Terms of Use',
  description:
    'Terms of Use for JurisCheck covering eligibility, payments, acceptable use requirements, confidentiality, and support.',
};

const updatedDate = 'November 2, 2025';

const termsSections = [
  {
    heading: '1. Acceptance of Terms',
    body: [
      'The JurisCheck verification service, add-ins, and related documentation are provided by Phaethon Order LLC. By creating an account, initiating a verification request, or otherwise accessing JurisCheck, you confirm that you have authority to bind your organization and agree to comply with these Terms of Use.',
      'If you do not agree with these Terms, you must not access the application, APIs, or any associated services.',
    ],
  },
  {
    heading: '2. Account Eligibility & Security',
    body: [
      'Accounts are available to legal professionals, law firms, academic institutions, and their authorized contractors. You are responsible for maintaining the confidentiality of login credentials and for all activity that occurs under your account.',
      'Notify support@phaethon.llc immediately if you suspect unauthorized access, credential compromise, or changes to the status of the individuals who use JurisCheck on your behalf.',
    ],
  },
  {
    heading: '3. Credits, Payments, and Refunds',
    body: [
      'Verification requests consume prepaid credits that are purchased through the embedded Stripe checkout. Credits do not expire but are non-transferable.',
      'Charges are non-refundable once a verification run has started. Contact support@phaethon.llc within three business days if you believe credits were deducted in error; remediation is handled case-by-case.',
    ],
  },
  {
    heading: '4. Acceptable Use',
    body: [
      'You may upload only documents for which you have the right to process and that do not include confidential or privileged information unless you have secured client consent and comply with your jurisdiction’s ethical obligations.',
      'You may not reverse engineer, probe for vulnerabilities, or attempt to access systems or data outside the scope of the JurisCheck service. Scraping or automated querying of application endpoints is prohibited.',
    ],
  },
  {
    heading: '5. Data Handling & Privacy',
    body: [
      'Uploaded documents are encrypted in transit, processed solely for citation verification, and deleted after delivery of results. Extracted citation metadata may be retained temporarily for auditing and payment reconciliation.',
      'Do not upload documents that contain personally identifiable information, protected health information, or other regulated data without ensuring you have the legal right to do so.',
    ],
  },
  {
    heading: '6. Confidentiality & Feedback',
    body: [
      'All materials you submit remain your property. Phaethon Order LLC treats your documents and resulting reports as confidential information and will not disclose them to third parties except as required by law.',
      'Feedback, suggestions, or bug reports you submit may be used to improve the service without obligation to you.',
    ],
  },
  {
    heading: '7. Disclaimer of Warranties',
    body: [
      'JurisCheck is provided on an “as-is” and “as-available” basis. While our verification pipeline aggregates multiple authoritative sources, we do not guarantee the completeness or absolute accuracy of results.',
      'You remain solely responsible for reviewing verification summaries before filing documents, and for ensuring compliance with court rules and ethical obligations.',
    ],
  },
  {
    heading: '8. Limitation of Liability',
    body: [
      'To the maximum extent permitted by law, Phaethon Order LLC is not liable for indirect, incidental, special, consequential, or punitive damages, or for loss of profits or revenue, arising from your use of JurisCheck.',
      'Total liability for any claim under these Terms will not exceed the amount you paid to JurisCheck for the applicable verification credits in the twelve months preceding the event giving rise to the claim.',
    ],
  },
  {
    heading: '9. Suspension & Termination',
    body: [
      'We may suspend or terminate access to JurisCheck if you violate these Terms, misuse the service, or create risk of liability for other users. You may terminate your account at any time by contacting support@phaethon.llc; unused credits are forfeited upon termination unless otherwise required by law.',
    ],
  },
  {
    heading: '10. Updates to These Terms',
    body: [
      'We may update these Terms from time to time. Material changes will be posted in the application footer and emailed to account administrators. Continued use of JurisCheck after an update constitutes acceptance of the revised Terms.',
    ],
  },
];

export default function TermsOfUsePage() {
  return (
    <main className={styles.page}>
      <div className={styles.container}>
        <header className={styles.header}>
          <span className={styles.eyebrow}>JurisCheck Legal</span>
          <h1 className={styles.title}>Terms of Use</h1>
          <p className={styles.subtitle}>
            These Terms govern access to and use of JurisCheck citation verification services, add-ins, and supporting
            documentation.
          </p>
          <p className={styles.meta}>Last updated: {updatedDate}</p>
        </header>

        <div className={styles.sections}>
          {termsSections.map((section) => (
            <section key={section.heading} className={styles.section}>
              <h2 className={styles.sectionHeading}>{section.heading}</h2>
              {section.body.map((paragraph, index) => (
                <p key={`${section.heading}-${index}`} className={styles.paragraph}>
                  {paragraph}
                </p>
              ))}
            </section>
          ))}
        <Link href="/" className={styles.backLink}>
          ⇱ Back to home
        </Link>
        </div>

        <footer className={styles.contact}>
          <h2 className={styles.sectionHeading}>Questions?</h2>
          <p className={styles.paragraph}>
            Reach the JurisCheck team at{' '}
            <a href="mailto:support@phaethon.llc" className={styles.link}>
              support@phaethon.llc
            </a>{' '}
            for account assistance, incident reports, or clarification of these Terms.
          </p>
        </footer>
      </div>
    </main>
  );
}
