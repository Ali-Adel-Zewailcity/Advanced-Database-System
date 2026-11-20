from typing import Dict, Type

from ...errors import CatalogError
from .base import StorageLayout
from .dsm import DSMTable
from .nsm import NSMTable

LAYOUTS: Dict[str, Type[StorageLayout]] = {"nsm": NSMTable, "dsm": DSMTable}


def get_layout(name: str) -> Type[StorageLayout]:
    try:
        return LAYOUTS[name.lower()]
    except KeyError:
        raise CatalogError(f"unknown storage layout {name!r}; choose from {sorted(LAYOUTS)}") from None


__all__ = ["StorageLayout", "NSMTable", "DSMTable", "LAYOUTS", "get_layout"]
