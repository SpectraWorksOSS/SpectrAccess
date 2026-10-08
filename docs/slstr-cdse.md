# Sentinel-3 SLSTR Level-1

Install `pip install "spectraccess[slstr]"` on Python 3.11 or later. This
extra pins Satpy 0.60.0, which is licensed GPLv3 or later. spectrAccess itself
is Apache-2.0. CDSETool supplies discovery and filtered downloads.

```python
from datetime import datetime, timezone
from spectraccess.connectors.slstr_cdse import SLSTRConnector

connector = SLSTRConnector()  # CDSE credentials from the OS keyring
bbox = (138.0, -39.0, 138.2, -38.8)
targets = connector.discover(
    bbox=bbox, start=datetime(2024, 5, 1, tzinfo=timezone.utc),
    end=datetime(2024, 5, 2, tzinfo=timezone.utc),
)
result = connector.fetch(targets[0], dest="data/slstr",
                         channels=("S4", "S7", "S8", "S9"), views=("nadir",))
granules = connector.parse_canonical(result, bbox=bbox)
pixels = connector.read(result, bbox=bbox,
                        channels=("S4", "S7", "S8", "S9"), views=("nadir",))
```

Use `spectraccess login cdse` or hand over a `Credential` source through
`credentials=`. Connectors never read login environment variables.
See [credentials](credentials.md) and [observation fields](observation-fields.md).

Only `SL_1_RBT___` is supported. Default channels are S1-S9 and F1-F2,
both nadir and oblique. Files are selected from the provider manifest,
including available stripes, flags, geodetic arrays, detector indices,
time files, solar irradiance calibration, tie-point angles and channel
quality/uncertainty files. An absent requested channel is an error.

`parse_canonical` returns one schema-v1 row for a granule, with provider
acquisition interval, footprint, processing and collection versions, and
manifest QA. `value` and scalar uncertainty are null: a granule row does
not represent a pixel measurement or computed average. Published array/table
uncertainty descriptions are recorded in `unc_definition`; no coverage factor
or granule sigma is inferred.

`read` returns provider-named radiance and brightness-temperature variables.
S4 is provider radiance, including its 1375 nm cirrus channel. Reflectance is
not published by this reader as a provider measurement: Satpy calculates it
from radiance and irradiance. Native 500 m stripes and 1 km thermal/fire
grids have separate `rows_<grid>` and `columns_<grid>` dimensions. A bbox
selects the smallest rectangular native window containing matching pixel
centres, so its corners can include pixels outside the bbox. An outside
bbox returns zero-sized measurement arrays and an empty canonical frame.
Provider tie-point solar and satellite angles share `rows_tx/columns_tx`
with `latitude_tx/longitude_tx` coordinates from `geodetic_tx.nc`. The
tie-point and measurement Cartesian arrays are also returned. Tie points
retain full granule extent, even when measurements are clipped; consumers
explicitly choose how to map those angles to pixels. No interpolation occurs.
Only native image rows/columns share dimensions across files. Other provider
dimensions, such as detector and uncertainty-table indices, are namespaced
by source asset to retain different table lengths without xarray alignment.
Each variable's `provider_dimensions` attr maps output dimensions back to
the original provider names.

F1 measurements use `fn/fo` while their published quality and scan annotations
use `in/io`. Fire-only selections include those annotation files and preserve
their provider support; uncertainty tables are not converted to pixel sigma.

`time_stamp_<grid>` coordinates decode the provider microsecond epoch
(2000-01-01 UTC), one timestamp per native row. The source fields
`time_stamp_a/b/i` describe sub-satellite row crossings common to both views,
not exact pixel acquisition times. Per-view minimal/maximal scan timestamps,
scan indices and pixel timing parameters remain available for consumers
requiring precise pixel times. Coordinate aliases identify the source variable
and timing support in attrs. Missing row time is an error;
granule time never replaces it. Cloud/confidence/pointing/Bayes and quality
fields keep encoded flag values, including fill codes, with provider masks
and meanings. Radiance errors remain arrays; IR uncertainty tables retain
table dimensions and descriptions. Neither becomes a cloud verdict or sigma.

## Executed synthetic Satpy spike

`tests/test_slstr_cdse.py::test_satpy_spike` runs the pinned real
`NCSLSTR1B` file handler on generated PDFS-layout arrays, across S1-S9
and both views. Other tests run F1/F2 through the same reader.

| Satpy default operation | Observed result | Connector disposition |
| --- | --- | --- |
| CF scale/offset and fill decoding | packed 40, scale 0.5, offset 1 yields 21 | Retained; provider packing metadata accompanies output |
| S1-S6 radiance adjustment | nadir factors 0.97, 0.98, 0.98, 1, 1.11, 1.13; oblique 0.94, 0.95, 0.95, 1, 1.04, 1.07 | Disabled with explicit unity factors for every channel/view; avoids double correction on Collection 005 |
| Reflectance calculation | adjusted radiance / detector irradiance times pi times 100 | Disabled by requesting radiance; no computed reflectance presented as provider data |
| Thermal adjustment and BT selection | S7-S9 factors 1; published BT selected | Explicit brightness-temperature request, unity override also covers F1/F2 |
| Normalized platform, sensor, view and calibration attrs | `S3A` becomes `Sentinel-3A` | Only source variable attrs retained; reader provenance identified separately |
| Tie-point angle interpolation, including fill-to-zero | Reader source review, not exercised by measurement spike | Bypassed; geometry direct-read on published tie-point grid |
| Flag CF fill masking | Reader source review | Bypassed with `mask_and_scale=False`; flags never changed |

The fixture generator is `tests/fixtures/slstr_cdse/generate.py`. It contains
artificial values, with different visible/thermal shapes, flags, time and
uncertainty metadata. No real observations are committed.

Maintainers run `python scripts/live_smoke.py slstr_cdse` in the private
login-enabled workflow. Pinned product ID:
`a18067ba-e29f-43ff-b16d-f09b81df9e98`, acquired 2024-05-01 00:12 UTC.
The smoke performs real discovery, filtered fetch, canonical parse and read;
each stage prints a progress line before it starts.
missing logins produce an explicit SKIP. A SKIP is not live verification.

Provider reference: [ESA SLSTR Level-1 Product Data Format Specification](https://sentinels.copernicus.eu/documents/d/sentinel/sentinel-3-product-data-format-specification-slstr-level-1-products).
See [data terms](../src/spectraccess/connectors/slstr_cdse/DATA_TERMS.md).
