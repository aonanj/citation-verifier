from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class PaymentPackage:
    key: str
    name: str
    credits: int
    amount_cents: int


PAYMENT_PACKAGES: Dict[str, PaymentPackage] = {
    "single": PaymentPackage(
        key="single",
        name="Document verification credit (1)",
        credits=1,
        amount_cents=450,
    ),
    "bundle_5": PaymentPackage(
        key="bundle_5",
        name="Document verification credits (5)",
        credits=5,
        amount_cents=1950,
    ),
    "bundle_10": PaymentPackage(
        key="bundle_10",
        name="Document verification credits (10)",
        credits=10,
        amount_cents=3950,
    ),
    "bundle_20": PaymentPackage(
        key="bundle_20",
        name="Document verification credits (20)",
        credits=20,
        amount_cents=7950,
    ),
}


def get_package(package_key: str) -> PaymentPackage:
    try:
        return PAYMENT_PACKAGES[package_key]
    except KeyError:
        valid_keys = ", ".join(PAYMENT_PACKAGES.keys())
        raise ValueError(f"Unknown package '{package_key}'. Valid packages: {valid_keys}")
