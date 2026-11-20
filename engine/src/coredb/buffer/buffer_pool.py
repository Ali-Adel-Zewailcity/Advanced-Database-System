"""Buffer pool manager.

* fixed number of frames, each one page (<= 4KB) of memory
* page table (page id -> frame) bounded by the number of frames
* pin counting: pinned frames are never evicted
* dirty tracking with *write-on-eviction* (plus explicit flush / checkpoint)
* pluggable replacement policy (see coredb.buffer.policies)
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from typing import Dict, Iterator, List, Optional

from ..errors import BufferPoolFullError, PageNotFoundError
from ..storage.disk_manager import DiskManager
from ..storage.page import INVALID_PAGE_ID
from .hash_table import FixedSizeHashTable
from .policies import ReplacementPolicy, create_policy


class Frame:
    __slots__ = ("frame_id", "data", "page_id", "pin_count", "dirty")

    def __init__(self, frame_id: int, size: int):
        self.frame_id = frame_id
        self.data = bytearray(size)
        self.page_id = INVALID_PAGE_ID
        self.pin_count = 0
        self.dirty = False


@dataclass
class PoolStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    dirty_evictions: int = 0     # write-on-eviction page writes
    flush_writes: int = 0        # writes caused by flush_page / flush_all
    new_pages: int = 0

    @property
    def requests(self) -> int:
        return self.hits + self.misses

    @property
    def hit_ratio(self) -> float:
        return self.hits / self.requests if self.requests else 0.0

    def reset(self) -> None:
        self.hits = self.misses = self.evictions = self.dirty_evictions = 0
        self.flush_writes = self.new_pages = 0

    def snapshot(self) -> Dict[str, float]:
        d = asdict(self)
        d["requests"], d["hit_ratio"] = self.requests, self.hit_ratio
        return d


class BufferPool:
    def __init__(self, disk: DiskManager, num_frames: int, policy: str = "lru", **policy_options):
        if num_frames < 1:
            raise ValueError("num_frames must be >= 1")
        self.disk = disk
        self.page_size = disk.page_size
        self.num_frames = num_frames
        self.stats = PoolStats()
        self._frames: List[Optional[Frame]] = [None] * num_frames   # created lazily
        self._next_unused = 0
        self._free: List[int] = []
        self._page_table = FixedSizeHashTable(num_frames)   # fixed-size: page id -> frame id
        self._zero = bytes(self.page_size)
        self._lock = threading.RLock()
        self.policy_name = policy
        self.policy: ReplacementPolicy = create_policy(policy, num_frames, **policy_options)

    # ---- policy control ----------------------------------------------------
    def set_policy(self, name: str, **options) -> None:
        """Swap the replacement policy at runtime; resident pages are re-registered."""
        with self._lock:
            new = create_policy(name, self.num_frames, **options)
            for pid, fid in self._page_table.items():
                new.record_access(pid)
                new.set_evictable(pid, self._frames[fid].pin_count == 0)
            self.policy, self.policy_name = new, name

    # ---- frame acquisition ---------------------------------------------------
    def _acquire_frame(self) -> Frame:
        if self._free:
            return self._frames[self._free.pop()]
        if self._next_unused < self.num_frames:
            f = Frame(self._next_unused, self.page_size)
            self._frames[self._next_unused] = f
            self._next_unused += 1
            return f
        victim = self.policy.evict()
        if victim is None:
            raise BufferPoolFullError(f"all {self.num_frames} frames are pinned")
        f = self._frames[self._page_table.pop(victim)]
        if f.dirty:
            self.disk.write_page(victim, f.data)       # write-on-eviction
            self.stats.dirty_evictions += 1
        self.stats.evictions += 1
        f.page_id, f.dirty, f.pin_count = INVALID_PAGE_ID, False, 0
        return f

    def _release_frame(self, f: Frame) -> None:
        f.page_id, f.dirty, f.pin_count = INVALID_PAGE_ID, False, 0
        self._free.append(f.frame_id)

    # ---- public API ------------------------------------------------------------
    def fetch_page(self, page_id: int) -> Frame:
        """Pin and return the frame holding `page_id` (reading from disk on a miss)."""
        with self._lock:
            fid = self._page_table.get(page_id)
            if fid is not None:
                f = self._frames[fid]
                self.stats.hits += 1
                self.policy.record_access(page_id)
                f.pin_count += 1
                if f.pin_count == 1:
                    self.policy.set_evictable(page_id, False)
                return f
            self.stats.misses += 1
            if not self.disk.directory.is_allocated(page_id):
                raise PageNotFoundError(f"page {page_id} is not allocated")
            f = self._acquire_frame()
            try:
                self.disk.read_page(page_id, f.data)
            except Exception:
                self._release_frame(f)
                raise
            f.page_id, f.pin_count, f.dirty = page_id, 1, False
            self._page_table[page_id] = f.frame_id
            self.policy.record_access(page_id)
            self.policy.set_evictable(page_id, False)
            return f

    def new_page(self) -> Frame:
        """Allocate a fresh zeroed page, pinned and marked dirty."""
        with self._lock:
            pid = self.disk.allocate_page()
            try:
                f = self._acquire_frame()
            except Exception:
                self.disk.free_page(pid)
                raise
            f.data[:] = self._zero
            f.page_id, f.pin_count, f.dirty = pid, 1, True
            self._page_table[pid] = f.frame_id
            self.policy.record_access(pid)
            self.policy.set_evictable(pid, False)
            self.stats.new_pages += 1
            return f

    def unpin_page(self, page_id: int, is_dirty: bool = False) -> None:
        with self._lock:
            fid = self._page_table.get(page_id)
            if fid is None:
                raise PageNotFoundError(f"page {page_id} is not in the buffer pool")
            f = self._frames[fid]
            if f.pin_count <= 0:
                raise ValueError(f"page {page_id} is not pinned")
            if is_dirty:
                f.dirty = True
            f.pin_count -= 1
            if f.pin_count == 0:
                self.policy.set_evictable(page_id, True)

    def flush_page(self, page_id: int) -> bool:
        with self._lock:
            fid = self._page_table.get(page_id)
            if fid is None:
                return False
            f = self._frames[fid]
            if f.dirty:
                self.disk.write_page(page_id, f.data)
                f.dirty = False
                self.stats.flush_writes += 1
            return True

    def flush_all(self) -> None:
        with self._lock:
            for pid in list(self._page_table):
                self.flush_page(pid)

    def checkpoint(self) -> None:
        """Flush every dirty page and force the file to stable storage."""
        self.flush_all()
        self.disk.sync()

    def delete_page(self, page_id: int) -> bool:
        """Drop a page from the pool (no write-back) and free it on disk."""
        with self._lock:
            fid = self._page_table.get(page_id)
            if fid is not None:
                f = self._frames[fid]
                if f.pin_count > 0:
                    return False
                del self._page_table[page_id]
                self.policy.remove(page_id)
                self._release_frame(f)
            self.disk.free_page(page_id)
            return True

    def close(self) -> None:
        self.checkpoint()

    # ---- helpers -----------------------------------------------------------
    @contextmanager
    def pin(self, page_id: int, write: bool = False) -> Iterator[Frame]:
        f = self.fetch_page(page_id)
        try:
            yield f
        finally:
            self.unpin_page(page_id, write)

    @contextmanager
    def new(self) -> Iterator[Frame]:
        f = self.new_page()
        try:
            yield f
        finally:
            self.unpin_page(f.page_id, True)

    def is_resident(self, page_id: int) -> bool:
        return page_id in self._page_table

    def resident_pages(self) -> List[int]:
        return list(self._page_table)

    def pinned_count(self) -> int:
        return sum(1 for fid in self._page_table.values() if self._frames[fid].pin_count > 0)

    def pin_count(self, page_id: int) -> int:
        fid = self._page_table.get(page_id)
        return 0 if fid is None else self._frames[fid].pin_count
