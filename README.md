# Advanced Database System **Coredb**

A relational database system built from scratch in pure Python, together with real-life applications that
exercise it and the tests, benchmarks and analysis that measure it. The engine is a standalone package with no
third-party dependencies.

## Project components

| # | Component | Purpose | Status |
|---|---|---|---|
| 1 | `engine/` (**coredb**) | The database system itself: storage, buffering, layouts, and everything built on them | In progress (storage engine done) |
| 2 | `applications/retail_on_coredb/` | Real-life Retail Orders application built on our own database | Planned |
| 3 | `applications/retail_on_sqlalchemy/` | The same application built with conventional Python libraries (SQLAlchemy on the SQLite engine) | Planned |
| 4 | `quality/` | Tests, benchmarks, analysis and reports covering all of the above | In progress |

Dependency rules: the engine depends on nothing in this repository; application 2 depends only on the engine; application 3
depends only on SQLAlchemy; `quality/` may depend on everything. The two examples never import each other, and both
are written against the same schema and workloads so the comparison between them is fair.

## Repository structure

```
Advanced-Database-System/
├── README.md                                this file
├── docs/
│   └── Project Details.pdf                  architecture, on-disk formats, requirement checklist, limitations
├── engine/                                  component 1: the database system
│   ├── README.md                            code reference: every package, module, class and function
│   ├── gui/                                 reserved for a future graphical front end
│   └── src/coredb/
│       ├── __init__.py                      public API: Database, Config, Column, Schema, ...
│       ├── config.py                        Config, auto_buffer_frames()
│       ├── errors.py                        exception hierarchy
│       ├── api/
│       │   └── database.py                  Database facade: tables, policy and layout control, statistics
│       ├── storage/
│       │   ├── disk_manager.py              page-level file I/O
│       │   ├── page_directory.py            persistent page id -> location map
│       │   ├── page.py                      page constants
│       │   └── layouts/
│       │       ├── base.py                  common interface of every storage model
│       │       ├── slotted_page.py          NSM page format
│       │       ├── nsm.py                   row-oriented table
│       │       ├── column_store.py          one column's pages + column header page
│       │       └── dsm.py                   column-oriented table
│       ├── buffer/
│       │   ├── buffer_pool.py               frames, pinning, dirty tracking, write-on-evict
│       │   ├── hash_table.py                fixed-size page id -> frame table
│       │   └── policies/                    base, lru, clock, lru_k, two_q, registry
│       ├── catalog/
│       │   ├── types.py, schema.py          column types and table schemas
│       │   ├── codec.py                     row encoding for NSM
│       │   └── store.py                     persistent table catalog
│       ├── objects/                         object storage (planned)
│       ├── security/                        encryption hooks (planned)
│       ├── index/                           B+ Tree, LSM Tree, workload-based index selection (planned)
│       └── sql/                             SQL-like query parsing and execution (planned)
├── examples/                                components 2 and 3 (planned)
│   ├── retail_common/                       shared schema spec, seeded data generator, workloads
│   ├── retail_on_coredb/                    Retail Orders app built on our database
│   └── retail_on_sqlalchemy/                the same app built on SQLAlchemy + SQLite
└── quality/                                 component 4
    ├── tests/
    │   ├── conftest.py                      shared fixtures (project-local temp files)
    │   ├── engine/                          unit and randomized tests, one file per engine module
    │   │   ├── test_disk_manager.py
    │   │   ├── test_buffer_pool.py
    │   │   ├── test_policies.py
    │   │   ├── test_types_codec.py
    │   │   ├── test_nsm.py
    │   │   ├── test_dsm.py
    │   │   └── test_database.py
    │   └── examples/                        both retail apps return identical results (planned)
    ├── benchmarks/
    │   ├── harness.py                       timing, project-local temp workspace, result/figure output
    │   ├── replacement_policies/run.py      LRU / CLOCK / LRU-K / 2Q comparison
    │   ├── storage_layouts/run.py           row (NSM) vs column (DSM) comparison
    │   ├── index_structures/run.py          B+ Tree vs LSM Tree (planned)
    │   └── retail_applications/run.py       our database vs SQLAlchemy + SQLite (planned)
    ├── analysis/results/                    generated: raw data (*.json) and figures/*.png
    └── reports/                             written by hand from the results
```

## How to use and understand the project

### Docs
* [`docs/Project Details.pdf`](docs/Project%20Details.pdf) : read this first. Architecture, on-disk formats, the requirement checklist and the known limitations.
* [`engine/README.md`](engine/README.md): the code reference. Keep it open as a dictionary while reading the source: it lists every
  package, module, class, method, parameter and type.
* `quality/reports/`: what the benchmarks measured and what they show.

### The Engine
Put `engine/src` on the Python path (`PYTHONPATH=engine/src python demo.py`; in PowerShell `$env:PYTHONPATH="engine/src"`):

```python
from coredb import Database, Config, Column

cfg = Config(buffer_frames=256, replacement_policy="lru-k", policy_options={"k": 2})
with Database("shop.db", cfg) as db:
    orders = db.create_table("orders", [Column("id", "INTEGER", nullable=False),
                                        Column("total", "DECIMAL", precision=12, scale=2),
                                        Column("note", "VARCHAR", max_length=100)],
                             layout="nsm")                       # "nsm" = rows, "dsm" = columns
    rid = orders.insert((1, "19.99", "first"))                   # rid == (page, slot)
    print(orders.get(rid))
    db.set_replacement_policy("2q")                              # switch policy at runtime
    print(db.stats())                                            # hits, misses, page I/O
```

To understand the code, read it bottom-up so each file only depends on files you have already read:
1. `config.py` and `errors.py` : basic configurations and errors which are dependencies of all other modules.
1. `storage/disk_manager.py` and `page_directory.py`: how pages live in the file and are found again.
2. `buffer/buffer_pool.py`, then `buffer/policies/`: caching, pinning and eviction.
3. `catalog/`: column types, schemas and row encoding.
4. `storage/layouts/slotted_page.py` and `nsm.py`: row storage; then `column_store.py` and `dsm.py`: column storage.
5. `api/database.py`: how `Database` ties everything together.

### Quality
Nothing needs installing except the two tools used by `quality/`:

```bash
pip install pytest matplotlib

cd quality
python -m pytest --capture=sys                          # correctness tests
python benchmarks/replacement_policies/run.py           # LRU / CLOCK / LRU-K / 2Q comparison
python benchmarks/storage_layouts/run.py                # row (NSM) vs column (DSM) comparison
```

* Tests mirror the engine: one file per module in `quality/tests/engine/`, including randomized tests that compare the engine with a plain Python model.
* Each benchmark prints its numbers, saves raw data to `quality/analysis/results/*.json` and draws its charts into `quality/analysis/results/figures/`.
  The written analysis in `quality/reports/` is not generated: it is written by hand from those results.
* Temporary database files are created in `quality/.tmp/` and deleted when the run ends; nothing is written outside the repository.
  (`--capture=sys` stops pytest itself from using an anonymous capture file in the system temp folder.)

### Real-Life Example
**Retail Orders and Product Search** is the application that exercises the engine on realistic data. It is built twice on the same
12-table schema (customers, sales_orders, sales_order_items, returns, return_items, product_categories, products, inventory_stock,
suppliers, supplier_products, purchase_orders, purchase_order_items), the same seeded data and the same workloads, so the two
versions can be compared directly:
* `examples/retail_common/`: the schema, the data generator and the workloads both apps use.
* `examples/retail_on_coredb/`: the app on our database.
* `examples/retail_on_sqlalchemy/`: the app on SQLAlchemy over SQLite.

It demonstrates transactional purchases (create an order and update inventory together), sales report joins (revenue by product
category and date range), product lookup and range queries, product-image object storage, vector search over embeddings, and
partitioning by customer or region. To understand it, start from the schema in `retail_common/`, read one app, then compare it with
the other; each app's own README will give its run command when the app is added.

## How to contribute

### Where each kind of addition goes

| If you add... | Put it in | Also update |
|---|---|---|
| A new engine capability (object storage, encryption hooks, indexes, SQL-like queries) | its own sub-package in `engine/src/coredb/` (`objects/`, `security/`, `index/`, `sql/`) | tests in `quality/tests/engine/`, a benchmark in `quality/benchmarks/`, `engine/README.md` |
| A graphical front end | `engine/gui/`, using only the engine's public API | its own README |
| The retail app on our database | `examples/retail_on_coredb/` (may import only `coredb`) | `quality/tests/examples/` |
| The retail app on SQLAlchemy | `examples/retail_on_sqlalchemy/` (may import only SQLAlchemy) | `quality/tests/examples/` |
| The retail schema, data generator or workloads | `examples/retail_common/` (shared by both apps) | both apps |
| A new test | `quality/tests/engine/test_<module>.py` (engine) or `quality/tests/examples/` (apps) | |
| A new benchmark | `quality/benchmarks/<topic>/run.py`, built on `harness.py` (`workspace()`, `save()`, `save_figure()`) | `quality/reports/` |
| Explanations and findings | design and formats in `docs/Project Details.pdf`; code reference in `engine/README.md`; benchmark analysis in `quality/reports/` | |

### Rules
* The engine uses only the Python standard library and never imports from `examples/` or `quality/`. Inside the engine, imports point downward only: `storage -> buffer -> catalog -> layouts -> api`.
* The two example apps never import each other.
* Temporary files stay inside the project: benchmarks use `workspace()`, tests use the `tmp_path` fixture (both write to `quality/.tmp/` and clean up).
* Do not edit `quality/analysis/results/` by hand; rerun the benchmark instead. Benchmarks must be fair: seeded data, and the same data, pool size and policy for every option compared.
* Every new public class, function or parameter is added to `engine/README.md`.

### Before you submit
1. Put the change in the part of the project listed above.
2. Add or update its tests, and run `cd quality && python -m pytest --capture=sys`.
3. If the change affects performance, rerun the relevant benchmark and update the report.
4. Update the docs the table names for your change.
