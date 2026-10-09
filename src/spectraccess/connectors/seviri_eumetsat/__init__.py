"""MSG SEVIRI Level 1.5 from the EUMETSAT Data Store."""
from .connector import (
    SEVIRIAuthorizationError, SEVIRIConnector, SEVIRIProviderError,
    SEVIRIResult, SEVIRITarget, target_to_canonical,
)

__all__ = ["SEVIRIAuthorizationError", "SEVIRIConnector", "SEVIRIProviderError",
           "SEVIRIResult", "SEVIRITarget", "target_to_canonical"]
