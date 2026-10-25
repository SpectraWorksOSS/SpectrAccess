# Published OLI relative spectral response tables

Original USGS Spectral Characteristics Viewer workbooks, unmodified.

| Instrument | File | Version | SHA256 |
| --- | --- | --- | --- |
| Landsat 8 OLI | L8_OLI_RSR.xlsx | v1.2 | 6b8bb8bf0c6f1a3cef56eb497096283f6cd66f1af163989eb03c59aa6588cfe0 |
| Landsat 9 OLI-2 | L9_OLI2_RSR.xlsx | v2-1 | 31b6c3c618716419d2c2a650b77ae8c4604abde36af84c7bd38031e58c174812 |

Sources:

- https://landsat.usgs.gov/landsat/spectral_viewer/bands/L8_OLI_RSR.xlsx
- https://landsat.usgs.gov/landsat/spectral_viewer/bands/L9_OLI2_RSR.xlsx

`read_usgs_srf` verifies the original-file hash before reading native band
samples and the published spread column. No interpolation, fitting, clipping
of small negative measurement values, or normalization is applied.
