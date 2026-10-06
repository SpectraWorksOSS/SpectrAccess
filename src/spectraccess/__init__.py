"""spectrAccess: clients for spectral reference data portals."""

from importlib.metadata import PackageNotFoundError, version

from .core.credentials import Credential, CredentialMissing, CredentialRejected, login, logout, status

__all__ = ["__version__", "Credential", "CredentialMissing", "CredentialRejected", "login", "logout", "status"]

try:
    # pyproject.toml is the single version source; installed metadata carries it.
    __version__ = version("spectraccess")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0+unknown"
