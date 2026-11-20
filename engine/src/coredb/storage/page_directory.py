"""Persistent page directory: logical page id -> physical location in the file.

The directory itself lives in *directory pages* chained through the database
file (physical pages, not managed by the buffer pool).  Every change is written
through immediately so the DBMS can find all pages again after a restart.

Directory page layout:  <4s magic 'DIRP'> <u32 next_phys> <u32 count> <u32 reserved>
                        then `count` entries of  <u32 phys> <u32 flags>.
"""
from __future__ import annotations

import struct
from typing import TYPE_CHECKING, List

from ..errors import CorruptDatabaseError, PageNotFoundError
from .page import INVALID_PAGE_ID

if TYPE_CHECKING:  # pragma: no cover
    from .disk_manager import DiskManager

FLAG_UNUSED, FLAG_ALLOCATED, FLAG_FREE = 0, 1, 2
_HDR = struct.Struct("<4sIII")
_ENTRY = struct.Struct("<II")
_MAGIC = b"DIRP"


class PageDirectory:
    def __init__(self, dm: "DiskManager"):
        self._dm = dm
        self.entries_per_page = (dm.page_size - _HDR.size) // _ENTRY.size
        self._phys: List[int] = []
        self._flags: List[int] = []
        self._dir_pages: List[int] = []   # physical numbers of directory pages
        self._free: List[int] = []        # reusable logical ids

    # ---- loading ---------------------------------------------------------
    def load(self, first_dir_phys: int, expected_pages: int) -> None:
        phys = first_dir_phys
        while phys != INVALID_PAGE_ID:
            raw = self._dm._read_phys(phys)
            magic, nxt, count, _ = _HDR.unpack_from(raw, 0)
            if magic != _MAGIC:
                raise CorruptDatabaseError(f"bad directory page at physical page {phys}")
            self._dir_pages.append(phys)
            for i in range(count):
                p, f = _ENTRY.unpack_from(raw, _HDR.size + i * _ENTRY.size)
                self._phys.append(p)
                self._flags.append(f)
            phys = nxt
        if len(self._phys) != expected_pages:
            raise CorruptDatabaseError("page directory is inconsistent with the file header")
        self._free = sorted((i for i, f in enumerate(self._flags) if f == FLAG_FREE), reverse=True)

    # ---- queries ---------------------------------------------------------
    def __len__(self) -> int:
        return len(self._phys)

    def locate(self, page_id: int) -> int:
        if page_id >= len(self._phys) or self._flags[page_id] != FLAG_ALLOCATED:
            raise PageNotFoundError(f"page {page_id} is not allocated")
        return self._phys[page_id]

    def is_allocated(self, page_id: int) -> bool:
        return 0 <= page_id < len(self._phys) and self._flags[page_id] == FLAG_ALLOCATED

    def allocated_count(self) -> int:
        return len(self._phys) - len(self._free)

    def allocated_ids(self) -> List[int]:
        return [i for i, f in enumerate(self._flags) if f == FLAG_ALLOCATED]

    # ---- mutations (write-through) --------------------------------------
    def allocate(self) -> int:
        if self._free:
            pid = self._free.pop()
            self._flags[pid] = FLAG_ALLOCATED
            self._flush_dir_page(pid // self.entries_per_page)
            return pid
        pid = len(self._phys)
        k = pid // self.entries_per_page
        self._phys.append(self._dm._new_phys())
        self._flags.append(FLAG_ALLOCATED)
        if k >= len(self._dir_pages):
            dp = self._dm._new_phys()
            self._dir_pages.append(dp)
            if k == 0:
                self._dm.first_dir_phys = dp
            else:
                self._flush_dir_page(k - 1)   # fix previous page's next pointer
        self._flush_dir_page(k)
        self._dm._write_header()
        return pid

    def free(self, page_id: int) -> None:
        self.locate(page_id)  # validates
        self._flags[page_id] = FLAG_FREE
        self._free.append(page_id)
        self._flush_dir_page(page_id // self.entries_per_page)

    def _flush_dir_page(self, k: int) -> None:
        epp = self.entries_per_page
        start = k * epp
        count = min(epp, len(self._phys) - start)
        nxt = self._dir_pages[k + 1] if k + 1 < len(self._dir_pages) else INVALID_PAGE_ID
        buf = bytearray(self._dm.page_size)
        _HDR.pack_into(buf, 0, _MAGIC, nxt, count, 0)
        for i in range(count):
            _ENTRY.pack_into(buf, _HDR.size + i * _ENTRY.size, self._phys[start + i], self._flags[start + i])
        self._dm._write_phys(self._dir_pages[k], buf, meta=True)
