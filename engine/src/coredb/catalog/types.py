"""Column data types and value <-> storage conversions."""
from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from ..errors import SchemaError

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
_US = _dt.timedelta(microseconds=1)


class DataType(str, Enum):
    INTEGER = "INTEGER"      # signed 64-bit
    FLOAT = "FLOAT"          # IEEE double
    BOOLEAN = "BOOLEAN"
    DECIMAL = "DECIMAL"      # exact, stored as scaled signed 64-bit
    TIMESTAMP = "TIMESTAMP"  # UTC, microseconds since epoch (signed 64-bit)
    VARCHAR = "VARCHAR"
    TEXT = "TEXT"
    BYTES = "BYTES"

    @classmethod
    def parse(cls, name: "str | DataType") -> "DataType":
        if isinstance(name, DataType):
            return name
        key = name.strip().upper()
        alias = {"INT": "INTEGER", "BIGINT": "INTEGER", "BOOL": "BOOLEAN", "DOUBLE": "FLOAT",
                 "REAL": "FLOAT", "TIMESTAMPTZ": "TIMESTAMP", "BLOB": "BYTES", "STRING": "TEXT",
                 "NUMERIC": "DECIMAL"}
        try:
            return cls(alias.get(key, key))
        except ValueError:
            raise SchemaError(f"unknown data type {name!r}") from None


_STRUCTS = {
    DataType.INTEGER: struct.Struct("<q"),
    DataType.FLOAT: struct.Struct("<d"),
    DataType.BOOLEAN: struct.Struct("<?"),
    DataType.DECIMAL: struct.Struct("<q"),
    DataType.TIMESTAMP: struct.Struct("<q"),
}
TYPE_CODES = {t: i + 1 for i, t in enumerate(DataType)}


@dataclass(frozen=True)
class Column:
    name: str
    type: DataType
    nullable: bool = True
    max_length: Optional[int] = None   # VARCHAR(n)
    precision: Optional[int] = None    # DECIMAL(p, s)
    scale: Optional[int] = None
    _st: Optional[struct.Struct] = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", DataType.parse(self.type))
        t = self.type
        if t is DataType.VARCHAR and self.max_length is None:
            object.__setattr__(self, "max_length", 255)
        if t is DataType.DECIMAL:
            if self.precision is None:
                object.__setattr__(self, "precision", 18)
            if self.scale is None:
                object.__setattr__(self, "scale", 2)
            if not (0 < self.precision <= 18) or not (0 <= self.scale <= self.precision):
                raise SchemaError("DECIMAL precision must be 1..18 and 0 <= scale <= precision")
        object.__setattr__(self, "_st", _STRUCTS.get(t))

    @property
    def is_fixed(self) -> bool:
        return self._st is not None

    @property
    def width(self) -> int:
        return self._st.size if self._st else 0

    @property
    def identity(self) -> bool:
        """True when storage form == python form (no conversion needed on read)."""
        return self.type in (DataType.INTEGER, DataType.FLOAT, DataType.BOOLEAN,
                             DataType.BYTES, DataType.VARCHAR, DataType.TEXT)

    def pack(self, raw: Any) -> bytes:
        return self._st.pack(raw)

    def pack_into(self, buf, off: int, raw: Any) -> None:
        self._st.pack_into(buf, off, raw)

    def unpack_from(self, buf, off: int) -> Any:
        return self._st.unpack_from(buf, off)[0]

    def to_storage(self, v: Any) -> Any:
        t = self.type
        try:
            if t is DataType.INTEGER:
                if isinstance(v, float) and not v.is_integer():
                    raise ValueError("non-integral float")
                v = int(v)
                if not -(1 << 63) <= v < (1 << 63):
                    raise ValueError("out of 64-bit range")
                return v
            if t is DataType.FLOAT:
                return float(v)
            if t is DataType.BOOLEAN:
                return bool(v)
            if t is DataType.DECIMAL:
                scaled = int((Decimal(str(v)).scaleb(self.scale)).to_integral_value())
                if abs(scaled) >= 10 ** self.precision:
                    raise ValueError("exceeds precision")
                return scaled
            if t is DataType.TIMESTAMP:
                if isinstance(v, _dt.datetime):
                    if v.tzinfo is None:
                        v = v.replace(tzinfo=_dt.timezone.utc)
                    return (v - _EPOCH) // _US
                return int(v)
            if t is DataType.VARCHAR:
                if not isinstance(v, str):
                    raise ValueError("expected str")
                if len(v) > self.max_length:
                    raise ValueError(f"longer than VARCHAR({self.max_length})")
                return v
            if t is DataType.TEXT:
                if not isinstance(v, str):
                    raise ValueError("expected str")
                return v
            if t is DataType.BYTES:
                return bytes(v)
        except Exception as e:  # noqa: BLE001
            raise SchemaError(f"column {self.name!r} ({t.value}): bad value {v!r}: {e}") from None
        raise SchemaError(f"unsupported type {t}")

    def from_storage(self, raw: Any) -> Any:
        t = self.type
        if t is DataType.DECIMAL:
            return Decimal(raw).scaleb(-self.scale)
        if t is DataType.TIMESTAMP:
            return _EPOCH + raw * _US
        return raw

    def encode_var(self, raw: Any) -> bytes:
        return raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)

    def decode_var(self, b: bytes) -> Any:
        if self.type is DataType.BYTES:
            return bytes(b)
        return bytes(b).decode("utf-8")

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type.value, "nullable": self.nullable,
                "max_length": self.max_length, "precision": self.precision, "scale": self.scale}

    @classmethod
    def from_dict(cls, d: dict) -> "Column":
        return cls(d["name"], DataType.parse(d["type"]), d.get("nullable", True),
                   d.get("max_length"), d.get("precision"), d.get("scale"))
