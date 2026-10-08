"""Generate tiny artificial SL_1_RBT arrays following ESA PDFS 2.12 layout.

Run with Python and spectraccess[slstr]. No downloaded measurements included.
"""
from pathlib import Path
import json
import numpy as np
import xarray as xr

ROOT = Path(__file__).resolve().parent
TITLE = "S3A_SL_1_RBT____20240501T100000_20240501T100300_20240501T120000_0179_111_222_3333_ESA_R_NT_005.SEN3"
ATTRS = {"start_time": "2024-05-01T10:00:00.000000Z", "stop_time": "2024-05-01T10:03:00.000000Z"}


def generate(root=ROOT):
    path = root / TITLE
    path.mkdir(parents=True, exist_ok=True)
    def write(name, variables, attrs=ATTRS):
        xr.Dataset(variables, attrs=attrs).to_netcdf(path / name, engine="h5netcdf")
    cal = {}
    time = {}
    for c in range(1, 7):
        cal[f"S{c}_solar_irradiances"] = (("detectors", "views"), np.full((2, 2), 100.), {"units": "W m-2 um-1"})
    write("viscal.nc", cal)
    for view in "no":
        for stripe in "abif":
            grid = stripe + view
            shape = (4, 6) if stripe in "ab" else (3, 4)
            dims = ("rows", "columns")
            lat = np.broadcast_to(np.linspace(50, 52, shape[0])[:, None], shape)
            lon = np.broadcast_to(np.linspace(3, 6, shape[1])[None, :], shape)
            write(f"geodetic_{grid}.nc", {f"latitude_{grid}": (dims, lat, {"units": "degrees_north"}),
                                        f"longitude_{grid}": (dims, lon, {"units": "degrees_east"})})
            write(f"indices_{grid}.nc", {f"detector_{grid}": (dims, np.zeros(shape, dtype="int16"))})
            write(f"flags_{grid}.nc", {f"cloud_{grid}": (dims, np.arange(np.prod(shape), dtype="uint16").reshape(shape),
                {"flag_masks": np.array([1, 2], dtype="uint16"), "flag_meanings": "visible thermal", "_FillValue": np.uint16(65535)}),
                f"confidence_{grid}": (dims, np.full(shape, 65535, dtype="uint16"))})
            # Use independent dimension names in the multi-grid time file.
            epoch = int((np.datetime64("2024-05-01T10:00:00", "us") - np.datetime64("2000-01-01", "us")) / np.timedelta64(1, "us"))
            time[f"time_stamp_{stripe}"] = ((f"rows_{stripe}",), np.arange(shape[0], dtype="int64") * 1000000 + epoch,
                {"units": "microseconds since 2000-01-01 00:00:00", "calendar": "gregorian"})
            channels = [f"S{i}" for i in range(1, 7)] if stripe in "ab" else (["S7", "S8", "S9", "F1", "F2"] if stripe == "i" else ["F1"])
            for channel in channels:
                kind = "radiance" if stripe in "ab" else "BT"
                name = f"{channel}_{kind}_{grid}"
                values = np.full(shape, 40, dtype="int16")
                variables = {name: (dims, values, {"units": "W m-2 um-1 sr-1" if kind == "radiance" else "K",
                    "scale_factor": 0.5, "add_offset": 1., "_FillValue": np.int16(-32768)})}
                if kind == "radiance":
                    variables[f"{channel}_radiance_err{grid}"] = (dims, np.full(shape, 0.25), {"long_name": "provider radiance error estimate", "units": "W m-2 um-1 sr-1"})
                write(name + ".nc", variables)
        tie = ("rows", "columns")
        write(f"geometry_t{view}.nc", {f"{name}_t{view}": (tie, np.full((2, 2), 30.), {"units": "degrees"})
              for name in ("solar_zenith", "solar_azimuth", "sat_zenith", "sat_azimuth")})
    # Provider time files have one row dimension per grid, separated here.
    for stripe in "abi":
        value = time[f"time_stamp_{stripe}"]
        write(f"time_{stripe}n.nc", {f"time_stamp_{stripe}": (("rows",), value[1], value[2]),
              f"Nadir_Minimal_ts_{stripe}n": (("rows",), value[1] - 200000, value[2]),
              f"Oblique_Minimal_ts_{stripe}o": (("rows",), value[1] - 500000, value[2])})
    write("S8_quality_in.nc", {"S8_radiometric_uncertainty": (("temperature",), [0.1, 0.2], {"long_name": "radiometric uncertainty lookup table", "units": "K"})})
    files = sorted(p.name for p in path.glob("*.nc"))
    xml = '<XFDU><metadata><startTime>2024-05-01T10:00:00Z</startTime><stopTime>2024-05-01T10:03:00Z</stopTime><software version="synthetic-1"/><qualityInformation status="PASSED"/></metadata><dataObjectSection>'
    xml += ''.join(f'<fileLocation href="./{name}"/>' for name in files) + '</dataObjectSection></XFDU>'
    (path / "xfdumanifest.xml").write_text(xml, encoding="utf-8")
    feature = {"Id": "synthetic-slstr", "Name": TITLE, "ContentDate": {"Start": "2024-05-01T10:00:00Z", "End": "2024-05-01T10:03:00Z"},
        "GeoFootprint": {"type": "Polygon", "coordinates": [[[3, 50], [6, 50], [6, 52], [3, 52], [3, 50]]]},
        "Attributes": [{"Name": "productType", "Value": "SL_1_RBT___"}, {"Name": "baselineCollection", "Value": "005"}]}
    (root / "feature.json").write_text(json.dumps(feature, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    print(generate())
