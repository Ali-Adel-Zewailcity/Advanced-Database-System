"""Engine configuration: the knobs that control storage model and buffer pool."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict

from .errors import ConfigError

PAGE_SIZE = 4096  # hard upper bound required by the spec (max 4KB per page)


def _total_memory_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        pass
    try:  # Windows
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
        return int(st.ullTotalPhys)
    except Exception:
        return 0


def auto_buffer_frames(memory_fraction: float = 0.02, min_frames: int = 64,
                       max_frames: int = 262_144, page_size: int = PAGE_SIZE) -> int:
    """Size the buffer pool from the device's RAM (default: 2% of physical memory)."""
    total = _total_memory_bytes()
    if total <= 0:
        return 1024
    frames = int(total * memory_fraction) // page_size
    return max(min_frames, min(max_frames, frames))


@dataclass
class Config:
    page_size: int = PAGE_SIZE
    buffer_frames: int = 256
    replacement_policy: str = "lru"          # lru | clock | lru-k | 2q
    policy_options: Dict[str, Any] = field(default_factory=dict)
    default_layout: str = "nsm"              # nsm | dsm
    fsync: bool = False                      # fsync on checkpoint / close

    def validate(self) -> "Config":
        ps = self.page_size
        if not (256 <= ps <= PAGE_SIZE) or ps & (ps - 1):
            raise ConfigError(f"page_size must be a power of two in [256, {PAGE_SIZE}], got {ps}")
        if self.buffer_frames < 2:
            raise ConfigError("buffer_frames must be >= 2")
        if self.default_layout.lower() not in ("nsm", "dsm"):
            raise ConfigError("default_layout must be 'nsm' or 'dsm'")
        return self
