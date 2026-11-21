"""Benchmark: page replacement policies under different access patterns.

Every run replays the SAME page-access trace (fixed seed) through the real
BufferPool + DiskManager, so hit ratios and page I/O are measured, not simulated.

Writes analysis/results/policies.json and analysis/results/figures/policies_*.png.
Temporary database files live in quality/.tmp/ and are deleted when the script ends.
"""
from __future__ import annotations

import itertools
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import save, save_figure, workspace  # noqa: E402

from coredb.buffer import BufferPool  # noqa: E402
from coredb.storage import DiskManager  # noqa: E402

N_PAGES = 2000
N_ACCESSES = 60_000
SEED = 42
POLICIES = {  # label -> (policy name, options)
    "lru": ("lru", {}), "clock": ("clock", {}), "lru-k": ("lru-k", {"k": 2}),
    "2q": ("2q", {}), "2q-kout100": ("2q", {"kout_ratio": 1.0}),
}


def zipf_trace(rnd, n_pages, n, s=1.0):
    w = [1 / (i + 1) ** s for i in range(n_pages)]
    order = list(range(n_pages)); rnd.shuffle(order)          # hot pages scattered in the file
    return [order[i] for i in rnd.choices(range(n_pages), weights=w, k=n)]


def workloads(seed=SEED):
    rnd = random.Random(seed)
    N, n = N_PAGES, N_ACCESSES
    out = {}
    out["sequential_loop"] = ([i % N for i in range(n)], 0.0)
    out["uniform_random"] = ([rnd.randrange(N) for _ in range(n)], 0.0)
    out["zipfian"] = (zipf_trace(rnd, N, n), 0.0)
    hot = list(range(0, N // 10))
    out["hotspot_90_10"] = ([rnd.choice(hot) if rnd.random() < 0.9 else rnd.randrange(N) for _ in range(n)], 0.0)
    # hot working set (150 pages) + periodic one-shot sequential scans of cold pages
    trace, cold = [], itertools.cycle(range(200, N))
    while len(trace) < n:
        trace += [rnd.randrange(150) for _ in range(1500)]
        trace += [next(cold) for _ in range(600)]
    out["scan_pollution"] = (trace[:n], 0.0)
    out["zipfian_30pct_writes"] = (zipf_trace(rnd, N, n), 0.30)
    return out


def make_db(path, n_pages):
    disk = DiskManager(path)
    ids = [disk.allocate_page() for _ in range(n_pages)]
    for i in ids:
        disk.write_page(i, bytes([i % 251]) * disk.page_size)
    return disk, ids


def replay(disk, ids, trace, write_ratio, frames, spec, seed=SEED):
    policy, opts = spec
    pool = BufferPool(disk, frames, policy, **opts)
    disk.stats.reset()
    rnd = random.Random(seed + 1)
    t0 = time.perf_counter()
    for pid in trace:
        pool.fetch_page(ids[pid])
        pool.unpin_page(ids[pid], write_ratio > 0 and rnd.random() < write_ratio)
    secs = time.perf_counter() - t0
    s = pool.stats
    return {"hit_ratio": round(s.hit_ratio, 4), "misses": s.misses, "evictions": s.evictions,
            "disk_reads": disk.stats.reads, "disk_writes": disk.stats.writes,
            "seconds": round(secs, 3)}


def plot(results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    wl, cfg = results["by_workload"], results["config"]
    pols = list(next(iter(wl.values())))
    names = list(wl)

    fig, ax = plt.subplots(figsize=(11, 4.5))
    w = 0.8 / len(pols)
    for i, p in enumerate(pols):
        ax.bar([x + i * w for x in range(len(names))], [wl[n][p]["hit_ratio"] for n in names], w, label=p)
    ax.set_xticks([x + 0.4 - w / 2 for x in range(len(names))])
    ax.set_xticklabels([n.replace("_", "\n") for n in names], fontsize=8)
    ax.set_ylabel("buffer hit ratio")
    ax.set_title(f"Replacement policies ({cfg['frames']} frames, {cfg['pages']} pages)")
    ax.legend(); fig.tight_layout()
    paths = [save_figure(fig, "policies_hit_ratio")]

    bs = results["by_buffer_size"]
    sizes = sorted(bs, key=int)
    fig, ax = plt.subplots(figsize=(6.5, 4))
    for p in pols:
        ax.plot([int(s) for s in sizes], [bs[s][p] for s in sizes], marker="o", label=p)
    ax.set_xscale("log"); ax.set_xlabel("buffer frames"); ax.set_ylabel("hit ratio")
    ax.set_title("Zipfian workload vs buffer size")
    ax.legend(); fig.tight_layout()
    paths.append(save_figure(fig, "policies_buffer_size"))
    return paths


def main(frames=200):
    results = {"config": {"pages": N_PAGES, "accesses": N_ACCESSES, "frames": frames,
                          "seed": SEED, "lru_k": 2}, "by_workload": {}, "by_buffer_size": {}}
    with workspace("policies") as tmp:
        disk, ids = make_db(os.path.join(tmp, "pol.db"), N_PAGES)
        try:
            wl = workloads()
            for name, (trace, wr) in wl.items():
                results["by_workload"][name] = {}
                for pol, spec in POLICIES.items():
                    r = replay(disk, ids, trace, wr, frames, spec)
                    results["by_workload"][name][pol] = r
                    print(f"{name:22s} {pol:10s} hit={r['hit_ratio']:.3f} reads={r['disk_reads']:6d} "
                          f"writes={r['disk_writes']:5d} {r['seconds']:.2f}s")
            trace, wr = wl["zipfian"]
            for fr in (50, 100, 200, 400, 800, 1600):
                results["by_buffer_size"][fr] = {p: replay(disk, ids, trace, wr, fr, spec)["hit_ratio"]
                                                 for p, spec in POLICIES.items()}
        finally:
            disk.close()
        print("saved", save("policies", results))
        for p in plot(results):                 # inside the workspace: matplotlib's cache stays in the project
            print("saved", p)


if __name__ == "__main__":
    main()
