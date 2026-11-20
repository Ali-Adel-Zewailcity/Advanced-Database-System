"""Replacement-policy interface.

Policies track *resident* pages by page id.  The pool calls:
  record_access(pid)       on every fetch (hit, or first access of a freshly loaded page)
  set_evictable(pid, b)    False while pinned, True when the pin count drops to 0
  evict()                  choose + forget a victim among evictable pages (None if none)
  remove(pid)              page deleted explicitly (no ghost history kept)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class ReplacementPolicy(ABC):
    name = "base"

    def __init__(self, capacity: int):
        self.capacity = capacity

    @abstractmethod
    def record_access(self, page_id: int) -> None: ...

    @abstractmethod
    def set_evictable(self, page_id: int, evictable: bool) -> None: ...

    @abstractmethod
    def evict(self) -> Optional[int]: ...

    @abstractmethod
    def remove(self, page_id: int) -> None: ...

    @abstractmethod
    def size(self) -> int:
        """Number of currently evictable pages."""
