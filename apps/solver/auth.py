"""Who is calling: verify the identity provider's tokens and scope each request.

Skeddie never handles passwords. People sign in with an external OpenID
Connect provider, which issues signed access tokens. The API checks each
token's signature against the provider's published keys, plus its issuer,
audience and expiry, then maps (issuer, subject) to a Skeddie user. A first
sign-in is linked to an existing user by verified email; there is no
self-signup.

Configuration (environment variables):
  AUTH_ISSUER    required: the provider's issuer (the token's `iss` claim)
  AUTH_AUDIENCE  required: this API's identifier (the token's `aud` claim)
  AUTH_JWKS_URL  optional: where the signing keys are published; defaults to
                 the jwks_uri in the issuer's OIDC discovery document. A
                 file: URI is read from disk (local development only).
Without AUTH_ISSUER and AUTH_AUDIENCE, every protected route answers 503.
"""

import json
import os
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from database import get_db, scope_session, unscope_session
from models import Admin

# Asymmetric only: a token signed with a shared secret, or not signed at all
# ("none"), is rejected however it is presented.
ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
LEEWAY_SECONDS = 30


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID


class InvalidToken(Exception):
    pass


class TokenVerifier:
    def __init__(self, issuer: str, audience: str, signing_key_for: Callable[[str], object]):
        self.issuer = issuer
        self.audience = audience
        self._signing_key_for = signing_key_for

    @classmethod
    def from_jwks_url(cls, issuer: str, audience: str, jwks_url: str) -> "TokenVerifier":
        """Keys fetched (and cached) from the provider over http(s), or read from a file: URI in local dev."""
        if jwks_url.startswith("file:"):
            key_set = jwt.PyJWKSet.from_json(Path(url2pathname(urlparse(jwks_url).path)).read_text())
            return cls(issuer, audience, lambda token: _key_by_id(key_set, token))
        client = jwt.PyJWKClient(jwks_url, cache_keys=True)
        return cls(issuer, audience, lambda token: client.get_signing_key_from_jwt(token).key)

    def verify(self, token: str) -> Identity:
        try:
            claims = jwt.decode(
                token,
                self._signing_key_for(token),
                algorithms=ALLOWED_ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from None
        verified = claims.get("email_verified")
        return Identity(
            issuer=claims["iss"],
            subject=claims["sub"],
            email=claims.get("email"),
            # Some providers send the flag as the string "true".
            email_verified=verified is True or str(verified).lower() == "true",
        )


def _key_by_id(key_set: jwt.PyJWKSet, token: str) -> object:
    key_id = jwt.get_unverified_header(token).get("kid")
    for key in key_set.keys:
        if key.key_id == key_id:
            return key.key
    raise jwt.InvalidTokenError(f"Unknown signing key {key_id!r}")


@lru_cache
def _configured_verifier() -> TokenVerifier | None:
    issuer, audience = os.environ.get("AUTH_ISSUER"), os.environ.get("AUTH_AUDIENCE")
    if not issuer or not audience:
        return None
    jwks_url = os.environ.get("AUTH_JWKS_URL") or _discover_jwks_url(issuer)
    return TokenVerifier.from_jwks_url(issuer, audience, jwks_url)


def _discover_jwks_url(issuer: str) -> str:
    discovery = issuer.rstrip("/") + "/.well-known/openid-configuration"
    with urllib.request.urlopen(discovery, timeout=10) as response:
        return json.load(response)["jwks_uri"]


def get_token_verifier() -> TokenVerifier:
    """The configured verifier; tests override this with locally signed keys."""
    try:
        verifier = _configured_verifier()
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not load the identity provider's signing keys"
        ) from exc
    if verifier is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Authentication is not configured: set AUTH_ISSUER and AUTH_AUDIENCE"
        )
    return verifier


_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    verifier: TokenVerifier = Depends(get_token_verifier),
    db: Session = Depends(get_db),
) -> CurrentUser:
    if credentials is None:
        raise _unauthorized("Sign in required")
    try:
        identity = verifier.verify(credentials.credentials)
    except InvalidToken:
        raise _unauthorized("Invalid or expired token") from None

    user_id = db.scalar(
        text("SELECT public.resolve_user(:issuer, :subject, :email, :verified)"),
        {
            "issuer": identity.issuer,
            "subject": identity.subject,
            "email": identity.email,
            "verified": identity.email_verified,
        },
    )
    # Keep a first sign-in's link even if the rest of the request fails.
    db.commit()
    if user_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No Skeddie account matches this sign-in")
    return CurrentUser(user_id)


def org_admin_db(
    org_id: uuid.UUID,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
):
    """The request's session, limited by row-level security to what this admin may touch."""
    scope_session(db, user.id)
    try:
        if db.scalar(select(Admin.id).where(Admin.user_id == user.id, Admin.organization_id == org_id)) is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not an admin of this organization")
        yield db
    finally:
        unscope_session(db)
