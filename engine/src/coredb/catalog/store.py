"""Minimal persistent catalog: a JSON document stored in a chain of pages
rooted at the database's root page.  """
from __future__ import annotations

import json
import struct
from typing import List

from ..errors import CorruptDatabaseError
from ..storage.page import INVALID_PAGE_ID

_HDR = struct.Struct("<4sIII")   # magic, next_page, chunk_len, total_len
_MAGIC = b"CATP"


class CatalogStore:
    def __init__(self, pool, root_page_id: int):
        self.pool, self.root = pool, root_page_id
        self._chunk = pool.page_size - _HDR.size

    def _chain(self) -> List[int]:
        out, pid = [], self.root
        while pid != INVALID_PAGE_ID:
            out.append(pid)
            with self.pool.pin(pid) as f:
                magic, nxt, _, _ = _HDR.unpack_from(f.data, 0)
            if magic != _MAGIC:
                raise CorruptDatabaseError("bad catalog page")
            pid = nxt
        return out

    def load(self) -> dict:
        parts = []
        for pid in self._chain():
            with self.pool.pin(pid) as f:
                _, _, ln, _ = _HDR.unpack_from(f.data, 0)
                parts.append(bytes(f.data[_HDR.size:_HDR.size + ln]))
        blob = b"".join(parts)
        return json.loads(blob) if blob else {"tables": {}}

    def save(self, obj: dict) -> None:
        blob = json.dumps(obj, separators=(",", ":")).encode()
        chunks = [blob[i:i + self._chunk] for i in range(0, len(blob), self._chunk)] or [b""]
        chain = self._chain()
        pids = chain[:len(chunks)]
        while len(pids) < len(chunks):
            with self.pool.new() as f:
                pids.append(f.page_id)
        for extra in chain[len(chunks):]:
            self.pool.delete_page(extra)
        for i, (pid, ch) in enumerate(zip(pids, chunks)):
            nxt = pids[i + 1] if i + 1 < len(pids) else INVALID_PAGE_ID
            with self.pool.pin(pid, write=True) as f:
                f.data[:] = bytes(len(f.data))
                _HDR.pack_into(f.data, 0, _MAGIC, nxt, len(ch), len(blob))
                f.data[_HDR.size:_HDR.size + len(ch)] = ch

    @classmethod
    def init_root(cls, pool) -> int:
        with pool.new() as f:
            _HDR.pack_into(f.data, 0, _MAGIC, INVALID_PAGE_ID, 0, 0)
            return f.page_id
