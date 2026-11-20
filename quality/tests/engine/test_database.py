import pytest

from coredb import Column, Config, Database
from coredb.errors import CatalogError, ConfigError


def test_catalog_persists_mixed_layouts(dbfile):
    cfg = Config(buffer_frames=4)
    with Database(dbfile, cfg) as db:
        a = db.create_table("rows", [Column("id", "INTEGER")], layout="nsm")
        b = db.create_table("cols", [Column("id", "INTEGER"), Column("s", "TEXT")], layout="dsm")
        a.insert((1,)); b.insert((2, "x"))
        with pytest.raises(CatalogError):
            db.create_table("rows", [Column("id", "INTEGER")])
    with Database(dbfile, cfg) as db:
        assert db.tables() == ["cols", "rows"]
        assert db.layout_of("rows") == "nsm" and db.layout_of("cols") == "dsm"
        assert list(db.table("rows").scan())[0][1] == (1,)
        assert db.table("cols").get(0) == (2, "x")


def test_many_tables_overflow_catalog_page(dbfile):
    with Database(dbfile, Config(buffer_frames=4)) as db:
        for i in range(60):
            db.create_table(f"table_{i}", [Column(f"col{j}", "VARCHAR", max_length=20) for j in range(12)])
    with Database(dbfile, Config(buffer_frames=4)) as db:
        assert len(db.tables()) == 60


def test_drop_table_frees_pages(dbfile):
    with Database(dbfile, Config(buffer_frames=4)) as db:
        t = db.create_table("t", [Column("id", "INTEGER")], layout="dsm")
        t.insert_many([(i,) for i in range(5000)])
        used = db.disk.page_count
        db.drop_table("t")
        assert db.disk.page_count < used and db.tables() == []
        db.create_table("t2", [Column("id", "INTEGER")], layout="nsm")   # reuses freed ids
        with pytest.raises(CatalogError):
            db.table("t")


def test_policy_and_layout_are_configurable(dbfile):
    with Database(dbfile, Config(buffer_frames=4, replacement_policy="clock", default_layout="dsm")) as db:
        assert db.stats()["policy"] == "clock"
        t = db.create_table("t", [Column("id", "INTEGER")])
        assert t.layout_name == "dsm"
        db.set_replacement_policy("lru-k", k=3)
        assert db.stats()["policy"] == "lru-k"
    with pytest.raises(ConfigError):
        Database(dbfile + "2", Config(replacement_policy="fifo"))
