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
from database.models import Payment, UserAccount
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


def _stripe_to_dict(obj: Any) -> Dict[str, Any]:
    """Convert Stripe objects to plain dicts for safer access."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj

    to_dict_recursive = getattr(obj, "to_dict_recursive", None)
    if callable(to_dict_recursive):
        try:
            result = to_dict_recursive()
            if isinstance(result, dict):
                return result
        except Exception:
            pass

    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
            if isinstance(result, dict):
                return result
        except Exception:
            pass

    try:
        return dict(obj)
    except Exception:
        return {}


def _stripe_get(obj: Any, key: str) -> Any:
    """Safely fetch a key from Stripe objects, dicts, or plain attrs."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    getter = getattr(obj, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except Exception:
            pass
    if hasattr(obj, key):
        return getattr(obj, key)
    return None


def _coerce_stripe_id(value: Any) -> Optional[str]:
    """Ensure Stripe identifiers are stored as plain strings."""
    if not value:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "id"):
        potential_id = getattr(value, "id")
        if isinstance(potential_id, str):
            return potential_id
    return str(value)


def _to_int(value: Any) -> Optional[int]:
    """Best-effort conversion to int with graceful failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_charge_list(charges: Any) -> Dict[str, Any]:
    """Normalize Stripe charges collection into plain dict with dict entries."""
    charges_dict = _stripe_to_dict(charges)
    if not charges_dict:
        return {}
    data = charges_dict.get("data") or []
    normalized: List[Dict[str, Any]] = []
    for item in data:
        charge_dict = _stripe_to_dict(item)
        if charge_dict:
            normalized.append(charge_dict)
    charges_dict["data"] = normalized
    return charges_dict


def _load_payment_intent(payment_intent: Any) -> tuple[Optional[str], Dict[str, Any]]:
    """Load payment intent details, fetching from Stripe if necessary."""
    payment_intent_id = _coerce_stripe_id(payment_intent)
    intent_data = _stripe_to_dict(payment_intent)

    if payment_intent_id and not intent_data:
        try:
            intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            intent_data = _stripe_to_dict(intent)
        except stripe.StripeError as exc:
            logger.warning("Unable to retrieve Stripe payment intent %s: %s", payment_intent_id, exc)
            intent_data = {}

    if intent_data:
        charges = _normalize_charge_list(intent_data.get("charges"))
        if charges:
            intent_data["charges"] = charges

    return payment_intent_id, intent_data


def _extract_customer_email(
    metadata: Dict[str, Any],
    session_obj: Any,
    payment_intent_data: Dict[str, Any],
    fallback_email: Optional[str] = None,
) -> Optional[str]:
    """Determine the best available email for the purchaser."""
    candidates: List[Optional[str]] = []
    if metadata:
        candidates.append(metadata.get("email"))

    customer_details = _stripe_to_dict(_stripe_get(session_obj, "customer_details"))
    if customer_details:
        candidates.append(customer_details.get("email"))

    if payment_intent_data:
        candidates.append(payment_intent_data.get("receipt_email"))
        charges = payment_intent_data.get("charges") or {}
        charges_dict = _normalize_charge_list(charges)
        for charge in charges_dict.get("data", []):
            billing_details = _stripe_to_dict(charge.get("billing_details"))
            if billing_details and billing_details.get("email"):
                candidates.append(billing_details.get("email"))
            if charge.get("receipt_email"):
                candidates.append(charge.get("receipt_email"))

    if fallback_email:
        candidates.append(fallback_email)

    for email in candidates:
        if email:
            return email
    return None


class PaymentPendingError(Exception):
    """Raised when a Stripe checkout session is not yet fully paid."""


class PaymentOwnershipError(Exception):
    """Raised when a checkout session does not belong to the expected user."""


def _process_checkout_completion(
    db: Session,
    *,
    auth0_sub: Optional[str],
    session_obj: Any,
    metadata: Dict[str, Any],
    fallback_email: Optional[str],
    require_matching_sub: bool = False,
) -> Dict[str, Any]:
    """
    Validate a completed Stripe checkout session and apply credits for the user.

    Returns context with processed user, credits, amount, and identifiers.
    """
    session_id = _coerce_stripe_id(_stripe_get(session_obj, "id"))
    if not session_id:
        raise ValueError("Stripe session is missing an identifier.")

    existing_payment = db.execute(
        select(Payment).where(Payment.stripe_checkout_session_id == session_id)
    ).scalar_one_or_none()

    payment_intent_id, payment_intent_data = _load_payment_intent(_stripe_get(session_obj, "payment_intent"))

    payment_status = _stripe_get(session_obj, "payment_status")
    session_status = _stripe_get(session_obj, "status")
    intent_status = payment_intent_data.get("status") if payment_intent_data else None

    is_paid = payment_status in {"paid", "no_payment_required"}
    if not is_paid and intent_status:
        if intent_status in {"succeeded", "requires_capture"}:
            is_paid = True
    if not is_paid and session_status == "complete" and intent_status == "succeeded":
        is_paid = True

    if not is_paid:
        raise PaymentPendingError("Payment not yet completed.")

    metadata = metadata or {}

    package_key = metadata.get("package_key")
    if not package_key and existing_payment:
        package_key = existing_payment.package_key

    package: Optional[PaymentPackage] = None
    if package_key:
        try:
            package = get_package(package_key)
        except ValueError:
            package = None

    credits = _to_int(metadata.get("credits"))
    if credits is None and existing_payment:
        credits = existing_payment.credits_purchased
    if credits is None and package:
        credits = package.credits
    if credits is None or credits <= 0:
        raise ValueError("Unable to determine credit amount for checkout session.")

    amount_cents = _to_int(metadata.get("amount_cents"))
    if amount_cents is None:
        amount_cents = _to_int(_stripe_get(session_obj, "amount_total"))
    if amount_cents is None:
        amount_cents = _to_int(_stripe_get(session_obj, "amount_subtotal"))
    if amount_cents is None and existing_payment:
        amount_cents = existing_payment.amount_paid_cents
    if amount_cents is None and package:
        amount_cents = package.amount_cents
    if amount_cents is None or amount_cents < 0:
        raise ValueError("Unable to determine payment amount for checkout session.")

    currency = (
        _stripe_get(session_obj, "currency")
        or (payment_intent_data.get("currency") if payment_intent_data else None)
        or (existing_payment.currency if existing_payment else None)
        or "usd"
    )

    existing_user: Optional[UserAccount] = None
    if existing_payment:
        existing_user = db.execute(
            select(UserAccount).where(UserAccount.id == existing_payment.user_id)
        ).scalar_one_or_none()
    if existing_user is None and auth0_sub:
        existing_user = db.execute(
            select(UserAccount).where(UserAccount.auth0_sub == auth0_sub)
        ).scalar_one_or_none()

    payment_owner_sub = existing_user.auth0_sub if existing_user else None
    effective_sub = auth0_sub or metadata.get("auth0_sub") or payment_owner_sub

    if require_matching_sub and payment_owner_sub and auth0_sub and payment_owner_sub != auth0_sub:
        raise PaymentOwnershipError("Checkout session belongs to a different user.")

    if effective_sub is None:
        raise ValueError("Unable to determine user for checkout session.")

    fallback_email = fallback_email or (existing_user.email if existing_user else None)
    purchaser_email = _extract_customer_email(metadata, session_obj, payment_intent_data, fallback_email)

    user: UserAccount
    if existing_user:
        user = existing_user
        if purchaser_email and user.email != purchaser_email:
            user.email = purchaser_email
            db.commit()
            db.refresh(user)
    else:
        user = get_or_create_user(db, effective_sub, purchaser_email)

    if existing_payment and existing_payment.status == "paid":
        if payment_intent_id and not existing_payment.stripe_payment_intent_id:
            existing_payment.stripe_payment_intent_id = payment_intent_id
            db.commit()
        return {
            "user": user,
            "credits": existing_payment.credits_purchased,
            "amount_cents": existing_payment.amount_paid_cents,
            "session_id": session_id,
            "payment_intent_id": existing_payment.stripe_payment_intent_id or payment_intent_id,
            "package_key": existing_payment.package_key,
            "currency": existing_payment.currency,
            "already_processed": True,
        }

    if not package_key:
        raise ValueError("Unable to determine package for checkout session.")

    processed_user = mark_payment_completed(
        db,
        checkout_session_id=session_id,
        payment_intent_id=payment_intent_id,
        package_key=package_key,
        credits=credits,
        amount_cents=amount_cents,
    )

    if processed_user is None:
        payment = Payment(
            user_id=user.id,
            stripe_checkout_session_id=session_id,
            stripe_payment_intent_id=payment_intent_id,
            package_key=package_key,
            credits_purchased=credits,
            amount_paid_cents=amount_cents,
            currency=currency,
            status="paid",
        )
        db.add(payment)
        user.credits += credits
        db.commit()
        db.refresh(user)
        processed_user = user

    return {
        "user": processed_user,
        "credits": credits,
        "amount_cents": amount_cents,
        "session_id": session_id,
        "payment_intent_id": payment_intent_id,
        "package_key": package_key,
        "currency": currency,
        "already_processed": False,
    }


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
        session = stripe.checkout.Session.retrieve(
            session_id,
            expand=["payment_intent", "customer", "customer_details"],
        )
        
        # Get metadata
        metadata = _stripe_to_dict(getattr(session, "metadata", None))
        auth0_sub = metadata.get("auth0_sub")
        
        # Verify this session belongs to the current user
        if auth0_sub != auth.sub:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Session does not belong to user")
        try:
            result = _process_checkout_completion(
                db,
                auth0_sub=auth.sub,
                session_obj=session,
                metadata=metadata,
                fallback_email=auth.email,
                require_matching_sub=True,
            )
        except PaymentPendingError:
            return {"status": "pending", "message": "Payment not yet completed"}
        except PaymentOwnershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        user: UserAccount = result["user"]

        if result.get("already_processed"):
            logger.info(
                "Payment session %s already processed for user %s (%s credits)",
                result["session_id"],
                auth.sub,
                result["credits"],
            )
            return {
                "status": "already_processed",
                "message": "Payment already credited",
                "credits": result["credits"],
                "new_balance": user.credits,
            }

        logger.info(
            "Manually verified and processed payment for user %s, session %s (%s credits)",
            auth.sub,
            result["session_id"],
            result["credits"],
        )
        
        return {
            "status": "success",
            "message": "Payment processed successfully",
            "credits": result["credits"],
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
        metadata = _stripe_to_dict(_stripe_get(session_obj, "metadata"))
        auth0_sub = metadata.get("auth0_sub")

        logger.info(
            "Processing webhook for session %s, user %s",
            _stripe_get(session_obj, "id"),
            auth0_sub or "unknown",
        )

        try:
            result = _process_checkout_completion(
                db,
                auth0_sub=auth0_sub,
                session_obj=session_obj,
                metadata=metadata,
                fallback_email=metadata.get("email"),
                require_matching_sub=False,
            )
        except PaymentPendingError:
            logger.info(
                "Stripe checkout session %s not yet paid; will retry later.",
                _stripe_get(session_obj, "id"),
            )
        except PaymentOwnershipError as exc:
            logger.error(
                "Stripe checkout session %s ownership mismatch: %s",
                _stripe_get(session_obj, "id"),
                exc,
            )
        except ValueError as exc:
            logger.error(
                "Unable to process Stripe session %s: %s",
                _stripe_get(session_obj, "id"),
                exc,
            )
        except Exception as exc:
            logger.error(
                "Failed to process webhook for session %s: %s",
                _stripe_get(session_obj, "id"),
                exc,
                exc_info=True,
            )
        else:
            user: UserAccount = result["user"]
            if result.get("already_processed"):
                logger.info(
                    "Stripe session %s already processed for user %s",
                    result["session_id"],
                    user.auth0_sub,
                )
            else:
                logger.info(
                    "Applied payment for user %s via Stripe session %s (%s credits)",
                    user.auth0_sub,
                    result["session_id"],
                    result["credits"],
                )

    return JSONResponse(status_code=200, content={"received": True})
