# Benchmarks (`quality/benchmarks/`)

This directory contains reproducible performance benchmarks for **CoreDB** (`engine/src/coredb`) and its real-world example applications (`examples/`).

Unlike unit tests (which verify functional correctness in `quality/tests/`), benchmarks do analysis to system under controlled, deterministic workloads.

---

## Architecture & Directory Layout

Every benchmark topic lives in its own subdirectory under `quality/benchmarks/` and outputs standardized data and figures to `quality/analysis/results/`:

```text
quality/
├── benchmarks/
│   ├── README.md                  This guide
│   ├── harness.py                 Shared utilities: timing, workspaces, JSON & figure output
│   └── <topic>/
│       └── run.py                 Self-contained benchmark entry point for a feature or comparison
├── analysis/
│   └── results/                   AUTO-GENERATED: Raw JSON metrics (*.json) & plots (figures/*.png)
└── reports/                       HAND-WRITTEN: Markdown analysis & architectural findings
```

* **`quality/benchmarks/<topic>/run.py`:** Each subsystem, data structure, algorithm, or application comparison gets its own `<topic>/` folder with a `run.py` entry point.
* **`quality/analysis/results/`:** Build artifacts generated automatically by `run.py`. Never edit these files by hand.
* **`quality/reports/`:** Human-written Markdown reports that interpret the generated JSON metrics and embed the generated figures (`../analysis/results/figures/<name>.png`).

---

## Running Benchmarks

No package installation is required—`harness.py` automatically resolves and adds `engine/src` to `sys.path`. Only `matplotlib` is needed to render charts:

```bash
pip install matplotlib
```

Run any benchmark directly using Python:

```bash
python quality/benchmarks/<topic>/run.py
```

### Execution Lifecycle

When any `run.py` script executes:
1. `workspace("<topic>")` creates an isolated temporary directory inside `quality/.tmp/`.
2. Real database files and workloads are created and executed using deterministic random seeds.
3. Summary results are printed to `stdout`.
4. Structured metrics (alongside Python, OS, CPU, and CoreDB version metadata) are saved to `quality/analysis/results/<topic>.json`.
5. High-DPI plots (`130 dpi`) are saved to `quality/analysis/results/figures/<topic>*.png`.
6. The temporary workspace in `quality/.tmp/` is automatically removed upon exit.

---

## The Benchmark Harness (`harness.py`)

All benchmarks **must** build on `harness.py` rather than implementing custom timers, output paths, or temporary directory logic:

| Utility | Signature | Purpose |
| :--- | :--- | :--- |
| **`workspace`** | `workspace(prefix="bench")` | Context manager that creates `quality/.tmp/<prefix>-*`, redirects `tempfile.tempdir` and `MPLCONFIGDIR` into it, and cleans up all files on exit (even if an exception occurs). |
| **`timed`** | `timed(fn, repeats=3)` | Runs `fn()` `repeats` times with `time.perf_counter()` and returns `(median_seconds, last_result)`. |
| **`save`** | `save(name, payload)` | Prepends environment metadata (`python`, `platform`, `machine`, `cpu_count`, `coredb` version) and writes JSON to `quality/analysis/results/<name>.json`. |
| **`save_figure`** | `save_figure(fig, name)` | Saves a `matplotlib` figure to `quality/analysis/results/figures/<name>.png` at `130 dpi` and closes the figure to free memory. |

---

## Contributor Rules & Best Practices

### 1. Fairness & Reproducibility
* **Deterministic Seeding:** Always instantiate local RNGs with explicit seeds (`rnd = random.Random(SEED)`). Never rely on global unseeded randomness.
* **Controlled Comparisons:** When comparing two or more implementations, algorithms, configurations, or engines, ensure every candidate runs against the **exact same dataset, schema, workload trace, page size, and buffer pool capacity**.
* **Isolate Setup from Measurement:** Flush dirty pages and reset statistics immediately before measuring an operation:
  ```python
  db.checkpoint()
  db.reset_stats()
  ```
* **Account for Repeats in Counters:** `timed(fn, repeats=R)` runs `fn` `R` times while `db.stats()` accumulates totals across all runs. Divide cumulative counters (e.g., `misses // R`) when reporting per-run averages.

### 2. Workspace & Temporary File Hygiene
* **Keep Everything Inside the Repo:** Never write database files or caches to `/tmp`, the OS temp directory, or the current working directory. All temporary files belong inside the `tmp` path yielded by `workspace("<topic>")`.
* **Render Plots Inside `workspace()`:** Matplotlib writes font and build caches to `MPLCONFIGDIR`. Because `workspace()` points `MPLCONFIGDIR` inside `quality/.tmp/`, always call `plot()` *inside* the `with workspace(...)` block so Matplotlib's cache is deleted when the script finishes.
* **Close File Handles Explicitly:** On Windows, open file handles lock directories and prevent cleanup. Always use `with Database(...) as db:` or close low-level managers (`disk.close()`) in a `finally:` block before exiting `workspace()`.

### 3. Dependency & Module Boundaries
* **Engine Isolation:** Benchmarks in `quality/` may import from `coredb` and `examples/`, but `engine/src/coredb` must never import anything from `quality/`.
* **Lazy-Load Plotting Libraries:** Import `matplotlib` and set `matplotlib.use("Agg")` inside your `plot()` function rather than at module top-level so headless environments work cleanly and non-plotting imports stay fast.
* **Never Hand-Edit Results:** Treat `quality/analysis/results/` strictly as generated output. If results are outdated or flawed, update the code and re-run the benchmark.

---

## Adding a New Benchmark

When adding a new engine capability (e.g., index structures, query execution, object storage, concurrency, security) or an application-level comparison:

### 1. Create the Benchmark Folder
Create `quality/benchmarks/<topic>/run.py`, choosing a clear, descriptive `<topic>` name that matches the output JSON filename (`analysis/results/<topic>.json`).

### 2. Start from the Standard Template

```python
"""Benchmark: <concise summary of what is measured or compared>.

Writes analysis/results/<topic>.json and analysis/results/figures/<topic>.png.
Temporary files live in quality/.tmp/ and are deleted when the script ends.
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

# Allow running from any working directory without installing packages
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import save, save_figure, timed, workspace  # noqa: E402

from coredb import Config, Database  # noqa: E402

SEED = 42
REPEATS = 3


def plot(results: dict) -> list[Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4.5))
    # ... render comparison charts from `results` ...
    fig.tight_layout()
    return [save_figure(fig, "<topic>")]


def main() -> None:
    results = {
        "config": {"seed": SEED, "repeats": REPEATS},
        "results": {},
    }

    with workspace("<topic>") as tmp:
        db_path = os.path.join(tmp, "bench.db")
        with Database(db_path, Config(buffer_frames=128)) as db:
            # 1. Generate deterministic workload/data using SEED
            # 2. Reset state prior to timed execution
            db.checkpoint()
            db.reset_stats()

            secs, _ = timed(lambda: ..., repeats=REPEATS)
            st = db.stats()

            results["results"]["workload_a"] = {
                "seconds": round(secs, 4),
                "page_misses": st["pool"]["misses"] // REPEATS,
                "hit_ratio": round(st["pool"]["hit_ratio"], 4),
            }

        print("saved", save("<topic>", results))
        for fig_path in plot(results):
            print("saved", fig_path)


if __name__ == "__main__":
    main()
```

### 3. Verify & Document
1. Execute `python quality/benchmarks/<topic>/run.py` and verify that:
   * `quality/analysis/results/<topic>.json` is generated with valid config and metrics.
   * `quality/analysis/results/figures/<topic>*.png` renders clearly with legible axes and legends.
   * `quality/.tmp/` is completely removed after completion.
2. Document the findings and trade-offs in `quality/reports/`, linking any generated figures via `../analysis/results/figures/<name>.png`.