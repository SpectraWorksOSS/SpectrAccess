from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
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


def _write_jasmin_family(root: Path) -> tuple[Path, ...]:
    date_dir = root / DATE_LABEL
    date_dir.mkdir(parents=True)
    paths = tuple(date_dir / f"{DATE_LABEL}_{name}.tif" for name in ("aod550", "tcwv", "gtco3"))
    for path in paths:
        path.write_bytes(b"fixture")
    return paths


def test_cached_jasmin_result_has_explicit_base_and_date_contract(tmp_path):
    paths = _write_jasmin_family(tmp_path)

    result = CAMSConnector(cache_dir=tmp_path, source="jasmin").resolve(SCENE_DATE)

    assert result.base_dir == tmp_path
    assert result.date_dir == tmp_path / DATE_LABEL
    assert result.files == paths
    assert result.resolved_source == "jasmin"
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

    assert result.resolved_source == "jasmin"
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
