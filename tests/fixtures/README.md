# Fixture provenance

## `sentinel2_cdse/S2B_T31UFT_20240501_feature.json`

- Source: public CDSE OData catalogue feature for product ID
  `d085d39b-03e2-486d-ae2a-0c8deca9bdc0`, queried live through
  `cdsetool==0.3.1` on 2026-07-14 with expanded attributes.
- Product: Sentinel-2B MSI L1C, tile 31UFT, sensing time
  2024-05-01T10:36:19.024Z. The full 756,691,750-byte SAFE was **not**
  downloaded or checked in; this hermetic fixture contains only the public
  catalogue metadata needed to prove discovery/parity/provenance behavior.
- Trimmed: retained identity, size, availability, source path, checksums,
  content date, exact provider footprint WKT, and the attributes consumed by
  the connector. Large unused attribute strings were omitted.
- Licence: Sentinel data, including its metadata, is available on a free,
  full and open basis under the Sentinel Data Legal Notice linked from the
  CDSE terms. This fixture is Copernicus DATA, not Apache-2.0 code. Credit:
  contains modified Copernicus Sentinel data (2024), processed by ESA.
- Terms: https://dataspace.copernicus.eu/terms-and-conditions
- Used by: `tests/test_sentinel2_cdse.py`.

## `radcalnet/GSCN01_2025_334_v04.05.output`

- Source: fetched live from `api/json/GSCN/data/GSCN01_2025_334_v04.05.output`
  on the RadCalNet official JSON API, 2026-07-07 (real authenticated fetch,
  not synthetic).
- Trimmed: the original file is 452 lines (234 reflectance wavelengths +
  atmospheric-uncertainty metadata + 234 uncertainty wavelengths, 400-2500nm
  step 10, 13 time columns). This fixture keeps the full site header and ALL
  13 per-time metadata rows verbatim (both blocks), but trims the spectral
  blocks to 8 representative wavelengths (400, 410, 500, 600, 700, 800, 1000,
  1010nm) chosen to exercise: a fill (9998) reflectance value (column 1, every
  row), positive per-wavelength uncertainty values, negative ("climatological")
  per-wavelength uncertainty values, and an all-fill wavelength (1010nm, both
  blocks) for the fill-uncertainty/fill-reflectance case.
- Used by: `tests/test_connectors.py` RadCalNet parse/canonical tests.
- Licence: this is a trimmed real RadCalNet data file redistributed under
  RadCalNet Data Policy v1.1, which permits redistribution with acknowledgment.
  We acknowledge RadCalNet and the GONA/GSCN Baotou site operators. This fixture
  is DATA and is not covered by this repository's Apache-2.0 code licence.

## `gsics_msg4_seviri_metopb_iasi_nrtc_20260704.nc`

- Source URL: `https://gsics.eumetsat.int/thredds/fileServer/msg4-seviri-metopb-iasi-oper-nrtc/W_XX-EUMETSAT-Darmstadt,SATCAL+NRTC+GEOLEOIR,MSG4+SEVIRI-MetOpB+IASI_C_EUMG_20260704000000_01.nc`
- Downloaded: 2026-07-05
- Format: netCDF-3 classic (`CDF\x01` magic), openable with `xarray.open_dataset(..., engine="scipy")`.
- Rights: file's own `license` global attribute states "Information delivered as a GSICS product is
  generated in accordance with the GSICS principles and practices. GSICS products are public and may
  be used and redistributed freely. Any publication using GSICS products should acknowledge both
  GSICS and the relevant data creator organization. Neither the data creator, nor the data publisher,
  nor any of their employees or contractors, makes any warranty, express or implied, including
  warranties of merchantability and fitness for a particular purpose, or assumes any legal liability
  for the accuracy, completeness, or usefulness, of this information." The EUMETSAT THREDDS catalog
  also documents this family as `<documentation type="Rights">Freely available</documentation>`.
- Used by: `tests/test_connectors.py` netCDF parse test, to validate the GSICS connector's real
  product-format parsing path against a real downloaded file (not a synthetic stand-in).
