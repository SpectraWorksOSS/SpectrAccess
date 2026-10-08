"""Small artificial files shaped from EUMETSAT FCI L1c PUG Appendices A.2/A.3.

Run with the fci extra installed. No downloaded observation is used.
"""
from pathlib import Path

import h5netcdf
import hdf5plugin
import numpy as np

PROJECTION = {"grid_mapping_name": "geostationary", "semi_major_axis": 6378137.,
              "inverse_flattening": 298.257223563, "perspective_point_height": 35786400.,
              "longitude_of_projection_origin": 0., "sweep_angle_axis": "y"}
CHANNELS = ("vis_04", "vis_05", "vis_06", "vis_08", "vis_09", "nir_13", "nir_16", "nir_22",
            "ir_38", "wv_63", "wv_73", "ir_87", "ir_97", "ir_105", "ir_123", "ir_133")
GRID_STEP_2KM = 5.58871526031607e-5
PREFIX = "W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--CHK-"


def filename(kind, count):
    return (f"{PREFIX}{kind}--DIS-NC4E_C_EUMT_20240501101000_IDPF_OPE_"
            f"20240501100000_20240501100930_N__C_0061_{count:04d}.nc")


def scalar(group, name, value, attrs=None):
    var = group.create_variable(name, (), dtype=np.asarray(value).dtype)
    var[()] = value
    var.attrs.update(attrs or {})
    return var


def generate(dest=None):
    dest = Path(dest) if dest is not None else Path(__file__).resolve().parent
    dest.mkdir(parents=True, exist_ok=True)
    for count in (1, 3):
        with h5netcdf.File(dest / filename("BODY", count), "w") as nc:
            nc.attrs.update(platform="MTI1", processor_version="synthetic-1", release_version="synthetic-2")
            nc.dimensions = {"index": 8}
            index = nc.create_variable("index", ("index",), dtype="u2")
            index[:] = np.arange(100, 108)
            time = nc.create_variable("time", ("index",), dtype="f8")
            time[:] = np.arange(8) + count * 100
            time.attrs.update(units="seconds since 2000-01-01 00:00:00", calendar="proleptic_gregorian")
            data = nc.create_group("data")
            scalar(data, "mtg_geos_projection", 0, PROJECTION)
            scalar(data, "swath_number", 1)
            scalar(data, "swath_direction", 1)
            state = nc.create_group("state")
            platform = state.create_group("platform")
            celestial = state.create_group("celestial")
            for name, value in [("platform_altitude", 35786400.), ("subsatellite_latitude", 0.),
                                ("subsatellite_longitude", 0.)]:
                var = platform.create_variable(name, ("index",), dtype="f8"); var[:] = value
            for name, value in [("earth_sun_distance", 149597870.7), ("subsolar_latitude", 0.),
                                ("subsolar_longitude", 0.), ("sun_satellite_distance", 149597870.7)]:
                var = celestial.create_variable(name, ("index",), dtype="f8"); var[:] = value
            for channel in CHANNELS:
                group = data.create_group(channel).create_group("measured")
                n = 8 if channel.startswith(("vis", "nir")) else 4
                group.dimensions = {"y": n, "x": n, "ancillary_size": count + 1}
                # Native orientation: west-positive x, south-to-north y.
                for axis, values in [("x", np.arange(n)), ("y", np.arange(n))]:
                    var = group.create_variable(axis, (axis,), dtype="u2"); var[:] = values
                    scale = GRID_STEP_2KM * 4 / n
                    var.attrs.update(scale_factor=-scale if axis == "x" else scale,
                                     add_offset=GRID_STEP_2KM * 2 if axis == "x" else
                                         (-GRID_STEP_2KM * 2 + (count - 1) * GRID_STEP_2KM * 4), units="rad")
                scalar(group, "start_position_row", 1 + (count - 1) * n)
                scalar(group, "end_position_row", count * n)
                scalar(group, "start_position_column", 1)
                scalar(group, "end_position_column", n)
                # FCI L1c Data Guide: lossless JPEG-LS via FCIDECOMP, filter 32018.
                rad = group.create_variable("effective_radiance", ("y", "x"), dtype="u2", fillvalue=65535,
                                            compression=hdf5plugin.FciDecomp(), chunks=(n, n))
                rad[:] = np.arange(n * n).reshape(n, n) % 8 + 1
                rad.attrs.update(scale_factor=5., add_offset=-10., warm_scale_factor=2., warm_add_offset=-300.,
                                 units="mW.m-2.sr-1.(cm-1)-1", valid_range=np.array([0, 8191 if channel == "ir_38" else 4095], dtype="u2"),
                                 ancillary_variables="pixel_quality", long_name="Effective radiance")
                if channel == "ir_38":
                    rad[-1, -1] = 5000
                rad[-1, 0] = 65535
                quality = group.create_variable("pixel_quality", ("y", "x"), dtype="u1", fillvalue=255)
                quality[:] = np.arange(n * n).reshape(n, n) % 8
                quality[0, 0] = 255
                quality.attrs.update(flag_masks=np.array([1, 2, 4], dtype="u1"),
                                     flag_meanings="missing_warning radiometric_warning noise_warning")
                imap = group.create_variable("index_map", ("y", "x"), dtype="u2", fillvalue=65535)
                imap[:] = 100 + np.arange(n * n).reshape(n, n) % 8
                imap[0, 0] = 65535
                scalar(group, "radiance_unit_conversion_coefficient", 1234.56)
                scalar(group, "channel_effective_solar_irradiance", 50.)
                for name, value in [("wavenumber", 955.), ("a", 1.), ("b", .4)]:
                    scalar(group, "radiance_to_bt_conversion_coefficient_" + name, value)
                scalar(group, "radiance_to_bt_conversion_constant_c1", 1.191042e-5)
                scalar(group, "radiance_to_bt_conversion_constant_c2", 1.4387752)
    with h5netcdf.File(dest / filename("TRAIL", 41), "w") as nc:
        nc.attrs.update(platform="MTI1", processor_version="synthetic-1", release_version="synthetic-2")
        data = nc.create_group("data")
        for channel in CHANNELS:
            root = data.create_group(channel)
            measured = root.create_group("measured")
            n = 3 if channel == "nir_13" else 5
            measured.dimensions = {"number_of_radiometric_noise_lut_steps": n}
            for name in ("radiometric_noise_lut_radiance", "radiometric_noise_lut_noise"):
                var = measured.create_variable(name, ("number_of_radiometric_noise_lut_steps",), dtype="u2")
                var[:] = np.arange(n)
                var.attrs.update(units="mW.m-2.sr-1.(cm-1)-1", scale_factor=.1, add_offset=0.,
                                 long_name="Radiometric noise model LUT" if name.endswith("noise") else "Radiance LUT abscissa")
            quality = root.create_group("quality_channel")
            scalar(quality, "radiometric_noise_compliance", 1, {"long_name": "Set True when compliant with radiometric noise requirement"})
    # A second source table exercises file-scoped dimensions independently
    # of channel-scoped dimensions in the normal repeat-cycle trailer.
    with h5netcdf.File(dest / "synthetic_other_noise.nc", "w") as nc:
        group = nc.create_group("data").create_group("ir_105").create_group("measured")
        group.dimensions = {"number_of_radiometric_noise_lut_steps": 7}
        var = group.create_variable("radiometric_noise_lut_noise", ("number_of_radiometric_noise_lut_steps",), dtype="u2")
        var[:] = np.arange(7)
        var.attrs.update(units="mW.m-2.sr-1.(cm-1)-1", scale_factor=.1, add_offset=0.)
    # L2 CF image, quality including fill code, and ancillary dimension with
    # a name shared by other source files but different size.
    for kind, variable in [("CLM", "cloud_state"), ("CT", "cloud_type"), ("CTTH", "cloud_top_height")]:
        with h5netcdf.File(dest / f"synthetic_{kind}.nc", "w") as nc:
            nc.attrs.update(platform="MTI1", spacecraft="MTI1", data_source="FCI", processor_version="synthetic-3")
            nc.dimensions = {"number_of_rows": 4, "number_of_columns": 4, "maximum_number_of_layers": 2}
            scalar(nc, "mtg_geos_projection", 0, PROJECTION)
            for axis, dim in [("x", "number_of_columns"), ("y", "number_of_rows")]:
                var = nc.create_variable(axis, (dim,), dtype="f8")
                var[:] = GRID_STEP_2KM * (np.array([2., 1., 0., -1.]) if axis == "x" else np.array([-2., -1., 0., 1.]))
                var.attrs["units"] = "rad"
            scalar(nc, "product_quality", 2)
            scalar(nc, "product_completeness", 100)
            if kind == "CLM":
                enum = nc.create_enumtype("u1", "cloud_state_type", {
                    "not_processed": 0, "cloud_free": 1, "cloud_contaminated": 2, "cloud_filled": 3, "missing": 255})
                var = nc.create_variable(variable, ("number_of_rows", "number_of_columns"), dtype=enum, fillvalue=255)
                var[:] = np.arange(16).reshape(4, 4) % 4; var[0, 0] = 255
            else:
                var = nc.create_variable(variable, ("number_of_rows", "number_of_columns"), dtype="i2", fillvalue=-32767)
                var[:] = np.arange(16).reshape(4, 4); var[0, 0] = -32767
            if kind == "CT":
                var.attrs.update(flag_values=np.array([0, 1, 2], dtype="i2"), flag_meanings="clear cloudy uncertain")
            elif kind == "CTTH":
                var.attrs.update(scale_factor=100., add_offset=10., unit="m")
                temp = nc.create_variable("cloud_top_temperature", ("number_of_rows", "number_of_columns"), dtype="i2", fillvalue=-32767)
                temp[:] = 200
                temp.attrs.update(scale_factor=.1, add_offset=200., unit="K")
            flags = nc.create_variable("quality_flag", ("number_of_rows", "number_of_columns"), dtype="i1", fillvalue=-127)
            flags[:] = 3; flags[0, 0] = -127
            flags.attrs.update(flag_values=np.array([0, 3], dtype="i1"), flag_meanings="good questionable")
    return dest


if __name__ == "__main__":
    generate()
