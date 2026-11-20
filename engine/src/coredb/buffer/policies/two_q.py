from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Optional

from .base import ReplacementPolicy


class TwoQPolicy(ReplacementPolicy):
    """Full 2Q (Johnson & Shasha).

    A1in  : FIFO of pages seen once (resident)         ~ kin_ratio * capacity
    A1out : ghost ids of pages recently evicted from A1in (no data)
    Am    : LRU of pages re-referenced after leaving A1in (the "hot" set)
    A page found in A1out when it is loaded again goes straight to Am.
    Eviction takes from A1in while |A1in| > Kin, otherwise from Am.
    """
    name = "2q"

    def __init__(self, capacity: int, kin_ratio: float = 0.25, kout_ratio: float = 0.5):
        super().__init__(capacity)
        self.kin = max(1, int(capacity * kin_ratio))
        self.kout = max(1, int(capacity * kout_ratio))
        self._a1in: "OrderedDict[int, bool]" = OrderedDict()   # pid -> evictable
        self._am: "OrderedDict[int, bool]" = OrderedDict()
        self._a1out: "OrderedDict[int, None]" = OrderedDict()
        self._where: Dict[int, OrderedDict] = {}

    def record_access(self, page_id: int) -> None:
        q = self._where.get(page_id)
        if q is self._am:
            self._am.move_to_end(page_id)
        elif q is self._a1in:
            pass                                    # FIFO: hits in A1in do not reorder
        else:
            if page_id in self._a1out:
                del self._a1out[page_id]
                self._am[page_id] = True
                self._where[page_id] = self._am
            else:
                self._a1in[page_id] = True
                self._where[page_id] = self._a1in

    def set_evictable(self, page_id: int, evictable: bool) -> None:
        if page_id not in self._where:
            self.record_access(page_id)
        self._where[page_id][page_id] = evictable

    @staticmethod
    def _first_evictable(q: "OrderedDict[int, bool]") -> Optional[int]:
        for pid, ev in q.items():
            if ev:
                return pid
        return None

    def evict(self) -> Optional[int]:
        order = (self._a1in, self._am) if (len(self._a1in) > self.kin or not self._am) else (self._am, self._a1in)
        for q in order:
            pid = self._first_evictable(q)
            if pid is not None:
                del q[pid]
                del self._where[pid]
                if q is self._a1in:
                    self._a1out[pid] = None
                    while len(self._a1out) > self.kout:
                        self._a1out.popitem(last=False)
                return pid
        return None

    def remove(self, page_id: int) -> None:
        q = self._where.pop(page_id, None)
        if q is not None:
            q.pop(page_id, None)
        self._a1out.pop(page_id, None)

    def size(self) -> int:
        return sum(1 for q in (self._a1in, self._am) for e in q.values() if e)
