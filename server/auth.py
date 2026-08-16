"""Small dependency for protecting administrator-only endpoints."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from server.config import ChatSettings


def require_admin(authorization: str | None = Header(default=None)) -> None:
    configured = ChatSettings.from_env().admin_token
    if not configured:
        raise HTTPException(status_code=503, detail="Administrator authentication is not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or not secrets.compare_digest(token, configured):
        raise HTTPException(status_code=401, detail="Administrator authentication required")
