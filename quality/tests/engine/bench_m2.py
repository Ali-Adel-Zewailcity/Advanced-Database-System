"""Milestone 2 Performance Benchmarks: B+ Tree vs. LSM Tree & Indexed Queries.

Run with:
    python -m benchmarks.bench_m2
"""
from __future__ import annotations

import random
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, List

from coredb.buffer.pool import BufferPool
from coredb.catalog.schema import Column, Schema
from coredb.index.btree import BPlusTreeIndex
from coredb.index.lsm import LSMTreeIndex
from coredb.index.manager import IndexManager, WorkloadProfile
from coredb.query.engine import QueryEngine
from coredb.storage.disk_manager import DiskManager
from coredb.storage.layouts.nsm import NSMTable


def _products_schema() -> Schema:
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


def bench_insert_lookup_and_size(n_records: int = 3000) -> List[Dict[str, float]]:
    tmp = Path(tempfile.mkdtemp(prefix="coredb_bench_m2_"))
    try:
        rng = random.Random(42)
        keys = list(range(1, n_records + 1))
        rng.shuffle(keys)
        lookup_keys = [rng.choice(keys) for _ in range(1000)]

        results = []

        # --- B+ Tree ---
        disk_b = DiskManager(str(tmp / "btree.db"), page_size=4096)
        pool_b = BufferPool(disk_b, num_frames=64, policy="lru")
        btree = BPlusTreeIndex.create(pool_b, "bench_btree", "id", unique=True)

        t0 = time.perf_counter()
        for i, k in enumerate(keys):
            btree.insert(k, (i // 32 + 1, i % 32))
        btree.flush()
        bt_insert_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        for k in lookup_keys:
            _ = btree.search(k)
        bt_lookup_ms = (time.perf_counter() - t0) * 1000.0

        results.append({
            "index": "B+ Tree",
            "records": n_records,
            "build_ms": round(bt_insert_ms, 2),
            "insert_us_per_op": round((bt_insert_ms * 1000.0) / n_records, 2),
            "lookup_us_per_op": round((bt_lookup_ms * 1000.0) / len(lookup_keys), 2),
            "size_kb": round(btree.size_bytes() / 1024.0, 2),
            "write_amp": round(btree.stats.write_amplification, 2),
        })
        pool_b.close()
        disk_b.close()

        # --- LSM Tree ---
        lsm = LSMTreeIndex(
            storage_dir=tmp / "lsm_bench",
            name="bench_lsm",
            column_name="id",
            unique=False,
            memtable_max_bytes=16 * 1024,
            compaction_threshold=4,
        )
        t0 = time.perf_counter()
        for i, k in enumerate(keys):
            lsm.insert(k, (i // 32 + 1, i % 32))
        lsm.flush()
        lsm_insert_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        for k in lookup_keys:
            _ = lsm.search(k)
        lsm_lookup_ms = (time.perf_counter() - t0) * 1000.0

        results.append({
            "index": "LSM Tree",
            "records": n_records,
            "build_ms": round(lsm_insert_ms, 2),
            "insert_us_per_op": round((lsm_insert_ms * 1000.0) / n_records, 2),
            "lookup_us_per_op": round((lsm_lookup_ms * 1000.0) / len(lookup_keys), 2),
            "size_kb": round(lsm.size_bytes() / 1024.0, 2),
            "write_amp": round(lsm.stats.write_amplification, 2),
        })
        lsm.close()
        return results
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def bench_indexed_vs_full_scan(n_products: int = 2500) -> List[Dict[str, float]]:
    tmp = Path(tempfile.mkdtemp(prefix="coredb_bench_scan_"))
    try:
        disk = DiskManager(str(tmp / "retail.db"), page_size=4096)
        pool = BufferPool(disk, num_frames=64, policy="lru")
        idx_mgr = IndexManager(pool, lsm_root_dir=tmp / "lsm")
        products = NSMTable.create(pool, _products_schema())
        engine = QueryEngine({"products": products}, idx_mgr)

        idx_mgr.create_index(
            products, "id", unique=True, workload=WorkloadProfile(read_ratio=0.9, range_query_ratio=0.1)
        )
        idx_mgr.create_index(
            products, "unit_price", unique=False, workload=WorkloadProfile(read_ratio=0.9, range_query_ratio=0.8)
        )

        rng = random.Random(7)
        for pid in range(1, n_products + 1):
            price = round(rng.uniform(5.0, 500.0), 2)
            engine.insert_row(
                "products",
                (pid, pid % 10 + 1, f"SKU-{pid:05d}", f"Item {pid}", price, "", True),
            )

        # 200 equality lookups
        eq_ids = [rng.randint(1, n_products) for _ in range(200)]
        t0 = time.perf_counter()
        for qid in eq_ids:
            engine.execute(f"SELECT id, unit_price FROM products WHERE id = {qid}")
        idx_eq_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        for qid in eq_ids:
            engine.execute(f"SELECT id, unit_price FROM products WHERE id = {qid}", force_full_scan=True)
        scan_eq_ms = (time.perf_counter() - t0) * 1000.0

        # 100 range queries
        t0 = time.perf_counter()
        for _ in range(100):
            engine.execute("SELECT id, unit_price FROM products WHERE unit_price BETWEEN 100.0 AND 125.0")
        idx_rng_ms = (time.perf_counter() - t0) * 1000.0

        t0 = time.perf_counter()
        for _ in range(100):
            engine.execute(
                "SELECT id, unit_price FROM products WHERE unit_price BETWEEN 100.0 AND 125.0",
                force_full_scan=True,
            )
        scan_rng_ms = (time.perf_counter() - t0) * 1000.0

        idx_mgr.close_all()
        pool.close()
        disk.close()

        return [
            {
                "query_type": "Equality (200x id = ?)",
                "indexed_ms": round(idx_eq_ms, 2),
                "full_scan_ms": round(scan_eq_ms, 2),
                "speedup": f"{scan_eq_ms / max(0.01, idx_eq_ms):.1f}x",
            },
            {
                "query_type": "Range (100x price BETWEEN)",
                "indexed_ms": round(idx_rng_ms, 2),
                "full_scan_ms": round(scan_rng_ms, 2),
                "speedup": f"{scan_rng_ms / max(0.01, idx_rng_ms):.1f}x",
            },
        ]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def bench_mixed_workloads(total_ops: int = 2000) -> List[Dict[str, float]]:
    """Evaluate B+ Tree vs. LSM Tree under Read-Heavy (85% R) vs. Write-Heavy (85% W)."""
    scenarios = [
        ("Read-Heavy (85% R / 15% W)", 0.85),
        ("Write-Heavy (15% R / 85% W)", 0.15),
    ]
    rows = []
    for label, read_ratio in scenarios:
        for kind in ("btree", "lsm"):
            tmp = Path(tempfile.mkdtemp(prefix="coredb_bench_mix_"))
            try:
                disk = DiskManager(str(tmp / "mix.db"), page_size=4096)
                pool = BufferPool(disk, num_frames=32, policy="lru")
                idx = (
                    BPlusTreeIndex.create(pool, "mix_bt", "id")
                    if kind == "btree"
                    else LSMTreeIndex(tmp / "lsm", "mix_lsm", "id", memtable_max_bytes=16 * 1024)
                )

                # Seed 500 initial keys
                for k in range(1, 501):
                    idx.insert(k, (1, k))

                rng = random.Random(123)
                next_key = 501
                t0 = time.perf_counter()
                for _ in range(total_ops):
                    if rng.random() < read_ratio:
                        _ = idx.search(rng.randint(1, next_key - 1))
                    else:
                        idx.insert(next_key, (next_key // 32 + 1, next_key % 32))
                        next_key += 1
                idx.flush()
                elapsed_ms = (time.perf_counter() - t0) * 1000.0

                rows.append({
                    "workload": label,
                    "index": kind.upper(),
                    "ops": total_ops,
                    "total_ms": round(elapsed_ms, 2),
                    "throughput_ops_sec": round(total_ops / (elapsed_ms / 1000.0), 1),
                    "write_amp": round(idx.stats.write_amplification, 2),
                })
                idx.close()
                pool.close()
                disk.close()
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    return rows


def _print_table(title: str, records: List[Dict]) -> None:
    print(f"\n=== {title} ===")
    if not records:
        return
    headers = list(records[0].keys())
    widths = {h: max(len(h), max(len(str(r[h])) for r in records)) for h in headers}
    header_line = " | ".join(h.ljust(widths[h]) for h in headers)
    print(header_line)
    print("-+-".join("-" * widths[h] for h in headers))
    for r in records:
        print(" | ".join(str(r[h]).ljust(widths[h]) for h in headers))


if __name__ == "__main__":
    _print_table("1. B+ Tree vs. LSM Tree Build, Latency, Size & Write Amplification", bench_insert_lookup_and_size())
    _print_table("2. Indexed Query vs. Full-Table Scan (Retail Products)", bench_indexed_vs_full_scan())
    _print_table("3. Read-Heavy vs. Write-Heavy Mixed Workloads", bench_mixed_workloads())
