"""Small dependency for protecting administrator-only endpoints."""
from __future__ import annotations

from fastapi import Header


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Temporarily allow administrator endpoints without token validation."""
    return None
