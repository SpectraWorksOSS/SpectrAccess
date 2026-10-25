from .imagery import read_imagery

__all__ = [
    "read_imagery",
    "CDSEConnectorError",
    "CDSEDownloadError",
    "CDSEProductError",
    "CDSEProviderError",
    "S2CDSEConnector",
    "Sentinel2CDSEConnector",
    "Sentinel2Target",
    "target_to_canonical",
    "target_to_frame",
]


def __getattr__(name):
    # Archive clients are optional for reading local imagery.
    if name in __all__ and name != "read_imagery":
        from importlib import import_module
        return getattr(import_module(".connector", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
