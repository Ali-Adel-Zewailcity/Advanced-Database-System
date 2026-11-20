from __future__ import annotations

import heapq
from collections import OrderedDict, deque
from typing import Deque, Dict, List, Optional, Tuple

from .base import ReplacementPolicy


class LRUKPolicy(ReplacementPolicy):
    """LRU-K (O'Neil et al.).

    Victim = evictable page with the largest backward K-distance.  Pages with
    fewer than K recorded accesses have infinite distance and go first (oldest
    first access first).  History of evicted pages is retained (bounded by
    `history_limit`) so a re-fetched page keeps its access past.
    Implementation: lazy-deletion min-heap keyed by (has_k_refs, K-th last access).
    """
    name = "lru-k"

    def __init__(self, capacity: int, k: int = 2, history_limit: Optional[int] = None):
        super().__init__(capacity)
        if k < 1:
            raise ValueError("k must be >= 1")
        self.k = k
        self.history_limit = history_limit if history_limit is not None else 2 * capacity
        self._clock = 0
        self._hist: Dict[int, Deque[int]] = {}
        self._resident: Dict[int, bool] = {}            # pid -> evictable
        self._ghosts: "OrderedDict[int, None]" = OrderedDict()
        self._ver: Dict[int, int] = {}
        self._heap: List[Tuple[int, int, int, int]] = []  # (cls, t, ver, pid)

    def _push(self, pid: int) -> None:
        h = self._hist[pid]
        self._ver[pid] = self._ver.get(pid, 0) + 1
        heapq.heappush(self._heap, (1 if len(h) >= self.k else 0, h[0], self._ver[pid], pid))
        if len(self._heap) > 8 * max(16, len(self._resident)):
            self._rebuild()

    def _rebuild(self) -> None:
        self._heap = []
        for pid, ev in self._resident.items():
            if ev:
                h = self._hist[pid]
                heapq.heappush(self._heap, (1 if len(h) >= self.k else 0, h[0], self._ver[pid], pid))
        heapq.heapify(self._heap)

    def record_access(self, page_id: int) -> None:
        self._clock += 1
        h = self._hist.get(page_id)
        if h is None:
            h = self._hist[page_id] = deque(maxlen=self.k)
        h.append(self._clock)
        self._ghosts.pop(page_id, None)
        if page_id not in self._resident:
            self._resident[page_id] = True
        if self._resident[page_id]:
            self._push(page_id)
        else:
            self._ver[page_id] = self._ver.get(page_id, 0) + 1

    def set_evictable(self, page_id: int, evictable: bool) -> None:
        if page_id not in self._resident:
            self.record_access(page_id)
        self._resident[page_id] = evictable
        if evictable:
            self._push(page_id)
        else:
            self._ver[page_id] = self._ver.get(page_id, 0) + 1   # invalidate heap entries

    def evict(self) -> Optional[int]:
        while self._heap:
            _, _, ver, pid = heapq.heappop(self._heap)
            if self._resident.get(pid) and self._ver.get(pid) == ver:
                del self._resident[pid]
                self._ghosts[pid] = None
                while len(self._ghosts) > self.history_limit:
                    old, _ = self._ghosts.popitem(last=False)
                    self._hist.pop(old, None)
                    self._ver.pop(old, None)
                return pid
        return None

    def remove(self, page_id: int) -> None:
        self._resident.pop(page_id, None)
        self._hist.pop(page_id, None)
        self._ghosts.pop(page_id, None)
        self._ver.pop(page_id, None)

    def size(self) -> int:
        return sum(1 for e in self._resident.values() if e)
