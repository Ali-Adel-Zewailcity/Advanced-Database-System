import datetime as dt
from decimal import Decimal

import pytest

from coredb import Column, Schema
from coredb.catalog import decode_row, encode_row
from coredb.errors import SchemaError


def schema():
    return Schema([
        Column("id", "INTEGER", nullable=False), Column("name", "VARCHAR", max_length=10),
        Column("price", "DECIMAL", precision=10, scale=2), Column("ok", "BOOLEAN"),
        Column("ts", "TIMESTAMP"), Column("score", "FLOAT"), Column("img", "BYTES"),
        Column("note", "TEXT"),
    ])


def test_roundtrip_with_nulls():
    s = schema()
    ts = dt.datetime(2026, 5, 1, 12, 30, 15, 123456, tzinfo=dt.timezone.utc)
    row = (7, "héllo", Decimal("19.99"), True, ts, 2.5, b"\x00\x01", "x" * 300)
    assert decode_row(s, encode_row(s, row)) == row
    row2 = (1, None, None, None, None, None, None, None)
    assert decode_row(s, encode_row(s, row2)) == row2


def test_validation():
    s = schema()
    with pytest.raises(SchemaError):
        encode_row(s, (None, "a", 1, 1, 1, 1, b"", ""))        # NOT NULL
    with pytest.raises(SchemaError):
        encode_row(s, (1, "x" * 11, 1, 1, 1, 1, b"", ""))      # VARCHAR(10)
    with pytest.raises(SchemaError):
        encode_row(s, (1, "a", Decimal("1e12"), 1, 1, 1, b"", ""))  # precision
    with pytest.raises(SchemaError):
        encode_row(s, (1, "a"))                                 # arity


def test_decimal_is_exact():
    s = Schema([Column("p", "DECIMAL", precision=12, scale=2)])
    assert decode_row(s, encode_row(s, (Decimal("0.10"),)))[0] + decode_row(s, encode_row(s, (Decimal("0.20"),)))[0] == Decimal("0.30")


def test_schema_roundtrip_dict():
    s = schema()
    assert Schema.from_dict(s.to_dict()) == s
