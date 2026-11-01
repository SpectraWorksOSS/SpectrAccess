# Metop IASI L1C

On Python 3.11+, install `pip install "spectraccess[iasi]"`. This extra pins EUMETSAT MetopPy
0.2.0 and uses EUMDAC 3.x for access. MetopPy wraps MetopDatasets.jl, rather
than implementing another EPS native decoder. It requires Julia and is still
an early API. Satpy 0.60.0 has no IASI L1C reader. An ecCodes BUFR route is not
used because this collection serves EPS native; no equivalent all-sample BUFR
or Data Tailor conversion route has been verified.

For Linux containers, install CA certificates and allow JuliaPkg to download
Julia and its binary/package dependencies at image build time. Run
`python -c "from metoppy import MetopReader; MetopReader()"` during the build,
under the same user/environment used by the job. Keep the resulting Julia
depot and environment, including Manifest.toml, in the image and pin the image
digest. The Python pin alone does not pin the transitive Julia package graph:
MetopPy requests Julia ^1.10 and MetopDatasets ^0.2.3. Local verification used
Julia 1.11.9, MetopDatasets 0.2.5 and juliacall 0.9.37. No system ecCodes or
hand-installed NetCDF library is needed; Julia obtains binary dependencies.
First initialization downloads and compiles substantial dependencies, so do
not leave it for a credential-bearing job's first product read.

```python
from spectraccess import Credential
from spectraccess.connectors.iasi_eumetsat import IASIConnector

# Resolve secrets in Python from the job secret store, never CLI arguments.
connector = IASIConnector(credentials=credential_source)
targets = connector.discover(
    bbox=(-10, 40, 25, 60), start="2024-08-22T00:00:00Z",
    end="2024-08-23T00:00:00Z", limit=2,
)
result = connector.fetch(targets[0], dest="data/iasi")
sample = connector.read(result, sample_size=1000, seed=20261101,
                        strata=lambda m: m["is_day"])
```

`credentials=` follows the shared EUMETSAT credential provider: an explicit
Credential, a zero-argument callback, or the operating-system
keyring. EUMDAC receives explicit key/secret pairs. Discovery and fetch each
resolve credentials, so callback rotation takes effect at each operation.
See [credentials](credentials.md) and
[source terms](../src/spectraccess/connectors/iasi_eumetsat/DATA_TERMS.md).

`discover(*, bbox=None, start, end, limit=10)` searches
`EO:EUM:DAT:METOP:IASIL1C-ALL` and returns `IASITarget` records, retaining raw
catalogue metadata and the requested bbox. Limit bounds product count; None
explicitly requests all. Searches should use short time windows (the provider
advertises a maximum period shorter than a year). Bboxes cannot cross the
antimeridian; issue two queries for that case. Provider footprint intersection
does not guarantee any selected FOV falls in the bbox.

`fetch(target, *, dest=None, credentials=None)` streams a whole product ZIP to
disk, then its native member to a local `IASIResult`. It does not perform remote
spatial or spectral subsetting. Allow several GB of disk per full orbit.

`read(result, *, bbox=None, sample_size=1000, seed=0, strata=None)` (also
`parse`) returns an xarray Dataset with at most sample_size observations.
The target bbox applies unless overridden. Metadata is scanned one MDR at a
time; only the retained FOV spectra are decoded. A caller-supplied
`strata(metadata)` callback sees flattened arrays for one MDR, returns one
label per FOV, and can combine latitude, fractions, SZA or VZA into labels.
The seeded reservoir assigns equal quotas among encountered labels, with
remainder slots assigned in sorted string-label order. Sparse strata leave
unused slots. Population counts after bbox selection appear in
`stratum_population`; no approved scientific population weighting is implied.

| Field | Meaning and units |
| --- | --- |
| radiance | Calibrated GS1cSpect, W m-2 sr-1 (cm-1)-1; reader GIADR scaling followed by multiplication by 100 from per m-1 |
| wavenumber | Product-declared grid, cm-1 (normally 645 to 2760 at 0.25 spacing); unused native storage excluded |
| longitude, latitude | Degrees; longitude normalized to [-180, 180) |
| satellite_zenith, satellite_azimuth | Product Metop angles, degrees |
| solar_zenith, solar_azimuth | Product surface solar angles, degrees |
| timestamp | GEPSDatIasi corrected UTC per scan position, shared by its four sounder pixels |
| cloud_fraction, land_fraction | AVHRR fractions, percent; neither is a thin-ice label |
| quality, quality_detailed, avhrr_quality | Unmodified reader-decoded system, detailed and AVHRR flags |
| degraded_inst_mdr, degraded_proc_mdr | Provider instrument and processing degradation flags for the MDR |
| is_day | Derived SZA < 90 degrees, 1 day / 0 night / NaN missing |
| noise_covariance_metadata | Product IDefCovarMatEigenVal1c metadata; not the full spectral covariance matrix |
| record, sounder, xtrack, stratum | Zero-based native indices and selected stratum |

Dataset attrs include native spacecraft ID, catalogue platform, collection,
product ID, metadata URL, retrieval time, reader and sampling seed. Missing
values follow the maintained reader's NaN masking. Negative radiance and all
quality flags are retained; callers must choose and count quality exclusions.
No resampling or implicit quality screening occurs. Selected records with
different spectral grids raise instead of silently interpolating. Correlated
IASI spectral noise must not be treated as independent channel noise.

Run the offline entrance test after installing the extra:
`python -m pytest tests/test_iasi_eumetsat.py -q`. It mocks HTTP only and passes
synthetic EPS v11 bytes through real EUMDAC and real MetopPy/MetopDatasets.
The generator contains no measured observations. Julia dependencies must
already be initialized for an offline run. Without metoppy, the physical
reader test skips; a skip is not reader proof. A synthetic fixture does not
establish live product compatibility. Validate discover, fetch and read on a
real product in the target Linux image before using the spectra as scientific
evidence.
