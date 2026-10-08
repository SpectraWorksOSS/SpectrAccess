"""Provider-layout fixtures exercise the installed Satpy and EUMDAC adapters."""
import io
import runpy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("satpy")
pytest.importorskip("eumdac")
from spectraccess.connectors.fci_eumetsat import FCIConnector, FCIResult, FCITarget, target_to_canonical
from spectraccess.connectors.fci_eumetsat import connector as mod
from spectraccess.core.credentials import Credential, CredentialMissing, CredentialRejected
from spectraccess.core.schema import validate

GENERATOR = runpy.run_path(str(Path(__file__).parent / "fixtures/fci_eumetsat/generate.py"))
START = datetime(2024, 5, 1, 10, tzinfo=timezone.utc)
BBOX = (-4., -4., 4., 4.)
CHANNELS = GENERATOR["CHANNELS"]
CREDENTIAL = Credential("password", "secret-never-print", "key-never-print")


@pytest.fixture(scope="module")
def fixture(tmp_path_factory):
    return GENERATOR["generate"](tmp_path_factory.mktemp("fci"))


def target(product_id="cycle-1", collection=mod.COLLECTIONS["l1c"], start=START):
    raw = {"geometry": {"type": "Polygon", "coordinates": [[[-79, -79], [79, -79], [79, 79], [-79, 79], [-79, -79]]]},
           "properties": {"productInformation": {"processingVersion": "1", "version": "2"}}}
    return FCITarget(product_id, collection, product_id, "https://example.org/fci", START, raw,
                     BBOX, start, start + timedelta(minutes=10))


def result(fixture, names=None, collection=mod.COLLECTIONS["l1c"]):
    if names is None:
        names = tuple(p.name for p in fixture.glob("*.nc") if "-CHK-" in p.name)
    return FCIResult(fixture, target(collection=collection), START, BBOX, tuple(names))


class LocalProduct:
    def __init__(self, fixture, product_id="cycle-1", start=START):
        self.fixture, self.product_id, self.sensing_start = fixture, product_id, start
        self.sensing_end = start + timedelta(minutes=10)
        self.metadata = target().raw
        self.entries = tuple(p.name for p in fixture.glob("*.nc") if "-CHK-" in p.name)
        self.calls = []

    def __str__(self):
        return self.product_id

    @contextmanager
    def open(self, *, entry, chunk=None):
        self.calls.append((entry, chunk))
        data = (self.fixture / entry).read_bytes()
        yield io.BytesIO(data[slice(*chunk)] if chunk is not None else data)


def test_spike_real_satpy(fixture):
    path = fixture / GENERATOR["filename"]("BODY", 1)
    handler = mod._l1_handler(path)
    info = {"units": "mW.m-2.sr-1.(cm-1)-1"}
    radiance = handler.get_dataset(mod._key("ir_105", 2000, "radiance"), info).compute()
    assert radiance.values[0, 0] == -5
    assert radiance.values[0, 1] == 0
    assert np.isnan(radiance.values[-1, 0])
    counts = handler.get_dataset(mod._key("ir_105", 2000, "counts"), {"units": "count"}).compute()
    assert counts.values[0, 0] == 1
    assert counts.values[-1, 0] == 65535
    bt = handler.get_dataset(mod._key("ir_105", 2000, "brightness_temperature"), {"units": "K"}).compute()
    assert np.isnan(bt.values[0, 0])
    refl = handler.get_dataset(mod._key("nir_13", 1000, "reflectance"), {"units": "%"}).compute()
    assert refl.values[0, 2] == pytest.approx(5 / 50 * np.pi * 100)
    quality = handler.get_dataset(mod._key("ir_105_pixel_quality", 2000), {}).compute()
    assert quality.values[0, 0] == 255
    pixel_time = handler.get_dataset(mod._key("ir_105_time", 2000), {}).compute()
    assert pixel_time.values[0, 1] == 101
    assert np.isnan(pixel_time.values[0, 0])
    assert handler.get_area_def(mod._key("ir_105", 2000)).shape == (4, 4)
    mod._close_l1(handler)
    clipped = mod._l1_handler(path, clip_negative_radiances=True)
    assert clipped.get_dataset(mod._key("ir_105", 2000, "radiance"), info).compute().values[0, 0] > 0
    mod._close_l1(clipped)


def test_native_values_time_flags_noise_and_dimensions(fixture):
    with mod.satpy.config.set({"readers.clip_negative_radiances": True}):
        ds = FCIConnector().read(result(fixture), channels=CHANNELS)
    assert ds.ir_105.shape == (8, 4)
    assert ds.nir_13.shape == (16, 8)
    assert ds.ir_105.values[0, 0] == -5
    assert np.isnan(ds.ir_105.values[3, 0])
    assert ds.ir_38.values[3, 3] == 5000 * 2 - 300
    assert ds.ir_105_pixel_quality.dtype == np.uint8
    assert ds.ir_105_pixel_quality.values[0, 0] == 255
    assert ds.ir_105_time.values[0, 1] == 101
    assert np.isnan(ds.ir_105_time.values[0, 0])
    assert ds.ir_105_time.attrs["units"] == "seconds since 2000-01-01 00:00:00"
    assert ds.ir_105_time.attrs["calendar"] == "proleptic_gregorian"
    provenance = ds.ir_105.attrs["reader_provenance"]
    assert provenance["clip_negative_radiances"] is False
    assert provenance["global_clip_negative_radiances"] is True
    assert "platform_name" not in ds.ir_105.attrs
    noise = ds.data_nir_13_measured_radiometric_noise_lut_noise
    other = ds.data_ir_105_measured_radiometric_noise_lut_noise
    assert noise.size == 3 and other.size == 5
    assert noise.dims != other.dims
    assert noise.attrs["scale_factor"] == .1
    assert np.array_equal(noise, [0, 1, 2])
    # Missing chunk 2 leaves a true gap in provider y coordinates, without padding.
    assert ds.y_ir_105.values[4] - ds.y_ir_105.values[3] == pytest.approx(5 * GENERATOR["GRID_STEP_2KM"])
    assert ds.attrs["segment_padding"] is False
    assert ds.attrs["product_id"] == "cycle-1"


@pytest.mark.parametrize("bbox", [(-.015, -.015, .015, .015), (-.1, -.015, .015, .015), (150., -10., 160., 10.)])
def test_bbox_native_window(fixture, bbox):
    ds = FCIConnector().read(result(fixture), bbox=bbox, channels=("ir_105", "nir_13"))
    for channel, n in [("ir_105", 4), ("nir_13", 8)]:
        expected = []
        for count in (1, 3):
            with mod.h5netcdf.File(fixture / GENERATOR["filename"]("BODY", count), "r") as nc:
                group = nc.groups["data"].groups[channel].groups["measured"]
                x, y = mod._angles(group.variables["x"]), mod._angles(group.variables["y"])
                inverse = mod.Transformer.from_crs(mod._projection(GENERATOR["PROJECTION"]), "EPSG:4326", always_xy=True)
                xx, yy = np.meshgrid(-x * 35786400., y * 35786400.)
                lon, lat = inverse.transform(xx, yy)
                inside = (lon >= bbox[0]) & (lon <= bbox[2]) & (lat >= bbox[1]) & (lat <= bbox[3])
                rr, cc = np.where(inside)
                if rr.size:
                    expected.append(((rr.max() - rr.min() + 1), (cc.max() - cc.min() + 1)))
        if expected:
            assert ds[channel].shape == (sum(r for r, c in expected), max(c for r, c in expected))
        else:
            assert channel not in ds or ds[channel].size == 0
    frame = FCIConnector().parse_canonical(result(fixture), bbox=bbox, channels=("ir_105",))
    assert frame.empty == (bbox[0] == 150.)


def test_canonical_provider_observations(fixture):
    frame = FCIConnector().parse_canonical(result(fixture), channels=CHANNELS)
    validate(frame)
    assert len(frame) == 1 and frame.attrs["spectraccess_schema_version"] == "1.0"
    row = frame.iloc[0]
    assert row.valid_time == START and row.integration_end == START + timedelta(minutes=10)
    assert row.support_kind == "swath" and row.unc_status == "unknown"
    assert row.algorithm_version == "synthetic-1" and row.collection_version == "synthetic-2"
    assert "radiometric noise model" in row.unc_definition
    assert "data/ir_105/quality_channel/radiometric_noise_compliance" in row.qa
    assert row.footprint_geometry == target().footprint


def test_entry_ranges_and_filtered_fetch(fixture, tmp_path, monkeypatch):
    product = LocalProduct(fixture)
    monkeypatch.setattr(FCIConnector, "_store", lambda *args: SimpleNamespace(get_product=lambda *args: product))
    r = FCIConnector().fetch(target(), dest=tmp_path, bbox=(-.1, -.05, .1, -.005), channels=("ir_105",))
    whole = [name for name, byte_range in product.calls if byte_range is None]
    assert GENERATOR["filename"]("BODY", 1) in whole
    assert GENERATOR["filename"]("BODY", 3) not in whole
    assert GENERATOR["filename"]("TRAIL", 41) in whole
    assert set(r.entries) == set(whole)
    assert any(byte_range for _, byte_range in product.calls)
    ds = FCIConnector().read(r, channels=("ir_105",))
    assert ds.ir_105.size


def test_discovery_preserves_ten_minute_cycles(fixture, monkeypatch):
    products = [LocalProduct(fixture, "cycle-1"), LocalProduct(fixture, "cycle-2", START + timedelta(minutes=10))]
    queries = []
    def search(**kw):
        queries.append(kw)
        return iter(products)
    monkeypatch.setattr(FCIConnector, "_store", lambda *args: SimpleNamespace(get_collection=lambda _: SimpleNamespace(search=search)))
    targets = FCIConnector().discover(bbox=BBOX, start=START, end=START + timedelta(minutes=20), products=("l1c",))
    assert [t.product_id for t in targets] == ["cycle-1", "cycle-2"]
    assert targets[1].start - targets[0].start == timedelta(minutes=10)
    assert queries[0]["bbox"] == "-4.0,-4.0,4.0,4.0"
    assert queries[0]["dtstart"] == START
    for t in targets:
        validate(target_to_canonical(t))


def test_missing_credentials_no_environment_fallback(monkeypatch):
    from spectraccess.core import credentials
    monkeypatch.setattr(credentials, "_entry", lambda _: None)
    monkeypatch.setenv("EUMETSAT_KEY", "not-a-credential-source")
    monkeypatch.setenv("EUMETSAT_SECRET", "not-a-credential-source")
    with pytest.raises(CredentialMissing):
        FCIConnector().discover(bbox=BBOX, start=START, end=START, products=("l1c",))


def test_auth_error_redacts_keys(monkeypatch):
    def reject(*args):
        from eumdac.errors import EumdacError
        raise EumdacError(f"{CREDENTIAL.account}:{CREDENTIAL.secret}", {"status": 401})
    monkeypatch.setattr(mod.eumdac, "AccessToken", reject)
    with pytest.raises(CredentialRejected) as exc:
        FCIConnector(credentials=CREDENTIAL)._store()
    assert CREDENTIAL.account not in str(exc.value) and CREDENTIAL.secret not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.parametrize("kind, collection, variable", [("CLM", "cloud_mask", "cloud_state"),
    ("CT", "cloud_type", "cloud_type"), ("CTTH", "ctth", "cloud_top_height")])
def test_l2_native_values_flags_time_unavailable(fixture, kind, collection, variable):
    r = result(fixture, (f"synthetic_{kind}.nc",), mod.COLLECTIONS[collection])
    ds = FCIConnector().read(r)
    assert ds[variable].shape == (4, 4)
    assert ds.quality_flag.values[0, 0] == -127
    assert ds.quality_flag.dtype == np.int8
    assert ds.attrs["pixel_time_available"] is False
    if kind == "CTTH":
        assert np.isnan(ds[variable].values[0, 0])
        assert ds[variable].values[0, 1] == 110
        assert ds.cloud_top_temperature.values[0, 1] == 220
    elif kind == "CLM":
        assert ds[variable].values[0, 0] == 255
        assert "cloud_filled" in ds[variable].attrs["flag_meanings"]
        assert 255 in ds[variable].attrs["flag_values"]
    else:
        assert ds[variable].values[0, 0] == -32767
    frame = FCIConnector().parse_canonical(r)
    assert frame.iloc[0].qa["product_quality"]["value"] == 2
    assert "unc_definition" not in frame


def test_byte_range_refusal_does_not_download_cycle(fixture):
    class IgnoringRange(LocalProduct):
        @contextmanager
        def open(self, *, entry, chunk=None):
            yield io.BytesIO((self.fixture / entry).read_bytes())
    with pytest.raises(RuntimeError, match="byte-range response"):
        mod._EntryRange(IgnoringRange(fixture), GENERATOR["filename"]("BODY", 1))


@pytest.mark.parametrize("bbox, present", [((-30., -10., 30., 10.), True),
    ((70., -10., 90., 10.), True), ((150., -10., 160., 10.), False)])
def test_inside_partial_outside_geostationary_disc(bbox, present):
    angles = np.linspace(-.156, .156, 160)
    rows, cols = mod._window(angles, angles, GENERATOR["PROJECTION"], bbox)
    assert bool(rows.stop and cols.stop) == present


def test_l2_outside_returns_empty_canonical_and_native_coordinates(fixture):
    r = result(fixture, ("synthetic_CTTH.nc",), mod.COLLECTIONS["ctth"])
    connector = FCIConnector()
    ds = connector.read(r)
    yd, xd = ds.cloud_top_height.dims
    np.testing.assert_array_equal(ds[yd], GENERATOR["GRID_STEP_2KM"] * np.array([-2., -1., 0., 1.]))
    np.testing.assert_array_equal(ds[xd], GENERATOR["GRID_STEP_2KM"] * np.array([2., 1., 0., -1.]))
    assert connector.parse_canonical(r, bbox=(150., -10., 160., 10.)).empty


def test_smoke_skips_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv("EUMETSAT_KEY", raising=False)
    monkeypatch.delenv("EUMETSAT_SECRET", raising=False)
    script = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/live_smoke.py"))
    script["smoke_fci_eumetsat"]()
    assert "FCI EUMETSAT smoke SKIP:" in capsys.readouterr().out


def test_empty_catalogue_geometry_remains_unavailable():
    t = target()
    raw = {"geometry": {}, "properties": {"acquisitionInformation": [{
        "platform": {"platformShortName": "MTI1"}, "acquisitionParameters": {"mtgCoverage": {
            "majorRegionCoverage": "FD", "repeatCycleIdentifier": "61"}}}],
        "productInformation": {"productType": "MTIFCI1CRRADFDHSI"}},
        "download_properties": {"productInformation": {"productVersion": "5.4.0",
            "processingInformation": {"processorVersion": "5.4.0"},
            "qualityInformation": {"qualityStatus": "NOMINAL"}}}}
    from dataclasses import replace
    t = replace(t, raw=raw)
    assert t.footprint is None and t.coverage[0]["majorRegionCoverage"] == "FD"
    row = target_to_canonical(t).iloc[0]
    assert row.footprint_geometry is None
    assert row.platform == "MTI1" and row.algorithm_version == "5.4.0"
    assert row.product_version == "5.4.0" and row.qa == {"qualityStatus": "NOMINAL"}


def test_same_named_non_grid_dimensions_across_source_files(fixture):
    source = "data/ir_105/measured"
    name = "radiometric_noise_lut_noise"
    arrays = {}
    for key, path in [("cycle_noise", fixture / GENERATOR["filename"]("TRAIL", 41)),
                      ("other_noise", fixture / "synthetic_other_noise.nc")]:
        with mod.h5netcdf.File(path, "r") as nc:
            var = nc.groups["data"].groups["ir_105"].groups["measured"].variables[name]
            arrays[key] = mod._source_array(var, name, source, path)
    ds = mod.xr.Dataset(arrays)
    assert ds.cycle_noise.size == 5 and ds.other_noise.size == 7
    assert ds.cycle_noise.dims != ds.other_noise.dims


@pytest.mark.parametrize("kwargs", [{"bbox": (1, 0, -1, 2)}, {"products": ("unknown",)}, {"limit": -1}])
def test_invalid_discovery_inputs(kwargs):
    params = dict(bbox=BBOX, start=START, end=START, products=("l1c",))
    params.update(kwargs)
    with pytest.raises(ValueError):
        FCIConnector().discover(**params)
