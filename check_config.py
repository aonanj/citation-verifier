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
    print("Citation Verifier Configuration Check")
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
        "OPENAI_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
        "OPENALEX_MAILTO",
    ]:
        ok, msg = check_env_var(var, required=False)
        print(msg)
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
