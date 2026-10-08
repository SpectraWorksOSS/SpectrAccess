"""Small synthetic NASA netCDF4 layouts; no downloaded science data.

Layout/uncertainty definition: LAADS VNP02MOD.fs and VNP03MOD.fs.
Run with the viirs extra installed. Outputs are deterministic.
"""
from pathlib import Path

import netCDF4
import numpy as np

ROOT = Path(__file__).resolve().parent


def generate(root=ROOT, *, missing_time=False, night=False):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for grid, detectors, bands in (("MOD", 16, ("M09", "M15", "M16")), ("IMG", 32, ("I01", "I05"))):
        if night and grid == 'IMG':
            continue
        if night and grid == 'MOD':
            # Inventory observed in a NASA night-mode VNP02MOD granule.
            bands = ('M07', 'M08', 'M10', 'M11', 'M12', 'M13', 'M14', 'M15', 'M16')
        for level in ("02", "03"):
            path = root / f"VNP{level}{grid}.A2024122.0000.002.2024122090000.nc"
            with netCDF4.Dataset(path, "w") as ds:
                ds.setncatts(dict(platform="Suomi-NPP", instrument="VIIRS", orbit_number=1,
                    startDirection="Ascending", end_direction="Ascending", endDirection="Ascending",
                    ShortName=f"VNP{level}{grid}", DayNightFlag="Night" if night else "Day", time_coverage_start="2024-05-01T00:00:00.000Z",
                    time_coverage_end="2024-05-01T00:06:00.000Z", processing_version="3.0.30"))
                ds.createDimension("number_of_lines", detectors * 3)
                ds.createDimension("number_of_pixels", 8)
                ds.createDimension("number_of_scans", 3)
                # Same name, unlike lengths in different files: must never align.
                ds.createDimension("lookup", 65536 if level == "02" else 7)
                scan = ds.createGroup("scan_line_attributes")
                times = scan.createVariable("unavailable" if missing_time else "scan_start_time", "f8", ("number_of_scans",))
                times.units = "seconds"
                times.long_name = "Scan start time (TAI58)" if level == "02" else "Scan start time (TAI93)"
                times[:] = np.array([988761610., 988761611.8, 988761613.6]) + (1104537600. if level == "02" else 0.)
                if level == "02":
                    scan.createVariable("scan_quality_flags", "u1", ("number_of_scans",))[:] = [0, 1, 2]
                    scan.createVariable("scan_state_flags", "u1", ("number_of_scans",))[:] = [0, 1, 4]
                else:
                    scan.createVariable("scan_quality", "i2", ("number_of_scans",))[:] = [0, 1, 2]
                group = ds.createGroup("observation_data" if level == "02" else "geolocation_data")
                dims = ("number_of_lines", "number_of_pixels")
                if level == "02":
                    for band in bands:
                        thermal = band in {"M12", "M13", "M14", "M15", "M16", "I05"}
                        val = group.createVariable(band, "u2", dims, fill_value=65535)
                        val.setncatts(dict(valid_min=np.uint16(0), valid_max=np.uint16(65527),
                            scale_factor=np.float32(.01 if thermal else .00002), add_offset=np.float32(0),
                            long_name=band, units="Watts/meter^2/steradian/micrometer" if thermal else "none",
                            flag_values=np.array([65532, 65533, 65534], dtype='u2'),
                            flag_meanings="Missing_EV Bowtie_Deleted Cal_Fail"))
                        if not thermal:
                            # NASA removed RSB units after L1 processing v1.1;
                            # radiance units/scaling are separate attributes.
                            val.delncattr('units')
                            val.radiance_units = "Watts/meter^2/steradian/micrometer"
                            val.radiance_scale_factor = np.float32(.0023088641)
                            val.radiance_add_offset = np.float32(0)
                        val.set_auto_maskandscale(False)
                        encoded = np.arange(detectors * 3 * 8, dtype="u2").reshape(detectors * 3, 8) + 10
                        encoded[0, 0] = 65535
                        val[:] = encoded
                        flag = group.createVariable(band + "_quality_flags", "u2", dims, fill_value=65535)
                        flag.setncatts(dict(flag_masks=np.array([1, 2, 4], dtype="u2"), flag_meanings="bad bowtie saturation"))
                        flag.set_auto_maskandscale(False)
                        flag[:] = np.arange(detectors * 3 * 8, dtype="u2").reshape(detectors * 3, 8)
                        ui = group.createVariable(band + "_uncert_index", "i1", dims, fill_value=-1)
                        ui.scale_factor = np.float32(.006138)
                        ui.units = "percent"
                        ui.conversion = "1.0 + scale*index^2"
                        ui.long_name = "Uncertainty index; percent uncertainty = 1 + scale_factor * UI^2"
                        ui.valid_min, ui.valid_max = np.int8(0), np.int8(127)
                        ui.set_auto_maskandscale(False)
                        ui[:] = 3
                        if thermal:
                            lut = group.createVariable(band + "_brightness_temperature_lut", "f4", ("lookup",))
                            lut.valid_min, lut.valid_max, lut.units = np.float32(100), np.float32(400), "K"
                            lut[:] = np.linspace(200, 350, 65536, dtype="f4")
                            lut[-1] = -999.8  # Provider LUT entries for invalid encoded values.
                else:
                    for name, array in dict(latitude=np.broadcast_to(np.linspace(50, 53, detectors * 3)[:, None], (detectors * 3, 8)),
                                            longitude=np.broadcast_to(np.arange(8)[None, :], (detectors * 3, 8)),
                                            solar_zenith=np.full((detectors * 3, 8), 30),
                                            solar_azimuth=np.full((detectors * 3, 8), 120),
                                            sensor_zenith=np.full((detectors * 3, 8), 10),
                                            sensor_azimuth=np.full((detectors * 3, 8), 40)).items():
                        var = group.createVariable(name, "f4", dims, fill_value=-999.9)
                        var.units = "degrees"
                        var[:] = array
                    # Additional synthetic collision control, not a claim that
                    # NASA publishes a field called navigation_table.
                    group.createVariable("navigation_table", "f4", ("lookup",))[:] = np.arange(7)
    for product, version in (() if night else (("CLDMSK_L2_VIIRS_SNPP", "002"), ("CLDPROP_L2_VIIRS_SNPP", "011"))):
        path = root / f"{product}.A2024122.0000.{version}.2024122090000.nc"
        with netCDF4.Dataset(path, 'w') as ds:
            ds.setncatts(dict(platform='Suomi-NPP', instrument='VIIRS', orbit_number=1,
                             time_coverage_start='2024-05-01T00:00:00.000Z',
                             time_coverage_end='2024-05-01T00:06:00.000Z'))
            for name, size in [('number_of_lines', 48), ('number_of_pixels', 8), ('number_of_scans', 3), ('byte_segment', 6), ('QA_dimension', 10)]:
                ds.createDimension(name, size)
            geo = ds.createGroup('geolocation_data')
            dims = ('number_of_lines', 'number_of_pixels')
            for name, data in dict(latitude=np.broadcast_to(np.linspace(50, 53, 48)[:, None], (48, 8)),
                                   longitude=np.broadcast_to(np.arange(8)[None, :], (48, 8)),
                                   solar_zenith=np.full((48, 8), 30)).items():
                var = geo.createVariable(name, 'f4', dims)
                var.units = 'degrees'
                var[:] = data
            times = ds.createGroup('scan_line_attributes').createVariable('scan_start_time', 'f8', ('number_of_scans',))
            times.units, times.long_name = 'seconds', 'Scan start time (TAI93)'
            times[:] = [988761610., 988761611.8, 988761613.6]
            science = ds.createGroup('geophysical_data')
            if product.startswith('CLDMSK'):
                confidence = science.createVariable('Clear_Sky_Confidence', 'f4', dims, fill_value=-999.)
                confidence.units, confidence.valid_min, confidence.valid_max = '1', np.float32(0), np.float32(1)
                confidence[:] = .75
                science.createVariable('Cloud_Mask', 'u1', ('byte_segment', *dims))[:] = 255
            else:
                science.createVariable('Cloud_Top_Height', 'i2', dims)[:] = 10000
                science['Cloud_Top_Height'].units = 'm'
                for name, offset, scale, units in [('Cloud_Top_Temperature', 100., .007935, 'K'),
                                                 ('Cloud_Top_Temperature_Uncertainty', 0., .01, 'percent')]:
                    var = science.createVariable(name, 'i2', dims, fill_value=-9999)
                    var.units, var.long_name = units, name.replace('_', ' ')
                    var.scale_factor, var.add_offset = scale, offset
                    var.set_auto_maskandscale(False)
                    var[:] = 100
            qa = science.createVariable('Quality_Assurance', 'u1', (*dims, 'QA_dimension'), fill_value=255)
            qa.set_auto_maskandscale(False)
            qa[:] = 255
    return root


if __name__ == "__main__":
    generate()
    generate(ROOT / 'night', night=True)
