from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from spectraccess.connectors.gsics import GSICSConnector
from spectraccess.connectors.gsics.connector import GSICSCatalog, to_canonical
from spectraccess.connectors.modis_viirs_cal import MODISNotImplemented, VIIRSCalibrationConnector
from spectraccess.connectors.radcalnet import RadCalNetConnector, RadCalNetTarget
from spectraccess.connectors.radcalnet.connector import (
    DEFAULT_BASE_URL,
    to_canonical as radcalnet_to_canonical,
)
from spectraccess.connectors.aeronet import (
    AeronetConnector,
    AeronetSchemaError,
    AeronetSiteMismatchError,
    interpolate_aod_550,
    parse_aeronet_csv,
    to_canonical as aeronet_to_canonical,
)


FIXTURES = Path(__file__).parent / "fixtures"
RADCALNET_FIXTURE = FIXTURES / "radcalnet" / "GSCN01_2025_334_v04.05.output"
AERONET_GRANADA = FIXTURES / "aeronet" / "Granada_2024_06_15_L2.0.csv"
AERONET_ISPRA = FIXTURES / "aeronet" / "Ispra_2024_07_15_L2.0.csv"


def test_aeronet_parse_granada_fixture():
    frame = parse_aeronet_csv(AERONET_GRANADA.read_text(encoding="utf-8"), requested_site="Granada")
    assert set(frame.columns) == {
        "observation_index",
        "timestamp", "site", "latitude", "longitude", "elevation_m", "data_level",
        "wavelength_nm", "aod", "pw_cm", "angstrom_440_870",
    }
    assert {500.0, 675.0} <= set(frame["wavelength_nm"])
    assert frame["pw_cm"].iloc[0] == pytest.approx(2.12, abs=0.01)
    assert str(frame["timestamp"].dt.tz) == "UTC"
    assert frame["site"].eq("Granada").all()


def test_aeronet_parse_ispra_fixture_seaprism_bands():
    frame = parse_aeronet_csv(AERONET_ISPRA.read_text(encoding="utf-8"), requested_site="Ispra")
    assert {510.0, 560.0} <= set(frame["wavelength_nm"])
    assert 500.0 not in set(frame["wavelength_nm"])
    assert frame["pw_cm"].notna().all()


def test_aeronet_interpolation_uses_instrument_specific_brackets():
    for fixture, site, expected in (
        (AERONET_ISPRA, "Ispra", (510, 560)),
        (AERONET_GRANADA, "Granada", (500, 675)),
    ):
        frame = parse_aeronet_csv(fixture.read_text(encoding="utf-8"), requested_site=site)
        observation = frame[frame["timestamp"] == frame["timestamp"].iloc[0]]
        bands = dict(zip(observation["wavelength_nm"].astype(int), observation["aod"]))
        result = interpolate_aod_550(bands)
        assert result is not None
        value, lo, hi = result
        assert (lo, hi) == expected
        assert min(bands[lo], bands[hi]) <= value <= max(bands[lo], bands[hi])


def _aeronet_csv(header: str, row: str) -> str:
    return f"metadata\n{header}\n{row}\n"


def test_aeronet_rejects_site_mismatch():
    text = _aeronet_csv(
        "Date(dd:mm:yyyy),Time(hh:mm:ss),Precipitable_Water(cm),AERONET_Site,AOD_500nm,AOD_675nm",
        "01:01:2024,12:00:00,1.2,Wrong_Site,0.1,0.08",
    )
    with pytest.raises(AeronetSiteMismatchError):
        parse_aeronet_csv(text, requested_site="Right_Site")


def test_aeronet_rejects_missing_required_column_and_insufficient_bands():
    missing_pw = _aeronet_csv(
        "Date(dd:mm:yyyy),Time(hh:mm:ss),AERONET_Site,AOD_500nm,AOD_675nm",
        "01:01:2024,12:00:00,Site,0.1,0.08",
    )
    with pytest.raises(AeronetSchemaError, match="Precipitable_Water"):
        parse_aeronet_csv(missing_pw)

    one_band = _aeronet_csv(
        "Date(dd:mm:yyyy),Time(hh:mm:ss),Precipitable_Water(cm),AERONET_Site,AOD_500nm",
        "01:01:2024,12:00:00,1.2,Site,0.1",
    )
    with pytest.raises(AeronetSchemaError, match="fewer than 2"):
        parse_aeronet_csv(one_band)


def test_aeronet_discover_and_fetch(requests_mock):
    connector = AeronetConnector()
    target = connector.discover(
        site="Granada", start=datetime(2024, 6, 15), end=datetime(2024, 6, 15), level="L2.0"
    )[0]
    assert "site=Granada" in target.url
    assert "year=2024" in target.url
    assert "AOD20=1" in target.url
    assert "if_no_html=1" in target.url
    requests_mock.get(target.url, text=AERONET_GRANADA.read_text(encoding="utf-8"))
    assert connector.fetch(target).startswith(b"AERONET Data Download")
    assert "Authorization" not in requests_mock.last_request.headers


def test_aeronet_to_canonical_includes_aod_and_deduplicated_pw():
    native = parse_aeronet_csv(AERONET_GRANADA.read_text(encoding="utf-8"), requested_site="Granada")
    canonical = aeronet_to_canonical(native)
    assert canonical.attrs["spectraccess_schema_version"] == "1.0"
    aod = canonical[canonical["quantity"] == "aerosol_optical_depth"]
    assert aod["unc_status"].eq("unknown").all()
    assert aod["unc_value"].isna().all()
    pw = canonical[canonical["quantity"] == "precipitable_water"]
    assert len(pw) == native["timestamp"].nunique()
    assert pw["units"].eq("cm").all()
    assert canonical["source"].eq("aeronet").all()


def test_aeronet_run_canonical_end_to_end_carries_source_url(requests_mock):
    connector = AeronetConnector()
    target = connector.discover(
        site="Granada", start=datetime(2024, 6, 15), end=datetime(2024, 6, 15)
    )[0]
    requests_mock.get(target.url, text=AERONET_GRANADA.read_text(encoding="utf-8"))
    canonical = connector.run(
        site="Granada", start=datetime(2024, 6, 15), end=datetime(2024, 6, 15), canonical=True
    )
    assert canonical.attrs["spectraccess_schema_version"] == "1.0"
    assert canonical["source_url"].eq(target.url).all()


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
    # The fixture's central_wavelength declares units "m", so wavelength_nm is
    # populated via an honest unit conversion (3.92e-06 m -> 3920 nm).
    assert canonical["wavelength_nm"].notna().all()
    assert canonical["wavelength_nm"].min() == pytest.approx(3920.0)
    assert canonical.attrs["spectraccess_schema_version"] == "1.0"


def test_gsics_to_canonical_from_csv_fallback_frame():
    # CSV-fallback native frames carry the generic correction_coefficient
    # column; they must melt to canonical rows (all-unknown uncertainty),
    # never to a silently empty frame.
    native = GSICSConnector().parse(FIXTURES / "gsics_coefficients.csv")
    canonical = to_canonical(native)

    assert len(canonical) == len(native)
    assert (canonical["quantity"] == "gsics_correction_coefficient").all()
    assert (canonical["unc_status"] == "unknown").all()
    assert canonical["unc_value"].isna().all()


def test_gsics_to_canonical_rejects_unrecognized_frame():
    frame = pd.DataFrame({"foo": [1.0], "bar": [2.0]})
    with pytest.raises(ValueError, match="no recognised GSICS quantity columns"):
        to_canonical(frame)


def test_gsics_parse_canonical_carries_retrieved_at():
    fixture = FIXTURES / "gsics_msg4_seviri_metopb_iasi_nrtc_20260704.nc"
    stamp = pd.Timestamp("2026-07-06T12:00:00")
    canonical = GSICSConnector().parse_canonical(fixture, retrieved_at=stamp)
    assert (canonical["retrieved_at"] == stamp).all()


def test_gsics_parse_rejects_empty_payload():
    with pytest.raises(ValueError, match="empty GSICS payload"):
        GSICSConnector().parse(b"")


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


def _radcalnet_connector(monkeypatch) -> RadCalNetConnector:
    monkeypatch.setenv("RADCALNET_USERNAME", "user")
    monkeypatch.setenv("RADCALNET_PASSWORD", "secret")
    return RadCalNetConnector()


def test_radcalnet_requires_credentials(monkeypatch):
    monkeypatch.delenv("RADCALNET_USERNAME", raising=False)
    monkeypatch.delenv("RADCALNET_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="RADCALNET_USERNAME"):
        RadCalNetConnector()


def test_radcalnet_sites_via_requests_mock(monkeypatch, requests_mock):
    connector = _radcalnet_connector(monkeypatch)
    requests_mock.get(
        DEFAULT_BASE_URL,
        json=[{"name": "GSCN"}, {"name": "BSCN"}, {"name": "Australia"}],
    )
    sites = connector.sites()
    assert sites == ["GSCN", "BSCN", "Australia"]
    # Basic auth must be sent on every request.
    assert requests_mock.last_request.headers["Authorization"].startswith("Basic ")


def test_radcalnet_discover_filters_kind_and_date_window(monkeypatch, requests_mock):
    connector = _radcalnet_connector(monkeypatch)
    requests_mock.get(
        f"{DEFAULT_BASE_URL}GSCN/data/",
        json=[
            {"name": "GSCN01_2025_330_v04.05.output"},
            {"name": "GSCN01_2025_334_v04.05.output"},
            {"name": "GSCN01_2025_334_v04.05.input"},
            {"name": "GSCN01_2025_340_v04.05.output"},
            {"name": "GSCN_archive.nc"},
        ],
    )

    targets = connector.discover(site="GSCN", start=(2025, 334), end=(2025, 334))
    assert [t.filename for t in targets] == ["GSCN01_2025_334_v04.05.output"]
    assert targets[0].kind == "output"
    assert targets[0].year == 2025
    assert targets[0].doy == 334
    assert targets[0].version == "04.05"

    all_outputs = connector.discover(site="GSCN")
    assert [t.filename for t in all_outputs] == [
        "GSCN01_2025_330_v04.05.output",
        "GSCN01_2025_334_v04.05.output",
        "GSCN01_2025_340_v04.05.output",
    ]

    unfiltered = connector.discover(site="GSCN", kind=None)
    assert "GSCN_archive.nc" in [t.filename for t in unfiltered]
    archive_target = next(t for t in unfiltered if t.filename == "GSCN_archive.nc")
    assert archive_target.kind == "archive"
    assert archive_target.year is None


def test_radcalnet_discover_nc_lists_datanc_directory(monkeypatch, requests_mock):
    connector = _radcalnet_connector(monkeypatch)
    requests_mock.get(
        f"{DEFAULT_BASE_URL}GSCN/datanc/",
        json=[{"name": "GSCN_archive.nc"}],
    )
    targets = connector.discover(site="GSCN", fmt="nc", kind=None)
    assert len(targets) == 1
    assert targets[0].fmt == "nc"
    assert targets[0].url == f"{DEFAULT_BASE_URL}GSCN/datanc/GSCN_archive.nc"


def test_radcalnet_fetch_basic_auth(monkeypatch, requests_mock):
    connector = _radcalnet_connector(monkeypatch)
    target = RadCalNetTarget(
        site="GSCN",
        filename="GSCN01_2025_334_v04.05.output",
        url=f"{DEFAULT_BASE_URL}GSCN/data/GSCN01_2025_334_v04.05.output",
        fmt="ascii",
    )
    requests_mock.get(target.url, content=b"Site:\tGSCN01\n")
    raw = connector.fetch(target)
    assert raw == b"Site:\tGSCN01\n"
    assert requests_mock.last_request.headers["Authorization"].startswith("Basic ")


def test_radcalnet_fetch_401_raises_credentials_error(monkeypatch, requests_mock):
    connector = _radcalnet_connector(monkeypatch)
    target = RadCalNetTarget(site="GSCN", filename="x.output", url=f"{DEFAULT_BASE_URL}GSCN/data/x.output", fmt="ascii")
    requests_mock.get(target.url, status_code=401)
    with pytest.raises(ValueError, match="RADCALNET_USERNAME"):
        connector.fetch(target)


def test_radcalnet_parse_fixture_row_counts_and_columns(monkeypatch):
    connector = _radcalnet_connector(monkeypatch)
    parsed = connector.parse(RADCALNET_FIXTURE)

    expected_columns = {
        "timestamp",
        "site",
        "lat",
        "lon",
        "alt_m",
        "wavelength_nm",
        "toa_reflectance",
        "value_is_climatological",
        "toa_reflectance_unc",
        "toa_reflectance_unc_status",
        "source_file",
        "source_version",
        "sza",
        "saa",
        "aod",
        "angstrom",
        "water_vapour_g_cm2",
        "ozone_du",
        "pressure_hpa",
        "temperature_k",
    }
    assert expected_columns <= set(parsed.columns)

    # Fixture: 8 wavelengths x 13 times, but column 1 (01:00 UTC) is 9998
    # (fill) for every wavelength, and wavelength 1010nm is all-fill across
    # every time -- both must be skipped entirely (no value, no row).
    assert (parsed["wavelength_nm"] == 1010.0).sum() == 0
    n_wavelengths_kept = 7  # 400, 410, 500, 600, 700, 800, 1000
    n_times_kept = 12  # 13 time columns minus the fill (01:00) column
    assert len(parsed) == n_wavelengths_kept * n_times_kept
    assert parsed["site"].eq("GSCN01").all()
    assert parsed["source_file"].eq("GSCN01_2025_334_v04.05.output").all()
    assert parsed["source_version"].eq("04.05").all()
    assert parsed["lat"].eq(36.3977).all()
    assert parsed["lon"].eq(94.3286).all()
    assert parsed["alt_m"].eq(2824.0).all()


def test_radcalnet_parse_fixture_timestamp_matches_ported_helper(monkeypatch):
    connector = _radcalnet_connector(monkeypatch)
    parsed = connector.parse(RADCALNET_FIXTURE)

    # Year 2025 DOY 334 01:30 UTC -> ported year/doy/UTC construction.
    expected = datetime(2025, 1, 1, tzinfo=timezone.utc) + pd.Timedelta(days=333, hours=1, minutes=30)
    row = parsed[(parsed["wavelength_nm"] == 400.0)].sort_values("timestamp").iloc[0]
    assert row["timestamp"] == pd.Timestamp(expected)


def test_radcalnet_parse_fixture_uncertainty_status_semantics(monkeypatch):
    connector = _radcalnet_connector(monkeypatch)
    parsed = connector.parse(RADCALNET_FIXTURE)

    wl400 = parsed[parsed["wavelength_nm"] == 400.0].sort_values("timestamp").reset_index(drop=True)
    # Column order (after dropping the fill 01:00 column): 01:30 (-0.0035,
    # negative -> prior), 02:00 (-0.0039, negative -> prior), 02:30 (0.0034,
    # positive -> provided), and so on.
    assert wl400.loc[0, "toa_reflectance_unc_status"] == "prior"
    assert wl400.loc[0, "toa_reflectance_unc"] == pytest.approx(0.0035)
    assert wl400.loc[1, "toa_reflectance_unc_status"] == "prior"
    assert wl400.loc[1, "toa_reflectance_unc"] == pytest.approx(0.0039)
    assert wl400.loc[2, "toa_reflectance_unc_status"] == "provided"
    assert wl400.loc[2, "toa_reflectance_unc"] == pytest.approx(0.0034)

    # No row carries an "unknown" uncertainty status in this fixture except
    # where the wavelength has no uncertainty entry at all -- every kept
    # wavelength here does have one, so all should be provided/prior.
    assert set(parsed["toa_reflectance_unc_status"]) <= {"provided", "prior", "unknown"}
    assert (parsed["toa_reflectance_unc_status"] == "unknown").sum() == 0


def test_radcalnet_parse_negative_reflectance_is_climatological():
    # No negative reflectance values survive trimming in the real fixture
    # (only the uncertainty block has negatives), so exercise the
    # climatological-reflectance path via a synthetic minimal .output string.
    text = (
        "Site:\tTEST01\n"
        "Lat:\t10.0\n"
        "Lon:\t20.0\n"
        "Alt:\t100\n"
        "\n"
        "Year:\t2025\t2025\n"
        "DOY(U):\t100\t100\n"
        "UTC:\t01:00\t01:30\n"
        "DOY(L):\t100\t100\n"
        "Local:\t09:00\t09:30\n"
        "P:\t900\t900\n"
        "T:\t280\t280\n"
        "WV:\t0.1\t0.1\n"
        "O3:\t250\t250\n"
        "AOD:\t0.05\t0.05\n"
        "Ang:\t1.0\t1.0\n"
        "Type:\tR\tR\n"
        "Zen:\t50.0\t50.0\n"
        "Azi:\t100.0\t100.0\n"
        "esd:\t0.98\t0.98\n"
        "\n"
        "\n"
        "500\t-0.21\t0.22\n"
        "\n"
        "P:\t4.0\t4.0\n"
        "T:\t0.5\t0.5\n"
        "WV:\t0.01\t0.01\n"
        "O3:\t7.0\t7.0\n"
        "AOD:\t0.001\t0.001\n"
        "Ang:\t0.01\t0.01\n"
        "500\t0.005\t0.006\n"
    )
    from spectraccess.connectors.radcalnet.connector import _parse_output_text

    frame = _parse_output_text(text, source_file="TEST01_2025_100_v01.00.output")
    assert len(frame) == 2
    negative_row = frame[frame["value_is_climatological"]].iloc[0]
    assert negative_row["toa_reflectance"] == pytest.approx(0.21)
    positive_row = frame[~frame["value_is_climatological"]].iloc[0]
    assert positive_row["toa_reflectance"] == pytest.approx(0.22)


def test_radcalnet_parse_zip_of_outputs_latest_version_per_day(monkeypatch):
    import zipfile
    from io import BytesIO

    connector = _radcalnet_connector(monkeypatch)
    fixture_text = RADCALNET_FIXTURE.read_text(encoding="utf-8")

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("GSCN/GSCN01_2025_334_v04.05.output", fixture_text)
        # Lower version for the same site+day must be superseded, not
        # concatenated alongside the higher one.
        zf.writestr("GSCN/GSCN01_2025_334_v04.00.output", fixture_text)

    parsed = connector.parse(buffer.getvalue())
    assert parsed["source_version"].eq("04.05").all()
    assert len(parsed) == 7 * 12  # same shape as the single-file parse
    # The zip (multi-file) path must NOT expose a per-file raw_metadata attr:
    # it is meaningless for a concatenated multi-file frame.
    assert "raw_metadata" not in parsed.attrs


def test_radcalnet_parse_canonical_validates_and_maps_fields(monkeypatch):
    connector = _radcalnet_connector(monkeypatch)
    canonical = connector.parse_canonical(
        RADCALNET_FIXTURE,
        source_url=f"{DEFAULT_BASE_URL}GSCN/data/GSCN01_2025_334_v04.05.output",
        retrieved_at=pd.Timestamp("2026-07-07T00:00:00Z"),
    )

    assert (canonical["quantity"] == "toa_reflectance").all()
    assert (canonical["units"] == "1").all()
    assert (canonical["source"] == "radcalnet").all()
    assert (canonical["source_agency"] == "RadCalNet (CEOS WGCV)").all()
    assert (canonical["site"] == "GSCN01").all()
    assert (canonical["latitude"] == 36.3977).all()
    assert (canonical["longitude"] == 94.3286).all()
    assert canonical.attrs["spectraccess_schema_version"] == "1.0"
    assert (canonical["unc_status"] == "provided").any()
    assert (canonical["unc_status"] == "prior").any()


def test_radcalnet_parse_canonical_matches_two_step_path(monkeypatch):
    connector = _radcalnet_connector(monkeypatch)
    native = connector.parse(RADCALNET_FIXTURE)
    two_step = radcalnet_to_canonical(native, source_url="https://example.test/gscn.output")
    end_to_end = connector.parse_canonical(RADCALNET_FIXTURE, source_url="https://example.test/gscn.output")
    pd.testing.assert_frame_equal(two_step.reset_index(drop=True), end_to_end.reset_index(drop=True))


def test_radcalnet_to_canonical_rejects_frame_without_reflectance():
    with pytest.raises(ValueError, match="toa_reflectance"):
        radcalnet_to_canonical(pd.DataFrame({"foo": [1.0]}))


# --- Connector.run() target-provenance contract (t_2af776c2) ---------------

from spectraccess.connectors.thredds import ThreddsDataset
from spectraccess.core.connector import Connector


class _RecordingConnector(Connector):
    """Minimal Connector that records the kwargs each parse path receives."""

    def __init__(self, target):
        self._target = target
        self.parse_kwargs = None
        self.canonical_kwargs = None

    def discover(self, **kwargs):
        return [self._target]

    def fetch(self, target, **kwargs):
        return b"raw"

    def parse(self, raw, **kwargs):
        self.parse_kwargs = kwargs
        return {"native": True}

    def parse_canonical(self, raw, **kwargs):
        self.canonical_kwargs = kwargs
        return {"canonical": True}

    def _parse_kwargs_for(self, target):
        return {"source_agency": target}

    def _canonical_kwargs_for(self, target):
        return {"source_agency": target, "source_url": f"u://{target}"}


class _BareConnector(Connector):
    """Overrides neither hook -- proves the default carries no target kwargs."""

    def discover(self, **kwargs):
        return ["target"]

    def fetch(self, target, **kwargs):
        return b"raw"

    def parse(self, raw, **kwargs):
        return kwargs


def test_run_carries_target_parse_provenance():
    connector = _RecordingConnector(target="EUMETSAT")
    connector.run()
    assert connector.parse_kwargs == {"source_agency": "EUMETSAT"}
    assert connector.canonical_kwargs is None


def test_run_canonical_uses_canonical_kwargs():
    connector = _RecordingConnector(target="CMA")
    connector.run(canonical=True)
    assert connector.canonical_kwargs == {"source_agency": "CMA", "source_url": "u://CMA"}
    assert connector.parse_kwargs is None


def test_run_parse_kwargs_overrides_target_provenance():
    connector = _RecordingConnector(target="EUMETSAT")
    connector.run(parse_kwargs={"source_agency": "OVERRIDE", "extra": 1})
    assert connector.parse_kwargs == {"source_agency": "OVERRIDE", "extra": 1}


def test_run_without_hook_overrides_passes_no_target_kwargs():
    # Backward compat: a connector that does not override the hooks still runs,
    # and its parse() sees no target-derived kwargs (as before this contract).
    assert _BareConnector().run() == {}


def test_gsics_run_hooks_extract_thredds_provenance():
    dataset = ThreddsDataset(
        name="msg4-seviri",
        catalog_url="http://host/cat.xml",
        access_url="http://host/f.nc",
        source_agency="EUMETSAT",
    )
    connector = GSICSConnector()
    assert connector._parse_kwargs_for(dataset) == {"source_agency": "EUMETSAT"}
    assert connector._canonical_kwargs_for(dataset) == {
        "source_agency": "EUMETSAT",
        "source_url": "http://host/f.nc",
    }
    # A bare-string target (fetch-by-URL path) carries no dataclass provenance.
    assert connector._parse_kwargs_for("http://host/f.nc") == {}
    assert connector._canonical_kwargs_for("http://host/f.nc") == {}


def test_radcalnet_run_canonical_hook_extracts_url(monkeypatch):
    connector = _radcalnet_connector(monkeypatch)
    target = RadCalNetTarget(
        site="GSCN",
        filename="GSCN01_2025_334_v04.05.output",
        url=f"{DEFAULT_BASE_URL}GSCN/data/GSCN01_2025_334_v04.05.output",
        fmt="ascii",
        kind="output",
    )
    assert connector._canonical_kwargs_for(target) == {"source_url": target.url}
    # Native parse() takes no provenance kwargs, so the native hook stays empty.
    assert connector._parse_kwargs_for(target) == {}


def test_radcalnet_parse_output_text_tolerates_missing_ancillary_rows():
    # A .output file that omits several of the 8 named ancillary rows (here:
    # only P/AOD/Type/Zen present, no Azi/Ang/WV/O3/T) must parse, not IndexError.
    from spectraccess.connectors.radcalnet import parse_output_text

    text = (
        "Site:\tRVUS00\nLat:\t38.497\nLon:\t-115.690\nAlt:\t1435\n\n"
        "Year:\t2026\t2026\nDOY(U):\t171\t171\nUTC:\t17:00\t17:30\n"
        "P:\t855\t855\nAOD:\t0.076\t0.072\nType:\tR\tR\nZen:\t37.9\t32.2\n\n"
        "400\t0.2528\t0.2534\n410\t0.2529\t0.2537\n"
    )
    frame = parse_output_text(text, source_file="RVUS00_2026_171_v04.05.output")
    assert not frame.empty
    assert set(frame["wavelength_nm"]) == {400.0, 410.0}
    # Present ancillary carried; absent ones are NaN, not a crash.
    assert (frame["aod"].isin([0.076, 0.072])).all()
    assert frame["water_vapour_g_cm2"].isna().all()


def test_radcalnet_parse_output_text_exposes_raw_metadata_passthrough():
    from spectraccess.connectors.radcalnet import parse_output_text

    frame = parse_output_text(RADCALNET_FIXTURE.read_text(encoding="utf-8"), source_file=RADCALNET_FIXTURE.name)
    raw = frame.attrs["raw_metadata"]

    # Every first-block metadata row is present verbatim, including rows the
    # cleaned tidy frame discards entirely (Local, DOY(L), Type, esd).
    rows = raw["rows"]
    for label in ("Year", "DOY(U)", "UTC", "DOY(L)", "Local", "Type", "esd", "AOD"):
        assert label in rows, f"missing raw metadata row {label!r}"
    # Verbatim, untransformed: Type is the raw string token, AOD is the raw value.
    assert rows["Type"][0] == "R"
    assert rows["AOD"][0] == "0.0533"
    assert raw["site_id"] == "GSCN01"
    assert raw["version"] == "04.05"
    assert len(raw["timestamps"]) == raw["n_times"]
    # Per-time row lengths line up with the time axis.
    assert len(rows["UTC"]) == raw["n_times"]


def test_radcalnet_run_canonical_end_to_end_carries_source_url(monkeypatch, requests_mock):
    connector = _radcalnet_connector(monkeypatch)
    filename = "GSCN01_2025_334_v04.05.output"
    file_url = f"{DEFAULT_BASE_URL}GSCN/data/{filename}"
    requests_mock.get(f"{DEFAULT_BASE_URL}GSCN/data/", json=[{"name": filename}])
    requests_mock.get(file_url, content=RADCALNET_FIXTURE.read_bytes())

    canonical = connector.run(site="GSCN", canonical=True)

    assert canonical.attrs["spectraccess_schema_version"] == "1.0"
    # The discovered target's URL survived the convenience path into provenance.
    assert (canonical["source_url"] == file_url).all()
    assert (canonical["site"] == "GSCN01").all()

