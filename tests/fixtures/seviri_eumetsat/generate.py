"""Generate small SYNTHETIC native files using pinned Satpy record dtypes.

No provider observations or credentials. Pattern: Satpy v0.60.0
satpy/tests/reader_tests/test_seviri_l1b_native.py physical-file builder.
"""
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from satpy.readers.seviri_l1b_native import NativeMSGFileHandler
from satpy.readers.seviri_l1b_native_hdr import get_native_header, native_trailer

START = datetime(2016, 6, 15, 10, tzinfo=timezone.utc)
CHANNELS = ("VIS006", "VIS008", "IR_016", "IR_039", "WV_062", "WV_073",
            "IR_087", "IR_097", "IR_108", "IR_120", "IR_134", "HRV")


def _cds(value):
    seconds = (value - datetime(1958, 1, 1, tzinfo=timezone.utc)).total_seconds()
    return int(seconds // 86400), int(seconds % 86400 * 1000)


def _pack(values):
    """Pack four ten-bit counts into five bytes, MSB first."""
    bits = ((np.asarray(values, dtype=np.uint16)[:, None] >> np.arange(9, -1, -1)) & 1).astype(np.uint8)
    return np.packbits(bits.reshape(-1))


def generate(folder, *, earth_model=2, single_channel=False, stacked_hrv=False):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "synthetic.nat"
    header = np.zeros(1, dtype=get_native_header(True))
    main = header['15_MAIN_PRODUCT_HEADER']
    main['FormatName']['Name'] = b'FormatName                  : '
    main['FormatName']['Value'] = b'NATIVE\n'
    main['QQOV']['Value'] = b'OK'
    sec = header['15_SECONDARY_PRODUCT_HEADER']
    params = dict(SelectedBandIDs='--------X---' if single_channel else 'XXXXXXXXXXXX',
                  SouthLineSelectedRectangle=1855, NorthLineSelectedRectangle=1858,
                  EastColumnSelectedRectangle=1853, WestColumnSelectedRectangle=1860,
                  NumberLinesVISIR=4, NumberColumnsVISIR=8, NumberLinesHRV=12, NumberColumnsHRV=24)
    if stacked_hrv:
        # Full-disc selected rectangle enables the native reader's two HRV
        # windows. Store only four synthetic VIS/IR rows, keeping file small.
        params.update(SouthLineSelectedRectangle=1, NorthLineSelectedRectangle=3712,
                      EastColumnSelectedRectangle=1, WestColumnSelectedRectangle=3712,
                      NumberColumnsVISIR=3712, NumberColumnsHRV=48)
    for name, value in params.items():
        sec[name]['Name'] = name.encode()
        sec[name]['Value'] = str(value).encode()
    data = header['15_DATA_HEADER']
    satellite = data['SatelliteStatus']['SatelliteDefinition']
    satellite['SatelliteId'] = 322
    satellite['NominalLongitude'] = 9.5
    data['GeometricProcessing']['EarthModel'] = (earth_model, 6378.169, 6356.5838, 6356.5838)
    description = data['ImageDescription']
    description['ProjectionDescription']['LongitudeOfSSP'] = 0
    for name, size, step in [('ReferenceGridVIS_IR', 3712, 3.0004031658172607), ('ReferenceGridHRV', 11136, 1.0001343488693237)]:
        description[name]['NumberOfLines'] = size
        description[name]['NumberOfColumns'] = size
        description[name]['LineDirGridStep'] = step
        description[name]['ColumnDirGridStep'] = step
        description[name]['GridOrigin'] = 2
    description['Level15ImageProduction']['PlannedChanProcessing'] = 2
    calibration = data['RadiometricProcessing']['Level15ImageCalibration']
    calibration['CalSlope'] = 2
    calibration['CalOffset'] = -10
    days, msecs = _cds(START)
    planned = data['ImageAcquisition']['PlannedAcquisitionTime']
    planned['TrueRepeatCycleStart'] = (days, msecs, 0, 0)
    planned['PlanForwardScanEnd'] = (days, msecs + 240000, 0, 0)
    planned['PlannedRepeatCycleEnd'] = (days, msecs + 300000, 0, 0)
    orbit = data['SatelliteStatus']['Orbit']['OrbitPolynomial']
    orbit['StartTime'][:, 0] = (days, msecs - 3600000)
    orbit['EndTime'][:, 0] = (days, msecs + 3600000)
    orbit['X'][:, 0, 0] = 84328  # Constant Chebyshev position: 42164 km from Earth's centre.
    header.tofile(path)
    # Obtain record dtype from maintained reader, without opening incomplete file.
    handler = object.__new__(NativeMSGFileHandler)
    available = {channel: (channel == 'IR_108' if single_channel else True) for channel in CHANNELS}
    columns = 3712 if stacked_hrv else 8
    handler.mda = dict(number_of_columns=columns, hrv_number_of_columns=24,
                       available_channels=available, channel_list=[c for c in CHANNELS if available[c]])
    records = np.zeros(4, dtype=handler._get_data_dtype())
    for channel in handler.mda['channel_list']:
        if channel == 'HRV':
            for i in range(3):
                lines = records['hrv'][:, i]
                lines['lineno'] = np.arange(4) * 3 + i + (3 * 1855 - 2)
                lines['chan_id'] = 12
                lines['acq_time']['Days'] = days
                lines['acq_time']['Milliseconds'] = msecs + np.arange(4) * 3000 + i * 1000
                for row in range(4):
                    lines['line_data'][row] = _pack(np.arange(24) + row * 24 + i + 1)
        else:
            lines = records['visir'][:, handler.mda['channel_list'].index(channel)]
            lines['lineno'] = np.arange(1855, 1859)
            lines['chan_id'] = CHANNELS.index(channel) + 1
            lines['acq_time']['Days'] = days
            lines['acq_time']['Milliseconds'] = msecs + np.arange(4) * 3000 + CHANNELS.index(channel) * 10
            lines['line_validity'] = [1, 2, 3, 4]
            lines['line_rquality'] = [0, 1, 2, 3]
            lines['line_gquality'] = [4, 3, 2, 1]
            for row in range(4):
                counts = (np.arange(columns) + row * columns) % 1023 + 1
                if row == 0:
                    counts[-1] = 0
                lines['line_data'][row] = _pack(counts)
    trailer = np.zeros(1, dtype=native_trailer)
    stats = trailer['15TRAILER']['ImageProductionStats']
    stats['ActualScanningSummary']['ReducedScan'] = 1
    stats['ActualScanningSummary']['ForwardScanStart'] = (days, msecs)
    stats['ActualScanningSummary']['ForwardScanEnd'] = (days, msecs + 240000)
    stats['ActualL15CoverageVIS_IR'] = (1855, 1858, 1853, 1860)
    stats['ActualL15CoverageHRV'] = (5563, 5574, 5557, 5580, 0, 0, 0, 0)
    if stacked_hrv:
        stats['ActualL15CoverageHRV'] = (5563, 5568, 5557, 5580, 5569, 5574, 5563, 5586)
    stats['L15ImageValidity']['NonNominalRadiometricQuality'] = np.arange(12) % 2
    with path.open('ab') as stream:
        records.tofile(stream)
        trailer.tofile(stream)
    return path


if __name__ == '__main__':
    import sys
    print(generate(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent))
