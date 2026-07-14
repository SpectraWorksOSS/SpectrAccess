"""Sentinel-2 L1C discovery and download through the maintained CDSETool client.

This module is deliberately a thin adapter. CDSETool remains authoritative for
OData query construction, pagination, authentication, retries, and download
transport. spectrAccess adds a stable target record, explicit provider errors,
lossless provenance, checksum verification, and canonical uncertainty labels.
It does not parse Sentinel pixels or implement a second CDSE transport.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from spectraccess.core.connector import Connector
from spectraccess.core.schema import (
    Uncertainty,
    UncertaintyStatus,
    uncertainty_columns,
    validate,
)

try:
    from cdsetool.credentials import Credentials
    from cdsetool.download import download_feature
    from cdsetool.query import get_product_attribute, query_features
except ImportError as exc:  # pragma: no cover - exercised by packaging, not fixture CI
    raise ImportError(
        "Sentinel2CDSEConnector requires the maintained CDSETool client. "
        "Install it with: pip install 'spectraccess[cdse]'"
    ) from exc


COLLECTION = "SENTINEL-2"
PRODUCT_TYPE = "S2MSI1C"
SOURCE = "copernicus-data-space-ecosystem"
SOURCE_AGENCY = "European Union / ESA"
CATALOGUE_PRODUCT_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products({product_id})"
DOWNLOAD_PRODUCT_URL = "https://download.dataspace.copernicus.eu/odata/v1/Products({product_id})/$value"
MAX_QUERY_RESULTS = 10_000

try:
    CDSETOOL_VERSION = version("cdsetool")
except PackageNotFoundError:  # pragma: no cover - guarded by import above
    CDSETOOL_VERSION = "unknown"


class CDSEConnectorError(RuntimeError):
    """Base error for failures at the public CDSE connector boundary."""


class CDSEProviderError(CDSEConnectorError):
    """CDSETool reported a provider/query failure rather than a clean empty result."""


class CDSEDownloadError(CDSEConnectorError):
    """CDSETool failed to download or verify a requested product."""


class CDSEProductError(CDSEConnectorError):
    """A provider feature is missing or contradicts required Sentinel-2 metadata."""


@dataclass(frozen=True)
class Sentinel2Target:
    """Lossless public record crossing the CDSE discover -> fetch boundary."""

    product_id: str
    title: str
    sensor_id: str
    platform_id: str
    start_time: datetime
    end_time: datetime
    footprint_wkt: str
    cloud_cover: float
    mgrs_tile: str
    content_length_bytes: int | None
    online: bool
    processor_version: str | None
    processing_date: datetime | None
    catalogue_url: str
    download_url: str
    retrieved_at: datetime
    checksums: Mapping[str, str]
    provider: str = "cdse"
    provider_client: str = "cdsetool"
    provider_client_version: str = CDSETOOL_VERSION
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def size_mb(self) -> float | None:
        """Decimal megabytes, matching RefCal's existing ProductRef convention."""
        if self.content_length_bytes is None:
            return None
        return self.content_length_bytes / 1_000_000


class _CaptureLogger:
    """Adapt CDSETool's logger hook while retaining swallowed provider errors."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    @staticmethod
    def _render(message: object, args: tuple[object, ...]) -> str:
        text = str(message)
        if args:
            try:
                return text % args
            except (TypeError, ValueError):
                return " ".join([text, *(str(arg) for arg in args)])
        return text

    def warning(self, message: object, *args: object) -> None:
        self.warnings.append(self._render(message, args))

    def error(self, message: object, *args: object) -> None:
        self.errors.append(self._render(message, args))

    def debug(self, _message: object, *_args: object) -> None:
        return None

    def info(self, _message: object, *_args: object) -> None:
        return None


class Sentinel2CDSEConnector(Connector):
    """Thin public adapter over CDSETool for Sentinel-2 MSI Level-1C products.

    Discovery is public and does not require credentials. Downloads use
    CDSETool's BYO-credential path: pass ``username`` + ``password``, an
    existing CDSETool ``credentials`` object, or let CDSETool read ``.netrc``.
    Credentials are never retained on this connector or added to provenance.
    """

    def __init__(self, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts

    def discover(
        self,
        *,
        bbox: tuple[float, float, float, float] | None = None,
        mgrs_tile: str | None = None,
        start: date | datetime | None = None,
        end: date | datetime | None = None,
        max_cloud_cover: float | None = None,
        limit: int = 10,
        **_kwargs: object,
    ) -> list[Sentinel2Target]:
        """Return newest-first Sentinel-2 L1C products matching the query.

        ``end`` is inclusive by UTC calendar day, preserving RefCal's existing
        operator semantics. CDSETool's current OData iterator orders oldest
        first, so this adapter uses its public count/skip controls to request
        the final page, then reverses it. Queries over more than CDSE's 10,000
        result pagination ceiling fail loudly and ask for a narrower window.
        """
        if bbox is None and mgrs_tile is None:
            raise ValueError("Provide bbox or mgrs_tile (or both).")
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if limit == 0:
            return []
        if max_cloud_cover is not None and not 0 <= max_cloud_cover <= 100:
            raise ValueError("max_cloud_cover must be between 0 and 100")

        start_utc = _as_utc(start)
        end_utc = _as_utc(end)
        if start_utc is not None and end_utc is not None and end_utc < start_utc:
            raise ValueError(f"end must be >= start (got start={start_utc}, end={end_utc})")

        search_terms: dict[str, object] = {"productType": PRODUCT_TYPE}
        if start_utc is not None:
            search_terms["contentDateStartGe"] = start_utc.date().isoformat()
        if end_utc is not None:
            search_terms["contentDateStartLt"] = (end_utc.date() + timedelta(days=1)).isoformat()
        if bbox is not None:
            search_terms["geometry"] = _bbox_to_wkt(bbox)
        if mgrs_tile is not None:
            search_terms["tileId"] = _normalise_tile(mgrs_tile)
        if max_cloud_cover is not None:
            search_terms["cloudCover"] = f"[0,{float(max_cloud_cover)}]"

        count_log = _CaptureLogger()
        count_query = _provider_query(
            {**search_terms, "top": 0},
            logger=count_log,
            max_attempts=self.max_attempts,
        )
        try:
            total = len(count_query)
        except Exception as exc:
            raise CDSEProviderError(f"CDSE discovery count failed: {exc}") from exc
        _raise_provider_errors("CDSE discovery count", count_log)
        if total > MAX_QUERY_RESULTS:
            raise CDSEProviderError(
                f"CDSE query matched {total:,} products, above the provider pagination ceiling "
                f"of {MAX_QUERY_RESULTS:,}; narrow the date, tile, geometry, or cloud-cover window"
            )
        if total == 0:
            return []

        page_log = _CaptureLogger()
        page_terms = {
            **search_terms,
            "skip": max(0, total - limit),
            "top": min(limit, 1000),
        }
        try:
            feature_query = _provider_query(
                page_terms,
                logger=page_log,
                max_attempts=self.max_attempts,
            )
            features = list(itertools.islice(feature_query, limit))
        except Exception as exc:
            raise CDSEProviderError(f"CDSE discovery page failed: {exc}") from exc
        _raise_provider_errors("CDSE discovery page", page_log)

        retrieved_at = datetime.now(timezone.utc)
        targets = [_feature_to_target(feature, retrieved_at=retrieved_at) for feature in features]
        targets.sort(key=lambda target: (target.start_time, target.title), reverse=True)
        return targets

    def fetch(
        self,
        target: Sentinel2Target,
        *,
        dest: str | Path,
        username: str | None = None,
        password: str | None = None,
        credentials: object | None = None,
        overwrite_existing: bool = False,
        filter_pattern: str | None = None,
        tmpdir: str | Path | None = None,
        verify_checksum: bool = True,
        **_kwargs: object,
    ) -> str:
        """Download one target through CDSETool and return its local path."""
        if credentials is not None and (username is not None or password is not None):
            raise ValueError("pass credentials or username/password, not both")
        if (username is None) != (password is None):
            raise ValueError("username and password must be provided together")

        output_dir = Path(dest)
        output_dir.mkdir(parents=True, exist_ok=True)
        log = _CaptureLogger()
        options: dict[str, object] = {
            "logger": log,
            "overwrite_existing": overwrite_existing,
        }
        if credentials is not None:
            options["credentials"] = credentials
        elif username is not None and password is not None:
            options["credentials"] = Credentials(username, password)
        if filter_pattern is not None:
            options["filter_pattern"] = filter_pattern
        if tmpdir is not None:
            options["tmpdir"] = str(tmpdir)

        try:
            filename = download_feature(deepcopy(dict(target.raw)), str(output_dir), options)
        except Exception as exc:
            raise CDSEDownloadError(f"CDSETool download failed for {target.product_id}: {exc}") from exc
        _raise_download_errors(target, log)
        if not filename:
            raise CDSEDownloadError(
                f"CDSETool returned no output for {target.product_id} ({target.title})"
            )

        output_path = output_dir / filename
        if not output_path.exists():
            raise CDSEDownloadError(
                f"CDSETool reported {filename!r} for {target.product_id}, but it does not exist "
                f"under {output_dir}"
            )
        if verify_checksum and output_path.is_file() and filter_pattern is None:
            _verify_checksum(output_path, target)
        return str(output_path)

    def parse(self, raw: bytes | str, *, target: Sentinel2Target | None = None) -> pd.DataFrame:
        """Return one lossless product-metadata row; Sentinel pixels stay untouched."""
        if target is None:
            raise CDSEProductError(
                "Sentinel-2 SAFE parsing requires the discovered target so provider provenance "
                "is not guessed; call parse(path, target=target) or Connector.run()"
            )
        return target_to_frame(target, local_path=_local_path(raw))

    def parse_canonical(
        self,
        raw: bytes | str,
        *,
        target: Sentinel2Target | None = None,
        retrieved_at: datetime | None = None,
    ) -> pd.DataFrame:
        """Emit the source-provided scene cloud cover with honest unknown uncertainty."""
        if target is None:
            raise CDSEProductError(
                "canonical Sentinel-2 metadata requires the discovered target; "
                "call parse_canonical(path, target=target) or Connector.run(canonical=True)"
            )
        return target_to_canonical(
            target,
            local_path=_local_path(raw),
            retrieved_at=retrieved_at,
        )

    def _parse_kwargs_for(self, target: object) -> dict[str, object]:
        return {"target": target} if isinstance(target, Sentinel2Target) else {}

    def _canonical_kwargs_for(self, target: object) -> dict[str, object]:
        return {"target": target} if isinstance(target, Sentinel2Target) else {}


# Preserve the proven RefCal connector name so the consumer flip can change
# dependency direction without also renaming every operator call site.
S2CDSEConnector = Sentinel2CDSEConnector


def _provider_query(
    search_terms: Mapping[str, object], *, logger: _CaptureLogger, max_attempts: int
):
    return query_features(
        COLLECTION,
        dict(search_terms),
        options={
            "expand_attributes": True,
            "max_attempts": max_attempts,
            "logger": logger,
        },
    )


def _raise_provider_errors(operation: str, logger: _CaptureLogger) -> None:
    if logger.errors:
        raise CDSEProviderError(f"{operation} failed: {logger.errors[-1]}")


def _raise_download_errors(target: Sentinel2Target, logger: _CaptureLogger) -> None:
    if logger.errors:
        raise CDSEDownloadError(
            f"CDSETool download failed for {target.product_id}: {logger.errors[-1]}"
        )


def _feature_to_target(feature: Mapping[str, Any], *, retrieved_at: datetime) -> Sentinel2Target:
    product_id = _required_text(feature, "Id")
    title = _required_text(feature, "Name")
    if not title.endswith(".SAFE"):
        raise CDSEProductError(f"CDSE feature {product_id} is not a SAFE product: {title!r}")
    if str(feature.get("Collection", COLLECTION)) != COLLECTION:
        raise CDSEProductError(
            f"CDSE feature {product_id} belongs to {feature.get('Collection')!r}, not {COLLECTION}"
        )
    product_type = get_product_attribute(dict(feature), "productType")
    if product_type != PRODUCT_TYPE:
        raise CDSEProductError(
            f"CDSE feature {product_id} productType={product_type!r}, expected {PRODUCT_TYPE!r}"
        )

    title_match = re.match(r"^(S2[ABC])_MSIL1C_", title)
    if title_match is None:
        raise CDSEProductError(f"cannot derive Sentinel-2 platform from SAFE title {title!r}")
    sensor_id = title_match.group(1)
    serial = get_product_attribute(dict(feature), "platformSerialIdentifier")
    if serial and sensor_id != f"S2{str(serial).upper()}":
        raise CDSEProductError(
            f"SAFE title platform {sensor_id!r} contradicts platformSerialIdentifier={serial!r}"
        )

    content_date = feature.get("ContentDate")
    if not isinstance(content_date, Mapping):
        raise CDSEProductError(f"CDSE feature {product_id} has no ContentDate record")
    start_time = _parse_provider_time(content_date.get("Start"), "ContentDate.Start", product_id)
    end_time = _parse_provider_time(content_date.get("End"), "ContentDate.End", product_id)
    cloud_cover = _required_float(
        get_product_attribute(dict(feature), "cloudCover"), "cloudCover", product_id
    )
    if not 0 <= cloud_cover <= 100:
        raise CDSEProductError(
            f"CDSE feature {product_id} cloudCover={cloud_cover!r} is outside 0..100"
        )
    tile = _normalise_tile(
        str(get_product_attribute(dict(feature), "tileId") or _tile_from_title(title))
    )
    footprint_wkt = _footprint_wkt(feature, product_id)
    content_length = _optional_positive_int(feature.get("ContentLength"), "ContentLength", product_id)
    processor_version = get_product_attribute(dict(feature), "processorVersion")
    processing_date_raw = get_product_attribute(dict(feature), "processingDate")
    processing_date = (
        _parse_provider_time(processing_date_raw, "processingDate", product_id)
        if processing_date_raw
        else None
    )
    checksums = {
        str(item.get("Algorithm", "")).upper(): str(item.get("Value", "")).lower()
        for item in feature.get("Checksum", [])
        if isinstance(item, Mapping) and item.get("Algorithm") and item.get("Value")
    }

    return Sentinel2Target(
        product_id=product_id,
        title=title,
        sensor_id=sensor_id,
        platform_id=sensor_id,
        start_time=start_time,
        end_time=end_time,
        footprint_wkt=footprint_wkt,
        cloud_cover=cloud_cover,
        mgrs_tile=tile,
        content_length_bytes=content_length,
        online=bool(feature.get("Online", False)),
        processor_version=str(processor_version) if processor_version is not None else None,
        processing_date=processing_date,
        catalogue_url=CATALOGUE_PRODUCT_URL.format(product_id=product_id),
        download_url=DOWNLOAD_PRODUCT_URL.format(product_id=product_id),
        retrieved_at=retrieved_at,
        checksums=checksums,
        raw=deepcopy(dict(feature)),
    )


def target_to_frame(target: Sentinel2Target, *, local_path: str | None = None) -> pd.DataFrame:
    """Convert one public target into the stable native product-metadata table."""
    frame = pd.DataFrame([_target_fields(target, local_path=local_path)])
    frame.attrs["source_metadata"] = deepcopy(dict(target.raw))
    frame.attrs["provider_client"] = target.provider_client
    frame.attrs["provider_client_version"] = target.provider_client_version
    return frame


def target_to_canonical(
    target: Sentinel2Target,
    *,
    local_path: str | None = None,
    retrieved_at: datetime | None = None,
) -> pd.DataFrame:
    """Emit one canonical scene-cloud-cover quantity with unknown uncertainty."""
    unc = Uncertainty(value=None, status=UncertaintyStatus.UNKNOWN)
    extra = _target_fields(target, local_path=local_path)
    row = {
        "time": target.start_time,
        "platform": target.sensor_id,
        "instrument": "MSI",
        "band": None,
        "wavelength_nm": None,
        "site": None,
        "latitude": None,
        "longitude": None,
        "reference": None,
        "quantity": "scene_cloud_cover",
        "value": target.cloud_cover,
        "units": "%",
        **uncertainty_columns(unc),
        "source": SOURCE,
        "source_agency": SOURCE_AGENCY,
        "source_url": target.catalogue_url,
        "retrieved_at": retrieved_at or target.retrieved_at,
        **extra,
    }
    frame = pd.DataFrame([row])
    frame.attrs["source_metadata"] = deepcopy(dict(target.raw))
    return validate(frame)


def _target_fields(target: Sentinel2Target, *, local_path: str | None) -> dict[str, object]:
    return {
        "product_id": target.product_id,
        "title": target.title,
        "sensor_id": target.sensor_id,
        "platform_id": target.platform_id,
        "start_time": target.start_time,
        "end_time": target.end_time,
        "footprint_wkt": target.footprint_wkt,
        "cloud_cover": target.cloud_cover,
        "mgrs_tile": target.mgrs_tile,
        "content_length_bytes": target.content_length_bytes,
        "size_mb": target.size_mb,
        "online": target.online,
        "processor_version": target.processor_version,
        "processing_date": target.processing_date,
        "catalogue_url": target.catalogue_url,
        "download_url": target.download_url,
        "provider": target.provider,
        "provider_client": target.provider_client,
        "provider_client_version": target.provider_client_version,
        "local_path": local_path,
    }


def _verify_checksum(path: Path, target: Sentinel2Target) -> None:
    expected = target.checksums.get("MD5")
    if not expected:
        return
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest().lower()
    if actual != expected.lower():
        raise CDSEDownloadError(
            f"checksum mismatch for {target.product_id}: provider MD5={expected.lower()}, "
            f"downloaded MD5={actual}"
        )


def _as_utc(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _bbox_to_wkt(bbox: tuple[float, float, float, float]) -> str:
    if len(bbox) != 4:
        raise ValueError("bbox must contain (west, south, east, north)")
    west, south, east, north = (float(value) for value in bbox)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError(f"invalid EPSG:4326 bbox: {bbox!r}")
    return (
        f"POLYGON(({west} {south}, {west} {north}, {east} {north}, "
        f"{east} {south}, {west} {south}))"
    )


def _normalise_tile(tile: str) -> str:
    normalised = tile.strip().upper().removeprefix("T")
    if not re.fullmatch(r"\d{2}[A-Z]{3}", normalised):
        raise ValueError(f"invalid MGRS tile {tile!r}; expected e.g. '31UFT' or 'T31UFT'")
    return normalised


def _tile_from_title(title: str) -> str:
    match = re.search(r"_T(\d{2}[A-Z]{3})_", title.upper())
    if match is None:
        raise CDSEProductError(f"cannot derive MGRS tile from SAFE title {title!r}")
    return match.group(1)


def _parse_provider_time(value: object, field_name: str, product_id: str) -> datetime:
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise CDSEProductError(
            f"CDSE feature {product_id} has invalid {field_name}={value!r}"
        ) from exc
    if pd.isna(parsed):
        raise CDSEProductError(f"CDSE feature {product_id} has null {field_name}")
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("UTC")
    else:
        parsed = parsed.tz_convert("UTC")
    return parsed.to_pydatetime()


def _required_text(feature: Mapping[str, Any], key: str) -> str:
    value = feature.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CDSEProductError(f"CDSE feature missing required {key!r}")
    return value.strip()


def _required_float(value: object, field_name: str, product_id: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise CDSEProductError(
            f"CDSE feature {product_id} has invalid {field_name}={value!r}"
        ) from exc
    if not pd.notna(result):
        raise CDSEProductError(f"CDSE feature {product_id} has null {field_name}")
    return result


def _optional_positive_int(value: object, field_name: str, product_id: str) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise CDSEProductError(
            f"CDSE feature {product_id} has invalid {field_name}={value!r}"
        ) from exc
    if result <= 0:
        raise CDSEProductError(
            f"CDSE feature {product_id} has non-positive {field_name}={result!r}"
        )
    return result


def _footprint_wkt(feature: Mapping[str, Any], product_id: str) -> str:
    footprint = feature.get("Footprint")
    if isinstance(footprint, str):
        match = re.fullmatch(r"geography'SRID=4326;(.*)'", footprint.strip())
        if match:
            return match.group(1)
        if footprint.strip().upper().startswith(("POLYGON", "MULTIPOLYGON")):
            return footprint.strip()
    raise CDSEProductError(
        f"CDSE feature {product_id} has no usable EPSG:4326 Footprint WKT"
    )


def _local_path(raw: bytes | str) -> str | None:
    if isinstance(raw, bytes):
        return None
    return str(raw)
