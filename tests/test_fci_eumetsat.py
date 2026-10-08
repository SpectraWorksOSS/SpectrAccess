"""Provider-layout fixtures exercise the installed Satpy and EUMDAC adapters."""
import io
import json
import logging
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np
import pytest
import requests

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


class DataStoreHTTP:
    """Only HTTPAdapter.send is stubbed; all EUMDAC APIs and parsing run intact.

    JSON follows the public browse/search GeoJSON and download metadata shape.
    OpenSearch description is parsed by Collection to validate search parameters.
    """
    token = "synthetic-bearer-never-log"

    def __init__(self, fixture, monkeypatch, *, ignore_range=False, token_status=200):
        self.fixture, self.ignore_range, self.token_status = fixture, ignore_range, token_status
        self.calls, self.streams = [], []
        root = Path(__file__).parent / "fixtures/fci_eumetsat"
        self.search = json.loads((root / "search.json").read_text())
        self.metadata = json.loads((root / "metadata.json").read_text())
        self.osdd = (root / "osdd.xml").read_bytes()
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", lambda _, request, **kw: self.send(request))

    def send(self, request):
        self.calls.append(request)
        path = unquote(urlparse(request.url).path)
        query = parse_qs(urlparse(request.url).query)
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, 200
        if path == "/token":
            response.status_code = self.token_status
            payload = {"access_token": self.token, "expires_in": 86400} if self.token_status == 200 else {"error": "invalid_client"}
        elif path.endswith("/osdd"):
            response._content = self.osdd
            return response
        elif path.endswith("/os"):
            payload = self.search
        elif path.endswith("/metadata"):
            payload = self.metadata
        elif "/browse/" in path and "/products/" in path:
            product_id = path.rsplit("/", 1)[-1]
            payload = next(f for f in self.search["features"] if f["id"] == product_id)
        elif path.endswith("/entry"):
            entry = query["name"][0]
            data = (self.fixture / entry).read_bytes()
            byte_range = request.headers.get("Range")
            if byte_range and not self.ignore_range:
                begin, end = map(int, byte_range.removeprefix("bytes=").split("-"))
                data = data[begin:end + 1]
                response.status_code = 206
                response.headers["Content-Range"] = f"bytes {begin}-{end}/{(self.fixture / entry).stat().st_size}"
            response.headers["Content-Disposition"] = f'attachment; filename="{entry}"'
            class Stream(io.BytesIO):
                bytes_read = 0
                def read(self, size=-1):
                    value = super().read(size)
                    self.bytes_read += len(value)
                    return value
            response.raw = Stream(data)
            self.streams.append(response.raw)
            return response
        else:
            raise AssertionError(f"Unexpected HTTP route: {path}")
        response._content = json.dumps(payload).encode()
        response.headers["Content-Type"] = "application/json"
        return response

    @property
    def entries_requested(self):
        return [(parse_qs(urlparse(r.url).query)["name"][0], r.headers.get("Range"))
                for r in self.calls if urlparse(r.url).path.endswith("/entry")]


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
    http = DataStoreHTTP(fixture, monkeypatch)
    connector = FCIConnector(credentials=CREDENTIAL)
    store = connector._store()
    assert isinstance(store.get_collection(mod.COLLECTIONS["l1c"]), mod.eumdac.collection.Collection)
    product = store.get_product(mod.COLLECTIONS["l1c"], "cycle-1")
    assert isinstance(product, mod.eumdac.product.Product)
    assert product.entries == tuple(http.search["features"][0]["properties"]["links"]["sip-entries"][i]["title"] for i in range(3))
    targets = connector.discover(bbox=BBOX, start=START, end=START + timedelta(minutes=20), products=("l1c",))
    r = connector.fetch(targets[0], dest=tmp_path, bbox=(-.1, -.05, .1, -.005), channels=("ir_105",))
    whole = [name for name, byte_range in http.entries_requested if byte_range is None]
    assert GENERATOR["filename"]("BODY", 1) in whole
    assert GENERATOR["filename"]("BODY", 3) not in whole
    assert GENERATOR["filename"]("TRAIL", 41) in whole
    assert set(r.entries) == set(whole)
    assert any(byte_range for _, byte_range in http.entries_requested)
    assert (GENERATOR["filename"]("BODY", 1), "bytes=0-63") in http.entries_requested
    ds = connector.read(r, channels=("ir_105",))
    assert ds.ir_105.shape == (2, 4) and ds.ir_105.values[0, 0] == -5
    validate(connector.parse_canonical(r, channels=("ir_105",)))


def test_discovery_preserves_ten_minute_cycles(fixture, monkeypatch):
    http = DataStoreHTTP(fixture, monkeypatch)
    targets = FCIConnector(credentials=CREDENTIAL).discover(bbox=BBOX, start=START, end=START + timedelta(minutes=20), products=("l1c",))
    assert [t.product_id for t in targets] == ["cycle-1", "cycle-2"]
    assert targets[1].start - targets[0].start == timedelta(minutes=10)
    query = next(parse_qs(urlparse(r.url).query) for r in http.calls if urlparse(r.url).path.endswith("/os"))
    assert query["bbox"] == ["-4.0,-4.0,4.0,4.0"]
    assert query["dtstart"] == [START.isoformat()]
    assert targets[0].start == START and targets[0].end == START + timedelta(minutes=9, seconds=30)
    assert targets[0].product_version == "synthetic-2"
    for t in targets:
        validate(target_to_canonical(t))


def test_missing_credentials_no_environment_fallback(monkeypatch):
    from spectraccess.core import credentials
    monkeypatch.setattr(credentials, "_entry", lambda _: None)
    monkeypatch.setenv("EUMETSAT_KEY", "not-a-credential-source")
    monkeypatch.setenv("EUMETSAT_SECRET", "not-a-credential-source")
    with pytest.raises(CredentialMissing):
        FCIConnector().discover(bbox=BBOX, start=START, end=START, products=("l1c",))


def test_auth_error_redacts_keys(fixture, monkeypatch):
    http = DataStoreHTTP(fixture, monkeypatch, token_status=401)
    # The same real raise_for_status used by AccessToken raises HTTPError.
    with pytest.raises(requests.HTTPError) as upstream:
        http.send(requests.Request("POST", "https://api.eumetsat.int/token").prepare()).raise_for_status()
    assert upstream.value.response.status_code == 401
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


def test_byte_range_refusal_does_not_download_cycle(fixture, tmp_path, monkeypatch):
    http = DataStoreHTTP(fixture, monkeypatch, ignore_range=True)
    product = FCIConnector(credentials=CREDENTIAL)._store().get_product(mod.COLLECTIONS["l1c"], "cycle-1")
    with pytest.raises(RuntimeError, match="byte-range response"):
        mod._EntryRange(product, GENERATOR["filename"]("BODY", 1))
    assert http.streams[0].bytes_read == 65 and http.streams[0].closed
    with pytest.raises(RuntimeError, match="entry fetch failed"):
        FCIConnector(credentials=CREDENTIAL).fetch(target(), dest=tmp_path, channels=("ir_105",))
    assert all(byte_range == "bytes=0-63" for _, byte_range in http.entries_requested)
    assert not list(tmp_path.rglob("*.nc")) and not list(tmp_path.rglob("*.part"))


def test_compressed_provider_filter_through_read(fixture):
    path = fixture / GENERATOR["filename"]("BODY", 1)
    with mod.h5py.File(path, "r") as nc:
        properties = nc["data/ir_105/measured/effective_radiance"].id.get_create_plist()
        assert properties.get_filter(0)[0] == 32018
    ds = FCIConnector().read(result(fixture), channels=("ir_105",))
    assert ds.ir_105.values[0, 0] == -5 and np.isnan(ds.ir_105.values[3, 0])


def test_debug_logging_redacts_real_eumdac_calls(fixture, tmp_path, monkeypatch, caplog):
    http = DataStoreHTTP(fixture, monkeypatch)
    logger = logging.getLogger("eumdac")
    before = len(logger.filters)
    level = logger.level
    mod._install_log_redaction()
    mod._install_log_redaction()
    assert len(logger.filters) == before and logger.level == level
    with caplog.at_level(logging.DEBUG, logger="eumdac"):
        connector = FCIConnector(credentials=CREDENTIAL)
        targets = connector.discover(bbox=BBOX, start=START, end=START + timedelta(minutes=20), products=("l1c",))
        connector.fetch(targets[0], dest=tmp_path, channels=("ir_105",))
    records = [r for r in caplog.records if r.name == "eumdac"]
    assert any("EUMDAC token status: <redacted>" in r.getMessage() for r in records)
    assert any("Bearer <redacted>" in r.getMessage() for r in records)
    for record in records:
        assert http.token not in record.getMessage() and http.token not in str(record.__dict__)
    assert http.token not in caplog.text


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
