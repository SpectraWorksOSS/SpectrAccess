"""Recorded catalogue and physical synthetic native files through real adapters."""
import io
import json
import logging
import runpy
import traceback
import zipfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np
import pytest
import requests

pytest.importorskip("satpy")
pytest.importorskip("eumdac")
from spectraccess.connectors.seviri_eumetsat import SEVIRIConnector, SEVIRIResult, SEVIRITarget, target_to_canonical
from spectraccess.connectors.seviri_eumetsat import connector as mod
from spectraccess.connectors import _eumetsat as shared
from spectraccess.core.credentials import Credential, CredentialMissing, CredentialRejected
from spectraccess.core.schema import validate

ROOT = Path(__file__).parent / 'fixtures/seviri_eumetsat'
GENERATOR = runpy.run_path(str(ROOT / 'generate.py'))
START = GENERATOR['START']
CREDENTIAL = Credential('password', 'synthetic-secret-never-log', 'synthetic-key-never-log')


def target():
    return SEVIRITarget('synthetic', mod.COLLECTIONS['rapid_scan'], 'rapid_scan', 'MSG2', START,
                        START + timedelta(minutes=4), 123, {}, 'https://example.org/product', START)


@pytest.fixture(scope='module')
def native(tmp_path_factory):
    return SEVIRIResult(GENERATOR['generate'](tmp_path_factory.mktemp('seviri')), target(), START)


class DataStoreHTTP:
    """Stub HTTP only; EUMDAC owns token, catalogue parsing and ZIP stream APIs."""
    token = 'synthetic-bearer-never-log'

    def __init__(self, native, monkeypatch, *, status=200, token_status=200, zip_entries=None):
        self.native, self.status, self.token_status = native, status, token_status
        self.zip_entries = zip_entries
        self.calls = []
        self.catalogues = {collection: json.loads((ROOT / (service + '.json')).read_text())
                           for service, collection in mod.COLLECTIONS.items()}
        monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', lambda _, request, **kw: self.send(request))

    def send(self, request):
        self.calls.append(request)
        path, query = unquote(urlparse(request.url).path), parse_qs(urlparse(request.url).query)
        response = requests.Response()
        response.request, response.url, response.status_code = request, request.url, 200
        if path == '/token':
            response.status_code = self.token_status
            payload = {'access_token': self.token, 'expires_in': 86400}
        elif self.status != 200 and '/data/download/' in path:
            response.status_code = self.status
            payload = {'error': f'GeneralLicense required {CREDENTIAL.account} {CREDENTIAL.secret} Bearer {self.token}'}
        elif path.endswith('/osdd'):
            response._content = (b'<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/" '
                b'xmlns:parameters="http://a9.com/-/spec/opensearch/extensions/parameters/1.0/">'
                b'<ShortName>Synthetic SEVIRI</ShortName><Url type="application/json" '
                b'template="https://api.eumetsat.int/data/search-products/1.0.0/os?format=json">'
                b'<parameters:Parameter name="dtstart" value="{time:start?}" />'
                b'<parameters:Parameter name="dtend" value="{time:end?}" />'
                b'<parameters:Parameter name="set" value="{eum:set?}" />'
                b'</Url></OpenSearchDescription>')
            return response
        elif path.endswith('/os'):
            payload = self.catalogues[query['pi'][0]]
        elif '/browse/' in path or path.endswith('/metadata'):
            identifier = path.split('/products/')[1].split('/')[0]
            payload = next(f for catalogue in self.catalogues.values() for f in catalogue['features'] if f['id'] == identifier)
        elif '/data/download/' in path:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                for name in self.zip_entries or ['provider/product.nat', 'EOPMetadata.xml']:
                    archive.writestr(name, self.native.path.read_bytes() if name.endswith('.nat') else b'<synthetic/>')
            response.headers['Content-Disposition'] = 'attachment; filename="synthetic.zip"'
            response.raw = io.BytesIO(buffer.getvalue())
            return response
        else:
            raise AssertionError(path)
        if response.status_code >= 400 and path == '/token':
            payload = {'error': f'invalid_client {CREDENTIAL.account} {CREDENTIAL.secret} Bearer {self.token}'}
        response._content = json.dumps(payload).encode()
        response.headers['Content-Type'] = 'application/json'
        return response


def test_discovery_and_zip_fetch_real_eumdac(native, tmp_path, monkeypatch):
    http = DataStoreHTTP(native, monkeypatch)
    connector = SEVIRIConnector(credentials=CREDENTIAL)
    targets = connector.discover(bbox=(4, 51, 6, 53), start=START, end=START + timedelta(minutes=20))
    assert len(targets) == 6
    assert {t.service for t in targets} == {'full_disc', 'rapid_scan'}
    assert {t.platform for t in targets} == {'MSG2', 'MSG3'}
    for t in targets:
        assert t.footprint is None and t.bbox == (4, 51, 6, 53)
        begin, end = t.raw['properties']['date'].split('/')
        assert t.sensing_start == mod._utc(begin) and t.sensing_end == mod._utc(end)
        assert t.size == t.raw['properties']['productInformation']['size']
        assert 'connector-declared' in t.service_coverage['basis']
        assert 'footprint' in t.service_coverage['basis']
        assert t.service_coverage['source'] == mod.COVERAGE_SOURCE
        validate(target_to_canonical(t))
    searches = [r for r in http.calls if urlparse(r.url).path.endswith('/os')]
    assert searches and all('bbox' not in parse_qs(urlparse(r.url).query) for r in searches)
    result = connector.fetch(targets[-1], dest=tmp_path)
    assert result.path.read_bytes() == native.path.read_bytes()
    assert result.source_entry == 'provider/product.nat'
    assert not list(result.path.parent.glob('*.part'))
    assert not (result.path.parent / 'provider').exists()
    assert len(connector.discover(start=START, end=START, limit=1)) == 2
    assert connector.discover(start=START, end=START, limit=0) == []


def test_native_explicit_calibration_time_and_qa(native):
    connector = SEVIRIConnector()
    ds = connector.read(native, channels=mod.CHANNELS)
    assert ds.IR_108.shape == (4, 8) and ds.HRV.shape == (12, 24)
    assert ds.IR_108.values[0, 0] == -8  # Satpy would clip this to zero.
    assert np.isnan(ds.IR_108.values[0, -1])
    np.testing.assert_array_equal(ds.IR_108_line_validity, [1, 2, 3, 4])
    np.testing.assert_array_equal(ds.IR_108_line_rquality, [0, 1, 2, 3])
    np.testing.assert_array_equal(ds.IR_108_line_gquality, [4, 3, 2, 1])
    assert ds.attrs['native_geometry'] is True and ds.attrs['resampled'] is False
    assert 'south to north' in ds.attrs['row_convention']
    assert ds.attrs['platform'] == 'MSG2'
    assert ds.attrs['satellite_actual_position_available']
    assert ds.attrs['orbital_parameters']['satellite_actual_longitude'] == pytest.approx(0)
    assert ds.attrs['orbital_parameters']['satellite_nominal_longitude'] == pytest.approx(9.5)
    for channel in mod.CHANNELS:
        times = connector.line_times(native, channel=channel)
        np.testing.assert_array_equal(ds[channel + '_time'], times.time)
        np.testing.assert_array_equal(ds['y_' + channel], times.row)
        assert np.all(np.diff(times.time.values) > np.timedelta64(0, 'ms'))
        first = 5563 if channel == 'HRV' else 1855
        np.testing.assert_array_equal(times.row, np.arange(first, first + times.sizes['row']))
        assert times.attrs['actual_coverage']['VIS_IR']['SouthernLineActual'] == 1855
        assert times.attrs['actual_coverage']['VIS_IR']['NorthernLineActual'] == 1858
        assert times.attrs['service_coverage']['south_line'] == 2321
        assert ds[channel].attrs['reader_provenance']['negative_radiance_clipping'] is False
    counts = connector.read(native, calibration='counts', channels=('VIS006', 'IR_108'))
    assert counts.VIS006.values[0, 0] == 1 and counts.IR_108.attrs['units'] == 'count'
    assert counts.VIS006.attrs['calibration_mode'] == 'nominal'


@pytest.mark.parametrize('calibration', ['reflectance', 'brightness_temperature', 'nominal', None])
def test_reject_calibration(native, calibration):
    with pytest.raises(ValueError, match='counts or radiance'):
        SEVIRIConnector().read(native, calibration=calibration)


@pytest.mark.parametrize('bbox', [(-.02, -.02, .02, .02), (-2, -.02, .02, .02), (150, -10, 160, 10)])
def test_bbox_native_window_without_resampling(native, bbox):
    connector = SEVIRIConnector()
    full = connector.read(native, channels=('IR_108', 'HRV'), calibration='counts')
    clipped = connector.read(native, bbox=bbox, channels=('IR_108', 'HRV'), calibration='counts')
    for channel in ('IR_108', 'HRV'):
        area = mod._handler(native).get_area_def(mod._key(channel))
        lon, lat = area.get_lonlats()
        rows, cols = np.where((lon >= bbox[0]) & (lon <= bbox[2]) & (lat >= bbox[1]) & (lat <= bbox[3]))
        if not rows.size:
            assert clipped[channel].size == 0
        else:
            expected = full[channel].values[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
            np.testing.assert_array_equal(clipped[channel], expected)
            np.testing.assert_array_equal(clipped['y_' + channel], full['y_' + channel][rows.min():rows.max() + 1])
    frame = connector.parse_canonical(native, bbox=bbox)
    assert frame.empty == (bbox[0] == 150)
    validate(frame)


def test_canonical_provider_qa(native):
    frame = SEVIRIConnector().parse_canonical(native)
    validate(frame)
    assert len(frame) == 1 and frame.attrs['spectraccess_schema_version'] == '1.0'
    row = frame.iloc[0]
    assert row.platform == 'MSG2' and row.instrument == 'SEVIRI'
    assert row.footprint_geometry is None and row.unc_status == 'unknown'
    assert row.integration_start == START and row.integration_end == START + timedelta(minutes=4)
    np.testing.assert_array_equal(row.qa['NonNominalRadiometricQuality'], np.arange(12) % 2)


def test_missing_credentials_no_env_fallback(monkeypatch):
    monkeypatch.setenv('EUMETSAT_KEY', 'should-not-be-read')
    monkeypatch.setenv('EUMETSAT_SECRET', 'should-not-be-read')
    monkeypatch.setattr('spectraccess.core.credentials.keyring.get_password', lambda *args: None)
    connector = SEVIRIConnector()
    with pytest.raises(CredentialMissing):
        connector.discover(start=START, end=START)
    with pytest.raises(CredentialMissing):
        connector.fetch(target())


@pytest.mark.parametrize('status', [401, 403, 500])
def test_provider_failure_redaction(native, monkeypatch, caplog, status):
    DataStoreHTTP(native, monkeypatch, status=status)
    caplog.set_level(logging.DEBUG, logger='eumdac')
    connector = SEVIRIConnector(credentials=CREDENTIAL)
    expected = mod.SEVIRIAuthorizationError if status in (401, 403) else mod.SEVIRIProviderError
    with pytest.raises(expected) as caught:
        connector.discover(start=START, end=START)
    error = caught.value
    assert error.status_code == status and error.stage == 'catalogue query'
    assert 'GeneralLicense' in str(error)
    detail = ''.join(traceback.format_exception(error)) + caplog.text
    assert all(secret not in detail for secret in (CREDENTIAL.account, CREDENTIAL.secret, DataStoreHTTP.token))
    assert isinstance(error, shared.EUMETSATProviderError)


def test_token_rejected(native, monkeypatch):
    DataStoreHTTP(native, monkeypatch, token_status=401)
    with pytest.raises(CredentialRejected):
        SEVIRIConnector(credentials=CREDENTIAL).discover(start=START, end=START)


@pytest.mark.parametrize('entries', [['one.nat', 'two.nat'], ['only.xml']])
def test_zip_requires_one_native_file(native, tmp_path, monkeypatch, entries):
    DataStoreHTTP(native, monkeypatch, zip_entries=entries)
    connector = SEVIRIConnector(credentials=CREDENTIAL)
    t = connector.discover(start=START, end=START, limit=1)[0]
    with pytest.raises(mod.SEVIRIProviderError, match='exactly one'):
        connector.fetch(t, dest=tmp_path)
    assert not list(tmp_path.rglob('*.part'))


def test_historical_georeferencing_offset(tmp_path):
    old = SEVIRIResult(GENERATOR['generate'](tmp_path / 'old', earth_model=1), target(), START)
    new = SEVIRIResult(GENERATOR['generate'](tmp_path / 'new', earth_model=2), target(), START)
    a = SEVIRIConnector().read(old)
    b = SEVIRIConnector().read(new)
    np.testing.assert_array_equal(a.IR_108, b.IR_108)
    assert a.attrs['provider_earth_model']['TypeOfEarthModel'] == 1
    np.testing.assert_allclose(a.IR_108_projection_x - b.IR_108_projection_x, 1500.20158, rtol=1e-6)
    np.testing.assert_allclose(a.IR_108_projection_y - b.IR_108_projection_y, -1500.20158, rtol=1e-6)


def test_single_channel_native_file(tmp_path):
    result = SEVIRIResult(GENERATOR['generate'](tmp_path, single_channel=True), target(), START)
    ds = SEVIRIConnector().read(result, channels=('IR_108',))
    np.testing.assert_array_equal(ds.IR_108_time, SEVIRIConnector().line_times(result).time)


def test_hrv_upper_lower_native_windows(tmp_path):
    result = SEVIRIResult(GENERATOR['generate'](tmp_path, stacked_hrv=True), target(), START)
    full = SEVIRIConnector().read(result, channels=('HRV',), calibration='counts')
    assert full.HRV.shape == (12, 24)
    assert len(full.HRV.attrs['native_area']) == 2
    # Upper window has a different native column origin; retain projected
    # coordinates per row rather than assigning one shared x coordinate.
    assert full.HRV_projection_x.values[0, 0] != full.HRV_projection_x.values[6, 0]
    clipped = SEVIRIConnector().read(result, channels=('HRV',), calibration='counts', bbox=(-.02, -.02, .02, .02))
    assert clipped.HRV.size > 0
    np.testing.assert_array_equal(clipped.HRV_time,
                                  full.HRV_time.sel(y_HRV=clipped.y_HRV))
    np.testing.assert_array_equal(clipped.HRV,
                                  full.HRV.sel(y_HRV=clipped.y_HRV, x_HRV=clipped.x_HRV))


def test_smoke_skips_without_credentials(monkeypatch, capsys):
    monkeypatch.delenv('EUMETSAT_KEY', raising=False)
    monkeypatch.delenv('EUMETSAT_SECRET', raising=False)
    smoke = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/live_smoke.py'))['smoke_seviri_eumetsat']
    smoke()
    assert 'SEVIRI EUMETSAT smoke SKIP' in capsys.readouterr().out

@pytest.mark.parametrize('status', [401, 403])
def test_smoke_reports_redacted_provider_stage(native, monkeypatch, capsys, status):
    DataStoreHTTP(native, monkeypatch, status=status)
    monkeypatch.setenv('EUMETSAT_KEY', CREDENTIAL.account)
    monkeypatch.setenv('EUMETSAT_SECRET', CREDENTIAL.secret)
    smoke = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/live_smoke.py'))['smoke_seviri_eumetsat']
    with pytest.raises(mod.SEVIRIAuthorizationError):
        smoke()
    output = capsys.readouterr().out
    assert 'catalogue query' in output and f'HTTP {status}' in output and 'GeneralLicense' in output
    assert all(secret not in output for secret in (CREDENTIAL.account, CREDENTIAL.secret, DataStoreHTTP.token))


def test_smoke_residual_and_coverage_output(tmp_path, monkeypatch, capsys):
    # Exercise smoke control flow with artificial native data and recorded
    # catalogue. This is not a credentialed provider proof.
    monkeypatch.setitem(GENERATOR['generate'].__globals__, 'START', START + timedelta(seconds=10.683))
    synthetic = SEVIRIResult(GENERATOR['generate'](tmp_path), target(), START)
    DataStoreHTTP(synthetic, monkeypatch)
    monkeypatch.setenv('EUMETSAT_KEY', CREDENTIAL.account)
    monkeypatch.setenv('EUMETSAT_SECRET', CREDENTIAL.secret)

    class SyntheticWindowConnector(SEVIRIConnector):
        def read(self, result, *, bbox=None, **kwargs):
            assert bbox == (4.8, 52.3, 4.95, 52.4)
            # Synthetic file's small native window is near the equator.
            return super().read(result, bbox=(-.02, -.02, .02, .02), **kwargs)

    monkeypatch.setattr('spectraccess.connectors.seviri_eumetsat.SEVIRIConnector', SyntheticWindowConnector)
    smoke = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/live_smoke.py'))['smoke_seviri_eumetsat']
    smoke()
    output = capsys.readouterr().out
    assert 'actual L1.5 coverage: 1855-1858; documented nominal: 2321-3712' in output
    assert 'scan-law residual seconds: max=' in output and 'rms=' in output
    assert 'SEVIRI EUMETSAT PASS' in output


def test_committed_synthetic_fixture_reproducible(tmp_path):
    assert GENERATOR['generate'](tmp_path).read_bytes() == (ROOT / 'synthetic.nat').read_bytes()
