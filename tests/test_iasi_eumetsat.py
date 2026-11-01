"""HTTP fixtures through real EUMDAC and synthetic EPS through real MetopPy."""
import io
import json
import runpy
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np
import pytest
import requests

pytest.importorskip("eumdac")
from spectraccess.connectors.iasi_eumetsat import IASIConnector
from spectraccess.connectors.iasi_eumetsat import connector as mod
from spectraccess.core.credentials import Credential, CredentialMissing, CredentialRejected

ROOT = Path(__file__).parent / "fixtures/iasi_eumetsat"
START = datetime(2024, 8, 22, tzinfo=timezone.utc)
AUTH = Credential("password", "synthetic-secret", "synthetic-key")


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    return runpy.run_path(str(ROOT / "generate.py"))["generate"](tmp_path_factory.mktemp("iasi"))


class DataStoreHTTP:
    """Mock only HTTP: no patched connector/client/reader methods."""
    def __init__(self, monkeypatch, native, *, status=200, stage="download"):
        self.calls, self.status, self.stage = [], status, stage
        self.native = native
        self.feature = {"type": "Feature", "id": "synthetic-iasi", "geometry": {}, "properties": {
            "identifier": "synthetic-iasi", "parentIdentifier": mod.COLLECTION,
            "extraInformation": {}, "productInformation": {"size": 123, "productType": "IASxxx1C"},
            "date": "2024-08-22T00:00:00Z/2024-08-22T00:00:16Z",
            "acquisitionInformation": [{"platform": {"platformShortName": "Metop-B"}}],
            "links": {"sip-entries": [], "alternates": [], "data": [{"href": "https://api.eumetsat.int/data/download/1.0.0/synthetic", "mediaType": "application/zip"}]}}}
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", lambda _, request, **kw: self.send(request))

    def send(self, request):
        self.calls.append(request)
        path = unquote(urlparse(request.url).path)
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, 200
        if (self.stage == "token" and path == "/token") or (self.stage == "download" and "/data/download/" in path and not path.endswith("metadata")):
            response.status_code = self.status
        if response.status_code != 200:
            payload = {"error": f"denied {AUTH.account} {AUTH.secret} Bearer synthetic-token"}
        elif path == "/token":
            payload = {"access_token": "synthetic-token", "expires_in": 86400}
        elif path.endswith("/osdd"):
            response._content = (b'<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/" '
                b'xmlns:parameters="http://a9.com/-/spec/opensearch/extensions/parameters/1.0/">'
                b'<ShortName>Synthetic IASI</ShortName><Url type="application/json" '
                b'template="https://api.eumetsat.int/data/search-products/1.0.0/os?format=json">'
                b'<parameters:Parameter name="dtstart" value="{time:start?}" />'
                b'<parameters:Parameter name="dtend" value="{time:end?}" />'
                b'<parameters:Parameter name="bbox" value="{geo:box?}" />'
                b'<parameters:Parameter name="set" value="{eum:set?}" />'
                b'</Url></OpenSearchDescription>')
            return response
        elif path.endswith("/os"):
            payload = {"type": "FeatureCollection", "totalResults": 1, "itemsPerPage": 10,
                       "startIndex": 0, "features": [self.feature]}
        elif "/browse/" in path or path.endswith("/metadata"):
            payload = self.feature
        elif "/data/download/" in path:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("provider/synthetic.nat", self.native.read_bytes())
            response.headers["Content-Disposition"] = 'attachment; filename="synthetic.zip"'
            response.raw = io.BytesIO(buffer.getvalue())
            return response
        else:
            raise AssertionError(path)
        response._content = json.dumps(payload).encode()
        response.headers["Content-Type"] = "application/json"
        return response


def entrance(monkeypatch, native, tmp_path):
    http = DataStoreHTTP(monkeypatch, native)
    c = IASIConnector(credentials=AUTH)
    targets = c.discover(bbox=(-10, 49, 19, 56), start=START, end="2024-08-22T00:01:00Z", limit=1)
    assert len(targets) == 1 and targets[0].platform == "Metop-B"
    query = next(parse_qs(urlparse(r.url).query) for r in http.calls if urlparse(r.url).path.endswith("/os"))
    assert query["bbox"] == ["-10.0,49.0,19.0,56.0"]
    return c, c.fetch(targets[0], dest=tmp_path)


def test_real_entrance_calibration_and_sampling(monkeypatch, native, tmp_path):
    # Dependencies are initialized explicitly before pytest, never downloaded
    # by the default fixture suite. A cold installed reader fails visibly.
    monkeypatch.setenv("PYTHON_JULIAPKG_OFFLINE", "yes")
    monkeypatch.setenv("JULIA_PKG_OFFLINE", "true")
    pytest.importorskip("metoppy")
    c, result = entrance(monkeypatch, native, tmp_path)
    ds = c.read(result, sample_size=8, seed=42, strata=lambda m: m["is_day"])
    assert ds.sizes["observation"] == 8
    assert ds.sizes["wavenumber"] == 8461  # excludes native padding
    np.testing.assert_array_equal(ds.wavenumber, np.arange(645, 2760.25, .25))
    assert ds.radiance.attrs["units"] == "W m-2 sr-1 (cm-1)-1"
    # Five GIADR bands; first sample negative and preserved.
    for channel, expected in [(0, -.001), (1, .01), (3340, .001), (6428, .0001), (6960, .001), (8140, .0001)]:
        if channel == 0:
            np.testing.assert_allclose(ds.radiance[:, channel], expected)
        else:
            np.testing.assert_allclose(ds.radiance[:, channel], expected * (1 + ds.record.values / 1000))
    assert ds.attrs["platform"] == "M01"
    assert set(ds.is_day.values) == {0, 1}
    assert np.sum(ds.is_day.values == 1) == 4
    assert ds.attrs["stratum_population"] == {"1.0": 120, "0.0": 120}
    np.testing.assert_allclose(ds.satellite_zenith, 45)
    np.testing.assert_allclose(ds.solar_azimuth, 120)
    np.testing.assert_allclose(ds.land_fraction, 100)
    np.testing.assert_allclose(ds.avhrr_quality, 5)
    np.testing.assert_allclose(ds.quality_detailed, 3)
    np.testing.assert_allclose(ds.noise_covariance_metadata, .123)
    assert np.all((ds.longitude >= -10) & (ds.longitude <= 19))
    assert np.all(ds.timestamp.values >= np.datetime64("2024-08-22"))
    assert np.all(ds.timestamp.values < np.datetime64("2024-08-23"))
    expected_times = np.datetime64("2024-08-22T00:00:00", "ms") + (ds.record.values * 8000 + ds.xtrack.values * 200).astype("timedelta64[ms]")
    np.testing.assert_array_equal(ds.timestamp.values, expected_times)
    np.testing.assert_array_equal(ds.degraded_inst_mdr, ds.record.values.astype(bool))
    again = c.parse(result, sample_size=8, seed=42, strata=lambda m: m["is_day"])
    assert ds.identical(again)
    subset = c.read(result, bbox=(-10, 49, -9, 56), sample_size=1000)
    assert subset.sizes["observation"] == 16
    assert bool(subset.quality.any())  # flags retained, never silently dropped
    with pytest.raises(ValueError, match="no IASI FOVs"):
        c.read(result, bbox=(100, -20, 110, -10), sample_size=1)


def test_credentials_handoff_rotation_and_ignored_env(monkeypatch, native, tmp_path):
    http = DataStoreHTTP(monkeypatch, native)
    monkeypatch.setenv("EUMETSAT_CONSUMER_KEY", "ignored-env-key")
    monkeypatch.setenv("EUMETSAT_CONSUMER_SECRET", "ignored-env-secret")
    credentials = [AUTH]
    c = IASIConnector(credentials=lambda: credentials[0])
    targets = c.discover(start=START, end=START)
    credentials[0] = Credential("password", "rotated-secret", "rotated-key")
    c.fetch(targets[0], dest=tmp_path)
    headers = [r.headers["Authorization"] for r in http.calls if urlparse(r.url).path == "/token"]
    import base64
    assert headers == ["Basic " + base64.b64encode(pair.encode()).decode() for pair in
                       ("synthetic-key:synthetic-secret", "rotated-key:rotated-secret")]


@pytest.mark.parametrize("stage,status", [("token", 401), ("download", 403)])
def test_rejection_redaction(monkeypatch, native, tmp_path, stage, status):
    http = DataStoreHTTP(monkeypatch, native, status=status, stage=stage)
    c = IASIConnector(credentials=AUTH)
    with pytest.raises(Exception) as caught:
        targets = c.discover(start=START, end=START)
        c.fetch(targets[0], dest=tmp_path)
    if stage == "token":
        assert isinstance(caught.value, CredentialRejected)
    else:
        from spectraccess.connectors._eumetsat import EUMETSATAuthorizationError
        assert isinstance(caught.value, EUMETSATAuthorizationError)
        assert caught.value.stage == "product fetch"
    assert caught.value.status_code == status
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    for value in (AUTH.account, AUTH.secret, "synthetic-token"):
        assert value not in rendered
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not list(tmp_path.rglob("*.part"))


def test_validation_and_missing_credentials(monkeypatch):
    from spectraccess.core import credentials
    monkeypatch.setattr(credentials, "_entry", lambda provider: None)
    c = IASIConnector()
    with pytest.raises(CredentialMissing):
        c.discover(start=START, end=START)
    assert c.discover(start=START, end=START, limit=0) == []
    with pytest.raises(ValueError):
        c.discover(start=START, end=START, bbox=(20, 0, -20, 1))
