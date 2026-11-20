from __future__ import annotations

from typing import Dict, List, Optional

from .base import ReplacementPolicy


class ClockPolicy(ReplacementPolicy):
    """CLOCK (second chance): circular buffer with a reference bit per frame slot."""
    name = "clock"

    def __init__(self, capacity: int, initial_ref: bool = True):
        super().__init__(capacity)
        self._pid: List[Optional[int]] = [None] * capacity
        self._ref = [False] * capacity
        self._evictable = [False] * capacity
        self._slot: Dict[int, int] = {}
        self._free = list(range(capacity - 1, -1, -1))
        self._hand = 0
        self._initial_ref = initial_ref

    def _grow(self) -> None:
        n = len(self._pid)
        self._pid.append(None); self._ref.append(False); self._evictable.append(False)
        self._free.append(n)
        self.capacity = n + 1

    def record_access(self, page_id: int) -> None:
        s = self._slot.get(page_id)
        if s is None:
            if not self._free:
                self._grow()
            s = self._free.pop()
            self._slot[page_id] = s
            self._pid[s] = page_id
            self._evictable[s] = True
            self._ref[s] = self._initial_ref
        else:
            self._ref[s] = True

    def set_evictable(self, page_id: int, evictable: bool) -> None:
        if page_id not in self._slot:
            self.record_access(page_id)
        self._evictable[self._slot[page_id]] = evictable

    def evict(self) -> Optional[int]:
        n = len(self._pid)
        for _ in range(2 * n):
            s = self._hand
            self._hand = (s + 1) % n
            if self._pid[s] is None or not self._evictable[s]:
                continue
            if self._ref[s]:
                self._ref[s] = False       # second chance
                continue
            pid = self._pid[s]
            self._clear(s)
            return pid
        return None

    def _clear(self, s: int) -> None:
        del self._slot[self._pid[s]]
        self._pid[s] = None
        self._ref[s] = self._evictable[s] = False
        self._free.append(s)

    def remove(self, page_id: int) -> None:
        s = self._slot.get(page_id)
        if s is not None:
            self._clear(s)

    def size(self) -> int:
        return sum(1 for s in self._slot.values() if self._evictable[s])
