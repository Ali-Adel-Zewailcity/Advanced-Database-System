# coredb

A relational database engine written from scratch in pure Python.

* **Paged file**: 4KB pages with a persistent page directory; pages are loaded on demand, never the whole file.
* **Buffer pool**: fixed-size hash table, pin counts, dirty tracking, write-on-evict, configurable frame count.
* **Replacement policies**: LRU, CLOCK (second chance), LRU-K and 2Q, selectable in `Config` or switchable at runtime.
* **Storage models**: NSM (slotted pages, record id = `(page, slot)`) and DSM (one page chain per column with a column header page).