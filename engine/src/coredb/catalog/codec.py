"""Row codec used by the row-oriented (NSM) layout.

record = null-bitmap | per non-null column: fixed -> packed value,
                                              variable -> u16 length + bytes
"""
from __future__ import annotations

from typing import Sequence, Tuple

from ..errors import RecordTooLargeError, SchemaError
from .schema import Schema


def encode_row(schema: Schema, row: Sequence) -> bytes:
    cols = schema.columns
    if len(row) != len(cols):
        raise SchemaError(f"expected {len(cols)} values, got {len(row)}")
    bitmap = bytearray((len(cols) + 7) >> 3)
    parts = []
    for i, (c, v) in enumerate(zip(cols, row)):
        if v is None:
            if not c.nullable:
                raise SchemaError(f"column {c.name!r} is NOT NULL")
            bitmap[i >> 3] |= 1 << (i & 7)
            continue
        raw = c.to_storage(v)
        if c.is_fixed:
            parts.append(c.pack(raw))
        else:
            b = c.encode_var(raw)
            if len(b) > 0xFFFF:
                raise RecordTooLargeError(f"value of column {c.name!r} exceeds 64KB")
            parts.append(len(b).to_bytes(2, "little"))
            parts.append(b)
    return bytes(bitmap) + b"".join(parts)


def decode_row(schema: Schema, buf, off: int = 0) -> Tuple:
    cols = schema.columns
    nb = (len(cols) + 7) >> 3
    pos = off + nb
    out = []
    for i, c in enumerate(cols):
        if buf[off + (i >> 3)] >> (i & 7) & 1:
            out.append(None)
            continue
        if c.is_fixed:
            out.append(c.from_storage(c.unpack_from(buf, pos)))
            pos += c.width
        else:
            n = buf[pos] | (buf[pos + 1] << 8)
            pos += 2
            out.append(c.from_storage(c.decode_var(buf[pos:pos + n])))
            pos += n
    return tuple(out)
