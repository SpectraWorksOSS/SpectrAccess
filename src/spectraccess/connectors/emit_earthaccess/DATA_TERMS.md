# NASA EMIT Data Terms

EMIT products are distributed by NASA's LP DAAC under the NASA Earth Science
Data and Information System data policy. NASA Earth science data are openly
available; users remain responsible for acknowledging the EMIT mission and the
LP DAAC and for following the citation guidance attached to the selected product.

The connector ships no EMIT science data. Public CMR discovery is anonymous.
Protected asset downloads use the caller's own free NASA Earthdata Login through
the maintained `earthaccess` credential chain.

Authoritative references:

- NASA Earthdata data and information policy: https://www.earthdata.nasa.gov/engage/open-data-services-and-software/data-and-information-policy
- EMIT data products: https://earth.jpl.nasa.gov/emit/data/data-products/
- EMIT L2A reflectance product: https://www.earthdata.nasa.gov/data/catalog/lpcloud-emitl2arfl-001
- earthaccess EMIT tutorial: https://earthaccess.readthedocs.io/en/latest/user/tutorials/emit-earthaccess/

This wrapper does not declare EMIT products suitable for calibration claims.
Scientific admission, product-version review, quality filtering, GLT interpretation,
and uncertainty use remain downstream decisions.

## Local cube API

`read_cube(reflectance, *, uncertainty=None, mask=None, observation=None,
target=None)` and `EMITEarthaccessConnector.read_cube` return a lazy
`xarray.Dataset` using xarray and h5netcdf from `spectraccess[emit]`.
`parse` and `parse_canonical` retain their metadata DataFrame outputs; all three
use the same local metadata parser to check file versions against an optional
CMR target. Discovery retains its V001 collection support; local reading
supports V001 and V002.

```python
from spectraccess.connectors.emit_earthaccess import read_cube

with read_cube("RFL.nc", uncertainty="RFL_UNCERT.nc",
               mask="MASK.nc", observation="OBS.nc") as cube:
    print(cube.reflectance.isel(downtrack=0, crosstrack=0).values)
```

Inputs are local NASA product paths. Optional files may be omitted. Explicit
missing files raise an error. Close the returned dataset after reading.
Reflectance and uncertainty share raw `(downtrack, crosstrack, bands)` indices
and provider units of dimensionless reflectance. The `provider_definition` attribute
quotes the L2A ATBD's "predicted uncertainty in the reflectance measurement for
each channel, in units of standard deviations (presuming a Gaussian
distribution)." Its long name quotes "Reflectance uncertainty (one standard
deviation)" when the file supplies no long name. Existing descriptions and
long names are preserved. No other interpretation is added.

Per-granule `wavelengths` and `fwhm` retain their provider units (nm) and
`good_wavelengths` retains its flag values. Global `product_version` metadata
binds them to the file. Only fill values are masked: reflectance and uncertainty
use `-9999`, while bad-band `-0.01` values remain. No band filtering or
resampling is performed.

The raw `lat` and `lon` coordinates retain provider degree units. GLT values
and dimensions remain as published, including V001 `-9999` and V002 `0` fill.
The reader does not infer an index base or orthorectify.

`mask` uses `(downtrack, crosstrack, mask_bands)`. V001 provider labels `AOD550`
and `H2O (g cm-2)` select `aerosol_optical_depth` and `water_vapor`
case-insensitively. Only absent labels permit fallback to ATBD channels 6 and 7
(zero-based indices 5 and 6). Each alias records `alias_source` as
`provider mask_bands label` or `ATBD channel order`. Published labels that fail
to identify both channels raise `ValueError`.
V002 accepts the separate EMITL2AMASK file and preserves provider value arrays.
`obs` uses `(downtrack, crosstrack, observation_bands)` for L1B observation
geometry. Labels are preserved as `mask_<provider_name>` and
`obs_<provider_name>`, including solar/view zenith and azimuth. Units are
copied from the files. Companion versions and raw dimensions must match;
scene identifiers are checked when present. No quality judgement is added.

Provider uncertainty definition: NASA EMIT L2A ATBD, section 5 and Table 1,
https://lpdaac.usgs.gov/documents/1571/EMITL2A_ATBD_v1.pdf.
