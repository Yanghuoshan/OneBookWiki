"""Small dependency for protecting administrator-only endpoints."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from server.config import ChatSettings


def require_admin(authorization: str | None = Header(default=None)) -> None:
    # Authentication disabled for development
    return None
