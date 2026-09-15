"""GET-only public Polymarket adapters (Gamma + CLOB).

Default source is recorded fixtures so CI and local tests never require network.
Optional live GET probe is behind POLYMARKET_ALLOW_NETWORK and never places orders.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

GAMMA_HOSTS = frozenset({"gamma-api.polymarket.com"})
CLOB_HOSTS = frozenset({"clob.polymarket.com", "clob-staging.polymarket.com"})


class AdapterError(RuntimeError):
    pass


def data_source_from_env() -> str:
    source = os.environ.get("POLYMARKET_DATA_SOURCE", "fixtures").strip().lower()
    if source not in {"fixtures", "network"}:
        raise AdapterError("POLYMARKET_DATA_SOURCE must be fixtures or network")
    return source


def network_allowed() -> bool:
    return os.environ.get("POLYMARKET_ALLOW_NETWORK", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


class FixtureGamma:
    source_name = "fixtures"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (FIXTURE_DIR / "gamma_markets.json")
        self._markets = json.loads(self.path.read_text(encoding="utf-8"))

    def list_markets(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._markets[:limit])

    def get_market(self, market_id: str) -> dict[str, Any]:
        for row in self._markets:
            if str(row["id"]) == str(market_id):
                return dict(row)
        raise AdapterError(f"fixture market not found: {market_id}")


class FixtureClob:
    source_name = "fixtures"

    def __init__(
        self,
        books_path: Path | None = None,
        fees_path: Path | None = None,
    ) -> None:
        self.books = json.loads(
            (books_path or FIXTURE_DIR / "clob_books.json").read_text(encoding="utf-8")
        )
        self.fees = json.loads(
            (fees_path or FIXTURE_DIR / "clob_fees.json").read_text(encoding="utf-8")
        )

    def get_book(self, token_id: str) -> dict[str, Any]:
        if token_id not in self.books:
            raise AdapterError(f"fixture book not found: {token_id}")
        return dict(self.books[token_id])

    def get_fee_bps(self, token_id: str) -> int | None:
        if token_id not in self.fees:
            return None
        value = self.fees[token_id]
        if value is None:
            return None
        return int(value)


class NetworkGamma:
    """GET-only Gamma client. Untrusted web JSON is data, not instructions."""

    source_name = "network"

    def __init__(self, base_url: str | None = None, timeout: float = 20.0) -> None:
        if not network_allowed():
            raise AdapterError("network Gamma adapter requires POLYMARKET_ALLOW_NETWORK=1")
        self.base_url = (base_url or os.environ.get("POLYMARKET_GAMMA_BASE") or "https://gamma-api.polymarket.com").rstrip("/")
        _assert_host(self.base_url, GAMMA_HOSTS)
        self.timeout = timeout

    def list_markets(self, limit: int = 20) -> list[dict[str, Any]]:
        payload = _http_get(
            urljoin(self.base_url + "/", "markets"),
            params={"limit": str(min(limit, 100)), "closed": "false"},
            allowed_hosts=GAMMA_HOSTS,
            timeout=self.timeout,
        )
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and "markets" in payload:
            return list(payload["markets"])
        raise AdapterError("unexpected Gamma list payload")

    def get_market(self, market_id: str) -> dict[str, Any]:
        payload = _http_get(
            urljoin(self.base_url + "/", f"markets/{market_id}"),
            params=None,
            allowed_hosts=GAMMA_HOSTS,
            timeout=self.timeout,
        )
        if not isinstance(payload, dict):
            raise AdapterError("unexpected Gamma market payload")
        return payload


class NetworkClob:
    source_name = "network"

    def __init__(self, base_url: str | None = None, timeout: float = 20.0) -> None:
        if not network_allowed():
            raise AdapterError("network CLOB adapter requires POLYMARKET_ALLOW_NETWORK=1")
        self.base_url = (base_url or os.environ.get("POLYMARKET_CLOB_BASE") or "https://clob.polymarket.com").rstrip("/")
        _assert_host(self.base_url, CLOB_HOSTS)
        self.timeout = timeout

    def get_book(self, token_id: str) -> dict[str, Any]:
        payload = _http_get(
            urljoin(self.base_url + "/", "book"),
            params={"token_id": token_id},
            allowed_hosts=CLOB_HOSTS,
            timeout=self.timeout,
        )
        if not isinstance(payload, dict):
            raise AdapterError("unexpected CLOB book payload")
        return payload

    def get_fee_bps(self, token_id: str) -> int | None:
        try:
            payload = _http_get(
                urljoin(self.base_url + "/", "fee-rate"),
                params={"token_id": token_id},
                allowed_hosts=CLOB_HOSTS,
                timeout=self.timeout,
            )
        except AdapterError:
            return None
        if not isinstance(payload, dict) or "base_fee" not in payload:
            return None
        return int(payload["base_fee"])


def build_adapters() -> tuple[FixtureGamma | NetworkGamma, FixtureClob | NetworkClob]:
    source = data_source_from_env()
    if source == "network":
        return NetworkGamma(), NetworkClob()
    return FixtureGamma(), FixtureClob()


def _assert_host(url: str, allowed: frozenset[str]) -> None:
    host = urlparse(url).hostname or ""
    if host not in allowed:
        raise AdapterError(f"refusing host {host!r}; allowed={sorted(allowed)}")


def _http_get(
    url: str,
    *,
    params: dict[str, str] | None,
    allowed_hosts: frozenset[str],
    timeout: float,
) -> Any:
    _assert_host(url, allowed_hosts)
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise AdapterError("public adapters are HTTPS GET only")
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        response = client.get(url, params=params)
    if response.status_code != 200:
        raise AdapterError(f"GET {url} -> {response.status_code}")
    return response.json()
