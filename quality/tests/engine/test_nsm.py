import random
from decimal import Decimal

import pytest

from coredb import Column, Config, Database, Schema
from coredb.errors import RecordNotFoundError, RecordTooLargeError
from coredb.storage.layouts import NSMTable
from coredb.storage.layouts.slotted_page import SlottedPage

SCHEMA = [Column("id", "INTEGER", nullable=False), Column("name", "VARCHAR", max_length=200),
          Column("price", "DECIMAL", precision=12, scale=2)]


def test_slotted_page_basics():
    sp = SlottedPage.init(bytearray(4096))
    s0 = sp.insert(b"aaa"); s1 = sp.insert(b"bbbbbb")
    assert (s0, s1) == (0, 1) and sp.get(0) == b"aaa"
    assert sp.delete(0) and sp.get(0) is None and not sp.delete(0)
    assert sp.insert(b"zz") == 0                 # tombstone slot reused
    assert sp.update(1, b"b") and sp.get(1) == b"b"
    assert sp.update(1, b"x" * 500) and sp.get(1) == b"x" * 500


def test_slotted_page_compaction_reclaims_space():
    sp = SlottedPage.init(bytearray(4096))
    slots = [sp.insert(b"x" * 100) for _ in range(39)]
    assert sp.insert(b"y" * 100) is None          # full
    for s in slots[::2]:
        sp.delete(s)
    s = sp.insert(b"y" * 300)                      # needs compaction of dead bytes
    assert s is not None and sp.get(s) == b"y" * 300
    for i in slots[1::2]:
        assert sp.get(i) == b"x" * 100             # survivors intact, same slot numbers


def test_record_too_large():
    sp = SlottedPage.init(bytearray(4096))
    with pytest.raises(RecordTooLargeError):
        sp.insert(b"x" * 5000)


def test_table_crud_and_scan(dbfile):
    with Database(dbfile, Config(buffer_frames=4)) as db:
        t = db.create_table("t", SCHEMA, layout="nsm")
        rids = [t.insert((i, f"name{i}", Decimal(i) / 4)) for i in range(1000)]
        assert t.count() == 1000 and t.page_count() > 5
        assert t.get(rids[500]) == (500, "name500", Decimal("125.00"))
        t.delete(rids[500])
        with pytest.raises(RecordNotFoundError):
            t.get(rids[500])
        new = t.update(rids[10], (10, "x" * 200, Decimal("1")))
        assert t.get(new)[1] == "x" * 200
        got = {r[0] for _, r in t.scan()}
        assert got == set(range(1000)) - {500} and t.count() == 999
        assert [r for _, r in t.scan(["name", "id"])][0] == ("name0", 0)


def test_persistence_and_freed_space_reuse(dbfile):
    cfg = Config(buffer_frames=4)
    db = Database(dbfile, cfg)
    t = db.create_table("t", SCHEMA, layout="nsm")
    rids = [t.insert((i, "n" * 50, Decimal(1))) for i in range(500)]
    for r in rids[:250]:
        t.delete(r)
    pages_before = t.page_count()
    for i in range(200):
        t.insert((i, "m" * 50, Decimal(2)))        # should reuse freed room, not grow the file
    assert t.page_count() == pages_before
    db.close()
    db = Database(dbfile, cfg)
    t = db.table("t")
    assert t.count() == 250 + 200 and sum(1 for _ in t.scan()) == 450
    db.close()


def test_random_ops_match_python_model(dbfile):
    rnd = random.Random(7)
    with Database(dbfile, Config(buffer_frames=3, replacement_policy="clock")) as db:
        t = db.create_table("t", SCHEMA, layout="nsm")
        model = {}
        for step in range(3000):
            op = rnd.random()
            if op < 0.5 or not model:
                row = (step, "v" * rnd.randint(0, 150), Decimal(rnd.randint(0, 10 ** 6)) / 100)
                model[t.insert(row)] = row
            elif op < 0.75:
                rid = rnd.choice(list(model)); t.delete(rid); del model[rid]
            else:
                rid = rnd.choice(list(model))
                row = (step, "u" * rnd.randint(0, 190), Decimal(1))
                new = t.update(rid, row); del model[rid]; model[new] = row
        assert {rid: row for rid, row in t.scan()} == model
        assert t.count() == len(model)
