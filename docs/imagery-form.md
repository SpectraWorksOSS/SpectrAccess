# Described-sample imagery (experimental)

The imagery API returns native samples in one xarray `DataTree` vocabulary.
Landsat OLI, Sentinel-2 MSI and EMIT retain their own grids, band timing,
geometry and spectral responses. There is no common resampled image grid.
The imagery schema is experimental, version `0.1.0`, independent of the
existing canonical table schema.

## Install and read

```sh
pip install "spectraccess[imagery]"
```

Discovery and downloading use the existing connectors and their separate
`landsat`, `cdse` or `emit` extras. The imagery extra reads local files; it
does not need archive clients or credentials. EMIT discovery still needs
Python 3.12+; local EMIT L1B RAD+OBS reading supports Python 3.10+.

```python
from spectraccess.connectors.sentinel2_cdse import read_imagery
from spectraccess.core.imagery import validate, write_netcdf

# Existing, extracted, single-tile L1C SAFE. Bounds are west/south/east/north.
tree = read_imagery("scene.SAFE", bands=["B02", "B03", "B04"],
                    bounds=(4.80, 51.83, 4.81, 51.84), collection="operational")
validate(tree)
write_netcdf(tree, "site.nc")
tree.close()
```

Each connector also exposes `ConnectorClass.read_imagery(...)` without
constructing a discovery client. The local readers accept:

| Reader | Inputs and options |
| --- | --- |
| `landsat_eodag.read_imagery` | Extracted L1 directory, text MTL, optional ANG; `bands` (OLI 1..9), `bounds`, `height` in metres, `spectral_responses` |
| `sentinel2_cdse.read_imagery` | Single-tile L1C SAFE; `bands`, `bounds`, `collection`, `external_srfs` |
| `emit_earthaccess.read_imagery` | L1B RAD netCDF, optional `observation=OBS`; `bounds`, optional `solar_irradiance`, `solar_spectrum` |

Raster bounds are applied before reading Landsat/S2 band values. EMIT values
use lazy chunks; close its tree after use. Geolocation arrays are read to
locate raw sensor pixels. These readers do not unpack telemetry or create
archive files. Landsat tar files and SAFE zip files must be extracted first.

## Runnable synthetic example

This example needs no satellite files or network access:

```python
import numpy as np
import xarray as xr
from spectraccess.core.imagery import (
    describe, measurement_group, imagery, validate, write_netcdf, open_netcdf,
)

counts = describe(xr.DataArray(np.ones((2, 3, 4)),
                  dims=("band", "line", "column")), basis="published", units="DN")
group = measurement_group({"counts": counts}, primary="counts",
    geometry_kind="sensor_geometry_with_lookup", processing_chain=[])
group["time"] = describe(xr.DataArray([0., 1., 2.], dims="line"),
                         basis="published", units="s")
tree = imagery({"raw": group})
assert validate(tree) == []
write_netcdf(tree, "synthetic.nc")
with open_netcdf("synthetic.nc") as restored:
    values, times = xr.broadcast(restored["/measurements/raw/counts"],
                                 restored["/measurements/raw/time"])
    assert values.shape == times.shape == (2, 3, 4)
```

## Tree, dimensions and broadcasting

`/measurements/<native-grid>` contains an xarray Dataset. S2 uses one group
per native resolution (`r10m`, `r20m`, `r60m`); Landsat currently keeps one
group per OLI band (`B1` through `B9`, including the native panchromatic grid);
EMIT uses `swath`. Layers are `counts`, `radiance`, `toa_reflectance`,
`surface_reflectance`, `brightness_temperature` or `derived`, where present.
Units are explicit. QA and extra provider fields can be added.

Descriptors use the dimensions where they vary: `band`, `detector`, `line`,
`column`, `y`, `x`. Raw EMIT values use `(band,line,column)`; map values use
`(band,y,x)`. `time`, `sun_zenith`, `sun_azimuth`, `view_zenith` and
`view_azimuth` need not have the same dimensions as a quantity layer.
`xr.broadcast(value, descriptor)` supplies the common consumer path.
Sharing EMIT line time across bands is explicit; no reader inherits one
scene time across staggered OLI/MSI bands. Times are UTC epoch seconds where
available, with their epoch recorded in units. `open_netcdf` preserves these
numeric values rather than automatically decoding CF timestamps.

Landsat ANG output keeps each contributing SCA separately on `detector`;
invalid SCA/pixel pairs are NaN and `detector_index` describes the mapping.
Default evaluation at provider band mean height carries an assumption record;
an explicit `height` overrides that evaluation height.
S2 detector angles stay separate, with the native raster `MSK_DETFOO` mask
applied when available on the same grid. Detector overlap is not averaged.
S2 GML detector polygons are rasterised at native pixel centres; without a
detector mask, per-pixel time remains unknown. Consumers must respect detector keys
and finite coverage when broadcasting, rather than counting extra detectors
as additional independent image pixels.

`/provider_models` retains S2 XML, coefficients and datation. Landsat keeps
MTL and ANG coefficients as group attributes. EMIT GLT arrays remain under
`/geolocation_lookup` on their original ortho grid, with original one-based
indices and fills. A cropped raw swath does not renumber those indices.

## Quantities and stages

`primary_quantity` names the least-converted calibration quantity.
OLI/MSI start in reflectance, EMIT in radiance. Other layers remain available
as explicitly labelled conversions of the primary quantity. These deterministic
conversions are fully correlated with the primary and supply no independent
calibration evidence. A solar-model conversion also introduces the solar-model
uncertainty, which these readers leave unknown. Landsat radiance evaluates
MTL coefficients on the same DN and is likewise not independent evidence.
Landsat also retains delivered DN.

Every measurement group has a JSON `stage` attribute with `quantity`,
`geometry_kind` (`map_grid` or `sensor_geometry_with_lookup`) and
`processing_chain`. Each step records `kind`, `actor`, `processor` and
`parameters`. `provider_level` is only the provider's label, never a substitute
for stage. The chain describes the primary quantity in chronological order.
Conversions of other layers are described by those layers' `conversion`
attributes, outside the primary chain. An empty chain represents an already
unpacked raw counts cube.

Landsat MTL reflectance is rho-prime, **without** the sun-angle term. MTL
radiance uses the provider coefficients directly, with per-micrometre units.
No scene-centre sun angle is silently applied to reflectance. The solar
spectrum implicit in those coefficients stays unstated if not supplied by
metadata. Earth-sun distance is retained where published.

S2 reflectance uses `(DN + RADIO_ADD_OFFSET) / QUANTIFICATION_VALUE`; DN zero
is no data. From PB 04.00 a missing per-band offset is an error. Radiance is
`rho * SOLAR_IRRADIANCE * U * cos(SZA) / pi`, in per-micrometre units. Its
`conversion` attribute retains each band's irradiance, U and sun-grid convention.
**This inversion is not exact**: an `operator_declared` AssumptionRecord names
bilinear interpolation of the tile's 5 km sun grid. The 70 degree zenith
clip is a separate `documented` record, sourced to
[SentiWiki S2 Products](https://sentiwiki.copernicus.eu/web/s2-products). The source solar spectrum behind the provider band irradiances remains
unstated. S2 radiance is always labelled evaluated, never published.

The connector's `processing.py` table cites SentiWiki's Collection-1/PB
history. For sourced collection/baseline combinations it records S2B-to-S2A
radiometric alignment and DEM refinement. Unsupported combinations remain
unstated. Pass `collection="operational"` or `collection="collection-1"`
only when known from the source; an Nxxxx baseline alone does not establish
collection identity. Per-band processing parameters stay in the shared stage
chain after concatenating a native resolution group.

EMIT preserves provider radiance units, typically `uW cm-2 sr-1 nm-1`, and
declares energy basis. Its optional reflectance conversion needs a caller
supplied positive per-band solar irradiance array **in matching area and
wavelength units**, plus a named `solar_spectrum`. The caller spectrum is
declared as an assumption. OBS sun zenith and Earth-sun distance enter the
conversion. Without a spectrum, the reflectance layer is explicitly unknown.

Every `toa_reflectance` layer requires an enumerated `sun_normalisation`:

| Value | Definition |
| --- | --- |
| `rho_prime_without_sun_term` | Rho-prime without division by cos(sun zenith), as in Landsat MTL |
| `true_rho_with_70_degree_clip` | Rho includes the sun term, with provider sun zenith clipped at 70 degrees, as in S2 L1C |
| `true_rho_without_clip` | Rho includes division by cos(sun zenith), without clipping, as in EMIT conversion |

The convention is required even for an explicitly unknown reflectance layer;
it describes the intended quantity, not availability of its values.

## Two descriptor axes, unknowns and assumptions

Each known descriptor has two independent attributes:

| Axis | Values |
| --- | --- |
| `basis` | `published`, `evaluated_from_provider_model`, `interpolated_from_provider_grid`, `scene_nominal`, `unknown` |
| `uncertainty_status` | `provided`, `derived`, `prior`, `unknown` |

Value basis definitions:

| Value | Meaning |
| --- | --- |
| `published` | Values supplied directly by the provider |
| `evaluated_from_provider_model` | Values calculated from provider coefficients or a provider model |
| `interpolated_from_provider_grid` | Values interpolated between provider grid nodes |
| `scene_nominal` | A scene-level nominal value, not a per-pixel evaluation |
| `unknown` | No value basis is established; missing values carry a reason |

Uncertainty statuses use the meanings in
[`core/schema.py`, `UncertaintyStatus`](../src/spectraccess/core/schema.py):
`provided` means supplied by the source; `derived` means calculated from other
information; `prior` means assumed rather than measured for these samples;
`unknown` means no uncertainty value is available. These describe uncertainty
provenance, independently of value basis. A `provided` record must carry scalar
attributes `uncertainty_value` (finite, nonnegative, in the value's units),
`uncertainty_k` (finite positive coverage factor), and `uncertainty_kind`
(`standard`, requiring k=1, or `expanded`). No reader currently emits provided
uncertainties. Correlation is not inferred from these fields.

For concatenated S2 bands, `per_band_metadata` is a JSON object keyed by band
containing each band's complete descriptor attributes, including basis, reason,
source, assumptions and uncertainty status. With `provenance_scope="per_band"`,
these keyed records are authoritative. Variable-level attributes contain only
shared metadata; a fallback `basis="unknown"` or `uncertainty_status="unknown"`
when records differ is not a statement about individual bands. Consumers must
select the band record as well as its values. This applies to time, detector
mapping and angles, including explicit unknown records when metadata is absent.

Evaluation does not establish uncertainty. Defaults only describe the absence
of uncertainty information; they never manufacture a quantity or geometry.
An absent optional field means unstated. `unknown(reason)` describes a known
missing field explicitly. Missing ANG, MTD_TL, datation, supported masks or OBS
produces unknown descriptors rather than nominal scene geometry or a crash.

Approximation/proxy records reuse `core.assumptions.AssumptionRecord`:
`assumption_id`, `statement`, five-value `basis`, and `resolves_when`.
The assumption basis and descriptor value basis are separate vocabularies.
Assumptions and structured processing parameters are JSON attributes for
portable serialization. Unknown extra fields are accepted by the validator.

S2 time evaluates a cubic-Hermite provider orbit against each pixel's band and
detector LOS, checking band/detector datation against orbit coverage.
LOS evaluation uses WGS84 ellipsoid height zero, declared in its assumption.
Provider datation and reference lines remain alongside the evaluation. A 0.05 s
relative-delay fixture envelope checks B02/B03/B04 against 0.527/1.005 s.
It is not an absolute-time uncertainty or standard deviation. Absolute timing
uncertainty remains unknown. LOS angular residual is retained separately.

## Spectral responses

`/spectral_responses/<entry>` is keyed by `band`, optionally `column` or
`detector`. Keyed entries require a `key_to_pixel_map` or declared assumption.
Representation is `tabulated` (`wavelength`, `response`), `parametric`
(`centre`, `fwhm`, shape and width definition), or `unknown`.

S2 embedded curves keep identity `(embedded, PB, flight unit, product id)`;
they have no invented version number. `read_esa_srf(path, unit="S2B")` reads
the local ESA v4.0 workbook, verified against a pinned SHA256, as a **separate** entry; pass it through
`external_srfs={"esa_v4": table}`. Metadata fixtures compare PB 03.00 and
05.10 embedded S2B curves with ESA v4.0. Both match at their published samples.
That result does not establish equality for other baselines or flight units.

`read_tabulated_srf(path, source=..., version=..., unit=...)` reads a long CSV
with `band,wavelength,response` and optional `stddev`, wavelength in nm. Use
`spectral_responses={"oli": table}` to supply an alternative versioned OLI RSR
table. By default Landsat uses bundled, SHA256-pinned USGS Spectral
Characteristics Viewer workbooks: OLI v1.2 for Landsat 8 and OLI-2 v2-1 for
Landsat 9, also available through `read_usgs_srf(unit="LANDSAT_9")`.
Published range, spacing, normalization and spread remain as supplied, without
fitting, resampling or normalizing curves. Bands retain native wavelength
samples; rectangular-table padding is NaN. The published spread is not
promoted to measurement uncertainty.

EMIT centre and half-max FWHM are published; Gaussian shape and column
invariance are declared assumptions, not published curves. No smile or SRF
spread is modelled. Consumers choose how to construct an operator from these
descriptions.

## Crops and I/O

`site_box(tree, (west,south,east,north))` returns a rectangular crop of whole
native pixels intersecting the box, retaining original pixel indices and
corner footprints. It does not calculate partial-pixel weights. Map readers
derive corners from CRS/transform. EMIT uses raw pixel centres to interpolate
approximate corners with an explicit assumption, including edge extrapolation;
it never crops radiance on GLT cells. Antimeridian-crossing boxes are currently
unsupported. Crops can include pixels outside the requested box.

`validate(tree)` raises on malformed known fields; use
`validate(tree, raise_errors=False)` to get error strings. It checks stages,
layer units, dimensional consistency, descriptor axes and SRF representation.
It does not validate numerical calibration against a provider's image pixels.

`write_netcdf(tree, path)` validates, then writes netCDF4 groups; it replaces
an existing file. `open_netcdf(path)` opens lazily and validates. Always close
opened trees. Zarr uses xarray directly (`tree.to_zarr`, `xr.open_datatree`);
round-trip tests cover it, but there is no separate Zarr writer API.

## Support

| Source | Imagery API status |
| --- | --- |
| Landsat 8/9 OLI L1 | Experimental; coefficient layers and per-SCA ANG evaluation; bundled pinned USGS RSR, optional external RSR |
| Sentinel-2 L1C SAFE | Experimental; native resolution groups, per-detector angles, LOS time where inputs exist, embedded SRFs |
| EMIT L1B RAD+OBS | Experimental; raw geometry, UTC line time, GLT, parametric SRF |
| Existing EMIT L2A reader | Existing `read_cube` unchanged |
| SLSTR, VIIRS, FCI, SEVIRI, OLCI and other connectors | Existing APIs unchanged; not migrated to this form |
| CHIME | Design-only vocabulary coverage; no reader or product conformance proof |

Metadata and synthetic-array checks do not establish real-image reader
correctness. Native image site boxes still need independent product checks
before relying on a new reader for quantitative work.

Landsat ANG evaluator tests compare metadata samples against the independent
USGS C oracle. Comparison with delivered band-4 angle grids is deferred to
the first real-scene read in the cloud validation phase.
