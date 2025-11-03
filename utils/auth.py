from __future__ import annotations

import os
from dataclasses import dataclass
import threading
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from jose.backends.rsa_backend import RSAKey

AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN")
AUTH0_AUDIENCE = os.getenv("AUTH0_AUDIENCE")
AUTH0_ISSUER = os.getenv("AUTH0_ISSUER") or (f"https://{AUTH0_DOMAIN}/" if AUTH0_DOMAIN else None)
ALGORITHMS = ["RS256"]

security = HTTPBearer(auto_error=False)


def _load_jwks_cache_ttl() -> int:
    raw_ttl = os.getenv("AUTH0_JWKS_CACHE_TTL")
    if not raw_ttl:
        return 3600
    try:
        parsed = int(raw_ttl)
    except ValueError:
        return 3600
    return max(parsed, 60)


JWKS_CACHE_TTL_SECONDS = _load_jwks_cache_ttl()
_jwks_cache: Optional[Dict[str, Any]] = None
_jwks_cache_expires_at: float = 0.0
_jwks_cache_lock = threading.Lock()


@dataclass
class AuthContext:
    token: str
    payload: Dict[str, Any]
    sub: str
    email: Optional[str]


def _require_auth0_configuration() -> None:
    missing = []
    if not AUTH0_DOMAIN:
        missing.append("AUTH0_DOMAIN")
    if not AUTH0_AUDIENCE:
        missing.append("AUTH0_AUDIENCE")
    if not AUTH0_ISSUER:
        missing.append("AUTH0_ISSUER")
    if missing:
        message = f"Missing Auth0 configuration values: {', '.join(missing)}"
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=message)


def _fetch_jwks() -> Dict[str, Any]:
    _require_auth0_configuration()
    jwks_url = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
    try:
        response = httpx.get(jwks_url, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:  # pragma: no cover - network failure path
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to fetch Auth0 public keys.",
        ) from exc
    return response.json()


def _get_jwks(force_refresh: bool = False) -> Dict[str, Any]:
    global _jwks_cache, _jwks_cache_expires_at
    now = time.monotonic()
    if force_refresh or _jwks_cache is None or now >= _jwks_cache_expires_at:
        with _jwks_cache_lock:
            now = time.monotonic()
            if force_refresh or _jwks_cache is None or now >= _jwks_cache_expires_at:
                _jwks_cache = _fetch_jwks()
                _jwks_cache_expires_at = now + JWKS_CACHE_TTL_SECONDS
    return _jwks_cache


def _find_jwk(jwks: Dict[str, Any], kid: Optional[str]) -> Optional[Dict[str, Any]]:
    return next((key for key in jwks.get("keys", []) if key.get("kid") == kid), None)


def _decode_token(token: str) -> Dict[str, Any]:
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token header.") from exc

    jwks = _get_jwks()
    jwk_key = _find_jwk(jwks, unverified_header.get("kid"))
    if jwk_key is None:
        jwks = _get_jwks(force_refresh=True)
        jwk_key = _find_jwk(jwks, unverified_header.get("kid"))
    if jwk_key is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token header.")
    public_key = RSAKey(jwk_key, ALGORITHMS[0])

    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=ALGORITHMS,
            audience=AUTH0_AUDIENCE,
            issuer=AUTH0_ISSUER,
        )
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token.") from exc
    return payload


def get_auth_context(credentials: HTTPAuthorizationCredentials = Depends(security)) -> AuthContext:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization header is required.")
    token = credentials.credentials
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token is missing.")

    payload = _decode_token(token)
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token payload is missing subject.")

    email = payload.get("email")
    return AuthContext(token=token, payload=payload, sub=sub, email=email)
