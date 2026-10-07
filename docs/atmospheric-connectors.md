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
# Run spectraccess login cdse, or pass credentials= with a Credential source.
if targets:
    raw = olci.fetch(targets[0], dest="./olci")
    iwv = olci.parse_canonical(raw, bbox=(4.0, 51.5, 5.0, 52.5))

ngl = NGLGNSSConnector()
target = ngl.discover(station="ABMF", day=date(2008, 9, 2))[0]
delays = ngl.parse_canonical(ngl.fetch(target))

# Run spectraccess login ads, or pass a token Credential source.
# Accept the EAC4 terms in ADS first.
cams = CAMSConnector(source="ads")
raw = cams.fetch_surface_pressure(
    valid_time=datetime(2024, 5, 1, 9, tzinfo=timezone.utc),
    area=(52.5, 4.0, 51.5, 5.0), dest="./pressure.nc",
)
pressure = cams.parse_surface_pressure(raw)
```

NGL delivers ZTD and tropospheric gradients in metres. OLCI IWV is kg m-2;
CAMS pressure is Pa.
The pressure request uses an exact EAC4 epoch; choose the epoch deliberately
instead of silently rounding an acquisition time.

## Optional observation contract

`SCHEMA_VERSION` stays `1.0`. `CANONICAL_COLUMNS` and `empty_frame()` retain
the original required-column shape. `OBSERVATION_COLUMNS` registers the
optional fields below; `validate()` checks them when present. An absent or
null optional value means unknown. An empty published list means a
known empty list; it is not substituted for an unknown one.

| Fields | Contract |
| --- | --- |
| `valid_time`, `integration_start`, `integration_end` | UTC observation time and published integration-window endpoints; cadence and scene duration do not establish an integration window |
| `footprint_geometry`, `support_kind`, `elevation_m` | GeoJSON geometry in longitude/latitude degrees, support `point`, `pixel`, `grid cell` or `swath`, and source support elevation in metres with its datum retained in QA |
| `assimilated_inputs` | Input identifiers listed by the provider |
| `retrieval_prior` | Published prior description/source metadata mapping |
| `prior_state`, `prior_covariance`, `averaging_kernel` | Published numerical arrays in the source state basis; labels, units, dimensions and reference belong in `retrieval_prior`; absent when unpublished |
| `unc_definition`, `assumptions` | Provider-stated uncertainty meaning and list of spectrAccess `AssumptionRecord` objects |
| `qa`, `algorithm_version`, `collection_version` | Source quality mapping, processor version and collection identity |

The new fields do not alter `Uncertainty` status rules. A source-supplied
formal error remains `provided`; a missing error remains `unknown`.
Numerical prior metadata is carried only when published, with no attempt to
construct an averaging kernel from an algorithm description.

OLCI retains pixel centres in `pixel_center_geometry` and the catalogue's
swath in `product_footprint`; neither is substituted for unknown pixel
boundaries. The published positive 7 to 10 percent wet-bias finding is a
connector-specific `published_bias` attribute with its reference, scope and
`applied=False`. NGL preserves `reported_fields` and its explicit gradient
direction correction. CAMS pressure includes the published grid coordinates
and file metadata.

## Connector release notes

- OLCI: adds public CDSE discovery of `OL_2_LFR___`, authenticated selective
  IWV/annotation downloads through CDSETool, packed NetCDF parsing, line times,
  source QA, published `IWV_unc` and its definition, with a null coverage
  factor, and the stated wet-bias finding. CDSE processor and
  baseline versions are preserved. No published prior/averaging kernel is
  present. See [OLCI terms](../src/spectraccess/connectors/olci_cdse/DATA_TERMS.md).
- NGL: adds credential-free station/year archive access, day selection and
  SINEX ZTD/gradient parsing, precise station metadata, formal errors
  and explicit handling of the provider's swapped-gradient warning. Processing
  software/reference frame come from the source. No water-vapour conversion
  or published prior/averaging kernel is supplied. Source licence clarification
  remains a provider matter. See [NGL terms](../src/spectraccess/connectors/ngl_gnss/DATA_TERMS.md).
- CAMS: adds regional, single-epoch EAC4 `surface_pressure` on ADS and canonical
  Pa rows, keeping the existing three-variable asset retrieval unchanged.
  Uncertainty/prior/averaging kernel remain unknown or absent. No CDS fallback
  is needed because ADS lists pressure. See [CAMS terms](../src/spectraccess/connectors/cams/DATA_TERMS.md).

Versioned release entries are generated by release-please from Conventional
Commits. Credentialed live checks run in a private workflow and distinguish
SKIP from verification: OLCI filtered product fetch
and pixel parsing skip without CDSE credentials; pressure
skips without ADS_TOKEN. NGL needs no secret and checks a small pinned archive.
