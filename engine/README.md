# coredb

A relational database engine written from scratch in pure Python (no dependencies).

* **Paged file**: 4KB pages with a persistent page directory; pages are loaded on demand, never the whole file.
* **Buffer pool**: fixed-size hash table, pin counts, dirty tracking, write-on-evict, configurable frame count.
* **Replacement policies**: LRU, CLOCK (second chance), LRU-K and 2Q, selectable in `Config` or switchable at runtime.
* **Storage models**: NSM (slotted pages, record id = `(page, slot)`) and DSM (one page chain per column with a column header page).


## Code documentation

Reference for every public name in `src/coredb/`, from the public API down to the storage layer.

---

### Package `coredb`

Top-level package; re-exports the public API (`from coredb import Database, Config, Column`).

| Global variable | Type | Description |
|---|---|---|
| `__version__` | str | Package version, `"0.1.0"`. |
| `__all__` | List[str] | `Database`, `Config`, `auto_buffer_frames`, `Column`, `DataType`, `Schema`, `BufferPool`, `DiskManager`, `available_policies`, `__version__`. Every exception of `coredb.errors` is re-exported too. |

#### Module `coredb.config`

Settings for page size, buffer pool, replacement policy and storage model.

| Global variable | Type | Description |
|---|---|---|
| `PAGE_SIZE` | int | `4096`: maximum and default page size in bytes. |

| Function | Parameters | Returns | Description |
|---|---|---|---|
| `auto_buffer_frames` | `memory_fraction: float = 0.02`, `min_frames: int = 64`, `max_frames: int = 262144`, `page_size: int = PAGE_SIZE` | int | Frames that fit in `memory_fraction` of the device's RAM, clamped to `[min_frames, max_frames]`; `1024` if the RAM size is unknown. |

**Class `Config`** (dataclass): engine settings passed to `Database`.

| Attribute | Type | Default | Description |
|---|---|---|---|
| `page_size` | int | `4096` | Page and frame size in bytes; a power of two in `[256, 4096]`. |
| `buffer_frames` | int | `256` | Number of buffer-pool frames (at least 2). |
| `replacement_policy` | str | `"lru"` | `"lru"`, `"clock"`, `"lru-k"` or `"2q"`. |
| `policy_options` | Dict[str, Any] | `{}` | Extra constructor arguments of the policy, e.g. `{"k": 2}`. |
| `default_layout` | str | `"nsm"` | Layout used by `create_table` when none is given: `"nsm"` or `"dsm"`. |
| `fsync` | bool | `False` | Force data to stable storage on `sync`/`close`. |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `validate` | none | Config | Checks the values and returns `self`; raises `ConfigError` if invalid. |

#### Module `coredb.errors`

Exception hierarchy; catch `DBError` for any engine error.

| Exception | Parent | Raised when |
|---|---|---|
| `DBError` | `Exception` | Base class of all engine errors. |
| `ConfigError` | `DBError` | A configuration value or policy name is invalid. |
| `StorageError` | `DBError` | Base class of low-level storage failures. |
| `CorruptDatabaseError` | `StorageError` | The file is not a valid coredb file or its metadata is inconsistent. |
| `PageNotFoundError` | `StorageError` | A page id is unallocated, freed or not in the buffer pool. |
| `RecordNotFoundError` | `StorageError` | A rid does not point to a live row. |
| `RecordTooLargeError` | `StorageError` | A record or value does not fit in one page. |
| `BufferPoolFullError` | `DBError` | Every frame is pinned, so nothing can be evicted. |
| `SchemaError` | `DBError` | A row or schema violates the table definition. |
| `CatalogError` | `DBError` | A table or layout name is unknown or duplicated. |

---

### Sub-package `coredb.api`

The public facade. Exports `Database`.

#### Module `coredb.api.database`

**Class `Database`**: one database file: its buffer pool, tables and statistics. Also a context manager (`with Database(...) as db`), which closes the file on exit.

`Database(path: str, config: Optional[Config] = None)` opens the file (creating it if missing), validates the config and loads the table catalog. `Database.open(path, config=None)` is an alias.

| Attribute | Type | Description |
|---|---|---|
| `config` | Config | The validated configuration. |
| `disk` | DiskManager | Page-level file access. |
| `pool` | BufferPool | The buffer pool all tables read and write through. |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `create_table` | `name: str`, `schema: Union[Schema, Sequence[Column]]`, `layout: Optional[str] = None` | StorageLayout | Creates a table; `layout` defaults to `config.default_layout`. `CatalogError` if the name exists. |
| `table` | `name: str` | StorageLayout | Opens an existing table. `CatalogError` if unknown. |
| `drop_table` | `name: str` | None | Frees all of the table's pages and removes it from the catalog. |
| `tables` | none | List[str] | Sorted table names. |
| `layout_of` | `name: str` | str | `"nsm"` or `"dsm"`. |
| `set_replacement_policy` | `name: str`, `**options` | None | Switches the policy at runtime; resident pages are kept. |
| `replacement_policies` | none (static) | List[str] | Available policy names. |
| `stats` | none | dict | `{"pool": PoolStats dict, "disk": IOStats dict, "policy": str, "frames": int, "pages": int}`. |
| `reset_stats` | none | None | Zeroes the pool and disk counters. |
| `checkpoint` | none | None | Persists table metadata, writes every dirty page and syncs the file. |
| `close` | none | None | Checkpoints and closes the file; safe to call twice. |

---

### Sub-package `coredb.storage`

Files, pages and table layouts. Exports `DiskManager`, `IOStats`, `INVALID_PAGE_ID`, `PageType`.

#### Module `coredb.storage.page`

| Global variable | Type | Description |
|---|---|---|
| `INVALID_PAGE_ID` | int | `0xFFFFFFFF`: the "no page" marker (page ids are unsigned 32-bit). |

**Class `PageType`** (`IntEnum`): page-kind codes `FREE=0`, `NSM_DATA=1`, `CATALOG=2`, `COLUMN_DATA_FIXED=3`, `COLUMN_DATA_VAR=4`. Only `NSM_DATA` is currently written into a page header.

#### Module `coredb.storage.disk_manager`

Page-granular file I/O. Internal constants: `_HEADER` (file-header struct).

| Global variable | Type | Description |
|---|---|---|
| `MAGIC` | bytes | `b"COREDB01"`: identifies a coredb file. |
| `FORMAT_VERSION` | int | `1`: file format version. |

**Class `IOStats`** (dataclass): I/O counters.

| Attribute | Type | Description |
|---|---|---|
| `reads`, `writes` | int | Data-page reads and writes. |
| `meta_reads`, `meta_writes` | int | File-header and directory-page reads and writes. |
| `allocations`, `frees` | int | Pages allocated and freed. |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `reset` | none | None | Zeroes all counters. |
| `snapshot` | none | Dict[str, int] | Copy of the counters. |

**Class `DiskManager`**: maps logical page ids to file locations through its `PageDirectory`.

`DiskManager(path: str, page_size: int = 4096, fsync: bool = False)` opens or creates the file and validates its header (`CorruptDatabaseError` for a foreign file, `ConfigError` for a page-size mismatch).

| Attribute | Type | Description |
|---|---|---|
| `path` | str | Database file path. |
| `page_size` | int | Bytes per page. |
| `fsync` | bool | Whether `sync` calls `os.fsync`. |
| `stats` | IOStats | I/O counters. |
| `directory` | PageDirectory | Logical-id to location map. |
| `root_page_id` | int | Page the `Database` uses as catalog root; `INVALID_PAGE_ID` until set. |
| `num_phys` | int | Physical pages in the file (header, directory and data pages). |
| `first_dir_phys` | int | Physical page of the first directory page. |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `set_root_page` | `page_id: int` | None | Stores the root page id in the file header. |
| `allocate_page` | none | int | New logical page id; freed ids are reused. |
| `free_page` | `page_id: int` | None | Releases the page. |
| `read_page` | `page_id: int`, `buf: bytearray` | None | Fills `buf` with the page (zeros if never written). `PageNotFoundError` if unallocated. |
| `write_page` | `page_id: int`, `data: bytes-like` | None | Writes one page of data. |
| `page_count` | property | int | Number of allocated pages. |
| `file_size` | none | int | File size in bytes. |
| `sync` | none | None | Flushes to the OS (and `os.fsync` if `fsync`). |
| `close` | none | None | Syncs and closes the file. |

Internal methods `_read_phys`, `_write_phys`, `_new_phys` and `_write_header` are called by `PageDirectory`.

#### Module `coredb.storage.page_directory`

Persistent map from logical page id to physical location; every change is written through. Internal constants: `_HDR`, `_ENTRY`, `_MAGIC = b"DIRP"` (directory-page layout).

| Global variable | Type | Description |
|---|---|---|
| `FLAG_UNUSED`, `FLAG_ALLOCATED`, `FLAG_FREE` | int | `0`, `1`, `2`: state of a directory entry. |

**Class `PageDirectory`**: `PageDirectory(dm: DiskManager)`. `len(directory)` is the number of ids ever created (allocated plus free).

| Attribute | Type | Description |
|---|---|---|
| `entries_per_page` | int | Entries per directory page (510 for 4KB pages). |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `load` | `first_dir_phys: int`, `expected_pages: int` | None | Reads the directory chain at startup; `CorruptDatabaseError` if inconsistent. |
| `locate` | `page_id: int` | int | Physical page number; `PageNotFoundError` if not allocated. |
| `is_allocated` | `page_id: int` | bool | Whether the id is in use. |
| `allocated_count` | none | int | Number of ids in use. |
| `allocated_ids` | none | List[int] | The ids in use. |
| `allocate` | none | int | Reuses a freed id, otherwise creates a new one (growing the directory if needed). |
| `free` | `page_id: int` | None | Marks the id reusable. |

#### Sub-package `coredb.storage.layouts`

Table storage models. Exports `StorageLayout`, `NSMTable`, `DSMTable`, `LAYOUTS`, `get_layout`.

| Global variable | Type | Description |
|---|---|---|
| `LAYOUTS` | Dict[str, Type[StorageLayout]] | `{"nsm": NSMTable, "dsm": DSMTable}`. |

| Function | Parameters | Returns | Description |
|---|---|---|---|
| `get_layout` | `name: str` | Type[StorageLayout] | Layout class by case-insensitive name; `CatalogError` if unknown. |

##### Module `coredb.storage.layouts.base`

**Class `StorageLayout`** (ABC): interface of every table. Rows are sequences in schema order with `None` for NULL; values are validated against the column types (`SchemaError`).

| Attribute | Type | Description |
|---|---|---|
| `layout_name` | str | `"nsm"` or `"dsm"`. |
| `schema` | Schema | The table's columns. |
| `header_page_id` | int | Page holding the table's metadata; stored in the catalog. |
| `pool` | BufferPool | Buffer pool used for page access (set by each implementation). |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `create` | `pool: BufferPool`, `schema: Schema` (classmethod) | StorageLayout | Creates an empty table. |
| `open` | `pool: BufferPool`, `schema: Schema`, `header_page_id: int` (classmethod) | StorageLayout | Reopens an existing table. |
| `insert` | `row: Sequence` | rid | Adds a row. |
| `insert_many` | `rows: Iterable[Sequence]` | List[rid] | Adds several rows. |
| `get` | `rid` | Tuple | Reads a row; `RecordNotFoundError` if absent. |
| `update` | `rid`, `row: Sequence` | rid | Replaces a row; returns its (possibly new) rid. |
| `delete` | `rid` | None | Removes a row. |
| `scan` | `columns: Optional[Sequence[str]] = None` | Iterator[Tuple[rid, Tuple]] | Yields `(rid, row)`; `columns` projects and reorders the output. |
| `count` | none | int | Live rows. |
| `page_count` | none | int | Pages used by the table. |
| `flush_meta` | none | None | Writes in-memory counters to the header page. |
| `drop` | none | None | Frees every page of the table. |

##### Module `coredb.storage.layouts.slotted_page`

| Global variable | Type | Description |
|---|---|---|
| `HEADER_SIZE` | int | `16`: page header bytes. |
| `SLOT_SIZE` | int | `4`: bytes per slot (offset, length). |

**Class `SlottedPage`**: a row page: header, slot array, free space, records growing from the end. A deleted slot stays reserved so other rids never change. `SlottedPage(data: bytearray)` wraps a page buffer.

| Attribute | Type | Description |
|---|---|---|
| `data` | bytearray | The page buffer. |
| `size` | int | Page size in bytes. |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `init` | `data: bytearray` (classmethod) | SlottedPage | Formats an empty page. |
| `num_slots` | property | int | Slots in the slot array, including deleted ones. |
| `next_page` | property (get/set) | int | Next page of the heap chain, or `INVALID_PAGE_ID`. |
| `max_record_size` | none | int | Largest record a page can hold. |
| `free_space` | none | int | Bytes available for a new record, counting space reclaimable by compaction. |
| `insert` | `rec: bytes` | Optional[int] | Slot number, or `None` if there is no room; `RecordTooLargeError` if it can never fit. |
| `get` | `slot: int` | Optional[bytes] | Record bytes, `None` if deleted or out of range. |
| `delete` | `slot: int` | bool | `False` if the slot was already empty. |
| `update` | `slot: int`, `rec: bytes` | bool | In-page update; `False` if the new record does not fit. |
| `compact` | none | None | Removes dead bytes; slot numbers are preserved. |
| `records` | none | Iterator[Tuple[int, bytes]] | Live `(slot, record)` pairs. |

##### Module `coredb.storage.layouts.nsm`

Internal constants: `_HDR`, `_MAGIC = b"NSMH"` (table header page).

| Global variable | Type | Description |
|---|---|---|
| `RID` | type alias | `Tuple[int, int]`: `(page_id, slot)`. |

**Class `NSMTable(StorageLayout)`**: row-oriented heap file of `SlottedPage`s linked by `next_page`; create it with `create`/`open`. Implements the interface above, with these specifics:
* `update` keeps the rid when the new row fits in its page; otherwise the row moves and the new rid is returned.
* Space freed by `delete` is reused by later inserts through an in-memory free-space map.

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `rebuild_free_space_map` | none | None | Walks the heap once to find pages with free space; call after reopening to reuse space freed earlier. |

##### Module `coredb.storage.layouts.column_store`

Internal constants: `_CH`, `_OVF`, `_DH`, `_SLOT`, `_ENTRY` and magic bytes `b"CLH1"`, `b"CLD1"` (column header, directory overflow and data page layouts).

| Global variable | Type | Description |
|---|---|---|
| `KIND_FIXED`, `KIND_VAR` | int | `1`, `2`: data-page kind (fixed-width or variable-length values). |
| `SORT_NONE`, `SORT_ASC`, `SORT_DESC` | int | `0`, `1`, `2`: sort state recorded in the column header. |

**Class `ColumnStore`**: all values of one column in their own pages, addressed by row position. It works on storage values (the output of `Column.to_storage`). `ColumnStore(pool, column: Column, header_page_id: int)`.

| Attribute | Type | Description |
|---|---|---|
| `pool` | BufferPool | Buffer pool used for all page access. |
| `column` | Column | The column definition. |
| `ps` | int | Page size in bytes. |
| `header_page_id` | int | Column header page (counts, min/max, sort state, data-page directory). |
| `num_rows` | int | Values stored, including NULLs. |
| `null_count` | int | Number of NULLs. |
| `sort_state` | int | `SORT_NONE`, `SORT_ASC` or `SORT_DESC`. |
| `minmax_valid` | bool | Whether the header's min/max are current (fixed-width columns only; cleared by `set`). |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `create` | `pool: BufferPool`, `column: Column` (classmethod) | ColumnStore | Creates an empty column. |
| `open` | `pool: BufferPool`, `column: Column`, `header_page_id: int` (classmethod) | ColumnStore | Reopens a column. |
| `append` | `raw: Any` | int | Appends a value (`None` for NULL); returns its row index. |
| `append_many` | `raws: Sequence[Any]` | int | Appends several values; returns the first row index. |
| `get` | `row: int` | Any | Value at a row; `RecordNotFoundError` if out of range. |
| `set` | `row: int`, `raw: Any` | None | Overwrites a value; fixed-width values in place, longer variable-length values repack (and may split) the page. |
| `iter_values` | none | Iterator[Any] | All values in row order, one page at a time. |
| `read_all` | none | List[Any] | All values. |
| `replace_all` | `raws: Sequence[Any]` | None | Rebuilds the column from new values and frees the old pages. |
| `stats` | none | dict | `{"column", "rows", "nulls", "pages", "min", "max", "sorted"}`; `sorted` is `None`, `"asc"` or `"desc"`. |
| `flush_meta` | none | None | Writes the header page. |
| `page_count` | none | int | Pages used, including header and directory pages. |
| `drop` | none | None | Frees every page of the column. |

##### Module `coredb.storage.layouts.dsm`

Internal constants: `_HDR`, `_MAGIC = b"DSMH"`, `_IDS_OFF` (table header page) and `_DELETED`, the hidden `__deleted__` BOOLEAN column.

**Class `DSMTable(StorageLayout)`**: column-oriented table with one `ColumnStore` per attribute; tuples are split on write and recombined on read. Create it with `create`/`open`. Implements the interface above, with these specifics:
* The rid is a row position (`int`). Positions change after `sort_by`.
* `insert_many` appends column by column; `update` returns the same rid.
* `delete` is logical: the first delete creates the hidden `__deleted__` column; `sort_by` removes deleted rows for good.
* `scan(columns)` reads only the pages of the requested columns.

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `update_column` | `rid: int`, `column: str`, `value: Any` | None | Changes one attribute, touching only that column's pages. |
| `scan_column` | `column: str` | Iterator[Any] | Values of one column, skipping deleted rows. |
| `column_stats` | `column: str` | dict | The column's `ColumnStore.stats()`. |
| `sort_by` | `column: str`, `descending: bool = False` | None | Reorders every column by the same permutation so tuples stay intact; NULLs last. |

---

### Sub-package `coredb.buffer`

Page caching. Exports `BufferPool`, `Frame`, `PoolStats`, `ReplacementPolicy`, `available_policies`, `create_policy`, `register_policy`.

#### Module `coredb.buffer.hash_table`

**Class `FixedSizeHashTable`**: page id to frame id, with separate chaining. The bucket array (smallest prime at or above twice `capacity`) is allocated once and never resizes. `FixedSizeHashTable(capacity: int)`.

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `get` | `key: int`, `default=None` | int | Value for `key`, or `default`. |
| `pop` | `key: int`, `*default` | int | Removes and returns the value; `KeyError` if absent and no default. |
| `items` | none | Iterator[Tuple[int, int]] | `(key, value)` pairs. |
| `values` | none | Iterator[int] | Values. |
| `[]`, `in`, `del`, `len()`, `iter()` | standard | | Dict-like access, membership, deletion, size and iteration over keys. |

#### Module `coredb.buffer.buffer_pool`

**Class `Frame`**: one buffer-pool slot holding one page. `Frame(frame_id: int, size: int)`.

| Attribute | Type | Description |
|---|---|---|
| `frame_id` | int | Position in the pool. |
| `data` | bytearray | The page bytes. |
| `page_id` | int | Page held, `INVALID_PAGE_ID` when empty. |
| `pin_count` | int | Active users; a pinned frame is never evicted. |
| `dirty` | bool | Modified since it was read or last written. |

**Class `PoolStats`** (dataclass): buffer-pool counters.

| Attribute | Type | Description |
|---|---|---|
| `hits`, `misses` | int | Requests served from memory or from disk. |
| `evictions` | int | Pages evicted. |
| `dirty_evictions` | int | Evictions that wrote the page back. |
| `flush_writes` | int | Writes caused by `flush_page`/`flush_all`. |
| `new_pages` | int | Pages created with `new_page`. |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `requests` | property | int | `hits + misses`. |
| `hit_ratio` | property | float | `hits / requests` (0.0 if none). |
| `reset` | none | None | Zeroes the counters. |
| `snapshot` | none | Dict[str, float] | Counters plus `requests` and `hit_ratio`. |

**Class `BufferPool`**: fixed number of frames, pin counting, dirty tracking, write-on-evict. `BufferPool(disk: DiskManager, num_frames: int, policy: str = "lru", **policy_options)`.

| Attribute | Type | Description |
|---|---|---|
| `disk` | DiskManager | Underlying file. |
| `page_size` | int | Bytes per frame. |
| `num_frames` | int | Number of frames. |
| `stats` | PoolStats | Counters. |
| `policy` | ReplacementPolicy | Active policy instance. |
| `policy_name` | str | Name it was created with. |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `fetch_page` | `page_id: int` | Frame | Pins and returns the page's frame, reading it on a miss and evicting a victim if needed (written back if dirty). `PageNotFoundError`; `BufferPoolFullError` if every frame is pinned. |
| `new_page` | none | Frame | Allocates a zeroed page; returned pinned and dirty. |
| `unpin_page` | `page_id: int`, `is_dirty: bool = False` | None | Releases one pin; `is_dirty=True` marks the page modified. |
| `flush_page` | `page_id: int` | bool | Writes the page if dirty; `False` if not resident. |
| `flush_all` | none | None | Writes every dirty page. |
| `checkpoint` | none | None | `flush_all` plus a file sync. |
| `delete_page` | `page_id: int` | bool | Drops the page without write-back and frees it; `False` if pinned. |
| `close` | none | None | Same as `checkpoint` (does not close the file). |
| `set_policy` | `name: str`, `**options` | None | Swaps the policy at runtime; resident pages are re-registered. |
| `pin` | `page_id: int`, `write: bool = False` (context manager) | Frame | Fetches the page, unpins on exit (marked dirty if `write`). |
| `new` | none (context manager) | Frame | Creates a page, unpins it dirty on exit. |
| `is_resident` | `page_id: int` | bool | Whether the page is in memory. |
| `resident_pages` | none | List[int] | Ids of the pages in memory. |
| `pinned_count` | none | int | Frames currently pinned. |
| `pin_count` | `page_id: int` | int | Pins on one page (0 if not resident). |

#### Sub-package `coredb.buffer.policies`

Page replacement algorithms. Exports the interface, the four policies and the registry functions.

##### Module `coredb.buffer.policies.base`

**Class `ReplacementPolicy`** (ABC): `ReplacementPolicy(capacity: int)`. The pool tracks resident pages by id through these methods; `name: str` is the registry name and `capacity: int` the frame count.

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `record_access` | `page_id: int` | None | Called on every fetch. |
| `set_evictable` | `page_id: int`, `evictable: bool` | None | `False` while the page is pinned. |
| `evict` | none | Optional[int] | Chooses and forgets a victim among evictable pages; `None` if there is none. |
| `remove` | `page_id: int` | None | Forgets a deleted page. |
| `size` | none | int | Number of evictable pages. |

##### Modules `lru`, `clock`, `lru_k`, `two_q`

Each implements `ReplacementPolicy`; only the constructor and algorithm differ.

| Class (`name`) | Constructor parameters | Algorithm |
|---|---|---|
| `LRUPolicy` (`"lru"`) | `capacity: int` | Evicts the least recently used page; recency is refreshed on access and on unpin. |
| `ClockPolicy` (`"clock"`) | `capacity: int`, `initial_ref: bool = True` | Second chance: a hand sweeps a circle of slots, clearing reference bits; `initial_ref` sets the bit of newly loaded pages. |
| `LRUKPolicy` (`"lru-k"`) | `capacity: int`, `k: int = 2`, `history_limit: Optional[int] = None` | Evicts the page with the largest backward K-distance (fewer than `k` accesses counts as infinite). Keeps access history of up to `history_limit` evicted pages (default `2 * capacity`). Attributes: `k`, `history_limit`. |
| `TwoQPolicy` (`"2q"`) | `capacity: int`, `kin_ratio: float = 0.25`, `kout_ratio: float = 0.5` | A1in FIFO for first-time pages, A1out ghost list of recently evicted ids, Am LRU for re-referenced pages. Attributes: `kin`, `kout` (sizes of A1in and A1out). |

##### Module `coredb.buffer.policies.registry`

Internal: `_REGISTRY` maps names (and aliases `"lruk"`, `"lru_k"`, `"twoq"`) to policy classes.

| Function | Parameters | Returns | Description |
|---|---|---|---|
| `available_policies` | none | List[str] | `["lru", "clock", "lru-k", "2q"]`. |
| `register_policy` | `name: str`, `cls: Type[ReplacementPolicy]` | None | Adds a custom policy. |
| `create_policy` | `name: str`, `capacity: int`, `**options` | ReplacementPolicy | Builds a policy by name; `ConfigError` if unknown. |

---

### Sub-package `coredb.catalog`

Column types, schemas, row encoding and the persistent table catalog. Exports `Column`, `DataType`, `Schema`, `encode_row`, `decode_row`.

#### Module `coredb.catalog.types`

| Global variable | Type | Description |
|---|---|---|
| `TYPE_CODES` | Dict[DataType, int] | Numeric code (1 to 8) of each type, stored in column header pages. |

**Class `DataType`** (`str`, `Enum`): column types.

| Member | Python type | Storage |
|---|---|---|
| `INTEGER` | int | Signed 64-bit. |
| `FLOAT` | float | IEEE double. |
| `BOOLEAN` | bool | 1 byte. |
| `DECIMAL` | decimal.Decimal | Exact: scaled signed 64-bit integer; precision up to 18. |
| `TIMESTAMP` | datetime.datetime | UTC microseconds since the epoch (naive values are taken as UTC; an int is accepted as microseconds). |
| `VARCHAR`, `TEXT` | str | Variable-length UTF-8; `VARCHAR` is limited by `max_length`. |
| `BYTES` | bytes | Variable-length. |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `parse` | `name: str or DataType` (classmethod) | DataType | Case-insensitive lookup; aliases `INT`, `BIGINT`, `BOOL`, `DOUBLE`, `REAL`, `NUMERIC`, `TIMESTAMPTZ`, `BLOB`, `STRING`. `SchemaError` if unknown. |

**Class `Column`** (frozen dataclass): `Column(name, type, nullable=True, max_length=None, precision=None, scale=None)`.

| Attribute | Type | Description |
|---|---|---|
| `name` | str | Column name. |
| `type` | DataType | Accepts a `DataType` or its name as a string. |
| `nullable` | bool | Whether NULL is allowed (default `True`). |
| `max_length` | Optional[int] | `VARCHAR` limit (default 255). |
| `precision`, `scale` | Optional[int] | `DECIMAL(p, s)` (defaults 18 and 2). |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `is_fixed` | property | bool | Whether the type has a fixed width. |
| `width` | property | int | Bytes per value (0 for variable-length types). |
| `identity` | property | bool | Whether the Python value equals the stored value (no conversion needed on read). |
| `to_storage` | `v: Any` | Any | Validates a Python value and converts it to storage form; `SchemaError` if invalid. |
| `from_storage` | `raw: Any` | Any | Converts a stored value back to its Python value. |
| `pack` | `raw: Any` | bytes | Packs a fixed-width value. |
| `pack_into` | `buf`, `off: int`, `raw: Any` | None | Packs a fixed-width value into a buffer. |
| `unpack_from` | `buf`, `off: int` | Any | Reads a fixed-width value. |
| `encode_var` | `raw: Any` | bytes | Bytes of a variable-length value. |
| `decode_var` | `b: bytes` | Any | Variable-length value from bytes. |
| `to_dict` | none | dict | Catalog form. |
| `from_dict` | `d: dict` (classmethod) | Column | Rebuilds from catalog form. |

#### Module `coredb.catalog.schema`

**Class `Schema`**: ordered columns of a table; raises `SchemaError` for no columns or duplicate names. `Schema(columns: Iterable[Column])`. Supports `len()`, iteration over columns, `==` and `repr`.

| Attribute | Type | Description |
|---|---|---|
| `columns` | Tuple[Column, ...] | The columns, in order. |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `names` | property | List[str] | Column names. |
| `index` | `name: str` | int | Position of a column; `SchemaError` if unknown. |
| `column` | `name: str` | Column | Column by name. |
| `check_row` | `row: Sequence` | None | Checks the length and NOT NULL rules; `SchemaError` on violation. |
| `to_dict` | none | list | Catalog form. |
| `from_dict` | `items: list` (classmethod) | Schema | Rebuilds from catalog form. |

#### Module `coredb.catalog.codec`

Row encoding for NSM: a null bitmap, then each non-null value (fixed-width packed, variable-length with a 2-byte length).

| Function | Parameters | Returns | Description |
|---|---|---|---|
| `encode_row` | `schema: Schema`, `row: Sequence` | bytes | Encodes a row; `SchemaError` if it violates the schema. |
| `decode_row` | `schema: Schema`, `buf`, `off: int = 0` | Tuple | Decodes a row starting at `off`. |

#### Module `coredb.catalog.store`

Internal constants: `_HDR`, `_MAGIC = b"CATP"` (catalog page layout).

**Class `CatalogStore`**: the table catalog, kept as JSON in a chain of pages. `CatalogStore(pool: BufferPool, root_page_id: int)`.

| Attribute | Type | Description |
|---|---|---|
| `pool` | BufferPool | Buffer pool used for page access. |
| `root` | int | First page of the chain. |

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `init_root` | `pool: BufferPool` (classmethod) | int | Creates an empty catalog root page and returns its id. |
| `load` | none | dict | `{"tables": {name: {"layout": str, "header_page": int, "schema": list}}}`. |
| `save` | `obj: dict` | None | Rewrites the catalog, growing or shrinking the page chain as needed. |
