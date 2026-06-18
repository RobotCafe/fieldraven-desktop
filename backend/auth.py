"""
FastAPI authentication dependency.
Verifies Firebase ID tokens from the browser and provides the current user context.
"""
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Callable
import functools

from . import firebase_client

# ── FastAPI security scheme ──────────────────────────────────
# Expects "Authorization: Bearer <id_token>" header
bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser:
    """
    Represents an authenticated user.
    Populated by the `require_auth` dependency.
    """
    def __init__(self, uid: str, email: Optional[str] = None, name: Optional[str] = None):
        self.uid = uid
        self.email = email
        self.name = name
        self._token: Optional[str] = None

    def __repr__(self) -> str:
        return f"CurrentUser(uid={self.uid}, email={self.email})"


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[CurrentUser]:
    """
    Optional auth dependency. Returns CurrentUser if a valid token is provided,
    or None if no token is present. Does NOT raise on missing token.
    Used for endpoints that work both authenticated and unauthenticated.
    """
    if credentials is None:
        return None

    token = credentials.credentials
    if not token:
        return None

    decoded = firebase_client.verify_id_token(token)
    if decoded is None:
        return None

    user = CurrentUser(
        uid=decoded.get('uid', 'unknown'),
        email=decoded.get('email'),
        name=decoded.get('name'),
    )
    user._token = token
    return user


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> CurrentUser:
    """
    Required auth dependency. Raises 401 if no valid token is provided.
    Use this on endpoints that need authentication.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Sign in with Google on the dashboard.",
        )

    token = credentials.credentials
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )

    decoded = firebase_client.verify_id_token(token)
    if decoded is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token. Please sign in again.",
        )

    user = CurrentUser(
        uid=decoded.get('uid', 'unknown'),
        email=decoded.get('email'),
        name=decoded.get('name'),
    )
    user._token = token
    return user