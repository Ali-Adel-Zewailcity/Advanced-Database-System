"""Common index interface, RID type, and order-preserving key codec."""
from __future__ import annotations

import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from ..errors import IndexError

RID = Tuple[int, int]  # (page_id, slot_id) matching NSMTable

# Type tags for order-preserving binary key encoding
_TAG_INT = 0x01
_TAG_FLOAT = 0x02
_TAG_STR = 0x03
_TAG_BYTES = 0x04


def encode_key(key: Any) -> bytes:
    """Serialize a Python key into big-endian, lexicographically order-preserving bytes.

    Supports int, float, Decimal, str, and bytes.
    """
    if isinstance(key, bool):
        key = int(key)
    if isinstance(key, int):
        # Flip sign bit so signed 64-bit integers sort properly as unsigned bytes
        unsigned = key + (1 << 63)
        if not (0 <= unsigned < (1 << 64)):
            raise IndexError(f"integer key out of 64-bit range: {key}")
        return bytes([_TAG_INT]) + struct.pack(">Q", unsigned)
    if isinstance(key, Decimal):
        key = float(key)
    if isinstance(key, float):
        # IEEE-754 order-preserving transform
        bits = struct.unpack(">Q", struct.pack(">d", key))[0]
        if bits & (1 << 63):
            bits = ~bits & 0xFFFFFFFFFFFFFFFF
        else:
            bits |= 1 << 63
        return bytes([_TAG_FLOAT]) + struct.pack(">Q", bits)
    if isinstance(key, str):
        return bytes([_TAG_STR]) + key.encode("utf-8")
    if isinstance(key, (bytes, bytearray)):
        return bytes([_TAG_BYTES]) + bytes(key)
    raise IndexError(f"unsupported index key type: {type(key).__name__}")


def decode_key(raw: bytes) -> Any:
    """Inverse of `encode_key`."""
    if not raw:
        raise IndexError("empty encoded key")
    tag, payload = raw[0], raw[1:]
    if tag == _TAG_INT:
        unsigned = struct.unpack(">Q", payload)[0]
        return unsigned - (1 << 63)
    if tag == _TAG_FLOAT:
        bits = struct.unpack(">Q", payload)[0]
        if bits & (1 << 63):
            bits &= ~(1 << 63)
        else:
            bits = ~bits & 0xFFFFFFFFFFFFFFFF
        return struct.unpack(">d", struct.pack(">Q", bits))[0]
    if tag == _TAG_STR:
        return payload.decode("utf-8")
    if tag == _TAG_BYTES:
        return payload
    raise IndexError(f"unknown key tag: {tag:#x}")


@dataclass
class IndexStats:
    inserts: int = 0
    updates: int = 0
    deletes: int = 0
    point_lookups: int = 0
    range_scans: int = 0
    bytes_written: int = 0
    logical_bytes_written: int = 0
    compactions: int = 0

    @property
    def write_amplification(self) -> float:
        if self.logical_bytes_written == 0:
            return 1.0
        return self.bytes_written / self.logical_bytes_written

    def reset(self) -> None:
        self.inserts = self.updates = self.deletes = 0
        self.point_lookups = self.range_scans = 0
        self.bytes_written = self.logical_bytes_written = self.compactions = 0

    def snapshot(self) -> Dict[str, Any]:
        d = asdict(self)
        d["write_amplification"] = self.write_amplification
        return d


class Index(ABC):
    """Unified interface for B+ Tree and LSM Tree indexes."""

    index_type: str = "base"

    def __init__(self, name: str, column_name: str, unique: bool = False):
        self.name = name
        self.column_name = column_name
        self.unique = unique
        self.stats = IndexStats()

    @abstractmethod
    def insert(self, key: Any, rid: RID) -> None:
        """Insert a (key, rid) mapping into the index."""

    @abstractmethod
    def delete(self, key: Any, rid: Optional[RID] = None) -> bool:
        """Delete a key (or specific (key, rid) pair for non-unique indexes).

        Returns True if an entry was removed.
        """

    def update(self, old_key: Any, new_key: Any, old_rid: RID, new_rid: Optional[RID] = None) -> None:
        """Maintain index consistency when a record's key or RID changes."""
        target_rid = old_rid if new_rid is None else new_rid
        if old_key == new_key and old_rid == target_rid:
            return
        self.delete(old_key, old_rid)
        self.insert(new_key, target_rid)
        self.stats.updates += 1

    @abstractmethod
    def search(self, key: Any) -> List[RID]:
        """Exact-match equality lookup. Returns all matching RIDs (empty list if none)."""

    @abstractmethod
    def range_scan(
        self,
        low: Optional[Any] = None,
        high: Optional[Any] = None,
        include_low: bool = True,
        include_high: bool = True,
    ) -> Iterator[Tuple[Any, RID]]:
        """Yield (key, rid) pairs in ascending key order within [low, high]."""

    def bulk_load(self, items: Iterable[Tuple[Any, RID]]) -> int:
        """Insert an iterable of (key, rid) pairs. Returns count inserted."""
        count = 0
        for key, rid in items:
            self.insert(key, rid)
            count += 1
        return count

    @abstractmethod
    def size_bytes(self) -> int:
        """Return total on-disk footprint of this index in bytes."""

    @abstractmethod
    def flush(self) -> None:
        """Flush buffered index state to durable storage."""

    @abstractmethod
    def close(self) -> None:
        """Flush and release resources."""