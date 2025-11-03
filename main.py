# Copyright © 2025 Phaethon Order LLC. All rights reserved. Provided solely for evaluation. See LICENSE.

import io
import os
from typing import Any, Dict, List, Optional

import stripe
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.datastructures import FileStorage

from database.crud import (
    create_payment_record,
    get_or_create_user,
    mark_payment_completed,
    record_document_usage,
)
from database.models import Payment
from database.session import Base, engine, get_db
from svc.citations_compiler import compile_citations
from svc.doc_processor import extract_text
from utils.auth import AuthContext, get_auth_context
from utils.logger import setup_logger
from utils.payments import PAYMENT_PACKAGES, PaymentPackage, get_package

logger = setup_logger()


class CitationOccurrence(BaseModel):
    citation_category: str | None
    matched_text: str | None
    span: List[int] | None
    pin_cite: str | None
    # New fields for string citation support
    string_group_id: str | None = None
    position_in_string: int | None = None


class CitationEntry(BaseModel):
    resource_key: str
    type: str
    status: str
    substatus: str | None = None
    normalized_citation: str | None
    resource: Dict[str, Any]
    occurrences: List[CitationOccurrence]
    verification_details: Dict[str, Any] | None = None


class VerificationResponse(BaseModel):
    citations: List[CitationEntry]
    extracted_text: str | None = None
    remaining_credits: int


class UserBalanceResponse(BaseModel):
    email: str | None
    credits: int


class PaymentPackageResponse(BaseModel):
    key: str
    name: str
    credits: int
    amount_cents: int


class CreateCheckoutSessionRequest(BaseModel):
    package_key: str
    success_url: str | None = None
    cancel_url: str | None = None


class CreateCheckoutSessionResponse(BaseModel):
    session_id: str
    checkout_url: str
    package_key: str
    credits: int
    amount_cents: int
load_dotenv()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://citation-verifier.vercel.app")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI(title="citation-verifier", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://localhost:3000",
        "https://127.0.0.1:3000",
        "https://localhost:5174",
        "https://127.0.0.1:5174",
        "https://citation-verifier.onrender.com",
        "https://citation-verifier.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/api/health")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint that verifies configuration."""
    auth0_domain = os.getenv("AUTH0_DOMAIN")
    auth0_audience = os.getenv("AUTH0_AUDIENCE")
    auth0_issuer = os.getenv("AUTH0_ISSUER")
    
    auth0_configured = bool(auth0_domain and auth0_audience and auth0_issuer)
    stripe_configured = bool(STRIPE_SECRET_KEY)
    
    return {
        "status": "ok",
        "auth0_configured": auth0_configured,
        "auth0_domain_set": bool(auth0_domain),
        "auth0_audience_set": bool(auth0_audience),
        "auth0_issuer_set": bool(auth0_issuer),
        "stripe_configured": stripe_configured,
    }


def _ensure_stripe_configured() -> None:
    if not stripe.api_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe API key is not configured.",
        )


def _ensure_webhook_configured() -> None:
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe webhook secret is not configured.",
        )


def _success_url(url: Optional[str]) -> str:
    base_url = url or f"{FRONTEND_BASE_URL}/payments/success"
    if "{CHECKOUT_SESSION_ID}" not in base_url:
        separator = "&" if "?" in base_url else "?"
        base_url = f"{base_url}{separator}session_id={{CHECKOUT_SESSION_ID}}"
    return base_url


def _cancel_url(url: Optional[str]) -> str:
    return url or f"{FRONTEND_BASE_URL}/payments/cancelled"


def _serialize_package(package: PaymentPackage) -> PaymentPackageResponse:
    return PaymentPackageResponse(
        key=package.key,
        name=package.name,
        credits=package.credits,
        amount_cents=package.amount_cents,
    )


def _sanitize_citations(raw: Dict[str, Dict[str, Any]]) -> List[CitationEntry]:
    sanitized: List[CitationEntry] = []
    for resource_key, payload in raw.items():
        occurrences_payload = payload.get("occurrences", [])
        occurrences: List[CitationOccurrence] = []

        for occurrence in occurrences_payload:
            span = occurrence.get("span")
            span_list = list(span) if isinstance(span, tuple) else span
            occurrences.append(
                CitationOccurrence(
                    citation_category=occurrence.get("citation_category"),
                    matched_text=occurrence.get("matched_text"),
                    span=span_list,
                    pin_cite=occurrence.get("pin_cite"),
                    string_group_id=occurrence.get("string_group_id"),
                    position_in_string=occurrence.get("position_in_string"),
                )
            )

        sanitized.append(
            CitationEntry(
                resource_key=resource_key,
                type=payload.get("type", "unknown"),
                status=payload.get("status", "unknown"),
                substatus=payload.get("substatus"),
                normalized_citation=payload.get("normalized_citation"),
                resource=payload.get("resource", {}),
                occurrences=occurrences,
                verification_details=payload.get("verification_details"),
            )
        )

    return sanitized


@app.post("/api/verify", response_model=VerificationResponse)
async def verify_document(
    document: UploadFile = File(..., alias="document"),
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> VerificationResponse:
    file = document

    if not file.filename:
        logger.error("Uploaded file is missing a filename.")
        raise HTTPException(status_code=400, detail="Uploaded file is missing a filename.")

    extension = os.path.splitext(file.filename)[1].lower()
    if extension not in {".pdf", ".docx", ".txt"}:
        logger.error(f"Unsupported file format: {extension}")
        raise HTTPException(status_code=400, detail=f"Unsupported file format: {extension}")

    file_contents = await file.read()
    if not file_contents:
        logger.error("Uploaded file is empty.")
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    user = get_or_create_user(db, auth.sub, auth.email)
    if user.credits <= 0:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Insufficient credits. Please purchase document verification credits to continue.",
        )

    storage = FileStorage(
        stream=io.BytesIO(file_contents),
        filename=file.filename,
        content_type=file.content_type,
    )

    try:
        extracted_text = extract_text(storage)
    except ValueError as exc:
        logger.error(f"Error in extract_text: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - unexpected failure
        logger.error(f"Error in extract_text: {exc}")
        raise HTTPException(status_code=500, detail="Failed to extract text.") from exc

    try:
        compiled = await compile_citations(extracted_text)
    except Exception as exc:  # pragma: no cover - unexpected failure
        logger.error(f"Error in compile_citations: {exc}")
        raise HTTPException(status_code=500, detail="Failed to compile citations.") from exc

    sanitized = _sanitize_citations(compiled)

    record_document_usage(db, user, file.filename, credits_used=1)

    logger.info("Document verified for user %s. Remaining credits: %s", auth.sub, user.credits)

    return VerificationResponse(
        citations=sanitized,
        extracted_text=extracted_text,
        remaining_credits=user.credits,
    )


@app.get("/api/payments/packages", response_model=List[PaymentPackageResponse])
async def list_payment_packages() -> List[PaymentPackageResponse]:
    packages = sorted(PAYMENT_PACKAGES.values(), key=lambda pkg: pkg.credits)
    return [_serialize_package(pkg) for pkg in packages]


@app.get("/api/user/me", response_model=UserBalanceResponse)
async def get_current_user_balance(
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> UserBalanceResponse:
    user = get_or_create_user(db, auth.sub, auth.email)
    return UserBalanceResponse(email=user.email, credits=user.credits)


@app.post("/api/payments/verify-session")
async def verify_payment_session(
    payload: Dict[str, str],
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Fallback endpoint to verify and process a completed payment session.
    Used when webhooks might not have been delivered.
    """
    _ensure_stripe_configured()
    
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="session_id is required")
    
    try:
        # Retrieve the session from Stripe
        session = stripe.checkout.Session.retrieve(session_id)
        
        # Check if payment was successful
        if session.payment_status != "paid":
            return {"status": "pending", "message": "Payment not yet completed"}
        
        # Get metadata
        metadata = session.metadata or {}
        auth0_sub = metadata.get("auth0_sub")
        
        # Verify this session belongs to the current user
        if auth0_sub != auth.sub:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Session does not belong to user")
        
        # Check if already processed
        from database.models import Payment as PaymentModel
        existing_payment = db.execute(
            select(PaymentModel).where(PaymentModel.stripe_checkout_session_id == session_id)
        ).scalar_one_or_none()
        if existing_payment and existing_payment.status == "paid":
            return {
                "status": "already_processed",
                "message": "Payment already credited",
                "credits": existing_payment.credits_purchased
            }
        
        # Process the payment
        package_key = metadata.get("package_key")
        credits_raw = metadata.get("credits")
        amount_raw = metadata.get("amount_cents")
        user_email = metadata.get("email") or session.customer_details.email if session.customer_details else None
        
        if not package_key:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid session metadata")
        
        try:
            package = get_package(package_key)
        except ValueError:
            package = None
        
        credits = int(credits_raw or (package.credits if package else 0) or 0)
        amount_cents = int(amount_raw or session.amount_total or (package.amount_cents if package else 0) or 0)
        
        if credits <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid credit amount")
        
        user = get_or_create_user(db, auth.sub, user_email)
        
        # Extract payment_intent as string
        payment_intent_str: Optional[str] = None
        if session.payment_intent:
            if isinstance(session.payment_intent, str):
                payment_intent_str = session.payment_intent
            else:
                payment_intent_str = getattr(session.payment_intent, 'id', str(session.payment_intent))
        
        updated_user = mark_payment_completed(
            db,
            checkout_session_id=session_id,
            payment_intent_id=payment_intent_str,
            package_key=package_key,
            credits=credits,
            amount_cents=amount_cents,
        )
        
        if updated_user is None:
            # Create new payment record
            from database.models import Payment
            payment = Payment(
                user_id=user.id,
                stripe_checkout_session_id=session_id,
                stripe_payment_intent_id=session.payment_intent,
                package_key=package_key,
                credits_purchased=credits,
                amount_paid_cents=amount_cents,
                currency=session.currency or "usd",
                status="paid",
            )
            db.add(payment)
            user.credits += credits
            db.commit()
            db.refresh(user)
        
        logger.info(
            "Manually verified and processed payment for user %s, session %s (%s credits)",
            auth.sub,
            session_id,
            credits
        )
        
        return {
            "status": "success",
            "message": "Payment processed successfully",
            "credits": credits,
            "new_balance": user.credits
        }
        
    except stripe.StripeError as exc:
        logger.error("Failed to retrieve Stripe session %s: %s", session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to verify payment with Stripe"
        ) from exc


@app.post("/api/payments/checkout", response_model=CreateCheckoutSessionResponse)
async def create_checkout_session(
    payload: CreateCheckoutSessionRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> CreateCheckoutSessionResponse:
    _ensure_stripe_configured()

    try:
        package = get_package(payload.package_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user = get_or_create_user(db, auth.sub, auth.email)

    success_url = _success_url(payload.success_url)
    cancel_url = _cancel_url(payload.cancel_url)

    metadata: Dict[str, str] = {
        "auth0_sub": auth.sub,
        "package_key": package.key,
        "credits": str(package.credits),
        "amount_cents": str(package.amount_cents),
    }
    if user.email:
        metadata["email"] = user.email

    # Log email status for debugging
    if not user.email:
        logger.warning(
            "User %s has no email address. Email not included in Auth0 token. "
            "Check Auth0 API settings to include 'email' scope.",
            auth.sub
        )

    # Prepare Stripe session parameters
    stripe_params = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "line_items": [
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": package.amount_cents,
                    "product_data": {"name": package.name},
                },
                "quantity": 1,
            }
        ],
        "metadata": metadata,
    }
    
    # Only add customer_email if we have a valid email
    if user.email:
        stripe_params["customer_email"] = user.email

    try:
        session = stripe.checkout.Session.create(**stripe_params)
    except stripe.StripeError as exc:  # pragma: no cover - requires Stripe API
        logger.error("Stripe checkout session creation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unable to initiate checkout session with Stripe.",
        ) from exc

    create_payment_record(
        db,
        user=user,
        checkout_session_id=session.id,
        package_key=package.key,
        credits=package.credits,
        amount_cents=package.amount_cents,
        currency="usd",
    )

    logger.info(
        "Created Stripe checkout session %s for user %s (%s credits)",
        session.id,
        auth.sub,
        package.credits,
    )

    return CreateCheckoutSessionResponse(
        session_id=session.id,
        checkout_url=session.url or "",
        package_key=package.key,
        credits=package.credits,
        amount_cents=package.amount_cents,
    )


@app.post("/api/payments/webhook")
async def stripe_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    _ensure_webhook_configured()

    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    if signature is None:
        logger.warning("Stripe webhook called without signature header")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Stripe signature header.")

    try:
        event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError) as exc:  # pragma: no cover - requires Stripe API
        logger.warning("Invalid Stripe webhook signature: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Stripe webhook signature.") from exc

    logger.info("Received Stripe webhook event: %s", event["type"])

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        metadata = session_obj.get("metadata") or {}
        auth0_sub = metadata.get("auth0_sub")
        package_key = metadata.get("package_key")
        credits_raw = metadata.get("credits")
        amount_raw = metadata.get("amount_cents")
        user_email = metadata.get("email") or session_obj.get("customer_details", {}).get("email")

        if auth0_sub and package_key:
            try:
                package = get_package(package_key)
            except ValueError:
                package = None

            credits = int(credits_raw or (package.credits if package else 0) or 0)
            amount_cents = int(
                amount_raw
                or session_obj.get("amount_total")
                or session_obj.get("amount_subtotal")
                or (package.amount_cents if package else 0)
                or 0
            )

            if credits > 0:
                user = get_or_create_user(db, auth0_sub, user_email)
                updated_user = mark_payment_completed(
                    db,
                    checkout_session_id=session_obj["id"],
                    payment_intent_id=session_obj.get("payment_intent"),
                    package_key=package_key,
                    credits=credits,
                    amount_cents=amount_cents,
                )

                if updated_user is None:
                    payment = Payment(
                        user_id=user.id,
                        stripe_checkout_session_id=session_obj["id"],
                        stripe_payment_intent_id=session_obj.get("payment_intent"),
                        package_key=package_key,
                        credits_purchased=credits,
                        amount_paid_cents=amount_cents,
                        currency=(session_obj.get("currency") or "usd"),
                        status="paid",
                    )
                    db.add(payment)
                    user.credits += credits
                    db.commit()
                    db.refresh(user)

                logger.info(
                    "Applied payment for user %s via Stripe session %s (%s credits)",
                    auth0_sub,
                    session_obj["id"],
                    credits,
                )

    return JSONResponse(status_code=200, content={"received": True})
