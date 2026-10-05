"""Additive schema v1 contracts, not connector-specific scientific defaults."""

import pandas as pd
import pytest

from spectraccess.core.assumptions import AssumptionBasis, AssumptionRecord
from spectraccess.core.schema import CANONICAL_COLUMNS, OBSERVATION_COLUMNS, SchemaError, frame_from_records, validate


def row(**extra):
    return dict(quantity="test", value=2., units="m", source="fixture",
                unc_value=None, unc_status="unknown", **extra)


def test_legacy_rows_and_empty_shape_unchanged():
    frame = frame_from_records([row()])
    assert not set(OBSERVATION_COLUMNS).intersection(frame.columns)
    assert frame.attrs["spectraccess_schema_version"] == "1.0"
    assert list(frame_from_records([])) == list(CANONICAL_COLUMNS)
    assert validate(frame) is frame


def test_complete_optional_contract_preserves_declared_objects():
    assumption = AssumptionRecord("fixture", "Test declaration", AssumptionBasis.OPERATOR_DECLARED, "Published metadata arrives")
    frame = frame_from_records([row(
        u_independent=.1, u_structured=.2, correlation_lengths={"spatial_m":500,"temporal_s":300},
        u_common=.3, common_group_id="shared", uncertainty_k=1, bias=-.2, u_bias=.05,
        likelihood_family="gaussian-in-transform", likelihood_transform="ln(tau+tau0)",
        likelihood_parameters={"tau0":.01}, valid_time="2024-01-01T00:00:00Z",
        integration_start="2024-01-01T00:00:00Z", integration_end="2024-01-01T00:01:00Z",
        footprint_geometry={"type":"Point","coordinates":[4.,52.]}, support_kind="point",
        elevation_m=5, correlation_groups=["shared"], assimilated_inputs=["aeronet-v3"],
        retrieval_prior={"source":"published"}, prior_state=[1.], prior_covariance=[[.1]],
        averaging_kernel=[[.8]], qa={"accepted":True}, sigma_basis="declared", assumptions=[assumption],
        algorithm_version="v1", collection_version="001",
    )])
    assert frame.loc[0,"bias"] == -.2
    assert frame.loc[0,"assumptions"][0] is assumption
    assert set(OBSERVATION_COLUMNS).issubset(frame.columns)


@pytest.mark.parametrize("extra,match", [
    ({"u_independent":-.1},"u_independent"),
    ({"u_bias":float("inf")},"u_bias"),
    ({"uncertainty_k":2},"uncertainty_k"),
    ({"likelihood_family":"normal"},"likelihood_family"),
    ({"likelihood_family":"gaussian-in-transform"},"likelihood_transform"),
    ({"support_kind":"area"},"support_kind"),
    ({"u_common":.2},"common_group_id"),
    ({"correlation_lengths":{"spatial_m":0}},"correlation_lengths"),
    ({"correlation_groups":"shared"},"correlation_groups"),
    ({"assimilated_inputs":[None]},"assimilated_inputs"),
    ({"valid_time":"bad time"},"valid_time"),
    ({"integration_start":"2024-01-02","integration_end":"2024-01-01"},"integration_end"),
    ({"assumptions":[{"statement":"not its own type"}]},"AssumptionRecord"),
])
def test_invalid_optional_metadata_rejected(extra,match):
    with pytest.raises(SchemaError,match=match):
        frame_from_records([row(**extra)])


@pytest.mark.parametrize("family",["gaussian","student_t","censored","categorical"])
def test_declared_likelihood_families(family):
    validate(frame_from_records([row(likelihood_family=family)]))


def test_optional_nulls_do_not_create_semantics():
    frame = frame_from_records([row(**{name:None for name in OBSERVATION_COLUMNS})])
    validate(frame)
    assert frame["uncertainty_k"].isna().all()


def test_nullable_optional_fields_are_unknown():
    frame = frame_from_records([row(likelihood_family=pd.NA, common_group_id=pd.NA)])
    validate(frame)
