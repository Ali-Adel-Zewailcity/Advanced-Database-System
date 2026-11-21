"""Milestone 2 Correctness Test Suite for coredb.

Run with:
    pytest tests/test_m2_indexing.py -v
"""
from __future__ import annotations

import os
import pytest

from coredb.buffer.pool import BufferPool
from coredb.catalog.schema import Column, Schema
from coredb.errors import CryptoError, DuplicateKeyError
from coredb.index.btree import BPlusTreeIndex
from coredb.index.lsm import LSMTreeIndex
from coredb.index.manager import IndexManager, WorkloadProfile
from coredb.query.engine import QueryEngine
from coredb.storage.crypto import AuthenticatedStreamHook
from coredb.storage.disk_manager import DiskManager
from coredb.storage.layouts.nsm import NSMTable
from coredb.storage.object_store import ObjectStore


def _make_products_schema() -> Schema:
    return Schema(
        name="products",
        columns=[
            Column("id", "INT"),
            Column("category_id", "INT"),
            Column("sku", "VARCHAR"),
            Column("name", "VARCHAR"),
            Column("unit_price", "FLOAT"),
            Column("image_url", "VARCHAR"),
            Column("is_active", "BOOL"),
        ],
    )


def _make_orders_schema() -> Schema:
    return Schema(
        name="sales_orders",
        columns=[
            Column("id", "INT"),
            Column("customer_id", "INT"),
            Column("status", "VARCHAR"),
            Column("total_amount", "FLOAT"),
        ],
    )


@pytest.fixture
def env(tmp_path):
    db_file = tmp_path / "retail_m2.db"
    disk = DiskManager(str(db_file), page_size=4096)
    pool = BufferPool(disk, num_frames=32, policy="lru")
    idx_mgr = IndexManager(pool, lsm_root_dir=tmp_path / "lsm_indexes")
    obj_store = ObjectStore(
        root_dir=tmp_path / "objects",
        encryption_hook=AuthenticatedStreamHook(b"super-secret-retail-key-2026"),
    )

    products = NSMTable.create(pool, _make_products_schema())
    orders = NSMTable.create(pool, _make_orders_schema())
    engine = QueryEngine({"products": products, "sales_orders": orders}, idx_mgr)

    yield {
        "tmp_path": tmp_path,
        "disk": disk,
        "pool": pool,
        "idx_mgr": idx_mgr,
        "obj_store": obj_store,
        "products": products,
        "orders": orders,
        "engine": engine,
    }

    idx_mgr.close_all()
    pool.close()
    disk.close()


# 1. B+ Tree Splits, Equality Search, and Range Scans

def test_btree_splits_equality_and_range_scan(env):
    pool = env["pool"]
    btree = BPlusTreeIndex.create(pool, name="idx_test_price", column_name="unit_price", unique=False)

    # Insert 600 keys (with duplicate prices) to force multiple leaf + internal + root splits
    expected_by_key = {}
    for i in range(600):
        price = round((i % 75) * 1.5 + 5.0, 2)
        rid = (i // 20 + 1, i % 20)
        btree.insert(price, rid)
        expected_by_key.setdefault(price, []).append(rid)

    assert btree.height >= 2
    assert btree.entry_count == 600

    # Verify equality lookup for every distinct price
    for price, expected_rids in expected_by_key.items():
        assert sorted(btree.search(price)) == sorted(expected_rids)

    # Verify range scan [20.0, 45.0]
    scanned = list(btree.range_scan(low=20.0, high=45.0, include_low=True, include_high=True))
    expected_range = sorted(
        (p, r)
        for p, rids in expected_by_key.items()
        if 20.0 <= p <= 45.0
        for r in rids
    )
    assert sorted(scanned) == expected_range


def test_btree_unique_constraint(env):
    btree = BPlusTreeIndex.create(env["pool"], name="idx_uniq_id", column_name="id", unique=True)
    btree.insert(101, (1, 0))
    with pytest.raises(DuplicateKeyError):
        btree.insert(101, (1, 1))


# 2. LSM Tree PUT, DELETE Tombstones, Flush, Compaction, and WAL Recovery

def test_lsm_put_delete_compaction_and_wal_recovery(tmp_path):
    lsm_dir = tmp_path / "lsm_orders"
    lsm = LSMTreeIndex(
        storage_dir=lsm_dir,
        name="idx_orders_id",
        column_name="id",
        memtable_max_bytes=1024,    # small threshold to force multiple SSTable flushes
        compaction_threshold=3,
    )

    # Insert 250 entries -> triggers flushes + compaction
    for oid in range(1, 251):
        lsm.insert(oid, (oid // 10 + 1, oid % 10))

    assert lsm.stats.compactions >= 1
    assert lsm.search(42) == [(5, 2)]

    # Delete even IDs -> tombstones must mask older SSTable entries
    for oid in range(2, 251, 2):
        assert lsm.delete(oid) is True

    assert lsm.search(42) == []
    assert lsm.search(43) == [(5, 3)]

    # Insert fresh keys into MemTable WITHOUT flushing, then simulate restart via WAL replay
    lsm.insert(9999, (99, 1))
    lsm._wal.close()  # simulate abrupt stop without flush_memtable()

    reopened = LSMTreeIndex(
        storage_dir=lsm_dir,
        name="idx_orders_id",
        column_name="id",
        memtable_max_bytes=1024,
        compaction_threshold=3,
    )
    assert reopened.search(9999) == [(99, 1)]
    assert reopened.search(42) == []
    assert reopened.search(43) == [(5, 3)]
    reopened.close()


# 3. SQL-Like Indexed Queries vs. Full-Table Scan & DML Maintenance

def test_indexed_queries_match_full_scan_and_stay_consistent_on_dml(env):
    engine: QueryEngine = env["engine"]
    idx_mgr: IndexManager = env["idx_mgr"]
    products: NSMTable = env["products"]

    # Create read-optimized B+ Tree indexes on products.id (unique) and products.unit_price
    idx_mgr.create_index(
        products, "id", unique=True, workload=WorkloadProfile(read_ratio=0.9, range_query_ratio=0.1)
    )
    idx_mgr.create_index(
        products, "unit_price", unique=False, workload=WorkloadProfile(read_ratio=0.85, range_query_ratio=0.7)
    )

    # Insert 120 retail products
    for pid in range(1, 121):
        price = round(10.0 + (pid % 30) * 3.25, 2)
        engine.insert_row(
            "products",
            (pid, pid % 5 + 1, f"SKU-{pid:04d}", f"Product {pid}", price, "", True),
        )

    # 1. Equality lookup via index vs full-table scan
    idx_eq = engine.execute("SELECT id, sku, unit_price FROM products WHERE id = 77")
    scan_eq = engine.execute("SELECT id, sku, unit_price FROM products WHERE id = 77", force_full_scan=True)
    assert idx_eq.plan == "index_equality_scan"
    assert idx_eq.index_type == "btree"
    assert scan_eq.plan == "full_table_scan"
    assert idx_eq.rows == scan_eq.rows == [(77, "SKU-0077", round(10.0 + (77 % 30) * 3.25, 2))]

    # 2. Range query via index vs full-table scan
    idx_rng = engine.execute(
        "SELECT id, unit_price FROM products WHERE unit_price BETWEEN 25.0 AND 55.0 ORDER BY id ASC"
    )
    scan_rng = engine.execute(
        "SELECT id, unit_price FROM products WHERE unit_price BETWEEN 25.0 AND 55.0 ORDER BY id ASC",
        force_full_scan=True,
    )
    assert idx_rng.plan == "index_range_scan"
    assert idx_rng.rows == scan_rng.rows
    assert len(idx_rng.rows) > 0

    # 3. Update with a much longer name so NSM relocates the record to a new RID
    long_name = "Premium Limited Edition Ultra-Long Retail Product Name " * 15
    engine.execute(f"UPDATE products SET name = '{long_name}', unit_price = 999.99 WHERE id = 77")

    after_upd = engine.execute("SELECT id, unit_price FROM products WHERE unit_price = 999.99")
    assert after_upd.plan == "index_equality_scan"
    assert after_upd.rows == [(77, 999.99)]
    # Old price bucket must no longer return id=77
    old_bucket = engine.execute("SELECT id FROM products WHERE id = 77")
    assert old_bucket.rows == [(77,)]

    # 4. Delete and verify index consistency
    engine.execute("DELETE FROM products WHERE id = 77")
    assert engine.execute("SELECT id FROM products WHERE id = 77").rows == []
    assert engine.execute("SELECT id FROM products WHERE unit_price = 999.99").rows == []


# 4. Workload-Based Auto-Selection (B+ Tree vs. LSM Tree)

def test_workload_based_index_auto_selection(env):
    idx_mgr: IndexManager = env["idx_mgr"]

    # Read/range-heavy product catalog workload -> auto-selects B+ Tree
    prod_idx = idx_mgr.create_index(
        env["products"],
        "unit_price",
        index_type="auto",
        workload=WorkloadProfile(read_ratio=0.85, range_query_ratio=0.60),
    )
    assert prod_idx.index_type == "btree"

    # Write-heavy order ingestion workload -> auto-selects LSM Tree
    order_idx = idx_mgr.create_index(
        env["orders"],
        "customer_id",
        index_type="auto",
        workload=WorkloadProfile(read_ratio=0.15, range_query_ratio=0.05),
    )
    assert order_idx.index_type == "lsm"


# 5. Object Storage & Encryption Hooks (Product Images)

def test_product_image_object_storage_and_encryption_hooks(env):
    obj_store: ObjectStore = env["obj_store"]
    engine: QueryEngine = env["engine"]
    idx_mgr: IndexManager = env["idx_mgr"]

    idx_mgr.create_index(env["products"], "id", unique=True, index_type="btree")

    # Simulate a 24 KB product image (larger than a 4KB database page)
    fake_png = b"\x89PNG\r\n\x1a\n" + os.urandom(24 * 1024)
    meta = obj_store.put(fake_png, content_type="image/png")
    assert meta.encrypted is True
    assert meta.uri.startswith("obj://")

    # Verify raw disk bytes are encrypted (not plaintext)
    raw_disk_bytes = obj_store.get_raw_stored_bytes(meta.uri)
    assert raw_disk_bytes != fake_png
    assert b"\x89PNG" not in raw_disk_bytes

    # Store the object URI inside the relational `products` record
    engine.insert_row(
        "products",
        (501, 1, "SKU-CAM-501", "Mirrorless Camera", 1299.00, meta.uri, True),
    )

    res = engine.execute("SELECT name, image_url FROM products WHERE id = 501")
    assert res.rows == [("Mirrorless Camera", meta.uri)]

    # Retrieve & decrypt the image via the URI stored in the product record
    restored_png = obj_store.get(res.rows[0][1])
    assert restored_png == fake_png

    # Wrong decryption key must fail authentication
    wrong_key_store = ObjectStore(
        root_dir=obj_store.root_dir,
        encryption_hook=AuthenticatedStreamHook(b"wrong-encryption-key-0000"),
    )
    with pytest.raises(CryptoError):
        wrong_key_store.get(meta.uri)
