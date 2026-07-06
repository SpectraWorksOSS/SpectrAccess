from __future__ import annotations

import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path
from typing import Iterable

import pandas as pd

from spectraccess.connectors.thredds import ThreddsCatalogRef, ThreddsDataset, walk_catalog
from spectraccess.core.connector import Connector
from spectraccess.core.fetch import fetch_url
from spectraccess.core.schema import Uncertainty, UncertaintyStatus, empty_frame, uncertainty_columns, validate

# Native GSICS value column -> (canonical quantity name, matching stderr column)
_CANONICAL_QUANTITIES: tuple[tuple[str, str, str], ...] = (
    ("slope", "gsics_correction_slope", "slope_se"),
    ("offset", "gsics_correction_offset", "offset_se"),
    ("std_scene_tb_bias", "gsics_std_scene_tb_bias", "std_scene_tb_bias_se"),
)


def _tokenize(text: str) -> frozenset[str]:
    return frozenset(t for t in re.split(r"[^a-z0-9]+", text) if t)


@dataclass(frozen=True)
class GSICSCatalog:
    agency: str
    url: str | None


DEFAULT_CATALOGS: tuple[GSICSCatalog, ...] = (
    # Verified live 2026-07-05: valid THREDDS InvCatalog 1.0.1, name
    # "EUMETSAT GSICS THREDDS Server Master Catalog".
    GSICSCatalog("EUMETSAT", "https://gsics.eumetsat.int/thredds/catalog.xml"),
    # Documented canonical URL is https://www.star.nesdis.noaa.gov/thredds/gsics/catalog.xml
    # but as of 2026-07-05 the host is unreachable (connection-level failure,
    # likely WAF/outage, confirmed from both EU and US vantage points). Kept
    # disabled (url=None) until reachability is reconfirmed. Note: NESDIS
    # products are also mirrored on the EUMETSAT collaboration server's master
    # catalog (see its `nesdisProducts.xml` catalogRef), so NOAA/NESDIS product
    # families may already be reachable indirectly via the EUMETSAT catalog.
    GSICSCatalog("NOAA STAR", None),
    # Verified live 2026-07-05: valid catalog, name
    # "CMA GSICS THREDDS Server Master Catalog". NOTE: the catalog tree is
    # currently a content-empty skeleton -- every product-family leaf catalog
    # sampled (FY4A/FY4B AGRI, FY2G VISSR; demo/preop/oper streams) is valid
    # THREDDS XML with metadata but ZERO file datasets (no urlPath entries,
    # no child catalogRefs), verified 2026-07-05. Kept as a live default so
    # discovery picks products up automatically if/when CMA populates them.
    GSICSCatalog("CMA", "https://gsics.nsmc.org.cn/thredds/catalog.xml"),
)


class GSICSConnector(Connector):
    """Connector for GSICS GPPA correction coefficient products."""

    def __init__(self, catalogs: Iterable[GSICSCatalog] | None = None, *, cache_dir: str | Path | None = None) -> None:
        self.catalogs = tuple(catalogs or DEFAULT_CATALOGS)
        self.cache_dir = cache_dir

    def discover(self, **kwargs: object) -> list[ThreddsDataset]:
        missing = [catalog.agency for catalog in self.catalogs if catalog.url is None]
        if missing and len(missing) == len(self.catalogs):
            raise NotImplementedError(
                "STOPPED-AT-STUB: GSICS GPPA live THREDDS catalog URLs require verification "
                f"for {', '.join(missing)}"
            )

        walk_kwargs = dict(kwargs)
        contains = walk_kwargs.pop("contains", None)
        max_results = walk_kwargs.pop("max_results", None)

        # Real GSICS catalogs fan out through several container hops (source
        # -> per-agency products -> per-sensor-pair products -> per-
        # processing-stream channel catalog) with dozens of sibling refs at
        # each hop (e.g. every monitored/reference sensor pair sits under the
        # same "<agency>Products.xml" listing). A plain substring test lets
        # every sibling through once the needle matches loosely (e.g. any ref
        # containing "seviri" or "metopb" alone), which reintroduces the same
        # fan-out the filter is meant to prevent. Instead, split the needle on
        # non-alphanumeric characters into tokens (e.g. "msg4-seviri-metopb-
        # iasi" -> {"msg4", "seviri", "metopb", "iasi"}) and only follow a ref
        # once ALL of those tokens appear in its name/href -- that uniquely
        # identifies one sensor pair's branch. Refs that haven't specialized
        # into any sensor pair yet (a generic "<agency>Products.xml" listing)
        # are also followed, since the walk must pass through them to reach
        # the matching branch.
        ref_filter = None
        needle_tokens: frozenset[str] = frozenset()
        if contains:
            needle_tokens = _tokenize(str(contains).lower())

            def ref_filter(ref: ThreddsCatalogRef, _needle_tokens: frozenset = needle_tokens) -> bool:
                tokens = _tokenize(f"{ref.name} {ref.href}".lower())
                if _needle_tokens <= tokens:
                    return True
                # A generic "products" listing (no needle tokens matched at
                # all yet) hasn't specialized into any sensor pair -- keep
                # following it. Once a ref carries SOME but not all needle
                # tokens, it has specialized into a different, non-matching
                # sensor pair (e.g. "msg1-seviri-metopb-iasi" when the needle
                # is "msg4-seviri-metopb-iasi") and must be pruned. Do NOT
                # fall back on bare "source"/"catalog" container terms: the
                # EUMETSAT master catalog's "...Source.xml"/"...Intermediate.xml"
                # branches are huge (tens of thousands of raw-instrument-data
                # leaves, not GPPA products) and starve the walk's
                # max_catalogs budget before it ever reaches a matching leaf --
                # verified empirically against the live catalog on 2026-07-05.
                return not (tokens & _needle_tokens) and ref.href.lower().endswith("products.xml")

        targets: list[ThreddsDataset] = []
        skipped = [c.agency for c in self.catalogs if c.url is None]
        if skipped:
            warnings.warn(
                f"GSICS catalogs skipped (no verified URL configured): {', '.join(skipped)}",
                stacklevel=2,
            )
        for catalog in self.catalogs:
            if catalog.url is None:
                continue
            try:
                found = walk_catalog(
                    catalog.url,
                    ref_filter=ref_filter,
                    source_agency=catalog.agency,
                    cache_dir=self.cache_dir,
                    **walk_kwargs,
                )
            except Exception as exc:  # noqa: BLE001 -- isolate per-catalog outages
                # One agency's outage must not sink results from the others
                # (e.g. a CMA outage should not hide live EUMETSAT data).
                warnings.warn(
                    f"GSICS catalog {catalog.agency!r} discovery failed and was skipped: {exc}",
                    stacklevel=2,
                )
                continue
            if contains:
                found = [d for d in found if needle_tokens <= _tokenize(d.name.lower())]
            targets.extend(found)

        if max_results is not None:
            targets = targets[: int(max_results)]
        return targets

    def fetch(self, target: ThreddsDataset | str, **kwargs: object) -> bytes:
        url = target.access_url if isinstance(target, ThreddsDataset) else target
        if not url:
            raise ValueError("target does not include an access URL")
        return fetch_url(url, cache_dir=self.cache_dir, **kwargs)

    def parse(self, raw: bytes | str, *, source_agency: str | None = None) -> pd.DataFrame:
        payload = _read_payload(raw)
        if _looks_like_netcdf(payload):
            return _parse_netcdf(payload, source_agency=source_agency)
        table = _read_table(payload)
        return _normalize_gsics_table(table)

    def parse_canonical(
        self,
        raw: bytes | str,
        *,
        source_agency: str | None = None,
        source_url: str | None = None,
    ) -> pd.DataFrame:
        """Parse raw GSICS content directly into the canonical schema.

        Equivalent to ``to_canonical(self.parse(raw, source_agency=...), source_url=...)``.
        """

        native = self.parse(raw, source_agency=source_agency)
        return to_canonical(native, source_url=source_url)


def to_canonical(
    frame: pd.DataFrame,
    *,
    source_url: str | None = None,
    retrieved_at=None,
) -> pd.DataFrame:
    """Melt a native GSICS GPPA frame (one row per channel x date) into the
    spectrAccess canonical long/tidy schema (one row per quantity value).

    Handles the netCDF-shaped native frame produced by `_parse_netcdf`
    (columns `timestamp, sensor, reference_sensor, band, source_agency,
    central_wavelength?, slope, slope_se, offset, offset_se,
    std_scene_tb_bias, std_scene_tb_bias_se`); any of the measure columns may
    be absent. CSV-fallback frames (from `_normalize_gsics_table`) lack the
    `_se` columns and a different value column shape, so they melt to
    all-`unknown` uncertainty rows where a recognised quantity column exists.

    Rows where the quantity value itself is null are skipped (no value, no
    row). The returned frame is always schema-valid (passed through
    `validate()`).
    """

    if frame.empty:
        return empty_frame()

    rows: list[dict[str, object]] = []
    for _, native_row in frame.iterrows():
        for value_column, quantity_name, se_column in _CANONICAL_QUANTITIES:
            if value_column not in frame.columns:
                continue
            value = native_row[value_column]
            if pd.isna(value):
                continue

            se_value = None
            if se_column in frame.columns:
                candidate = native_row[se_column]
                if pd.notna(candidate):
                    se_value = float(candidate)

            if se_value is not None:
                unc = Uncertainty(value=se_value, status=UncertaintyStatus.PROVIDED, k=1.0, provider="source")
            else:
                unc = Uncertainty(value=None, status=UncertaintyStatus.UNKNOWN)

            row: dict[str, object] = {
                "time": native_row.get("timestamp"),
                "platform": None,
                "instrument": native_row.get("sensor"),
                "band": native_row.get("band"),
                "wavelength_nm": None,
                "site": None,
                "latitude": None,
                "longitude": None,
                "reference": native_row.get("reference_sensor"),
                "quantity": quantity_name,
                "value": float(value),
                "units": None,
                "source": "gsics",
                "source_agency": native_row.get("source_agency"),
                "source_url": source_url,
                "retrieved_at": retrieved_at,
            }
            row.update(uncertainty_columns(unc))
            if "central_wavelength" in frame.columns:
                row["central_wavelength"] = native_row.get("central_wavelength")
            rows.append(row)

    if not rows:
        return empty_frame()

    long_frame = pd.DataFrame(rows)
    return validate(long_frame)


def _read_payload(raw: bytes | str) -> bytes:
    if isinstance(raw, bytes):
        return raw
    path = Path(raw)
    if path.exists():
        return path.read_bytes()
    return raw.encode("utf-8")


def _looks_like_netcdf(payload: bytes) -> bool:
    return payload[:3] == b"CDF" or payload[:4] == b"\x89HDF"


def _parse_netcdf(payload: bytes, *, source_agency: str | None = None) -> pd.DataFrame:
    import xarray as xr

    is_classic = payload[:3] == b"CDF"

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as tmp:
            tmp.write(payload)
            tmp_path = tmp.name

        dataset = None
        errors: list[str] = []
        if is_classic:
            try:
                dataset = xr.open_dataset(tmp_path, engine="scipy")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"scipy engine failed: {exc}")
        if dataset is None:
            try:
                dataset = xr.open_dataset(tmp_path)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"default engine failed: {exc}")
                raise RuntimeError(
                    "Could not open GSICS netCDF payload. If this is an HDF5-based "
                    "netCDF4 file, install the optional 'h5netcdf' or 'netCDF4' "
                    f"package to add a compatible xarray backend. Details: {'; '.join(errors)}"
                ) from exc

        with dataset:
            return _dataset_to_frame(dataset, source_agency=source_agency)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _decode_channel_name(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip("\x00").strip()
    return str(value).strip()


def _dataset_to_frame(dataset, *, source_agency: str | None = None) -> pd.DataFrame:
    attrs = dataset.attrs
    sensor = attrs.get("monitored_instrument")
    reference_sensor = attrs.get("reference_instrument")
    agency = attrs.get("institution") or source_agency

    if "channel_name" not in dataset.variables:
        raise ValueError(
            "GSICS netCDF payload has no 'channel_name' variable -- not a "
            "recognised GPPA correction product (variables present: "
            f"{sorted(dataset.variables)})"
        )
    channel_names = [_decode_channel_name(v) for v in dataset["channel_name"].values]
    n_chan = len(channel_names)

    has_date_dim = "date" in dataset.dims
    n_date = dataset.sizes["date"] if has_date_dim else 1
    dates = dataset["date"].values if has_date_dim else [None]

    per_chan_vars = ["central_wavelength"]
    per_date_chan_vars = [
        "slope",
        "slope_se",
        "offset",
        "offset_se",
        "std_scene_tb_bias",
        "std_scene_tb_bias_se",
    ]

    rows = []
    for date_idx in range(n_date):
        timestamp = dates[date_idx]
        for chan_idx in range(n_chan):
            row = {
                "timestamp": timestamp,
                "sensor": sensor,
                "reference_sensor": reference_sensor,
                "band": channel_names[chan_idx],
                "source_agency": agency,
            }
            for var_name in per_chan_vars:
                if var_name in dataset.variables:
                    row[var_name] = dataset[var_name].values[chan_idx]
            for var_name in per_date_chan_vars:
                if var_name in dataset.variables:
                    var = dataset[var_name]
                    if "date" in var.dims:
                        row[var_name] = var.values[date_idx, chan_idx]
                    else:
                        row[var_name] = var.values[chan_idx]
            rows.append(row)

    frame = pd.DataFrame(rows)
    if "timestamp" in frame:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    return frame


def _read_table(payload: bytes) -> pd.DataFrame:
    text = payload.decode("utf-8", errors="replace")
    if "," in text.splitlines()[0]:
        return pd.read_csv(StringIO(text), comment="#")
    try:
        return pd.read_csv(BytesIO(payload), sep=None, engine="python", comment="#")
    except Exception:
        return pd.read_csv(StringIO(text), delim_whitespace=True, comment="#")


def _normalize_gsics_table(table: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "timestamp": ["timestamp", "valid_time", "time", "date"],
        "sensor": ["sensor", "monitored_sensor", "satellite_pair", "pair"],
        "reference_sensor": ["reference_sensor", "reference", "ref_sensor"],
        "band": ["band", "channel", "ch"],
        "correction_coefficient": ["correction_coefficient", "coefficient", "slope", "value"],
        "source_agency": ["source_agency", "agency"],
    }
    normalized = pd.DataFrame()
    lower_columns = {column.lower().strip(): column for column in table.columns}
    for output, candidates in aliases.items():
        for candidate in candidates:
            source = lower_columns.get(candidate)
            if source is not None:
                normalized[output] = table[source]
                break
    if "timestamp" in normalized:
        normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce")
    if "source_agency" not in normalized:
        normalized["source_agency"] = "GSICS"
    return normalized
