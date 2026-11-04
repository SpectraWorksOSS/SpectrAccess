# Imagery metadata fixtures

No provider image rasters are stored here. Synthetic image arrays are created
by the tests in temporary directories. `sources.json` pins public metadata
URLs and SHA256 hashes. The two Sentinel-2 datastrip fixtures contain only
Time_Stamp and GPS_Points_List with their ancestor hierarchy; other metadata
was removed without altering retained values. Original-file and extracted-file
hashes are separate in the manifest.

The original `20210507_MTD_MSIL1C.xml` and `20210507_MTD_TL.xml`:
Copernicus Sentinel data 2021. The original `20240501_MTD_MSIL1C.xml` and
`20240501_MTD_TL.xml`: Copernicus Sentinel data 2024.
The extracted `20210507_MTD_DS.xml`: Contains modified Copernicus Sentinel data 2021.
The extracted `20240501_MTD_DS.xml`: Contains modified Copernicus Sentinel data 2024.

`fetch_metadata.py` refreshes metadata from public archives and independent
oracle sources. ESA reference downloads use the separate script below. It never downloads spectral image rasters. Refreshing
fixtures is a deliberate maintainer action, not part of pytest.

`vendor/sentinel_metadata.py` is a pinned pycontrails source, under the adjacent
Apache 2.0 license (`vendor/PYCONTRAILS_LICENSE`) and attribution notice
(`vendor/PYCONTRAILS_NOTICE`). Tests execute its unmodified band/detector parser in
isolation; the aviation package is not a runtime dependency.

`vendor/l8_angles` contains unmodified C/header files from the USGS l8_angles
2.7.0 distribution mirrored by brazil-data-cube/landsat-angles-docker. Source
archive hashes are recorded. USGS identifies its software as public domain.
The included `lablib3.c` and headers carry authorship/history headers, not
copyright or licence notices. This code is a
test oracle, never the package's runtime evaluator.

`build_landsat_oracle.py --cc gcc` compiles `landsat_oracle.c` against the
vendored USGS implementation and emits `landsat_oracle.json`: 251 metadata
sample evaluations across OLI bands 1..9, preserving each contributing SCA.
On Windows a Zig compiler can be used with `--cc <zig.exe> --zig --temp-root
<short temporary path>`. The executable is temporary and no image is created.
The first oracle covers actual provider ANG coefficients from a Collection-1
L1GT product. `landsat9_C2_ANG.txt` adds Collection-2 Landsat-9 coefficients
shipped with a provider L2 product (the original per-band L1T/L1R model).
`landsat9_C2_oracle.json` has 265 samples, generated with the same C harness
using `--ang <metadata path> --output <JSON path>`. Neither replaces comparison
against delivered band-4 angle grids or validates spectral image pixel reads.

The ESA v4.0 workbook is not redistributed. Run
`python tests/fixtures/imagery/fetch_esa_srf.py` to fetch it from ESA into the
git-ignored `.cache` directory. The download and reader both verify its pinned
SHA256; CI fetches it before tests. Tests skip with instructions when absent.
ESA website terms apply to the document. The frozen
`s2_srf_comparison.json` records a comparison on embedded wavelength samples
for S2B PB 03.00 and 05.10. All 13 embedded curves match exactly at those
samples in both fixtures. This JSON contains our derived equality flags and
maximum differences, not ESA response values. No equality claim is made for other units/baselines.

Timing tests compare one common valid node per detector in each S2 fixture
against the published B02/B03 and B02/B04 delays. A 0.05 s relative-delay
envelope is a fixture check, not an absolute-time standard uncertainty.
