"""CMR/earthaccess discovery and shared Earthdata transport for NASA VIIRS."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlparse

import earthaccess
import netCDF4
import numpy as np
import xarray as xr
from satpy.readers.viirs_l1b import VIIRSL1BFileHandler
from satpy.readers.viirs_l2 import VIIRSL2FileHandler

from spectraccess.core.connector import Connector
from spectraccess.core.credentials import CredentialSession, resolve
from spectraccess.core.schema import frame_from_records
from spectraccess.connectors.emit_earthaccess.connector import (
    _as_start, _as_end, _validate_bbox, _verify_checksum,
)

# CMR collections checked 2026-10-08. CMR version strings are not filename tokens.
PLATFORMS = {"SNPP": "VNP", "NOAA20": "VJ1", "NOAA21": "VJ2"}
COLLECTIONS = {
    **{prefix + level + grid: "2" if prefix == "VNP" else "2.1"
       for prefix in PLATFORMS.values() for level in ("02", "03") for grid in ("MOD", "IMG")},
    "CLDMSK_L2_VIIRS_SNPP": "2", "CLDMSK_L2_VIIRS_NOAA20": "2",
    "CLDMSK_L2_VIIRS_NOAA21": "1",
    "CLDPROP_L2_VIIRS_SNPP": "1.1", "CLDPROP_L2_VIIRS_NOAA20": "1.1",
}
_NAME = re.compile(r"(?P<product>.+)\.A(?P<day>\d{7})\.(?P<time>\d{4})\.(?P<collection>\d{3})\.\d{13}\.nc$")
UNCERTAINTY_DEFINITION = (
    "NASA VIIRS L1B uncertainty index UI (valid 0..127): percent uncertainty = "
    "1.0 + scale_factor * UI^2; band-dependent scale_factor is the UI variable attribute. "
    "Index retained encoded; no standard uncertainty or coverage factor asserted."
)
_HOSTS = {"data.laadsdaac.earthdatacloud.nasa.gov", "ladsweb.modaps.eosdis.nasa.gov"}
_THERMAL_BANDS = {'M12', 'M13', 'M14', 'M15', 'M16', 'I04', 'I05'}


def _selection_error(path, group, fields, reason):
    # Only identifiers: no provider values or arrays enter this diagnostic.
    with netCDF4.Dataset(path) as root:
        product = root.getncattr('ShortName') if 'ShortName' in root.ncattrs() else _identity(path)['product']
    return ValueError(f"{reason}; product={product}; "
                      f"file={path.name}; group={group}; "
                      f"found_variables={sorted(fields)}")


def _band_annotations(band, fields):
    """NASA L1B UG section 5.1, Tables 8/9; VNP02MOD.fs section 2."""
    annotations = [(band + '_quality_flags', 'quality_flags'),
                   (band + '_uncert_index', 'uncertainty_index')]
    if band in _THERMAL_BANDS:
        annotations.append((band + '_brightness_temperature_lut', 'brightness_temperature_lookup'))
    return [(name, role) for name, role in annotations if name in fields]


@dataclass(frozen=True)
class VIIRSTarget:
    product_id: str
    title: str
    collection: str
    version: str
    platform: str
    start_time: datetime
    end_time: datetime
    footprint_geometry: Mapping | None
    source_url: str
    asset_url: str
    size_bytes: int | None
    checksums: Mapping[str, tuple[str, str]]
    retrieved_at: datetime
    raw: Mapping = field(repr=False)


@dataclass(frozen=True)
class VIIRSResult:
    paths: tuple[Path, ...]
    targets: tuple[VIIRSTarget, ...]
    retrieved_at: datetime


def _identity(path):
    match = _NAME.fullmatch(Path(path).name)
    if not match or match['product'] not in COLLECTIONS:
        raise ValueError(f"unsupported NASA VIIRS filename: {Path(path).name}")
    return match.groupdict()


def _platform(product):
    for platform, prefix in PLATFORMS.items():
        if product.startswith(prefix) or product.endswith("_" + platform):
            return platform
    raise ValueError(f"unknown VIIRS platform for {product}")


def _pair(image, geo):
    a, b = _identity(image), _identity(geo)
    if (b['product'] != a['product'].replace("02", "03", 1)
            or any(a[k] != b[k] for k in ('day', 'time', 'collection'))):
        raise ValueError("VIIRS 02/03 pairing mismatch: platform, grid, acquisition or collection")


def _footprint(umm):
    geometry = umm.get("SpatialExtent", {}).get("HorizontalSpatialDomain", {}).get("Geometry", {})
    rings = []
    for polygon in geometry.get("GPolygons", []):
        points = polygon['Boundary']['Points']
        ring = [[p['Longitude'], p['Latitude']] for p in points]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        rings.append([ring])
    if rings:
        return {"type": "Polygon", "coordinates": rings[0]} if len(rings) == 1 else {"type": "MultiPolygon", "coordinates": rings}
    rectangles = geometry.get("BoundingRectangles", [])
    if len(rectangles) == 1:
        r = rectangles[0]
        w, s, e, n = (r[k] for k in ('WestBoundingCoordinate', 'SouthBoundingCoordinate', 'EastBoundingCoordinate', 'NorthBoundingCoordinate'))
        return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
    return None


def _target(granule, product, version):
    raw = deepcopy(dict(granule))
    umm, meta = raw['umm'], raw['meta']
    ref = umm['CollectionReference']
    if ref.get('ShortName') != product or ref.get('Version') != version:
        raise ValueError("CMR returned an unexpected collection/version")
    urls = [r['URL'] for r in umm.get('RelatedUrls', []) if r.get('Type') == 'GET DATA'
            and urlparse(r['URL']).scheme == 'https' and urlparse(r['URL']).path.endswith('.nc')]
    if len(urls) != 1:
        raise ValueError("CMR must supply exactly one HTTPS netCDF asset")
    title = Path(urlparse(urls[0]).path).name
    identity = _identity(title)
    if identity['product'] != product:
        raise ValueError("CMR asset product contradicts collection")
    tokens = {'2': '002', '2.1': '021', '1': '001', '1.1': '011'}
    if identity['collection'] != tokens[version]:
        raise ValueError("CMR asset filename collection contradicts version")
    temporal = umm['TemporalExtent']['RangeDateTime']
    records = umm.get('DataGranule', {}).get('ArchiveAndDistributionInformation', [])
    checksums = {}
    size = None
    if len(records) == 1:
        record = records[0]
        size = record.get('SizeInBytes')
        if 'Checksum' in record:
            checksum = record['Checksum']
            checksums[title] = (checksum['Algorithm'], checksum['Value'])
    return VIIRSTarget(meta['concept-id'], title, product, version, _platform(product),
        _as_start(datetime.fromisoformat(temporal['BeginningDateTime'].replace('Z', '+00:00'))),
        _as_end(datetime.fromisoformat(temporal['EndingDateTime'].replace('Z', '+00:00'))),
        _footprint(umm), 'https://cmr.earthdata.nasa.gov/search/granules.umm_json?concept_id=' + meta['concept-id'],
        urls[0], int(size) if size is not None else None, checksums, datetime.now(timezone.utc), raw)


class VIIRSConnector(Connector):
    """Find and read NASA L1B and L2 granules without resampling.

    Products may be short names or MOD, IMG, CLDMSK, CLDPROP aliases.
    Discover cloud products explicitly; fetch each returned target separately.
    L1B fetch always includes its matching geolocation file.
    """
    credential_provider = "earthdata"

    def __init__(self, *, credentials=None):
        self.credentials = credentials

    def discover(self, *, bbox, start, end, products=("MOD",), platforms=("SNPP", "NOAA20", "NOAA21"),
                 limit=10, granule_name=None):
        _validate_bbox(bbox)
        start, end = _as_start(start), _as_end(end)
        if start is None or end is None or end < start:
            raise ValueError("a valid start/end interval is required")
        if not 0 <= limit <= 2000:
            raise ValueError("limit must be between 0 and 2000")
        if any(p not in PLATFORMS for p in platforms):
            raise ValueError(f"platforms must be drawn from {tuple(PLATFORMS)}")
        names = []
        for product in products:
            if product in ('MOD', 'IMG', 'CLDMSK', 'CLDPROP'):
                names.extend(PLATFORMS[p] + '02' + product if product in ('MOD', 'IMG')
                             else product + '_L2_VIIRS_' + p for p in platforms)
            elif product in COLLECTIONS or product == 'CLDPROP_L2_VIIRS_NOAA21':
                if _platform(product) in platforms:
                    names.append(product)
            else:
                raise ValueError(f"unsupported VIIRS product {product!r}")
        targets = []
        for name in dict.fromkeys(names):
            if name not in COLLECTIONS or limit == 0:
                continue  # NASA does not publish NOAA-21 CLDPROP in CMR.
            query = dict(short_name=name, version=COLLECTIONS[name], bounding_box=bbox,
                         temporal=(start, end), count=limit, sort_key='start_date')
            if granule_name is not None:
                query['granule_name'] = granule_name
            granules = earthaccess.search_data(**query)
            targets.extend(_target(g, name, COLLECTIONS[name]) for g in granules)
        return targets

    def fetch(self, target, *, dest, credentials=None, verify_checksum=True, max_bytes=400_000_000):
        source = credentials if credentials is not None else self.credentials
        credential = resolve('earthdata', source)  # Missing credentials fail before any download.
        targets = [target]
        identity = _identity(target.title)
        if '02' in identity['product'] and not identity['product'].startswith('CLD'):
            product = target.collection.replace('02', '03', 1)
            pattern = f"{product}.A{identity['day']}.{identity['time']}.{identity['collection']}.*.nc"
            matches = [_target(g, product, target.version) for g in earthaccess.search_data(
                short_name=product, version=target.version, granule_name=pattern, count=10)]
            if len(matches) != 1:
                raise ValueError(f"matching VIIRS 03 file must be unique; found {len(matches)}")
            _pair(target.title, matches[0].title)
            targets.append(matches[0])
        if sum(t.size_bytes or 0 for t in targets) > max_bytes:
            raise ValueError("VIIRS requested files exceed max_bytes download budget")
        directory = Path(dest)
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        transferred = 0
        # Same CredentialSession streaming transport used by EMIT; no earthaccess
        # login, environment credential fallback, or netrc lookup.
        with CredentialSession('earthdata', credential) as session:
            for item in targets:
                url = urlparse(item.asset_url)
                if url.scheme != 'https' or url.hostname not in _HOSTS or url.port not in (None, 443):
                    raise ValueError("refusing Earthdata credentials for an off-origin VIIRS URL")
                path = directory / item.title
                part = path.with_name('.' + path.name + '.part')
                try:
                    with session.get(item.asset_url, stream=True, timeout=120) as response, part.open('wb') as stream:
                        for chunk in response.iter_content(chunk_size=65536):
                            transferred += len(chunk)
                            if transferred > max_bytes:
                                raise ValueError("VIIRS transfer exceeds max_bytes")
                            stream.write(chunk)
                    if verify_checksum:
                        # Shared EMIT checksum helper; filename is retained on staged file.
                        from types import SimpleNamespace
                        _verify_checksum(part, SimpleNamespace(checksums={part.name: item.checksums.get(item.title)}))
                    part.replace(path)
                    paths.append(path)
                finally:
                    part.unlink(missing_ok=True)
        return VIIRSResult(tuple(paths), tuple(targets), datetime.now(timezone.utc))

    def read(self, raw, *, bbox=None, bands=None, variables=None):
        """Read one native granule (02/03 pair or one cloud product).

        Bands default to all published bands. Reflective values remain provider
        scaled reflectance (rho*cos(SZA)); thermal values use the published LUT.
        Cloud variables default to all geophysical fields; unmapped fields use
        direct provider decoding. Time stays numeric on its provider scan support.
        """
        if bbox is not None:
            _validate_bbox(bbox)
        paths = _paths(raw)
        science = [p for p in paths if not re.match(r'V(?:NP|J[12])03', p.name)]
        if len(science) != 1:
            raise ValueError("read expects one L1B pair or one cloud granule")
        image = science[0]
        ident = _identity(image)
        cloud = ident['product'].startswith('CLD')
        geo = image if cloud else next((p for p in paths if p.name.startswith(ident['product'].replace('02', '03', 1) + '.')), None)
        if geo is None:
            raise ValueError("VIIRS L1B requires its matching 03 geolocation file")
        if not cloud:
            _pair(image, geo)
            _check_metadata_pair(image, geo)
        output = xr.Dataset()
        with xr.open_dataset(geo, group='geolocation_data', decode_times=False) as geometry:
            lat, lon = geometry['latitude'], geometry['longitude']
            if lat.ndim != 2 or lat.dims != lon.dims:
                raise ValueError("VIIRS latitude/longitude must share a native 2D grid")
            grid_dims = lat.dims
            mask = np.isfinite(lat.values) & np.isfinite(lon.values)
            if bbox is not None:
                mask &= (lon.values >= bbox[0]) & (lon.values <= bbox[2]) & (lat.values >= bbox[1]) & (lat.values <= bbox[3])
            rows, columns = np.nonzero(mask)
            window = {grid_dims[0]: slice(int(rows.min()), int(rows.max()) + 1) if rows.size else slice(0, 0),
                      grid_dims[1]: slice(int(columns.min()), int(columns.max()) + 1) if rows.size else slice(0, 0)}
            shape = lat.shape
            _copy_group(output, geo, 'geolocation_data', window, grid_dims)
        group = 'geophysical_data' if cloud else 'observation_data'
        with xr.open_dataset(image, group=group, decode_cf=False) as encoded:
            fields = list(encoded.variables)
            if cloud:
                selected = list(encoded.data_vars) if variables is None else list(variables)
            else:
                available = [n for n in fields if re.fullmatch(r'[MI]\d{2}', n)]
                selected = available if bands is None else list(bands)
                if not selected:
                    raise _selection_error(image, group, fields, "no VIIRS bands selected")
                if any(n not in available for n in selected):
                    missing = sorted(set(selected) - set(available))
                    raise _selection_error(image, group, fields,
                                           f"bands must name published I/M observation bands; missing={missing}")
            for name in selected:
                if name not in encoded:
                    raise _selection_error(image, group, fields, f"missing provider variable {name}")
                provider = encoded[name]
                # Ensure a measurement cannot silently resize the geolocation grid.
                if not cloud and (provider.dims != grid_dims or provider.shape != shape):
                    raise ValueError(f"{name} differs from the matching 03 image grid")
                if not cloud or name in ('Clear_Sky_Confidence', 'Cloud_Top_Height'):
                    if name in _THERMAL_BANDS and name + '_brightness_temperature_lut' not in fields:
                        raise _selection_error(image, group, fields, f"missing provider brightness-temperature LUT for {name}")
                    _measurement(output, image, group, name, provider, window, cloud)
                else:
                    _copy_group(output, image, group, window, grid_dims, names=[name])
                if not cloud:
                    flag = name + '_quality_flags'
                    if flag not in encoded:
                        raise _selection_error(image, group, fields, f"missing provider quality flags {flag}")
                    annotations = _band_annotations(name, fields)
                    _copy_group(output, image, group, window, grid_dims, names=[n for n, _ in annotations])
                    for annotation, role in annotations:
                        output[annotation].attrs.update(associated_band=name, annotation_role=role)
                    output[name].attrs.update(quality_variable=flag,
                                             scan_time_variable='scan_start_time',
                                             row_scan_index_variable='scan_index')
        _scan_times(output, geo, window, grid_dims, shape[0], cloud)
        if not cloud:
            _science_scan_annotations(output, image)
        # These coordinates locate measurement pixels, flags and solar/view angles
        # on the same published native 03 (or cloud) support. LUTs are non-spatial.
        output = output.set_coords(['latitude', 'longitude'])
        with xr.open_dataset(image, decode_cf=False) as root:
            output.attrs['provider_global_attributes'] = deepcopy(root.attrs)
        output.attrs.update(native_geometry=True, resampled=False, support_kind='pixel',
                            provider_file=image.name, bowtie='provider geometry retained')
        return output

    def parse(self, raw, **kwargs):
        return self.read(raw, **kwargs)

    def parse_canonical(self, raw, *, target=None, bbox=None):
        paths = _paths(raw)
        if isinstance(raw, VIIRSResult):
            target = raw.targets[0]
        science = [p for p in paths if not re.match(r'V(?:NP|J[12])03', p.name)]
        if len(science) != 1:
            raise ValueError("canonical parse expects one science granule")
        path = science[0]
        if bbox is not None:
            ds = self.read(raw, bbox=bbox)
            if ds['latitude'].size == 0:
                return frame_from_records([])
        with netCDF4.Dataset(path) as root:
            attrs = {k: root.getncattr(k) for k in root.ncattrs()}
            group = root.groups.get('observation_data')
            uncertainties = {n: UNCERTAINTY_DEFINITION for n in group.variables if n.endswith('_uncert_index')} if group else {}
            cloud_group = root.groups.get('geophysical_data')
            if cloud_group:
                uncertainties.update({n: {k: v.getncattr(k) for k in v.ncattrs() if k in ('long_name', 'units', 'description')}
                                      for n, v in cloud_group.variables.items() if 'uncertainty' in n.lower()})
        if target is not None and path.name != target.title:
            raise ValueError("local file does not match the discovered target")
        start = attrs.get('time_coverage_start') or (target.start_time if target else None)
        end = attrs.get('time_coverage_end') or (target.end_time if target else None)
        if start is None or end is None:
            raise ValueError("provider acquisition interval missing")
        if target is not None:
            for value, expected in ((start, target.start_time), (end, target.end_time)):
                parsed = _as_start(datetime.fromisoformat(value.replace('Z', '+00:00'))) if isinstance(value, str) else value
                if parsed != expected:
                    raise ValueError("local acquisition interval contradicts discovered target")
        qa = deepcopy(target.raw.get('umm', {}).get('DataQuality', {})) if target else {}
        qa.update({k: v for k, v in attrs.items() if k in ('QAPercentMissingData', 'QAPercentOutofBoundsData', 'QAPercentInterpolatedData')})
        return frame_from_records([dict(time=start, valid_time=start, integration_start=start, integration_end=end,
            footprint_geometry=target.footprint_geometry if target else None, support_kind='swath',
            platform=attrs.get('platform'), instrument=attrs.get('instrument'), quantity='viirs_granule_observation',
            value=None, units=None, unc_value=None, unc_status='unknown',
            unc_definition=json.dumps(uncertainties) if uncertainties else None,
            source='nasa-viirs', source_agency='NASA / LAADS DAAC', source_url=target.source_url if target else None,
            retrieved_at=raw.retrieved_at if isinstance(raw, VIIRSResult) else (target.retrieved_at if target else None),
            product_id=target.product_id if target else path.name, product_title=path.name,
            algorithm_version=attrs.get('processing_version') or (target.raw.get('umm', {}).get('PGEVersionClass', {}).get('PGEVersion') if target else None),
            collection_version=target.version if target else None, filename_collection=_identity(path)['collection'],
            qa=qa, provider_metadata=attrs, native_data_path=str(path))])

    def _parse_kwargs_for(self, target):
        return {'target': target}

    def _canonical_kwargs_for(self, target):
        return {'target': target}


def _paths(raw):
    if isinstance(raw, VIIRSResult):
        return list(raw.paths)
    path = Path(raw)
    return sorted(path.glob('*.nc')) if path.is_dir() else [path]


def _check_metadata_pair(image, geo):
    with netCDF4.Dataset(image) as a, netCDF4.Dataset(geo) as b:
        for path, root in ((image, a), (geo, b)):
            if 'ShortName' in root.ncattrs() and root.getncattr('ShortName') != _identity(path)['product']:
                raise ValueError(f"VIIRS file identity mismatch: file={path.name}; "
                                 f"provider_product={root.getncattr('ShortName')}")
        for key in ('platform', 'time_coverage_start', 'time_coverage_end'):
            if key not in a.ncattrs() or key not in b.ncattrs() or a.getncattr(key) != b.getncattr(key):
                raise ValueError(f"VIIRS 02/03 provider metadata mismatch: {key}")
        for dimension in ('number_of_scans', 'number_of_lines', 'number_of_pixels'):
            if len(a.dimensions[dimension]) != len(b.dimensions[dimension]):
                raise ValueError(f"VIIRS 02/03 native support mismatch: {dimension}")


class _Key(dict):
    def to_dict(self):
        return dict(self)


def _measurement(output, path, group, name, provider, window, cloud):
    attrs = deepcopy(provider.attrs)
    thermal = not cloud and name in _THERMAL_BANDS
    units = attrs.get('units')
    handler_type = VIIRSL2FileHandler if cloud else VIIRSL1BFileHandler
    handler = handler_type(str(path), {'platform_shortname': _platform(_identity(path)['product'])}, {})
    key = _Key(name=name)
    if not cloud:
        key['calibration'] = 'brightness_temperature' if thermal else 'reflectance'
    info = {'name': name, 'file_key': group + '/' + name,
            'units': ('m' if name == 'Cloud_Top_Height' else '1') if cloud else ('K' if thermal else '%')}
    values = handler.get_dataset(key, info)
    rename = {d: original for d, original in zip(('y', 'x'), provider.dims) if d in values.dims}
    values = values.rename(rename).isel({d: s for d, s in window.items() if d in provider.dims}).compute()
    # Satpy checks BT range after LUT lookup. Retain the original encoded
    # validity as well, including explicitly published fill codes.
    counts = provider.isel({d: s for d, s in window.items() if d in provider.dims}).load()
    if '_FillValue' in attrs:
        values = values.where(counts != attrs['_FillValue'])
    if 'valid_min' in attrs and 'valid_max' in attrs:
        values = values.where((counts >= attrs['valid_min']) & (counts <= attrs['valid_max']))
    if not cloud and not thermal:
        values = values / 100  # Undo Satpy's percent normalization.
        units = '1'
    elif thermal:
        units = 'K'
    elif units in (None, 'none', 'None'):
        units = info['units']
    values.attrs = {**{k: v for k, v in attrs.items() if k not in ('scale_factor', 'add_offset', '_FillValue', 'valid_min', 'valid_max', 'valid_range', 'flag_values', 'flag_meanings')},
        'units': units, 'provider_attributes': attrs, 'provider_file': path.name, 'provider_variable': group + '/' + name,
        'reader': 'satpy.viirs_l2' if cloud else 'satpy.viirs_l1b', 'reader_version': '0.60.0',
        'value_transform': 'provider range mask and scale/offset' if cloud else (
            'provider brightness-temperature LUT and valid range' if thermal else 'provider scale/offset and valid range; Satpy percent normalization undone; no cosine correction'),
        'calibration': key.get('calibration')}
    output[name] = values


def _raw_field(name, value):
    return ('flag' in name.lower() or 'mask' in name.lower() or 'quality' in name.lower() or name.endswith('_uncert_index')
            or 'flag_meanings' in value.attrs or 'flag_masks' in value.attrs or 'flag_values' in value.attrs)


def _copy_group(output, path, group, window, grid_dims, names=None):
    with xr.open_dataset(path, group=group, decode_times=False) as decoded, xr.open_dataset(path, group=group, decode_cf=False) as encoded:
        for name in names if names is not None else list(encoded.data_vars):
            raw_field = _raw_field(name, encoded[name])
            value = encoded[name] if raw_field else decoded[name]
            if not raw_field:
                attrs = encoded[name].attrs
                if 'valid_min' in attrs and 'valid_max' in attrs:
                    value = value.where((encoded[name] >= attrs['valid_min']) & (encoded[name] <= attrs['valid_max']))
                elif 'valid_range' in attrs:
                    value = value.where((encoded[name] >= attrs['valid_range'][0]) & (encoded[name] <= attrs['valid_range'][1]))
            value = value.isel({d: s for d, s in window.items() if d in value.dims}).load()
            if not raw_field and ('scale_factor' in encoded[name].attrs or 'add_offset' in encoded[name].attrs):
                # Packed valid ranges describe encoded counts, not decoded units.
                # Keep them in provider_attributes without mislabelling the output.
                for key in ('valid_min', 'valid_max', 'valid_range'):
                    value.attrs.pop(key, None)
            # Only the shared native image grid may share dimensions. Tables,
            # bytes, LUTs and non-grid supports belong to their source variable.
            value = value.rename({d: f"{path.stem}_{group}_{d}" for d in value.dims if d not in grid_dims})
            value.attrs.update(provider_file=path.name, provider_variable=group + '/' + name,
                               provider_attributes=deepcopy(encoded[name].attrs), reader='provider.netcdf')
            if name.endswith('_uncert_index'):
                value.attrs['uncertainty_definition'] = UNCERTAINTY_DEFINITION
            if name in output:
                raise ValueError(f"duplicate provider variable {name}")
            output[name] = value


def _scan_times(output, geo, window, grid_dims, total_rows, cloud):
    with netCDF4.Dataset(geo) as root:
        group = root.groups.get('scan_line_attributes')
        if group is None or 'scan_start_time' not in group.variables:
            raise ValueError("VIIRS file missing provider scan_start_time")
        var = group.variables['scan_start_time']
        var.set_auto_maskandscale(False)
        values = np.asarray(var[:])
        attrs = {k: var.getncattr(k) for k in var.ncattrs()}
        if values.ndim != 1 or not values.size or total_rows % values.size:
            raise ValueError("VIIRS scan time/native row support mismatch")
        detectors = total_rows // values.size
        expected = 32 if 'IMG' in _identity(geo)['product'] else 16
        if not cloud and detectors != expected:
            raise ValueError("VIIRS row/scan ratio contradicts native detector count")
        rows = window[grid_dims[0]]
        indices = np.arange(rows.start // detectors, (rows.stop + detectors - 1) // detectors) if rows.stop > rows.start else np.array([], dtype=int)
        output.coords['scan_start_time'] = xr.DataArray(values[indices], dims=('number_of_scans',), attrs={
            **attrs, 'provider_file': geo.name, 'provider_variable': 'scan_line_attributes/scan_start_time',
            'time_support': 'scan start, numeric provider epoch; no UTC conversion',
            'provider_time_metadata': {k: root.getncattr(k) for k in root.ncattrs() if 'TAI' in k or 'leapsecond' in k.lower()}})
        output.coords['number_of_scans'] = indices
        for name, variable in group.variables.items():
            if name == 'scan_start_time':
                continue
            variable.set_auto_maskandscale(False)
            slices = tuple(indices if d == var.dimensions[0] else slice(None) for d in variable.dimensions)
            data = np.asarray(variable[slices])
            dimensions = tuple('number_of_scans' if d == var.dimensions[0]
                               else f'{geo.stem}_scan_line_attributes_{name}_{d}' for d in variable.dimensions)
            output[name] = xr.DataArray(data, dims=dimensions, attrs={
                **{k: variable.getncattr(k) for k in variable.ncattrs()},
                'provider_file': geo.name, 'provider_variable': 'scan_line_attributes/' + name,
                'reader': 'provider.netcdf', 'value_transform': 'encoded provider scan field'})
        output.coords['scan_index'] = xr.DataArray(np.arange(rows.start, rows.stop) // detectors, dims=(grid_dims[0],))
        output.coords[grid_dims[0]] = np.arange(rows.start, rows.stop)
        output.coords[grid_dims[1]] = np.arange(window[grid_dims[1]].start, window[grid_dims[1]].stop)


def _science_scan_annotations(output, image):
    """NASA L1B scan annotations (UG section 5.2/Table 11), on paired scans."""
    indices = output.number_of_scans.values
    with netCDF4.Dataset(image) as root:
        group = root.groups.get('scan_line_attributes')
        if group is None:
            raise ValueError(f"missing L1B scan_line_attributes in {image.name}")
        count = len(root.dimensions['number_of_scans'])
        for name, variable in group.variables.items():
            variable.set_auto_maskandscale(False)
            if 'number_of_scans' in variable.dimensions and variable.shape[variable.dimensions.index('number_of_scans')] != count:
                raise ValueError(f"L1B scan annotation shape mismatch: {name}")
            slices = tuple(indices if d == 'number_of_scans' else slice(None) for d in variable.dimensions)
            data = np.asarray(variable[slices])
            # 02 and 03 can publish same-named time fields with different epochs.
            alias = 'l1b_' + name if name in output else name
            dimensions = tuple(d if d == 'number_of_scans'
                               else f'{image.stem}_scan_line_attributes_{d}' for d in variable.dimensions)
            output[alias] = xr.DataArray(data, dims=dimensions, attrs={
                **{k: variable.getncattr(k) for k in variable.ncattrs()},
                'provider_file': image.name, 'provider_variable': 'scan_line_attributes/' + name,
                'reader': 'provider.netcdf', 'value_transform': 'encoded provider L1B scan field'})
    for name in output:
        if re.fullmatch(r'[MI]\d{2}', name):
            for annotation in ('scan_quality_flags', 'scan_state_flags'):
                if annotation in output:
                    output[name].attrs[annotation + '_variable'] = annotation
