"""Slotted page used by NSM.

  [ header 16B ][ slot array -> ............ free ............ <- records ]

header  <B type><B flags><H n_slots><H free_end><H dead_bytes><H n_tombstones><I next_page><2x>
slot    <H offset><H length>   offset == 0 marks a deleted slot (slot numbers stay
        stable, so record ids (page, slot) never change when other records move).
"""
from __future__ import annotations

import struct
from typing import Iterator, Optional, Tuple

from ...errors import RecordTooLargeError
from ..page import INVALID_PAGE_ID, PageType

_HDR = struct.Struct("<BBHHHHIxx")
_SLOT = struct.Struct("<HH")
HEADER_SIZE = _HDR.size          # 16
SLOT_SIZE = _SLOT.size           # 4


class SlottedPage:
    __slots__ = ("data", "size")

    def __init__(self, data: bytearray):
        self.data = data
        self.size = len(data)

    @classmethod
    def init(cls, data: bytearray) -> "SlottedPage":
        _HDR.pack_into(data, 0, PageType.NSM_DATA, 0, 0, len(data), 0, 0, INVALID_PAGE_ID)
        return cls(data)

    # ---- header fields ---------------------------------------------------
    def _h(self):
        return _HDR.unpack_from(self.data, 0)

    def _set(self, n_slots=None, free_end=None, dead=None, tomb=None, nxt=None) -> None:
        t, fl, ns, fe, dd, tb, nx = self._h()
        _HDR.pack_into(self.data, 0, t, fl,
                       ns if n_slots is None else n_slots,
                       fe if free_end is None else free_end,
                       dd if dead is None else dead,
                       tb if tomb is None else tomb,
                       nx if nxt is None else nxt)

    @property
    def num_slots(self) -> int:
        return self._h()[2]

    @property
    def next_page(self) -> int:
        return self._h()[6]

    @next_page.setter
    def next_page(self, v: int) -> None:
        self._set(nxt=v)

    def max_record_size(self) -> int:
        return self.size - HEADER_SIZE - SLOT_SIZE

    def free_space(self) -> int:
        """Bytes usable for a new record (incl. space reclaimable by compaction)."""
        _, _, ns, fe, dd, tb, _ = self._h()
        contiguous = fe - (HEADER_SIZE + ns * SLOT_SIZE)
        return contiguous + dd - (0 if tb else SLOT_SIZE)

    # ---- record operations -------------------------------------------------
    def insert(self, rec: bytes) -> Optional[int]:
        n = len(rec)
        if n == 0:
            raise ValueError("empty record")
        if n > self.max_record_size():
            raise RecordTooLargeError(f"record of {n} bytes cannot fit in a {self.size}-byte page")
        _, _, ns, fe, dd, tb, _ = self._h()
        slot = -1
        if tb:
            for i in range(ns):
                if _SLOT.unpack_from(self.data, HEADER_SIZE + i * SLOT_SIZE)[0] == 0:
                    slot = i
                    break
        need = n if slot >= 0 else n + SLOT_SIZE
        contiguous = fe - (HEADER_SIZE + ns * SLOT_SIZE)
        if contiguous < need:
            if contiguous + dd < need:
                return None
            self.compact()
            _, _, ns, fe, dd, tb, _ = self._h()
        off = fe - n
        self.data[off:off + n] = rec
        if slot >= 0:
            _SLOT.pack_into(self.data, HEADER_SIZE + slot * SLOT_SIZE, off, n)
            self._set(free_end=off, tomb=tb - 1)
        else:
            slot = ns
            _SLOT.pack_into(self.data, HEADER_SIZE + slot * SLOT_SIZE, off, n)
            self._set(n_slots=ns + 1, free_end=off)
        return slot

    def get(self, slot: int) -> Optional[bytes]:
        if slot < 0 or slot >= self.num_slots:
            return None
        off, ln = _SLOT.unpack_from(self.data, HEADER_SIZE + slot * SLOT_SIZE)
        if off == 0:
            return None
        return bytes(self.data[off:off + ln])

    def delete(self, slot: int) -> bool:
        if slot < 0 or slot >= self.num_slots:
            return False
        pos = HEADER_SIZE + slot * SLOT_SIZE
        off, ln = _SLOT.unpack_from(self.data, pos)
        if off == 0:
            return False
        _SLOT.pack_into(self.data, pos, 0, 0)
        _, _, _, _, dd, tb, _ = self._h()
        self._set(dead=dd + ln, tomb=tb + 1)
        return True

    def update(self, slot: int, rec: bytes) -> bool:
        """In-page update.  Returns False when the new record cannot fit in this page."""
        if slot < 0 or slot >= self.num_slots or not rec:
            return False
        pos = HEADER_SIZE + slot * SLOT_SIZE
        off, ln = _SLOT.unpack_from(self.data, pos)
        if off == 0:
            return False
        n = len(rec)
        _, _, ns, fe, dd, tb, _ = self._h()
        if n <= ln:                                   # shrink / same size: rewrite in place
            self.data[off:off + n] = rec
            _SLOT.pack_into(self.data, pos, off, n)
            self._set(dead=dd + ln - n)
            return True
        contiguous = fe - (HEADER_SIZE + ns * SLOT_SIZE)
        if contiguous + dd + ln < n:
            return False
        _SLOT.pack_into(self.data, pos, 0, 0)         # free old copy (slot stays reserved)
        self._set(dead=dd + ln)
        if contiguous < n:
            self.compact()
        _, _, _, fe, _, _, _ = self._h()
        new_off = fe - n
        self.data[new_off:new_off + n] = rec
        _SLOT.pack_into(self.data, pos, new_off, n)
        self._set(free_end=new_off)
        return True

    def compact(self) -> None:
        """Squeeze out dead bytes; slot numbers are preserved."""
        old = bytes(self.data)
        ns = self.num_slots
        pos_end = self.size
        for i in range(ns):
            sp = HEADER_SIZE + i * SLOT_SIZE
            off, ln = _SLOT.unpack_from(old, sp)
            if off == 0:
                continue
            pos_end -= ln
            self.data[pos_end:pos_end + ln] = old[off:off + ln]
            _SLOT.pack_into(self.data, sp, pos_end, ln)
        self._set(free_end=pos_end, dead=0)

    def records(self) -> Iterator[Tuple[int, bytes]]:
        for i in range(self.num_slots):
            off, ln = _SLOT.unpack_from(self.data, HEADER_SIZE + i * SLOT_SIZE)
            if off:
                yield i, bytes(self.data[off:off + ln])
