"""EUMDAC transport and bounded MetopPy native IASI L1C reads."""
from __future__ import annotations

import hashlib
import itertools
import shutil
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import numpy as np
import xarray as xr

from spectraccess.connectors._eumetsat import _auth_values, _eumdac_context, _provider_failure
from spectraccess.core.connector import Connector
from spectraccess.core.credentials import resolve

COLLECTION = "EO:EUM:DAT:METOP:IASIL1C-ALL"


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _bbox(value):
    if value is None:
        return None
    w, s, e, n = map(float, value)
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError("bbox must be (west, south, east, north), without crossing the antimeridian")
    return w, s, e, n


@dataclass(frozen=True)
class IASITarget:
    product_id: str
    platform: str | None
    sensing_start: datetime
    sensing_end: datetime
    source_url: str
    raw: dict
    bbox: tuple | None = None
    collection: str = COLLECTION


@dataclass(frozen=True)
class IASIResult:
    path: Path
    target: IASITarget
    retrieved_at: datetime


class IASIConnector(Connector):
    """Discover, fetch, then sample measured spectra without loading an orbit.

    Install spectraccess[iasi]. MetopPy/Julia is imported only by read().
    Whole native products are downloaded; bounding applies to decoded memory.
    """
    credential_provider = "eumetsat"

    def __init__(self, *, credentials=None):
        self.credentials = credentials

    def _store(self, credentials=None):
        import eumdac

        credential = resolve("eumetsat", self.credentials if credentials is None else credentials)
        token, failure = None, None
        try:
            token = eumdac.AccessToken((credential.account, credential.secret))
            with _eumdac_context(credential, token):
                str(token)
            return eumdac.DataStore(token)
        except Exception as exc:
            failure = _provider_failure(exc, "token authentication", values=_auth_values(credential, token))
        raise failure from None

    def discover(self, *, bbox=None, start, end, limit=10):
        """Search intersecting product footprints; bbox also filters FOVs at read.

        limit bounds products (default 10); None explicitly requests all.
        """
        bbox, start, end = _bbox(bbox), _utc(start), _utc(end)
        if end < start or (limit is not None and (type(limit) is not int or limit < 0)):
            raise ValueError("end must be >= start; limit must be a nonnegative integer or None")
        if limit == 0:
            return []
        store, targets, failure = self._store(), [], None
        with _eumdac_context(token=store.token):
            try:
                filters = {"dtstart": start, "dtend": end}
                if bbox is not None:
                    filters["bbox"] = ",".join(map(str, bbox))
                found = store.get_collection(COLLECTION).search(**filters)
                for product in itertools.islice(found, limit):
                    raw = deepcopy(product.metadata)
                    props = raw["properties"]
                    begin, finish = props["date"].split("/")
                    acquisitions = props.get("acquisitionInformation", [])
                    platform = acquisitions[0].get("platform", {}).get("platformShortName") if acquisitions else None
                    url = (f"https://api.eumetsat.int/data/download/1.0.0/collections/{quote(COLLECTION, safe='')}"
                           f"/products/{quote(str(product), safe='')}/metadata?format=json")
                    targets.append(IASITarget(str(product), platform, _utc(begin), _utc(finish), url, raw, bbox))
            except Exception as exc:
                failure = _provider_failure(exc, "catalogue query", COLLECTION, values=_auth_values(token=store.token))
        if failure is not None:
            raise failure from None
        return targets

    def fetch(self, target, *, dest=None, credentials=None):
        """Stream the product ZIP to disk and extract its sole native member."""
        store = self._store(credentials)
        folder = Path(dest or "data/iasi") / hashlib.sha256(target.product_id.encode()).hexdigest()[:20]
        folder.mkdir(parents=True, exist_ok=True)
        archive, partial, output = (folder / name for name in ("product.zip.part", "product.nat.part", "product.nat"))
        failure = None
        with _eumdac_context(token=store.token):
            try:
                with store.get_product(target.collection, target.product_id).open() as source, archive.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                with zipfile.ZipFile(archive) as zipped:
                    entries = [n for n in zipped.namelist() if n.lower().endswith(".nat")]
                    if len(entries) != 1:
                        raise ValueError("expected exactly one native .nat member")
                    with zipped.open(entries[0]) as source, partial.open("wb") as sink:
                        shutil.copyfileobj(source, sink)
                partial.replace(output)
            except Exception as exc:
                failure = _provider_failure(exc, "product fetch", COLLECTION, values=_auth_values(token=store.token))
            finally:
                archive.unlink(missing_ok=True)
                partial.unlink(missing_ok=True)
        if failure is not None:
            raise failure from None
        return IASIResult(output, target, datetime.now(timezone.utc))

    def read(self, result, *, bbox=None, sample_size=1000, seed=0, strata=None):
        """Return a bounded xarray sample; strata(metadata) returns one label/FOV.

        A seeded reservoir gives each observed stratum an equal quota, with
        deterministic label ordering when sample_size is smaller than the
        number of strata. Metadata is scanned one MDR (120 FOVs) at a time;
        only selected spectra are decoded. Quotas are sample_size // strata
        count, with the remainder assigned in sorted label order. Sparse
        strata are retained without filling their unused quota from others.
        No quality exclusions are implicit. Day means solar zenith < 90 deg.
        """
        if type(sample_size) is not int or sample_size <= 0:
            raise ValueError("sample_size must be a positive integer")
        bbox = _bbox(result.target.bbox if bbox is None else bbox)
        from metoppy import MetopReader

        reader, rng = MetopReader(), np.random.default_rng(seed)
        # Algorithm R with a globally bounded, dynamically rebalanced quota.
        reservoirs, seen = {}, {}
        with reader.dataset(str(result.path), maskingvalue=float("nan"), high_precision=True) as native:
            def array(name, index):
                value = native[name][index]
                return np.asarray(value if np.isscalar(value) else reader.as_array(value))

            records = reader.shape(native["gs1cspect"])[-1]
            for record in range(records):
                loc = array("ggeosondloc", (slice(None), slice(None), slice(None), record))
                view = array("ggeosondanglesmetop", (slice(None), slice(None), slice(None), record))
                sun = array("ggeosondanglessun", (slice(None), slice(None), slice(None), record))
                meta = {"longitude": ((loc[0] + 180) % 360 - 180).reshape(-1), "latitude": loc[1].reshape(-1),
                        "satellite_zenith": view[0].reshape(-1), "satellite_azimuth": view[1].reshape(-1),
                        "solar_zenith": sun[0].reshape(-1), "solar_azimuth": sun[1].reshape(-1)}
                for output, source in (("cloud_fraction", "geumavhrr1bcldfrac"), ("land_fraction", "geumavhrr1blandfrac"),
                                       ("avhrr_quality", "geumavhrr1bqual"), ("quality_detailed", "gqisflagqualdetailed")):
                    meta[output] = array(source, (slice(None), slice(None), record)).reshape(-1)
                quality = array("gqisflagqual", (slice(None), slice(None), slice(None), record))
                times = array("gepsdatiasi", (slice(None), record))
                for name in ("degraded_inst_mdr", "degraded_proc_mdr"):
                    meta[name] = np.full(len(meta["latitude"]), array(name, record).item())
                meta["is_day"] = np.where(np.isfinite(meta["solar_zenith"]), (meta["solar_zenith"] < 90).astype(float), np.nan)
                labels = np.asarray(strata(meta) if strata else ["all"] * len(meta["latitude"]))
                if labels.shape != meta["latitude"].shape:
                    raise ValueError("strata must return one label per FOV")
                for point, label in enumerate(labels):
                    if not np.isfinite(meta["latitude"][point]) or not np.isfinite(meta["longitude"][point]):
                        continue
                    if bbox and not (bbox[0] <= meta["longitude"][point] <= bbox[2] and bbox[1] <= meta["latitude"][point] <= bbox[3]):
                        continue
                    label = str(label)
                    if label not in seen:
                        seen[label], reservoirs[label] = 0, []
                        for i, key in enumerate(sorted(seen)):
                            quota = sample_size // len(seen) + (i < sample_size % len(seen))
                            # Random thinning preserves each reservoir's uniformity.
                            bucket = reservoirs[key]
                            if len(bucket) > quota:
                                reservoirs[key] = [bucket[j] for j in rng.choice(len(bucket), quota, replace=False)]
                    keys = sorted(seen)
                    quota = sample_size // len(keys) + (keys.index(label) < sample_size % len(keys))
                    seen[label] += 1
                    sounder, xtrack = np.unravel_index(point, loc.shape[1:])
                    row = {key: value[point] for key, value in meta.items()}
                    row.update(record=record, sounder=sounder, xtrack=xtrack, stratum=label,
                               quality=quality[:, sounder, xtrack], timestamp=str(times[xtrack]))
                    bucket = reservoirs[label]
                    if len(bucket) < quota:
                        bucket.append(row)
                    elif quota:
                        j = rng.integers(seen[label])
                        if j < quota:
                            bucket[j] = row
            rows = [r for key in sorted(reservoirs) for r in reservoirs[key]]
            if not rows:
                raise ValueError("no IASI FOVs intersect the requested bbox")
            spectra, grids, covariance = [], [], []
            for row in rows:
                r, s, x = row["record"], row["sounder"], row["xtrack"]
                # The native 8700-element storage has unused trailing slots.
                # Keep the product-declared first..last samples, not padding.
                first = int(array("idefnsfirst1b", r))
                last = int(array("idefnslast1b", r))
                count = last - first + 1
                grid = array("spectra_wavenumber", (slice(0, count), r)) / 100
                grids.append(grid)
                spectra.append(array("gs1cspect", (slice(0, count), s, x, r)) * 100)
                covariance.append(array("idefcovarmateigenval1c", (slice(None), slice(None), r)))
            if any(not np.array_equal(grids[0], g) for g in grids[1:]):
                raise ValueError("selected FOVs have different wavenumber grids")
            platform = str(native.main_product_header.spacecraft_id)
        ds = xr.Dataset({key: ("observation", [row[key] for row in rows]) for key in
                         ("longitude", "latitude", "satellite_zenith", "satellite_azimuth", "solar_zenith", "solar_azimuth",
                          "cloud_fraction", "land_fraction", "avhrr_quality", "quality_detailed", "degraded_inst_mdr", "degraded_proc_mdr",
                          "is_day", "stratum", "record", "sounder", "xtrack")},
                        coords={"wavenumber": grids[0]})
        ds["timestamp"] = ("observation", np.array([r["timestamp"] for r in rows], dtype="datetime64[ms]"))
        ds["radiance"] = (("observation", "wavenumber"), np.stack(spectra))
        ds["quality"] = (("observation", "quality_band"), np.stack([r["quality"] for r in rows]))
        ds["noise_covariance_metadata"] = (("observation", "covariance_component", "eigenvalue"), np.stack(covariance))
        ds.radiance.attrs = {"units": "W m-2 sr-1 (cm-1)-1", "source_variable": "GS1cSpect", "calibration": "MetopDatasets per-channel GIADR scaling; per m-1 multiplied by 100"}
        ds.wavenumber.attrs["units"] = "cm-1"
        for name in ("latitude", "longitude", "satellite_zenith", "satellite_azimuth", "solar_zenith", "solar_azimuth"):
            ds[name].attrs["units"] = "degrees"
        for name in ("cloud_fraction", "land_fraction"):
            ds[name].attrs["units"] = "%"
        ds.timestamp.attrs = {"source_variable": "GEPSDatIasi", "timezone": "UTC", "support": "corrected UTC per scan position, shared by four sounder pixels"}
        ds.is_day.attrs["definition"] = "solar_zenith < 90 degrees; NaN when solar zenith missing"
        ds.noise_covariance_metadata.attrs = {"source_variable": "IDefCovarMatEigenVal1c", "description": "provider covariance metadata, not a full spectral covariance matrix"}
        ds.attrs = {"platform": platform, "catalogue_platform": result.target.platform or "", "sensor": "IASI", "source_agency": "EUMETSAT",
                    "collection": result.target.collection, "product_id": result.target.product_id, "source_url": result.target.source_url,
                    "retrieved_at": result.retrieved_at.isoformat(), "reader": "metoppy 0.2.0 / MetopDatasets.jl", "sample_seed": seed,
                    "stratum_population": seen, "quality_exclusions": "none; provider flags retained", "indices": "zero-based native record, sounder and xtrack"}
        return ds

    def parse(self, raw, **kwargs):
        return self.read(raw, **kwargs)
