"""Evaluate USGS ANG rational polynomials separately for each band and SCA.

Equations follow USGS l8_angles 2.7.0 calculate_angles_rpc and find_scas.
No band-4 proxy and no averaging of overlapping SCAs is performed here.
"""

import ast
from datetime import datetime, timedelta, timezone
import re

import numpy as np
import xarray as xr

from spectraccess.core.imagery import describe


def read_odl(path):
    """Read scalar/list ODL metadata. Retain group-qualified duplicate keys."""
    text = path.read_text(encoding="utf-8")
    values = {}
    for match in re.finditer(r"^\s*(\w+)\s*=\s*(\([^)]*\)|[^\r\n]+)", text, re.M):
        key, raw = match.groups()
        if key in {"GROUP", "END_GROUP"}:
            continue
        try:
            value = ast.literal_eval(raw.replace("D+", "E+").replace("D-", "E-"))
        except (ValueError, SyntaxError):
            value = raw.strip().strip('"')
        values[key] = value
    return values


def _rpc(num, den, terms):
    return sum(c * t for c, t in zip(num, terms)) / (
        1 + sum(c * t for c, t in zip(den, terms[1:])))


def evaluate_angles(metadata, band, line, sample, *, height):
    """Return (detector, y, x) descriptors and provider coefficients.

    Coordinates are zero-based full L1T line/sample. Height is explicit in
    metres; use provider mean height only by passing it deliberately.
    Non-contributing SCAs carry NaN, including outside active image bounds.
    """
    p = f"BAND{int(band):02d}"
    line, sample = np.broadcast_arrays(line, sample)
    if line.ndim != 2:
        raise ValueError("line and sample must broadcast to a 2D grid")
    scas = np.atleast_1d(metadata[p + "_SCA_LIST"]).astype(int)
    output = {k: [] for k in ("view_zenith", "view_azimuth", "sun_zenith", "sun_azimuth", "time")}
    for sca_position, sca in enumerate(scas):
        q = p + f"_SCA{sca:02d}"
        ml, ms = metadata[q + "_MEAN_L1T_LINE_SAMP"]
        l, s = line - ml, sample - ms
        h = height - metadata[q + "_MEAN_HEIGHT"]
        terms = [np.ones_like(l), l, s, h, l * s]
        rline, rsamp = [
            _rpc(metadata[q + f"_{axis}_NUM_COEF"], metadata[q + f"_{axis}_DEN_COEF"], terms)
            + metadata[q + "_MEAN_L1R_LINE_SAMP"][i]
            for i, axis in enumerate(("LINE", "SAMP"))
        ]
        valid = ((rsamp >= 0) & (rsamp <= metadata[p + "_NUM_L1R_SAMPS"] - 1)
                 & (rline >= 0) & (rline < metadata[p + "_NUM_L1R_LINES"]))
        ml, ms = metadata[p + "_MEAN_L1T_LINE_SAMP"]
        l, s = line - ml, sample - ms
        rl = rline - metadata[p + "_MEAN_L1R_LINE_SAMP"][0]
        rs = rsamp + sca_position * metadata[p + "_NUM_L1R_SAMPS"] - metadata[p + "_MEAN_L1R_LINE_SAMP"][1]
        h = height - metadata[p + "_MEAN_HEIGHT"]
        terms = [np.ones_like(l), l, s, h, rl, l*l, s*l, s*s, rs*rl*rl, rl*rl*rl]
        for prefix, kind in (("SAT", "view"), ("SUN", "sun")):
            vector = np.stack([
                _rpc(metadata[p + f"_{prefix}_{axis}_NUM_COEF"],
                     metadata[p + f"_{prefix}_{axis}_DEN_COEF"], terms)
                + metadata[p + f"_MEAN_{prefix}_VECTOR"][i]
                for i, axis in enumerate("XYZ")
            ])
            norm = np.linalg.norm(vector, axis=0)
            zenith = np.degrees(np.arccos(np.clip(vector[2] / norm, -1, 1)))
            azimuth = np.degrees(np.arctan2(vector[0], vector[1])) % 360
            output[kind + "_zenith"].append(np.where(valid, zenith, np.nan))
            output[kind + "_azimuth"].append(np.where(valid, azimuth, np.nan))
        epoch = datetime(int(metadata["EPHEMERIS_EPOCH_YEAR"]), 1, 1, tzinfo=timezone.utc)
        epoch += timedelta(days=int(metadata["EPHEMERIS_EPOCH_DAY"]) - 1,
                           seconds=metadata["EPHEMERIS_EPOCH_SECONDS"])
        seconds = epoch.timestamp() + metadata[p + "_START_TIME"] + rline * metadata[p + "_LINE_TIME"]
        output["time"].append(np.where(valid, seconds, np.nan))
    result = xr.Dataset(coords={"detector": scas})
    for name, values in output.items():
        result[name] = describe(xr.DataArray(np.stack(values), dims=("detector", "y", "x")),
                                basis="evaluated_from_provider_model", source="USGS ANG RPC",
                                evaluation_height_m=float(height),
                                units="seconds since 1970-01-01 00:00:00 UTC" if name == "time" else "degree")
    return result
