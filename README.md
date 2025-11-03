# VeriCite - Citation Verification

Full-stack toolchain that verifies legal citations in legal briefs, memos, journal articles, and other legal and academic documents against primary sources. Available as a web service with a Next.js frontend that accepts DOCX, PDF, and plain text documents. The backend is a containerized Python service that extracts citations from a document (both inline citations and footnotes are compatible), normalizes them, and then verifies each citation. The annotated results are displayed with contextual highlights. The service is also available as a Microsoft Word Add-In. 

See [/addons/word-taskpane](/addons/word-taskpane/README.md) for further details about Word integration. 

(Note: Citations are assumed to be in Bluebook standard format.)

## Live Deployment
- App: https://citation-verifier.vercel.app/
- Demo login: `phaethon@phaethon.llc` / `pollc123#` (Auth0 username/password grant)

## Screenshots

### Next.js UI - Citation Verification List
![Citation Verification List](docs/screenshots/CV-Screenshot-2.png)

### Next.js UI - Uploaded Document with Verification Indicators
![Uploaded Document with Verification Indicators](docs/screenshots/CV-Screenshot-3.png)

### Word Add-In UI
![Word Add-In UI](docs/screenshots/CV-Screenshot-4.png)


## Overview
- **Document ingestion**: Accepts PDF (text or scanned), DOCX, and plain text files. Compatible with both inline citations and footnote citations.
- **Text normalization**: Uses PyMuPDF, python-docx, and Tesseract OCR when needed to produce a clean text stream with inline footnote content.
- **Citation resolution**: Eyecite identifies full, short, id., supra, and reference citations, clusters short forms with their full cite, and records pin cites and spans. Proprietary functionality accurately extracts footnote citations, string citations (semicolon-separated citations), and citations to secondary legal sources, clusters short forms, and maintains the sequential order of citations in document. 
- **Verification**:
  - **Case law**: CourtListener citation lookup with fuzzy matching (RapidFuzz) to flag name/year discrepancies.
  - **Federal law**: GovInfo link service with reporter-aware URL building for U.S.C., C.F.R., Stat., Pub. L., Fed. Reg., and related materials.
  - **State law**: OpenAI `gpt-5` Responses API with built-in web search tool access (Justia, Cornell LII, FindLaw) to score validity and return a matching or nearly matching citation, as well as a confidence score corresponding to verification status. 
  - **Journals**: OpenAlex API query with fallback to Semantic Scholar API query. Queries on title and author, with fallback to query on volume, journal, page, and year.
  - **Secondary Sources**: Library of Congress Search API query with fuzzy matching for legal encyclopedias (C.J.S., Am. Jur.), restatements, ALR annotations, and treatises.   
- **Results delivery**: FastAPI serializes a single payload containing citation metadata, status/substatus, occurrences, extracted text, and reference citation grouping information for the UI.

Pipeline: `document upload → POST /api/verify (FastAPI) → extract_text → compile_citations → verifiers → JSON response → Next.js renderer`.

### Known Issues & Limitations
- **Bluebook format**: Citations must follow standard Bluebook rules. No support is planned for other formats. 
- **URLs and other citations**: Supported citation types: (1) federal cases; (2) federal law; (3) state cases; (4) state laws; (5) journals; (6) secondary legal sources (legal encyclopedias, restatements, ALR, treatises). URLs and citation types other than those listed above are not supported. Support for additional citation types is in development. 
- **_infra_ short citations**: Short citations using _infra_ are not supported. To request this feature, contact [support@phaethon.llc](mailto:support@phaethon.llc).  

## System Architecture
### Backend (Python)
- **FastAPI** (`main.py`): Exposes async `POST /api/verify` endpoint, enforces file-type and size validation, orchestrates extraction and verification, and returns a typed Pydantic response model with CORS support for multiple origins.
- **Document processing** (`svc/doc_processor.py`):
  - PDF parsing via PyMuPDF with heuristics to merge wrapped lines and pull footnotes into context.
  - DOCX traversal that walks paragraphs, nested tables, and footnote XML, inlining references next to their markers.
  - OCR fallback for image-only PDFs using Pillow + Tesseract.
  - Normalization routines that standardize whitespace, smart quotes, and superscripts.
- **Citation compiler** (`svc/citations_compiler.py`): Cleans text, resolves eyecite clusters to stable `ResourceKey`s, records occurrences, processes string citations and secondary sources, and calls the appropriate verifier based on citation type and jurisdiction classification. Performs async verification for improved performance.
- **Verification modules** (`verifiers/`):
  - `case_verifier.py`: CourtListener integration with credential support, year extraction, and fuzzy name comparisons.
  - `federal_law_verifier.py`: Jurisdiction heuristics, GovInfo request builder, and reporter-specific parsing (e.g., CFR parts vs. sections).
  - `journal_verifier.py`: Queries OpenAlex API for journal articles, with fallback to Semantic Scholar API for enhanced coverage.
  - `secondary_sources_verifier.py`: Verifies secondary legal sources (C.J.S., Am. Jur., restatements, ALR, treatises) using Library of Congress Search API with fuzzy matching.
  - `state_law_verifier.py`: Constructs Bluebook-style prompts and interprets structured JSON replies with confidence scoring.
- **Citation handlers** (`svc/`):
  - `secondary_citation_handler.py`: Detects and resolves secondary source citations (treatises, encyclopedias, restatements) using regex patterns and antecedent matching.
  - `string_citation_handler.py`: Identifies and splits string citations (semicolon-separated citations) while preserving accurate spans.
- **Utilities** (`utils/`): Shared logging (env-aware file/console handlers), string cleaning, span recovery for eyecite tokens, and resource resolution helpers.

### Frontend (Next.js 15 / React 18)
- Single-page workflow in `app/page.tsx` for file upload, async status messaging, and rich results display.
- **Authentication**: Auth0 integration via `@auth0/auth0-react` with secure login/logout flow. Users must authenticate before uploading documents.
- Highlights occurrences inside the extracted text, color-coded by verification status with numbered badges.
- Summaries group citations by type, while each citation card shows type, occurrences, substatus, and verifier-supplied diagnostics.
- String citations are displayed with grouping indicators showing their relationship.
- `BACKEND_URL` configures the backend target; defaults to `http://localhost:8000` for local development.

## Project Layout
```
├── main.py                           # FastAPI entrypoint
├── addons/word-taskpane              # Word Add-In service
│   └── ...                           # See Word Add-In README.md
├── app/                              # Next.js application (App Router)
│   ├── layout.tsx                    # Global metadata and styling
│   ├── page.tsx                      # Upload form + results dashboard
│   ├── providers.tsx                 # Auth0 provider configuration
│   └── api/auth/[auth0]/route.ts     # Auth0 callback handler
├── svc/
│   ├── doc_processor.py              # Text extraction and normalization
│   ├── citations_compiler.py         # Eyecite integration and verifier dispatch
│   ├── secondary_citation_handler.py # Extracts secondary legal sources (supplement eyecite)
│   └── string_citation_handler.py    # Formats string citations
├── verifiers/                        # Citation verification services
│   ├── case_verifier.py              # CourtListener API integration
│   ├── federal_law_verifier.py       # GovInfo API integration
│   ├── journal_verifier.py           # OpenAlex + Semantic Scholar APIs
│   ├── secondary_sources_verifier.py # Library of Congress API integration
│   └── state_law_verifier.py         # OpenAI Responses API with web search
├── utils/                            # Shared utilities
│   ├── cleaner.py                    # String normalization and cleaning
│   ├── logger.py                     # Environment-aware logging setup
│   ├── resource_resolver.py          # Citation metadata extraction
│   └── span_finder.py                # Span calculation for eyecite tokens
├── resources/                        # Sample documents and reference material
├── Dockerfile                        # Container build
├── requirements.txt                  # Python dependency pins
├── package.json                      # Frontend dependencies
└── pyproject.toml                    # Backend build metadata
```

## Dependencies
### Python runtime
Major libraries are pinned in `requirements.txt` (compiled with Python 3.13):
- `fastapi`, `uvicorn` – API framework and ASGI server
- `eyecite` – legal citation parsing and clustering
- `pymupdf`, `pytesseract`, `python-docx`, `Pillow` – document ingestion and OCR
- `httpx`, `rapidfuzz`, `openai` – HTTP client, fuzzy matching, and AI verification
- `pydantic` – data validation and serialization
- `python-dotenv`, `werkzeug`, `regex`, `psycopg[binary]` – supporting utilities

### Node.js runtime
Key dependencies in `package.json`:
- `next` (15.5.3+), `react` (18.3.1+), `react-dom` – frontend framework
- `@auth0/auth0-react`, `@auth0/nextjs-auth0` – authentication
- `mammoth`, `pdf-parse` – client-side document preview
- `express`, `express-oauth2-jwt-bearer` – API middleware (Word Add-In)

### External services
- **CourtListener** citation lookup API (optional token for rate limit increase)
- **GovInfo** link service (API key recommended for higher rate limits)
- **OpenAI** Responses API (`gpt-5` model) with built-in web-search tool access
- **OpenAlex** API (optional mailto parameter for polite pool)
- **Semantic Scholar** API (optional API key for expanded access)
- **Library of Congress** Search API
- **Tesseract OCR** (local installation required for scanned PDFs)

## Setup
1. **Prerequisites**
   - Python 3.12 or 3.13 (requirements compiled with 3.13)
   - Node.js 18+ with npm
   - Tesseract OCR (`brew install tesseract` on macOS, `sudo apt-get install tesseract-ocr` on Debian/Ubuntu)
2. **Install backend packages**
   ```bash
   pip install -r requirements.txt
   ```
3. **Install frontend packages**
   ```bash
   npm install
   ```

## Configuration
Create `.env` in the project root for backend configuration:
```bash
# API Keys for verification services
COURTLISTENER_API_TOKEN=...   # CourtListener API (case verifications)
GOVINFO_API_KEY=...           # GovInfo API (federal law verifications)
OPENAI_API_KEY=...            # OpenAI API (state law verifications, gpt-5 model)
SEMANTIC_SCHOLAR_API_KEY=...  # Semantic Scholar API (journal verifications)
OPENALEX_MAILTO=...           # OpenAlex polite pool (journal verifications, optional)

# Logging configuration
LOG_TO_FILE=true              # Optional: write logs to disk
LOG_FILE_PATH=./citeverify.log

# Authentication & payments
AUTH0_DOMAIN=<tenant>.auth0.com
AUTH0_AUDIENCE=...
# Optional override; defaults to https://<tenant>.auth0.com/
AUTH0_ISSUER=https://<tenant>.auth0.com/
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
FRONTEND_BASE_URL=http://localhost:3000  # Stripe success/cancel redirect base
DATABASE_URL=sqlite:///./citation_verifier.db  # Optional: override default SQLite path

# CORS / backend routing
BACKEND_URL=http://localhost:8000  # Or production URL
```

Create `.env.local` in the project root for frontend configuration:
```bash
# Auth0 configuration (required)
NEXT_PUBLIC_AUTH0_DOMAIN=<tenant>.auth0.com
NEXT_PUBLIC_AUTH0_CLIENT_ID=...
NEXT_PUBLIC_AUTH0_AUDIENCE=...

# Backend URL
BACKEND_URL=http://localhost:8000  # Or production URL
```
Environment variables fall back to sane defaults when omitted; state-law verification returns errors if no OpenAI key is present.

**Auth0 Setup** (required for frontend):
- Create a "Regular Web Application" in Auth0
- Configure allowed callback URLs: `http://localhost:3000/api/auth/callback`, `http://localhost:3000`
- Set allowed logout URLs: `http://localhost:3000`
- Add environment variables to `.env.local` (frontend):
  - `NEXT_PUBLIC_AUTH0_DOMAIN`
  - `NEXT_PUBLIC_AUTH0_CLIENT_ID`
  - `NEXT_PUBLIC_AUTH0_AUDIENCE`
- Add the matching values to the backend `.env` (`AUTH0_DOMAIN`, `AUTH0_AUDIENCE`, optional `AUTH0_ISSUER`)

## Running Locally
```bash
# Backend (FastAPI)
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
      
uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2

# Frontend (Next.js) - run from repo root
npm run dev
```
- Visit `http://localhost:3000` to access the UI.
- Point the frontend at a different backend by setting `BACKEND_URL` before `npm run dev`.
- For Stripe webhooks in development, run `stripe listen --forward-to 127.0.0.1:8000/api/payments/webhook`.

## API
`POST /api/verify`
- **Auth**: `Authorization: Bearer <access_token>` (Auth0 access token)
- **Payload**: multipart form with a single `document` field containing a PDF, DOCX, or TXT file.
- **Response** (`application/json`):
  ```json
  {
    "citations": [
      {
        "resource_key": "case::...",
        "type": "case|law|journal|secondary",
        "status": "verified|warning|no_match|error",
        "substatus": "...",
        "normalized_citation": "...",
        "resource": { "kind": "case", "id_tuple": ["..."] },
        "occurrences": [
          {
            "citation_category": "full|short|id|supra|reference",
            "matched_text": "...",
            "span": [start, end],
            "pin_cite": "...",
            "string_group_id": "optional-string-citation-group-id",
            "position_in_string": 0
          }
        ],
        "verification_details": { /* verifier-specific metadata */ }
      }
    ],
    "extracted_text": "full normalized document text",
    "remaining_credits": 4
  }
  ```
- Errors use standard FastAPI problem responses (`detail` message with 4xx or 5xx).

`GET /api/user/me`
- **Auth**: `Authorization: Bearer <access_token>`
- **Response**: `{ "email": "...", "credits": 3 }`

`GET /api/payments/packages`
- **Auth**: none
- **Response**: array of available Stripe checkout packages (`key`, `name`, `credits`, `amount_cents`)

`POST /api/payments/checkout`
- **Auth**: `Authorization: Bearer <access_token>`
- **Payload**: `{ "package_key": "single|bundle_5|bundle_10|bundle_20" }`
- **Response**: `{ "session_id": "cs_test_...", "checkout_url": "https://checkout.stripe.com/...", "package_key": "...", "credits": 5, "amount_cents": 1950 }`
- Redirect the browser to `checkout_url` to complete payment. Register the Stripe webhook at `/api/payments/webhook` to credit purchases.

## Deployment
The backend is containerized using Docker for easy deployment:
```bash
docker build -t citation-verifier .
docker run -p 8000:8000 --env-file .env citation-verifier
```
The Dockerfile uses Python 3.12-slim, installs Tesseract OCR, and exposes port 8000. The `PORT` environment variable can be configured for cloud deployments (e.g., Render, Railway).

## Usage Tips
- **Authentication**: Sign in via Auth0 before uploading; the upload panel remains disabled until authentication is complete.
- **Payments**: Each verification consumes one credit ($4.50 per document, with 5/10/20-document bundles available). Purchase credits via the Stripe checkout buttons in the UI.
- **File limits**: Uploaded files must be PDF, DOCX, or TXT format. Files are validated before processing.
- **Results visualization**: The frontend highlights every matched occurrence in context; hover or scan the numbered badges to correlate citation cards with text spans.
- **Citation sequence**: The sequential order of citations in the document is maintained. Note, however, that the numbering displayed for verified citations may not correspond to the footnote numbering in documents using footnote citations. 
- **String citations**: Citations separated by semicolons are individually verified. The sequential order of citations in the document is maintained.
- **Status interpretation**: `substatus` provides detailed explanations for warnings and errors (e.g., `case name mismatch`, `closest_match: …`, `confidence: 0.75`).

## Logging
`utils/logger.py` honors `LOG_TO_FILE`/`LOG_FILE_PATH` or defaults to console output. Log formatting matches `[timestamp] - logger level message` for easier aggregation.

## License

This repository is publicly viewable for portfolio purposes only. The code is proprietary.
Copyright © 2025 Phaethon Order LLC. All rights reserved.
Contact [support@phaethon.llc](mailto:support@phaethon.llc) for licensing or reuse requests.

See [LICENSE](LICENSE)

Note: `package.json` may list a different license; the authoritative license for this repository is proprietary.

## Contact
Questions or support: [support@phaethon.llc](mailto:support@phaethon.llc).
