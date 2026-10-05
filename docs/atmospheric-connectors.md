# Atmospheric observations in canonical schema v1

These additions expose OLCI integrated water vapour, NGL GNSS zenith delays
and CAMS EAC4 surface pressure. Existing connectors and canonical columns
retain their behavior. Install `spectraccess[cdse]` for OLCI and
`spectraccess[cams]` for ADS pressure. NGL uses the base installation.

## Usage

```python
from datetime import date, datetime, timezone
from spectraccess.connectors.olci_cdse import OLCICDSEConnector
from spectraccess.connectors.ngl_gnss import NGLGNSSConnector
from spectraccess.connectors.cams import CAMSConnector

olci = OLCICDSEConnector()
targets = olci.discover(
    bbox=(4.0, 51.5, 5.0, 52.5), start=date(2024, 5, 1),
    end=date(2024, 5, 1), limit=1,
)
# Downloads use CDSE_USERNAME/CDSE_PASSWORD or CDSETool's .netrc.
if targets:
    raw = olci.fetch(targets[0], dest="./olci")
    iwv = olci.parse_canonical(raw, bbox=(4.0, 51.5, 5.0, 52.5))

ngl = NGLGNSSConnector()
target = ngl.discover(station="ABMF", day=date(2008, 9, 2))[0]
delays = ngl.parse_canonical(ngl.fetch(target))

# ADS_TOKEN from your account; accept the EAC4 terms in ADS first.
cams = CAMSConnector(source="ads")
raw = cams.fetch_surface_pressure(
    valid_time=datetime(2024, 5, 1, 9, tzinfo=timezone.utc),
    area=(52.5, 4.0, 51.5, 5.0), dest="./pressure.nc",
)
pressure = cams.parse_surface_pressure(raw)
```

NGL delivers ZTD in metres, not water vapour. A downstream operator must keep
surface pressure, weighted mean atmospheric temperature and height handling
visible when converting delays. OLCI IWV is kg m-2; CAMS pressure is Pa.
The pressure request uses an exact EAC4 epoch; choose the epoch deliberately
instead of silently rounding an acquisition time.

## Optional observation contract

`SCHEMA_VERSION` stays `1.0`. `CANONICAL_COLUMNS` and `empty_frame()` retain
the original required-column shape. `OBSERVATION_COLUMNS` registers the
optional fields below; `validate()` checks them when present. An absent or
null optional value means unknown, including likelihood, coverage factor,
zero bias, correlation and prior metadata. An empty published list means a
known empty list; it is not substituted for an unknown one.

| Fields | Contract |
| --- | --- |
| `u_independent`, `u_structured`, `u_common` | Non-negative standard uncertainties in the observation's units or declared likelihood-transform space |
| `uncertainty_k` | Exactly 1 when declared; original `unc_k` retains existing semantics |
| `correlation_lengths` | Mapping of positive lengths: `spatial_m`, `temporal_s`, `vertical_m`; only known axes supplied |
| `common_group_id` | Shared-effect identifier, required when `u_common` is given |
| `bias`, `u_bias` | Signed observation-minus-truth bias and non-negative standard uncertainty, separate from the value and random error |
| `likelihood_family` | `gaussian`, `gaussian-in-transform`, `student_t`, `censored`, `categorical` |
| `likelihood_transform`, `likelihood_parameters` | Declared transform expression/name and mapping of its parameters, degrees of freedom, censor bounds or category probabilities; transform required for gaussian-in-transform |
| `valid_time`, `integration_start`, `integration_end` | UTC observation time and published integration-window endpoints; cadence and scene duration do not establish an integration window |
| `footprint_geometry`, `support_kind`, `elevation_m` | GeoJSON geometry in longitude/latitude degrees, support `point`, `pixel`, `grid cell` or `swath`, and source support elevation in metres with its datum retained in QA |
| `correlation_groups`, `assimilated_inputs` | Lists of declared group and input identifiers, without guessed independence or complete assimilation inventories |
| `retrieval_prior` | Published prior description/source metadata mapping |
| `prior_state`, `prior_covariance`, `averaging_kernel` | Published numerical arrays in the source state basis; labels, units, dimensions and reference belong in `retrieval_prior`; absent when unpublished |
| `sigma_basis`, `assumptions` | Uncertainty basis text and list of spectrAccess `AssumptionRecord` objects; no import of a downstream assumption type |
| `qa`, `algorithm_version`, `collection_version` | Source quality mapping, processor version and collection identity |

The new fields do not alter `Uncertainty` status rules. A source-supplied
formal error remains `provided`; a missing error remains `unknown`. A
formal error is not automatically an independent-error component. These
connectors do not invent a likelihood or missing common/structured magnitudes.
Numerical prior metadata is carried only when published, with no attempt to
construct an averaging kernel from an algorithm description.

OLCI retains pixel centres in `pixel_center_geometry` and the catalogue's
swath in `product_footprint`; neither is substituted for unknown pixel
boundaries. The published positive 7 to 10 percent wet-bias finding is a
connector-specific `published_bias` attribute with `applied=False`, not a
per-pixel bias model. NGL preserves `reported_fields` and its explicit gradient
direction correction. CAMS pressure belongs to `G-MOD`, not an independent
observation arm.

## Connector release notes

- OLCI: adds public CDSE discovery of `OL_2_LFR___`, authenticated selective
  IWV/annotation downloads through CDSETool, packed NetCDF parsing, line times,
  source QA, random error and the stated wet-bias finding. CDSE processor and
  baseline versions are preserved. No published prior/averaging kernel is
  present. See [OLCI terms](../src/spectraccess/connectors/olci_cdse/DATA_TERMS.md).
- NGL: adds credential-free station/year archive access, day selection and
  SINEX ZTD/gradient parsing, precise station metadata, formal standard errors
  and explicit handling of the provider's swapped-gradient warning. Processing
  software/reference frame come from the source. No water-vapour conversion
  or published prior/averaging kernel is supplied. Source licence clarification
  remains a provider matter. See [NGL terms](../src/spectraccess/connectors/ngl_gnss/DATA_TERMS.md).
- CAMS: adds regional, single-epoch EAC4 `surface_pressure` on ADS and canonical
  Pa rows, keeping the existing three-variable asset retrieval unchanged.
  Uncertainty/prior/averaging kernel remain unknown or absent. No CDS fallback
  is needed because ADS lists pressure. See [CAMS terms](../src/spectraccess/connectors/cams/DATA_TERMS.md).

Versioned release entries are generated by release-please from Conventional
Commits. Weekly live checks distinguish SKIP from verification: OLCI catalogue
and authenticated tiny-manifest access skip without CDSE credentials; pressure
skips without ADS_TOKEN. NGL needs no secret and checks a small pinned archive.
