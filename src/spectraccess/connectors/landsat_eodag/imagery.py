"""Landsat 8/9 OLI Collection 2 L1 quantity layers, without resampling."""

import json
from pathlib import Path

import numpy as np
import xarray as xr

from spectraccess.core.assumptions import AssumptionBasis, AssumptionRecord

from spectraccess.core.imagery import describe, imagery, measurement_group, unknown
from spectraccess.core.imagery.raster import read_band
from spectraccess.core.imagery.spectral import read_usgs_srf
from .angles import read_odl, evaluate_angles


def read_imagery(root, *, bands=None, bounds=None, height=None, spectral_responses=None):
    """Read an extracted local L1 directory; optional lon/lat bounds limit I/O.

    OLI reflective bands only. Missing ANG produces unknown geometry/time.
    Coefficients are retained; height=None explicitly uses each band's mean
    height and labels it. DN zero is no data. Thermal channels are not OLI.
    """
    root = Path(root)
    mtl_path, = root.glob("*_MTL.txt")
    mtl = read_odl(mtl_path)
    if mtl.get("SPACECRAFT_ID") not in {"LANDSAT_8", "LANDSAT_9"}:
        raise ValueError("expected Landsat 8/9")
    if not str(mtl.get("PROCESSING_LEVEL", "")).startswith("L1"):
        raise ValueError("expected Landsat L1")
    paths = list(root.glob("*_ANG.txt"))
    ang = read_odl(paths[0]) if paths else None
    groups = {}
    for band in bands or range(1, 10):
        band = int(str(band).removeprefix("B"))
        if not 1 <= band <= 9:
            raise ValueError("only OLI reflective bands B1..B9 are supported")
        file = mtl.get(f"FILE_NAME_BAND_{band}")
        if file is None:
            if bands is not None:
                raise ValueError(f"MTL missing band {band}")
            continue
        raw = read_band(root / file, bounds=bounds)
        dn = raw.dn.where(raw.dn != 0).expand_dims(band=[f"B{band}"])
        rho = dn * float(mtl[f"REFLECTANCE_MULT_BAND_{band}"]) + float(mtl[f"REFLECTANCE_ADD_BAND_{band}"])
        rad = dn * float(mtl[f"RADIANCE_MULT_BAND_{band}"]) + float(mtl[f"RADIANCE_ADD_BAND_{band}"])
        chain = [{"kind": "calibration", "actor": "USGS", "processor": "MTL coefficients",
                  "parameters": {k: v for k, v in mtl.items() if k.endswith(f"BAND_{band}")}},
                 {"kind": "geometry_processing", "actor": "USGS", "processor": "Landsat L1",
                  "parameters": {"processing_level": mtl["PROCESSING_LEVEL"]}}]
        conversion = {"solar_spectrum": "provider implicit in MTL radiance/reflectance coefficients; spectrum unstated",
                      "sun_angle_convention": "rho_prime without sun term",
                      "earth_sun_distance_au": mtl.get("EARTH_SUN_DISTANCE"),
                      "irradiance_source": "MTL coefficient ratio"}
        group = measurement_group({
            "counts": describe(dn, basis="published", units="DN", source=str(mtl_path)),
            "toa_reflectance": describe(rho, basis="evaluated_from_provider_model", units="1",
                                        sun_normalisation="rho_prime_without_sun_term", source=str(mtl_path)),
            "radiance": describe(rad, basis="evaluated_from_provider_model", units="W m-2 sr-1 um-1",
                                 source=str(mtl_path), conversion=json.dumps(conversion)),
        }, primary="toa_reflectance", geometry_kind="map_grid", processing_chain=chain,
           provider_level=mtl["PROCESSING_LEVEL"], sensor="OLI", flight_unit=mtl["SPACECRAFT_ID"],
           provider_metadata=json.dumps(mtl), **raw.attrs)
        for name in raw.data_vars:
            if name != "dn":
                group[name] = raw[name]
        if ang is None:
            for name in ("time", "sun_zenith", "sun_azimuth", "view_zenith", "view_azimuth"):
                group[name] = unknown("ANG file absent")
        else:
            h = float(ang[f"BAND{band:02d}_MEAN_HEIGHT"]) if height is None else height
            rows, cols = np.meshgrid(raw.y.values, raw.x.values, indexing="ij")
            angles = evaluate_angles(ang, band, rows, cols, height=h)
            angles = angles.assign_coords(y=raw.y, x=raw.x)
            if height is None:
                assumption = AssumptionRecord(
                    "landsat_band_mean_height", "ANG evaluated at the provider band mean height; "
                    "per-pixel terrain height is not supplied.", AssumptionBasis.OPERATOR_DECLARED,
                    "Per-pixel terrain heights are supplied for ANG evaluation.",
                )
                for name in angles.data_vars:
                    angles[name].attrs["assumptions"] = json.dumps([assumption.to_dict()])
            group.update(angles)
            detector_map = xr.broadcast(angles.detector, angles.time)[0].where(angles.time.notnull())
            group["detector_index"] = describe(detector_map, basis="evaluated_from_provider_model",
                                               units="1", source="ANG L1T to L1R SCA mapping")
            group.attrs["provider_angle_coefficients"] = json.dumps(ang)
            group.attrs["height_basis"] = "provider band mean" if height is None else "caller supplied"
        groups[f"B{band}"] = group
    if spectral_responses is None:
        spectral_responses = {"oli": read_usgs_srf(unit=mtl["SPACECRAFT_ID"])}
    return imagery(groups, spectral_responses=spectral_responses,
                   sensor="OLI", product_id=str(mtl.get("LANDSAT_PRODUCT_ID", root.name)))
