"""Synthetic EPS v11 writer for tests only; no measured data or decoding.

Offsets: EUM.EPS.SYS.SPE.990003 and EUMETSAT MetopDatasets v0.2.3
src/Instruments/IASI/csv_formats/IASI_xxx_1C_V11.csv (MDR size 2728908).
Two MDRs, 120 FOVs each, five scale bands, including negative radiance.
"""
import struct
from pathlib import Path

import numpy as np

FIELDS = """product_name parent_product_name_1 parent_product_name_2 parent_product_name_3 parent_product_name_4
instrument_id instrument_model product_type processing_level spacecraft_id sensing_start sensing_end
sensing_start_theoretical sensing_end_theoretical processing_centre processor_major_version processor_minor_version
format_major_version format_minor_version processing_time_start processing_time_end processing_mode disposition_mode
receiving_ground_station receive_time_start receive_time_end orbit_start orbit_end actual_product_size state_vector_time
semi_major_axis eccentricity inclination perigee_argument right_ascension mean_anomaly x_position y_position z_position
x_velocity y_velocity z_velocity earth_sun_distance_ratio location_tolerance_radial location_tolerance_crosstrack
location_tolerance_alongtrack yaw_error roll_error pitch_error subsat_latitude_start subsat_longitude_start
subsat_latitude_end subsat_longitude_end leap_second leap_second_utc total_records total_mphr total_sphr total_ipr
total_geadr total_giadr total_veadr total_viadr total_mdr count_degraded_inst_mdr count_degraded_proc_mdr
count_degraded_inst_mdr_blocks count_degraded_proc_mdr_blocks duration_of_product milliseconds_of_data_present
milliseconds_of_data_missing subsetted_product""".split()


def header(kind, size, subclass=0, start_ms=0):
    return struct.pack(">BBBBIHIHI", kind, 8, subclass, 1, size, 9000, start_ms, 9000, start_ms + 8000)


def generate(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "synthetic.nat"
    mdr_size, mdr_count = 2728908, 2
    giadr_offset = 3307 + 2 * 27
    mdr_offset = giadr_offset + 84
    size = mdr_offset + mdr_count * mdr_size
    values = dict.fromkeys(FIELDS, "0")
    for name in FIELDS:
        if "time" in name or name.startswith("sensing_") or name == "leap_second_utc":
            values[name] = "20240822000000Z"
    values.update(product_name="IASI_xxx_1C_SYNTHETIC", instrument_id="IASI", product_type="xxx", processing_level="1C",
                  spacecraft_id="M01", format_major_version="11", total_records="6", total_mphr="1", total_ipr="2",
                  total_giadr="1", total_mdr=str(mdr_count), actual_product_size=str(size))
    values["sensing_end"] = "20240822000016Z"
    text = "\n".join(f"{key.upper()}={values[key]}" for key in FIELDS) + "\n"
    # Pad within final value: header reader strips whitespace around values.
    text = text.rstrip("\n").ljust(3286) + "\n"
    assert len(text) == 3287
    content = bytearray(header(1, 3307) + text.encode("ascii"))
    content += header(3, 27) + struct.pack(">BBBI", 5, 8, 1, giadr_offset)
    content += header(3, 27) + struct.pack(">BBBI", 8, 8, 2, mdr_offset)
    content += header(5, 84, 1)
    content += struct.pack(">h10h10h10hh", 5, 2581, 5921, 9009, 9541, 10721, *([0] * 5),
                           5920, 9008, 9540, 10720, 11041, *([0] * 5), 7, 8, 9, 8, 9, *([0] * 5), 6)
    for record in range(mdr_count):
        data = bytearray(mdr_size)
        data[:20] = header(8, mdr_size, 2, start_ms=record * 8000)
        data[20] = record  # one MDR carries an instrument-degradation flag
        def put(offset, a, dtype):
            encoded = np.asarray(a, dtype=dtype).tobytes(order="F")
            data[offset:offset + len(encoded)] = encoded
        for x in range(30):
            data[9122 + 6*x:9128 + 6*x] = struct.pack(">HI", 9000, record*8000 + x*200)
        lonlat = np.zeros((2, 4, 30), dtype=int)
        lonlat[0] = (350 + np.arange(30)[None, :]) * 1000000
        lonlat[1] = (50 + record + np.arange(4)[:, None]) * 1000000
        put(255893, lonlat, ">i4")
        angles = np.zeros((2, 4, 30), dtype=int)
        angles[0], angles[1] = 45000000, 120000000
        put(256853, angles, ">i4")
        angles[0] = np.where(np.arange(30)[None, :] < 15, 60000000, 110000000)
        put(263813, angles, ">i4")
        data[276777:276782] = struct.pack(">bi", 1, 250)  # 25 m-1 = 0.25 cm-1
        data[276782:276790] = struct.pack(">ii", 2581, 11041)
        spectra = np.full((8700, 4, 30), 1000 + record, dtype=int)
        spectra[0] = -100
        spectra[8461:] = 32000  # padding must never escape as valid channels
        put(276790, spectra, ">i2")
        quality = np.zeros((3, 4, 30), dtype=int)
        quality[0, 0, 0] = 1
        put(255260, quality, "u1")
        put(255620, np.full((4, 30), 3), ">u2")
        put(2728548, np.tile(np.arange(30), (4, 1)), "u1")
        put(2728668, np.full((4, 30), 100), "u1")
        put(2728788, np.full((4, 30), 5), "u1")
        # 200 VInteger4 values: exponent byte followed by signed mantissa.
        data[2364790:2365790] = struct.pack(">bi", 3, 123) * 200
        content += data
    assert len(content) == size
    path.write_bytes(content)
    return path
