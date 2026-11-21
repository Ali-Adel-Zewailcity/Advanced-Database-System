"""SQL-like query parser and index-aware executor for coredb.

Supports:
  - SELECT <cols|*> FROM <table> [WHERE <predicate>] [ORDER BY <col> [ASC|DESC]] [LIMIT <n>]
  - INSERT INTO <table> [(<cols>)] VALUES (<vals>)
  - UPDATE <table> SET <col>=<val> [, ...] [WHERE <predicate>]
  - DELETE FROM <table> [WHERE <predicate>]

Where predicates support:
  - Equality: `id = 42`, `sku = 'SKU-100'`
  - Range: `unit_price > 10.0`, `unit_price <= 99.99`, `unit_price BETWEEN 10.0 AND 50.0`
"""
from __future__ import annotations

import ast
import operator
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..errors import CatalogError, QueryError
from ..index.base import RID
from ..index.index_manager import IndexManager

_OPS: Dict[str, Callable[[Any, Any], bool]] = {
    "=": operator.eq,
    "==": operator.eq,
    "!=": operator.ne,
    "<>": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@dataclass
class _Predicate:
    column: str
    op: str                     # "=", "<", "<=", ">", ">=", "!=", "BETWEEN"
    value: Any
    high_value: Any = None      # used when op == "BETWEEN"


@dataclass
class QueryResult:
    columns: List[str]
    rows: List[Tuple[Any, ...]]
    plan: str                   # "index_equality_scan" | "index_range_scan" | "full_table_scan" | "dml"
    index_used: Optional[str] = None
    index_type: Optional[str] = None
    rids: List[RID] = field(default_factory=list)
    affected_rows: int = 0
    elapsed_ms: float = 0.0

    def to_dicts(self) -> List[Dict[str, Any]]:
        return [dict(zip(self.columns, r)) for r in self.rows]


class QueryEngine:
    """Executes SQL-like queries using IndexManager + NSM storage tables."""

    def __init__(self, tables: Dict[str, Any], index_manager: IndexManager):
        self.tables = tables
        self.index_manager = index_manager

    def register_table(self, table: Any) -> None:
        self.tables[table.schema.name] = table

    def _get_table(self, name: str):
        tbl = self.tables.get(name)
        if tbl is None:
            raise CatalogError(f"unknown table '{name}'")
        return tbl

    # High-Level Coordinated DML Helpers

    def insert_row(self, table_name: str, row: Sequence[Any]) -> RID:
        """Insert a row into `table_name` and automatically update all its indexes."""
        tbl = self._get_table(table_name)
        rid = tbl.insert(row)
        self.index_manager.on_insert(table_name, rid, row)
        return rid

    def update_row(self, table_name: str, rid: RID, new_row: Sequence[Any]) -> RID:
        """Update a row at `rid` and keep all indexes synchronized (even if RID moves)."""
        tbl = self._get_table(table_name)
        old_row = tbl.get(rid)
        new_rid = tbl.update(rid, new_row)
        self.index_manager.on_update(table_name, rid, old_row, new_rid, new_row)
        return new_rid

    def delete_row(self, table_name: str, rid: RID) -> None:
        """Delete a row at `rid` and remove its entries from all indexes."""
        tbl = self._get_table(table_name)
        old_row = tbl.get(rid)
        tbl.delete(rid)
        self.index_manager.on_delete(table_name, rid, old_row)

    # SQL-Like Query Entry Point

    def execute(self, sql: str, force_full_scan: bool = False) -> QueryResult:
        t0 = time.perf_counter()
        cleaned = sql.strip().rstrip(";").strip()
        if not cleaned:
            raise QueryError("empty query")

        head = cleaned.split(None, 1)[0].upper()
        if head == "SELECT":
            res = self._exec_select(cleaned, force_full_scan=force_full_scan)
        elif head == "INSERT":
            res = self._exec_insert(cleaned)
        elif head == "UPDATE":
            res = self._exec_update(cleaned, force_full_scan=force_full_scan)
        elif head == "DELETE":
            res = self._exec_delete(cleaned, force_full_scan=force_full_scan)
        else:
            raise QueryError(f"unsupported SQL statement '{head}'")

        res.elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return res

    # ---- Value & Predicate Parsing ----------------------------------------

    @staticmethod
    def _parse_literal(token: str) -> Any:
        tok = token.strip()
        upper = tok.upper()
        if upper == "NULL":
            return None
        if upper == "TRUE":
            return True
        if upper == "FALSE":
            return False
        if (tok.startswith("'") and tok.endswith("'")) or (
            tok.startswith('"') and tok.endswith('"')
        ):
            return tok[1:-1]
        try:
            if "." in tok:
                return float(tok)
            return int(tok)
        except ValueError:
            try:
                return ast.literal_eval(tok)
            except Exception as exc:
                raise QueryError(f"cannot parse literal value: {token!r}") from exc

    @classmethod
    def _parse_where(cls, where_clause: Optional[str]) -> Optional[_Predicate]:
        if not where_clause or not where_clause.strip():
            return None
        clause = where_clause.strip()

        m_between = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*)\s+BETWEEN\s+(.+?)\s+AND\s+(.+)$",
            clause,
            flags=re.IGNORECASE,
        )
        if m_between:
            col, low_s, high_s = m_between.groups()
            return _Predicate(
                column=col,
                op="BETWEEN",
                value=cls._parse_literal(low_s),
                high_value=cls._parse_literal(high_s),
            )

        m_cmp = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|<>|<=|>=|=|<|>)\s*(.+)$",
            clause,
        )
        if not m_cmp:
            raise QueryError(f"unsupported WHERE clause: {where_clause!r}")
        col, op, val_s = m_cmp.groups()
        if op == "==":
            op = "="
        return _Predicate(column=col, op=op, value=cls._parse_literal(val_s))

    @staticmethod
    def _matches(row: Tuple[Any, ...], col_idx: int, pred: _Predicate) -> bool:
        val = row[col_idx]
        if val is None:
            return False
        if pred.op == "BETWEEN":
            return pred.value <= val <= pred.high_value
        return _OPS[pred.op](val, pred.value)

    # ---- Candidate RID Resolution (Index vs. Full Scan) -------------------

    def _resolve_candidates(
        self,
        table,
        pred: Optional[_Predicate],
        force_full_scan: bool = False,
    ) -> Tuple[List[Tuple[RID, Tuple[Any, ...]]], str, Optional[str], Optional[str]]:
        """Resolve matching `(rid, full_row)` pairs using an index when possible."""
        table_name = table.schema.name
        if pred is None:
            return list(table.scan()), "full_table_scan", None, None

        col_idx = table.schema.index(pred.column)
        idx = None if force_full_scan else self.index_manager.get_index(table_name, pred.column)

        if idx is not None and pred.op in ("=", "<", "<=", ">", ">=", "BETWEEN"):
            if pred.op == "=":
                rids = idx.search(pred.value)
                rows = [(rid, table.get(rid)) for rid in rids]
                return rows, "index_equality_scan", idx.name, idx.index_type

            if pred.op == "BETWEEN":
                pairs = list(
                    idx.range_scan(
                        low=pred.value,
                        high=pred.high_value,
                        include_low=True,
                        include_high=True,
                    )
                )
            elif pred.op == ">":
                pairs = list(idx.range_scan(low=pred.value, include_low=False))
            elif pred.op == ">=":
                pairs = list(idx.range_scan(low=pred.value, include_low=True))
            elif pred.op == "<":
                pairs = list(idx.range_scan(high=pred.value, include_high=False))
            else:  # "<="
                pairs = list(idx.range_scan(high=pred.value, include_high=True))

            rows = [(rid, table.get(rid)) for _, rid in pairs]
            return rows, "index_range_scan", idx.name, idx.index_type

        # Fallback: full table scan
        matched = [
            (rid, row)
            for rid, row in table.scan()
            if self._matches(row, col_idx, pred)
        ]
        return matched, "full_table_scan", None, None

    # SELECT

    def _exec_select(self, sql: str, force_full_scan: bool = False) -> QueryResult:
        m = re.match(
            r"^SELECT\s+(.+?)\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)"
            r"(?:\s+WHERE\s+(.+?))?"
            r"(?:\s+ORDER\s+BY\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+(ASC|DESC))?)?"
            r"(?:\s+LIMIT\s+(\d+))?$",
            sql,
            flags=re.IGNORECASE,
        )
        if not m:
            raise QueryError(f"invalid SELECT syntax: {sql!r}")

        cols_raw, table_name, where_raw, order_col, order_dir, limit_raw = m.groups()
        table = self._get_table(table_name)
        all_cols = [c.name for c in table.schema.columns]

        if cols_raw.strip() == "*":
            out_cols = all_cols
            proj_indices = list(range(len(all_cols)))
        else:
            out_cols = [c.strip() for c in cols_raw.split(",") if c.strip()]
            proj_indices = [table.schema.index(c) for c in out_cols]

        pred = self._parse_where(where_raw)
        candidates, plan, idx_name, idx_type = self._resolve_candidates(
            table, pred, force_full_scan=force_full_scan
        )

        if order_col:
            ord_idx = table.schema.index(order_col)
            reverse = (order_dir or "ASC").upper() == "DESC"
            candidates.sort(key=lambda item: (item[1][ord_idx] is None, item[1][ord_idx]), reverse=reverse)

        if limit_raw is not None:
            candidates = candidates[: int(limit_raw)]

        rids = [rid for rid, _ in candidates]
        projected = [tuple(row[i] for i in proj_indices) for _, row in candidates]

        return QueryResult(
            columns=out_cols,
            rows=projected,
            plan=plan,
            index_used=idx_name,
            index_type=idx_type,
            rids=rids,
            affected_rows=len(projected),
        )

    # INSERT

    def _exec_insert(self, sql: str) -> QueryResult:
        m = re.match(
            r"^INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)"
            r"(?:\s*\(([^)]+)\))?\s+VALUES\s*\((.+)\)$",
            sql,
            flags=re.IGNORECASE,
        )
        if not m:
            raise QueryError(f"invalid INSERT syntax: {sql!r}")

        table_name, cols_raw, vals_raw = m.groups()
        table = self._get_table(table_name)
        schema_cols = table.schema.columns
        values = [self._parse_literal(v) for v in self._split_csv(vals_raw)]

        if cols_raw:
            col_names = [c.strip() for c in cols_raw.split(",") if c.strip()]
            if len(col_names) != len(values):
                raise QueryError("INSERT column count does not match VALUES count")
            row_list: List[Any] = [getattr(c, "default", None) for c in schema_cols]
            for cname, val in zip(col_names, values):
                row_list[table.schema.index(cname)] = val
        else:
            if len(values) != len(schema_cols):
                raise QueryError(
                    f"INSERT expected {len(schema_cols)} values, got {len(values)}"
                )
            row_list = values

        rid = self.insert_row(table_name, row_list)
        return QueryResult(
            columns=["rid"],
            rows=[(rid,)],
            plan="dml_insert",
            rids=[rid],
            affected_rows=1,
        )

    # UPDATE

    def _exec_update(self, sql: str, force_full_scan: bool = False) -> QueryResult:
        m = re.match(
            r"^UPDATE\s+([A-Za-z_][A-Za-z0-9_]*)\s+SET\s+(.+?)(?:\s+WHERE\s+(.+))?$",
            sql,
            flags=re.IGNORECASE,
        )
        if not m:
            raise QueryError(f"invalid UPDATE syntax: {sql!r}")

        table_name, set_raw, where_raw = m.groups()
        table = self._get_table(table_name)
        pred = self._parse_where(where_raw)

        assignments: List[Tuple[int, Any]] = []
        for part in self._split_csv(set_raw):
            if "=" not in part:
                raise QueryError(f"invalid SET assignment: {part!r}")
            col, val_s = part.split("=", 1)
            assignments.append((table.schema.index(col.strip()), self._parse_literal(val_s)))

        candidates, plan, idx_name, idx_type = self._resolve_candidates(
            table, pred, force_full_scan=force_full_scan
        )
        new_rids: List[RID] = []
        for old_rid, old_row in candidates:
            updated = list(old_row)
            for cidx, new_val in assignments:
                updated[cidx] = new_val
            new_rid = self.update_row(table_name, old_rid, updated)
            new_rids.append(new_rid)

        return QueryResult(
            columns=[],
            rows=[],
            plan=plan,
            index_used=idx_name,
            index_type=idx_type,
            rids=new_rids,
            affected_rows=len(new_rids),
        )

    # DELETE

    def _exec_delete(self, sql: str, force_full_scan: bool = False) -> QueryResult:
        m = re.match(
            r"^DELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+WHERE\s+(.+))?$",
            sql,
            flags=re.IGNORECASE,
        )
        if not m:
            raise QueryError(f"invalid DELETE syntax: {sql!r}")

        table_name, where_raw = m.groups()
        table = self._get_table(table_name)
        pred = self._parse_where(where_raw)

        candidates, plan, idx_name, idx_type = self._resolve_candidates(
            table, pred, force_full_scan=force_full_scan
        )
        deleted_rids: List[RID] = []
        for rid, _ in candidates:
            self.delete_row(table_name, rid)
            deleted_rids.append(rid)

        return QueryResult(
            columns=[],
            rows=[],
            plan=plan,
            index_used=idx_name,
            index_type=idx_type,
            rids=deleted_rids,
            affected_rows=len(deleted_rids),
        )

    @staticmethod
    def _split_csv(text: str) -> List[str]:
        """Split comma-separated tokens while respecting single/double quoted strings."""
        parts: List[str] = []
        buf: List[str] = []
        quote: Optional[str] = None
        for ch in text:
            if quote:
                buf.append(ch)
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
                buf.append(ch)
            elif ch == ",":
                parts.append("".join(buf).strip())
                buf.clear()
            else:
                buf.append(ch)
        if buf:
            parts.append("".join(buf).strip())
        return parts
