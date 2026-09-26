"""spectrAccess: clients for spectral reference data portals."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    # pyproject.toml is the single version source; installed metadata carries it.
    __version__ = version("spectraccess")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0+unknown"
