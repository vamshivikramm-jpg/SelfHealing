"""Login for the dashboard (operator sessions).

The dashboard's controls change the network (inject failures, launch
attacks, approve actions), so every page and API call needs a signed-in
operator. One account, configured by environment variables:

    VIEWER_USER        user name           (default: root)
    VIEWER_PASSWORD    password            (default: toor — lab default, change it
                                            on any shared network)
    VIEWER_SESSION_HOURS  session lifetime (default: 12)
    VIEWER_AUTH=0      switch the login off (development only)

Sessions are random 256-bit tokens kept in memory (only their SHA-256 is
stored) and sent as an HttpOnly, SameSite=Strict cookie; restarting the
dashboard signs everyone out. Passwords are compared in constant time and
an address is locked out for a minute after 5 failed attempts.
``/api/health`` stays public (the start/stop scripts and monitoring use it);
it exposes no controls.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional
from urllib.parse import quote

COOKIE = "sh_session"
PUBLIC_PATHS = ("/login", "/api/login", "/api/health", "/favicon.ico")
PUBLIC_PREFIXES = ("/static/",)
MAX_FAILURES = 5
LOCKOUT_S = 60.0


class Auth:
    def __init__(self, user: Optional[str] = None, password: Optional[str] = None, enabled: Optional[bool] = None,
                 session_hours: Optional[float] = None, clock=time.time):
        self.enabled = (os.environ.get("VIEWER_AUTH", "1") != "0") if enabled is None else enabled
        self.user = user if user is not None else os.environ.get("VIEWER_USER", "root")
        self._password = password if password is not None else os.environ.get("VIEWER_PASSWORD", "toor")
        hours = session_hours if session_hours is not None else float(os.environ.get("VIEWER_SESSION_HOURS", "12"))
        self.ttl_s = hours * 3600.0
        self._clock = clock
        self._sessions: Dict[str, dict] = {}            # sha256(token) -> {user, expires}
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    # -- credentials ---------------------------------------------------------------
    def check(self, user: str, password: str) -> bool:
        ok_user = hmac.compare_digest((user or "").encode(), self.user.encode())
        ok_pass = hmac.compare_digest((password or "").encode(), self._password.encode())
        return ok_user and ok_pass

    def locked_out(self, address: str) -> float:
        """Seconds until ``address`` may try again (0 if it may now)."""
        now = self._clock()
        with self._lock:
            q = self._failures[address]
            while q and now - q[0] > LOCKOUT_S:
                q.popleft()
            return max(0.0, LOCKOUT_S - (now - q[0])) if len(q) >= MAX_FAILURES else 0.0

    def record_failure(self, address: str) -> None:
        with self._lock:
            self._failures[address].append(self._clock())

    # -- sessions ------------------------------------------------------------------
    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def login(self, user: str, password: str, address: str) -> dict:
        """Returns {"ok": True, "token", "user", "expires"} or {"ok": False, "status", "detail"}."""
        wait = self.locked_out(address)
        if wait:
            return {"ok": False, "status": 429, "detail": f"Too many attempts. Try again in {int(wait) + 1} s."}
        if not self.check(user, password):
            self.record_failure(address)
            return {"ok": False, "status": 401, "detail": "Wrong user name or password."}
        with self._lock:
            self._failures.pop(address, None)
            token = secrets.token_urlsafe(32)
            expires = self._clock() + self.ttl_s
            self._sessions[self._key(token)] = {"user": user, "expires": expires}
            self._prune()
        return {"ok": True, "token": token, "user": user, "expires": expires}

    def session(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        with self._lock:
            s = self._sessions.get(self._key(token))
            if s is None:
                return None
            if s["expires"] < self._clock():
                self._sessions.pop(self._key(token), None)
                return None
            return dict(s)

    def logout(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._sessions.pop(self._key(token), None)

    def _prune(self) -> None:
        now = self._clock()
        for k in [k for k, s in self._sessions.items() if s["expires"] < now]:
            del self._sessions[k]

    # -- request gate ------------------------------------------------------------------
    @staticmethod
    def is_public(path: str) -> bool:
        return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)

    def gate(self, path: str, query: str, token: Optional[str]) -> Optional[dict]:
        """None if the request may proceed, else what to answer:
        {"status": 401, "json": ...} for API calls, {"redirect": url} for pages."""
        if not self.enabled or self.is_public(path) or self.session(token):
            return None
        if path.startswith("/api/"):
            return {"status": 401, "json": {"detail": "Sign in required."}}
        target = path + (f"?{query}" if query else "")
        return {"redirect": f"/login?next={quote(target, safe='')}"}


def safe_next(target: Optional[str]) -> str:
    """Only same-site paths are allowed after login (no open redirects)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target
