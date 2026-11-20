"""External Large Object Storage with pluggable encryption hooks.

Stores large binary blobs (e.g., product images exceeding 4KB NSM page limits)
outside slotted heap pages and returns a compact URI (`obj://<object_id>`)
that can be stored in relational columns such as `products.image_url`.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

from ..errors import ObjectStorageError
from .crypto import EncryptionHook, NoOpEncryptionHook

OBJ_URI_PREFIX = "obj://"


@dataclass
class ObjectMetadata:
    object_id: str
    content_type: str
    plain_size: int
    stored_size: int
    sha256: str
    encrypted: bool
    encryption_hook: str

    @property
    def uri(self) -> str:
        return f"{OBJ_URI_PREFIX}{self.object_id}"


class ObjectStore:
    """Manages large binary objects on disk with transparent encrypt/decrypt hooks."""

    def __init__(
        self,
        root_dir: str | Path,
        encryption_hook: Optional[EncryptionHook] = None,
    ):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._hook: EncryptionHook = encryption_hook or NoOpEncryptionHook()

    # ---- Hook Management --------------------------------------------------

    @property
    def encryption_hook(self) -> EncryptionHook:
        return self._hook

    def set_encryption_hook(self, hook: Optional[EncryptionHook]) -> None:
        self._hook = hook or NoOpEncryptionHook()

    # ---- URI Helpers ------------------------------------------------------

    @staticmethod
    def make_uri(object_id: str) -> str:
        return f"{OBJ_URI_PREFIX}{object_id}"

    @staticmethod
    def parse_uri(uri_or_id: str) -> str:
        if uri_or_id.startswith(OBJ_URI_PREFIX):
            return uri_or_id[len(OBJ_URI_PREFIX):]
        return uri_or_id

    def _blob_path(self, object_id: str) -> Path:
        return self.root_dir / f"{object_id}.bin"

    def _meta_path(self, object_id: str) -> Path:
        return self.root_dir / f"{object_id}.meta.json"

    # ---- CRUD Operations --------------------------------------------------

    def put(
        self,
        data: bytes,
        content_type: str = "application/octet-stream",
        object_id: Optional[str] = None,
    ) -> ObjectMetadata:
        """Encrypt (via hook) and persist a large object. Returns its metadata & URI."""
        if not isinstance(data, (bytes, bytearray)):
            raise ObjectStorageError("object data must be bytes")
        raw = bytes(data)
        oid = object_id or uuid.uuid4().hex
        digest = hashlib.sha256(raw).hexdigest()

        encrypted = not isinstance(self._hook, NoOpEncryptionHook)
        stored_bytes = self._hook.encrypt(raw, context=oid.encode("utf-8"))

        meta = ObjectMetadata(
            object_id=oid,
            content_type=content_type,
            plain_size=len(raw),
            stored_size=len(stored_bytes),
            sha256=digest,
            encrypted=encrypted,
            encryption_hook=self._hook.hook_name,
        )

        self._blob_path(oid).write_bytes(stored_bytes)
        self._meta_path(oid).write_text(json.dumps(asdict(meta), indent=2), encoding="utf-8")
        return meta

    def get(self, uri_or_id: str, verify_checksum: bool = True) -> bytes:
        """Fetch an object by `obj://<id>` or raw `<id>`, decrypting via the active hook."""
        oid = self.parse_uri(uri_or_id)
        bpath = self._blob_path(oid)
        mpath = self._meta_path(oid)
        if not bpath.exists() or not mpath.exists():
            raise ObjectStorageError(f"object '{oid}' not found")

        meta_dict = json.loads(mpath.read_text(encoding="utf-8"))
        stored_bytes = bpath.read_bytes()
        plaintext = self._hook.decrypt(stored_bytes, context=oid.encode("utf-8"))

        if verify_checksum:
            actual_sha = hashlib.sha256(plaintext).hexdigest()
            if actual_sha != meta_dict["sha256"]:
                raise ObjectStorageError(f"SHA-256 mismatch for object '{oid}'")

        return plaintext

    def get_raw_stored_bytes(self, uri_or_id: str) -> bytes:
        """Inspect raw on-disk ciphertext bytes (useful for verifying encryption at rest)."""
        oid = self.parse_uri(uri_or_id)
        bpath = self._blob_path(oid)
        if not bpath.exists():
            raise ObjectStorageError(f"object '{oid}' not found")
        return bpath.read_bytes()

    def get_metadata(self, uri_or_id: str) -> ObjectMetadata:
        oid = self.parse_uri(uri_or_id)
        mpath = self._meta_path(oid)
        if not mpath.exists():
            raise ObjectStorageError(f"object '{oid}' not found")
        data = json.loads(mpath.read_text(encoding="utf-8"))
        return ObjectMetadata(**data)

    def delete(self, uri_or_id: str) -> bool:
        oid = self.parse_uri(uri_or_id)
        bpath = self._blob_path(oid)
        mpath = self._meta_path(oid)
        existed = bpath.exists() or mpath.exists()
        if bpath.exists():
            bpath.unlink()
        if mpath.exists():
            mpath.unlink()
        return existed

    def exists(self, uri_or_id: str) -> bool:
        oid = self.parse_uri(uri_or_id)
        return self._blob_path(oid).exists() and self._meta_path(oid).exists()

    def stats(self) -> Dict[str, int]:
        blobs = list(self.root_dir.glob("*.bin"))
        return {
            "object_count": len(blobs),
            "total_stored_bytes": sum(p.stat().st_size for p in blobs),
        }