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
    "single": PaymentPackage(key="single", name="1 verification", credits=1, amount_cents=450),
    "bundle_5": PaymentPackage(key="bundle_5", name="5 verifications", credits=5, amount_cents=1950),
    "bundle_10": PaymentPackage(key="bundle_10", name="10 verifications", credits=10, amount_cents=3950),
    "bundle_20": PaymentPackage(key="bundle_20", name="20 verifications", credits=20, amount_cents=7950),
}


def get_package(package_key: str) -> PaymentPackage:
    try:
        return PAYMENT_PACKAGES[package_key]
    except KeyError:
        valid_keys = ", ".join(PAYMENT_PACKAGES.keys())
        raise ValueError(f"Unknown package '{package_key}'. Valid packages: {valid_keys}")
