from __future__ import annotations

import hmac

from fastapi import Request

from .config import settings


def authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def valid_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, settings.app_username) and hmac.compare_digest(password, settings.app_password)
