# MSG SEVIRI Level 1.5 from EUMETSAT

Install `pip install "spectraccess[seviri]"` on Python 3.11 or later.
Satpy 0.60.0 requires Python >=3.11; the base package retains Python >=3.10.
The extra contains `satpy==0.60.0` and `eumdac>=3.1,<4` with their dependencies.
Native files need no HDF5 plugin or additional NetCDF backend. The seviri extra
is subject to Satpy's GPLv3 or later licence; EUMDAC is MIT. spectrAccess
code remains Apache-2.0. Observation-data terms are separate and, for these two
MSG collections, not yet confirmed; see
[data terms](../src/spectraccess/connectors/seviri_eumetsat/DATA_TERMS.md)
before sharing any output.

Run `spectraccess login eumetsat`, entering the consumer key as account and
consumer secret as password. Alternatively pass a credential source:
`SEVIRIConnector(credentials=lambda: Credential("password", secret, key))`.
Credential environment variables and EUMDAC login files are not read.
Missing credentials raise `CredentialMissing`; rejected token authentication
raises `CredentialRejected`. Authenticated 401/403 collection failures raise
`SEVIRIAuthorizationError`; other failures raise `SEVIRIProviderError`.
Errors retain HTTP status, stage and a bounded provider message while redacting
credentials and bearer tokens from logs and chained exceptions. Licence changes
may require a fresh Data Store login and propagation time; see the
[EUMETSAT Data Store FAQ](https://user.eumetsat.int/resources/user-guides/frequently-asked-questions-for-data-store).

```python
from datetime import datetime, timezone
from spectraccess.connectors.seviri_eumetsat import SEVIRIConnector

connector = SEVIRIConnector()
targets = connector.discover(
    bbox=(4, 51, 6, 53),
    start=datetime(2016, 6, 15, 10, tzinfo=timezone.utc),
    end=datetime(2016, 6, 15, 10, 20, tzinfo=timezone.utc),
    services=("rapid_scan",), limit=1,
)
result = connector.fetch(targets[0], dest="data/seviri")
lines = connector.line_times(result, channel="IR_108")
radiance = connector.read(result, bbox=(4.8, 52.3, 4.95, 52.4),
                          channels=("IR_108",), calibration="radiance")
counts = connector.read(result, channels=("VIS006",), calibration="counts")
products = connector.parse_canonical(result, channels=("IR_108",))
```

## Discovery and download

| Service | Collection | Nominal cycle and support |
| --- | --- | --- |
| `full_disc` | `EO:EUM:DAT:MSG:HRSEVIRI` | 15 minutes; 0 degree full disc |
| `rapid_scan` | `EO:EUM:DAT:MSG:MSG15-RSS` | 5 minutes; 9.5 degrees east, northern disc |

IODC is outside this connector's scope. EUMDAC `Collection.search` discovers
products; `limit` is per service, `None` returns all matches. `SEVIRITarget`
retains product ID, collection, service, provider `platformShortName` (MSG2,
MSG3, etc.), catalogue `date` interval as `sensing_start`/`sensing_end`, raw
metadata, catalogue size without unit conversion, metadata URL, retrieval time
and requested bbox. The catalogue interval describes measured acquisition,
not the complete nominal 5- or 15-minute slot. No satellite position is inferred
from the platform name or service longitude.

The recorded catalogue publishes empty geometry: `footprint` is `None`.
`bbox` is validated and recorded but **does not filter discovery**, because no
product geometry exists to filter. The ZIP has one native image and ancillary
XML entries, rather than independently selectable image chunks. `fetch` downloads
the complete ZIP using `Product.open()` and streams its `.nat` into a local
file. `SEVIRIResult.path` is that file, and `source_entry` retains its archive
name. No geographic or channel subsetting occurs during download.

`service_coverage` is explicitly connector-declared nominal documentation,
never a product footprint. Its VIS/IR bounds are 1-3712 for full disc and
2321-3712 for RSS. The numerical RSS range follows the
[pinned reader's `is_roi` documentation](https://github.com/pytroll/satpy/blob/v0.60.0/satpy/readers/seviri_l1b_native.py),
which states 1392 northernmost lines on a 3712-row grid. An independent provider
numerical RSS table has not been verified. Actual product coverage is separate.

## Native values, geometry and transforms

`read` returns an xarray Dataset with separate `y_<channel>` and `x_<channel>`
dimensions. `y` holds original line-header ICD numbers; `x` holds zero-based
local array column indices. Array row 0 is the southernmost available row;
rows run south to north. ICD 105 grid numbering is one-based from the south-east
corner. HRV has its own 11136-line grid and separate window geometry; it is
never aligned or resampled onto the 3712-line VIS/IR grid.
`<channel>_projection_x` and `<channel>_projection_y` retain projected pixel
centres in metres, including HRV's potentially different upper/lower window
column origins. Each image records its CRS, original native area extent(s),
shape and selected row/column window. Geographic selection returns the smallest
native rectangle containing pixel centres inside the bbox. Rectangle corners
can lie outside the bbox. An outside bbox returns empty image arrays and an
empty canonical frame.

Only explicitly requested `counts` and `radiance` are accepted. The default
is explicitly `radiance`, including solar channels. Reflectance and brightness
temperature requests raise `ValueError` before opening the file.
Radiance units are `mW m-2 sr-1 (cm-1)-1`; counts have units `count`.
`CalSlope`, `CalOffset`, `calibration_mode="nominal"` and their native header
source are recorded per image. No external, GSICS or Meirink coefficients are
used. Negative radiances from the published affine coefficients are retained.

| Pinned Satpy operation | Connector disposition |
| --- | --- |
| Ten-bit native unpacking | Retained provider encoding |
| Zero-count missing-data mask | Retained; zero counts become NaN, including counts output |
| Nominal affine calibration | Retained header CalSlope/CalOffset for radiance; counts remain counts |
| Unconditional negative-radiance clipping to zero | Disabled with a narrow calibration-algorithm override; no pinned provider basis |
| Reflectance and Earth-Sun distance correction | Disabled by explicit calibration selection |
| Planck/thermal brightness-temperature conversion | Disabled by explicit calibration selection |
| Alternate/external calibration coefficients | Disabled; nominal mode and empty external coefficients |
| Line-quality masking | Native reader does not apply it; encoded line flags retained separately |
| CDS acquisition-time decoding | Retained provider line mean time, channel-specific and grid-specific |
| Disk/window padding | Disabled with `fill_disk=False` |
| Area geometry | Retained native grid steps, mean north/south polar radius and ICD south-east origin |
| Historical georeferencing offset | Retained header TypeOfEarthModel rule: type 1 has half VIS/IR pixel offset (1.5 HRV pixels); type 2 has none |
| Actual satellite position | Satpy evaluates native header orbit polynomials at observation start, preserving source coefficients and evaluation basis |
| Resampling, aggregation and parallax corrections | Not applied |

The grid and historical offset are documented in the
[MSG Level 1.5 Image Data Format Description, ICD 105](https://user.eumetsat.int/s3/eup-strapi-media/pdf_ten_05105_msg_img_data_e7c8b315e6.pdf),
sections 3.1.3-5. Historical type-1 geometry is particularly relevant to
pre-December-2017 data. Source header image description, Earth model, satellite
definition and orbit polynomials are retained in Dataset attrs.
`orbital_parameters` distinguishes projection longitude, nominal satellite
longitude and actual evaluated satellite longitude/latitude/altitude. Actual
position is not replaced with the service longitude when orbit parameters are
unavailable: `satellite_actual_position_available=False` then records the gap.
The position conversion uses the product ellipsoid; Satpy's satpos docstring
calls it WGS-84, but its implementation passes the native Earth-model radii.

## Measured line timing and actual coverage

`line_times(result, channel="IR_108")` returns an xarray Dataset with `row`
(ICD grid line numbers), `time` (UTC datetime64), `chan_id`, `line_validity`,
`line_rquality` and `line_gquality`. These are channel-specific native
`LineSideInfo` facts; no scan-law times are generated. HRV's three interleaved
line records per VIS/IR row are preserved in HRV row order. `read` exposes the
same channel-specific mean times as `<channel>_time`, with the same cropped
rows. Missing CDS time is NaT. Flags stay encoded and unmasked.
See the [MSG native format definition](https://user.eumetsat.int/s3/eup-strapi-media/pdf_fg15_msg_native_format_15_6b513c5bb3.pdf),
sections 1.1.3.7-8, for line number, channel ID, acquisition time and quality.

Both methods expose `actual_coverage` from the trailer's
`15TRAILER/ImageProductionStats/ActualL15CoverageVIS_IR` and
`ActualL15CoverageHRV`. VIS/IR fields include `SouthernLineActual` and
`NorthernLineActual`; HRV carries lower/upper window bounds. These are actual
L1.5 production bounds, distinct from archive-header `selected_rectangle` and
nominal service coverage. `provider_scanning_summary` also preserves actual
ForwardScanStart/ForwardScanEnd. Catalogue sensing interval remains separate.

`parse_canonical` emits one schema-v1 granule row per non-empty product,
with platform, product identity, source URL, retrieval time, measured integration
interval, null footprint and unknown numerical uncertainty. Provider product
QA comes from trailer L15ImageValidity; no derived quality verdict is added.
`target_to_canonical` supports metadata-only rows without decoding pixels.

## Verification

Physical synthetic `.nat` fixtures are generated using Satpy's native header,
line-record and trailer dtypes by `tests/fixtures/seviri_eumetsat/generate.py`.
Recorded keyless discovery JSON contains both services. Tests exercise real
EUMDAC against stubbed HTTP and real Satpy against artificial file bytes,
including every channel, HRV timing/grid, historical offset, unclipped radiance,
encoded QA, native inside/partial/outside windows and canonical validation.
Two narrow reader adaptations disable clipping and normalize acquisition-time
shape for single-channel/single-row products; time values are unchanged.
These fixtures establish adapter mechanics, not real-data scientific validity.

`python scripts/live_smoke.py seviri_eumetsat` discovers both services over
Europe for 2016-06-15 10:00-10:20 UTC, then fetches pinned RSS product
`MSG2-SEVI-MSG15-0100-NA-20160615100416.823000000Z-NA`. It checks that grid rows
increase strictly from south to north, that every line time lies inside the
catalogue interval, and that no line time runs backwards by more than 1 s
(smaller backward steps are reported, not failed). It prints actual coverage beside
nominal 2321-3712, and prints maximum absolute and RMS residual in seconds
against `sensing_start + (row - first_row) / rows_in_service * sensing_duration`.
It prints the number of timed rows used, then checks IR_108 radiance and VIS006
counts over a small Netherlands window. The smoke alone accepts
`EUMETSAT_KEY`/`EUMETSAT_SECRET` and hands them to the connector as a Credential.
Absent values produce SKIP. A live run of this smoke has passed against the
EUMETSAT Data Store. It fetches and reads one rapid-scan product; full-disc
products are covered by live discovery only.

See [observation fields](observation-fields.md), [credentials](credentials.md)
and [data terms](../src/spectraccess/connectors/seviri_eumetsat/DATA_TERMS.md).
