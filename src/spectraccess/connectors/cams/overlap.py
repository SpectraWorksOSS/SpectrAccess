"""EAC4/forecast overlap comparison, kept explicitly outside aggregation.

Use this rig to test forecast retrieval and choose a lead from real overlap
evidence.  Its metrics do not establish cross-product equivalence or permit
pooling forecast and EAC4 values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import xarray as xr
import pandas as pd

from .connector import ADS_DATASET, ADS_FORECAST_DATASET, ADS_VARIABLES, CAMSResult


@dataclass(frozen=True)
class CAMSVariableOverlap:
    variable: str
    eac4_unit: str | None
    forecast_unit: str | None
    eac4_mean: float
    forecast_mean: float
    mean_difference: float


@dataclass(frozen=True)
class CAMSOverlapComparison:
    """One explicit EAC4/forecast difference, never a pooled aggregate."""

    scene_date: datetime
    cycle: str
    lead_time_hours: int
    valid_time: datetime
    variables: tuple[CAMSVariableOverlap, ...]


def compare_eac4_forecast_overlap(
    eac4: CAMSResult, forecast: CAMSResult
) -> CAMSOverlapComparison:
    """Compare same-date products by global variable means at valid time.

    The deliberately small metric makes candidate lead scans cheap and robust
    across differing native grids.  It detects step-0 AOD, unit, and
    valid-time mistakes before a larger spatial benchmark is commissioned.
    """

    _require_overlap_pair(eac4, forecast)
    valid_time = _forecast_valid_time(forecast)
    with xr.open_dataset(_single_file(eac4)) as eac4_ds, xr.open_dataset(
        _single_file(forecast)
    ) as forecast_ds:
        variables = tuple(
            _compare_variable(eac4_ds, forecast_ds, variable, valid_time)
            for variable in ADS_VARIABLES
        )
    return CAMSOverlapComparison(
        scene_date=forecast.scene_date,
        cycle=forecast.forecast_cycle or "",
        lead_time_hours=forecast.forecast_lead_time_hours or 0,
        valid_time=valid_time,
        variables=variables,
    )


def compare_candidate_leads(
    eac4: CAMSResult, forecasts: Iterable[CAMSResult]
) -> tuple[CAMSOverlapComparison, ...]:
    """Run the same explicit comparison for every fetched lead candidate."""

    return tuple(compare_eac4_forecast_overlap(eac4, forecast) for forecast in forecasts)


def _require_overlap_pair(eac4: CAMSResult, forecast: CAMSResult) -> None:
    if eac4.dataset != ADS_DATASET or eac4.stratum != "eac4-reanalysis":
        raise ValueError("overlap reference must be an EAC4 reanalysis result")
    if forecast.dataset != ADS_FORECAST_DATASET or forecast.stratum != "forecast-nrt":
        raise ValueError("overlap candidate must be a CAMS forecast/NRT result")
    if eac4.scene_date.date() != forecast.scene_date.date():
        raise ValueError("overlap comparison requires EAC4 and forecast for the same date")
    if not forecast.assumption_ids:
        raise ValueError("forecast result must carry its declared assumption ID")


def _forecast_valid_time(result: CAMSResult) -> datetime:
    if result.forecast_cycle is None or result.forecast_lead_time_hours is None:
        raise ValueError("forecast result lacks cycle or lead-time provenance")
    cycle_hour, cycle_minute = map(int, result.forecast_cycle.split(":"))
    return result.scene_date.replace(
        hour=cycle_hour, minute=cycle_minute, second=0, microsecond=0
    ) + timedelta(hours=result.forecast_lead_time_hours)


def _single_file(result: CAMSResult) -> Path:
    if len(result.files) != 1:
        raise ValueError("overlap comparator requires exactly one netCDF asset per product")
    return result.files[0]


def _compare_variable(
    eac4: xr.Dataset,
    forecast: xr.Dataset,
    variable: str,
    valid_time: datetime,
) -> CAMSVariableOverlap:
    if variable not in eac4 or variable not in forecast:
        raise ValueError(f"overlap file is missing required CAMS variable {variable!r}")
    eac4_values = _at_valid_time(eac4[variable], valid_time)
    forecast_values = _at_valid_time(forecast[variable], valid_time)
    eac4_mean = float(eac4_values.mean(skipna=True).item())
    forecast_mean = float(forecast_values.mean(skipna=True).item())
    return CAMSVariableOverlap(
        variable=variable,
        eac4_unit=eac4_values.attrs.get("units"),
        forecast_unit=forecast_values.attrs.get("units"),
        eac4_mean=eac4_mean,
        forecast_mean=forecast_mean,
        mean_difference=forecast_mean - eac4_mean,
    )


def _at_valid_time(values: xr.DataArray, valid_time: datetime) -> xr.DataArray:
    expected = pd.Timestamp(valid_time).tz_convert("UTC").tz_localize(None)
    for coordinate in ("valid_time", "time"):
        if coordinate in values.coords:
            try:
                return values.sel({coordinate: expected})
            except KeyError:
                raise ValueError(
                    f"{coordinate} does not exactly match forecast valid time {valid_time!r}"
                ) from None
    raise ValueError("CAMS variable has neither valid_time nor time coordinate")
