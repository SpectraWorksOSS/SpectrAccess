"""Core interfaces and helpers shared by spectrAccess connectors."""

from .connector import Connector
from .schema import (
    SCHEMA_VERSION,
    OBSERVATION_COLUMNS,
    SchemaError,
    Uncertainty,
    UncertaintyStatus,
    empty_frame,
    frame_from_records,
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
    "OBSERVATION_COLUMNS",
    "UncertaintyStatus",
    "Uncertainty",
    "SchemaError",
    "validate",
    "empty_frame",
    "frame_from_records",
]

