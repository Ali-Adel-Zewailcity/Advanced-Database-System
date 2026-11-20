"""Disk manager: page-granular file I/O + persistent page directory.

File layout
  physical page 0   : file header  (magic, version, page size, #physical pages,
                      #logical pages, first directory page, user root page id)
  physical page N   : data pages and directory pages, interleaved as allocated.

Logical page ids are stable; the directory maps them to physical slots, which
lets freed pages be reused and leaves room for multi-file placement later.
"""
from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass, asdict
from typing import Dict

from ..errors import ConfigError, CorruptDatabaseError
from .page import INVALID_PAGE_ID
from .page_directory import PageDirectory

MAGIC = b"COREDB01"
FORMAT_VERSION = 1
_HEADER = struct.Struct("<8sHHIIII")  # magic, ver, page_size, n_phys, n_logical, first_dir, root


@dataclass
class IOStats:
    reads: int = 0          # data page reads
    writes: int = 0         # data page writes
    meta_reads: int = 0     # header / directory reads
    meta_writes: int = 0    # header / directory writes
    allocations: int = 0
    frees: int = 0

    def reset(self) -> None:
        self.reads = self.writes = self.meta_reads = self.meta_writes = 0
        self.allocations = self.frees = 0

    def snapshot(self) -> Dict[str, int]:
        return asdict(self)


class DiskManager:
    def __init__(self, path: str, page_size: int = 4096, fsync: bool = False):
        self.path = path
        self.page_size = page_size
        self.fsync = fsync
        self.stats = IOStats()
        self._lock = threading.RLock()
        self.num_phys = 1
        self.first_dir_phys = INVALID_PAGE_ID
        self.root_page_id = INVALID_PAGE_ID
        existed = os.path.exists(path) and os.path.getsize(path) > 0
        self._f = open(path, "r+b" if existed else "w+b", buffering=0)
        self.directory = PageDirectory(self)
        if existed:
            self._load_header()
        else:
            self._write_header()

    # ---- header ----------------------------------------------------------
    def _write_header(self) -> None:
        buf = bytearray(self.page_size)
        _HEADER.pack_into(buf, 0, MAGIC, FORMAT_VERSION, self.page_size, self.num_phys,
                          len(self.directory), self.first_dir_phys, self.root_page_id)
        self._write_phys(0, buf, meta=True)

    def _load_header(self) -> None:
        raw = self._read_phys(0, size=_HEADER.size)
        if len(raw) < _HEADER.size:
            raise CorruptDatabaseError("file too small to be a coredb database")
        magic, ver, ps, n_phys, n_log, first_dir, root = _HEADER.unpack_from(raw, 0)
        if magic != MAGIC:
            raise CorruptDatabaseError("not a coredb database file")
        if ver != FORMAT_VERSION:
            raise CorruptDatabaseError(f"unsupported format version {ver}")
        if ps != self.page_size:
            raise ConfigError(f"database was created with page_size={ps}, config says {self.page_size}")
        self.num_phys, self.first_dir_phys, self.root_page_id = n_phys, first_dir, root
        self.directory.load(first_dir, n_log)

    def set_root_page(self, page_id: int) -> None:
        self.root_page_id = page_id
        self._write_header()

    # ---- raw physical I/O -----------------------------------------------
    def _read_phys(self, phys: int, size: int | None = None) -> bytes:
        with self._lock:
            self._f.seek(phys * self.page_size)
            data = self._f.read(size or self.page_size)
            self.stats.meta_reads += 1
        if size is None and len(data) < self.page_size:
            data = data + bytes(self.page_size - len(data))
        return data

    def _write_phys(self, phys: int, data, meta: bool = False) -> None:
        with self._lock:
            self._f.seek(phys * self.page_size)
            self._f.write(data)
            if meta:
                self.stats.meta_writes += 1
            else:
                self.stats.writes += 1

    def _new_phys(self) -> int:
        p = self.num_phys
        self.num_phys += 1
        return p

    # ---- logical page API ------------------------------------------------
    def allocate_page(self) -> int:
        with self._lock:
            self.stats.allocations += 1
            return self.directory.allocate()

    def free_page(self, page_id: int) -> None:
        with self._lock:
            self.directory.free(page_id)
            self.stats.frees += 1

    def read_page(self, page_id: int, buf) -> None:
        """Fill `buf` (a bytearray of page_size) with the page's bytes."""
        with self._lock:
            phys = self.directory.locate(page_id)
            self._f.seek(phys * self.page_size)
            n = self._f.readinto(buf) or 0
            if n < self.page_size:          # page reserved but never written yet
                buf[n:] = bytes(self.page_size - n)
            self.stats.reads += 1

    def write_page(self, page_id: int, data) -> None:
        with self._lock:
            phys = self.directory.locate(page_id)
            self._write_phys(phys, data)

    @property
    def page_count(self) -> int:
        return self.directory.allocated_count()

    def file_size(self) -> int:
        return os.path.getsize(self.path)

    # ---- lifecycle -------------------------------------------------------
    def sync(self) -> None:
        with self._lock:
            self._f.flush()
            if self.fsync:
                os.fsync(self._f.fileno())

    def close(self) -> None:
        if self._f and not self._f.closed:
            self.sync()
            self._f.close()
