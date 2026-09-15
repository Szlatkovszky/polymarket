"""Forecast envelope validation. Does not size trades or change risk limits."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import jsonschema

from research_lab.hashing import canonical_forecast_json, forecast_content_hash
from research_lab.money import D
from research_lab.timeutil import parse_utc

def _schema_dir() -> Path:
    pkg = Path(__file__).resolve().parent / "schemas"
    root = Path(__file__).resolve().parent.parent / "schemas"
    if (pkg / "forecast.schema.json").exists():
        return pkg
    return root


_SCHEMA_DIR = _schema_dir()
_FORECAST_SCHEMA = json.loads((_SCHEMA_DIR / "forecast.schema.json").read_text(encoding="utf-8"))


class ForecastValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedForecast:
    payload: dict[str, Any]
    canonical_json: str
    content_hash: str
    forecast_id: str
    market_id: str
    model_version: str
    rules_hash: str
    p_yes: Decimal
    p_low: Decimal
    p_high: Decimal
    as_of_iso: str
    expires_at_iso: str

    def expired(self, now) -> bool:
        return parse_utc(self.expires_at_iso) <= now


def validate_forecast_dict(payload: Mapping[str, Any]) -> ValidatedForecast:
    try:
        jsonschema.Draft202012Validator(_FORECAST_SCHEMA).validate(payload)
    except jsonschema.ValidationError as exc:
        raise ForecastValidationError(exc.message) from exc

    p_yes = D(payload["p_yes"])
    p_low = D(payload["p_low"])
    p_high = D(payload["p_high"])
    if not (p_low <= p_yes <= p_high):
        raise ForecastValidationError("require p_low <= p_yes <= p_high")

    as_of = parse_utc(str(payload["as_of"]))
    expires = parse_utc(str(payload["expires_at"]))
    if expires <= as_of:
        raise ForecastValidationError("expires_at must be after as_of")

    for source in payload["sources"]:
        available = parse_utc(str(source["available_at"]))
        if available > as_of:
            raise ForecastValidationError(
                "evidence available_at must be <= as_of (no look-ahead)"
            )

    body = dict(payload)
    canonical = canonical_forecast_json(body)
    return ValidatedForecast(
        payload=json.loads(canonical),
        canonical_json=canonical,
        content_hash=forecast_content_hash(body),
        forecast_id=str(payload["forecast_id"]),
        market_id=str(payload["market_id"]),
        model_version=str(payload["model_version"]),
        rules_hash=str(payload["rules_hash"]),
        p_yes=p_yes,
        p_low=p_low,
        p_high=p_high,
        as_of_iso=as_of.isoformat(),
        expires_at_iso=expires.isoformat(),
    )


def validate_decision_envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    if action == "ABSTAIN":
        if payload.get("forecast") is not None:
            raise ForecastValidationError("ABSTAIN must not include a forecast (no import)")
        return dict(payload)
    if action == "PROPOSE":
        if "forecast" not in payload:
            raise ForecastValidationError("PROPOSE requires a forecast object")
        forecast = validate_forecast_dict(payload["forecast"])
        out = dict(payload)
        out["forecast"] = forecast.payload
        return out
    raise ForecastValidationError("action must be ABSTAIN or PROPOSE")
