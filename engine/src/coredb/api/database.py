"""High-level facade: open a database file, create tables with a chosen storage
model, and control the buffer pool / replacement policy."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Union

from ..buffer.buffer_pool import BufferPool
from ..buffer.policies import available_policies
from ..catalog.schema import Schema
from ..catalog.store import CatalogStore
from ..catalog.types import Column
from ..config import Config
from ..errors import CatalogError
from ..storage.disk_manager import DiskManager
from ..storage.layouts import StorageLayout, get_layout
from ..storage.page import INVALID_PAGE_ID


class Database:
    def __init__(self, path: str, config: Optional[Config] = None):
        self.config = (config or Config()).validate()
        c = self.config
        self.disk = DiskManager(path, c.page_size, fsync=c.fsync)
        self.pool = BufferPool(self.disk, c.buffer_frames, c.replacement_policy, **c.policy_options)
        if self.disk.root_page_id == INVALID_PAGE_ID:
            self.disk.set_root_page(CatalogStore.init_root(self.pool))
        self._catalog = CatalogStore(self.pool, self.disk.root_page_id)
        self._meta: dict = self._catalog.load()
        self._open: Dict[str, StorageLayout] = {}
        self._closed = False

    open = classmethod(lambda cls, path, config=None: cls(path, config))

    # ---- DDL ---------------------------------------------------------------------
    def create_table(self, name: str, schema: Union[Schema, Sequence[Column]],
                     layout: Optional[str] = None) -> StorageLayout:
        if name in self._meta["tables"]:
            raise CatalogError(f"table {name!r} already exists")
        schema = schema if isinstance(schema, Schema) else Schema(schema)
        layout = (layout or self.config.default_layout).lower()
        table = get_layout(layout).create(self.pool, schema)
        self._meta["tables"][name] = {"layout": layout, "header_page": table.header_page_id,
                                      "schema": schema.to_dict()}
        self._catalog.save(self._meta)
        self._open[name] = table
        return table

    def table(self, name: str) -> StorageLayout:
        if name in self._open:
            return self._open[name]
        try:
            info = self._meta["tables"][name]
        except KeyError:
            raise CatalogError(f"no such table {name!r}") from None
        t = get_layout(info["layout"]).open(self.pool, Schema.from_dict(info["schema"]), info["header_page"])
        self._open[name] = t
        return t

    def drop_table(self, name: str) -> None:
        t = self.table(name)
        t.drop()
        del self._meta["tables"][name]
        self._open.pop(name, None)
        self._catalog.save(self._meta)

    def tables(self) -> List[str]:
        return sorted(self._meta["tables"])

    def layout_of(self, name: str) -> str:
        return self._meta["tables"][name]["layout"]

    # ---- buffer pool control -------------------------------------------------------
    def set_replacement_policy(self, name: str, **options) -> None:
        self.pool.set_policy(name, **options)

    @staticmethod
    def replacement_policies() -> List[str]:
        return available_policies()

    def stats(self) -> dict:
        return {"pool": self.pool.stats.snapshot(), "disk": self.disk.stats.snapshot(),
                "policy": self.pool.policy_name, "frames": self.pool.num_frames,
                "pages": self.disk.page_count}

    def reset_stats(self) -> None:
        self.pool.stats.reset()
        self.disk.stats.reset()

    # ---- lifecycle ------------------------------------------------------------------
    def checkpoint(self) -> None:
        for t in self._open.values():
            t.flush_meta()
        self.pool.checkpoint()

    def close(self) -> None:
        if self._closed:
            return
        self.checkpoint()
        self.disk.close()
        self._closed = True

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
