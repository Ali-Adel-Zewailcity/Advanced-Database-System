"""ColumnStore: all values of ONE column, stored in their own pages (DSM building block).

Column header page  = dedicated metadata area for the whole column
    <4s 'CLH1'><B type><B flags(bit0: min/max valid)><B width(0=var)><B sort_state>
    <I num_rows><I null_count><I num_data_pages><I next_dir_page><8s min><8s max>
    ... then (page_id, first_row) directory entries starting at offset 48.
    More entries spill into chained overflow pages ('CLD1').

Data page (fixed width)   : 16B header | null bitmap | packed values   (O(1) access)
Data page (variable width): 16B header | slot array (off,len)... free ... values <- end
    data header: <B kind><B flags><H count><I first_row><I extra><4x>

The store speaks *storage values* (python int/float/bool/str/bytes); the table
layer does the python <-> storage conversion.  Row position == array index.
"""
from __future__ import annotations

import struct
from bisect import bisect_right
from typing import Any, Iterator, List, Optional, Sequence, Tuple

from ...catalog.types import TYPE_CODES, Column
from ...errors import CorruptDatabaseError, RecordNotFoundError, RecordTooLargeError
from ..page import INVALID_PAGE_ID

_CH = struct.Struct("<4sBBBBIIII8s8s")
_CH_MAGIC = b"CLH1"
_OVF = struct.Struct("<4sI")
_OVF_MAGIC = b"CLD1"
_DH = struct.Struct("<BBHII4x")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_ENTRY = struct.Struct("<II")
_SLOT = struct.Struct("<HH")
_HDR_ENTRIES_OFF = 48
_OVF_ENTRIES_OFF = 16
_NULL_OFF = 0xFFFF
KIND_FIXED, KIND_VAR = 1, 2
SORT_NONE, SORT_ASC, SORT_DESC = 0, 1, 2


class ColumnStore:
    def __init__(self, pool, column: Column, header_page_id: int):
        self.pool, self.column, self.header_page_id = pool, column, header_page_id
        self.ps = pool.page_size
        self.num_rows = 0
        self.null_count = 0
        self.sort_state = SORT_NONE
        self.minmax_valid = column.is_fixed
        self._min: Any = None
        self._max: Any = None
        self._dir: List[Tuple[int, int]] = []     # (page_id, first_row)
        self._firsts: List[int] = []
        self._ovf: List[int] = []                 # overflow directory pages
        self._hdr_cap = (self.ps - _HDR_ENTRIES_OFF) // _ENTRY.size
        self._ovf_cap = (self.ps - _OVF_ENTRIES_OFF) // _ENTRY.size
        if column.is_fixed:
            w = column.width
            n = ((self.ps - 16) * 8) // (8 * w + 1)
            while 16 + (n + 7) // 8 + n * w > self.ps:
                n -= 1
            self._cap, self._nb = n, (n + 7) // 8
        else:
            self._cap = self._nb = 0

    # ---- create / open -------------------------------------------------------
    @classmethod
    def create(cls, pool, column: Column) -> "ColumnStore":
        with pool.new() as h:
            hid = h.page_id
        cs = cls(pool, column, hid)
        cs._write_header()
        return cs

    @classmethod
    def open(cls, pool, column: Column, header_page_id: int) -> "ColumnStore":
        cs = cls(pool, column, header_page_id)
        with pool.pin(header_page_id) as h:
            (magic, _t, flags, _w, sort, rows, nulls, npages, nxt, mn, mx) = _CH.unpack_from(h.data, 0)
            if magic != _CH_MAGIC:
                raise CorruptDatabaseError("bad column header page")
            cs.num_rows, cs.null_count, cs.sort_state = rows, nulls, sort
            cs.minmax_valid = bool(flags & 1) and column.is_fixed
            if cs.minmax_valid and rows - nulls > 0:
                cs._min, cs._max = column.unpack_from(mn, 0), column.unpack_from(mx, 0)
            entries = []
            for i in range(min(npages, cs._hdr_cap)):
                entries.append(_ENTRY.unpack_from(h.data, _HDR_ENTRIES_OFF + i * 8))
        while len(entries) < npages and nxt != INVALID_PAGE_ID:
            cs._ovf.append(nxt)
            with pool.pin(nxt) as o:
                _, nxt = _OVF.unpack_from(o.data, 0)
                for i in range(min(cs._ovf_cap, npages - len(entries))):
                    entries.append(_ENTRY.unpack_from(o.data, _OVF_ENTRIES_OFF + i * 8))
        cs._dir = [tuple(e) for e in entries]
        cs._firsts = [e[1] for e in cs._dir]
        return cs

    # ---- header / directory persistence ------------------------------------------
    def _write_header(self) -> None:
        col = self.column
        mn = mx = bytes(8)
        if self.minmax_valid and self._min is not None:
            mn, mx = col.pack(self._min).ljust(8, b"\0"), col.pack(self._max).ljust(8, b"\0")
        nxt = self._ovf[0] if self._ovf else INVALID_PAGE_ID
        with self.pool.pin(self.header_page_id, write=True) as h:
            _CH.pack_into(h.data, 0, _CH_MAGIC, TYPE_CODES[col.type], 1 if self.minmax_valid else 0,
                          col.width, self.sort_state, self.num_rows, self.null_count,
                          len(self._dir), nxt, mn, mx)

    flush_meta = _write_header

    def _ensure_ovf(self, k: int) -> None:
        while len(self._ovf) <= k:
            with self.pool.new() as o:
                _OVF.pack_into(o.data, 0, _OVF_MAGIC, INVALID_PAGE_ID)
                pid = o.page_id
            if self._ovf:
                with self.pool.pin(self._ovf[-1], write=True) as prev:
                    _OVF.pack_into(prev.data, 0, _OVF_MAGIC, pid)
            self._ovf.append(pid)

    def _write_entry(self, i: int) -> None:
        if i < self._hdr_cap:
            page, off = self.header_page_id, _HDR_ENTRIES_OFF + i * 8
        else:
            j = i - self._hdr_cap
            k = j // self._ovf_cap
            self._ensure_ovf(k)
            page, off = self._ovf[k], _OVF_ENTRIES_OFF + (j % self._ovf_cap) * 8
        with self.pool.pin(page, write=True) as f:
            _ENTRY.pack_into(f.data, off, *self._dir[i])

    def _write_directory(self) -> None:
        for i in range(len(self._dir)):
            self._write_entry(i)
        self._write_header()

    # ---- page-level helpers ----------------------------------------------------------
    def _init_page(self, data: bytearray, first_row: int) -> None:
        if self.column.is_fixed:
            _DH.pack_into(data, 0, KIND_FIXED, 0, 0, first_row, self._cap)
        else:
            _DH.pack_into(data, 0, KIND_VAR, 0, 0, first_row, self.ps)

    def _page_append(self, data: bytearray, raw: Any) -> bool:
        cnt = _U16.unpack_from(data, 2)[0]
        if self.column.is_fixed:
            if cnt >= self._cap:
                return False
            if raw is None:
                data[16 + (cnt >> 3)] |= 1 << (cnt & 7)
            else:
                self.column.pack_into(data, 16 + self._nb + cnt * self.column.width, raw)
            _U16.pack_into(data, 2, cnt + 1)
            return True
        free_end = _U32.unpack_from(data, 8)[0]
        slot_end = 16 + (cnt + 1) * 4
        if raw is None:
            if slot_end > free_end:
                return False
            _SLOT.pack_into(data, 16 + cnt * 4, _NULL_OFF, 0)
        else:
            b = self.column.encode_var(raw)
            if len(b) > self.ps - 16 - 4:
                raise RecordTooLargeError(f"value of column {self.column.name!r} does not fit in a page")
            if free_end - len(b) < slot_end:
                return False
            off = free_end - len(b)
            data[off:off + len(b)] = b
            _SLOT.pack_into(data, 16 + cnt * 4, off, len(b))
            _U32.pack_into(data, 8, off)
        _U16.pack_into(data, 2, cnt + 1)
        return True

    def _page_get(self, data, j: int) -> Any:
        col = self.column
        if col.is_fixed:
            if data[16 + (j >> 3)] >> (j & 7) & 1:
                return None
            return col.unpack_from(data, 16 + self._nb + j * col.width)
        off, ln = _SLOT.unpack_from(data, 16 + j * 4)
        if off == _NULL_OFF:
            return None
        return col.decode_var(data[off:off + ln])

    def _page_values(self, data) -> List[Any]:
        cnt = _U16.unpack_from(data, 2)[0]
        col = self.column
        if col.is_fixed:
            nb, w, up = self._nb, col.width, col.unpack_from
            return [None if data[16 + (j >> 3)] >> (j & 7) & 1 else up(data, 16 + nb + j * w)
                    for j in range(cnt)]
        out = []
        for j in range(cnt):
            off, ln = _SLOT.unpack_from(data, 16 + j * 4)
            out.append(None if off == _NULL_OFF else col.decode_var(data[off:off + ln]))
        return out

    # ---- append ---------------------------------------------------------------------------
    def _note(self, raw: Any) -> None:
        if raw is None:
            self.null_count += 1
        elif self.minmax_valid:
            if self._min is None or raw < self._min:
                self._min = raw
            if self._max is None or raw > self._max:
                self._max = raw

    def append_many(self, raws: Sequence[Any]) -> int:
        """Append storage values; returns the row index of the first one."""
        first = self.num_rows
        if not raws:
            return first
        self.sort_state = SORT_NONE
        pool = self.pool
        cur = self._dir[-1][0] if self._dir else None
        f = pool.fetch_page(cur) if cur is not None else None
        dirty = False
        try:
            for raw in raws:
                if f is None or not self._page_append(f.data, raw):
                    if f is not None:
                        pool.unpin_page(cur, dirty)
                        f, dirty = None, False
                    f = pool.new_page()
                    cur, dirty = f.page_id, True
                    self._init_page(f.data, self.num_rows)
                    self._dir.append((cur, self.num_rows))
                    self._firsts.append(self.num_rows)
                    pool.unpin_page(cur, True)           # persist directory entry (pins header)
                    self._write_entry(len(self._dir) - 1)
                    f = pool.fetch_page(cur)
                    if not self._page_append(f.data, raw):
                        raise RecordTooLargeError("value does not fit in an empty page")
                dirty = True
                self._note(raw)
                self.num_rows += 1
        finally:
            if f is not None:
                pool.unpin_page(cur, dirty)
            self._write_header()
        return first

    def append(self, raw: Any) -> int:
        return self.append_many([raw])

    # ---- point access -----------------------------------------------------------------------
    def _locate(self, row: int) -> Tuple[int, int, int]:
        if row < 0 or row >= self.num_rows:
            raise RecordNotFoundError(f"row {row} out of range (0..{self.num_rows - 1})")
        k = bisect_right(self._firsts, row) - 1
        pid, first = self._dir[k]
        return k, pid, row - first

    def get(self, row: int) -> Any:
        _, pid, j = self._locate(row)
        with self.pool.pin(pid) as f:
            return self._page_get(f.data, j)

    def set(self, row: int, raw: Any) -> None:
        k, pid, j = self._locate(row)
        col = self.column
        self.sort_state = SORT_NONE
        self.minmax_valid = False
        with self.pool.pin(pid, write=True) as f:
            data = f.data
            old = self._page_get(data, j)
            if col.is_fixed:
                if raw is None:
                    data[16 + (j >> 3)] |= 1 << (j & 7)
                else:
                    data[16 + (j >> 3)] &= ~(1 << (j & 7)) & 0xFF
                    col.pack_into(data, 16 + self._nb + j * col.width, raw)
                done = True
            else:
                off, ln = _SLOT.unpack_from(data, 16 + j * 4)
                if raw is None:
                    _SLOT.pack_into(data, 16 + j * 4, _NULL_OFF, 0)
                    done = True
                else:
                    b = col.encode_var(raw)
                    if off != _NULL_OFF and len(b) <= ln:      # fits in the old cell
                        data[off:off + len(b)] = b
                        _SLOT.pack_into(data, 16 + j * 4, off, len(b))
                        done = True
                    else:
                        done = False
        self.null_count += (raw is None) - (old is None)
        if not done:
            self._rewrite_var_page(k, pid, j, raw)
        self._write_header()

    def _rewrite_var_page(self, k: int, pid: int, j: int, raw: Any) -> None:
        """Repack a variable-length page after a value grew; split if necessary."""
        col = self.column
        with self.pool.pin(pid) as f:
            vals = self._page_values(f.data)
        vals[j] = raw
        groups: List[List[Any]] = [[]]
        used = 16
        for v in vals:
            cost = 4 + (0 if v is None else len(col.encode_var(v)))
            if used + cost > self.ps and groups[-1]:
                groups.append([])
                used = 16
            groups[-1].append(v)
            used += cost
        first_row = self._dir[k][1]
        new_entries = []
        row = first_row
        for gi, g in enumerate(groups):
            if gi == 0:
                f = self.pool.fetch_page(pid)
                page_id = pid
            else:
                f = self.pool.new_page()
                page_id = f.page_id
                new_entries.append((page_id, row))
            f.data[:] = bytes(self.ps)
            self._init_page(f.data, row)
            for v in g:
                if not self._page_append(f.data, v):
                    raise RecordTooLargeError("value does not fit in a page")
            self.pool.unpin_page(page_id, True)
            row += len(g)
        if new_entries:
            self._dir[k + 1:k + 1] = new_entries
            self._firsts = [e[1] for e in self._dir]
            self._write_directory()

    # ---- scans -------------------------------------------------------------------------------------
    def iter_values(self) -> Iterator[Any]:
        for pid, _ in list(self._dir):
            with self.pool.pin(pid) as f:
                vals = self._page_values(f.data)
            yield from vals

    def read_all(self) -> List[Any]:
        return list(self.iter_values())

    # ---- bulk replace (used by sort) -------------------------------------------------------------
    def replace_all(self, raws: Sequence[Any]) -> None:
        old = [pid for pid, _ in self._dir] + self._ovf
        self._dir, self._firsts, self._ovf = [], [], []
        self.num_rows = self.null_count = 0
        self._min = self._max = None
        self.minmax_valid = self.column.is_fixed
        self.append_many(raws)
        for pid in old:
            self.pool.delete_page(pid)
        self._write_header()

    # ---- metadata ------------------------------------------------------------------------------------
    def stats(self) -> dict:
        valid = self.minmax_valid
        return {"column": self.column.name, "rows": self.num_rows, "nulls": self.null_count,
                "pages": len(self._dir), "min": self._min if valid else None,
                "max": self._max if valid else None,
                "sorted": {SORT_NONE: None, SORT_ASC: "asc", SORT_DESC: "desc"}[self.sort_state]}

    def page_count(self) -> int:
        return 1 + len(self._dir) + len(self._ovf)

    def drop(self) -> None:
        for pid, _ in self._dir:
            self.pool.delete_page(pid)
        for pid in self._ovf:
            self.pool.delete_page(pid)
        self.pool.delete_page(self.header_page_id)
        self._dir, self._firsts, self._ovf = [], [], []
