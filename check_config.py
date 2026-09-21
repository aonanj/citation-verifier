#!/usr/bin/env python3
"""
Diagnostic script to verify Auth0 and environment configuration.
Run this to check if your environment variables are properly set.

Usage:
    python check_config.py
"""

import os
import sys
from typing import List, Tuple

# Try to load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
    print("✓ Loaded .env file\n")
except ImportError:
    print("⚠ python-dotenv not installed, skipping .env file\n")


def check_env_var(name: str, required: bool = True) -> Tuple[bool, str]:
    """Check if environment variable is set and return status."""
    value = os.getenv(name)
    if value:
        # Mask sensitive values
        if "KEY" in name or "SECRET" in name or "TOKEN" in name:
            masked = value[:8] + "..." if len(value) > 8 else "***"
            return True, f"✓ {name}: {masked}"
        return True, f"✓ {name}: {value}"
    else:
        status = "✗" if required else "○"
        return False, f"{status} {name}: NOT SET"


def main() -> None:
    print("=" * 60)
    print("JurisCheck Configuration Check")
    print("=" * 60)
    print()

    issues: List[str] = []

    # Auth0 Configuration
    print("Auth0 Configuration:")
    print("-" * 40)
    
    for var in ["AUTH0_DOMAIN", "AUTH0_AUDIENCE", "AUTH0_ISSUER"]:
        ok, msg = check_env_var(var, required=True)
        print(msg)
        if not ok:
            issues.append(f"Missing required variable: {var}")
    
    # Check Auth0 domain format
    domain = os.getenv("AUTH0_DOMAIN")
    if domain:
        if domain.startswith("https://"):
            print("  ⚠ AUTH0_DOMAIN should NOT include 'https://'")
            issues.append("AUTH0_DOMAIN includes protocol (should be just 'tenant.auth0.com')")
        else:
            print("  ✓ AUTH0_DOMAIN format looks correct")
    
    # Check Auth0 issuer format
    issuer = os.getenv("AUTH0_ISSUER")
    if issuer:
        if not issuer.endswith("/"):
            print("  ⚠ AUTH0_ISSUER should end with '/'")
            issues.append("AUTH0_ISSUER missing trailing slash")
        else:
            print("  ✓ AUTH0_ISSUER format looks correct")
    
    print()

    # Database
    print("Database Configuration:")
    print("-" * 40)
    ok, msg = check_env_var("DATABASE_URL", required=False)
    print(msg)
    if not ok:
        print("  ℹ Using default SQLite database")
    print()

    # Stripe
    print("Stripe Configuration:")
    print("-" * 40)
    for var in ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"]:
        ok, msg = check_env_var(var, required=False)
        print(msg)
    
    ok, msg = check_env_var("FRONTEND_BASE_URL", required=False)
    print(msg)
    if not ok:
        print("  ℹ Using default: http://localhost:3000")
    print()

    # API Keys
    print("External API Keys (Optional):")
    print("-" * 40)
    for var in [
        "COURTLISTENER_API_TOKEN",
        "GOVINFO_API_KEY",
        "AI_API_KEY",
        "AI_MODEL",
        "SEMANTIC_SCHOLAR_API_KEY",
        "OPENALEX_MAILTO",
    ]:
        ok, msg = check_env_var(var, required=False)
        print(msg)
    print()

    # Citation extraction
    print("Citation Extraction:")
    print("-" * 40)
    extractor = (os.getenv("CITATION_EXTRACTOR") or "llm").strip().lower()
    model = (os.getenv("AI_MODEL") or "").strip()
    if not model:
        print("○ AI_MODEL: NOT SET (state-law verification and the LLM extractor return errors)")
    elif not model.startswith("gpt"):
        print(f"✗ AI_MODEL: {model!r} is not an OpenAI \"gpt...\" model, the only kind implemented")
        issues.append(f"AI_MODEL {model!r} is not supported (state-law verification and the LLM extractor return errors)")
    if extractor == "llm":
        print(f"✓ CITATION_EXTRACTOR: llm (model {model or 'NOT SET'}, "
              f"reasoning effort {os.getenv('LLM_EXTRACTOR_REASONING_EFFORT') or 'none'})")
        if not model:
            issues.append("CITATION_EXTRACTOR=llm but AI_MODEL is not set")
        if not os.getenv("AI_API_KEY"):
            print("✗ AI_API_KEY: NOT SET (required by the LLM extractor)")
            issues.append("CITATION_EXTRACTOR=llm but AI_API_KEY is not set")
    elif extractor == "rules":
        print("✓ CITATION_EXTRACTOR: rules (eyecite + regex; set to 'llm' for the LLM extractor)")
    else:
        print(f"✗ CITATION_EXTRACTOR: {extractor!r} is not 'rules' or 'llm' (falls back to rules)")
        issues.append(f"CITATION_EXTRACTOR has an unknown value: {extractor!r}")
    print()

    # OCR / Tesseract
    print("OCR Configuration:")
    print("-" * 40)
    try:
        from svc.doc_processor import ocr_available
        if ocr_available():
            print("✓ Tesseract OCR: found on PATH (scanned/image-only PDFs can be processed)")
        else:
            print("✗ Tesseract OCR: NOT found on PATH")
            print("  ℹ Image-only PDF pages will fail or be skipped with a warning.")
            print("  ℹ Install: 'brew install tesseract' (macOS) or 'apt-get install tesseract-ocr' (Debian/Ubuntu).")
            issues.append("Tesseract OCR binary not found on PATH")
    except ImportError as exc:
        print(f"⚠ Could not check Tesseract OCR (svc.doc_processor import failed: {exc})")
    print()

    # Document normalization (what the LLM extractor reads)
    print("Document Normalization:")
    print("-" * 40)
    try:
        import importlib.util

        from svc.normalization.config import NormalizationConfig, normalization_enabled
        from svc.normalization.office import LibreOfficeFallbackRenderer
        from svc.normalization.pdf_ocr import ocr_available as normalization_ocr_available

        norm_config = NormalizationConfig.from_env()
        norm_on = normalization_enabled()
        missing = [m for m in ("docx2python", "pdfplumber", "pypdf", "pypdfium2", "ocrmypdf") if importlib.util.find_spec(m) is None]
        if not norm_on:
            print("○ DOCUMENT_NORMALIZATION: off (the LLM extractor reads the text svc.doc_processor extracts)")
        elif extractor != "llm":
            print("○ DOCUMENT_NORMALIZATION: on, but CITATION_EXTRACTOR is not 'llm', so it has no effect")
        else:
            print("✓ DOCUMENT_NORMALIZATION: on (DOCX as tagged text, PDF as pages; answers validated against the document)")
        if missing and norm_on:
            print(f"✗ Missing packages: {', '.join(missing)} (pip install -r requirements.txt)")
            issues.append(f"DOCUMENT_NORMALIZATION=on but these packages are not installed: {', '.join(missing)}")
        elif missing:
            print(f"○ Packages not installed (needed only when it is on): {', '.join(missing)}")
        if norm_on:
            if normalization_ocr_available():
                print("✓ OCR (OCRmyPDF + Tesseract): available for scanned pages")
            else:
                print("⚠ OCR (OCRmyPDF + Tesseract): not available; scanned pages are passed through unread, with a warning")
            print("✓ LibreOffice: found (DOCX files the parser can't read reliably are rendered to PDF)"
                  if LibreOfficeFallbackRenderer(norm_config).available()
                  else "○ LibreOffice: not found (optional: the DOCX fallback renderer; such files keep their tagged text and a warning)")
            if norm_config.cache_mode == "disk":
                print(f"⚠ NORMALIZATION_CACHE=disk: text of uploaded documents is kept on this server for {norm_config.cache_ttl_s:.0f} s "
                      "(the Terms say documents are not retained: a product decision, see CLAUDE.md item 35)")
            print(f"  limits: {norm_config.max_source_bytes // (1024 * 1024)} MB per file, {norm_config.max_pdf_pages} PDF pages, "
                  f"OCR timeout {norm_config.ocr_timeout_s:.0f} s, total timeout {norm_config.total_timeout_s:.0f} s")
    except ImportError as exc:
        print(f"⚠ Could not check document normalization (import failed: {exc})")
    print()

    # Frontend Configuration
    print("Frontend Configuration:")
    print("-" * 40)
    for var in [
        "NEXT_PUBLIC_AUTH0_DOMAIN",
        "NEXT_PUBLIC_AUTH0_CLIENT_ID",
        "NEXT_PUBLIC_AUTH0_AUDIENCE",
    ]:
        ok, msg = check_env_var(var, required=False)
        print(msg)
    
    ok, msg = check_env_var("BACKEND_URL", required=False)
    print(msg)
    if not ok:
        print("  ℹ Using default: http://localhost:8000")
    print()

    # Check for audience mismatch
    backend_audience = os.getenv("AUTH0_AUDIENCE")
    frontend_audience = os.getenv("NEXT_PUBLIC_AUTH0_AUDIENCE")
    if backend_audience and frontend_audience:
        if backend_audience == frontend_audience:
            print("✓ Backend and frontend audiences match")
        else:
            print("✗ Audience mismatch!")
            print(f"  Backend:  {backend_audience}")
            print(f"  Frontend: {frontend_audience}")
            issues.append("AUTH0_AUDIENCE mismatch between backend and frontend")
    print()

    # Summary
    print("=" * 60)
    if issues:
        print("⚠ ISSUES FOUND:")
        for issue in issues:
            print(f"  - {issue}")
        print()
        print("Please fix these issues before deploying.")
        print("See TROUBLESHOOTING_AUTH.md and RENDER_DEPLOYMENT.md for help.")
        sys.exit(1)
    else:
        print("✓ Configuration looks good!")
        print()
        print("Next steps:")
        print("  1. For local development: uvicorn main:app --reload")
        print("  2. For production: Set environment variables in Render dashboard")
        print("  3. Check health endpoint: /api/health")
        sys.exit(0)


if __name__ == "__main__":
    main()
