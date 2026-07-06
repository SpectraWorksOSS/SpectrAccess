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

# Columns whose VALUES must never be null.
_NEVER_NULL_COLUMNS = ("quantity", "unc_status", "source")

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

    unc_present = df.loc[~unc_value_null, "unc_value"]
    if not unc_present.empty:
        numeric = pd.to_numeric(unc_present, errors="coerce")
        non_finite = numeric.isna() | ~numeric.apply(lambda v: isfinite(v) if pd.notna(v) else False)
        negative = numeric < 0
        if non_finite.any():
            errors.append(
                f"column 'unc_value' has non-finite entries ({int(non_finite.sum())} row(s))"
            )
        if (negative.fillna(False)).any():
            errors.append(
                f"column 'unc_value' has negative entries ({int(negative.fillna(False).sum())} row(s))"
            )

    for column in ("time", "retrieved_at"):
        if not _coercible_to_datetime(df[column]):
            errors.append(f"column {column!r} has values not coercible to datetime")

    if errors:
        raise SchemaError("; ".join(errors))

    return _stamp(df)
