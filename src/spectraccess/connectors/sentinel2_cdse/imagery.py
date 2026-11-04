"""Experimental local Sentinel-2 L1C SAFE described-sample reader.

Sources: ESA S2 PSD 15.1 (datation, angle grids, spectral information),
https://sentiwiki.copernicus.eu/web/s2-processing (PB 04.00 offsets).
Sun-grid inversion is deliberately not claimed exact.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import numpy as np
from scipy.interpolate import RegularGridInterpolator
import xarray as xr

from spectraccess.core.assumptions import AssumptionBasis, AssumptionRecord
from spectraccess.core.imagery import describe, imagery, measurement_group, unknown
from spectraccess.core.imagery.raster import read_band
from .processing import processing_steps
from .detectors import read_gml_mask

BANDS = ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B10", "B11", "B12"]
SUN_ASSUMPTION = AssumptionRecord(
    "s2_sun_grid_inversion", "Bilinear interpolation of per-tile 5 km Sun_Angles_Grid; "
    "ESA per-pixel basis "
    "unverified; radiance inversion is not exact.", AssumptionBasis.OPERATOR_DECLARED,
    "ESA processor per-pixel sun-zenith basis is established and implemented.",
)
SUN_CLIP = AssumptionRecord(
    "s2_sun_zenith_clip", "Provider reflectance processing clips sun zenith at 70 degrees; "
    "https://sentiwiki.copernicus.eu/web/s2-products", AssumptionBasis.DOCUMENTED,
    "Provider revises its published sun-zenith convention.",
)
TIME_ASSUMPTION = AssumptionRecord(
    "s2_los_time", "L1C pixel LOS inversion through a cubic Hermite provider orbit, "
    "checked against band/detector datation coverage. LOS uses WGS84 ellipsoid height 0 m; "
    "terrain height and angle interpolation affect time; "
    "relative B02/B03/B04 delays checked against published 0.527/1.005 s with a "
    "0.05 s fixture envelope. This is not an absolute-time uncertainty or standard deviation; "
    "no provider per-pixel time uncertainty is available.", AssumptionBasis.OPERATOR_DECLARED,
    "Provider L1C to L1B pixel mapping and timing uncertainty are available.",
)


def xml(path):
    """Strip namespace prefixes, including documents with qualified children."""
    root = ET.parse(path).getroot()
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]
    return root


def text(root, name):
    el = root.find(".//" + name)
    if el is None or el.text is None:
        raise ValueError(f"missing provider field {name}")
    return el.text.strip()


def parse_product(path):
    root = xml(path)
    quant = float(text(root, "QUANTIFICATION_VALUE"))
    if quant <= 0:
        raise ValueError("QUANTIFICATION_VALUE must be positive")
    baseline = text(root, "PROCESSING_BASELINE")
    offsets = {BANDS[int(el.attrib["band_id"])]: float(el.text)
               for el in root.iter("RADIO_ADD_OFFSET")}
    e0 = {BANDS[int(el.attrib["bandId"])]: float(el.text)
          for el in root.iter("SOLAR_IRRADIANCE")}
    return root, quant, baseline, offsets, e0, float(text(root, "U"))


def spectral_responses(product, *, baseline, unit, product_id):
    """Keep embedded SRF identity, without assigning ESA's external version."""
    result = {}
    for el in product.iter("Spectral_Information"):
        band = BANDS[int(el.attrib["bandId"])]
        response = el.find("Spectral_Response")
        if response is None:
            continue
        values = np.fromstring(text(response, "VALUES"), sep=" ")
        start = float(text(el, "Wavelength/MIN"))
        step = float(text(response, "STEP"))
        result[f"embedded_{band}"] = xr.Dataset({
            "wavelength": ("wavelength_sample", start + step * np.arange(len(values)),
                           {"units": "nm", "basis": "published", "uncertainty_status": "unknown"}),
            "response": (("band", "wavelength_sample"), values[None],
                         {"units": "1", "basis": "published", "uncertainty_status": "unknown"}),
        }, coords={"band": [band]}, attrs={
            "representation": "tabulated", "source": "product Spectral_Information",
            "identity": json.dumps(["embedded", baseline, unit, product_id]),
            "flight_unit": unit, "processing_baseline": baseline,
            "normalisation": "as published", "step_nm": step,
        })
    return result


def angle_grids(tile):
    """Return distinct sun and per-(band, detector) angle grids with step sizes."""
    result = {}
    for el in tile.iter():
        if el.tag not in {"Sun_Angles_Grid", "Viewing_Incidence_Angles_Grids"}:
            continue
        band = BANDS[int(el.attrib["bandId"])] if "bandId" in el.attrib else None
        detector = int(el.attrib["detectorId"]) if "detectorId" in el.attrib else None
        for tag in ("Zenith", "Azimuth"):
            part = el.find(tag)
            values = np.array([np.fromstring(row.text, sep=" ") for row in part.findall(".//VALUES")])
            result[(band, detector, tag.lower())] = (
                values, float(text(part, "ROW_STEP")), float(text(part, "COL_STEP")))
    return result


def interpolate_grid(grid, row_m, col_m, *, azimuth=False):
    """Bilinear tie-point evaluation, no extrapolation or filling missing nodes.

    Azimuth interpolates circular components across north. Missing provider
    nodes remain NaN; a detector is never substituted for another detector.
    """
    values, dy, dx = grid
    axes = (np.arange(values.shape[0]) * dy, np.arange(values.shape[1]) * dx)
    points = np.stack(np.broadcast_arrays(row_m, col_m), axis=-1)
    def interp(v):
        return RegularGridInterpolator(axes, v, bounds_error=False, fill_value=np.nan)(points)
    if azimuth:
        radians = np.radians(values)
        return np.degrees(np.arctan2(interp(np.sin(radians)), interp(np.cos(radians)))) % 360
    return interp(values)


def gps_epoch(value):
    """GPS timestamp to UTC for Sentinel-2 era (2015 onward)."""
    timestamp = value.strip().replace("Z", "+00:00")
    # Python 3.10 accepts only three or six fractional digits. Provider
    # GPS_TIME fields also contain five; padding preserves their value.
    timestamp = re.sub(r"\.([0-9]{1,5})(?=[+-]|$)",
                       lambda match: "." + match[1].ljust(6, "0"), timestamp)
    stamp = datetime.fromisoformat(timestamp)
    stamp = stamp.replace(tzinfo=timezone.utc)
    day = stamp.strftime("%Y-%m-%d")
    leap = 18 if day >= "2017-01-01" else 17 if day >= "2015-07-01" else 16
    return stamp.timestamp() - leap


def parse_datation(datastrip):
    """Preserve per-band/detector GPS reference and L1B reference line."""
    result = {}
    for el in datastrip.iter("Band_Time_Stamp"):
        band = BANDS[int(el.attrib["bandId"])]
        for det in el.findall("Detector"):
            result[(band, int(det.attrib["detectorId"]))] = {
                "gps_time": text(det, "GPS_TIME"),
                "utc_epoch": gps_epoch(text(det, "GPS_TIME")),
                "reference_line": float(text(det, "REFERENCE_LINE")),
            }
    return result


def ephemeris(datastrip):
    times, positions, velocities = [], [], []
    for pt in datastrip.iter("GPS_Point"):
        p, v = pt.find("POSITION_VALUES"), pt.find("VELOCITY_VALUES")
        if p is None or v is None:
            continue
        scale = {"mm": .001, "mm/s": .001, "m": 1., "m/s": 1.}
        times.append(gps_epoch(text(pt, "GPS_TIME")))
        positions.append(np.fromstring(p.text, sep=" ") * scale[p.get("unit", "mm")])
        velocities.append(np.fromstring(v.text, sep=" ") * scale[v.get("unit", "mm/s")])
    order = np.argsort(times)
    return np.asarray(times)[order], np.asarray(positions)[order], np.asarray(velocities)[order]


def imaging_time(eph, lon, lat, zenith_deg, azimuth_deg, *, anchor=None, height=0.):
    """Port of cubic-Hermite LOS inversion; returns UTC epoch s, residual deg.

    Numerical search is not a timing uncertainty estimate. Anchor checks that
    band/detector datation belongs to the provider orbit coverage.
    """
    from pyproj import Transformer
    times, pos, vel = eph
    if len(times) < 3 or np.any(np.diff(times) <= 0):
        raise ValueError("at least three increasing ephemeris points required")
    g = np.array(Transformer.from_crs(4979, 4978, always_xy=True).transform(lon, lat, height))
    lo, la, z, a = np.radians([lon, lat, zenith_deg, azimuth_deg])
    east = np.array([-np.sin(lo), np.cos(lo), 0])
    north = np.array([-np.sin(la)*np.cos(lo), -np.sin(la)*np.sin(lo), np.cos(la)])
    up = np.array([np.cos(la)*np.cos(lo), np.cos(la)*np.sin(lo), np.sin(la)])
    look = np.sin(z)*np.sin(a)*east + np.sin(z)*np.cos(a)*north + np.cos(z)*up
    def angle(s):
        d = s - g
        return np.degrees(np.arccos(np.clip((d @ look) / np.linalg.norm(d, axis=-1), -1., 1.)))
    def orbit(t):
        k = np.clip(np.searchsorted(times, t) - 1, 0, len(times)-2)
        h = times[k+1] - times[k]
        x = ((t-times[k])/h)[:, None]
        return ((2*x**3-3*x**2+1)*pos[k] + (x**3-2*x**2+x)*h[:, None]*vel[k]
                + (-2*x**3+3*x**2)*pos[k+1] + (x**3-x**2)*h[:, None]*vel[k+1])
    k = int(np.argmin(angle(pos)))
    if anchor is not None:
        # Datation identifies the pass; never extrapolate the orbit.
        if not times[0] <= anchor <= times[-1]:
            raise ValueError("band/detector datation outside provider ephemeris")
    low, high = times[max(k-1, 0)], times[min(k+1, len(times)-1)]
    for _ in range(3):
        grid = np.linspace(low, high, 2001)
        angles = angle(orbit(grid))
        j = int(np.argmin(angles))
        low, high = grid[max(j-2, 0)], grid[min(j+2, len(grid)-1)]
    return float(grid[j]), float(angles[j])


def read_imagery(root, *, bands=None, bounds=None, collection=None, external_srfs=None):
    """Read L1C SAFE native groups. Bounds are lon/lat, read before evaluation.

    Missing MTD_TL/MTD_DS or detector masks produce explicit unknowns. External
    SRFs are separate entries supplied by callers, never substituted silently.
    For a multi-tile product choose an extracted single granule explicitly.
    """
    root = Path(root)
    product_path = root / "MTD_MSIL1C.xml"
    product, quant, baseline, offsets, e0, u = parse_product(product_path)
    tiles = list(root.glob("GRANULE/*/MTD_TL.xml"))
    if len(tiles) > 1:
        raise ValueError("read one tile at a time")
    tile = xml(tiles[0]) if tiles else None
    grids = angle_grids(tile) if tile is not None else {}
    strips = list(root.glob("DATASTRIP/*/MTD_DS.xml"))
    strip = xml(strips[0]) if len(strips) == 1 else None
    datation = parse_datation(strip) if strip is not None else {}
    eph = ephemeris(strip) if strip is not None else ([], [], [])
    files = {}
    for path in root.rglob("*.jp2"):
        match = re.search(r"_(B(?:\d{2}|8A))(?:_\d+m)?\.jp2$", path.name)
        if match and "MSK_" not in path.name:
            if match[1] in files:
                raise ValueError("ambiguous duplicate native band raster")
            files[match[1]] = path
    selected = list(bands) if bands is not None else [b for b in BANDS if b in files]
    unit_el = product.find(".//SPACECRAFT_NAME")
    unit = unit_el.text if unit_el is not None else "unstated"
    groups = {}
    for band in selected:
        raw = read_band(files[band], bounds=bounds)
        from affine import Affine
        transform = Affine(*json.loads(raw.attrs["transform"]))
        resolution = int(round(abs(transform.a)))
        if tile is not None:
            positions = [p for p in tile.iter("Geoposition") if int(p.attrib["resolution"]) == resolution]
            if len(positions) != 1:
                raise ValueError("MTD_TL lacks a unique native-resolution geoposition")
            position = positions[0]
            expected_x = float(text(position, "ULX")) + raw.x.values[0]*resolution
            expected_y = float(text(position, "ULY")) - raw.y.values[0]*resolution
            if not (np.isclose(transform.c, expected_x, rtol=0, atol=1e-5)
                    and np.isclose(transform.f, expected_y, rtol=0, atol=1e-5)
                    and transform.b == transform.d == 0 and transform.e == -resolution
                    and raw.attrs["crs"] == text(tile, "HORIZONTAL_CS_CODE")):
                raise ValueError("native raster grid differs from MTD_TL")
        if float(baseline) >= 4 and band not in offsets:
            raise ValueError(f"RADIO_ADD_OFFSET missing at PB {baseline}: {band}")
        offset = offsets.get(band, 0.)
        rho = (raw.dn.where(raw.dn != 0) + offset) / quant
        coords = {"y": raw.y, "x": raw.x}
        descriptors = {}
        row_m = (raw.y.values[:, None] + .5) * resolution
        col_m = (raw.x.values[None, :] + .5) * resolution
        for kind, key in (("zenith", "sun_zenith"), ("azimuth", "sun_azimuth")):
            grid = grids.get((None, None, kind))
            if grid is not None:
                values = interpolate_grid(grid, row_m, col_m, azimuth=kind == "azimuth")
                descriptors[key] = describe(xr.DataArray(values, dims=("y", "x"), coords=coords),
                    basis="interpolated_from_provider_grid", source=str(tiles[0]),
                    interpolation="bilinear circular" if kind == "azimuth" else "bilinear", units="degree")
            else:
                descriptors[key] = unknown("MTD_TL sun grid absent", units="degree")
        sza = descriptors["sun_zenith"].clip(max=70)
        conversion = {"solar_spectrum": "provider SOLAR_IRRADIANCE; spectrum identity unstated in SAFE",
                      "solar_irradiance": e0.get(band), "U": u,
                      "sun_angle_convention": "bilinear per-tile 5 km sun grid, SZA clipped at 70 degree",
                      "exactness": "not_exact", "sun_zenith_source": str(tiles[0]) if tiles else "unknown"}
        rad = rho * e0[band] * u * np.cos(np.radians(sza)) / np.pi
        chain = [{"kind": "calibration", "actor": "ESA", "processor": "MSI L1C",
                  "parameters": {"QUANTIFICATION_VALUE": quant, "RADIO_ADD_OFFSET": offset,
                                 "processing_baseline": baseline, "collection": collection,
                                 "flight_unit": unit}},
                 {"kind": "orthorectification", "actor": "ESA", "processor": "MSI L1C",
                  "parameters": {"source": "https://sentiwiki.copernicus.eu/web/s2-processing"}}]
        chain[1:1] = processing_steps(baseline, collection)
        group = measurement_group({
            "toa_reflectance": describe(rho.expand_dims(band=[band]), basis="evaluated_from_provider_model",
                                        source=str(product_path), units="1", sun_normalisation="true_rho_with_70_degree_clip"),
            "radiance": describe(rad.expand_dims(band=[band]), basis="evaluated_from_provider_model",
                                 source=str(product_path), units="W m-2 sr-1 um-1", assumptions=[SUN_ASSUMPTION, SUN_CLIP],
                                 conversion=json.dumps(conversion)),
        }, primary="toa_reflectance", geometry_kind="map_grid", processing_chain=chain,
           provider_level="L1C", sensor="MSI", flight_unit=unit,
           processing_baseline=baseline, **raw.attrs)
        for name in raw.data_vars:
            if name != "dn":
                group[name] = raw[name]
        group.update(descriptors)
        group["solar_irradiance"] = describe(xr.DataArray([e0[band]], dims="band", coords={"band": [band]}),
                                            basis="published", units="W m-2 um-1", source=str(product_path))
        group["earth_sun_distance_factor"] = describe(xr.DataArray(u), basis="published", units="1",
                                                       source=str(product_path), provider_field="U")
        detectors = sorted({d for b, d, _ in grids if b == band})
        mask_paths = list(root.rglob(f"MSK_DETFOO_{band}.jp2"))
        mask = None
        if len(mask_paths) == 1:
            candidate = read_band(mask_paths[0], bounds=bounds)
            if candidate.attrs["transform"] == raw.attrs["transform"] and candidate.dn.shape == raw.dn.shape:
                mask = candidate.dn
                group["detector_index"] = describe(mask, basis="published", source=str(mask_paths[0]), units="1")
        if mask is None:
            gml_paths = list(root.rglob(f"MSK_DETFOO_{band}.gml"))
            if len(gml_paths) == 1:
                mask = read_gml_mask(gml_paths[0], shape=raw.dn.shape, transform=transform,
                                     crs=raw.attrs["crs"]).assign_coords(y=raw.y, x=raw.x)
                group["detector_index"] = describe(mask, basis="evaluated_from_provider_model", source=str(gml_paths[0]),
                    units="1", evaluation="provider polygons rasterised at native pixel centres")
        if detectors:
            arrays = {}
            for kind in ("zenith", "azimuth"):
                values = np.stack([interpolate_grid(grids[(band, d, kind)], row_m, col_m,
                                                   azimuth=kind == "azimuth") for d in detectors])
                arrays[kind] = xr.DataArray(values, dims=("detector", "y", "x"), coords=dict(coords, detector=detectors))
                if mask is not None:
                    arrays[kind] = arrays[kind].where(arrays[kind].detector == mask)
                group["view_" + kind] = describe(arrays[kind], basis="interpolated_from_provider_grid",
                    source=str(tiles[0]), units="degree", interpolation="bilinear per detector; no filling")
            if mask is not None and len(eph[0]) >= 3 and all((band, d) in datation for d in detectors):
                times = np.full(arrays["zenith"].shape, np.nan)
                residuals = np.full_like(times, np.nan)
                for di, det in enumerate(detectors):
                    for iy, ix in np.argwhere(np.isfinite(arrays["zenith"].values[di])):
                        times[di, iy, ix], residuals[di, iy, ix] = imaging_time(
                            eph, float(raw.longitude[iy, ix]), float(raw.latitude[iy, ix]),
                            arrays["zenith"].values[di, iy, ix], arrays["azimuth"].values[di, iy, ix],
                            anchor=datation[(band, det)]["utc_epoch"])
                group["time"] = describe(xr.DataArray(times, dims=arrays["zenith"].dims, coords=arrays["zenith"].coords),
                    basis="evaluated_from_provider_model", assumptions=[TIME_ASSUMPTION],
                    relative_band_delay_check_envelope_s=0.05,
                    absolute_time_uncertainty="unknown",
                    source="MTD_DS GPS_Point + Band_Time_Stamp; MTD_TL angles", units="seconds since 1970-01-01 00:00:00 UTC")
                group["time_los_residual"] = xr.DataArray(residuals, dims=arrays["zenith"].dims, coords=arrays["zenith"].coords,
                                                               attrs={"units": "degree", "meaning": "fit residual, not uncertainty"})
            else:
                group["time"] = unknown("ephemeris, band/detector datation or native detector mask missing")
        else:
            for name in ("view_zenith", "view_azimuth", "time"):
                group[name] = unknown("MTD_TL per-band/detector angle grids missing")
        if "detector_index" not in group:
            group["detector_index"] = unknown("native detector mask missing", units="1")
        # Every evaluated descriptor is band-specific before native-grid concat.
        for name in ("view_zenith", "view_azimuth", "time", "detector_index", "time_los_residual"):
            if name in group:
                group[name] = group[name].expand_dims(band=[band])
        groups.setdefault(f"r{resolution}m", []).append(group)
    combined = {}
    for name, parts in groups.items():
        if any(p.attrs["transform"] != parts[0].attrs["transform"] or
               p.attrs["crs"] != parts[0].attrs["crs"] or p.toa_reflectance.shape[1:] != parts[0].toa_reflectance.shape[1:]
               for p in parts):
            raise ValueError("native bands at the same resolution must share a grid")
        combined[name] = xr.concat(parts, dim="band", data_vars="all", coords="minimal", compat="override", join="outer")
        # Attribute dictionaries cannot broadcast. Keep complete provenance keyed
        # by band; only identical attributes also appear at variable scope.
        for variable in combined[name].data_vars:
            records = {str(p.band.values[0]): dict(p[variable].attrs) for p in parts
                       if variable in p and "basis" in p[variable].attrs}
            if len(records) != len(parts):
                continue
            shared = {k: v for k, v in next(iter(records.values())).items()
                      if all(record.get(k) == v for record in records.values())}
            shared.setdefault("basis", "unknown")
            shared.setdefault("uncertainty_status", "unknown")
            shared.update(provenance_scope="per_band", per_band_metadata=json.dumps(records, sort_keys=True))
            combined[name][variable].attrs = shared
        chain = []
        for part in parts:
            for step in json.loads(part.attrs["stage"])["processing_chain"]:
                step["parameters"]["bands"] = part.band.values.tolist()
                chain.append(step)
        stage = json.loads(combined[name].attrs["stage"])
        stage["processing_chain"] = chain
        combined[name].attrs["stage"] = json.dumps(stage)
        conversions = {str(p.band.values[0]): json.loads(p.radiance.attrs["conversion"]) for p in parts}
        combined[name]["radiance"].attrs["conversion"] = json.dumps({
            "solar_spectrum": "provider band irradiances; source spectrum unstated in SAFE",
            "sun_angle_convention": "bilinear per-tile 5 km sun grid, SZA clipped at 70 degree",
            "solar_irradiance_variable": "solar_irradiance", "U_variable": "earth_sun_distance_factor",
            "per_band_parameters": conversions, "exactness": "not_exact",
        })
    srfs = spectral_responses(product, baseline=baseline, unit=unit, product_id=root.name)
    srfs.update(external_srfs or {})
    tree = imagery(combined, spectral_responses=srfs, sensor="MSI", product_id=root.name)
    # Provider XML preserved verbatim, including coefficients not interpreted here.
    tree["/provider_models"] = xr.DataTree(xr.Dataset(attrs={
        "product_xml": product_path.read_text(encoding="utf-8"),
        "tile_xml": tiles[0].read_text(encoding="utf-8") if tiles else "unstated",
        "datastrip_xml": strips[0].read_text(encoding="utf-8") if len(strips) == 1 else "unstated",
        "datation": json.dumps({f"{b}/{d}": v for (b, d), v in datation.items()}),
    }))
    return tree
