"""Disk-backed, buffer-pool-managed B+ Tree index for coredb.

Page layouts (4KB default):
1. Meta Page (PageType.BTREE_META):
   [ type:1B | flags:1B | unique:1B | pad:1B | root_page:4B | height:4B |
     entry_count:8B | page_count:8B ]

2. Leaf Page (PageType.BTREE_LEAF):
   [ type:1B | flags:1B | n_keys:2B | free_end:2B | prev_leaf:4B | next_leaf:4B | pad:2B ]
   Followed by slot directory: [ offset:2B | length:2B ] * n_keys
   Records at end of page: [ key_len:2B | encoded_key | page_id:4B | slot_id:4B ]

3. Internal Page (PageType.BTREE_INTERNAL):
   [ type:1B | flags:1B | n_keys:2B | free_end:2B | left_child:4B | pad:6B ]
   Followed by slot directory: [ offset:2B | length:2B ] * n_keys
   Records at end of page: [ key_len:2B | encoded_key | right_child:4B ]

Non-unique keys are supported seamlessly by ordering leaf entries by
(encoded_key, page_id, slot_id), while internal separator keys route by encoded_key.
"""
from __future__ import annotations

import bisect
import struct
from typing import Any, Iterator, List, Optional, Tuple

from ..errors import CorruptDatabaseError, DuplicateKeyError, IndexError
from ..storage.page import INVALID_PAGE_ID, PageType
from .base import Index, RID, decode_key, encode_key

_META_HDR = struct.Struct("<BBBBIIQQ")        # 28 bytes
_LEAF_HDR = struct.Struct("<BBHHIIH")         # 16 bytes
_INT_HDR = struct.Struct("<BBHHI6x")          # 16 bytes
_SLOT = struct.Struct("<HH")                  # 4 bytes
_RID_PACK = struct.Struct("<II")              # 8 bytes (page_id, slot_id)
_CHILD_PACK = struct.Struct("<I")             # 4 bytes (right_child_page_id)
_KEY_LEN = struct.Struct("<H")                # 2 bytes

_HEADER_SIZE = 16
_SLOT_SIZE = 4


# ---------------------------------------------------------------------------
# Low-level 4KB B+ Tree Page Wrappers
# ---------------------------------------------------------------------------

class _BTreeLeafPage:
    """In-place view over a 4KB B+ Tree leaf page bytearray."""

    __slots__ = ("data", "size")

    def __init__(self, data: bytearray):
        self.data = data
        self.size = len(data)

    @classmethod
    def init(cls, data: bytearray, prev_leaf: int = INVALID_PAGE_ID, next_leaf: int = INVALID_PAGE_ID) -> "_BTreeLeafPage":
        _LEAF_HDR.pack_into(data, 0, PageType.BTREE_LEAF, 0, 0, len(data), prev_leaf, next_leaf, 0)
        return cls(data)

    def _hdr(self):
        return _LEAF_HDR.unpack_from(self.data, 0)

    @property
    def num_keys(self) -> int:
        return self._hdr()[2]

    @property
    def prev_leaf(self) -> int:
        return self._hdr()[4]

    @prev_leaf.setter
    def prev_leaf(self, pid: int) -> None:
        t, fl, nk, fe, _, nxt, _ = self._hdr()
        _LEAF_HDR.pack_into(self.data, 0, t, fl, nk, fe, pid, nxt, 0)

    @property
    def next_leaf(self) -> int:
        return self._hdr()[5]

    @next_leaf.setter
    def next_leaf(self, pid: int) -> None:
        t, fl, nk, fe, prv, _, _ = self._hdr()
        _LEAF_HDR.pack_into(self.data, 0, t, fl, nk, fe, prv, pid, 0)

    def _read_entry(self, idx: int) -> Tuple[bytes, RID]:
        off, _ = _SLOT.unpack_from(self.data, _HEADER_SIZE + idx * _SLOT_SIZE)
        klen = _KEY_LEN.unpack_from(self.data, off)[0]
        kstart = off + 2
        kend = kstart + klen
        key_bytes = bytes(self.data[kstart:kend])
        rid = _RID_PACK.unpack_from(self.data, kend)
        return key_bytes, (rid[0], rid[1])

    def entries(self) -> List[Tuple[bytes, RID]]:
        return [self._read_entry(i) for i in range(self.num_keys)]

    @staticmethod
    def _pack_entry(key_bytes: bytes, rid: RID) -> bytes:
        return _KEY_LEN.pack(len(key_bytes)) + key_bytes + _RID_PACK.pack(rid[0], rid[1])

    def write_all(self, entries: List[Tuple[bytes, RID]], prev_leaf: int, next_leaf: int) -> bool:
        """Rewrite all sorted entries into this page. Returns False if they exceed page capacity."""
        packed = [self._pack_entry(k, r) for k, r in entries]
        total_bytes = _HEADER_SIZE + len(packed) * _SLOT_SIZE + sum(len(p) for p in packed)
        if total_bytes > self.size:
            return False
        fe = self.size
        for i, raw in enumerate(packed):
            fe -= len(raw)
            self.data[fe:fe + len(raw)] = raw
            _SLOT.pack_into(self.data, _HEADER_SIZE + i * _SLOT_SIZE, fe, len(raw))
        _LEAF_HDR.pack_into(self.data, 0, PageType.BTREE_LEAF, 0, len(packed), fe, prev_leaf, next_leaf, 0)
        return True


class _BTreeInternalPage:
    """In-place view over a 4KB B+ Tree internal router page bytearray."""

    __slots__ = ("data", "size")

    def __init__(self, data: bytearray):
        self.data = data
        self.size = len(data)

    @classmethod
    def init(cls, data: bytearray, left_child: int = INVALID_PAGE_ID) -> "_BTreeInternalPage":
        _INT_HDR.pack_into(data, 0, PageType.BTREE_INTERNAL, 0, 0, len(data), left_child)
        return cls(data)

    def _hdr(self):
        return _INT_HDR.unpack_from(self.data, 0)

    @property
    def num_keys(self) -> int:
        return self._hdr()[2]

    @property
    def left_child(self) -> int:
        return self._hdr()[4]

    @left_child.setter
    def left_child(self, pid: int) -> None:
        t, fl, nk, fe, _ = self._hdr()
        _INT_HDR.pack_into(self.data, 0, t, fl, nk, fe, pid)

    def _read_item(self, idx: int) -> Tuple[bytes, int]:
        off, _ = _SLOT.unpack_from(self.data, _HEADER_SIZE + idx * _SLOT_SIZE)
        klen = _KEY_LEN.unpack_from(self.data, off)[0]
        kstart = off + 2
        kend = kstart + klen
        key_bytes = bytes(self.data[kstart:kend])
        right_child = _CHILD_PACK.unpack_from(self.data, kend)[0]
        return key_bytes, right_child

    def items(self) -> Tuple[int, List[bytes], List[int]]:
        """Return (left_child, separator_keys, right_children)."""
        keys: List[bytes] = []
        children: List[int] = []
        for i in range(self.num_keys):
            k, c = self._read_item(i)
            keys.append(k)
            children.append(c)
        return self.left_child, keys, children

    @staticmethod
    def _pack_item(key_bytes: bytes, right_child: int) -> bytes:
        return _KEY_LEN.pack(len(key_bytes)) + key_bytes + _CHILD_PACK.pack(right_child)

    def write_all(self, left_child: int, keys: List[bytes], right_children: List[int]) -> bool:
        """Rewrite router entries into this page. Returns False if capacity is exceeded."""
        packed = [self._pack_item(k, c) for k, c in zip(keys, right_children)]
        total_bytes = _HEADER_SIZE + len(packed) * _SLOT_SIZE + sum(len(p) for p in packed)
        if total_bytes > self.size:
            return False
        fe = self.size
        for i, raw in enumerate(packed):
            fe -= len(raw)
            self.data[fe:fe + len(raw)] = raw
            _SLOT.pack_into(self.data, _HEADER_SIZE + i * _SLOT_SIZE, fe, len(raw))
        _INT_HDR.pack_into(self.data, 0, PageType.BTREE_INTERNAL, 0, len(packed), fe, left_child)
        return True


# ---------------------------------------------------------------------------
# B+ Tree Index Implementation
# ---------------------------------------------------------------------------

class BPlusTreeIndex(Index):
    """Balanced, block-oriented B+ Tree stored in 4KB BufferPool pages."""

    index_type = "btree"

    def __init__(
        self,
        pool,
        name: str,
        column_name: str,
        meta_page_id: int,
        root_page_id: int,
        height: int = 1,
        entry_count: int = 0,
        page_count: int = 2,
        unique: bool = False,
        max_fill_ratio: float = 1.0,
    ):
        super().__init__(name=name, column_name=column_name, unique=unique)
        self.pool = pool
        self.meta_page_id = meta_page_id
        self.root_page_id = root_page_id
        self.height = height
        self.entry_count = entry_count
        self._page_count = page_count
        self._max_fill_ratio = max_fill_ratio

    # ---- Construction & Persistence ---------------------------------------

    @classmethod
    def create(cls, pool, name: str, column_name: str, unique: bool = False) -> "BPlusTreeIndex":
        """Allocate a new empty B+ Tree (1 meta page + 1 empty root leaf page)."""
        with pool.new() as meta_f:
            meta_pid = meta_f.page_id
        with pool.new() as root_f:
            _BTreeLeafPage.init(root_f.data)
            root_pid = root_f.page_id
        tree = cls(
            pool=pool,
            name=name,
            column_name=column_name,
            meta_page_id=meta_pid,
            root_page_id=root_pid,
            height=1,
            entry_count=0,
            page_count=2,
            unique=unique,
        )
        tree.flush_meta()
        return tree

    @classmethod
    def open(cls, pool, name: str, column_name: str, meta_page_id: int) -> "BPlusTreeIndex":
        """Re-open an existing B+ Tree from its meta page ID."""
        with pool.pin(meta_page_id) as f:
            ptype, _, uniq, _, root_pid, height, entries, pages = _META_HDR.unpack_from(f.data, 0)
        if ptype != PageType.BTREE_META:
            raise CorruptDatabaseError(f"page {meta_page_id} is not a B+ Tree meta page")
        return cls(
            pool=pool,
            name=name,
            column_name=column_name,
            meta_page_id=meta_page_id,
            root_page_id=root_pid,
            height=height,
            entry_count=entries,
            page_count=pages,
            unique=bool(uniq),
        )

    def flush_meta(self) -> None:
        with self.pool.pin(self.meta_page_id, write=True) as f:
            _META_HDR.pack_into(
                f.data,
                0,
                PageType.BTREE_META,
                0,
                1 if self.unique else 0,
                0,
                self.root_page_id,
                self.height,
                self.entry_count,
                self._page_count,
            )

    def flush(self) -> None:
        self.flush_meta()
        self.pool.flush_all()

    def close(self) -> None:
        self.flush()

    def size_bytes(self) -> int:
        return self._page_count * self.pool.page_size

    # ---- Traversal Helpers ------------------------------------------------

    def _is_leaf(self, page_data: bytearray) -> bool:
        return page_data[0] == PageType.BTREE_LEAF

    def _find_leaf_path(self, key_bytes: bytes) -> List[int]:
        """Descend from root to the target leaf, returning the list of page IDs visited."""
        path: List[int] = []
        curr = self.root_page_id
        while True:
            path.append(curr)
            with self.pool.pin(curr) as f:
                if self._is_leaf(f.data):
                    return path
                left_child, sep_keys, right_children = _BTreeInternalPage(f.data).items()
            # First separator strictly greater than key_bytes chooses the child to its left
            idx = bisect.bisect_right(sep_keys, key_bytes)
            curr = left_child if idx == 0 else right_children[idx - 1]

    def _leftmost_leaf(self) -> int:
        curr = self.root_page_id
        while True:
            with self.pool.pin(curr) as f:
                if self._is_leaf(f.data):
                    return curr
                curr = _BTreeInternalPage(f.data).left_child

    # ---- Public Index Operations ------------------------------------------

    def search(self, key: Any) -> List[RID]:
        self.stats.point_lookups += 1
        kbytes = encode_key(key)
        results: List[RID] = []
        curr = self._leftmost_leaf()
        while curr != INVALID_PAGE_ID:
            with self.pool.pin(curr) as f:
                leaf = _BTreeLeafPage(f.data)
                entries = leaf.entries()
                nxt = leaf.next_leaf
            for ek, rid in entries:
                if ek == kbytes:
                    results.append(rid)
                elif ek > kbytes:
                    return results
            curr = nxt
        return results

    def range_scan(
        self,
        low: Optional[Any] = None,
        high: Optional[Any] = None,
        include_low: bool = True,
        include_high: bool = True,
    ) -> Iterator[Tuple[Any, RID]]:
        self.stats.range_scans += 1
        low_b = encode_key(low) if low is not None else None
        high_b = encode_key(high) if high is not None else None

        curr = self._leftmost_leaf() if low_b is None else self._find_leaf_path(low_b)[-1]
        while curr != INVALID_PAGE_ID:
            with self.pool.pin(curr) as f:
                leaf = _BTreeLeafPage(f.data)
                entries = leaf.entries()
                nxt = leaf.next_leaf
            for ek, rid in entries:
                if low_b is not None:
                    if ek < low_b or (ek == low_b and not include_low):
                        continue
                if high_b is not None:
                    if ek > high_b or (ek == high_b and not include_high):
                        return
                yield decode_key(ek), rid
            curr = nxt

    def insert(self, key: Any, rid: RID) -> None:
        kbytes = encode_key(key)
        if self.unique and self.search(key):
            raise DuplicateKeyError(f"duplicate key {key!r} in unique index '{self.name}'")

        path = self._find_leaf_path(kbytes)
        leaf_pid = path.pop()

        with self.pool.pin(leaf_pid, write=True) as f:
            leaf = _BTreeLeafPage(f.data)
            entries = leaf.entries()
            prv, nxt = leaf.prev_leaf, leaf.next_leaf
            item = (kbytes, rid)
            pos = bisect.bisect_left(entries, item)
            if pos < len(entries) and entries[pos] == item:
                return  # exact (key, rid) already present
            entries.insert(pos, item)

            if leaf.write_all(entries, prv, nxt):
                self.entry_count += 1
                self.stats.inserts += 1
                self.stats.logical_bytes_written += len(kbytes) + 8
                self.stats.bytes_written += self.pool.page_size
                return

        # Leaf overflow -> split leaf and propagate separator upward
        self._split_leaf_and_propagate(leaf_pid, entries, prv, nxt, path)
        self.entry_count += 1
        self.stats.inserts += 1
        self.stats.logical_bytes_written += len(kbytes) + 8

    def _split_leaf_and_propagate(
        self,
        leaf_pid: int,
        entries: List[Tuple[bytes, RID]],
        prev_leaf: int,
        next_leaf: int,
        ancestors: List[int],
    ) -> None:
        mid = len(entries) // 2
        left_entries = entries[:mid]
        right_entries = entries[mid:]
        separator_key = right_entries[0][0]

        with self.pool.new() as new_f:
            new_leaf_pid = new_f.page_id
            right_leaf = _BTreeLeafPage.init(new_f.data, prev_leaf=leaf_pid, next_leaf=next_leaf)
            if not right_leaf.write_all(right_entries, leaf_pid, next_leaf):
                raise IndexError("single B+ Tree leaf entry exceeds page capacity")
        self._page_count += 1

        with self.pool.pin(leaf_pid, write=True) as old_f:
            left_leaf = _BTreeLeafPage(old_f.data)
            if not left_leaf.write_all(left_entries, prev_leaf, new_leaf_pid):
                raise IndexError("single B+ Tree leaf entry exceeds page capacity")

        if next_leaf != INVALID_PAGE_ID:
            with self.pool.pin(next_leaf, write=True) as nxt_f:
                _BTreeLeafPage(nxt_f.data).prev_leaf = new_leaf_pid

        self.stats.bytes_written += 2 * self.pool.page_size
        self._insert_into_parent(leaf_pid, separator_key, new_leaf_pid, ancestors)

    def _insert_into_parent(
        self,
        left_child_pid: int,
        separator_key: bytes,
        right_child_pid: int,
        ancestors: List[int],
    ) -> None:
        # Root split: allocate a brand-new root internal page
        if not ancestors:
            with self.pool.new() as root_f:
                new_root_pid = root_f.page_id
                root_page = _BTreeInternalPage.init(root_f.data, left_child=left_child_pid)
                root_page.write_all(left_child_pid, [separator_key], [right_child_pid])
            self.root_page_id = new_root_pid
            self.height += 1
            self._page_count += 1
            self.stats.bytes_written += self.pool.page_size
            self.flush_meta()
            return

        parent_pid = ancestors.pop()
        with self.pool.pin(parent_pid, write=True) as pf:
            parent = _BTreeInternalPage(pf.data)
            lchild, keys, children = parent.items()
            pos = bisect.bisect_right(keys, separator_key)
            keys.insert(pos, separator_key)
            children.insert(pos, right_child_pid)
            if parent.write_all(lchild, keys, children):
                self.stats.bytes_written += self.pool.page_size
                return

        # Internal node overflow -> split internal node and push middle separator up
        mid = len(keys) // 2
        promoted_key = keys[mid]
        left_keys = keys[:mid]
        left_children = children[:mid]
        right_left_child = children[mid]
        right_keys = keys[mid + 1:]
        right_children = children[mid + 1:]

        with self.pool.new() as new_int_f:
            new_right_pid = new_int_f.page_id
            right_page = _BTreeInternalPage.init(new_int_f.data, left_child=right_left_child)
            if not right_page.write_all(right_left_child, right_keys, right_children):
                raise IndexError("internal B+ Tree entry exceeds page capacity")
        self._page_count += 1

        with self.pool.pin(parent_pid, write=True) as old_int_f:
            left_page = _BTreeInternalPage(old_int_f.data)
            if not left_page.write_all(lchild, left_keys, left_children):
                raise IndexError("internal B+ Tree entry exceeds page capacity")

        self.stats.bytes_written += 2 * self.pool.page_size
        self._insert_into_parent(parent_pid, promoted_key, new_right_pid, ancestors)

    def delete(self, key: Any, rid: Optional[RID] = None) -> bool:
        kbytes = encode_key(key)
        curr = self._find_leaf_path(kbytes)[-1]
        removed_any = False

        while curr != INVALID_PAGE_ID:
            with self.pool.pin(curr, write=True) as f:
                leaf = _BTreeLeafPage(f.data)
                entries = leaf.entries()
                prv, nxt = leaf.prev_leaf, leaf.next_leaf
                new_entries: List[Tuple[bytes, RID]] = []
                changed = False
                for ek, erid in entries:
                    if ek == kbytes and (rid is None or erid == rid):
                        changed = True
                        removed_any = True
                        self.entry_count -= 1
                        self.stats.deletes += 1
                    else:
                        new_entries.append((ek, erid))
                if changed:
                    leaf.write_all(new_entries, prv, nxt)
                    self.stats.bytes_written += self.pool.page_size
            if entries and entries[-1][0] > kbytes:
                break
            curr = nxt

        return removed_any
