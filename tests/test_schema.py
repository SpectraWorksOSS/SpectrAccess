from __future__ import annotations

import math

import pandas as pd
import pytest

from spectraccess.core.schema import (
    CANONICAL_COLUMNS,
    SchemaError,
    Uncertainty,
    UncertaintyStatus,
    empty_frame,
    validate,
)


def _valid_row(**overrides: object) -> dict[str, object]:
    row = {name: None for name in CANONICAL_COLUMNS}
    row.update(
        {
            "time": pd.Timestamp("2026-07-04", tz="UTC"),
            "quantity": "gsics_correction_slope",
            "value": 1.0,
            "unc_value": 0.1,
            "unc_status": UncertaintyStatus.PROVIDED.value,
            "source": "gsics",
            "retrieved_at": pd.Timestamp("2026-07-05", tz="UTC"),
        }
    )
    row.update(overrides)
    return row


# --- Uncertainty -------------------------------------------------------


def test_uncertainty_accepts_provided_value():
    unc = Uncertainty(value=0.1, status="provided")
    assert unc.value == 0.1
    assert unc.status == "provided"


def test_uncertainty_accepts_unknown_none():
    unc = Uncertainty(value=None, status="unknown")
    assert unc.value is None
    assert unc.status == "unknown"


def test_uncertainty_accepts_enum_status():
    unc = Uncertainty(value=0.1, status=UncertaintyStatus.DERIVED)
    assert unc.status == "derived"


def test_uncertainty_rejects_bad_status():
    with pytest.raises(SchemaError):
        Uncertainty(value=0.1, status="bogus")


def test_uncertainty_rejects_none_value_with_non_unknown_status():
    with pytest.raises(SchemaError):
        Uncertainty(value=None, status="provided")


def test_uncertainty_rejects_value_with_unknown_status():
    with pytest.raises(SchemaError):
        Uncertainty(value=0.1, status="unknown")


def test_uncertainty_rejects_nan():
    with pytest.raises(SchemaError):
        Uncertainty(value=float("nan"), status="provided")


def test_uncertainty_rejects_negative():
    with pytest.raises(SchemaError):
        Uncertainty(value=-0.1, status="provided")


def test_uncertainty_rejects_inf():
    with pytest.raises(SchemaError):
        Uncertainty(value=math.inf, status="provided")


def test_uncertainty_accepts_zero():
    unc = Uncertainty(value=0.0, status="provided")
    assert unc.value == 0.0


# --- empty_frame / validate happy path ---------------------------------


def test_empty_frame_validates_clean():
    df = empty_frame()
    validated = validate(df)
    assert list(validated.columns) == list(CANONICAL_COLUMNS)
    assert validated.attrs["spectraccess_schema_version"] == "1.0"


def test_hand_built_valid_frame_passes_and_gets_stamped():
    df = pd.DataFrame([_valid_row()])
    validated = validate(df)
    assert validated.attrs["spectraccess_schema_version"] == "1.0"


# --- validate failure cases ---------------------------------------------


def test_validate_missing_column():
    df = pd.DataFrame([_valid_row()]).drop(columns=["quantity"])
    with pytest.raises(SchemaError, match="quantity"):
        validate(df)


def test_validate_null_unc_status():
    df = pd.DataFrame([_valid_row(unc_status=None)])
    with pytest.raises(SchemaError, match="unc_status"):
        validate(df)


def test_validate_out_of_vocab_status():
    df = pd.DataFrame([_valid_row(unc_status="bogus")])
    with pytest.raises(SchemaError, match="unc_status"):
        validate(df)


def test_validate_null_unc_value_with_status_provided():
    df = pd.DataFrame([_valid_row(unc_value=None, unc_status="provided")])
    with pytest.raises(SchemaError, match="unc_value"):
        validate(df)


def test_validate_non_null_unc_value_with_status_unknown():
    df = pd.DataFrame([_valid_row(unc_value=0.1, unc_status="unknown")])
    with pytest.raises(SchemaError, match="unc_value"):
        validate(df)


def test_validate_null_quantity():
    df = pd.DataFrame([_valid_row(quantity=None)])
    with pytest.raises(SchemaError, match="quantity"):
        validate(df)


def test_validate_null_source():
    df = pd.DataFrame([_valid_row(source=None)])
    with pytest.raises(SchemaError, match="source"):
        validate(df)


def test_validate_negative_unc_value():
    df = pd.DataFrame([_valid_row(unc_value=-1.0, unc_status="provided")])
    with pytest.raises(SchemaError, match="unc_value"):
        validate(df)


def test_validate_non_finite_unc_value():
    df = pd.DataFrame([_valid_row(unc_value=float("nan"), unc_status="provided")])
    with pytest.raises(SchemaError, match="unc_value"):
        validate(df)


def test_validate_non_coercible_time():
    df = pd.DataFrame([_valid_row(time="not-a-date-at-all-###")])
    with pytest.raises(SchemaError, match="time"):
        validate(df)


def test_validate_collects_multiple_errors_in_one_message():
    df = pd.DataFrame([_valid_row(quantity=None, source=None)])
    with pytest.raises(SchemaError) as exc_info:
        validate(df)
    message = str(exc_info.value)
    assert "quantity" in message
    assert "source" in message
