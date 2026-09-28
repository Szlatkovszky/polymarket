"""Offline NWS points URL and redirect tests. No network."""

from __future__ import annotations

import pytest

from research_lab.weather_source import (
    WeatherSourceError,
    _nws_coord,
    _nws_http_get,
    _points_url_from_station,
)

# Imported at module scope so the autouse network block, which replaces
# research_lab.weather_source._nws_http_get, does not replace this reference.
# Every call below passes an injected http_get and never opens a socket.


class _Hop:
    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body

    def json(self) -> dict:
        assert self._body is not None
        return self._body


def test_points_url_rounds_station_coordinates() -> None:
    url = _points_url_from_station(
        "https://api.weather.gov",
        {
            "geometry": {
                "type": "Point",
                "coordinates": [-73.88, 40.77917],
            }
        },
    )
    assert url == "https://api.weather.gov/points/40.7792,-73.88"
    assert _nws_coord("40.77920") == "40.7792"
    assert _nws_coord("-73.8800") == "-73.88"
    assert _nws_coord(-73.88) == "-73.88"


def test_same_host_301_is_followed() -> None:
    calls: list[str] = []

    def http_get(url: str, *, timeout: float) -> _Hop:
        del timeout
        calls.append(url)
        if url.endswith("40.77917,-73.88"):
            return _Hop(301, {"Location": "/points/40.7792,-73.88"})
        if url.endswith("40.7792,-73.88"):
            return _Hop(200, {}, {"properties": {"gridId": "OKX"}})
        raise AssertionError(url)

    body = _nws_http_get(
        "https://api.weather.gov/points/40.77917,-73.88",
        timeout=1.0,
        http_get=http_get,
    )
    assert body == {"properties": {"gridId": "OKX"}}
    assert calls == [
        "https://api.weather.gov/points/40.77917,-73.88",
        "https://api.weather.gov/points/40.7792,-73.88",
    ]


@pytest.mark.parametrize(
    "location",
    [
        "https://example.com/points/40.7792,-73.88",
        "https://weather.gov/points/40.7792,-73.88",
    ],
)
def test_off_host_redirect_rejected(location: str) -> None:
    calls: list[str] = []

    def http_get(url: str, *, timeout: float) -> _Hop:
        del timeout
        calls.append(url)
        return _Hop(301, {"Location": location})

    with pytest.raises(WeatherSourceError, match="off-host"):
        _nws_http_get(
            "https://api.weather.gov/points/40.77917,-73.88",
            timeout=1.0,
            http_get=http_get,
        )
    assert calls == ["https://api.weather.gov/points/40.77917,-73.88"]


def test_http_redirect_on_same_host_rejected() -> None:
    calls: list[str] = []

    def http_get(url: str, *, timeout: float) -> _Hop:
        del timeout
        calls.append(url)
        return _Hop(301, {"Location": "http://api.weather.gov/points/40.7792,-73.88"})

    with pytest.raises(WeatherSourceError, match="HTTPS"):
        _nws_http_get(
            "https://api.weather.gov/points/40.77917,-73.88",
            timeout=1.0,
            http_get=http_get,
        )
    assert calls == ["https://api.weather.gov/points/40.77917,-73.88"]


def test_at_most_three_redirects() -> None:
    calls: list[str] = []

    def http_get(url: str, *, timeout: float) -> _Hop:
        del timeout
        calls.append(url)
        step = len(calls)
        if step <= 3:
            return _Hop(301, {"Location": f"/points/hop-{step}"})
        return _Hop(200, {}, {"ok": True, "hop": step})

    body = _nws_http_get(
        "https://api.weather.gov/points/start",
        timeout=1.0,
        http_get=http_get,
    )
    assert body == {"ok": True, "hop": 4}
    assert len(calls) == 4

    def always_redirect(url: str, *, timeout: float) -> _Hop:
        del url, timeout
        return _Hop(301, {"Location": "/points/again"})

    with pytest.raises(WeatherSourceError, match="too many redirects"):
        _nws_http_get(
            "https://api.weather.gov/points/start",
            timeout=1.0,
            http_get=always_redirect,
        )
