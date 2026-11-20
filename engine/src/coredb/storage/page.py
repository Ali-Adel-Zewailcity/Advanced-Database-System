"""Shared page constants."""
from enum import IntEnum

INVALID_PAGE_ID = 0xFFFFFFFF


class PageType(IntEnum):
    FREE = 0
    NSM_DATA = 1
    CATALOG = 2
    COLUMN_DATA_FIXED = 3
    COLUMN_DATA_VAR = 4
