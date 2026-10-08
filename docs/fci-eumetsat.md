# MTG-FCI observations from EUMETSAT

Install `pip install "spectraccess[fci]"` on Python 3.11 or later. The extra
uses Satpy 0.60.0 (GPLv3 or later), EUMDAC 3.1 (MIT), hdf5plugin (MIT), and
NetCDF/HDF5 reader dependencies. hdf5plugin bundles filters with their own
licences: the FCIDECOMP filter used here is Apache-2.0, and its CharLS codec
is BSD-3-Clause. See [hdf5plugin licence information](https://hdf5plugin.readthedocs.io/en/stable/information.html#license).
spectrAccess itself is Apache-2.0.

The [FCI L1c data guide](https://user.eumetsat.int/resources/user-guides/mtg-fci-level-1c-data-guide#MTGFCIlevel1cdataguide-Specialcompressionofradiances,indexmapandqualityflags)
specifies lossless JPEG-LS (CharLS) compression for radiance, index map and
quality variables in files marked `JLS`. The connector imports hdf5plugin to
register FCIDECOMP (HDF5 filter 32018) and appends its bundled plugin directory
to `HDF5_PLUGIN_PATH`, preserving existing directories, so Satpy's NetCDF4
backend can also decode compressed data.

Create an [EUMETSAT account](https://user.eumetsat.int/) and obtain a consumer
key and consumer secret from the [API key page](https://api.eumetsat.int/api-key).
Run `spectraccess login eumetsat`: enter the consumer key at the account prompt
and consumer secret at the hidden password prompt. Alternatively hand over
`Credential("password", consumer_secret, consumer_key)` via `credentials=`.
The connector reads no credential environment variables or EUMDAC login files.
Authentication failures omit the consumer key and secret from exception text.
HTTP 401/403 token rejection raises `CredentialRejected`. A filter on EUMDAC's
logger redacts upstream token-status and bearer-request records, including
at DEBUG, without changing the user's log level.

Ten-minute L1c cycles are EUMETSAT Recommended Data. Retrospective access
after at least one hour is Without Charge; original numerical data have
redistribution restrictions. Hourly L1 and derived L2 products are Core Data
under CC-BY-4.0. See the [data terms](../src/spectraccess/connectors/fci_eumetsat/DATA_TERMS.md)
for the policy distinction and attribution requirements.

```python
from datetime import datetime, timezone
from spectraccess.connectors.fci_eumetsat import FCIConnector

connector = FCIConnector()
bbox = (4.8, 52.3, 4.95, 52.4)
targets = connector.discover(
    bbox=bbox,
    start=datetime(2024, 10, 1, 10, tzinfo=timezone.utc),
    end=datetime(2024, 10, 1, 10, 30, tzinfo=timezone.utc),
    products=("l1c",),
)
result = connector.fetch(targets[0], dest="data/fci", channels=("ir_105", "nir_13"))
pixels = connector.read(result, channels=("ir_105", "nir_13"))
cycles = connector.parse_canonical(result, channels=("ir_105", "nir_13"))
```

## Products and cadence

These IDs were checked against the public EUMETSAT Data Store catalogue on
2026-10-08. `products=` accepts the names in the first column. The default
selects all four products; `limit` bounds targets per product collection.

| Product name | Collection | Provider product |
| --- | --- | --- |
| `l1c` | `EO:EUM:DAT:0662` | FDHSI full-disc normal-resolution L1c, 16 channels |
| `cloud_mask` | `EO:EUM:DAT:0678` | Cloud Mask, NetCDF |
| `cloud_type` | `EO:EUM:DAT:0680` | Cloud Type |
| `ctth` | `EO:EUM:DAT:0681` | Cloud Top Temperature and Height |

Each published repeat cycle remains a separate target and canonical row.
The normal full-disc cadence is 10 minutes. The acquisition interval is
the provider's actual sensing start/end, which need not occupy all ten
minutes. The target retains catalogue metadata, product identity, coverage
and published versions. The catalogue can omit a footprint (the pinned
L1c product has empty geometry); `footprint` is then `None`, and `coverage`
retains the provider's full-disc coverage declaration. The reader supplies
provider projection parameters and native scan angles for locating pixels.
No rectangular query bbox is presented as the provider footprint.

## Chunk downloads and native grids

EUMDAC supplies `AccessToken`, `DataStore.get_collection().search()`,
`Product.entries` and `Product.open(entry=...)`. L1c fetch inspects HDF5
metadata and coordinate vectors with EUMDAC byte-range requests across body
entries, then downloads the complete entries containing requested-channel
pixel centres inside the bbox, plus the trailer. Chunk heights can vary;
selection uses their published scan coordinates. Pixel arrays in other body
entries are not decoded for selection. A server that does not honor byte ranges
raises an error. This transport behavior still needs a credentialed live run.
L2 products are single NetCDF entries and are downloaded before local clipping.
Short cache filenames avoid Windows NetCDF path limits; result metadata retains
the original provider entry names.

`read` returns an xarray Dataset. L1c channels retain separate `y_<channel>`
and `x_<channel>` dimensions, including separate 1 km VNIR and 2 km IR
supports. Each coordinate contains decoded provider scan angles in radians,
with original packing attrs under `provider_attrs`. X is positive westwards;
Y runs south to north. The smallest rectangular native window of pixel
centres inside the bbox is selected. Window corners may include centres
outside the bbox. Existing segment rows are concatenated in provider order;
missing segments are never padded or represented as observations. A bbox
outside the disc returns no image data and an empty canonical frame.
The reader performs no resampling, parallax correction or aggregation.

Every L1c channel returns decoded effective radiance, including thermal
channels, in the provider's radiance-per-wavenumber units. `ir_38` uses
the published dual-gain scale and offset. No conversion to radiance per
wavelength, brightness temperature or reflectance is made. The source
packing metadata remains in `provider_attrs`, and `reader_provenance`
records the reader, version, calibration and effective clipping settings.
Satpy-normalized platform, time and geometry attrs are not provider attrs.

`<channel>_time` contains acquisition times obtained from the provider's
global `time` vector using the channel's `index_map`. Values retain the
source epoch, units and calendar; invalid indices produce NaN. The provider
guide identifies these as UTC acquisition times. `<channel>_pixel_quality`
retains encoded integers, fill codes, bit masks and meanings unchanged.
Cloud classifications and L2 quality fields also retain encoded flag values.
Other L2 image variables use provider CF scale/offset and fill decoding.
Their additional dimensions are namespaced per source variable. L2 cloud
products do not publish pixel acquisition time: `pixel_time_available=False`
and the provider granule interval remains available in canonical rows.

The trailer's `radiometric_noise_lut_radiance` and
`radiometric_noise_lut_noise` are preserved as paired packed provider arrays,
with scale, offset, units and source names. Each channel's table dimensions
are independent, even when identically named source dimensions differ in size.
The [L1c guide](https://user.eumetsat.int/resources/user-guides/mtg-fci-level-1c-data-guide)
and Appendices A.3.5 describe these as outputs of a radiometric noise model
at tabulated effective radiances. They are not a published per-pixel sigma.
Canonical `unc_definition` describes the tables when present; `unc_value`
remains null and `unc_status="unknown"`. No coverage factor is inferred.
Provider trailer quality groups and L2 product quality fields populate `qa`;
pixel flags remain image variables.

## Executed synthetic Satpy spike

`tests/test_fci_eumetsat.py::test_spike_real_satpy` exercises real pinned
Satpy file handlers on generated L1c files; the native reader tests exercise
all 16 FDHSI channels. The L2 tests run Cloud Mask,
Cloud Type and CTTH through the applicable reader or preserve encoded flags.

| Reader operation | Synthetic observation | Connector disposition |
| --- | --- | --- |
| L1c radiance packing | count 1, scale 5, offset -10 returns -5 | Retained provider scale/offset; explicit `radiance` selection |
| IR 3.8 dual gain | count 5000, warm scale 2, offset -300 returns 9700 | Retained provider dual-gain packing |
| Negative-radiance clipping | Enabled clipping changes a negative radiance to positive | Disabled explicitly, even with global clipping enabled; both settings recorded |
| Valid range and fill mask | Source valid range/fill defines invalid measurement locations | Retained as provider mask; QA values preserved separately |
| Planck brightness temperature | Negative input becomes NaN; positive radiance is converted using provider coefficients | Disabled; output remains effective radiance |
| Reflectance calculation | Radiance 5 / irradiance 50 times pi times 100 | Disabled; output remains effective radiance |
| Per-wavelength unit conversion | Provider conversion coefficient remains available in source metadata | Disabled; provider per-wavenumber units retained |
| Acquisition-time lookup | Index 101 selects time 101; fill index gives NaN | Provider indexed time reconstruction, source units/calendar preserved |
| Satpy segment padding | Scene assembly can pad missing segments | Bypassed using individual file handlers; only existing rows concatenated |
| Satpy normalized attrs and area construction | Reader generates platform labels and area metadata | Source attrs explicitly mapped; provider scan angles/projection retained |
| L2 CF decoding | packed height 1, scale 100, offset 10 returns 110; fill gives NaN | Retained provider packing for measurements |
| L2 enum, quality and configured fill masking | Raw quality fill -127 remains -127 in connector output | Direct encoded reads preserve enums, flags and provider meanings |
| L2 total optical thickness and test-mask extraction | Reader supports derived OCA totals and special test masks | Outside the selected cloud-product fields; no computed quantities requested |

Fixtures are generated by `tests/fixtures/fci_eumetsat/generate.py` from the
published group/variable layout, using artificial values. Tests include
different grids, missing segments, same-name/different-size noise tables,
partial/outside windows, acquisition times, flag fill values and ten-minute
target separation. No real observations are committed.

The [CLM, CT and CTTH provider guide](https://user.eumetsat.int/resources/user-guides/mtg-fci-clm-ct-and-ctth-data-guide)
identifies CLM `cloud_state`, CT `cloud_type` / `cloud_phase`, and CTTH
temperature, pressure, height, aviation height, effective cloudiness and
provider parallax-offset fields. The connector reads the variables actually
present, preserving their provider names; it does not apply parallax offsets.
The guide does not identify a per-pixel uncertainty field for these products.

Maintainers run `python scripts/live_smoke.py fci_eumetsat` in a login-enabled
workflow. It discovers the pinned 2024-10-01 10:00 UTC repeat cycle, fetches
only entries for a small Amsterdam bbox, and reads `ir_105` and `nir_13`,
their pixel times and quality. It prints progress for discover, fetch and
read. Missing `EUMETSAT_KEY` / `EUMETSAT_SECRET` produces SKIP; the smoke
hands those values over as a Credential. SKIP is not live verification.

See [credentials](credentials.md), [observation fields](observation-fields.md)
and [data terms](../src/spectraccess/connectors/fci_eumetsat/DATA_TERMS.md).
