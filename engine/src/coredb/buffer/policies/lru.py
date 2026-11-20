from __future__ import annotations

from collections import OrderedDict
from typing import Optional

from .base import ReplacementPolicy


class LRUPolicy(ReplacementPolicy):
    """Least Recently Used.  Order is refreshed on access and when a page is unpinned."""
    name = "lru"

    def __init__(self, capacity: int):
        super().__init__(capacity)
        self._order: "OrderedDict[int, None]" = OrderedDict()   # evictable pages, LRU first
        self._pinned: set = set()

    def record_access(self, page_id: int) -> None:
        if page_id in self._pinned:
            return
        self._order[page_id] = None
        self._order.move_to_end(page_id)

    def set_evictable(self, page_id: int, evictable: bool) -> None:
        if evictable:
            self._pinned.discard(page_id)
            self._order[page_id] = None
            self._order.move_to_end(page_id)
        else:
            self._order.pop(page_id, None)
            self._pinned.add(page_id)

    def evict(self) -> Optional[int]:
        if not self._order:
            return None
        return self._order.popitem(last=False)[0]

    def remove(self, page_id: int) -> None:
        self._order.pop(page_id, None)
        self._pinned.discard(page_id)

    def size(self) -> int:
        return len(self._order)
