"""Canonical hashes. Preserve rules_hash bytes exactly once computed from logged rules text."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

FORECAST_CANONICAL_KEYS = (
    "forecast_id",
    "market_id",
    "model_version",
    "rules_hash",
    "as_of",
    "expires_at",
    "p_yes",
    "p_low",
    "p_high",
    "sources",
    "thesis",
    "invalidation",
)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rules_hash_from_text(rules_text: str) -> str:
    return sha256_hex(rules_text)


def canonical_forecast_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    missing = [k for k in FORECAST_CANONICAL_KEYS if k not in payload]
    if missing:
        raise ValueError(f"forecast missing canonical keys: {missing}")
    sources = payload["sources"]
    normalized_sources = sorted(
        (
            {"url": str(s["url"]), "available_at": str(s["available_at"])}
            for s in sources
        ),
        key=lambda row: (row["url"], row["available_at"]),
    )
    return {
        "forecast_id": str(payload["forecast_id"]),
        "market_id": str(payload["market_id"]),
        "model_version": str(payload["model_version"]),
        "rules_hash": str(payload["rules_hash"]),
        "as_of": str(payload["as_of"]),
        "expires_at": str(payload["expires_at"]),
        "p_yes": payload["p_yes"],
        "p_low": payload["p_low"],
        "p_high": payload["p_high"],
        "sources": normalized_sources,
        "thesis": str(payload["thesis"]),
        "invalidation": str(payload["invalidation"]),
    }


def canonical_forecast_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        canonical_forecast_payload(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def forecast_content_hash(payload: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_forecast_json(payload))
