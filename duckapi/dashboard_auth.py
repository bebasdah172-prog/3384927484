"""Cookie-session authentication for the browser dashboard."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import deque

from fastapi import HTTPException, Request, Response

from security import client_ip_from_scope

DASHBOARD_USERNAME = os.getenv("DUCKAI_DASHBOARD_USERNAME", "admin").strip() or "admin"
DASHBOARD_PASSWORD = os.getenv("DUCKAI_DASHBOARD_PASSWORD", "")
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_COOKIE = "duckapi_dashboard_session"

# SESSION_SECRET is managed outside the source tree. The ephemeral fallback
# keeps local development usable but invalidates sessions whenever the process
# restarts.
_SESSION_KEY = (
    os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32)
).encode("utf-8")

_LOGIN_WINDOW_SECONDS = 5 * 60
_LOGIN_MAX_FAILURES = 10
_login_failures: dict[str, deque[float]] = {}
_login_lock = threading.Lock()


def _signature(payload: str) -> str:
    return hmac.new(_SESSION_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _valid_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, supplied_signature = token.rsplit(".", 1)
    try:
        expiry_text, nonce = payload.split(":", 1)
        expiry = int(expiry_text)
    except (ValueError, TypeError):
        return False
    if not nonce or expiry <= int(time.time()):
        return False
    return hmac.compare_digest(_signature(payload), supplied_signature)


def is_authenticated(request: Request) -> bool:
    return _valid_token(request.cookies.get(SESSION_COOKIE))


def require_dashboard_session(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="dashboard login required")


def _secure_cookie(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def set_session_cookie(response: Response, request: Request) -> None:
    payload = f"{int(time.time()) + SESSION_TTL_SECONDS}:{secrets.token_urlsafe(18)}"
    response.set_cookie(
        key=SESSION_COOKIE,
        value=f"{payload}.{_signature(payload)}",
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=_secure_cookie(request),
        samesite="strict",
    )


def _failure_window(client_ip: str) -> tuple[bool, int]:
    now = time.monotonic()
    with _login_lock:
        attempts = _login_failures.setdefault(client_ip, deque())
        while attempts and attempts[0] <= now - _LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= _LOGIN_MAX_FAILURES:
            return False, max(1, int(_LOGIN_WINDOW_SECONDS - (now - attempts[0])))
        return True, 0


def authenticate(request: Request, username: str, password: str) -> bool:
    client_ip = client_ip_from_scope(request.scope) or "unknown"
    allowed, retry_after = _failure_window(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="too many failed dashboard logins",
            headers={"Retry-After": str(retry_after)},
        )

    username_ok = hmac.compare_digest(username.encode("utf-8"), DASHBOARD_USERNAME.encode("utf-8"))
    password_ok = hmac.compare_digest(password.encode("utf-8"), DASHBOARD_PASSWORD.encode("utf-8"))
    if not (username_ok and password_ok):
        now = time.monotonic()
        with _login_lock:
            attempts = _login_failures.setdefault(client_ip, deque())
            attempts.append(now)
        return False

    with _login_lock:
        _login_failures.pop(client_ip, None)
    return True
