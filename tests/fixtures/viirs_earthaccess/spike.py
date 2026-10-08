"""Run the pinned Satpy transforms against the synthetic MOD and IMG files."""
from pathlib import Path

import numpy as np
from satpy.readers.viirs_l1b import VIIRSL1BFileHandler


class Key(dict):
    def to_dict(self):
        return dict(self)


def spike():
    root = Path(__file__).resolve().parent
    for grid, bands in (("MOD", ("M09", "M15")), ("IMG", ("I01", "I05"))):
        for band in bands:
            handler = VIIRSL1BFileHandler(str(next(root.glob(f"VNP02{grid}.*.nc"))),
                                         {"platform_shortname": "NP"}, {})
            thermal = band in {"M15", "I05"}
            key = Key(name=band, calibration="brightness_temperature" if thermal else "reflectance")
            result = handler.get_dataset(key, {"name": band, "units": "K" if thermal else "%"}).compute()
            assert result.dims == ("y", "x")
            assert np.isnan(result.values[0, 0])
            expected = np.linspace(200, 350, 65536, dtype="f4")[11] if thermal else 11 * .00002 * 100
            np.testing.assert_allclose(result.values[0, 1], expected, rtol=1e-6)
            print(grid, band, key['calibration'], float(result.values[0, 1]), 'mask/range, packing/LUT, unit normalization, y/x confirmed')
            np.testing.assert_equal(handler.adjust_scaling_factors([.1, 2], 'W cm-2 sr-1', 'W m-2 sr-1'), [1000., 20000.])
        geo = VIIRSL1BFileHandler(str(next(root.glob(f"VNP03{grid}.*.nc"))), {"platform_shortname": "NP"}, {})
        latitude = geo.get_dataset(Key(name="latitude"), {"file_key": "geolocation_data/latitude", "units": "degrees"}).compute()
        assert latitude.shape[1] == 8
        print(grid, '03 geolocation native shape', latitude.shape)


if __name__ == "__main__":
    spike()
