"""Benchmark: NSM (row store) vs DSM (column store).

Same schema, same rows (fixed seed), same pool size and replacement policy.
The table is much larger than the pool, so reads are mostly cold.

Writes analysis/results/layouts.json and analysis/results/figures/layouts.png.
Temporary database files live in quality/.tmp/ and are deleted when the script ends.
"""
from __future__ import annotations

import os
import random
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import save, save_figure, timed, workspace  # noqa: E402

from coredb import Column, Config, Database  # noqa: E402

N_ROWS = 40_000
FRAMES = 128
SEED = 7
REPEATS = 3
STATUS = ["PENDING", "PAID", "SHIPPED", "RETURNED", "CANCELLED"]
SCHEMA = [Column("id", "INTEGER", nullable=False), Column("order_id", "INTEGER"),
          Column("product_id", "INTEGER"), Column("quantity", "INTEGER"),
          Column("unit_price", "DECIMAL", precision=12, scale=2), Column("discount", "FLOAT"),
          Column("status", "VARCHAR", max_length=12), Column("note", "VARCHAR", max_length=80)]


def gen_rows(n, seed=SEED):
    r = random.Random(seed)
    return [(i, r.randrange(n // 4), r.randrange(5000), r.randint(1, 10),
             Decimal(r.randint(100, 99999)) / 100, r.random() * 0.3, r.choice(STATUS),
             None if r.random() < 0.3 else "note-" + "x" * r.randint(5, 40)) for i in range(n)]


def measure(db, fn, repeats=REPEATS):
    db.checkpoint()
    db.reset_stats()
    secs, res = timed(fn, repeats)
    st = db.stats()
    return {"seconds": round(secs, 4), "page_misses": st["pool"]["misses"] // repeats,
            "hit_ratio": round(st["pool"]["hit_ratio"], 4)}, res


def run_layout(layout, rows, tmp):
    path = os.path.join(tmp, f"{layout}.db")
    with Database(path, Config(buffer_frames=FRAMES, replacement_policy="lru")) as db:
        t = db.create_table("order_items", SCHEMA, layout=layout)
        out = {}
        t0 = time.perf_counter()
        rids = t.insert_many(rows)
        db.checkpoint()
        out["bulk_load"] = {"seconds": round(time.perf_counter() - t0, 4)}
        out["storage"] = {"pages": t.page_count(), "bytes": t.page_count() * 4096,
                          "file_bytes": db.disk.file_size()}
        rnd = random.Random(SEED)
        sample = [rids[rnd.randrange(len(rids))] for _ in range(2000)]

        out["full_scan_all_columns"], _ = measure(db, lambda: sum(1 for _ in t.scan()))
        out["sum_1_column"], _ = measure(db, (lambda: sum(t.scan_column("quantity"))) if layout == "dsm"
                                         else (lambda: sum(r[0] for _, r in t.scan(["quantity"]))))
        out["sum_2_columns"], _ = measure(db, lambda: sum(q * p for _, (q, p) in t.scan(["quantity", "unit_price"])))
        out["point_lookup_2000"], _ = measure(db, lambda: [t.get(r) for r in sample])

        upd = sample[:1000]
        if layout == "dsm":
            out["update_1_attr_1000"], _ = measure(db, lambda: [t.update_column(r, "status", "SHIPPED") for r in upd], 1)
        else:
            def nsm_update():
                for r in upd:
                    row = list(t.get(r)); row[6] = "SHIPPED"; t.update(r, tuple(row))
            out["update_1_attr_1000"], _ = measure(db, nsm_update, 1)

        extra = gen_rows(3000, seed=99)
        out["oltp_insert_3000"], _ = measure(db, lambda: [t.insert(r) for r in extra], 1)
        if layout == "dsm":
            out["sort_by_price"], _ = measure(db, lambda: t.sort_by("unit_price"), 1)
        return out


def plot(res):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    L = res["layouts"]
    ops = [o for o in L["nsm"] if o != "storage" and o in L["dsm"]]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    for ax, key, title in ((axes[0], "page_misses", "pool page misses (log scale)"),
                           (axes[1], "seconds", "seconds (log scale)")):
        ops2 = [o for o in ops if key in L["nsm"][o]]
        for i, lay in enumerate(("nsm", "dsm")):
            ax.bar([x + i * 0.4 for x in range(len(ops2))], [max(L[lay][o][key], 1e-4) for o in ops2],
                   0.4, label=lay.upper())
        ax.set_xticks([x + 0.2 for x in range(len(ops2))])
        ax.set_xticklabels([o.replace("_", "\n") for o in ops2], fontsize=7)
        ax.set_yscale("log"); ax.set_title(title); ax.legend()
    fig.tight_layout()
    return [save_figure(fig, "layouts")]


def main():
    rows = gen_rows(N_ROWS)
    res = {"config": {"rows": N_ROWS, "frames": FRAMES, "policy": "lru", "seed": SEED,
                      "repeats": REPEATS, "page_size": 4096}, "layouts": {}}
    with workspace("layouts") as tmp:
        for layout in ("nsm", "dsm"):
            res["layouts"][layout] = run_layout(layout, rows, tmp)
            print(layout)
            for k, v in res["layouts"][layout].items():
                print("  ", k, v)
        print("saved", save("layouts", res))
        for p in plot(res):                     # inside the workspace: matplotlib's cache stays in the project
            print("saved", p)


if __name__ == "__main__":
    main()
