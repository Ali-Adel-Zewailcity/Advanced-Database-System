import random
from decimal import Decimal

import pytest

from coredb import Column, Config, Database
from coredb.errors import RecordNotFoundError, SchemaError

SCHEMA = [Column("id", "INTEGER", nullable=False), Column("name", "VARCHAR", max_length=300),
          Column("price", "DECIMAL", precision=12, scale=2), Column("flag", "BOOLEAN"),
          Column("score", "FLOAT")]


def row(i, rnd=None):
    name = None if i % 7 == 0 else "n" * ((i * 37) % 90)
    return (i, name, Decimal(i) / 4, None if i % 5 == 0 else i % 2 == 0, i * 0.5)


@pytest.fixture
def db(dbfile):
    d = Database(dbfile, Config(buffer_frames=6, replacement_policy="2q"))
    yield d
    d.close()


def test_insert_get_scan_multi_page(db):
    t = db.create_table("t", SCHEMA, layout="dsm")
    rids = [t.insert(row(i)) for i in range(3000)]
    assert t.count() == 3000 and t.page_count() > 10
    for i in (0, 1, 7, 1499, 2999):
        assert t.get(rids[i]) == row(i)
    assert [r for _, r in t.scan()] == [row(i) for i in range(3000)]


def test_insert_many_equals_insert(db):
    a = db.create_table("a", SCHEMA, layout="dsm"); b = db.create_table("b", SCHEMA, layout="dsm")
    rows = [row(i) for i in range(1500)]
    for r in rows:
        a.insert(r)
    b.insert_many(rows)
    assert [r for _, r in a.scan()] == [r for _, r in b.scan()] == rows


def test_projection_reads_only_needed_columns(db):
    t = db.create_table("t", SCHEMA, layout="dsm")
    t.insert_many([row(i) for i in range(4000)])
    db.checkpoint()
    db.reset_stats()
    s = sum(t.scan_column("score"))
    one_col_reads = db.stats()["pool"]["misses"]
    db.reset_stats()
    sum(1 for _ in t.scan())
    all_col_reads = db.stats()["pool"]["misses"]
    assert s == sum(i * 0.5 for i in range(4000))
    assert one_col_reads < all_col_reads / 3


def test_update_fixed_and_variable_with_page_split(db):
    t = db.create_table("t", SCHEMA, layout="dsm")
    rows = [row(i) for i in range(600)]
    t.insert_many(rows)
    t.update(10, (10, "q" * 300, Decimal("1.5"), True, 1.0))      # grows a var-length value
    rows[10] = (10, "q" * 300, Decimal("1.5"), True, 1.0)
    for i in range(0, 600, 3):                                    # many growing updates -> splits
        rows[i] = (i, "w" * 250, rows[i][2], rows[i][3], rows[i][4])
        t.update(i, rows[i])
    assert [r for _, r in t.scan()] == rows
    assert [t.get(i) for i in (0, 3, 299, 599)] == [rows[i] for i in (0, 3, 299, 599)]
    t.update_column(5, "name", None)
    assert t.get(5)[1] is None


def test_delete_hides_rows(db):
    t = db.create_table("t", SCHEMA, layout="dsm")
    t.insert_many([row(i) for i in range(100)])
    for i in (3, 50, 99):
        t.delete(i)
    with pytest.raises(RecordNotFoundError):
        t.get(50)
    assert t.count() == 97
    assert [r[0] for _, r in t.scan()] == [i for i in range(100) if i not in (3, 50, 99)]
    assert list(t.scan_column("id")) == [i for i in range(100) if i not in (3, 50, 99)]
    t.insert(row(100))
    assert t.get(100) == row(100)


def test_sort_keeps_tuples_intact_and_compacts(db):
    rnd = random.Random(3)
    t = db.create_table("t", SCHEMA, layout="dsm")
    rows = [row(i) for i in range(1200)]
    rnd.shuffle(rows)
    t.insert_many(rows)
    t.delete(0)
    removed = rows[0]
    t.sort_by("price")
    expect = sorted((r for r in rows if r != removed), key=lambda r: r[2])
    got = [r for _, r in t.scan()]
    assert got == expect                                   # same tuples, sorted by price
    assert t.count() == 1199 and t.column_stats("price")["sorted"] == "asc"
    t.sort_by("name", descending=True)                     # NULLs last, every column follows
    got = [r for _, r in t.scan()]
    non_null = [r[1] for r in got if r[1] is not None]
    assert non_null == sorted(non_null, reverse=True)
    assert all(r[1] is None for r in got[len(non_null):])
    assert sorted(got, key=lambda r: r[0]) == sorted(expect, key=lambda r: r[0])


def test_column_header_metadata(db):
    t = db.create_table("t", SCHEMA, layout="dsm")
    t.insert_many([row(i) for i in range(100)])
    st = t.column_stats("id")
    assert (st["rows"], st["nulls"], st["min"], st["max"]) == (100, 0, 0, 99)
    assert t.column_stats("flag")["nulls"] == 20
    assert t.column_stats("name")["nulls"] == 15


def test_validation_is_atomic(db):
    t = db.create_table("t", SCHEMA, layout="dsm")
    t.insert(row(1))
    with pytest.raises(SchemaError):
        t.insert((2, "ok", Decimal("1"), True, "not-a-float"))
    with pytest.raises(SchemaError):
        t.insert((None, "ok", Decimal("1"), True, 1.0))
    assert t.count() == 1 and list(t.scan_column("id")) == [1]       # nothing half-written


def test_persistence_with_overflow_directory(dbfile):
    cfg = Config(buffer_frames=6, page_size=256)          # tiny pages -> directory spills to overflow pages
    db = Database(dbfile, cfg)
    t = db.create_table("t", [Column("v", "BOOLEAN")], layout="dsm")
    n = 20000
    t.insert_many([(i % 3 == 0,) for i in range(n)])
    pages = t.column_stats("v")["pages"]
    db.close()
    db = Database(dbfile, cfg)
    t = db.table("t")
    assert pages > 26 and t.count() == n                  # more entries than fit in the header page
    assert t.get(n - 1) == (((n - 1) % 3 == 0),) and t.get(12345) == (12345 % 3 == 0,)
    assert sum(1 for _ in t.scan_column("v")) == n
    db.close()


def test_persistence_roundtrip_all_types(dbfile):
    cfg = Config(buffer_frames=5)
    db = Database(dbfile, cfg)
    t = db.create_table("t", SCHEMA, layout="dsm")
    t.insert_many([row(i) for i in range(2500)])
    t.delete(7); t.sort_by("id", descending=True)
    db.close()
    db = Database(dbfile, cfg)
    t = db.table("t")
    assert t.count() == 2499
    assert [r[0] for _, r in t.scan()] == sorted((i for i in range(2500) if i != 7), reverse=True)
    db.close()


def test_random_ops_match_python_model(dbfile):
    rnd = random.Random(11)
    with Database(dbfile, Config(buffer_frames=5, replacement_policy="lru-k")) as db:
        t = db.create_table("t", SCHEMA, layout="dsm")
        model = []                                          # list of rows (None = deleted)
        for step in range(1500):
            op = rnd.random()
            if op < 0.6 or not model:
                r = row(step); t.insert(r); model.append(r)
            elif op < 0.85:
                i = rnd.randrange(len(model))
                if model[i] is not None:
                    r = (step, "z" * rnd.randint(0, 200), Decimal(step), None, 0.0)
                    t.update(i, r); model[i] = r
            else:
                i = rnd.randrange(len(model))
                if model[i] is not None:
                    t.delete(i); model[i] = None
        assert [r for _, r in t.scan()] == [r for r in model if r is not None]
