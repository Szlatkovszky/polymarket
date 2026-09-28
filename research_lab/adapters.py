"""GET-only public Polymarket adapters (Gamma + CLOB).

Default source is recorded fixtures so CI and local tests never require network.
Optional live GET probe is behind POLYMARKET_ALLOW_NETWORK and never places orders.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from research_lab.discovery import classify_weather_market, discovery_skip_reason
from research_lab.timeutil import Clock, SystemUTCClock, as_utc

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

GAMMA_HOSTS = frozenset({"gamma-api.polymarket.com"})
CLOB_HOSTS = frozenset({"clob.polymarket.com", "clob-staging.polymarket.com"})

# Top GET /markets is politics-heavy. Weather city dailies are found via search
# plus active/closed catalog pages. Callers still filter tradable books.
WEATHER_SEARCH_QUERIES: tuple[str, ...] = (
    "highest temperature",
    "daily maximum temperature",
    "NOAA temperature",
)
MAX_SEARCH_PAGES = 8
GAMMA_PAGE_SIZE = 50
MAX_CATALOG_PAGES = 12


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

    def list_markets(self, limit: int = 20, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self._markets[:limit])

    def get_market(self, market_id: str) -> dict[str, Any]:
        for row in self._markets:
            if str(row["id"]) == str(market_id):
                return dict(row)
        raise AdapterError(f"fixture market not found: {market_id}")


def fixture_book_timestamp_ms(now: datetime) -> str:
    """CLOB-style millisecond epoch so receive age, not file mtime, governs freshness."""
    return str(int(as_utc(now).timestamp() * 1000))


class FixtureClob:
    """Recorded CLOB books. Server ``timestamp`` is refreshed at fetch to ``clock.now``.

    risk-v2 ``stale_book`` is max(receive age, server book time). Fixture JSON is a
    frozen demo tape, so a recorded timestamp hours ago would refuse every paper
    fill. NetworkClob does not rewrite timestamps.
    """

    source_name = "fixtures"

    def __init__(
        self,
        books_path: Path | None = None,
        fees_path: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.clock = clock or SystemUTCClock()
        self.books = json.loads(
            (books_path or FIXTURE_DIR / "clob_books.json").read_text(encoding="utf-8")
        )
        self.fees = json.loads(
            (fees_path or FIXTURE_DIR / "clob_fees.json").read_text(encoding="utf-8")
        )

    def get_book(self, token_id: str) -> dict[str, Any]:
        if token_id not in self.books:
            raise AdapterError(f"fixture book not found: {token_id}")
        book = dict(self.books[token_id])
        book["timestamp"] = fixture_book_timestamp_ms(self.clock.now())
        return book

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
        self.last_discovery_skips: list[dict[str, Any]] = []

    def list_markets(self, limit: int = 20, **kwargs: Any) -> list[dict[str, Any]]:
        """GET-only weather discovery. Never places orders.

        Keyword ``/public-search`` is paged first. Closed and non-tradable rows
        do not consume ``limit``. ``/events`` and ``/markets`` are then paged
        with ``active=true`` and ``closed=false`` so later offsets can still
        reach open daily-high markets. Skip reasons land on
        ``last_discovery_skips``.
        """

        self.last_discovery_skips = []
        cap = max(1, min(int(limit), 200))
        found: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        on_http = kwargs.get("on_http")
        filter_kwargs = {
            "now": kwargs.get("now"),
            "min_event_date": kwargs.get("min_event_date"),
            "use_station_today": bool(kwargs.get("use_station_today")),
            "city": kwargs.get("city"),
            "station": kwargs.get("station"),
        }

        def budget_ok() -> bool:
            if on_http is None:
                return True
            if on_http():
                return True
            if not any(
                row.get("reason") == "research_call_ceiling" for row in self.last_discovery_skips
            ):
                self.last_discovery_skips.append(
                    {
                        "market_id": "",
                        "reason": "research_call_ceiling",
                        "question": None,
                    }
                )
            return False

        def consider(row: dict[str, Any], *, require_weather: bool) -> None:
            if len(found) >= cap:
                return
            market_id = str(row.get("id") or "").strip()
            if not market_id or market_id in seen:
                return
            if require_weather and not classify_weather_market(row).weather_like:
                return
            seen.add(market_id)
            reason = discovery_skip_reason(row, **filter_kwargs)
            if reason:
                self.last_discovery_skips.append(
                    {
                        "market_id": market_id,
                        "reason": reason,
                        "question": None if row.get("question") is None else str(row.get("question")),
                    }
                )
                return
            found[market_id] = row

        for query in WEATHER_SEARCH_QUERIES:
            if len(found) >= cap or not budget_ok():
                break
            try:
                _collect_search_markets(
                    base_url=self.base_url,
                    query=query,
                    timeout=self.timeout,
                    consider=lambda row: consider(row, require_weather=False),
                    cap_reached=lambda: len(found) >= cap,
                    budget_ok=budget_ok,
                )
            except AdapterError as exc:
                self.last_discovery_skips.append(
                    {
                        "market_id": f"public-search:{query}",
                        "reason": "gamma_page_error",
                        "question": str(exc)[:180],
                    }
                )
                break
        for path in ("events", "markets"):
            if len(found) >= cap or not budget_ok():
                break
            _collect_catalog_pages(
                base_url=self.base_url,
                path=path,
                timeout=self.timeout,
                consider=lambda row: consider(row, require_weather=True),
                cap_reached=lambda: len(found) >= cap,
                budget_ok=budget_ok,
                skips=self.last_discovery_skips,
            )
        return list(found.values())[:cap]

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
        # Live book age uses the exchange timestamp as returned. Do not rewrite.
        return payload

    def get_fee_bps(self, token_id: str) -> int | None:
        """``GET /fee-rate`` ``base_fee`` in basis points.

        https://docs.polymarket.com/api-reference/market-data/get-fee-rate
        This is not ``feeSchedule.rate``. The paper gate uses Gamma
        ``feesEnabled`` + ``feeSchedule`` and does not call this method.
        """

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


def markets_from_search_payload(payload: Any) -> list[dict[str, Any]]:
    """Flatten Gamma ``/public-search`` events[].markets (and top-level markets)."""

    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        raise AdapterError("unexpected Gamma search payload")
    rows: list[dict[str, Any]] = []
    top = payload.get("markets")
    if isinstance(top, list):
        rows.extend(row for row in top if isinstance(row, dict))
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        nested = event.get("markets")
        if isinstance(nested, list):
            rows.extend(row for row in nested if isinstance(row, dict))
    return rows


def search_payload_has_more(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    pagination = payload.get("pagination") or {}
    if isinstance(pagination, dict) and "hasMore" in pagination:
        return bool(pagination.get("hasMore"))
    return False


def markets_from_events_payload(payload: Any) -> list[dict[str, Any]]:
    """Flatten ``GET /events`` (nested markets) or a bare ``GET /markets`` list."""

    if isinstance(payload, dict):
        events = payload.get("events") or payload.get("data") or payload.get("markets") or []
    elif isinstance(payload, list):
        events = payload
    else:
        return []
    rows: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        nested = event.get("markets")
        if isinstance(nested, list) and any(isinstance(item, dict) for item in nested):
            rows.extend(item for item in nested if isinstance(item, dict))
            continue
        if event.get("question") or event.get("clobTokenIds") or event.get("clob_token_ids"):
            rows.append(event)
    return rows


def _collect_search_markets(
    *,
    base_url: str,
    query: str,
    timeout: float,
    consider: Any,
    cap_reached: Any,
    budget_ok: Any,
) -> None:
    page = 1
    seen_ids: set[str] = set()
    while page <= MAX_SEARCH_PAGES and not cap_reached():
        if page > 1 and not budget_ok():
            return
        payload = _http_get(
            urljoin(base_url + "/", "public-search"),
            params={
                "q": query,
                "limit_per_type": "50",
                "page": str(page),
                "search_tags": "false",
                "search_profiles": "false",
                "events_status": "active",
            },
            allowed_hosts=GAMMA_HOSTS,
            timeout=timeout,
        )
        rows = markets_from_search_payload(payload)
        fresh = 0
        for row in rows:
            market_id = str(row.get("id") or "").strip()
            if not market_id or market_id in seen_ids:
                continue
            seen_ids.add(market_id)
            fresh += 1
            consider(row)
            if cap_reached():
                return
        if not rows or not search_payload_has_more(payload) or fresh == 0:
            return
        page += 1


def _collect_catalog_pages(
    *,
    base_url: str,
    path: str,
    timeout: float,
    consider: Any,
    cap_reached: Any,
    budget_ok: Any,
    skips: list[dict[str, Any]],
) -> None:
    """Page ``/events`` or ``/markets`` with active/closed filters and offset."""

    offset = 0
    for _page_index in range(MAX_CATALOG_PAGES):
        if cap_reached():
            return
        if offset > 0 and not budget_ok():
            return
        try:
            payload = _http_get(
                urljoin(base_url + "/", path),
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": str(GAMMA_PAGE_SIZE),
                    "offset": str(offset),
                },
                allowed_hosts=GAMMA_HOSTS,
                timeout=timeout,
            )
        except AdapterError as exc:
            skips.append(
                {
                    "market_id": f"{path}:{offset}",
                    "reason": "gamma_page_error",
                    "question": str(exc)[:180],
                }
            )
            return
        rows = markets_from_events_payload(payload)
        if not rows:
            return
        for row in rows:
            consider(row)
            if cap_reached():
                return
        if len(rows) < GAMMA_PAGE_SIZE:
            return
        offset += GAMMA_PAGE_SIZE


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
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise AdapterError(f"GET {url} failed: {exc}") from exc
    if response.status_code != 200:
        snippet = (response.text or "").replace("\n", " ")[:240].strip()
        extra = f" {snippet}" if snippet else ""
        raise AdapterError(f"GET {url} -> {response.status_code}{extra}")
    return response.json()
