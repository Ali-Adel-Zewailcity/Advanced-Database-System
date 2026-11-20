"""Log-Structured Merge-Tree (LSM Tree) index for write-intensive workloads.

On-disk layout inside `base_dir` (defaults to `<db_path>.lsm_<index_name>`):
  - `wal.log`: Append-only Write-Ahead Log of PUT / DELETE records with CRC32
  - `sst_000001.sst`, `sst_000002.sst`, ...: Immutable Sorted String Tables

SSTable binary format:
  [ Entry 0 ][ Entry 1 ] ... [ Entry N-1 ]
  [ Sparse Index Block: (key_len:2B, key_bytes, file_offset:8B) * K ]
  [ Footer (40B): magic(4B) | entry_count(8B) | index_offset(8B) |
                  index_count(4B) | min_key_off(8B) | max_key_off(8B) ]

Each Entry:
  [ op:1B (1=PUT, 2=TOMBSTONE) | key_len:2B | key_bytes | page_id:4B | slot_id:4B ]
"""
from __future__ import annotations

import bisect
import os
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..errors import CorruptDatabaseError, DuplicateKeyError, IndexError
from .base import Index, RID, decode_key, encode_key

_OP_PUT = 1
_OP_DEL = 2

_WAL_HDR = struct.Struct("<IBHII")      # crc32, op, key_len, page_id, slot_id (15 bytes)
_ENTRY_HDR = struct.Struct("<BH")       # op, key_len (3 bytes)
_RID_PACK = struct.Struct("<II")        # page_id, slot_id (8 bytes)
_IDX_ENTRY = struct.Struct("<HQ")       # key_len, offset (10 bytes)
_SST_FOOTER = struct.Struct("<4sQQIQQ")  # magic, n_entries, idx_off, n_idx, min_off, max_off (40 bytes)
_SST_MAGIC = b"SST1"


# ---------------------------------------------------------------------------
# Write-Ahead Log (WAL)
# ---------------------------------------------------------------------------

class _WriteAheadLog:
    """Append-only WAL guaranteeing durability of unflushed MemTable mutations."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(self.path, "ab+")

    def append(self, op: int, key_bytes: bytes, rid: RID) -> int:
        payload = struct.pack("<BHII", op, len(key_bytes), rid[0], rid[1]) + key_bytes
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        record = struct.pack("<I", crc) + payload
        self._fp.write(record)
        self._fp.flush()
        return len(record)

    def replay(self) -> List[Tuple[int, bytes, RID]]:
        """Read all valid records from the WAL (stops cleanly on truncated tail)."""
        records: List[Tuple[int, bytes, RID]] = []
        if not self.path.exists():
            return records
        with open(self.path, "rb") as rf:
            data = rf.read()
        pos = 0
        total = len(data)
        while pos + _WAL_HDR.size <= total:
            crc, op, klen, pid, slot = _WAL_HDR.unpack_from(data, pos)
            end = pos + _WAL_HDR.size + klen
            if end > total:
                break
            payload = data[pos + 4:end]
            if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
                break
            kbytes = data[pos + _WAL_HDR.size:end]
            records.append((op, kbytes, (pid, slot)))
            pos = end
        return records

    def reset(self) -> None:
        self._fp.close()
        self._fp = open(self.path, "wb+")

    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.flush()
            self._fp.close()


# ---------------------------------------------------------------------------
# In-Memory Sorted MemTable
# ---------------------------------------------------------------------------

class _MemTable:
    """Sorted in-memory buffer keyed by (encoded_key, page_id, slot_id)."""

    def __init__(self):
        # Sorted list of composite keys: (key_bytes, page_id, slot_id)
        self._keys: List[Tuple[bytes, int, int]] = []
        # Maps (key_bytes, page_id, slot_id) -> is_tombstone (bool)
        self._tombstones: Dict[Tuple[bytes, int, int], bool] = {}
        self.approx_bytes: int = 0

    def put(self, key_bytes: bytes, rid: RID) -> None:
        ckey = (key_bytes, rid[0], rid[1])
        if ckey not in self._tombstones:
            bisect.insort(self._keys, ckey)
            self.approx_bytes += len(key_bytes) + 16
        self._tombstones[ckey] = False

    def delete(self, key_bytes: bytes, rid: RID) -> None:
        ckey = (key_bytes, rid[0], rid[1])
        if ckey not in self._tombstones:
            bisect.insort(self._keys, ckey)
            self.approx_bytes += len(key_bytes) + 16
        self._tombstones[ckey] = True

    def get_key_entries(self, key_bytes: bytes) -> List[Tuple[RID, bool]]:
        """Return [(rid, is_tombstone), ...] for all entries matching `key_bytes`."""
        lo = bisect.bisect_left(self._keys, (key_bytes, 0, 0))
        out: List[Tuple[RID, bool]] = []
        for i in range(lo, len(self._keys)):
            kb, pid, slot = self._keys[i]
            if kb != key_bytes:
                break
            out.append(((pid, slot), self._tombstones[(kb, pid, slot)]))
        return out

    def iter_items(self) -> Iterator[Tuple[bytes, RID, bool]]:
        for kb, pid, slot in self._keys:
            yield kb, (pid, slot), self._tombstones[(kb, pid, slot)]

    def __len__(self) -> int:
        return len(self._keys)

    def clear(self) -> None:
        self._keys.clear()
        self._tombstones.clear()
        self.approx_bytes = 0


# ---------------------------------------------------------------------------
# Immutable On-Disk SSTable Reader / Writer
# ---------------------------------------------------------------------------

class _SSTable:
    """Immutable on-disk Sorted String Table with sparse index and min/max bounds."""

    def __init__(self, path: Path, seq: int, level: int = 0):
        self.path = path
        self.seq = seq
        self.level = level
        self.entry_count: int = 0
        self._index_offset: int = 0
        self._sparse_index: List[Tuple[bytes, int]] = []
        self.min_key: Optional[bytes] = None
        self.max_key: Optional[bytes] = None
        self._load_footer()

    def _load_footer(self) -> None:
        size = self.path.stat().st_size
        if size < _SST_FOOTER.size:
            raise CorruptDatabaseError(f"SSTable {self.path} is truncated")
        with open(self.path, "rb") as f:
            f.seek(size - _SST_FOOTER.size)
            magic, n_entries, idx_off, n_idx, min_off, max_off = _SST_FOOTER.unpack(
                f.read(_SST_FOOTER.size)
            )
            if magic != _SST_MAGIC:
                raise CorruptDatabaseError(f"bad SSTable magic in {self.path}")
            self.entry_count = n_entries
            self._index_offset = idx_off

            # Read sparse index
            f.seek(idx_off)
            sparse: List[Tuple[bytes, int]] = []
            for _ in range(n_idx):
                klen, off = _IDX_ENTRY.unpack(f.read(_IDX_ENTRY.size))
                kb = f.read(klen)
                sparse.append((kb, off))
            self._sparse_index = sparse

            if n_entries > 0:
                self.min_key = self._read_key_at(f, min_off)
                self.max_key = self._read_key_at(f, max_off)

    @staticmethod
    def _read_key_at(fp, offset: int) -> bytes:
        fp.seek(offset)
        _, klen = _ENTRY_HDR.unpack(fp.read(_ENTRY_HDR.size))
        return fp.read(klen)

    @classmethod
    def write_from_stream(
        cls,
        path: Path,
        seq: int,
        level: int,
        items: Iterator[Tuple[bytes, RID, bool]],
        sparse_interval: int = 32,
    ) -> Tuple["_SSTable", int]:
        """Write sorted `(key_bytes, rid, is_tombstone)` stream to disk.

        Returns `(SSTable, bytes_written)`.
        """
        sparse: List[Tuple[bytes, int]] = []
        n_entries = 0
        min_off = 0
        max_off = 0

        with open(path, "wb") as f:
            for kb, rid, is_del in items:
                off = f.tell()
                if n_entries == 0:
                    min_off = off
                max_off = off
                if n_entries % sparse_interval == 0:
                    sparse.append((kb, off))
                op = _OP_DEL if is_del else _OP_PUT
                f.write(_ENTRY_HDR.pack(op, len(kb)))
                f.write(kb)
                f.write(_RID_PACK.pack(rid[0], rid[1]))
                n_entries += 1

            idx_off = f.tell()
            for kb, off in sparse:
                f.write(_IDX_ENTRY.pack(len(kb), off))
                f.write(kb)

            f.write(_SST_FOOTER.pack(_SST_MAGIC, n_entries, idx_off, len(sparse), min_off, max_off))
            total_written = f.tell()

        return cls(path, seq=seq, level=level), total_written

    def may_contain(self, key_bytes: bytes) -> bool:
        if self.entry_count == 0 or self.min_key is None or self.max_key is None:
            return False
        return self.min_key <= key_bytes <= self.max_key

    def _seek_offset_for(self, key_bytes: bytes) -> int:
        if not self._sparse_index:
            return 0
        keys = [k for k, _ in self._sparse_index]
        pos = bisect.bisect_right(keys, key_bytes) - 1
        return self._sparse_index[max(0, pos)][1]

    def get_key_entries(self, key_bytes: bytes) -> List[Tuple[RID, bool]]:
        if not self.may_contain(key_bytes):
            return []
        start_off = self._seek_offset_for(key_bytes)
        out: List[Tuple[RID, bool]] = []
        with open(self.path, "rb") as f:
            f.seek(start_off)
            while f.tell() < self._index_offset:
                op, klen = _ENTRY_HDR.unpack(f.read(_ENTRY_HDR.size))
                kb = f.read(klen)
                pid, slot = _RID_PACK.unpack(f.read(_RID_PACK.size))
                if kb < key_bytes:
                    continue
                if kb == key_bytes:
                    out.append(((pid, slot), op == _OP_DEL))
                else:
                    break
        return out

    def iter_items(self, start_key: Optional[bytes] = None) -> Iterator[Tuple[bytes, RID, bool]]:
        if self.entry_count == 0:
            return
        if start_key is not None and self.max_key is not None and start_key > self.max_key:
            return
        start_off = 0 if start_key is None else self._seek_offset_for(start_key)
        with open(self.path, "rb") as f:
            f.seek(start_off)
            while f.tell() < self._index_offset:
                op, klen = _ENTRY_HDR.unpack(f.read(_ENTRY_HDR.size))
                kb = f.read(klen)
                pid, slot = _RID_PACK.unpack(f.read(_RID_PACK.size))
                if start_key is not None and kb < start_key:
                    continue
                yield kb, (pid, slot), (op == _OP_DEL)

    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0


# ---------------------------------------------------------------------------
# LSM Tree Index Implementation
# ---------------------------------------------------------------------------

class LSMTreeIndex(Index):
    """Write-optimized LSM Tree index with WAL, MemTable, SSTables, and Compaction."""

    index_type = "lsm"

    def __init__(
        self,
        storage_dir: str | Path,
        name: str,
        column_name: str,
        unique: bool = False,
        memtable_max_bytes: int = 32 * 1024,
        compaction_threshold: int = 4,
    ):
        super().__init__(name=name, column_name=column_name, unique=unique)
        self.dir = Path(storage_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.memtable_max_bytes = memtable_max_bytes
        self.compaction_threshold = compaction_threshold

        self._memtable = _MemTable()
        self._wal = _WriteAheadLog(self.dir / "wal.log")
        self._sstables: List[_SSTable] = []     # ordered oldest -> newest
        self._next_seq = 1

        self._load_existing()

    def _load_existing(self) -> None:
        # 1. Load existing SSTables sorted by sequence number (oldest -> newest)
        sst_files = sorted(self.dir.glob("sst_*.sst"))
        for p in sst_files:
            # Filename format: sst_<seq:06d>_L<level>.sst
            stem = p.stem
            parts = stem.split("_")
            seq = int(parts[1])
            level = int(parts[2][1:]) if len(parts) > 2 and parts[2].startswith("L") else 0
            sst = _SSTable(p, seq=seq, level=level)
            self._sstables.append(sst)
            self._next_seq = max(self._next_seq, seq + 1)

        # 2. Replay WAL into MemTable for crash recovery
        for op, kbytes, rid in self._wal.replay():
            if op == _OP_PUT:
                self._memtable.put(kbytes, rid)
            elif op == _OP_DEL:
                self._memtable.delete(kbytes, rid)

    # ---- Write Path (WAL -> MemTable -> Flush -> Compact) ------------------

    def insert(self, key: Any, rid: RID) -> None:
        kbytes = encode_key(key)
        if self.unique and self.search(key):
            raise DuplicateKeyError(f"duplicate key {key!r} in unique LSM index '{self.name}'")

        wal_bytes = self._wal.append(_OP_PUT, kbytes, rid)
        self._memtable.put(kbytes, rid)

        self.stats.inserts += 1
        self.stats.logical_bytes_written += len(kbytes) + 8
        self.stats.bytes_written += wal_bytes

        if self._memtable.approx_bytes >= self.memtable_max_bytes:
            self.flush_memtable()

    def delete(self, key: Any, rid: Optional[RID] = None) -> bool:
        kbytes = encode_key(key)
        target_rids = self.search(key) if rid is None else [rid]
        if not target_rids:
            return False

        for target in target_rids:
            wal_bytes = self._wal.append(_OP_DEL, kbytes, target)
            self._memtable.delete(kbytes, target)
            self.stats.deletes += 1
            self.stats.logical_bytes_written += len(kbytes) + 8
            self.stats.bytes_written += wal_bytes

        if self._memtable.approx_bytes >= self.memtable_max_bytes:
            self.flush_memtable()
        return True

    def flush_memtable(self) -> None:
        """Flush sorted MemTable entries into a new immutable Level-0 SSTable."""
        if len(self._memtable) == 0:
            return
        seq = self._next_seq
        self._next_seq += 1
        sst_path = self.dir / f"sst_{seq:06d}_L0.sst"
        sst, written = _SSTable.write_from_stream(
            sst_path, seq=seq, level=0, items=self._memtable.iter_items()
        )
        self._sstables.append(sst)
        self.stats.bytes_written += written

        self._memtable.clear()
        self._wal.reset()

        if len(self._sstables) >= self.compaction_threshold:
            self.compact()

    def compact(self) -> None:
        """Merge all active SSTables into a single compacted SSTable.

        Resolves multiple versions of the same (key, rid) keeping the newest state,
        and drops tombstones since no older levels exist beneath the merged output.
        """
        if len(self._sstables) <= 1:
            return

        # Merge newest -> oldest so the newest mutation wins for each (key, rid)
        latest_state: Dict[Tuple[bytes, RID], bool] = {}
        for sst in reversed(self._sstables):
            for kb, rid, is_del in sst.iter_items():
                ckey = (kb, rid)
                if ckey not in latest_state:
                    latest_state[ckey] = is_del

        # Keep only live (non-tombstone) entries sorted by (key_bytes, page_id, slot_id)
        live_items = sorted(
            (kb, rid, False)
            for (kb, rid), is_del in latest_state.items()
            if not is_del
        )

        seq = self._next_seq
        self._next_seq += 1
        new_level = max(s.level for s in self._sstables) + 1
        compacted_path = self.dir / f"sst_{seq:06d}_L{new_level}.sst"
        new_sst, written = _SSTable.write_from_stream(
            compacted_path, seq=seq, level=new_level, items=iter(live_items)
        )

        old_tables = self._sstables
        self._sstables = [new_sst]
        for old in old_tables:
            if old.path.exists():
                old.path.unlink()

        self.stats.compactions += 1
        self.stats.bytes_written += written

    # ---- Read Path (MemTable + Newest-to-Oldest SSTables) ------------------

    def search(self, key: Any) -> List[RID]:
        self.stats.point_lookups += 1
        kbytes = encode_key(key)

        # Track seen RIDs from newest to oldest so tombstones mask older PUTs
        seen: Dict[RID, bool] = {}

        for rid, is_del in self._memtable.get_key_entries(kbytes):
            if rid not in seen:
                seen[rid] = is_del

        for sst in reversed(self._sstables):
            for rid, is_del in sst.get_key_entries(kbytes):
                if rid not in seen:
                    seen[rid] = is_del

        return sorted(rid for rid, is_del in seen.items() if not is_del)

    def range_scan(
        self,
        low: Optional[Any] = None,
        high: Optional[Any] = None,
        include_low: bool = True,
        include_high: bool = True,
    ) -> Iterator[Tuple[Any, RID]]:
        self.stats.range_scans += 1
        low_b = encode_key(low) if low is not None else None
        high_b = encode_key(high) if high is not None else None

        # Merge newest -> oldest across MemTable and SSTables
        latest: Dict[Tuple[bytes, RID], bool] = {}

        for kb, rid, is_del in self._memtable.iter_items():
            if low_b is not None and (kb < low_b or (kb == low_b and not include_low)):
                continue
            if high_b is not None and (kb > high_b or (kb == high_b and not include_high)):
                break
            latest[(kb, rid)] = is_del

        for sst in reversed(self._sstables):
            for kb, rid, is_del in sst.iter_items(start_key=low_b):
                if low_b is not None and (kb < low_b or (kb == low_b and not include_low)):
                    continue
                if high_b is not None and (kb > high_b or (kb == high_b and not include_high)):
                    break
                ckey = (kb, rid)
                if ckey not in latest:
                    latest[ckey] = is_del

        for (kb, rid), is_del in sorted(latest.items(), key=lambda x: x[0]):
            if not is_del:
                yield decode_key(kb), rid

    # ---- Lifecycle & Metrics ----------------------------------------------

    @property
    def sstable_count(self) -> int:
        return len(self._sstables)

    def size_bytes(self) -> int:
        return self._wal.size_bytes() + sum(s.size_bytes() for s in self._sstables)

    def flush(self) -> None:
        self.flush_memtable()

    def close(self) -> None:
        self.flush_memtable()
        self._wal.close()