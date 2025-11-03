from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DocumentUsage, Payment, UserAccount


def get_or_create_user(db: Session, auth0_sub: str, email: Optional[str] = None) -> UserAccount:
    user = db.execute(select(UserAccount).where(UserAccount.auth0_sub == auth0_sub)).scalar_one_or_none()
    if user is None:
        user = UserAccount(auth0_sub=auth0_sub, email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
    elif email and user.email != email:
        user.email = email
        db.commit()
        db.refresh(user)
    return user


def record_document_usage(db: Session, user: UserAccount, document_name: Optional[str], credits_used: int = 1) -> None:
    usage = DocumentUsage(user_id=user.id, document_name=document_name, credits_used=credits_used)
    db.add(usage)
    user.credits = max(user.credits - credits_used, 0)
    db.commit()
    db.refresh(user)


def create_payment_record(
    db: Session,
    *,
    user: UserAccount,
    checkout_session_id: str,
    package_key: str,
    credits: int,
    amount_cents: int,
    currency: str,
) -> Payment:
    payment = Payment(
        user_id=user.id,
        stripe_checkout_session_id=checkout_session_id,
        package_key=package_key,
        credits_purchased=credits,
        amount_paid_cents=amount_cents,
        currency=currency,
        status="pending",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return payment


def mark_payment_completed(
    db: Session,
    *,
    checkout_session_id: str,
    payment_intent_id: Optional[str],
    package_key: str,
    credits: int,
    amount_cents: int,
) -> Optional[UserAccount]:
    payment = (
        db.execute(select(Payment).where(Payment.stripe_checkout_session_id == checkout_session_id))
        .scalar_one_or_none()
    )
    if payment is None:
        return None

    payment.status = "paid"
    payment.stripe_payment_intent_id = payment_intent_id
    payment.credits_purchased = credits
    payment.amount_paid_cents = amount_cents

    user = db.execute(select(UserAccount).where(UserAccount.id == payment.user_id)).scalar_one()
    user.credits += credits

    db.commit()
    db.refresh(user)
    return user
