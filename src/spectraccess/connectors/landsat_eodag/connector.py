"""Landsat Collection 2 Level-1 discovery and download through EODAG/USGS.

EODAG remains authoritative for provider configuration, search, authentication,
retry policy, and download transport.  spectrAccess adds a stable public target
record, explicit Collection-2 identity fields, lossless provider provenance,
and canonical metadata output.  It deliberately does not parse Landsat pixels.
"""

from __future__ import annotations

import os
import re
import tempfile
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
    from eodag import EODataAccessGateway
    from eodag.api.product import EOProduct
    import eodag.plugins.apis.usgs as _eodag_usgs_api  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised by packaging
    raise ImportError(
        "LandsatEodagConnector requires EODAG's USGS plugin. "
        "Install it with: pip install 'spectraccess[landsat]'"
    ) from exc


PROVIDER = "usgs"
COLLECTION = "LANDSAT_C2L1"
USGS_COLLECTION = "landsat_ot_c2_l1"
USGS_API_PLUGIN_TYPE = "UsgsApi"
USERNAME_ENV = "EODAG__USGS__API__CREDENTIALS__USERNAME"
PASSWORD_ENV = "EODAG__USGS__API__CREDENTIALS__PASSWORD"
SOURCE = "usgs-earth-resources-observation-and-science-center"
SOURCE_AGENCY = "U.S. Geological Survey"
CATALOGUE_URL = "https://earthexplorer.usgs.gov/"

try:
    EODAG_VERSION = version("eodag")
except PackageNotFoundError:  # pragma: no cover - guarded by import above
    EODAG_VERSION = "unknown"


class LandsatConnectorError(RuntimeError):
    """Base error for failures at the public Landsat connector boundary."""


class LandsatProviderError(LandsatConnectorError):
    """EODAG/USGS discovery failed rather than returning a clean empty result."""


class LandsatDownloadError(LandsatConnectorError):
    """EODAG did not produce the requested local Landsat archive."""


class LandsatProductError(LandsatConnectorError):
    """Provider metadata is missing or contradicts the Collection-2 identity."""


_TITLE_RE = re.compile(
    r"^(?P<platform>LC0[89])_(?P<level>L1TP)_"
    r"(?P<path>\d{3})(?P<row>\d{3})_"
    r"(?P<acquired>\d{8})_(?P<processed>\d{8})_"
    r"(?P<collection>\d{2})_(?P<tier>T1|T2|RT)$",
    re.IGNORECASE,
)
_PLATFORM_TO_SENSOR = {"LC08": "L8_OLI", "LC09": "L9_OLI2"}
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")


@dataclass(frozen=True)
class LandsatTarget:
    """Lossless public record crossing the EODAG discover -> fetch boundary."""

    product_id: str
    title: str
    sensor_id: str
    platform_id: str
    start_time: datetime
    end_time: datetime
    footprint_wkt: str
    cloud_cover: float | None
    size_mb: float | None
    product_level: str
    collection_number: str
    tier: str
    product_type: str
    wrs_path: int
    wrs_row: int
    source_family: str
    catalogue_url: str
    retrieved_at: datetime
    raw_metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    provider: str = PROVIDER
    provider_client: str = "eodag"
    provider_client_version: str = EODAG_VERSION
    raw: "EOProduct | Any" = field(default=None, repr=False, compare=False)

    @property
    def cache_identifier(self) -> str:
        """Stable provider/product identifier used by downstream source caches."""
        return f"{self.provider}:{self.product_id}"


class LandsatEodagConnector(Connector):
    """Thin public adapter over EODAG's USGS Landsat Collection-2 client.

    Credentials are BYO. Pass ``username`` and ``password`` to the constructor,
    or use EODAG's standard USGS credential environment variables. The password
    slot is the USGS M2M application token. Credentials are only handed to
    EODAG and are never included in targets or output provenance.
    """

    def __init__(
        self,
        *,
        provider: str = PROVIDER,
        collection: str = COLLECTION,
        username: str | None = None,
        password: str | None = None,
        gateway: Any | None = None,
    ) -> None:
        if (username is None) != (password is None):
            raise ValueError("username and password must be provided together")
        self.provider = provider
        self.collection = collection
        self._dag = gateway or EODataAccessGateway(user_conf_file_path=_empty_eodag_config())
        provider_config = _provider_config(
            provider,
            collection=collection,
            username=username,
            password=password,
        )
        if provider_config:
            self._dag.update_providers_config(dict_conf=provider_config)
        self._dag.set_preferred_provider(provider)

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
    ) -> list[LandsatTarget]:
        """Return Landsat C2 L1TP targets for an EPSG:4326 bounding box.

        ``end`` is inclusive by calendar day, preserving the established
        RefCal work-list contract. Landsat is WRS-2 indexed, so MGRS input is
        rejected instead of being silently ignored.
        """
        if mgrs_tile is not None:
            raise ValueError("Landsat discovery is WRS-2/bbox based; mgrs_tile is Sentinel-2-only")
        if bbox is None:
            raise ValueError("Provide bbox=(west, south, east, north) for Landsat discovery")
        _validate_bbox(bbox)
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if limit == 0:
            return []
        if max_cloud_cover is not None and not 0 <= max_cloud_cover <= 100:
            raise ValueError("max_cloud_cover must be between 0 and 100")
        start_day = _as_date(start)
        end_day = _as_date(end)
        if start_day is not None and end_day is not None and end_day < start_day:
            raise ValueError(f"end must be >= start (got start={start_day}, end={end_day})")

        search_kwargs: dict[str, Any] = {
            "provider": self.provider,
            "collection": self.collection,
            "limit": limit,
            "raise_errors": True,
            "geom": {
                "lonmin": bbox[0],
                "latmin": bbox[1],
                "lonmax": bbox[2],
                "latmax": bbox[3],
            },
        }
        if start_day is not None:
            search_kwargs["start"] = start_day.isoformat()
        if end_day is not None:
            search_kwargs["end"] = (end_day + timedelta(days=1)).isoformat()
        targets = self._search(search_kwargs)
        if max_cloud_cover is not None:
            targets = [
                target
                for target in targets
                if target.cloud_cover is not None and target.cloud_cover <= max_cloud_cover
            ]
        return targets

    def discover_title(self, title: str, *, limit: int = 5) -> list[LandsatTarget]:
        """Search by exact USGS Collection-2 display identifier."""
        search_id = _strip_archive_suffixes(title)
        _parse_title(search_id)
        if limit < 1:
            raise ValueError("limit must be >= 1")
        return self._search(
            {
                "provider": self.provider,
                "collection": self.collection,
                "id": search_id,
                "limit": limit,
                "raise_errors": True,
            }
        )

    def _search(self, search_kwargs: Mapping[str, Any]) -> list[LandsatTarget]:
        try:
            products = self._dag.search(**dict(search_kwargs))
            retrieved_at = datetime.now(timezone.utc)
            return [_product_to_target(product, retrieved_at=retrieved_at) for product in products]
        except LandsatConnectorError:
            raise
        except Exception as exc:
            raise LandsatProviderError(f"USGS Landsat discovery failed: {exc}") from exc

    def fetch(
        self,
        target: LandsatTarget,
        *,
        dest: str | Path,
        wait: float | None = None,
        timeout: float | None = None,
        **_kwargs: object,
    ) -> str:
        """Download one target through EODAG and return its archive path."""
        output_dir = Path(dest)
        output_dir.mkdir(parents=True, exist_ok=True)
        download_kwargs: dict[str, Any] = {"output_dir": str(output_dir), "extract": False}
        if wait is not None:
            download_kwargs["wait"] = wait
        if timeout is not None:
            download_kwargs["timeout"] = timeout
        try:
            result = self._dag.download(target.raw, **download_kwargs)
        except Exception as exc:
            raise LandsatDownloadError(
                f"EODAG download failed for {target.product_id} ({target.title}): {exc}"
            ) from exc
        path = _download_path(result)
        if path is None or not path.exists():
            path = _find_downloaded_archive(output_dir, target.title)
        if path is None or not path.is_file():
            raise LandsatDownloadError(
                f"EODAG download of {target.title!r} produced no matching archive under {output_dir}"
            )
        return str(path)

    def parse(self, raw: bytes | str, *, target: LandsatTarget | None = None) -> pd.DataFrame:
        """Return one lossless metadata row; Landsat pixels stay untouched."""
        if target is None:
            raise LandsatProductError(
                "Landsat archive parsing requires the discovered target so provider provenance "
                "is not guessed; call parse(path, target=target) or Connector.run()"
            )
        return target_to_frame(target, local_path=_local_path(raw))

    def parse_canonical(
        self,
        raw: bytes | str,
        *,
        target: LandsatTarget | None = None,
        retrieved_at: datetime | None = None,
    ) -> pd.DataFrame:
        """Emit provider scene cloud cover with honest unknown uncertainty."""
        if target is None:
            raise LandsatProductError(
                "canonical Landsat metadata requires the discovered target; "
                "call parse_canonical(path, target=target) or Connector.run(canonical=True)"
            )
        return target_to_canonical(target, local_path=_local_path(raw), retrieved_at=retrieved_at)

    def _parse_kwargs_for(self, target: object) -> dict[str, object]:
        return {"target": target} if isinstance(target, LandsatTarget) else {}

    def _canonical_kwargs_for(self, target: object) -> dict[str, object]:
        return {"target": target} if isinstance(target, LandsatTarget) else {}


def _product_to_target(product: "EOProduct", *, retrieved_at: datetime) -> LandsatTarget:
    props = deepcopy(dict(getattr(product, "properties", {}) or {}))
    title = _strip_archive_suffixes(str(props.get("title") or props.get("id") or props.get("uid") or ""))
    identity = _parse_title(title)
    provider_product_id = str(props.get("id") or props.get("uid") or title).strip()
    if not provider_product_id:
        raise LandsatProductError(f"USGS product {title!r} has no provider product id")
    platform_id = identity["platform"]
    start_time = _parse_time(
        props.get("start_datetime")
        or props.get("datetime")
        or props.get("startTimeFromAscendingNode"),
        field_name="start time",
        title=title,
    )
    end_time = _parse_time(
        props.get("end_datetime")
        or props.get("completionTimeFromAscendingNode")
        or start_time,
        field_name="end time",
        title=title,
    )
    cloud_cover = _optional_float(
        props.get("eo:cloud_cover", props.get("cloudCover", props.get("cloud_cover"))),
        field_name="cloud cover",
        title=title,
    )
    if cloud_cover is not None and not 0 <= cloud_cover <= 100:
        raise LandsatProductError(f"USGS product {title!r} cloud cover is outside 0..100")
    tier = identity["tier"]
    level = identity["level"]
    collection_number = identity["collection"]
    return LandsatTarget(
        product_id=provider_product_id,
        title=title,
        sensor_id=_PLATFORM_TO_SENSOR[platform_id],
        platform_id=platform_id,
        start_time=start_time,
        end_time=end_time,
        footprint_wkt=_geometry_to_wkt(product, title=title),
        cloud_cover=cloud_cover,
        size_mb=_product_size_mb(props),
        product_level=level,
        collection_number=collection_number,
        tier=tier,
        product_type=f"LANDSAT_C2_{level}_{tier}",
        wrs_path=int(identity["path"]),
        wrs_row=int(identity["row"]),
        source_family="landsat_c2_l1tp",
        catalogue_url=CATALOGUE_URL,
        retrieved_at=retrieved_at,
        raw_metadata=props,
        raw=product,
    )


def target_to_frame(target: LandsatTarget, *, local_path: str | None = None) -> pd.DataFrame:
    frame = pd.DataFrame([_target_fields(target, local_path=local_path)])
    frame.attrs["source_metadata"] = deepcopy(dict(target.raw_metadata))
    frame.attrs["provider_client"] = target.provider_client
    frame.attrs["provider_client_version"] = target.provider_client_version
    return frame


def target_to_canonical(
    target: LandsatTarget,
    *,
    local_path: str | None = None,
    retrieved_at: datetime | None = None,
) -> pd.DataFrame:
    unc = Uncertainty(value=None, status=UncertaintyStatus.UNKNOWN)
    row = {
        "time": target.start_time,
        "platform": target.sensor_id,
        "instrument": "OLI" if target.platform_id == "LC08" else "OLI-2",
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
        **_target_fields(target, local_path=local_path),
    }
    frame = pd.DataFrame([row])
    frame.attrs["source_metadata"] = deepcopy(dict(target.raw_metadata))
    return validate(frame)


def _target_fields(target: LandsatTarget, *, local_path: str | None) -> dict[str, object]:
    return {
        "product_id": target.product_id,
        "title": target.title,
        "sensor_id": target.sensor_id,
        "platform_id": target.platform_id,
        "start_time": target.start_time,
        "end_time": target.end_time,
        "footprint_wkt": target.footprint_wkt,
        "cloud_cover": target.cloud_cover,
        "size_mb": target.size_mb,
        "product_level": target.product_level,
        "collection_number": target.collection_number,
        "tier": target.tier,
        "product_type": target.product_type,
        "wrs_path": target.wrs_path,
        "wrs_row": target.wrs_row,
        "source_family": target.source_family,
        "provider": target.provider,
        "provider_client": target.provider_client,
        "provider_client_version": target.provider_client_version,
        "cache_identifier": target.cache_identifier,
        "catalogue_url": target.catalogue_url,
        "local_path": local_path,
    }


def _provider_config(
    provider: str,
    *,
    collection: str,
    username: str | None,
    password: str | None,
) -> dict[str, Any]:
    if provider != PROVIDER:
        return {}
    resolved_username = (username or os.environ.get(USERNAME_ENV, "")).strip()
    resolved_password = (password or os.environ.get(PASSWORD_ENV, "")).strip()
    if not resolved_username or not resolved_password:
        return {}
    return {
        provider: {
            "products": {collection: {"_collection": USGS_COLLECTION}},
            "api": {
                "type": USGS_API_PLUGIN_TYPE,
                "credentials": {
                    "username": resolved_username,
                    "password": resolved_password,
                },
            },
        }
    }


def _empty_eodag_config() -> str:
    path = Path(tempfile.gettempdir()) / "spectraccess-empty-eodag.yml"
    if not path.exists():
        path.write_text("", encoding="utf-8")
    return str(path)


def _parse_title(title: str) -> dict[str, str]:
    match = _TITLE_RE.fullmatch(title.strip())
    if match is None:
        raise LandsatProductError(
            f"expected a Landsat 8/9 Collection-2 L1TP display id, got {title!r}"
        )
    identity = {key: value.upper() for key, value in match.groupdict().items()}
    if identity["collection"] != "02":
        raise LandsatProductError(
            f"Landsat product {title!r} is collection {identity['collection']}, expected 02"
        )
    return identity


def _strip_archive_suffixes(title: str) -> str:
    text = str(title).strip()
    lower = text.lower()
    for suffix in _ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _as_date(value: date | datetime | None) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def _parse_time(value: Any, *, field_name: str, title: str) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        raise LandsatProductError(f"USGS product {title!r} has no {field_name}")
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LandsatProductError(
            f"USGS product {title!r} has invalid {field_name}: {value!r}"
        ) from exc
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _geometry_to_wkt(product: "EOProduct", *, title: str) -> str:
    geometry = getattr(product, "geometry", None)
    if geometry is not None and hasattr(geometry, "wkt"):
        return str(geometry.wkt)
    props = getattr(product, "properties", {}) or {}
    value = props.get("footprint") or props.get("geometry") or props.get("spatial_extent")
    if isinstance(value, str) and value.upper().startswith(("POLYGON", "MULTIPOLYGON")):
        return value
    raise LandsatProductError(f"cannot extract EPSG:4326 footprint WKT from {title!r}")


def _optional_float(value: Any, *, field_name: str, title: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LandsatProductError(
            f"USGS product {title!r} has invalid {field_name}: {value!r}"
        ) from exc


def _product_size_mb(props: Mapping[str, Any]) -> float | None:
    for key in ("ContentLength", "ContentFileSize", "size", "fileSize"):
        value = props.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number / 1_000_000 if number > 100_000 else number
    return None


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    west, south, east, north = bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError(f"invalid EPSG:4326 bbox: {bbox!r}")


def _download_path(result: object) -> Path | None:
    if isinstance(result, (list, tuple)):
        result = result[0] if result else None
    return Path(str(result)) if result else None


def _find_downloaded_archive(output_dir: Path, title: str) -> Path | None:
    expected = title.upper()
    candidates = sorted(
        path
        for path in output_dir.iterdir()
        if path.is_file()
        and _strip_archive_suffixes(path.name).upper() == expected
        and any(path.name.lower().endswith(suffix) for suffix in _ARCHIVE_SUFFIXES)
    )
    return candidates[0] if candidates else None


def _local_path(raw: bytes | str) -> str | None:
    if isinstance(raw, bytes):
        return None
    return str(raw)

