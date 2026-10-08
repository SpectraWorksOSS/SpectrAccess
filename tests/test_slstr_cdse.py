import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

pytest.importorskip("satpy")
from spectraccess.connectors.slstr_cdse import SLSTRConnector, SLSTRTarget
from spectraccess.connectors.slstr_cdse import connector as module
from spectraccess.core.credentials import Credential, CredentialMissing
from spectraccess.core.schema import validate

ROOT = Path(__file__).parent / "fixtures" / "slstr_cdse"
SPEC = importlib.util.spec_from_file_location("slstr_fixture", ROOT / "generate.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


@pytest.fixture
def product(tmp_path):
    return generator.generate(tmp_path)


@pytest.fixture
def target(product):
    raw = json.loads((product.parent / "feature.json").read_text())
    return SLSTRTarget(raw["Id"], raw["Name"], "https://example.test/product", datetime.now(timezone.utc), raw)


def test_satpy_spike(product):
    """Execute default adjustment, CF decoding, reflectance and unity override."""
    from satpy.readers.slstr_l1b import CHANCALIB_FACTORS, NCSLSTR1B
    for channel in (f"S{i}" for i in range(1, 10)):
        for view in "no":
            stripe = "a" if int(channel[1:]) < 7 else "i"
            kind = "radiance" if stripe == "a" else "BT"
            filename = product / f"{channel}_{kind}_{stripe}{view}.nc"
            info = {"dataset_name": channel, "stripe": stripe, "view": view, "mission_id": "S3A"}
            for override in (None, module.UNITY):
                handler = NCSLSTR1B(str(filename), info, {}, user_calibration=override)
                try:
                    key = module._ReaderKey(channel, stripe, view, "radiance" if kind == "radiance" else "brightness_temperature")
                    value = handler.get_dataset(key, {}).compute()
                    full_view = "nadir" if view == "n" else "oblique"
                    factor = 1 if override else CHANCALIB_FACTORS[channel + "_" + full_view]
                    np.testing.assert_allclose(value, 21 * factor)
                    assert value.attrs["platform_name"] == "Sentinel-3A"
                    if kind == "radiance":
                        reflectance = handler.get_dataset(module._ReaderKey(channel, stripe, view, "reflectance"), {}).compute()
                        np.testing.assert_allclose(reflectance, 21 * factor * np.pi)
                        assert reflectance.attrs["units"] == "%"
                finally:
                    handler.nc.close(); handler.cal.close(); handler.indices.close()


@pytest.mark.parametrize("bbox,shape", [((3.5, 50.5, 5.5, 51.5), (2, 4)), ((2, 49, 4, 51), (2, 2)), ((10, 10, 11, 11), (0, 0))])
def test_bbox_native_window(product, bbox, shape):
    ds = SLSTRConnector().read(product, bbox=bbox, channels=("S4", "S7", "S8", "S9"), views=("nadir",))
    assert ds.S4_radiance_an.shape == shape
    assert ds.attrs["resampled"] is False
    if ds.time_stamp_an.size > 1:
        assert np.all(np.diff(ds.time_stamp_an.values) > np.timedelta64(0, "us"))
    assert ds.time_stamp_an.size == shape[0]
    with xr.open_dataset(product / "flags_an.nc", mask_and_scale=False) as flags:
        if shape == (2, 4):
            np.testing.assert_array_equal(ds.cloud_an, flags.cloud_an[1:3, 1:5])
            np.testing.assert_array_equal(ds.confidence_an, flags.confidence_an[1:3, 1:5])
    assert ds.cloud_an.dtype == np.dtype("uint16")
    assert ds.solar_zenith_tn.shape == (2, 2)
    assert ds.S8_radiometric_uncertainty.attrs["long_name"] == "radiometric uncertainty lookup table"


def test_all_channels_both_views(product):
    ds = SLSTRConnector().read(product)
    assert ds.S1_radiance_an.shape != ds.S7_BT_in.shape
    for channel in module.CHANNELS:
        for view in "no":
            kind, stripe = ("radiance", "a") if channel in module.CHANNELS[:6] else ("BT", "i")
            value = ds[f"{channel}_{kind}_{stripe}{view}"]
            np.testing.assert_allclose(value, 21)
            assert "platform_name" not in value.attrs
            assert value.attrs["radiance_adjustment_factor"] == 1


def test_missing_credentials_no_env_fallback(product, target, monkeypatch):
    monkeypatch.setenv("CDSE_USERNAME", "irrelevant")
    monkeypatch.setenv("CDSE_PASSWORD", "irrelevant")
    monkeypatch.setattr("spectraccess.core.credentials.keyring.get_password", lambda *args: None)
    with pytest.raises(CredentialMissing):
        SLSTRConnector().fetch(target, dest=product.parent / "fetch")


def test_granule_contract(product, target):
    frame = SLSTRConnector().parse_canonical(product, target=target)
    validate(frame)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row.support_kind == "swath"
    assert row.footprint_geometry == target.footprint
    assert row.integration_start == target.time_window["Start"]
    assert row.collection_version == "005"
    assert row.algorithm_version == "synthetic-1"
    assert row.qa["qualityInformation"][0]["attributes"] == {"status": "PASSED"}
    assert row.unc_status == "unknown"
    assert "S8_radiometric_uncertainty" in row.unc_definition
    assert SLSTRConnector().parse_canonical(product, target=target, bbox=(10, 10, 11, 11)).empty


def test_discover_exact_time(product, target, monkeypatch):
    captured = {}
    def query(collection, terms, options):
        captured.update(terms)
        return iter([target.raw])
    monkeypatch.setattr(module, "query_features", query)
    connector = SLSTRConnector()
    assert connector.discover(bbox=(3, 50, 6, 52), start=datetime(2024, 5, 1, 10), end=datetime(2024, 5, 1, 11))[0].footprint == target.footprint
    assert captured["productType"] == "SL_1_RBT___"
    assert connector.discover(bbox=(3, 50, 6, 52), start=datetime(2024, 5, 1, 11), end=datetime(2024, 5, 1, 12)) == []


def test_filtered_fetch(product, target, monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(module, "_ExplicitCredentials", lambda account, secret: SimpleNamespace(username=account))
    patterns = []
    def manifest(url, dest, options):
        dest.write_bytes((product / "xfdumanifest.xml").read_bytes())
        return True
    def download(raw, dest, options):
        name = options["filter_pattern"]
        patterns.append(name)
        out = Path(dest) / target.title
        out.mkdir()
        (out / name).write_bytes((product / name).read_bytes())
        return target.title
    monkeypatch.setattr(module, "download_file", manifest)
    monkeypatch.setattr(module, "download_feature", download)
    result = SLSTRConnector(credentials=Credential("password", "dummy", "account")).fetch(
        target, dest=tmp_path / "downloads", channels=("S4", "S8"), views=("nadir",))
    assert "S4_radiance_an.nc" in patterns
    assert "S8_quality_in.nc" in patterns
    assert "S1_radiance_an.nc" not in patterns
    assert not any("*" in p for p in patterns)
    assert SLSTRConnector().read(result, channels=("S4", "S8"), views=("nadir",)).time_stamp_in.size == 3


def test_missing_time_fails(product):
    (product / "time_an.nc").unlink()
    with pytest.raises(ValueError, match="time_stamp_an"):
        SLSTRConnector().read(product, channels=("S4",), views=("nadir",))


def test_provider_fill_decoding(product):
    asset = product / "S4_radiance_an.nc"
    with xr.open_dataset(asset, decode_cf=False) as source:
        packed = source.load()
    packed.S4_radiance_an.values[0, 0] = -32768
    packed.to_netcdf(asset, engine="h5netcdf", mode="w")
    ds = SLSTRConnector().read(product, channels=("S4",), views=("nadir",))
    assert np.isnan(ds.S4_radiance_an.values[0, 0])
    assert ds.S4_radiance_an.values[0, 1] == 21
    assert ds.S4_radiance_an.attrs["provider_packing"]["scale_factor"] == 0.5


def test_rejected_credentials_and_rotation(product, target, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(module, "_ExplicitCredentials", lambda account, secret: SimpleNamespace(username=account))
    from spectraccess.core.credentials import CredentialRejected
    used = []
    def reject(url, dest, options):
        used.append(options["credentials"].username)
        error = RuntimeError("provider failure includes top-secret")
        error.status_code = 401
        raise error
    monkeypatch.setattr(module, "download_file", reject)
    connector = SLSTRConnector(credentials=lambda: Credential("password", "top-secret", str(len(used))))
    for _ in range(2):
        with pytest.raises(CredentialRejected) as caught:
            connector.fetch(target, dest=product.parent / "failed", channels=("S4",), views=("nadir",))
        assert "top-secret" not in str(caught.value)
        assert caught.value.__context__ is None
    assert used == ["0", "1"]


def test_smoke_skips_without_credentials(monkeypatch, capsys):
    import runpy
    monkeypatch.delenv("CDSE_USERNAME", raising=False)
    monkeypatch.delenv("CDSE_PASSWORD", raising=False)
    runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "live_smoke.py"))["smoke_slstr_cdse"]()
    assert "SLSTR CDSE smoke SKIP" in capsys.readouterr().out
