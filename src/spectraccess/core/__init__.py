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
from .session import CredentialSession

__all__ = [
    "Connector",
    "CredentialSession",
    "SCHEMA_VERSION",
    "UncertaintyStatus",
    "Uncertainty",
    "SchemaError",
    "validate",
    "empty_frame",
]

