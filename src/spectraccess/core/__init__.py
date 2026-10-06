"""Core interfaces and helpers shared by spectrAccess connectors."""

from .connector import Connector
from .schema import (
    SCHEMA_VERSION,
    SchemaError,
    Uncertainty,
    UncertaintyStatus,
    empty_frame,
    validate,
)
from .credentials import Credential, CredentialMissing, CredentialRejected, CredentialSession

__all__ = [
    "Connector",
    "CredentialSession",
    "Credential",
    "CredentialMissing",
    "CredentialRejected",
    "SCHEMA_VERSION",
    "UncertaintyStatus",
    "Uncertainty",
    "SchemaError",
    "validate",
    "empty_frame",
]

