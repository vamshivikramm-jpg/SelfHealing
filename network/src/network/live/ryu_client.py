"""REST client for the self-healing Ryu app (standard library only)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import quote

DEFAULT_URL = os.environ.get("RYU_API", "http://ryu-controller:8080")


class RyuError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Ryu API {status}: {message}")
        self.status = status


class RyuClient:
    def __init__(self, base_url: str = DEFAULT_URL, timeout_s: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read() or b"{}").get("error", exc.reason)
            except ValueError:
                message = exc.reason
            raise RyuError(exc.code, str(message)) from None
        except (urllib.error.URLError, OSError) as exc:
            raise RyuError(0, f"controller unreachable: {exc}") from None
        return json.loads(raw) if raw else None

    # convenience wrappers ------------------------------------------------------
    def health(self) -> dict:
        return self.request("GET", "/sh/health")

    def topology(self) -> dict:
        return self.request("GET", "/sh/topology")

    def set_topology(self, filename: str) -> dict:
        return self.request("PUT", "/sh/config", {"topology": filename})

    def links(self) -> dict:
        return self.request("GET", "/sh/stats/links")

    def ports(self) -> dict:
        return self.request("GET", "/sh/stats/ports")

    def routes(self) -> dict:
        return self.request("GET", "/sh/routes")

    def pin(self, dst_ip: str, switches) -> dict:
        return self.request("PUT", f"/sh/routes/{quote(dst_ip)}", {"path": list(switches)})

    def unpin(self, dst_ip: str) -> dict:
        return self.request("DELETE", f"/sh/routes/{quote(dst_ip)}")

    def blocks(self) -> list:
        return self.request("GET", "/sh/blocks")

    def block(self, match: dict, reason: str = "") -> dict:
        return self.request("POST", "/sh/blocks", {"match": match, "reason": reason})

    def unblock(self, block_id: int) -> dict:
        return self.request("DELETE", f"/sh/blocks/{int(block_id)}")
