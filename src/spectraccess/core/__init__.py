"""Core interfaces and helpers shared by spectrAccess connectors."""

from .connector import Connector
from .session import CredentialSession

__all__ = ["Connector", "CredentialSession"]

