from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from spectraccess.connectors.sentinel2_cdse import (
    CDSEDownloadError,
    CDSEProductError,
    CDSEProviderError,
    S2CDSEConnector,
    Sentinel2CDSEConnector,
    target_to_canonical,
)
from spectraccess.connectors.sentinel2_cdse import connector as module


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "sentinel2_cdse"
    / "S2B_T31UFT_20240501_feature.json"
)


def _feature() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_refcal_compatible_connector_name_is_public_alias():
    assert S2CDSEConnector is Sentinel2CDSEConnector


class _FakeQuery:
    def __init__(self, features: list[dict], *, total: int | None = None) -> None:
        self.features = features
        self.total = len(features) if total is None else total

    def __len__(self) -> int:
        return self.total

    def __iter__(self):
        return iter(self.features)


def _patch_query(monkeypatch, features: list[dict], *, total: int | None = None):
    calls: list[tuple[str, dict, dict]] = []

    def fake_query(collection, search_terms, options=None):
        calls.append((collection, dict(search_terms), dict(options or {})))
        if search_terms["top"] == 0:
            return _FakeQuery([], total=len(features) if total is None else total)
        skip = int(search_terms.get("skip", 0))
        top = int(search_terms["top"])
        return _FakeQuery(features[skip : skip + top], total=len(features))

    monkeypatch.setattr(module, "query_features", fake_query)
    return calls


def _dated_features(count: int) -> list[dict]:
    features = []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for index in range(count):
        timestamp = start + timedelta(minutes=index)
        compact = timestamp.strftime("%Y%m%dT%H%M%S")
        item = deepcopy(_feature())
        item["Id"] = f"id-{index:05d}"
        item["Name"] = f"S2B_MSIL1C_{compact}_N0510_R008_T31UFT_{compact}.SAFE"
        item["ContentDate"] = {
            "Start": timestamp.isoformat().replace("+00:00", "Z"),
            "End": timestamp.isoformat().replace("+00:00", "Z"),
        }
        features.append(item)
    return features


def test_discover_wraps_cdsetool_query_and_preserves_refcal_product_fields(monkeypatch):
    feature = _feature()
    calls = _patch_query(monkeypatch, [feature])

    target = Sentinel2CDSEConnector().discover(
        mgrs_tile="T31UFT",
        start=datetime(2024, 5, 1),
        end=datetime(2024, 5, 1),
        max_cloud_cover=20,
        limit=10,
    )[0]

    assert len(calls) == 2
    assert all(call[0] == "SENTINEL-2" for call in calls)
    assert calls[0][1] == {
        "productType": "S2MSI1C",
        "contentDateStartGe": "2024-05-01",
        "contentDateStartLt": "2024-05-02",
        "tileId": "31UFT",
        "cloudCover": "[0,20.0]",
        "top": 0,
    }
    assert calls[1][1]["skip"] == 0
    assert calls[1][1]["top"] == 1
    assert calls[0][2]["expand_attributes"] is True

    # Golden parity with the real RefCal ProductRef fields used by source
    # lineage and promotion. Provider transport stays opaque in target.raw.
    assert target.product_id == "d085d39b-03e2-486d-ae2a-0c8deca9bdc0"
    assert target.title == "S2B_MSIL1C_20240501T103619_N0510_R008_T31UFT_20240501T131918.SAFE"
    assert target.sensor_id == "S2B"
    assert target.platform_id == "S2B"
    assert target.start_time == datetime(2024, 5, 1, 10, 36, 19, 24000, tzinfo=timezone.utc)
    assert target.end_time == target.start_time
    assert target.footprint_wkt.startswith("POLYGON ((4.436637852600691 51.37051002083518")
    assert target.cloud_cover == pytest.approx(18.327762365519)
    assert target.mgrs_tile == "31UFT"
    assert target.content_length_bytes == 756_691_750
    assert target.size_mb == pytest.approx(756.69175)
    assert target.online is True
    assert target.processor_version == "05.10"
    assert target.checksums["MD5"] == "69bb891d68cdfdd72b59d1e892d75916"
    assert target.provider == "cdse"
    assert target.provider_client == "cdsetool"
    assert target.raw["S3Path"].startswith("/eodata/Sentinel-2/MSI/L1C/")


def test_discover_uses_cdsetool_count_skip_for_newest_first(monkeypatch):
    features = []
    for day in range(1, 6):
        item = deepcopy(_feature())
        item["Id"] = f"id-{day}"
        item["Name"] = f"S2B_MSIL1C_2024050{day}T103619_N0510_R008_T31UFT_2024050{day}T131918.SAFE"
        item["ContentDate"] = {
            "Start": f"2024-05-0{day}T10:36:19Z",
            "End": f"2024-05-0{day}T10:36:19Z",
        }
        features.append(item)
    calls = _patch_query(monkeypatch, features)

    targets = Sentinel2CDSEConnector().discover(mgrs_tile="31UFT", limit=2)

    assert calls[1][1]["skip"] == 3
    assert [target.product_id for target in targets] == ["id-5", "id-4"]


@pytest.mark.parametrize(
    "total, limit, expected_page_sizes, expected_first_skip",
    [
        (999, 1500, [999], 0),
        (1000, 1000, [1000], 0),
        (1001, 1001, [1000, 1], 0),
        (2000, 1001, [1000, 1], 999),
        (2505, 1501, [1000, 501], 1004),
    ],
)
def test_discover_paginates_large_newest_slice_exactly(
    monkeypatch, total, limit, expected_page_sizes, expected_first_skip
):
    features = _dated_features(total)
    calls = _patch_query(monkeypatch, features)

    targets = Sentinel2CDSEConnector().discover(mgrs_tile="31UFT", limit=limit)

    requested = min(total, limit)
    expected_indices = list(range(total - 1, total - requested - 1, -1))
    assert len(targets) == requested
    assert [target.product_id for target in targets] == [
        f"id-{index:05d}" for index in expected_indices
    ]
    page_calls = calls[1:]
    assert [call[1]["top"] for call in page_calls] == expected_page_sizes
    assert page_calls[0][1]["skip"] == expected_first_skip
    assert [call[1]["skip"] for call in page_calls] == [
        expected_first_skip + sum(expected_page_sizes[:index])
        for index in range(len(expected_page_sizes))
    ]


def test_discover_fails_loud_when_catalogue_changes_between_count_and_page(monkeypatch):
    feature = _feature()

    def short_page_query(_collection, search_terms, options=None):
        if search_terms["top"] == 0:
            return _FakeQuery([], total=2)
        return _FakeQuery([feature], total=1)

    monkeypatch.setattr(module, "query_features", short_page_query)
    with pytest.raises(CDSEProviderError, match="catalogue changed during pagination"):
        Sentinel2CDSEConnector().discover(mgrs_tile="31UFT", limit=2)


def test_discover_bbox_is_delegated_as_cdsetool_geometry(monkeypatch):
    calls = _patch_query(monkeypatch, [_feature()])
    Sentinel2CDSEConnector().discover(bbox=(4.0, 51.0, 6.5, 52.5), limit=1)
    assert calls[0][1]["geometry"] == (
        "POLYGON((4.0 51.0, 4.0 52.5, 6.5 52.5, 6.5 51.0, 4.0 51.0))"
    )


def test_discover_rejects_provider_pagination_overflow(monkeypatch):
    _patch_query(monkeypatch, [], total=10_001)
    with pytest.raises(CDSEProviderError, match="10,001 products"):
        Sentinel2CDSEConnector().discover(mgrs_tile="31UFT")


def test_discover_surfaces_cdsetool_swallowed_error(monkeypatch):
    def failed_query(_collection, _search_terms, options=None):
        options["logger"].error("Failed to fetch features after %d attempts", 3)
        return _FakeQuery([], total=0)

    monkeypatch.setattr(module, "query_features", failed_query)
    with pytest.raises(CDSEProviderError, match="Failed to fetch features after 3 attempts"):
        Sentinel2CDSEConnector().discover(mgrs_tile="31UFT")


def test_invalid_or_contradictory_provider_metadata_fails_loud(monkeypatch):
    feature = _feature()
    for attribute in feature["Attributes"]:
        if attribute["Name"] == "platformSerialIdentifier":
            attribute["Value"] = "C"
    _patch_query(monkeypatch, [feature])
    with pytest.raises(CDSEProductError, match="contradicts platformSerialIdentifier"):
        Sentinel2CDSEConnector().discover(mgrs_tile="31UFT")


def test_fetch_delegates_transport_and_verifies_provider_md5(monkeypatch, tmp_path):
    calls = _patch_query(monkeypatch, [_feature()])
    target = Sentinel2CDSEConnector().discover(mgrs_tile="31UFT")[0]
    payload = b"fixture-safe-archive"
    expected = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    object.__setattr__(target, "checksums", {"MD5": expected})
    download_calls = []

    def fake_download(feature, path, options):
        download_calls.append((feature, path, options))
        filename = feature["Name"] + ".zip"
        (Path(path) / filename).write_bytes(payload)
        return filename

    monkeypatch.setattr(module, "download_feature", fake_download)
    marker_credentials = object()
    path = Sentinel2CDSEConnector().fetch(
        target,
        dest=tmp_path,
        credentials=marker_credentials,
    )

    assert Path(path).read_bytes() == payload
    assert download_calls[0][0]["Id"] == target.product_id
    assert download_calls[0][2]["credentials"] is marker_credentials
    assert "username" not in target.raw
    assert len(calls) == 2


def test_fetch_checksum_mismatch_is_explicit(monkeypatch, tmp_path):
    _patch_query(monkeypatch, [_feature()])
    target = Sentinel2CDSEConnector().discover(mgrs_tile="31UFT")[0]

    def fake_download(feature, path, _options):
        filename = feature["Name"] + ".zip"
        (Path(path) / filename).write_bytes(b"wrong")
        return filename

    monkeypatch.setattr(module, "download_feature", fake_download)
    with pytest.raises(CDSEDownloadError, match="checksum mismatch"):
        Sentinel2CDSEConnector().fetch(target, dest=tmp_path, credentials=object())


def test_fetch_requires_complete_nonconflicting_credential_input(monkeypatch):
    _patch_query(monkeypatch, [_feature()])
    target = Sentinel2CDSEConnector().discover(mgrs_tile="31UFT")[0]
    with pytest.raises(ValueError, match="provided together"):
        Sentinel2CDSEConnector().fetch(target, dest="unused", username="user")
    with pytest.raises(ValueError, match="not both"):
        Sentinel2CDSEConnector().fetch(
            target,
            dest="unused",
            username="user",
            password="pass",
            credentials=object(),
        )


def test_native_and_canonical_outputs_preserve_provenance_and_unknown_uncertainty(monkeypatch):
    _patch_query(monkeypatch, [_feature()])
    connector = Sentinel2CDSEConnector()
    target = connector.discover(mgrs_tile="31UFT")[0]

    native = connector.parse("/cloud/cache/product.zip", target=target)
    assert native.loc[0, "product_id"] == target.product_id
    assert native.loc[0, "local_path"] == "/cloud/cache/product.zip"
    assert native.loc[0, "provider"] == "cdse"
    assert native.attrs["source_metadata"]["Id"] == target.product_id

    canonical = target_to_canonical(target, local_path="/cloud/cache/product.zip")
    assert canonical.attrs["spectraccess_schema_version"] == "1.0"
    assert canonical.loc[0, "quantity"] == "scene_cloud_cover"
    assert canonical.loc[0, "value"] == pytest.approx(18.327762365519)
    assert canonical.loc[0, "units"] == "%"
    assert canonical.loc[0, "unc_status"] == "unknown"
    assert pd.isna(canonical.loc[0, "unc_value"])
    assert pd.isna(canonical.loc[0, "unc_k"])
    assert pd.isna(canonical.loc[0, "unc_provider"])
    assert canonical.loc[0, "source"] == "copernicus-data-space-ecosystem"
    assert canonical.loc[0, "source_url"] == target.catalogue_url
    assert canonical.loc[0, "footprint_wkt"] == target.footprint_wkt


def test_run_hooks_carry_target_into_parse_paths(monkeypatch):
    _patch_query(monkeypatch, [_feature()])
    target = Sentinel2CDSEConnector().discover(mgrs_tile="31UFT")[0]
    connector = Sentinel2CDSEConnector()
    assert connector._parse_kwargs_for(target) == {"target": target}
    assert connector._canonical_kwargs_for(target) == {"target": target}


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({}, "Provide bbox or mgrs_tile"),
        ({"mgrs_tile": "BAD"}, "invalid MGRS tile"),
        ({"mgrs_tile": "31UFT", "max_cloud_cover": 101}, "between 0 and 100"),
        (
            {
                "mgrs_tile": "31UFT",
                "start": datetime(2024, 5, 2),
                "end": datetime(2024, 5, 1),
            },
            "end must be >= start",
        ),
    ],
)
def test_discover_input_validation(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Sentinel2CDSEConnector().discover(**kwargs)
