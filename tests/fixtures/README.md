# Fixture provenance

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
