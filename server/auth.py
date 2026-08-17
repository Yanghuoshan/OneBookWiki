"""Small dependency for protecting administrator-only endpoints."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from server.config import ChatSettings


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Require a configured bearer token for administrator-only audit endpoints."""
    expected = ChatSettings.from_env().admin_token
    if not expected:
        raise HTTPException(status_code=503, detail="Administrator endpoints are not configured")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Administrator authorization is required")
