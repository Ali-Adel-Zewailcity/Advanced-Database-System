"""Workload-Based Index Manager for coredb.

Responsibilities:
  1. Select between B+ Tree and LSM Tree based on expected workload profile
     (read/write ratio, range-query ratio, and benchmark-calibrated thresholds)
     or explicit user override ("btree" | "lsm" | "auto").
  2. Maintain secondary and primary indexes consistently across table INSERT,
     UPDATE (including RID relocation), and DELETE operations.
  3. Track live workload telemetry and support migrating an index if observed
     workload characteristics invert.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..errors import CatalogError, IndexError
from .base import Index, RID
from .Bplustree import BPlusTreeIndex
from .LSMtree import LSMTreeIndex


@dataclass
class WorkloadProfile:
    """Describes expected or observed workload characteristics for index selection."""

    read_ratio: float = 0.5          # fraction of operations that are reads [0.0, 1.0]
    range_query_ratio: float = 0.2   # fraction of reads that are range scans [0.0, 1.0]
    expected_rows: int = 10_000
    point_lookup_heavy: bool = True

    def __post_init__(self) -> None:
        if not (0.0 <= self.read_ratio <= 1.0):
            raise ValueError("read_ratio must be between 0.0 and 1.0")
        if not (0.0 <= self.range_query_ratio <= 1.0):
            raise ValueError("range_query_ratio must be between 0.0 and 1.0")

    @property
    def write_ratio(self) -> float:
        return 1.0 - self.read_ratio


@dataclass
class SelectionDecision:
    """Captures the chosen index engine and the quantitative rationale."""

    chosen_type: str        # "btree" or "lsm"
    btree_score: float
    lsm_score: float
    reason: str


class IndexSelectionPolicy:
    """Benchmark-calibrated cost model selecting between B+ Tree and LSM Tree.

    Rationale:
      - B+ Tree excels at read-intensive workloads and linked-leaf range scans
        (O(log_B N + K) sequential leaf traversal without multi-SSTable merge).
      - LSM Tree excels at write-intensive workloads via sequential WAL + MemTable
        appends, avoiding random 4KB page splits and write-on-eviction churn.
    """

    def __init__(
        self,
        write_heavy_cutoff: float = 0.55,
        range_scan_bonus: float = 0.35,
        btree_read_cost: float = 1.0,
        btree_write_cost: float = 3.2,
        lsm_read_cost: float = 1.8,
        lsm_write_cost: float = 1.0,
    ):
        self.write_heavy_cutoff = write_heavy_cutoff
        self.range_scan_bonus = range_scan_bonus
        self.btree_read_cost = btree_read_cost
        self.btree_write_cost = btree_write_cost
        self.lsm_read_cost = lsm_read_cost
        self.lsm_write_cost = lsm_write_cost

    def choose(self, profile: WorkloadProfile, unique: bool = False) -> SelectionDecision:
        r = profile.read_ratio
        w = profile.write_ratio
        rng = profile.range_query_ratio

        # Expected normalized cost per operation (lower is better)
        # Range queries penalize LSM slightly because multiple SSTables must be merged
        effective_lsm_read = self.lsm_read_cost * (1.0 + self.range_scan_bonus * rng)
        btree_cost = r * self.btree_read_cost + w * self.btree_write_cost
        lsm_cost = r * effective_lsm_read + w * self.lsm_write_cost

        # Unique constraints require point-lookup verification on every insert
        if unique:
            lsm_cost += w * 0.6

        if w >= self.write_heavy_cutoff and lsm_cost < btree_cost:
            return SelectionDecision(
                chosen_type="lsm",
                btree_score=round(btree_cost, 4),
                lsm_score=round(lsm_cost, 4),
                reason=(
                    f"Write-intensive workload (write_ratio={w:.2f} >= {self.write_heavy_cutoff:.2f}, "
                    f"range_ratio={rng:.2f}): LSM sequential WAL/MemTable writes minimize page-split I/O "
                    f"(estimated cost {lsm_cost:.2f} vs B+ Tree {btree_cost:.2f})."
                ),
            )

        return SelectionDecision(
            chosen_type="btree",
            btree_score=round(btree_cost, 4),
            lsm_score=round(lsm_cost, 4),
            reason=(
                f"Read/range-oriented workload (read_ratio={r:.2f}, range_ratio={rng:.2f}"
                f"{', unique=True' if unique else ''}): B+ Tree provides direct O(log N) lookups "
                f"and linked-leaf range scans (estimated cost {btree_cost:.2f} vs LSM {lsm_cost:.2f})."
            ),
        )


@dataclass
class _ManagedIndex:
    table_name: str
    column_name: str
    col_idx: int
    index: Index
    decision: SelectionDecision
    observed_reads: int = 0
    observed_writes: int = 0


class IndexManager:
    """Coordinates index creation, workload-based selection, lookup, and DML maintenance."""

    def __init__(
        self,
        pool,
        lsm_root_dir: str | Path,
        policy: Optional[IndexSelectionPolicy] = None,
    ):
        self.pool = pool
        self.lsm_root_dir = Path(lsm_root_dir)
        self.lsm_root_dir.mkdir(parents=True, exist_ok=True)
        self.policy = policy or IndexSelectionPolicy()

        # table_name -> column_name -> _ManagedIndex
        self._indexes: Dict[str, Dict[str, _ManagedIndex]] = {}
        # index_name -> (table_name, column_name)
        self._by_name: Dict[str, Tuple[str, str]] = {}

    # Index Creation & Selection 

    def create_index(
        self,
        table,
        column_name: str,
        index_name: Optional[str] = None,
        index_type: str = "auto",
        unique: bool = False,
        workload: Optional[WorkloadProfile] = None,
        populate: bool = True,
        lsm_memtable_bytes: int = 32 * 1024,
    ) -> Index:
        """Create (and optionally bulk-populate) an index on `table.schema[column_name]`.

        `index_type` may be "auto", "btree", or "lsm".
        """
        table_name = table.schema.name
        if column_name not in [c.name for c in table.schema.columns]:
            raise CatalogError(f"column '{column_name}' does not exist on table '{table_name}'")

        col_idx = table.schema.index(column_name)
        name = index_name or f"idx_{table_name}_{column_name}"
        if name in self._by_name:
            raise IndexError(f"index '{name}' already exists")

        profile = workload or WorkloadProfile()
        kind = index_type.lower()
        if kind == "auto":
            decision = self.policy.choose(profile, unique=unique)
        elif kind in ("btree", "lsm"):
            decision = SelectionDecision(
                chosen_type=kind,
                btree_score=0.0,
                lsm_score=0.0,
                reason=f"Explicitly configured as '{kind}'.",
            )
        else:
            raise IndexError(f"unsupported index_type '{index_type}' (use 'auto', 'btree', or 'lsm')")

        if decision.chosen_type == "btree":
            idx: Index = BPlusTreeIndex.create(
                pool=self.pool,
                name=name,
                column_name=column_name,
                unique=unique,
            )
        else:
            idx = LSMTreeIndex(
                storage_dir=self.lsm_root_dir / name,
                name=name,
                column_name=column_name,
                unique=unique,
                memtable_max_bytes=lsm_memtable_bytes,
            )

        if populate:
            entries = (
                (row[col_idx], rid)
                for rid, row in table.scan()
                if row[col_idx] is not None
            )
            idx.bulk_load(entries)

        managed = _ManagedIndex(
            table_name=table_name,
            column_name=column_name,
            col_idx=col_idx,
            index=idx,
            decision=decision,
        )
        self._indexes.setdefault(table_name, {})[column_name] = managed
        self._by_name[name] = (table_name, column_name)
        return idx

    # Query & Telemetry Accessors 

    def get_index(self, table_name: str, column_name: str) -> Optional[Index]:
        m = self._indexes.get(table_name, {}).get(column_name)
        if m is not None:
            m.observed_reads += 1
            return m.index
        return None

    def has_index(self, table_name: str, column_name: str) -> bool:
        return column_name in self._indexes.get(table_name, {})

    def list_indexes(self, table_name: Optional[str] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        tables = [table_name] if table_name is not None else list(self._indexes.keys())
        for t in tables:
            for col, m in self._indexes.get(t, {}).items():
                out.append({
                    "name": m.index.name,
                    "table": t,
                    "column": col,
                    "type": m.index.index_type,
                    "unique": m.index.unique,
                    "reason": m.decision.reason,
                    "observed_reads": m.observed_reads,
                    "observed_writes": m.observed_writes,
                    "size_bytes": m.index.size_bytes(),
                    "stats": m.index.stats.snapshot(),
                })
        return out

    def get_selection_decision(self, table_name: str, column_name: str) -> Optional[SelectionDecision]:
        m = self._indexes.get(table_name, {}).get(column_name)
        return None if m is None else m.decision

    # ---- Automatic DML Maintenance Hooks ----------------------------------

    def on_insert(self, table_name: str, rid: RID, row: Sequence[Any]) -> None:
        for m in self._indexes.get(table_name, {}).values():
            val = row[m.col_idx]
            if val is not None:
                m.index.insert(val, rid)
                m.observed_writes += 1

    def on_update(
        self,
        table_name: str,
        old_rid: RID,
        old_row: Sequence[Any],
        new_rid: RID,
        new_row: Sequence[Any],
    ) -> None:
        for m in self._indexes.get(table_name, {}).values():
            old_val = old_row[m.col_idx]
            new_val = new_row[m.col_idx]
            if old_val == new_val and old_rid == new_rid:
                continue
            if old_val is not None:
                m.index.delete(old_val, old_rid)
            if new_val is not None:
                m.index.insert(new_val, new_rid)
            m.observed_writes += 1

    def on_delete(self, table_name: str, rid: RID, row: Sequence[Any]) -> None:
        for m in self._indexes.get(table_name, {}).values():
            val = row[m.col_idx]
            if val is not None:
                m.index.delete(val, rid)
                m.observed_writes += 1

    # ---- Dynamic Re-evaluation Based on Observed Workload -----------------

    def evaluate_and_migrate(self, table, column_name: str, min_ops: int = 100) -> Optional[SelectionDecision]:
        """Re-evaluate observed read/write ratio and migrate index type if warranted."""
        table_name = table.schema.name
        m = self._indexes.get(table_name, {}).get(column_name)
        if m is None:
            return None
        total = m.observed_reads + m.observed_writes
        if total < min_ops:
            return m.decision

        obs_read_ratio = m.observed_reads / total
        range_ratio = (
            m.index.stats.range_scans / max(1, m.observed_reads)
            if m.observed_reads > 0
            else 0.0
        )
        obs_profile = WorkloadProfile(
            read_ratio=obs_read_ratio,
            range_query_ratio=min(1.0, range_ratio),
        )
        new_decision = self.policy.choose(obs_profile, unique=m.index.unique)
        if new_decision.chosen_type == m.index.index_type:
            m.decision = new_decision
            return new_decision

        # Migrate to the better-suited index structure
        old_index = m.index
        name = old_index.name
        unique = old_index.unique
        old_index.close()
        del self._indexes[table_name][column_name]
        del self._by_name[name]

        new_idx = self.create_index(
            table=table,
            column_name=column_name,
            index_name=name,
            index_type=new_decision.chosen_type,
            unique=unique,
            workload=obs_profile,
            populate=True,
        )
        self._indexes[table_name][column_name].decision = new_decision
        return new_decision

    def flush_all(self) -> None:
        for cols in self._indexes.values():
            for m in cols.values():
                m.index.flush()

    def close_all(self) -> None:
        for cols in self._indexes.values():
            for m in cols.values():
                m.index.close()
