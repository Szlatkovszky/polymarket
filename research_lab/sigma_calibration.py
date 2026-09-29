"""Offline, reproducible sigma bookkeeping.

Forecast-error sigma by lead time needs pairs of an *issued* forecast and the
contract resolution quantity. For these NOAA city markets that quantity is the
daily maximum of the hourly ``Temp`` column (whole degrees Fahrenheit) on
``weather.gov/wrh/timeseries``, not the NWS gridpoint daytime high the
specialist reads from ``api.weather.gov``.

``api.weather.gov`` serves the current gridpoint document only. It does not
archive issued forecasts, so those pairs cannot be built from the public API
this lab is allowed to GET. This module does not invent them and does not
replace ``NWS_DEFAULT_SIGMA_C`` with a made-up scale.

What it does provide:

- ``identity_stub_record``: the default. Lists the missing inputs. The
  specialist keeps this until a real pair set exists.
- ``fit_forecast_error_by_lead``: sample standard deviation by lead bin when
  the caller supplies pairs. A content hash is the calibration id, so a
  different table is a different id. The result is applied to probabilities
  only when the pairs are marked as the resolution quantity and the caller
  explicitly opts in. Anything that is not a verified holdout stays labeled
  ``out_of_sample: false``.
- ``climatological_day_to_day_sigma``: sample std of successive daily-max
  changes. That is day-to-day weather, not forecast error, and it is not
  applied to probabilities.

None of this is statistical validation. ``edge_proven`` stays false.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from research_lab.hashing import sha256_hex
from research_lab.money import D

IDENTITY_CALIBRATION_ID = "identity-stub-v0"
MODEL_FAMILY = "weather-station-baseline-v2-same-day-v1"
SIGMA_QUANT = Decimal("0.000001")

# Lead bins in hours: [lo, hi). The last bin includes its upper edge.
LEAD_BINS: tuple[tuple[Decimal, Decimal, str], ...] = (
    (D(0), D(12), "0-12"),
    (D(12), D(24), "12-24"),
    (D(24), D(48), "24-48"),
    (D(48), D(72), "48-72"),
    (D(72), D(168), "72-168"),
)

MISSING_ARCHIVED_FORECASTS = "archived_issued_nws_forecasts"
MISSING_RESOLUTION_PAIRS = "paired_resolution_quantity_hourly_temp_column_whole_degree_f"

_STUB_NOTE = (
    "Forecast-error sigma by lead time is not estimated. "
    "api.weather.gov serves only the current gridpoint forecast, not an archive "
    "of issued forecasts. The contract resolves on the daily maximum of the hourly "
    "Temp column (whole degrees Fahrenheit) at weather.gov/wrh/timeseries, which is "
    "a different quantity from that gridpoint high. Without pairs of "
    "(issued forecast, realized Temp-column daily max), there is nothing honest "
    "to fit. NWS_DEFAULT_SIGMA_C remains an uncalibrated assumption. "
    "This is not statistical validation and does not prove an edge."
)


def model_version_for(calibration_id: str) -> str:
    """Model id changes when the calibration id changes.

    ``MODEL_FAMILY`` also changes when same-day conditioning rules change.
    """

    token = (calibration_id or "").strip()
    if not token or any(ch.isspace() for ch in token):
        raise ValueError("calibration id required")
    return f"{MODEL_FAMILY}-calibration-{token}"


@dataclass(frozen=True)
class ForecastErrorPair:
    """One issued forecast versus a realized daily max, in Celsius."""

    lead_hours: Decimal
    forecast_max_c: Decimal
    realized_max_c: Decimal

    @property
    def error_c(self) -> Decimal:
        return D(self.forecast_max_c) - D(self.realized_max_c)


@dataclass(frozen=True)
class CalibrationRecord:
    calibration_id: str
    kind: str
    out_of_sample: bool
    applies_to_probabilities: bool
    sigma_c: str | None
    sigma_by_lead_c: dict[str, str]
    sample_n: int | None
    missing: tuple[str, ...]
    note: str
    edge_proven: bool = False

    def __post_init__(self) -> None:
        if self.edge_proven:
            raise ValueError("edge_proven must stay false")

    def to_dict(self) -> dict[str, object]:
        return {
            "calibration_id": self.calibration_id,
            "kind": self.kind,
            "out_of_sample": self.out_of_sample,
            "applies_to_probabilities": self.applies_to_probabilities,
            "sigma_c": self.sigma_c,
            "sigma_by_lead_c": dict(self.sigma_by_lead_c),
            "sample_n": self.sample_n,
            "missing": list(self.missing),
            "note": self.note,
            "edge_proven": False,
        }


def identity_stub_record() -> CalibrationRecord:
    return CalibrationRecord(
        calibration_id=IDENTITY_CALIBRATION_ID,
        kind="identity_stub",
        out_of_sample=False,
        applies_to_probabilities=False,
        sigma_c=None,
        sigma_by_lead_c={},
        sample_n=None,
        missing=(MISSING_ARCHIVED_FORECASTS, MISSING_RESOLUTION_PAIRS),
        note=_STUB_NOTE,
    )


def _stub_with(extra_missing: tuple[str, ...], extra_note: str) -> CalibrationRecord:
    base = identity_stub_record()
    missing = tuple(dict.fromkeys((*base.missing, *extra_missing)))
    return CalibrationRecord(
        calibration_id=base.calibration_id,
        kind="identity_stub",
        out_of_sample=False,
        applies_to_probabilities=False,
        sigma_c=None,
        sigma_by_lead_c={},
        sample_n=None,
        missing=missing,
        note=f"{base.note} {extra_note}",
    )


def _sample_std(values: Sequence[Decimal]) -> Decimal | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum((D(v) for v in values), D(0)) / D(n)
    var = sum((D(v) - mean) ** 2 for v in values) / D(n - 1)
    if var < 0:
        return None
    std = var.sqrt().quantize(SIGMA_QUANT)
    if std <= 0:
        return None
    return std


def _lead_label(lead_hours: Decimal) -> str | None:
    hours = D(lead_hours)
    if hours < 0:
        return None
    last_lo, _last_hi, last_label = LEAD_BINS[-1]
    for lo, hi, label in LEAD_BINS:
        if lo <= hours < hi or (label == last_label and hours == hi):
            return label
    if hours >= last_lo:
        return last_label
    return None


def _calibration_id(body: Mapping[str, object]) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "cal-" + sha256_hex(raw)[:20]


def fit_forecast_error_by_lead(
    pairs: Sequence[ForecastErrorPair],
    *,
    source: str,
    out_of_sample: bool,
    apply_to_probabilities: bool = False,
) -> CalibrationRecord:
    """Estimate sigma by lead bin. Does not invent pairs that were not passed in.

    ``apply_to_probabilities`` is honored only when ``source`` is
    ``paired_forecast_and_resolution_quantity``. Fixture or synthetic sources
    stay inapplicable even if the flag is set. ``out_of_sample`` is recorded
    as supplied; this module does not check a holdout.
    """

    if len(pairs) < 2:
        return _stub_with(
            ("insufficient_forecast_error_pairs",),
            "Fewer than two forecast/realized pairs were supplied. No sigma was invented.",
        )
    grouped: dict[str, list[Decimal]] = {}
    for pair in pairs:
        label = _lead_label(D(pair.lead_hours))
        if label is None:
            continue
        grouped.setdefault(label, []).append(pair.error_c)
    sigma_by_lead: dict[str, str] = {}
    thin: list[str] = []
    for _lo, _hi, label in LEAD_BINS:
        errors = grouped.get(label) or []
        if len(errors) < 2:
            if errors:
                thin.append(f"lead_bin_{label}_n<2")
            continue
        std = _sample_std(errors)
        if std is None:
            thin.append(f"lead_bin_{label}_degenerate")
            continue
        sigma_by_lead[label] = str(std)
    if not sigma_by_lead:
        return _stub_with(
            ("insufficient_forecast_error_pairs", *thin),
            "No lead bin had two or more non-degenerate errors. No sigma was invented.",
        )

    applicable = bool(
        apply_to_probabilities and source == "paired_forecast_and_resolution_quantity"
    )
    missing: list[str] = []
    if source != "paired_forecast_and_resolution_quantity":
        missing.append("pairs_are_not_the_resolution_quantity")
    if not out_of_sample:
        missing.append("not_out_of_sample")
    missing.extend(thin)
    body = {
        "method": "forecast_error_sample_std_by_lead",
        "source": source,
        "out_of_sample": bool(out_of_sample),
        "sigma_by_lead_c": sigma_by_lead,
        "sample_n": len(pairs),
    }
    note = (
        "Sample standard deviation of (forecast_max_c - realized_max_c) by lead bin. "
        f"source={source}. out_of_sample={bool(out_of_sample)} was supplied by the caller; "
        "this module does not verify a holdout. "
        "The resolution quantity is the daily max of the hourly Temp column "
        "(whole degrees F on weather.gov/wrh/timeseries), not the gridpoint forecast high. "
        "Bins with fewer than two errors are omitted rather than filled in. "
        "This is not statistical validation and does not prove an edge."
    )
    if not applicable:
        note += (
            " This table is not applied to probabilities "
            "(source is not the resolution quantity, or apply_to_probabilities is false)."
        )
    return CalibrationRecord(
        calibration_id=_calibration_id(body),
        kind="forecast_error_by_lead",
        out_of_sample=bool(out_of_sample),
        applies_to_probabilities=applicable,
        sigma_c=None,
        sigma_by_lead_c=sigma_by_lead,
        sample_n=len(pairs),
        missing=tuple(missing),
        note=note,
    )


def climatological_day_to_day_sigma(
    daily_max_c: Sequence[Decimal | int | str],
) -> CalibrationRecord:
    """Day-to-day dispersion of daily maxima. Not forecast error. Not applied."""

    values = [D(v) for v in daily_max_c]
    if len(values) < 3:
        return _stub_with(
            ("insufficient_day_to_day_sample",),
            "Need at least three daily maxima for a sample std of day-to-day changes. "
            "No sigma was invented.",
        )
    diffs = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    std = _sample_std(diffs)
    if std is None:
        return _stub_with(
            ("degenerate_day_to_day_dispersion",),
            "Day-to-day changes have no positive sample dispersion. No sigma was invented.",
        )
    body = {
        "method": "climatological_day_to_day_sample_std",
        "sigma_c": str(std),
        "sample_n_days": len(values),
        "sample_n_diffs": len(diffs),
        "out_of_sample": False,
    }
    return CalibrationRecord(
        calibration_id=_calibration_id(body),
        kind="climatological_day_to_day",
        out_of_sample=False,
        applies_to_probabilities=False,
        sigma_c=str(std),
        sigma_by_lead_c={},
        sample_n=len(values),
        missing=(
            MISSING_ARCHIVED_FORECASTS,
            MISSING_RESOLUTION_PAIRS,
            "not_forecast_error",
            "not_out_of_sample",
        ),
        note=(
            "Sample standard deviation of successive daily-max changes. "
            "This is climatological day-to-day dispersion, not NWS forecast error "
            "by lead time, and it is not an out-of-sample score. "
            "It is not applied to probabilities. "
            "Archived issued NWS forecasts paired with the hourly Temp-column daily max "
            "(whole degrees F on weather.gov/wrh/timeseries) are still missing. "
            "This is not statistical validation and does not prove an edge."
        ),
    )


def sigma_for_lead(record: CalibrationRecord, horizon_hours: Decimal) -> Decimal | None:
    """Look up a lead bin. Missing bins return None; callers must not invent one."""

    if not record.sigma_by_lead_c:
        return None
    label = _lead_label(D(horizon_hours))
    if label is None:
        return None
    raw = record.sigma_by_lead_c.get(label)
    if raw is None:
        return None
    sigma = D(raw)
    if sigma <= 0:
        return None
    return sigma


class TableCalibrator:
    """Applies a lead-bin sigma table. Refuses records that are not applicable."""

    def __init__(self, record: CalibrationRecord) -> None:
        if not record.applies_to_probabilities:
            raise ValueError(
                "calibration record is not applicable to probabilities; "
                "refusing to pretend it is a fitted error scale"
            )
        if record.edge_proven:
            raise ValueError("edge_proven must stay false")
        self.record = record
        self.version = record.calibration_id

    def apply(
        self,
        mu: Decimal,
        sigma: Decimal,
        *,
        station_id: str,
        horizon_hours: Decimal,
    ) -> tuple[Decimal, Decimal]:
        del station_id
        chosen = sigma_for_lead(self.record, horizon_hours)
        if chosen is None:
            # Missing bin: keep the incoming scale. Do not fill a gap with a guess.
            return mu, sigma
        return mu, chosen
