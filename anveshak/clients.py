"""API clients, roles and per-client limits (ADR-0023).

Each client (the Sahyog portal, an agency's investigators, an auditor) has its own key. Only
the key's sha256 is stored. A client has roles (which map to permissions), an optional
`agency_id` that scopes which cases it can see, an optional IP allowlist and a rate limit.

    clients:
      - client_id: sahyog-portal
        key_sha256: 5e88...          # sha256 of the key; create with `anveshak clients add`
        roles: [sahyog]
        ip_allowlist: [10.20.0.0/16]
        rate_limit_per_minute: 600
      - client_id: dl-ifso-investigators
        key_sha256: 9f86...
        roles: [investigator]
        agency_id: DL-IFSO

Without a clients file, the legacy single ANVESHAK_API_TOKEN (if set) is one `admin` client;
with neither, the API is open and every caller is the local `admin` (development only — the
/v1/meta response says so).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections import deque
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # the Sahyog portal: submits reported wallets, reads every case's recommendations,
    # reports what happened to them, records VASP replies, screens addresses
    "sahyog": frozenset({"case:create", "case:read_all", "recommendation:status", "attest", "screen", "directory:write", "intel:read", "alerts:read"}),
    # an agency's investigators: their own agency's cases only
    "investigator": frozenset({"case:create", "case:read", "screen", "intel:read", "alerts:read", "watchlist"}),
    # a supervisor in an agency: investigator + recording VASP replies and outcomes (+ standalone approvals)
    "supervisor": frozenset({"case:create", "case:read", "screen", "intel:read", "alerts:read", "watchlist", "attest", "recommendation:status", "approve"}),
    # read-only oversight across agencies, including the audit log
    "auditor": frozenset({"case:read_all", "intel:read", "alerts:read", "audit:read"}),
    "admin": frozenset({"*"}),
}


class ApiClient(BaseModel):
    client_id: str = Field(min_length=2, max_length=60)
    key_sha256: str = Field(min_length=64, max_length=64)
    roles: tuple[str, ...]
    agency_id: str | None = Field(default=None, max_length=60)
    ip_allowlist: tuple[str, ...] = ()
    rate_limit_per_minute: int = Field(default=120, ge=1, le=100_000)

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [r for r in v if r not in ROLE_PERMISSIONS]
        if unknown or not v:
            raise ValueError(f"unknown or missing roles {unknown}; choose from {sorted(ROLE_PERMISSIONS)}")
        return v

    @field_validator("ip_allowlist")
    @classmethod
    def _valid_networks(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for net in v:
            ipaddress.ip_network(net, strict=False)
        return v

    @model_validator(mode="after")
    def _scoped_clients_need_an_agency(self) -> ApiClient:
        if self.can("case:read") and not self.sees_all_agencies and not self.agency_id:
            raise ValueError(f"client {self.client_id}: roles {list(self.roles)} are agency-scoped and need agency_id")
        return self

    @property
    def permissions(self) -> frozenset[str]:
        return frozenset().union(*(ROLE_PERMISSIONS[r] for r in self.roles))

    def can(self, permission: str) -> bool:
        p = self.permissions
        return "*" in p or permission in p

    @property
    def sees_all_agencies(self) -> bool:
        return self.can("case:read_all")

    def ip_allowed(self, host: str | None) -> bool:
        if not self.ip_allowlist:
            return True
        try:
            ip = ipaddress.ip_address(host or "")
        except ValueError:
            return False
        return any(ip in ipaddress.ip_network(net, strict=False) for net in self.ip_allowlist)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


LOCAL_ADMIN = ApiClient(client_id="local-admin", key_sha256="0" * 64, roles=("admin",), rate_limit_per_minute=100_000)


class ClientRegistry:
    def __init__(self, clients: list[ApiClient], open_mode: bool = False):
        ids = [c.client_id for c in clients]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate client_id in API clients")
        self._by_hash = {c.key_sha256: c for c in clients}
        self.clients = clients
        self.open_mode = open_mode

    @classmethod
    def from_settings(cls, clients_file: Path | None, legacy_token: str) -> ClientRegistry:
        if clients_file and Path(clients_file).exists():
            doc = yaml.safe_load(Path(clients_file).read_text(encoding="utf-8")) or {}
            return cls([ApiClient.model_validate(c) for c in doc.get("clients") or []])
        if legacy_token:
            return cls([ApiClient(client_id="token", key_sha256=key_hash(legacy_token), roles=("admin",), rate_limit_per_minute=100_000)])
        return cls([], open_mode=True)

    def authenticate(self, key: str | None) -> ApiClient | None:
        if self.open_mode:
            return LOCAL_ADMIN
        if not key:
            return None
        digest = key_hash(key)
        for stored, client in self._by_hash.items():
            if hmac.compare_digest(stored, digest):
                return client
        return None


def add_client(path: Path, client_id: str, roles: list[str], agency_id: str | None, ip_allowlist: list[str], rate_limit: int) -> str:
    """Create a client with a fresh random key; returns the key (shown once, never stored)."""
    key = secrets.token_urlsafe(32)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    doc = doc or {"clients": []}
    if any(c.get("client_id") == client_id for c in doc["clients"]):
        raise ValueError(f"client {client_id} already exists")
    client = ApiClient(client_id=client_id, key_sha256=key_hash(key), roles=tuple(roles), agency_id=agency_id, ip_allowlist=tuple(ip_allowlist), rate_limit_per_minute=rate_limit)
    doc["clients"].append(client.model_dump(mode="json", exclude_defaults=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    return key


class RateLimiter:
    """Sliding one-minute window per client, in process memory (per API instance)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, client: ApiClient) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(client.client_id, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= client.rate_limit_per_minute:
                return False
            q.append(now)
            return True
