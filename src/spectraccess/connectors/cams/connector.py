"""CAMS EAC4/JASMIN access with an explicit cache-directory contract.

The connector deliberately stops at data access.  It retrieves the JASMIN
GeoTIFF family or the official ADS EAC4 netCDF through ECMWF's maintained
``cdsapi`` client; downstream atmospheric-correction code remains responsible
for any model-specific format conversion.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pandas as pd

from spectraccess.core.connector import Connector

CAMSMode = Literal["auto", "jasmin", "ads"]
CAMSResolvedSource = Literal["jasmin", "ads", "cache-unknown"]

JASMIN_BASE_URL = "https://gws-access.jasmin.ac.uk/public/nceo_ard/cams/"
ADS_API_URL = "https://ads.atmosphere.copernicus.eu/api"
ADS_DATASET = "cams-global-reanalysis-eac4"
ADS_VARIABLES = (
    "total_aerosol_optical_depth_550nm",
    "total_column_water_vapour",
    "total_column_ozone",
)
SIAC_VARIABLES = ("aod550", "tcwv", "gtco3")
ADS_TIMES = ("00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00")


class CAMSConnectorError(RuntimeError):
    """Base error at the public CAMS connector boundary."""


class CAMSDateUnavailableError(CAMSConnectorError):
    """The requested date is definitively absent from configured sources."""


class CAMSProviderError(CAMSConnectorError):
    """A credential, network, provider, or download failure occurred."""


class CAMSCredentialsError(CAMSProviderError):
    """ADS access was requested but no personal access token was supplied."""


class CAMSADSDateNotFoundError(CAMSProviderError):
    """ADS specifically reported that no data match the requested date."""


@dataclass(frozen=True)
class CAMSTarget:
    """One date/source request crossing the discover-to-fetch boundary."""

    scene_date: datetime
    requested_source: CAMSMode
    cache_root: Path

    @property
    def date_label(self) -> str:
        return self.scene_date.strftime("%Y_%m_%d")


@dataclass(frozen=True)
class CAMSResult:
    """Retrieved CAMS assets with an unambiguous directory contract.

    ``base_dir`` is always the directory *containing* the ``YYYY_MM_DD``
    subtree.  ``date_dir`` is always that subtree itself.  Consumers must not
    infer one from an untyped path string; this distinction prevents the
    doubled-date failure caused by handing a date directory to software that
    appends the date again.
    """

    scene_date: datetime
    requested_source: CAMSMode
    resolved_source: CAMSResolvedSource
    base_dir: Path
    date_dir: Path
    files: tuple[Path, ...]
    source_url: str | None
    retrieved_at: datetime | None
    cache_hit: bool
    dataset: str | None = None

    def __post_init__(self) -> None:
        expected = self.base_dir / self.scene_date.strftime("%Y_%m_%d")
        if self.date_dir != expected:
            raise ValueError(
                f"date_dir must equal base_dir/YYYY_MM_DD ({expected}), got {self.date_dir}"
            )


class CAMSConnector(Connector):
    """Public CAMS access wrapper with JASMIN-preferred ADS fallback.

    ADS credentials are bring-your-own: pass ``ads_token`` or set
    ``ADS_TOKEN``.  Tokens are never stored in results, frames, logs, or error
    messages.  ``auto`` falls back to ADS only after a definitive JASMIN date
    gap; provider/network errors remain hard failures.
    """

    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        source: CAMSMode | None = None,
        ads_token: str | None = None,
        fallback_url: str | None = None,
        max_attempts: int = 4,
        retry_delay_seconds: float = 5.0,
        connect_timeout_seconds: float = 30.0,
        read_timeout_seconds: float = 120.0,
    ) -> None:
        configured = (source or os.environ.get("CAMS_SOURCE", "auto")).strip().lower()
        if configured not in {"auto", "jasmin", "ads"}:
            raise ValueError(f"unsupported CAMS source {configured!r}; use auto, jasmin, or ads")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.source: CAMSMode = configured  # type: ignore[assignment]
        self.cache_dir = Path(
            cache_dir
            or os.environ.get("REFCAL_CAMS_CACHE_DIR", "")
            or Path.home() / ".cache" / "spectraccess" / "cams"
        )
        self.ads_token = (ads_token or os.environ.get("ADS_TOKEN", "")).strip() or None
        self.fallback_url = (
            fallback_url
            if fallback_url is not None
            else os.environ.get("REFCAL_CAMS_FALLBACK_URL", "")
        ).rstrip("/")
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds

    @property
    def ads_available(self) -> bool:
        return bool(self.ads_token)

    def discover(
        self,
        *,
        scene_date: datetime,
        source: CAMSMode | None = None,
        **_kwargs: object,
    ) -> list[CAMSTarget]:
        mode = source or self.source
        if mode not in {"auto", "jasmin", "ads"}:
            raise ValueError(f"unsupported CAMS source {mode!r}")
        return [CAMSTarget(_as_utc(scene_date), mode, self.cache_dir)]

    def fetch(self, target: CAMSTarget, **_kwargs: object) -> CAMSResult:
        if target.requested_source == "jasmin":
            return self._fetch_jasmin(target)
        if target.requested_source == "ads":
            return self._fetch_ads(target)

        try:
            return self._fetch_jasmin(target)
        except CAMSDateUnavailableError as jasmin_error:
            if not self.ads_available:
                raise
            try:
                return self._fetch_ads(target)
            except CAMSADSDateNotFoundError as ads_error:
                raise CAMSDateUnavailableError(
                    f"CAMS {target.date_label} is unavailable from JASMIN and ADS"
                ) from ads_error
            except CAMSProviderError:
                # Credential, transport, provider, and data-integrity failures
                # are not availability signals and must remain loud.
                raise

    def resolve(
        self, scene_date: datetime, *, source: CAMSMode | None = None
    ) -> CAMSResult:
        return self.fetch(self.discover(scene_date=scene_date, source=source)[0])

    def parse(self, raw: CAMSResult, **_kwargs: object) -> pd.DataFrame:
        if not isinstance(raw, CAMSResult):
            raise TypeError("CAMSConnector.parse expects the CAMSResult returned by fetch/resolve")
        rows = []
        for path in raw.files:
            rows.append(
                {
                    "scene_date": raw.scene_date,
                    "requested_source": raw.requested_source,
                    "resolved_source": raw.resolved_source,
                    "dataset": raw.dataset,
                    "source_url": raw.source_url,
                    "retrieved_at": raw.retrieved_at,
                    "cache_hit": raw.cache_hit,
                    "base_dir": str(raw.base_dir),
                    "date_dir": str(raw.date_dir),
                    "local_path": str(path),
                    "asset_name": path.name,
                }
            )
        return pd.DataFrame(rows)

    def _fetch_jasmin(self, target: CAMSTarget) -> CAMSResult:
        date_dir = target.cache_root / target.date_label
        paths = tuple(date_dir / f"{target.date_label}_{name}.tif" for name in SIAC_VARIABLES)
        cache_hit = all(path.exists() for path in paths)
        resolved_source: CAMSResolvedSource
        resolved_url: str | None
        retrieved_at: datetime | None
        if not cache_hit:
            mirrors = [JASMIN_BASE_URL.rstrip("/")]
            if self.fallback_url:
                mirrors.append(self.fallback_url)
            last_error: CAMSProviderError | None = None
            resolved_base: str | None = None
            for base in mirrors:
                try:
                    if not self._date_available(base, target.date_label):
                        continue
                    date_dir.mkdir(parents=True, exist_ok=True)
                    # A partial legacy cache has no trustworthy common origin.
                    # Stage and replace the *whole* family before assigning
                    # definitive JASMIN provenance; never fill only the holes
                    # and launder the old members into the new source label.
                    with tempfile.TemporaryDirectory(
                        dir=date_dir, prefix=".spectraccess-cams-family-"
                    ) as staging_name:
                        staging_dir = Path(staging_name)
                        staged = tuple(staging_dir / path.name for path in paths)
                        for staged_path in staged:
                            self._download(
                                f"{base}/{target.date_label}/{staged_path.name}",
                                staged_path,
                            )
                        for staged_path, path in zip(staged, paths):
                            staged_path.replace(path)
                    resolved_base = base
                    break
                except CAMSProviderError as exc:
                    last_error = exc
            if not all(path.exists() for path in paths):
                if last_error is not None:
                    raise last_error
                raise CAMSDateUnavailableError(
                    f"CAMS {target.date_label} is not published on a configured JASMIN-style mirror"
                )
            manifest = _write_source_manifest(date_dir, "jasmin", resolved_base, paths)
            resolved_source = manifest.resolved_source
            resolved_url = manifest.source_url
            retrieved_at = manifest.recorded_at
        else:
            manifest = _read_source_manifest(
                date_dir,
                expected_source="jasmin",
                expected_assets=tuple(path.name for path in paths),
            )
            if manifest is not None:
                resolved_source = manifest.resolved_source
                resolved_url = manifest.source_url
                retrieved_at = manifest.recorded_at
            else:
                # Pre-spectrAccess shared caches carry no trustworthy source
                # marker. Do not launder path shape into false provenance.
                resolved_source = "cache-unknown"
                resolved_url = None
                retrieved_at = None

            if target.requested_source == "jasmin" and resolved_source != "jasmin":
                raise CAMSProviderError(
                    f"cached CAMS {target.date_label} provenance is {resolved_source!r}, "
                    "not explicit source='jasmin'; use a separate cache or source='auto'"
                )

        return CAMSResult(
            scene_date=target.scene_date,
            requested_source=target.requested_source,
            resolved_source=resolved_source,
            base_dir=target.cache_root,
            date_dir=date_dir,
            files=paths,
            source_url=resolved_url,
            retrieved_at=retrieved_at,
            cache_hit=cache_hit,
        )

    def _fetch_ads(self, target: CAMSTarget) -> CAMSResult:
        if not self.ads_token:
            raise CAMSCredentialsError(
                "ADS personal access token is required; pass ads_token or set ADS_TOKEN"
            )
        date_dir = target.cache_root / target.date_label
        path = date_dir / f"cams_eac4_{target.date_label}.nc"
        cache_hit = path.exists()
        resolved_source: CAMSResolvedSource
        resolved_url: str | None
        retrieved_at: datetime | None
        if not cache_hit:
            date_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.part")
            request = {
                "variable": list(ADS_VARIABLES),
                "date": [target.scene_date.strftime("%Y-%m-%d")],
                "time": list(ADS_TIMES),
                "data_format": "netcdf",
            }
            try:
                client = _cds_client(ADS_API_URL, self.ads_token)
                client.retrieve(ADS_DATASET, request, str(tmp))
                if not tmp.exists() or tmp.stat().st_size == 0:
                    raise CAMSProviderError("ADS retrieval completed without a non-empty output file")
                tmp.replace(path)
                manifest = _write_source_manifest(
                    date_dir,
                    "ads",
                    f"{ADS_API_URL}/retrieve/v1/processes/{ADS_DATASET}",
                    (path,),
                )
                resolved_source = manifest.resolved_source
                resolved_url = manifest.source_url
                retrieved_at = manifest.recorded_at
            except CAMSProviderError:
                tmp.unlink(missing_ok=True)
                raise
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                raise CAMSProviderError(
                    f"ADS retrieval failed for {target.scene_date.date()} ({type(exc).__name__})"
                ) from None
        else:
            manifest = _read_source_manifest(
                date_dir,
                expected_source="ads",
                expected_assets=(path.name,),
            )
            if manifest is None:
                # A filename is not provider authority. Preserve usable legacy
                # bytes without inventing ADS provenance or retrieval time.
                resolved_source = "cache-unknown"
                resolved_url = None
                retrieved_at = None
            else:
                resolved_source = manifest.resolved_source
                resolved_url = manifest.source_url
                retrieved_at = manifest.recorded_at

        return CAMSResult(
            scene_date=target.scene_date,
            requested_source=target.requested_source,
            resolved_source=resolved_source,
            base_dir=target.cache_root,
            date_dir=date_dir,
            files=(path,),
            source_url=resolved_url,
            retrieved_at=retrieved_at,
            cache_hit=cache_hit,
            dataset=ADS_DATASET if resolved_source == "ads" else None,
        )

    def _date_available(self, base: str, date_label: str) -> bool:
        for name in SIAC_VARIABLES:
            url = f"{base}/{date_label}/{date_label}_{name}.tif"
            if not self._probe(url):
                return False
        return True

    def _probe(self, url: str) -> bool:
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                request = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(request, timeout=self.connect_timeout_seconds) as response:
                    if response.status == 200:
                        return True
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return False
                last = exc
            except Exception as exc:
                last = exc
            if attempt + 1 < self.max_attempts:
                time.sleep(min(self.retry_delay_seconds * 2**attempt, 120.0))
        raise CAMSProviderError(
            f"CAMS mirror probe failed after {self.max_attempts} attempts ({type(last).__name__})"
        )

    def _download(self, url: str, dest: Path) -> None:
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".cams-")
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                timeout = max(self.connect_timeout_seconds, self.read_timeout_seconds)
                with urllib.request.urlopen(url, timeout=timeout) as response, tmp.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
                if tmp.stat().st_size == 0:
                    raise OSError("empty download")
                tmp.replace(dest)
                return
            except Exception as exc:
                last = exc
                tmp.unlink(missing_ok=True)
            if attempt + 1 < self.max_attempts:
                time.sleep(min(self.retry_delay_seconds * 2**attempt, 120.0))
        raise CAMSProviderError(
            f"CAMS mirror download failed after {self.max_attempts} attempts ({type(last).__name__})"
        )


def _cds_client(url: str, token: str):
    try:
        import cdsapi
    except ImportError as exc:  # pragma: no cover - package-extra guard
        raise ImportError(
            "CAMS ADS access requires ECMWF's maintained cdsapi client; "
            "install with: pip install 'spectraccess[cams]'"
        ) from exc
    return cdsapi.Client(url=url, key=token, quiet=True)


def _manifest_path(date_dir: Path) -> Path:
    return date_dir / "spectraccess-cams-source.json"


@dataclass(frozen=True)
class _SourceManifest:
    resolved_source: Literal["jasmin", "ads"]
    source_url: str
    assets: tuple[str, ...]
    recorded_at: datetime


def _write_source_manifest(
    date_dir: Path, source: Literal["jasmin", "ads"], source_url: str | None, files: tuple[Path, ...]
) -> _SourceManifest:
    payload = {
        "schema": "spectraccess-cams-source-v1",
        "resolved_source": source,
        "source_url": source_url,
        "assets": [path.name for path in files],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _manifest_path(date_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    manifest = _read_source_manifest(
        date_dir,
        expected_source=source,
        expected_assets=tuple(file.name for file in files),
    )
    if manifest is None:  # pragma: no cover - path was just atomically written
        raise CAMSProviderError("CAMS cache provenance manifest disappeared after write")
    return manifest


def _read_source_manifest(
    date_dir: Path,
    *,
    expected_source: Literal["jasmin", "ads"] | None = None,
    expected_assets: tuple[str, ...] | None = None,
) -> _SourceManifest | None:
    path = _manifest_path(date_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CAMSProviderError(
            f"invalid CAMS cache provenance manifest ({type(exc).__name__})"
        ) from None
    if not isinstance(payload, dict):
        raise CAMSProviderError("CAMS cache provenance manifest must be a JSON object")
    if payload.get("schema") != "spectraccess-cams-source-v1":
        raise CAMSProviderError("unsupported CAMS cache provenance manifest schema")
    source = payload.get("resolved_source")
    if source not in {"jasmin", "ads"}:
        raise CAMSProviderError("invalid CAMS cache provenance resolved_source")
    if expected_source is not None and source != expected_source:
        raise CAMSProviderError(
            f"CAMS cache provenance source {source!r} does not match expected {expected_source!r}"
        )

    source_url = payload.get("source_url")
    if not isinstance(source_url, str) or not source_url.strip():
        raise CAMSProviderError("invalid CAMS cache provenance source_url")
    parsed_url = urllib.parse.urlparse(source_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise CAMSProviderError("invalid CAMS cache provenance source_url")

    raw_assets = payload.get("assets")
    if (
        not isinstance(raw_assets, list)
        or not raw_assets
        or not all(isinstance(asset, str) and asset for asset in raw_assets)
    ):
        raise CAMSProviderError("invalid CAMS cache provenance assets")
    assets = tuple(raw_assets)
    if len(set(assets)) != len(assets) or any(Path(asset).name != asset for asset in assets):
        raise CAMSProviderError("invalid CAMS cache provenance assets")
    if expected_assets is not None and assets != expected_assets:
        raise CAMSProviderError(
            "CAMS cache provenance assets do not match the requested complete asset family"
        )
    if any(not (date_dir / asset).is_file() for asset in assets):
        raise CAMSProviderError("CAMS cache provenance references a missing asset")

    raw_recorded_at = payload.get("recorded_at")
    if not isinstance(raw_recorded_at, str):
        raise CAMSProviderError("invalid CAMS cache provenance recorded_at")
    try:
        recorded_at = datetime.fromisoformat(raw_recorded_at)
    except ValueError:
        raise CAMSProviderError("invalid CAMS cache provenance recorded_at") from None
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise CAMSProviderError("invalid CAMS cache provenance recorded_at")

    return _SourceManifest(
        resolved_source=source,
        source_url=source_url,
        assets=assets,
        recorded_at=recorded_at.astimezone(timezone.utc),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
