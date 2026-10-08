# NASA VIIRS native observations

Install `pip install "spectraccess[viirs]"` on Python 3.12 or newer.
The extra uses earthaccess 0.18.0 and Satpy 0.60.0. Satpy 0.60.0 is
GPLv3+; spectrAccess itself is Apache-2.0. Consider the extra's licence
when distributing software that uses it.

Discovery is public CMR access through earthaccess. Fetch uses the same
shared `CredentialSession` transport as EMIT. Store an Earthdata account
with `spectraccess login earthdata`, or hand over a `Credential`/callable
through `credentials=`. Missing credentials raise `CredentialMissing`.
The connector does not read environment credentials or use `.netrc`.

```python
from datetime import datetime, timezone
from spectraccess.connectors.viirs_earthaccess import VIIRSConnector

connector = VIIRSConnector()
targets = connector.discover(
    bbox=(15.0, -36.0, 15.2, -35.8),
    start=datetime(2024, 5, 1, 0, 0, 1, tzinfo=timezone.utc),
    end=datetime(2024, 5, 1, 0, 5, 59, tzinfo=timezone.utc),
    products=("MOD",), platforms=("SNPP",),
)
raw = connector.fetch(targets[0], dest="./viirs")
measurements = connector.read(raw, bbox=(15.0, -36.0, 15.2, -35.8),
                              bands=("M09", "M15", "M16"))
granules = connector.parse_canonical(raw)
```

`discover` also accepts exact collection short names. `limit` bounds each
collection query. Aliases `MOD` and `IMG` select 02 imagery; fetch adds
the unique 03 file with the same platform, acquisition stamp, image grid
and filename collection token. Their provider platform and time-coverage
attrs must also agree at read. A mismatch fails explicitly.
`CLDMSK` and `CLDPROP` must be requested explicitly. Each cloud target
is fetched and read separately, with its own geolocation and scan time:

```python
cloud_targets = connector.discover(
    bbox=(15.0, -36.0, 15.2, -35.8),
    start=datetime(2024, 5, 1, tzinfo=timezone.utc),
    end=datetime(2024, 5, 2, tzinfo=timezone.utc),
    products=("CLDMSK", "CLDPROP"), platforms=("SNPP",),
)
cloud_raw = connector.fetch(cloud_targets[0], dest="./viirs/cloud")
cloud = connector.read(cloud_raw, bbox=(15.0, -36.0, 15.2, -35.8))
```

Fetch defaults to a 400 MB transfer cap, configurable through `max_bytes`.
It verifies the CMR checksum before publishing each downloaded file.
Provider files are downloaded whole; bbox selection happens during read.
Use separate results for each granule rather than collecting unlike swaths
in one directory for read.

## Published collections

Public CMR collection queries on 2026-10-08 returned the following.
These are CMR versions; filename tokens such as `002`, `021`, `011`
are retained separately and are not interchangeable version strings.

| Platform | Imagery/geolocation short names | CMR version | Cloud mask | Cloud properties |
| --- | --- | --- | --- | --- |
| SNPP | VNP02MOD, VNP03MOD, VNP02IMG, VNP03IMG | 2 | CLDMSK_L2_VIIRS_SNPP, version 2 (also 1) | CLDPROP_L2_VIIRS_SNPP, version 1.1 |
| NOAA20 | VJ102MOD, VJ103MOD, VJ102IMG, VJ103IMG | 2.1 | CLDMSK_L2_VIIRS_NOAA20, version 2 (also 1) | CLDPROP_L2_VIIRS_NOAA20, version 1.1 |
| NOAA21 | VJ202MOD, VJ203MOD, VJ202IMG, VJ203IMG | 2.1 | CLDMSK_L2_VIIRS_NOAA21, version 1 | Absent from CMR at build |

The adapter queries the versions shown, selecting version 2 for SNPP and
NOAA20 cloud masks. NOAA21 cloud-property discovery is empty; no substitute
product is invented. DNB, NOAA SDR and near-real-time products are outside
this connector.

## Native measurements and time

`read` returns an xarray Dataset. A bbox selects the smallest native
row/column rectangle enclosing matching pixel centres. It can contain
centres outside the bbox. A bbox outside the granule returns zero-length
image dimensions and zero scan times. `parse_canonical(..., bbox=...)`
then returns an empty schema-v1 frame. No resampling, bow-tie removal or
solar-zenith correction is performed. Provider bow-tie deletion/overlap
and cloud products' provider processing remain as supplied.

Bands retain names such as `M09`, `M15`, `I01`. Each selected band includes
its encoded `<band>_quality_flags`, `<band>_uncert_index` where present,
and provider brightness-temperature LUT where present. `bands=None`
loads all published I/M bands. Cloud `variables=None` reads all
`geophysical_data` variables. Only `Clear_Sky_Confidence` and
`Cloud_Top_Height` use Satpy's L2 mapping; cloud-top temperature, pressure,
uncertainty, masks and QA are direct provider reads. Encoded flags and QA
bytes retain their values, dtypes, fill codes and meanings.

Solar/view geometry comes from provider `geolocation_data`, including
`solar_zenith`, `solar_azimuth`, `sensor_zenith`, `sensor_azimuth`.
Image dimensions keep provider names. Longitude and latitude are coordinates
on each image variable and solar/view angle field. Non-image dimensions are
namespaced per file/group so unrelated LUTs or byte axes never align, while
variables on a shared published dimension within that group retain that support.
Band annotations follow NASA's observation-data layout: the band name followed
by _quality_flags, _uncert_index, or the thermal _brightness_temperature_lut.
Annotation attrs identify their associated band and role; each band identifies
its quality variable, scan-time variable and row-to-scan index.
L1B scan-quality/state flags from the 02 file retain their scan support and
association with selected bands. Same-named 02 time fields receive an l1b_
prefix so their published epoch can coexist with the 03 scan time.

Band selection remains strict. If a requested band is unavailable, the error
lists missing bands, the opened product/file, group, and all variable names
found in that group. It prints no scientific values. This inventory distinguishes
an absent band from a different layout or a misidentified file.

`scan_start_time` is a numeric coordinate on `number_of_scans`.
`scan_index` links each retained image row to its original provider scan;
MOD uses 16 detector rows per scan, IMG 32. It is scan start time, not
exact time for each detector/pixel. Variable units, long name and
provider leap-second metadata are preserved. Older specs identify TAI93;
newer L1B processing may use TAI58. Neither epoch is relabelled as UTC.
Consumers must use the file's actual epoch and documented leap-second
conversion for UTC joins. Missing scan time fails explicitly; no granule
timestamp is substituted. Granule rows carry provider acquisition coverage
separately.

Provider attributes are stored in `provider_attributes` and
`provider_global_attributes`; `provider_file`/`provider_variable` identify
their origin. `reader`, `reader_version`, calibration and transform attrs
are adapter provenance. Satpy-normalized platform names and sensor labels
are not emitted as provider attrs.

## Satpy transform spike

Synthetic MOD and IMG 02/03 files follow NASA groups and detector counts.
`tests/fixtures/viirs_earthaccess/spike.py` exercises pinned Satpy 0.60.0.

| Satpy operation | Disposition in this connector |
| --- | --- |
| Reflective scale_factor and add_offset | Applied as the provider publishes them; original packing kept in provider_attributes |
| Reflectance multiplied by 100 to percent | Undone by dividing by 100; output units `1` |
| Radiance W cm-2 to W m-2 multiplied by 10000 | This calibration is not requested; reflective bands return scaled reflectance, thermal bands provider LUT BT |
| Thermal encoded value indexed into published BT LUT | Retained and stated; output K; LUT also retained |
| Valid-range masking | Retained as provider range mask; fill/range metadata preserved |
| Removal of scale/offset and flag meanings on physical bands | Original attrs retained separately; flag fields are direct encoded reads |
| Renaming lines/pixels to y/x | Undone; provider image dimensions retained |
| Platform/sensor/unit normalization and orbit/calibration attrs | Provider attrs restored explicitly; adapter provenance identified separately |
| No mapped scan time | Direct-read scan_line_attributes/scan_start_time without UTC decoding |
| L2 range masking and scale/offset on mapped variables | Retained; unmapped variables direct-read with provider metadata |

NASA's L1B file spec states that decoded reflective values are
`rho * cos(solar_zenith)`. They are often called scaled reflectance in
product metadata. This connector returns that published quantity; it does
not divide by the solar-zenith cosine or present it as corrected reflectance.

## Uncertainty and granule rows

NASA publishes `<band>_uncert_index` with valid UI range 0..127 and formula
`percent uncertainty = 1.0 + scale_factor * UI^2`, using the UI field's
band-dependent scale factor. These are encoded indices, not linearly packed
uncertainty and not quality flags. They stay encoded with formula and
source attrs. No sigma or coverage factor is invented.
Cloud properties publish fields such as `Cloud_Top_Temperature_Uncertainty`
and `Cloud_Top_Pressure_Uncertainty` in percent; they are decoded with
their provider packing and retain provider descriptions. No standard
uncertainty interpretation is asserted beyond those descriptions.

`parse_canonical` emits one [observation row](observation-fields.md) per
science granule. It carries provider coverage, CMR footprint, granule QA,
processing/collection version, identity and uncertainty definitions.
Scalar value/uncertainty stay null with `unc_status="unknown"`.
No cloud fraction, confidence-to-sigma conversion or inferred quality
verdict is added. Local files without a target have no invented CMR
footprint or collection version.

## Live verification

`python scripts/live_smoke.py viirs_earthaccess` hands over
`EARTHDATA_USERNAME`/`EARTHDATA_PASSWORD`; it SKIPs when absent.
The pinned pair is `G2962669872-LAADS` / `G2962643279-LAADS`:
`VNP02MOD.A2024122.0000.002.2024122092548.nc` and
`VNP03MOD.A2024122.0000.002.2024122090017.nc` (120,157,355 bytes total).
It prints discover/fetch/read stages and checks real bands, flags,
scan time, geometry and a canonical row. Authenticated live verification
runs in the maintainer's private workflow before merge.

## Provider references

- [NASA LAADS VNP02MOD file specification](https://ladsweb.modaps.eosdis.nasa.gov/filespec/VIIRS/1/VNP02MOD.fs)
- [NASA VIIRS L1B User Guide, August 2021](https://ladsweb.modaps.eosdis.nasa.gov/api/v2/content/archives/Document%20Archive/Science%20Data%20Product%20Documentation/NASA_VIIRS_L1B_UG_August_2021.pdf), section 5.1 and Tables 8/9
- [NASA LAADS VNP03MOD file specification](https://ladsweb.modaps.eosdis.nasa.gov/filespec/VIIRS/1/VNP03MOD.fs)
- [NASA cloud-mask file specification](https://ladsweb.modaps.eosdis.nasa.gov/filespec/VIIRS/1/CLDMSK_L2_VIIRS_SNPP)
- [NASA cloud-property file specification](https://ladsweb.modaps.eosdis.nasa.gov/filespec/VIIRS/1/CLDPROP_L2_VIIRS_SNPP)
- [Satpy 0.60.0 VIIRS L1B reader](https://github.com/pytroll/satpy/blob/v0.60.0/satpy/readers/viirs_l1b.py)
- [Satpy 0.60.0 VIIRS L2 mapping](https://github.com/pytroll/satpy/blob/v0.60.0/satpy/etc/readers/viirs_l2.yaml)
- [Data terms and attribution](../src/spectraccess/connectors/viirs_earthaccess/DATA_TERMS.md)
