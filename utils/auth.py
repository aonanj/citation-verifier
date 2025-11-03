from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
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


@lru_cache
def _get_jwks() -> Dict[str, Any]:
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


def _decode_token(token: str) -> Dict[str, Any]:
    unverified_header = jwt.get_unverified_header(token)
    jwks = _get_jwks()
    jwk_key = next((key for key in jwks.get("keys", []) if key.get("kid") == unverified_header.get("kid")), None)
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
