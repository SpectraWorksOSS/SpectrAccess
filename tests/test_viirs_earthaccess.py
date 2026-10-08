"""NASA-shaped native swath fixtures; no authenticated or real downloads."""
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

pytest.importorskip('satpy')
pytest.importorskip('earthaccess')
pytest.importorskip('netCDF4')
import netCDF4
from spectraccess.connectors.viirs_earthaccess import VIIRSConnector, VIIRSResult
from spectraccess.connectors.viirs_earthaccess import connector as module
from spectraccess.core.credentials import Credential, CredentialMissing
from spectraccess.core.schema import validate

FIXTURES = Path(__file__).parent / 'fixtures' / 'viirs_earthaccess'


def generate(root, **kwargs):
    spec = importlib.util.spec_from_file_location('viirs_generate', FIXTURES / 'generate.py')
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    return generator.generate(root, **kwargs)


@pytest.fixture(scope='module')
def fixtures(tmp_path_factory):
    return generate(tmp_path_factory.mktemp('viirs'))


def pair(root, grid='MOD'):
    paths = tuple(sorted(root.glob(f'VNP*{grid}.*.nc')))
    return VIIRSResult(paths, (), datetime.now(timezone.utc))


def granule(path):
    name = path.name
    product = name.split('.')[0]
    return {'meta': {'concept-id': 'G1-LAADS'}, 'umm': {
        'CollectionReference': {'ShortName': product, 'Version': module.COLLECTIONS[product]},
        'TemporalExtent': {'RangeDateTime': {'BeginningDateTime': '2024-05-01T00:00:00Z',
                                           'EndingDateTime': '2024-05-01T00:06:00Z'}},
        'RelatedUrls': [{'Type': 'GET DATA', 'URL': f'https://data.laadsdaac.earthdatacloud.nasa.gov/prod-lads/{product}/{name}'}],
        'DataGranule': {'ArchiveAndDistributionInformation': [{'SizeInBytes': path.stat().st_size,
            'Checksum': {'Algorithm': 'MD5', 'Value': hashlib.md5(path.read_bytes()).hexdigest()}}]},
        'SpatialExtent': {'HorizontalSpatialDomain': {'Geometry': {'GPolygons': [{'Boundary': {'Points': [
            {'Longitude': x, 'Latitude': y} for x, y in [(0, 50), (7, 50), (7, 53), (0, 53), (0, 50)]]}}]}}},
        'DataQuality': {'QualityFlag': 'provider verdict'}, 'PGEVersionClass': {'PGEVersion': '3.0.30'}}}


@pytest.mark.parametrize('grid,bands,detectors', [('MOD', ('M09', 'M15'), 16), ('IMG', ('I01', 'I05'), 32)])
@pytest.mark.parametrize('bbox', [(2, 51, 4, 52), (-2, 49, 3, 51), (30, 10, 31, 11), None])
def test_native_windows(fixtures, grid, bands, detectors, bbox):
    result = pair(fixtures, grid)
    ds = VIIRSConnector().read(result, bbox=bbox, bands=bands)
    geo = result.paths[1]
    with xr.open_dataset(geo, group='geolocation_data') as native:
        lat, lon = native.latitude.values, native.longitude.values
        inside = np.isfinite(lat)
        if bbox:
            inside &= (lon >= bbox[0]) & (lon <= bbox[2]) & (lat >= bbox[1]) & (lat <= bbox[3])
        ii, jj = np.nonzero(inside)
        window = (slice(ii.min(), ii.max()+1), slice(jj.min(), jj.max()+1)) if ii.size else (slice(0, 0), slice(0, 0))
        np.testing.assert_equal(ds.latitude.values, lat[window])
        np.testing.assert_equal(ds.longitude.values, lon[window])
        assert ds[bands[0]].shape == lat[window].shape
    with xr.open_dataset(result.paths[0], group='observation_data', decode_cf=False) as raw:
        np.testing.assert_equal(ds[bands[0] + '_quality_flags'].values, raw[bands[0] + '_quality_flags'].values[window])
        np.testing.assert_equal(ds[bands[0] + '_uncert_index'].values, raw[bands[0] + '_uncert_index'].values[window])
        encoded = raw[bands[0]].values[window]
        expected = encoded * raw[bands[0]].attrs['scale_factor']
        expected = np.where(encoded > 65527, np.nan, expected)
        np.testing.assert_allclose(ds[bands[0]], expected, rtol=1e-6)
        lut = raw[bands[1] + '_brightness_temperature_lut'].values
        bt = lut[raw[bands[1]].values[window]]
        bt = np.where(raw[bands[1]].values[window] > 65527, np.nan, bt)
        np.testing.assert_allclose(ds[bands[1]], bt)
    assert ds.scan_start_time.attrs['long_name'] == 'Scan start time (TAI93)'
    assert ds.scan_start_time.attrs['units'] == 'seconds'
    assert np.all(np.diff(ds.scan_start_time.values) > 0)
    assert ds.scan_start_time.dtype == np.float64
    np.testing.assert_equal(ds.scan_index.values, ds.number_of_lines.values // detectors)
    assert ds.attrs['native_geometry'] and not ds.attrs['resampled']
    assert ds[bands[0]].attrs['units'] == '1'
    assert ds[bands[0]].attrs['provider_attributes']['units'] == 'none'
    assert ds[bands[0]].attrs['provider_attributes']['valid_max'] == 65527
    assert 'valid_max' not in ds[bands[0]].attrs
    assert 'platform_name' not in ds[bands[0]].attrs
    # Identical dimension name "lookup" with 65536 and 7 elements survives.
    assert ds.navigation_table.size == 7
    assert ds[bands[1] + '_brightness_temperature_lut'].size == 65536
    assert ds.navigation_table.dims != ds[bands[1] + '_brightness_temperature_lut'].dims


@pytest.mark.parametrize('product', ['CLDMSK_L2_VIIRS_SNPP', 'CLDPROP_L2_VIIRS_SNPP'])
def test_cloud_native(fixtures, product):
    path = next(fixtures.glob(product + '.*.nc'))
    ds = VIIRSConnector().read(path, bbox=(2, 51, 4, 52))
    assert ds.latitude.shape == (16, 3)
    assert ds.scan_start_time.size == 1
    assert ds.Quality_Assurance.dtype == np.uint8
    assert np.all(ds.Quality_Assurance.values == 255)
    if product.startswith('CLDMSK'):
        np.testing.assert_allclose(ds.Clear_Sky_Confidence, .75)
        assert ds.Cloud_Mask.shape == (6, 16, 3)
        assert np.all(ds.Cloud_Mask.values == 255)
    else:
        np.testing.assert_allclose(ds.Cloud_Top_Height, 10000)
        np.testing.assert_allclose(ds.Cloud_Top_Temperature, 100.7935)
        np.testing.assert_allclose(ds.Cloud_Top_Temperature_Uncertainty, 1.)
        assert ds.Cloud_Top_Temperature.attrs['reader'] == 'provider.netcdf'
        frame = VIIRSConnector().parse_canonical(path)
        assert 'Cloud_Top_Temperature_Uncertainty' in frame.unc_definition.iloc[0]


def test_canonical(fixtures):
    paths = pair(fixtures).paths
    target = module._target(granule(paths[0]), 'VNP02MOD', '2')
    result = VIIRSResult(paths, (target,), datetime.now(timezone.utc))
    frame = VIIRSConnector().parse_canonical(result)
    validate(frame)
    assert len(frame) == 1
    assert {'valid_time', 'integration_start', 'integration_end', 'footprint_geometry',
            'support_kind', 'qa', 'algorithm_version', 'collection_version', 'unc_definition'} <= set(frame.columns)
    assert frame.support_kind.iloc[0] == 'swath'
    assert frame.collection_version.iloc[0] == '2'
    assert frame.algorithm_version.iloc[0] == '3.0.30'
    assert frame.qa.iloc[0] == {'QualityFlag': 'provider verdict'}
    assert frame.footprint_geometry.iloc[0]['type'] == 'Polygon'
    assert 'UI^2' in frame.unc_definition.iloc[0]
    assert frame.unc_value.isna().all() and frame.unc_k.isna().all()
    assert VIIRSConnector().parse_canonical(result, bbox=(30, 10, 31, 11)).empty


@pytest.mark.parametrize('replacement', ['VJ103MOD', 'VNP03IMG', '.0001.', '.021.'])
def test_pair_mismatch(fixtures, replacement):
    image, geo = pair(fixtures).paths
    bad = geo.name.replace('VNP03MOD', replacement) if replacement.startswith('V') else geo.name.replace('.0000.' if replacement == '.0001.' else '.002.', replacement)
    with pytest.raises(ValueError, match='pairing mismatch'):
        module._pair(image, bad)


def test_metadata_mismatch(fixtures, tmp_path):
    import shutil
    image, geo = pair(fixtures).paths
    new_geo = tmp_path / geo.name
    shutil.copyfile(geo, new_geo)
    with netCDF4.Dataset(new_geo, 'a') as root:
        root.platform = 'NOAA-20'
    result = VIIRSResult((image, new_geo), (), datetime.now(timezone.utc))
    with pytest.raises(ValueError, match='metadata mismatch'):
        VIIRSConnector().read(result)


def test_discovery_platform_products(fixtures, monkeypatch):
    calls = []
    def search(**kwargs):
        calls.append(kwargs)
        return [granule(next(fixtures.glob('VNP02MOD.*.nc')))] if kwargs['short_name'] == 'VNP02MOD' else []
    monkeypatch.setattr(module.earthaccess, 'search_data', search)
    targets = VIIRSConnector().discover(bbox=(0, 50, 7, 53), start=datetime(2024, 5, 1),
        end=datetime(2024, 5, 2), products=('MOD', 'IMG', 'CLDMSK', 'CLDPROP'))
    assert len(targets) == 1
    assert len(calls) == 11
    assert not any(c['short_name'] == 'CLDPROP_L2_VIIRS_NOAA21' for c in calls)
    assert next(c for c in calls if c['short_name'] == 'VJ202MOD')['version'] == '2.1'
    assert targets[0].start_time.tzinfo == timezone.utc


def test_missing_credential(fixtures, monkeypatch, tmp_path):
    monkeypatch.setattr('spectraccess.core.credentials._entry', lambda provider: None)
    monkeypatch.setenv('EARTHDATA_USERNAME', 'ignored')
    monkeypatch.setenv('EARTHDATA_PASSWORD', 'ignored')
    target = module._target(granule(pair(fixtures).paths[0]), 'VNP02MOD', '2')
    with pytest.raises(CredentialMissing):
        VIIRSConnector().fetch(target, dest=tmp_path)


def test_fetch_matching_pair(fixtures, monkeypatch, requests_mock, tmp_path):
    image, geo = pair(fixtures).paths
    source = Credential('password', 'fixture-secret', 'fixture-user')
    target = module._target(granule(image), 'VNP02MOD', '2')
    monkeypatch.setattr(module.earthaccess, 'search_data', lambda **kwargs: [granule(geo)])
    for path in (image, geo):
        url = module._target(granule(path), path.name.split('.')[0], '2').asset_url
        requests_mock.get(url, content=path.read_bytes())
    result = VIIRSConnector(credentials=source).fetch(target, dest=tmp_path)
    assert len(result.paths) == 2
    assert all(p.exists() for p in result.paths)
    assert not list(tmp_path.glob('*.part'))
    assert VIIRSConnector().read(result, bands=('M09',)).M09.size > 0


def test_fetch_bad_checksum(fixtures, monkeypatch, requests_mock, tmp_path):
    path = next(fixtures.glob('CLDMSK*.nc'))
    target = module._target(granule(path), 'CLDMSK_L2_VIIRS_SNPP', '2')
    requests_mock.get(target.asset_url, content=b'corrupt')
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        VIIRSConnector(credentials=Credential('password', 'secret', 'user')).fetch(target, dest=tmp_path)
    assert not list(tmp_path.iterdir())


def test_fetch_missing_pair(fixtures, monkeypatch, tmp_path):
    target = module._target(granule(pair(fixtures).paths[0]), 'VNP02MOD', '2')
    monkeypatch.setattr(module.earthaccess, 'search_data', lambda **kwargs: [])
    with pytest.raises(ValueError, match='must be unique'):
        VIIRSConnector(credentials=Credential('password', 'secret', 'user')).fetch(target, dest=tmp_path)


def test_download_budget(fixtures, requests_mock, tmp_path):
    path = next(fixtures.glob('CLDMSK*.nc'))
    target = module._target(granule(path), 'CLDMSK_L2_VIIRS_SNPP', '2')
    with pytest.raises(ValueError, match='download budget'):
        VIIRSConnector(credentials=Credential('password', 'secret', 'user')).fetch(target, dest=tmp_path, max_bytes=10)
    assert not requests_mock.called


def test_missing_time(fixtures, tmp_path):
    image, new_geo = pair(generate(tmp_path, missing_time=True)).paths
    with pytest.raises(ValueError, match='missing provider scan_start_time'):
        VIIRSConnector().read(VIIRSResult((image, new_geo), (), datetime.now(timezone.utc)), bands=('M09',))


def test_smoke_skip(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('viirs_smoke', Path(__file__).parents[1] / 'scripts' / 'live_smoke.py')
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    monkeypatch.delenv('EARTHDATA_USERNAME', raising=False)
    monkeypatch.delenv('EARTHDATA_PASSWORD', raising=False)
    smoke.smoke_viirs_earthaccess()
    assert 'SKIP' in capsys.readouterr().out
