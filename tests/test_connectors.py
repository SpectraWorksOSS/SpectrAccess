from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from spectraccess.connectors.gsics import GSICSConnector
from spectraccess.connectors.gsics.connector import GSICSCatalog, to_canonical
from spectraccess.connectors.modis_viirs_cal import MODISNotImplemented, VIIRSCalibrationConnector
from spectraccess.connectors.radcalnet import RadCalNetConnector


FIXTURES = Path(__file__).parent / "fixtures"


def test_gsics_parse_fixture():
    parsed = GSICSConnector().parse(FIXTURES / "gsics_coefficients.csv")
    assert list(parsed.columns) == [
        "timestamp",
        "sensor",
        "band",
        "correction_coefficient",
        "source_agency",
    ]
    assert parsed.loc[0, "source_agency"] == "EUMETSAT"


def test_gsics_parse_netcdf_fixture():
    fixture = FIXTURES / "gsics_msg4_seviri_metopb_iasi_nrtc_20260704.nc"
    parsed = GSICSConnector().parse(fixture)

    assert len(parsed) == 8
    assert (parsed["sensor"] == "MSG4 SEVIRI").all()
    assert (parsed["reference_sensor"] == "MetOpB IASI").all()

    assert pd.api.types.is_numeric_dtype(parsed["slope"])
    assert pd.api.types.is_numeric_dtype(parsed["offset"])
    assert parsed["slope"].notna().all()
    assert parsed["offset"].notna().all()


def test_gsics_to_canonical_from_netcdf_fixture():
    fixture = FIXTURES / "gsics_msg4_seviri_metopb_iasi_nrtc_20260704.nc"
    native = GSICSConnector().parse(fixture)

    # Fixture check (not hardcoded blindly): 8 channels x 1 date, with slope,
    # offset, std_scene_tb_bias, their _se columns, and central_wavelength all
    # present and fully populated -- so melting yields 8 x 3 = 24 rows, all
    # with a provided uncertainty.
    assert len(native) == 8
    for column in ("slope", "slope_se", "offset", "offset_se", "std_scene_tb_bias", "std_scene_tb_bias_se"):
        assert column in native.columns
        assert native[column].notna().all()

    canonical = to_canonical(native, source_url="https://example.test/gsics.nc")

    assert len(canonical) == 24
    assert set(canonical["quantity"]) == {
        "gsics_correction_slope",
        "gsics_correction_offset",
        "gsics_std_scene_tb_bias",
    }
    assert (canonical["quantity"].value_counts() == 8).all()
    assert (canonical["unc_status"] == "provided").all()
    assert (canonical["source"] == "gsics").all()
    assert (canonical["instrument"] == "MSG4 SEVIRI").all()
    assert (canonical["reference"] == "MetOpB IASI").all()
    assert "central_wavelength" in canonical.columns
    assert canonical["central_wavelength"].notna().all()
    assert canonical["wavelength_nm"].isna().all()
    assert canonical.attrs["spectraccess_schema_version"] == "1.0"


def test_gsics_parse_canonical_matches_two_step_path():
    fixture = FIXTURES / "gsics_msg4_seviri_metopb_iasi_nrtc_20260704.nc"
    connector = GSICSConnector()

    two_step = to_canonical(connector.parse(fixture), source_url="https://example.test/gsics.nc")
    end_to_end = connector.parse_canonical(fixture, source_url="https://example.test/gsics.nc")

    pd.testing.assert_frame_equal(two_step.reset_index(drop=True), end_to_end.reset_index(drop=True))


def test_gsics_discover_is_stub_without_verified_catalogs():
    catalogs = [
        GSICSCatalog("EUMETSAT", None),
        GSICSCatalog("NOAA STAR", None),
        GSICSCatalog("CMA", None),
    ]
    with pytest.raises(NotImplementedError, match="STOPPED-AT-STUB"):
        GSICSConnector(catalogs=catalogs).discover()


def test_viirs_parse_fixture():
    parsed = VIIRSCalibrationConnector().parse(FIXTURES / "viirs_factors.csv")
    assert list(parsed.columns) == ["timestamp", "sensor", "platform", "band", "f_factor", "source_agency"]
    assert parsed.loc[0, "source_agency"] == "NOAA STAR"


def test_viirs_discover_is_stub_without_verified_catalog():
    with pytest.raises(NotImplementedError, match="STOPPED-AT-STUB"):
        VIIRSCalibrationConnector().discover()


def test_modis_is_documented_stub():
    with pytest.raises(NotImplementedError, match="web-only"):
        MODISNotImplemented().discover()


def test_radcalnet_parse_fixture(monkeypatch):
    monkeypatch.setenv("RADCALNET_USERNAME", "user")
    monkeypatch.setenv("RADCALNET_PASSWORD", "secret")
    parsed = RadCalNetConnector().parse(FIXTURES / "radcalnet_daily.csv")
    assert {"timestamp", "site", "wavelength_nm", "reflectance", "uncertainty"} <= set(parsed.columns)


def test_radcalnet_live_fetch_is_stub(monkeypatch):
    monkeypatch.setenv("RADCALNET_USERNAME", "user")
    monkeypatch.setenv("RADCALNET_PASSWORD", "secret")
    with pytest.raises(NotImplementedError, match="STOPPED-AT-STUB"):
        RadCalNetConnector().fetch("daily")

