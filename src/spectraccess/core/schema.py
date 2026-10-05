"""Canonical tidy schema (v1) shared by all spectrAccess connectors.

Every connector's `parse()` output is source-specific and stays that way (see
`Connector.parse` for the loose, per-connector tidy shape). This module defines
a SECOND, versioned, long/tidy schema -- one row per (quantity, uncertainty)
observation -- that connectors additionally emit via a `to_canonical(...)`
function (or a `parse_canonical` convenience method) so downstream consumers
can rely on one stable contract across all sources, instead of one ad-hoc
shape per connector.

Uncertainty is carried as a record with a provenance status: the numeric
value may be absent (``None`` / null), but the status is never absent -- it is
always one of ``provided``, ``derived``, ``prior``, or ``unknown``. A row that
claims an uncertainty status other than ``unknown`` must carry an actual
value, and a row with no value must be honest that its status is ``unknown``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from math import isfinite
from typing import NamedTuple

import pandas as pd

SCHEMA_VERSION = "1.0"

_ATTR_KEY = "spectraccess_schema_version"


class UncertaintyStatus(str, enum.Enum):
    """Provenance status of an uncertainty value.

    - ``PROVIDED``: the source itself supplied the uncertainty (e.g. a
      standard error column shipped alongside the value).
    - ``DERIVED``: computed by spectrAccess or a downstream tool from other
      information (not asserted by the source).
    - ``PRIOR``: a prior/assumed uncertainty, not measured for this row.
    - ``UNKNOWN``: no uncertainty value is available.
    """

    PROVIDED = "provided"
    DERIVED = "derived"
    PRIOR = "prior"
    UNKNOWN = "unknown"


VALID_UNCERTAINTY_STATUSES: frozenset[str] = frozenset(status.value for status in UncertaintyStatus)


class SchemaError(ValueError):
    """Raised when a frame or record violates the canonical schema."""


def _normalize_status(status: "UncertaintyStatus | str") -> str:
    if isinstance(status, UncertaintyStatus):
        return status.value
    return str(status)


@dataclass(frozen=True)
class Uncertainty:
    """A single uncertainty record: value, provenance status, and metadata.

    ``value`` may be ``None`` only when ``status`` is ``"unknown"``; any other
    status requires a finite, non-negative ``value``. Zero is allowed (an
    uncertainty of exactly zero is a legitimate, if unusual, claim);
    NaN/inf/negative values are rejected.
    """

    value: float | None
    status: str
    k: float | None = None
    provider: str | None = None

    def __post_init__(self) -> None:
        status = _normalize_status(self.status)
        if status not in VALID_UNCERTAINTY_STATUSES:
            raise SchemaError(
                f"Uncertainty.status={status!r} is not one of {sorted(VALID_UNCERTAINTY_STATUSES)}"
            )
        object.__setattr__(self, "status", status)

        if self.value is None:
            if status != UncertaintyStatus.UNKNOWN.value:
                raise SchemaError(
                    "Uncertainty.value is None but status="
                    f"{status!r} -- a claimed-but-absent uncertainty is not allowed; "
                    "use status='unknown' when no value is available"
                )
        else:
            if status == UncertaintyStatus.UNKNOWN.value:
                raise SchemaError(
                    f"Uncertainty.value={self.value!r} is not None but status='unknown' -- "
                    "a known value cannot carry an unknown status"
                )
            if not isfinite(self.value):
                raise SchemaError(f"Uncertainty.value={self.value!r} must be finite")
            if self.value < 0:
                raise SchemaError(f"Uncertainty.value={self.value!r} must be >= 0")


class ColumnSpec(NamedTuple):
    """Describes one canonical column: its dtype kind and whether it (the
    column itself, not necessarily every value in it) is required."""

    dtype: str  # one of "datetime", "str", "float"
    required: bool  # column must be present; see per-column value rules below


# Ordered canonical column set. `required=True` means the COLUMN must be
# present in any validated frame. Whether individual VALUES may be null is
# documented in the module docstring / spec table, not encoded here except
# for the value-level checks `validate()` performs explicitly below.
CANONICAL_COLUMNS: dict[str, ColumnSpec] = {
    "time": ColumnSpec("datetime", True),
    "platform": ColumnSpec("str", True),
    "instrument": ColumnSpec("str", True),
    "band": ColumnSpec("str", True),
    "wavelength_nm": ColumnSpec("float", True),
    "site": ColumnSpec("str", True),
    "latitude": ColumnSpec("float", True),
    "longitude": ColumnSpec("float", True),
    "reference": ColumnSpec("str", True),
    "quantity": ColumnSpec("str", True),
    "value": ColumnSpec("float", True),
    "units": ColumnSpec("str", True),
    "unc_value": ColumnSpec("float", True),
    "unc_status": ColumnSpec("str", True),
    "unc_k": ColumnSpec("float", True),
    "unc_provider": ColumnSpec("str", True),
    "source": ColumnSpec("str", True),
    "source_agency": ColumnSpec("str", True),
    "source_url": ColumnSpec("str", True),
    "retrieved_at": ColumnSpec("datetime", True),
}

# Additive v1 observation contract. Keep the original column registry and
# empty_frame shape stable for existing connectors. Absence means unknown.
# Object fields carry JSON-compatible structures, except assumptions which
# may also carry spectrAccess's own AssumptionRecord instances.
OBSERVATION_COLUMNS: dict[str, ColumnSpec] = {
    **{name: ColumnSpec("float", False) for name in (
        "u_independent", "u_structured", "u_common", "uncertainty_k",
        "bias", "u_bias", "elevation_m",
    )},
    **{name: ColumnSpec("str", False) for name in (
        "common_group_id", "likelihood_family", "likelihood_transform",
        "support_kind", "sigma_basis", "algorithm_version", "collection_version",
    )},
    **{name: ColumnSpec("datetime", False) for name in (
        "valid_time", "integration_start", "integration_end",
    )},
    **{name: ColumnSpec("object", False) for name in (
        "correlation_lengths", "likelihood_parameters", "footprint_geometry",
        "correlation_groups", "assimilated_inputs", "retrieval_prior",
        "prior_state", "prior_covariance", "averaging_kernel", "qa", "assumptions",
    )},
}

LIKELIHOOD_FAMILIES = frozenset({
    "gaussian", "gaussian-in-transform", "student_t", "censored", "categorical",
})
SUPPORT_KINDS = frozenset({"point", "pixel", "grid cell", "swath"})


def frame_from_records(records: list[dict[str, object]]) -> pd.DataFrame:
    """Build canonical rows without filling optional observations with defaults.

    Each caller explicitly supplies source, quantity and uncertainty status.
    Missing original v1 identity fields are null, as permitted by v1.
    """
    if not records:
        return empty_frame()
    frame = pd.DataFrame(records)
    for name in CANONICAL_COLUMNS:
        if name not in frame:
            frame[name] = None
    return validate(frame)


def _present(value: object) -> bool:
    return value is not None and not (pd.api.types.is_scalar(value) and pd.isna(value))


def _validate_observations(df: pd.DataFrame, errors: list[str]) -> None:
    from spectraccess.core.assumptions import AssumptionRecord

    for name, spec in OBSERVATION_COLUMNS.items():
        if name not in df:
            continue
        for value in df[name]:
            if not _present(value):
                continue
            if spec.dtype == "float":
                if isinstance(value, (str, bool)) or not isinstance(value, (int, float)) or not isfinite(value):
                    errors.append(f"{name} must be finite numeric")
                elif name in {"u_independent", "u_structured", "u_common", "u_bias"} and value < 0:
                    errors.append(f"{name} must be >= 0")
                elif name == "uncertainty_k" and value != 1:
                    errors.append("uncertainty_k must be 1 (standard uncertainty)")
            elif spec.dtype == "str" and (not isinstance(value, str) or not value.strip()):
                errors.append(f"{name} must be a non-blank string")
            if name == "likelihood_family" and (not isinstance(value, str) or value not in LIKELIHOOD_FAMILIES):
                errors.append(f"likelihood_family must be one of {sorted(LIKELIHOOD_FAMILIES)}")
            if name == "support_kind" and (not isinstance(value, str) or value not in SUPPORT_KINDS):
                errors.append(f"support_kind must be one of {sorted(SUPPORT_KINDS)}")
            if name in {"correlation_groups", "assimilated_inputs"} and (
                not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value)
            ):
                errors.append(f"{name} must be a list of non-blank identifiers")
            if name == "correlation_lengths":
                if not isinstance(value, dict) or not value or any(
                    key not in {"spatial_m", "temporal_s", "vertical_m"}
                    or isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or v <= 0
                    for key, v in value.items()
                ):
                    errors.append("correlation_lengths requires positive spatial_m/temporal_s/vertical_m")
            if name in {"footprint_geometry", "likelihood_parameters", "qa", "retrieval_prior"} and not isinstance(value, dict):
                errors.append(f"{name} must be a mapping")
            if name == "assumptions" and (
                not isinstance(value, list) or any(not isinstance(v, AssumptionRecord) for v in value)
            ):
                errors.append("assumptions must contain spectrAccess AssumptionRecord instances")
        if spec.dtype == "datetime" and not _coercible_to_datetime(df[name]):
            errors.append(f"{name} has values not coercible to datetime")

    for _, row in df.iterrows():
        family = row.get("likelihood_family")
        if isinstance(family, str) and family == "gaussian-in-transform" and not _present(row.get("likelihood_transform")):
            errors.append("gaussian-in-transform requires likelihood_transform")
        if _present(row.get("u_common")) and not _present(row.get("common_group_id")):
            errors.append("u_common requires common_group_id")
        start, end = row.get("integration_start"), row.get("integration_end")
        if _present(start) and _present(end):
            try:
                if pd.Timestamp(start) > pd.Timestamp(end):
                    errors.append("integration_end precedes integration_start")
            except (ValueError, TypeError):
                errors.append("integration window has incompatible timestamps")

# Columns whose VALUES must never be null.
_NEVER_NULL_COLUMNS = ("quantity", "unc_status", "source")

# Float columns whose non-null VALUES must be numeric and finite. `unc_value`
# additionally must be >= 0 (checked separately); the others may be negative
# (offsets, biases, latitudes, longitudes).
_FLOAT_COLUMNS = ("wavelength_nm", "latitude", "longitude", "value", "unc_value", "unc_k")

_DTYPE_TO_PANDAS = {
    "datetime": "datetime64[ns, UTC]",
    "str": "object",
    "float": "float64",
}


def _empty_series(dtype_kind: str) -> pd.Series:
    if dtype_kind == "datetime":
        return pd.Series([], dtype="datetime64[ns, UTC]")
    if dtype_kind == "float":
        return pd.Series([], dtype="float64")
    return pd.Series([], dtype="object")


def _stamp(df: pd.DataFrame) -> pd.DataFrame:
    df.attrs[_ATTR_KEY] = SCHEMA_VERSION
    return df


def empty_frame() -> pd.DataFrame:
    """Return a zero-row canonical frame with all columns and correct dtypes."""

    data = {name: _empty_series(spec.dtype) for name, spec in CANONICAL_COLUMNS.items()}
    df = pd.DataFrame(data)
    return _stamp(df)


def uncertainty_columns(unc: Uncertainty) -> dict[str, object]:
    """Map an `Uncertainty` record to the four `unc_*` canonical column values."""

    return {
        "unc_value": unc.value,
        "unc_status": unc.status,
        "unc_k": unc.k,
        "unc_provider": unc.provider,
    }


def _coercible_to_datetime(series: pd.Series) -> bool:
    non_null = series.dropna()
    if non_null.empty:
        return True
    try:
        pd.to_datetime(non_null)
    except (ValueError, TypeError):
        return False
    return True


def validate(df: pd.DataFrame) -> pd.DataFrame:
    """Validate a frame against the canonical schema.

    Collects every violation before raising a single `SchemaError` naming
    each problem. Returns the frame (attrs stamped with the schema version)
    unchanged otherwise -- callers are expected to have already produced
    correctly-typed columns; `validate()` checks, it does not coerce.
    """

    errors: list[str] = []

    missing_columns = [name for name in CANONICAL_COLUMNS if name not in df.columns]
    if missing_columns:
        errors.append(f"missing canonical columns: {missing_columns}")

    if missing_columns:
        # Value-level checks below assume the columns exist; bail out early
        # with just the missing-columns error rather than raising spurious
        # KeyErrors on top of it.
        raise SchemaError("; ".join(errors))

    for column in _NEVER_NULL_COLUMNS:
        if df[column].isna().any():
            errors.append(f"column {column!r} must never be null but has null values")

    bad_status_mask = ~df["unc_status"].isin(VALID_UNCERTAINTY_STATUSES)
    if bad_status_mask.any():
        bad_values = sorted(set(df.loc[bad_status_mask, "unc_status"].dropna().tolist()))
        errors.append(
            f"column 'unc_status' has values outside {sorted(VALID_UNCERTAINTY_STATUSES)}: {bad_values}"
        )

    unc_value_null = df["unc_value"].isna()
    unc_status_unknown = df["unc_status"] == UncertaintyStatus.UNKNOWN.value

    null_but_not_unknown = unc_value_null & ~unc_status_unknown
    if null_but_not_unknown.any():
        errors.append(
            "rows with null 'unc_value' must have unc_status='unknown' "
            f"({int(null_but_not_unknown.sum())} row(s) violate this)"
        )

    present_but_unknown = ~unc_value_null & unc_status_unknown
    if present_but_unknown.any():
        errors.append(
            "rows with non-null 'unc_value' must not have unc_status='unknown' "
            f"({int(present_but_unknown.sum())} row(s) violate this)"
        )

    for column in _FLOAT_COLUMNS:
        present = df.loc[df[column].notna(), column]
        if present.empty:
            continue
        numeric = pd.to_numeric(present, errors="coerce")
        non_finite = numeric.isna() | ~numeric.apply(lambda v: isfinite(v) if pd.notna(v) else False)
        if non_finite.any():
            errors.append(
                f"column {column!r} has non-numeric or non-finite entries ({int(non_finite.sum())} row(s))"
            )

    unc_present = df.loc[~unc_value_null, "unc_value"]
    if not unc_present.empty:
        numeric = pd.to_numeric(unc_present, errors="coerce")
        negative = numeric < 0
        if (negative.fillna(False)).any():
            errors.append(
                f"column 'unc_value' has negative entries ({int(negative.fillna(False).sum())} row(s))"
            )

    for column in ("time", "retrieved_at"):
        if not _coercible_to_datetime(df[column]):
            errors.append(f"column {column!r} has values not coercible to datetime")

    _validate_observations(df, errors)

    if errors:
        raise SchemaError("; ".join(errors))

    return _stamp(df)
