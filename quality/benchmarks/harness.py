"""Shared benchmark utilities: timing, a project-local temporary workspace, result and figure output."""
from __future__ import annotations

import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Tuple

QUALITY_DIR = Path(__file__).resolve().parent.parent
# run straight from the repository: no installation needed
sys.path.insert(0, str(QUALITY_DIR.parent / "engine" / "src"))

TMP_ROOT = QUALITY_DIR / ".tmp"
RESULTS_DIR = QUALITY_DIR / "analysis" / "results"
FIGURES_DIR = RESULTS_DIR / "figures"


def environment() -> dict:
    import coredb
    return {"python": sys.version.split()[0], "platform": platform.platform(),
            "machine": platform.machine(), "cpu_count": os.cpu_count(),
            "coredb": coredb.__version__}


def timed(fn: Callable[[], Any], repeats: int = 3) -> Tuple[float, Any]:
    """Run `fn` `repeats` times; return (median seconds, last result)."""
    times, res = [], None
    for _ in range(repeats):
        t0 = time.perf_counter()
        res = fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times), res


@contextmanager
def workspace(prefix: str = "bench") -> Iterator[Path]:
    """A temporary directory INSIDE the project (quality/.tmp/), deleted on exit.

    Database files and any library cache (matplotlib's font cache, `tempfile`) are
    redirected here, so nothing is written outside the project. Cleanup also runs
    when the benchmark fails or is interrupted.
    """
    TMP_ROOT.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix + "-", dir=TMP_ROOT))
    old_tempdir, old_mpl = tempfile.tempdir, os.environ.get("MPLCONFIGDIR")
    tempfile.tempdir = str(path)
    (path / "mpl").mkdir()
    os.environ["MPLCONFIGDIR"] = str(path / "mpl")
    try:
        yield path
    finally:
        tempfile.tempdir = old_tempdir
        if old_mpl is None:
            os.environ.pop("MPLCONFIGDIR", None)
        else:
            os.environ["MPLCONFIGDIR"] = old_mpl
        for _ in range(5):                       # retry: some platforms release file handles late
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                break
            time.sleep(0.1)
        try:
            TMP_ROOT.rmdir()                     # removes quality/.tmp itself once it is empty
        except OSError:
            pass


def save(name: str, payload: dict) -> Path:
    """Write raw results to analysis/results/<name>.json."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"environment": environment(), **payload}
    path = RESULTS_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def save_figure(fig, name: str) -> Path:
    """Write a matplotlib figure to analysis/results/figures/<name>.png."""
    import matplotlib.pyplot as plt
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIGURES_DIR / f"{name}.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path
