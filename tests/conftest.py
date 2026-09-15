"""Block outbound adapter HTTP in default pytest. Network tests must opt in.

Do not patch ``httpx.Client`` globally: FastAPI's TestClient subclasses it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _block_adapter_network(request, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("network"):
        return

    def _fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("network blocked in default pytest")

    monkeypatch.setattr("research_lab.weather_source._nws_http_get", _fail)
    monkeypatch.setattr("research_lab.adapters._http_get", _fail)
