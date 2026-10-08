# Artificial SLSTR product subset

`generate.py` creates this small SEN3 directory and `feature.json` without
accessing a provider service. Layout follows ESA SLSTR Level-1 PDFS issue
2.12: separate channel/view/stripe radiance or BT files, geodetic coordinates,
indices, bit fields, time_an/time_bn/time_in, viscal, tie-point geometry and
geodetic/Cartesian support,
quality tables and the XFDU manifest. Numeric measurements are artificial.

Visible stripes are 4 by 6; thermal/fire grids are 3 by 4. Packed measurements
decode from 40 to 21 using source scale 0.5 and offset 1. Irradiance is 100.
Time variables use microseconds since 2000-01-01, with acquisition beginning
2024-05-01 10:00 UTC. Cloud flags count upwards and confidence flags contain
65535. Solar/view geometry is on a separate 2 by 2 tie-point grid. The IR
quality files use PDFS scene-temperature LUTs, detector-by-LUT uncertainty
and row-by-detector-by-integrator noise. S7 and S8 reuse `uncert_table` with
161 and 301 entries, reproducing the live dimension collision. F1 uses only
`F1_BT_fn/fo` measurements with `F1_quality_in/io` annotations.
The manifest uses dataObject/byteStream/fileLocation nesting so tests exercise
CDSETool's real manifest parser and filename selection with mocked transport.

Regenerate from the repository root:

```powershell
python tests/fixtures/slstr_cdse/generate.py
```

Tests generate independent temporary copies. Provider field names and shapes
are exercised; these fixtures are not evidence of successful live retrieval.
