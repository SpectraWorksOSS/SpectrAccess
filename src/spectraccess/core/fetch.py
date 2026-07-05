from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

import requests


class FetchError(RuntimeError):
    """Raised when a URL cannot be fetched after retries."""


def default_cache_dir() -> Path:
    root = os.environ.get("SPECTRACCESS_CACHE_DIR")
    if root:
        return Path(root)
    return Path.cwd() / ".spectraccess_cache"


def cache_path_for_url(url: str, cache_dir: str | Path | None = None) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    suffix = Path(quote(url, safe="")).suffix
    return Path(cache_dir or default_cache_dir()) / f"{digest}{suffix}"


def fetch_url(
    url: str,
    *,
    session: requests.Session | None = None,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
    retries: int = 3,
    backoff_seconds: float = 0.2,
    timeout: float = 30,
    headers: Mapping[str, str] | None = None,
) -> bytes:
    """Fetch URL bytes with simple retry and URL-keyed disk caching."""
    path = cache_path_for_url(url, cache_dir)
    if use_cache and path.exists():
        return path.read_bytes()

    client = session or requests.Session()
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = client.get(url, timeout=timeout, headers=headers)
            response.raise_for_status()
            content = response.content
            if use_cache:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                except OSError:
                    # Cache is an optimization, never a requirement: a
                    # read-only cwd / container layer must not fail a fetch
                    # that already succeeded over HTTP.
                    pass
            return content
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(backoff_seconds * (2**attempt))

    raise FetchError(f"failed to fetch {url!r} after {retries} attempts") from last_error

