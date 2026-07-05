# Fixture provenance

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
