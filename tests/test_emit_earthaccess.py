from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from spectraccess.connectors.emit_earthaccess import (
    EMITDownloadError,
    EMITEarthaccessConnector,
    EMITProductError,
    EMITProviderError,
    target_to_canonical,
)
from spectraccess.connectors.emit_earthaccess import connector as module


PRIMARY = "EMIT_L2A_RFL_001_20240101T010326_2400101_002.nc"
UNCERTAINTY = "EMIT_L2A_RFLUNCERT_001_20240101T010326_2400101_002.nc"
MASK = "EMIT_L2A_MASK_001_20240101T010326_2400101_002.nc"
BASE = (
    "https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/"
    "EMITL2ARFL.001/EMIT_L2A_RFL_001_20240101T010326_2400101_002/"
)


def _granule() -> dict:
    assets = [(PRIMARY, 101), (UNCERTAINTY, 102), (MASK, 103)]
    return {
        "meta": {
            "concept-id": "G2828535051-LPCLOUD",
            "native-id": "EMIT_L2A_RFL_001_20240101T010326_2400101_002",
            "collection-concept-id": "C2408750690-LPCLOUD",
            "provider-id": "LPCLOUD",
        },
        "umm": {
            "TemporalExtent": {
                "RangeDateTime": {
                    "BeginningDateTime": "2024-01-01T01:03:26Z",
                    "EndingDateTime": "2024-01-01T01:03:38Z",
                }
            },
            "GranuleUR": "EMIT_L2A_RFL_001_20240101T010326_2400101_002",
            "AdditionalAttributes": [
                {"Name": "ORBIT", "Values": ["2400101"]},
                {"Name": "ORBIT_SEGMENT", "Values": ["1"]},
                {"Name": "SCENE", "Values": ["2"]},
                {"Name": "SOLAR_ZENITH", "Values": ["20.24"]},
                {"Name": "SOLAR_AZIMUTH", "Values": ["329.71"]},
            ],
            "SpatialExtent": {
                "HorizontalSpatialDomain": {
                    "Geometry": {
                        "GPolygons": [
                            {
                                "Boundary": {
                                    "Points": [
                                        {"Longitude": 175.58415, "Latitude": -40.48677},
                                        {"Longitude": 175.18309, "Latitude": -41.20219},
                                        {"Longitude": 176.07585, "Latitude": -41.70265},
                                        {"Longitude": 176.47691, "Latitude": -40.98723},
                                        {"Longitude": 175.58415, "Latitude": -40.48677},
                                    ]
                                }
                            }
                        ]
                    }
                }
            },
            "CollectionReference": {"ShortName": "EMITL2ARFL", "Version": "001"},
            "RelatedUrls": [
                {"URL": BASE + name, "Description": "Download " + name, "Type": "GET DATA"}
                for name, _size in assets
            ],
            "CloudCover": 69,
            "DataGranule": {
                "ArchiveAndDistributionInformation": [
                    {
                        "Name": name,
                        "SizeInBytes": size,
                        "Format": "NETCDF-4",
                        "Checksum": {"Value": "0" * 128, "Algorithm": "SHA-512"},
                    }
                    for name, size in assets
                ]
            },
            "Platforms": [
                {
                    "ShortName": "ISS",
                    "Instruments": [{"ShortName": "EMIT Imaging Spectrometer"}],
                }
            ],
        },
        "size": 3575.885766983032,
    }


def _discover(monkeypatch, granules=None):
    calls = []

    def fake_search_data(**kwargs):
        calls.append(kwargs)
        return [_granule()] if granules is None else granules

    monkeypatch.setattr(module.earthaccess, "search_data", fake_search_data)
    result = EMITEarthaccessConnector().discover(
        product="EMITL2ARFL",
        bbox=(175.0, -42.0, 177.0, -40.0),
        start=date(2024, 1, 1),
        end=date(2024, 1, 1),
        limit=1,
    )
    return result, calls


def test_discover_delegates_to_earthaccess_and_preserves_cmr_provenance(monkeypatch):
    targets, calls = _discover(monkeypatch)
    assert calls == [
        {
            "short_name": "EMITL2ARFL",
            "version": "001",
            "count": 1,
            "bounding_box": (175.0, -42.0, 177.0, -40.0),
            "temporal": (
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                datetime(2024, 1, 1, 23, 59, 59, 999999, tzinfo=timezone.utc),
            ),
        }
    ]
    target = targets[0]
    assert target.product_id == "EMIT_L2A_RFL_001_20240101T010326_2400101_002"
    assert target.concept_id == "G2828535051-LPCLOUD"
    assert target.collection_concept_id == "C2408750690-LPCLOUD"
    assert target.provider_id == "LPCLOUD"
    assert target.platform_id == "ISS"
    assert target.sensor_id == "EMIT Imaging Spectrometer"
    assert target.orbit == "2400101"
    assert target.scene == "2"
    assert target.cloud_cover == 69
    assert target.solar_zenith_deg == pytest.approx(20.24)
    assert target.solar_azimuth_deg == pytest.approx(329.71)
    assert target.size_mb == pytest.approx(3575.885766983032)
    assert set(target.assets) == {PRIMARY, UNCERTAINTY, MASK}
    assert target.checksums[PRIMARY] == ("SHA-512", "0" * 128)
    assert target.footprint_wkt.startswith("POLYGON ((175.58415 -40.48677")
    assert target.raw["umm"]["CollectionReference"]["ShortName"] == "EMITL2ARFL"


@pytest.mark.parametrize("product", ["EMITL2BMIN", "SENTINEL2"])
def test_discover_rejects_unreviewed_product_before_provider_call(monkeypatch, product):
    monkeypatch.setattr(
        module.earthaccess,
        "search_data",
        lambda **_kwargs: pytest.fail("provider should not be called"),
    )
    with pytest.raises(ValueError, match="unsupported EMIT product"):
        EMITEarthaccessConnector().discover(product=product)


def test_discover_rejects_unreviewed_version_and_unbounded_limit():
    with pytest.raises(ValueError, match="reviewed version"):
        EMITEarthaccessConnector().discover(version="002")
    with pytest.raises(ValueError, match="between 0 and 2000"):
        EMITEarthaccessConnector().discover(limit=2001)
    with pytest.raises(ValueError, match="invalid EPSG:4326 bbox"):
        EMITEarthaccessConnector().discover(bbox=(10, 20, -10, 30))


def test_discover_wraps_provider_failure(monkeypatch):
    monkeypatch.setattr(
        module.earthaccess,
        "search_data",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("CMR unavailable")),
    )
    with pytest.raises(EMITProviderError, match="CMR unavailable"):
        EMITEarthaccessConnector().discover()


def test_discover_fails_closed_on_collection_or_asset_metadata_drift(monkeypatch):
    wrong_collection = _granule()
    wrong_collection["umm"]["CollectionReference"]["ShortName"] = "EMITL1BRAD"
    monkeypatch.setattr(module.earthaccess, "search_data", lambda **_kwargs: [wrong_collection])
    with pytest.raises(EMITProductError, match="expected 'EMITL2ARFL'"):
        EMITEarthaccessConnector().discover()

    missing_asset = _granule()
    missing_asset["umm"]["RelatedUrls"].pop()
    monkeypatch.setattr(module.earthaccess, "search_data", lambda **_kwargs: [missing_asset])
    with pytest.raises(EMITProductError, match="assets disagree"):
        EMITEarthaccessConnector().discover()


def test_fetch_downloads_one_selected_asset_and_verifies_sha512(monkeypatch, tmp_path):
    targets, _calls = _discover(monkeypatch)
    target = targets[0]
    payload = b"small-fixture-not-an-emit-cube"
    checksum = hashlib.sha512(payload).hexdigest()
    target = replace(target, checksums={**target.checksums, PRIMARY: ("SHA-512", checksum)})
    login_calls = []
    download_calls = []

    monkeypatch.setattr(
        module.earthaccess, "login", lambda **kwargs: login_calls.append(kwargs)
    )

    def fake_download(urls, *, local_path, threads, show_progress):
        download_calls.append((urls, Path(local_path), threads, show_progress))
        output = Path(local_path) / PRIMARY
        output.write_bytes(payload)
        return [output]

    monkeypatch.setattr(module.earthaccess, "download", fake_download)
    path = EMITEarthaccessConnector().fetch(target, dest=tmp_path)
    assert Path(path).read_bytes() == payload
    assert login_calls == [{"strategy": "environment"}]
    assert download_calls == [([BASE + PRIMARY], tmp_path, 1, False)]


def test_fetch_rejects_off_origin_and_checksum_mismatch(monkeypatch, tmp_path):
    targets, _calls = _discover(monkeypatch)
    target = targets[0]
    poisoned = replace(target, assets={PRIMARY: "https://evil.example/scene.nc"})
    with pytest.raises(EMITDownloadError, match="off-origin"):
        EMITEarthaccessConnector().fetch(poisoned, dest=tmp_path)

    monkeypatch.setattr(module.earthaccess, "login", lambda **_kwargs: None)

    def fake_download(_urls, *, local_path, **_kwargs):
        output = Path(local_path) / PRIMARY
        output.write_bytes(b"wrong")
        return [output]

    monkeypatch.setattr(module.earthaccess, "download", fake_download)
    with pytest.raises(EMITDownloadError, match="checksum mismatch"):
        EMITEarthaccessConnector().fetch(target, dest=tmp_path)


def test_l1b_primary_selector_does_not_ambiguously_include_observation_companion():
    targets = {
        "EMIT_L1B_RAD_001_scene.nc": "https://data.lpdaac.earthdatacloud.nasa.gov/x/rad.nc",
        "EMIT_L1B_OBS_001_scene.nc": "https://data.lpdaac.earthdatacloud.nasa.gov/x/obs.nc",
    }
    l2_target = module._target_from_granule(_granule(), expected_product="EMITL2ARFL")
    l1_target = replace(l2_target, collection="EMITL1BRAD", assets=targets)
    assert module._select_asset(l1_target, "primary") == (
        "EMIT_L1B_RAD_001_scene.nc",
        targets["EMIT_L1B_RAD_001_scene.nc"],
    )
    assert module._select_asset(l1_target, "observation") == (
        "EMIT_L1B_OBS_001_scene.nc",
        targets["EMIT_L1B_OBS_001_scene.nc"],
    )


def test_parse_and_canonical_keep_product_provenance_and_unknown_uncertainty(monkeypatch):
    targets, _calls = _discover(monkeypatch)
    target = targets[0]
    native = EMITEarthaccessConnector().parse("scene.nc", target=target)
    assert native.loc[0, "concept_id"] == "G2828535051-LPCLOUD"
    assert native.loc[0, "local_path"] == "scene.nc"

    canonical = target_to_canonical(target, local_path="scene.nc")
    assert set(canonical["quantity"]) == {
        "scene_cloud_cover",
        "solar_zenith_angle",
        "solar_azimuth_angle",
    }
    assert canonical["unc_status"].eq("unknown").all()
    assert canonical["unc_value"].isna().all()
    assert canonical["unc_k"].isna().all()
    assert canonical["unc_provider"].isna().all()
    assert canonical["source"].eq("nasa-earthdata-lp-daac").all()
    assert canonical["product_id"].eq(target.product_id).all()
    assert canonical.attrs["spectraccess_schema_version"] == "1.0"


def test_connector_run_carries_target_into_parse(monkeypatch, tmp_path):
    targets, _calls = _discover(monkeypatch)
    connector = EMITEarthaccessConnector()
    monkeypatch.setattr(connector, "discover", lambda **_kwargs: targets)
    monkeypatch.setattr(connector, "fetch", lambda _target, **_kwargs: str(tmp_path / PRIMARY))
    result = connector.run(canonical=True, fetch_kwargs={"dest": tmp_path})
    assert result["product_id"].eq(targets[0].product_id).all()
    assert result["source"].eq("nasa-earthdata-lp-daac").all()
