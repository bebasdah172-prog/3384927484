"""Runtime client-IP access policy and per-IP request limits."""
from __future__ import annotations

import ipaddress
import json
import math
import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional


_DEFAULT_RATE = int(os.getenv("DUCKAI_RATE_LIMIT_PER_MINUTE", "60"))
_POLICY_PATH = Path(__file__).resolve().with_name("runtime-policy.json")
_MAX_RULES = 256
_MAX_RATE = 10_000


def _normalize_networks(values: list[str], field: str) -> tuple[list[str], list]:
    if not isinstance(values, list):
        raise ValueError(f"{field} must be a list of IP addresses or CIDRs")
    if len(values) > _MAX_RULES:
        raise ValueError(f"{field} cannot contain more than {_MAX_RULES} entries")

    normalized: list[str] = []
    networks = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise ValueError(f"{field} entries must be strings")
        item = item.strip()
        if not item or item.startswith("#"):
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid IP address or CIDR in {field}: {item}") from exc
        canonical = str(network)
        if canonical not in seen:
            seen.add(canonical)
            normalized.append(canonical)
            networks.append(network)
    return normalized, networks


def client_ip_from_scope(scope: dict) -> Optional[str]:
    """Resolve the caller address behind a trusted local/reverse-proxy hop.

    Replit and other reverse proxies connect to the app from private network
    addresses and supply X-Real-IP or X-Forwarded-For. For direct public peers,
    ignore forwarding headers so callers cannot spoof their address.
    """
    peer = None
    client = scope.get("client")
    if client and client[0]:
        try:
            peer = ipaddress.ip_address(client[0])
        except ValueError:
            peer = None

    headers = {
        key.lower(): value.decode("latin-1").strip()
        for key, value in scope.get("headers", [])
    }
    trusted_proxy_peer = bool(
        peer and (peer.is_private or peer.is_loopback or peer.is_link_local)
    )

    if trusted_proxy_peer:
        real_ip = headers.get(b"x-real-ip")
        if real_ip:
            try:
                return str(ipaddress.ip_address(real_ip))
            except ValueError:
                pass

        forwarded = headers.get(b"x-forwarded-for", "")
        if forwarded:
            # X-Forwarded-For is conventionally ordered client, proxy, proxy.
            for candidate in forwarded.split(","):
                try:
                    return str(ipaddress.ip_address(candidate.strip()))
                except ValueError:
                    continue

    return str(peer) if peer else None


class SecurityPolicy:
    """A persisted, non-secret policy with an in-memory sliding-window limiter."""

    def __init__(
        self,
        path: Optional[Path] = _POLICY_PATH,
        clock: Callable[[], float] = time.monotonic,
        default_rate: int = _DEFAULT_RATE,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.RLock()
        self._windows: dict[str, deque[float]] = {}
        self._last_prune = 0.0
        self._settings = {
            "allowlist": [],
            "blocklist": [],
            "requests_per_minute": self._validate_rate(default_rate),
        }
        self._allow_networks = []
        self._block_networks = []
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._settings, self._allow_networks, self._block_networks = self._validated(raw)
            except Exception as exc:
                raise RuntimeError(f"cannot load security policy from {self.path}: {exc}") from exc

    @staticmethod
    def _validate_rate(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("requests_per_minute must be an integer")
        if value < 0 or value > _MAX_RATE:
            raise ValueError(f"requests_per_minute must be between 0 and {_MAX_RATE}")
        return value

    @classmethod
    def _validated(cls, raw: dict) -> tuple[dict, list, list]:
        if not isinstance(raw, dict):
            raise ValueError("policy must be a JSON object")
        allowlist, allow_networks = _normalize_networks(raw.get("allowlist", []), "allowlist")
        blocklist, block_networks = _normalize_networks(raw.get("blocklist", []), "blocklist")
        rate = cls._validate_rate(raw.get("requests_per_minute", _DEFAULT_RATE))
        return (
            {
                "allowlist": allowlist,
                "blocklist": blocklist,
                "requests_per_minute": rate,
            },
            allow_networks,
            block_networks,
        )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "allowlist": list(self._settings["allowlist"]),
                "blocklist": list(self._settings["blocklist"]),
                "requests_per_minute": self._settings["requests_per_minute"],
            }

    def update(self, allowlist: list[str], blocklist: list[str], requests_per_minute: int) -> dict:
        settings, allow_networks, block_networks = self._validated({
            "allowlist": allowlist,
            "blocklist": blocklist,
            "requests_per_minute": requests_per_minute,
        })
        with self._lock:
            previous = self._settings
            previous_allow = self._allow_networks
            previous_block = self._block_networks
            self._settings = settings
            self._allow_networks = allow_networks
            self._block_networks = block_networks
            try:
                if self.path:
                    self._save()
            except OSError:
                self._settings = previous
                self._allow_networks = previous_allow
                self._block_networks = previous_block
                raise
            self._windows.clear()
            return self.snapshot()

    def _save(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".runtime-policy-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _matches(ip: Optional[ipaddress.IPv4Address | ipaddress.IPv6Address], networks: list) -> bool:
        if ip is None:
            return False
        return any(ip.version == network.version and ip in network for network in networks)

    def check(self, client_ip: Optional[str]) -> Optional[dict]:
        """Return a rejection payload, or None when the request is allowed."""
        try:
            parsed_ip = ipaddress.ip_address(client_ip) if client_ip else None
        except ValueError:
            parsed_ip = None

        key = str(parsed_ip) if parsed_ip else "unknown"
        now = self._clock()
        with self._lock:
            if self._matches(parsed_ip, self._block_networks):
                return {"status": 403, "code": "ip_blocked", "message": "client IP is blocked"}
            if self._allow_networks and not self._matches(parsed_ip, self._allow_networks):
                return {
                    "status": 403,
                    "code": "ip_not_allowed",
                    "message": "client IP is not on the allowlist",
                }

            limit = self._settings["requests_per_minute"]
            if not limit:
                return None

            window = self._windows.setdefault(key, deque())
            cutoff = now - 60.0
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= limit:
                retry_after = max(1, math.ceil(window[0] + 60.0 - now))
                return {
                    "status": 429,
                    "code": "rate_limit_exceeded",
                    "message": f"rate limit exceeded ({limit} requests per minute)",
                    "retry_after": retry_after,
                }
            window.append(now)

            if now - self._last_prune > 300:
                for old_key, old_window in list(self._windows.items()):
                    while old_window and old_window[0] <= cutoff:
                        old_window.popleft()
                    if not old_window:
                        self._windows.pop(old_key, None)
                self._last_prune = now
        return None


POLICY = SecurityPolicy()
