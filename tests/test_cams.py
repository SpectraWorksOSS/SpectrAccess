from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from spectraccess.connectors.cams import (
    ADS_DATASET,
    CAMSADSDateNotFoundError,
    CAMSConnector,
    CAMSDateUnavailableError,
    CAMSProviderError,
    CAMSResult,
)
from spectraccess.connectors.cams import connector as cams_module


SCENE_DATE = datetime(2025, 10, 4, 10, 36, tzinfo=timezone.utc)
DATE_LABEL = "2025_10_04"
RECORDED_AT = "2025-10-05T12:34:56+00:00"


def _write_jasmin_family(root: Path) -> tuple[Path, ...]:
    date_dir = root / DATE_LABEL
    date_dir.mkdir(parents=True)
    paths = tuple(date_dir / f"{DATE_LABEL}_{name}.tif" for name in ("aod550", "tcwv", "gtco3"))
    for path in paths:
        path.write_bytes(b"fixture")
    return paths


def _write_manifest(
    date_dir: Path,
    *,
    source: str,
    source_url: str,
    assets: list[str],
    recorded_at: str = RECORDED_AT,
) -> None:
    (date_dir / "spectraccess-cams-source.json").write_text(
        json.dumps(
            {
                "schema": "spectraccess-cams-source-v1",
                "resolved_source": source,
                "source_url": source_url,
                "assets": assets,
                "recorded_at": recorded_at,
            }
        ),
        encoding="utf-8",
    )


def test_cached_jasmin_result_has_explicit_base_and_date_contract(tmp_path):
    paths = _write_jasmin_family(tmp_path)

    result = CAMSConnector(cache_dir=tmp_path, source="auto").resolve(SCENE_DATE)

    assert result.base_dir == tmp_path
    assert result.date_dir == tmp_path / DATE_LABEL
    assert result.files == paths
    assert result.resolved_source == "cache-unknown"
    assert result.source_url is None
    assert result.retrieved_at is None
    assert result.cache_hit is True
    siac_path = result.base_dir / DATE_LABEL / f"{DATE_LABEL}_aod550.tif"
    assert siac_path == paths[0]
    assert DATE_LABEL + DATE_LABEL not in str(siac_path)


def test_result_rejects_date_directory_masquerading_as_base(tmp_path):
    date_dir = tmp_path / DATE_LABEL
    with pytest.raises(ValueError, match="date_dir must equal base_dir"):
        CAMSResult(
            scene_date=SCENE_DATE,
            requested_source="ads",
            resolved_source="ads",
            base_dir=date_dir,
            date_dir=date_dir,
            files=(date_dir / "raw.nc",),
            source_url="https://example.test",
            retrieved_at=SCENE_DATE,
            cache_hit=False,
            dataset=ADS_DATASET,
        )


def test_auto_prefers_jasmin_without_touching_ads(tmp_path, monkeypatch):
    _write_jasmin_family(tmp_path)
    connector = CAMSConnector(cache_dir=tmp_path, source="auto", ads_token="secret")
    ads = Mock(side_effect=AssertionError("ADS must not run on a JASMIN/cache hit"))
    monkeypatch.setattr(connector, "_fetch_ads", ads)

    result = connector.resolve(SCENE_DATE)

    assert result.resolved_source == "cache-unknown"
    ads.assert_not_called()


def test_auto_falls_back_to_ads_only_after_definitive_jasmin_gap(tmp_path, monkeypatch):
    connector = CAMSConnector(cache_dir=tmp_path, source="auto", ads_token="secret")
    monkeypatch.setattr(
        connector,
        "_fetch_jasmin",
        Mock(side_effect=CAMSDateUnavailableError("gap")),
    )
    expected = CAMSResult(
        scene_date=SCENE_DATE,
        requested_source="auto",
        resolved_source="ads",
        base_dir=tmp_path,
        date_dir=tmp_path / DATE_LABEL,
        files=(tmp_path / DATE_LABEL / "raw.nc",),
        source_url="https://example.test",
        retrieved_at=SCENE_DATE,
        cache_hit=False,
        dataset=ADS_DATASET,
    )
    monkeypatch.setattr(connector, "_fetch_ads", Mock(return_value=expected))

    assert connector.resolve(SCENE_DATE) is expected


def test_auto_does_not_hide_jasmin_operational_failure(tmp_path, monkeypatch):
    connector = CAMSConnector(cache_dir=tmp_path, source="auto", ads_token="secret")
    monkeypatch.setattr(
        connector,
        "_fetch_jasmin",
        Mock(side_effect=CAMSProviderError("network")),
    )
    ads = Mock()
    monkeypatch.setattr(connector, "_fetch_ads", ads)

    with pytest.raises(CAMSProviderError, match="network"):
        connector.resolve(SCENE_DATE)
    ads.assert_not_called()


def test_auto_maps_both_definitive_date_gaps_to_unavailable(tmp_path, monkeypatch):
    connector = CAMSConnector(cache_dir=tmp_path, source="auto", ads_token="secret")
    monkeypatch.setattr(connector, "_fetch_jasmin", Mock(side_effect=CAMSDateUnavailableError("gap")))
    monkeypatch.setattr(connector, "_fetch_ads", Mock(side_effect=CAMSADSDateNotFoundError("gap")))

    with pytest.raises(CAMSDateUnavailableError, match="unavailable from JASMIN and ADS"):
        connector.resolve(SCENE_DATE)


def test_ads_uses_maintained_client_and_preserves_provenance(tmp_path, monkeypatch):
    calls = {}

    class FakeClient:
        def retrieve(self, dataset, request, target):
            calls.update(dataset=dataset, request=request, target=target)
            Path(target).write_bytes(b"netcdf")

    monkeypatch.setattr(cams_module, "_cds_client", lambda url, token: FakeClient())
    connector = CAMSConnector(cache_dir=tmp_path, source="ads", ads_token="secret")

    result = connector.resolve(SCENE_DATE)
    frame = connector.parse(result)

    assert calls["dataset"] == ADS_DATASET
    assert calls["request"]["date"] == ["2025-10-04"]
    assert calls["request"]["data_format"] == "netcdf"
    assert result.base_dir == tmp_path
    assert result.date_dir == tmp_path / DATE_LABEL
    assert result.files[0].read_bytes() == b"netcdf"
    assert frame.loc[0, "requested_source"] == "ads"
    assert frame.loc[0, "resolved_source"] == "ads"
    assert frame.loc[0, "dataset"] == ADS_DATASET
    assert "secret" not in frame.to_string()


def test_ads_errors_redact_token(tmp_path, monkeypatch):
    token = "never-print-this-token"

    class BrokenClient:
        def retrieve(self, dataset, request, target):
            raise RuntimeError(f"provider exploded with {token}")

    monkeypatch.setattr(cams_module, "_cds_client", lambda url, supplied: BrokenClient())

    with pytest.raises(CAMSProviderError) as caught:
        CAMSConnector(cache_dir=tmp_path, source="ads", ads_token=token).resolve(SCENE_DATE)
    assert token not in str(caught.value)


def test_ads_ambiguous_not_available_message_remains_hard_failure(tmp_path, monkeypatch):
    class AmbiguousClient:
        def retrieve(self, dataset, request, target):
            raise RuntimeError("service temporarily not available")

    monkeypatch.setattr(cams_module, "_cds_client", lambda url, supplied: AmbiguousClient())

    with pytest.raises(CAMSProviderError) as caught:
        CAMSConnector(cache_dir=tmp_path, source="ads", ads_token="secret").resolve(SCENE_DATE)
    assert type(caught.value) is CAMSProviderError


def test_ads_accepts_only_typed_provider_confirmed_date_absence(tmp_path, monkeypatch):
    class ConfirmedAbsentClient:
        def retrieve(self, dataset, request, target):
            raise CAMSADSDateNotFoundError("provider-confirmed date absence")

    monkeypatch.setattr(cams_module, "_cds_client", lambda url, supplied: ConfirmedAbsentClient())

    with pytest.raises(CAMSADSDateNotFoundError, match="provider-confirmed"):
        CAMSConnector(cache_dir=tmp_path, source="ads", ads_token="secret").resolve(SCENE_DATE)


def test_new_jasmin_fetch_records_actual_fallback_origin(tmp_path, monkeypatch):
    connector = CAMSConnector(
        cache_dir=tmp_path,
        source="jasmin",
        fallback_url="https://fallback.example/cams",
        max_attempts=1,
    )
    monkeypatch.setattr(
        connector,
        "_date_available",
        lambda base, label: base == "https://fallback.example/cams",
    )

    def fake_download(url, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")

    monkeypatch.setattr(connector, "_download", fake_download)
    result = connector.resolve(SCENE_DATE)

    assert result.resolved_source == "jasmin"
    assert result.source_url == "https://fallback.example/cams"
    assert result.retrieved_at is not None
    manifest = (result.date_dir / "spectraccess-cams-source.json").read_text()
    assert "fallback.example" in manifest


def test_known_jasmin_cache_uses_sidecar_recorded_at(tmp_path):
    paths = _write_jasmin_family(tmp_path)
    _write_manifest(
        tmp_path / DATE_LABEL,
        source="jasmin",
        source_url="https://jasmin.example/cams",
        assets=[path.name for path in paths],
    )

    result = CAMSConnector(cache_dir=tmp_path, source="auto").resolve(SCENE_DATE)

    assert result.resolved_source == "jasmin"
    assert result.retrieved_at == datetime.fromisoformat(RECORDED_AT)


def test_unknown_ads_cache_is_not_backfilled_with_invented_provenance(tmp_path):
    date_dir = tmp_path / DATE_LABEL
    date_dir.mkdir(parents=True)
    raw = date_dir / f"cams_eac4_{DATE_LABEL}.nc"
    raw.write_bytes(b"netcdf")

    result = CAMSConnector(cache_dir=tmp_path, source="ads", ads_token="secret").resolve(SCENE_DATE)

    assert result.resolved_source == "cache-unknown"
    assert result.source_url is None
    assert result.retrieved_at is None
    assert result.dataset is None
    assert not (date_dir / "spectraccess-cams-source.json").exists()


def test_partial_legacy_jasmin_cache_is_wholly_replaced_before_labelling(
    tmp_path, monkeypatch
):
    date_dir = tmp_path / DATE_LABEL
    date_dir.mkdir(parents=True)
    old = date_dir / f"{DATE_LABEL}_aod550.tif"
    old.write_bytes(b"legacy-unknown")
    connector = CAMSConnector(cache_dir=tmp_path, source="jasmin", max_attempts=1)
    monkeypatch.setattr(connector, "_date_available", lambda base, label: True)

    def fresh_download(url, path):
        path.write_bytes(b"fresh-known")

    monkeypatch.setattr(connector, "_download", fresh_download)
    result = connector.resolve(SCENE_DATE)

    assert result.resolved_source == "jasmin"
    assert all(path.read_bytes() == b"fresh-known" for path in result.files)
    assert old.read_bytes() == b"fresh-known"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema": "wrong"},
        {
            "schema": "spectraccess-cams-source-v1",
            "resolved_source": "unknown",
            "source_url": "https://jasmin.example/cams",
            "assets": [],
            "recorded_at": RECORDED_AT,
        },
        {
            "schema": "spectraccess-cams-source-v1",
            "resolved_source": "jasmin",
            "source_url": "not-a-url",
            "assets": [f"{DATE_LABEL}_aod550.tif"],
            "recorded_at": RECORDED_AT,
        },
        {
            "schema": "spectraccess-cams-source-v1",
            "resolved_source": "jasmin",
            "source_url": "https://jasmin.example/cams",
            "assets": ["../escape.tif"],
            "recorded_at": RECORDED_AT,
        },
        {
            "schema": "spectraccess-cams-source-v1",
            "resolved_source": "jasmin",
            "source_url": "https://jasmin.example/cams",
            "assets": [
                f"{DATE_LABEL}_aod550.tif",
                f"{DATE_LABEL}_tcwv.tif",
                f"{DATE_LABEL}_gtco3.tif",
            ],
            "recorded_at": "2025-10-05T12:34:56",
        },
    ],
)
def test_invalid_sidecar_is_rejected_not_trusted(tmp_path, payload):
    _write_jasmin_family(tmp_path)
    (tmp_path / DATE_LABEL / "spectraccess-cams-source.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(CAMSProviderError):
        CAMSConnector(cache_dir=tmp_path, source="auto").resolve(SCENE_DATE)


def test_sidecar_with_wrong_or_incomplete_asset_family_is_rejected(tmp_path):
    paths = _write_jasmin_family(tmp_path)
    _write_manifest(
        tmp_path / DATE_LABEL,
        source="jasmin",
        source_url="https://jasmin.example/cams",
        assets=[paths[0].name],
    )

    with pytest.raises(CAMSProviderError, match="complete asset family"):
        CAMSConnector(cache_dir=tmp_path, source="auto").resolve(SCENE_DATE)


def test_ads_sidecar_cannot_relabel_jasmin_asset_family(tmp_path):
    paths = _write_jasmin_family(tmp_path)
    _write_manifest(
        tmp_path / DATE_LABEL,
        source="ads",
        source_url="https://ads.example/process",
        assets=[path.name for path in paths],
    )

    with pytest.raises(CAMSProviderError, match="does not match expected"):
        CAMSConnector(cache_dir=tmp_path, source="auto", ads_token="secret").resolve(SCENE_DATE)
