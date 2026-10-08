# Granule observations and native measurements

Connectors following the granule-plus-Dataset pattern, such as SLSTR,
expose two complementary outputs. Existing OLCI instead returns pixel rows.
For the granule pattern, `parse_canonical`
produces a schema-v1 DataFrame with one row per provider granule or
observation. `read` produces an xarray Dataset containing measurements
and annotation fields on their native supports. Join them by product
identity, retaining the acquisition interval and source provenance.

| Granule column | Meaning |
| --- | --- |
| `valid_time`, `time` | Provider acquisition start for a granule; not every pixel's time |
| `integration_start`, `integration_end` | Provider acquisition coverage interval, not an assumed pixel exposure |
| `footprint_geometry` | Provider footprint as GeoJSON; absent when unavailable |
| `support_kind` | `swath` for granule rows; measurements retain their pixel/grid support |
| `qa` | Provider granule QA only; pixel flags stay in the Dataset |
| `algorithm_version` | Published processor version |
| `collection_version` | Published collection/baseline identifier |
| `unc_definition` | Published uncertainty meaning; may describe arrays or tables rather than a scalar |
| `source_url`, `retrieved_at` | Provider identity URL and retrieval provenance |

A granule row's `value`, `unc_value`, and `unc_k` can be null. That does
not imply zero uncertainty. `unc_status="unknown"` means no scalar estimate
is attached to that row. A provider uncertainty array or lookup table
belongs with the measurement Dataset, preserving its definition and units.
Flags and probabilities must not be relabelled as standard uncertainty.
Version and QA fields remain absent/null when the provider has not supplied
them. No cloud fraction, average, inferred quality verdict or calibrated
correction is added to a granule row.

Native readers keep separate grids, views and segment supports. A bbox
selects a native row/column window or segment slice, never a resampled
grid. Rectangular swath windows can contain centres outside the bbox.
Tie-point geometry can have its own dimensions and extent: consult each
connector's page before associating those angles with measurement pixels.
Preserve longitude/latitude arrays rather than inventing pixel polygons.
Tie-point angle arrays carry their own published coordinates. Non-grid
dimensions are asset-specific, with original provider dimension names in
attrs; matching dimension names in different files do not establish support.

Use provider per-row, per-scan or per-pixel times for temporal joins.
Granule timestamps do not replace those times. Preserve source epochs,
calendars and units in provenance when decoding them. Missing timing must
be explicit, and a time coordinate must retain its native support.

For [SLSTR](slstr-cdse.md), `time_stamp_an` belongs to `rows_an`, whereas
`time_stamp_in` belongs to the thermal `rows_in` grid. Variables retain
provider names, with source file and source variable attrs. Reader name,
version and selected calibration are separately identified provenance;
Satpy-normalized platform or sensor labels are not provider metadata.
Consumers build their own observation records from these outputs, stating
any derived geometry, reflectance, aggregation or QA interpretation they add.
