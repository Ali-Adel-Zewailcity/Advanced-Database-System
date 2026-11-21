"""Compatibility import for BufferPool.

The implementation lives in :mod:`coredb.buffer.buffer_pool`.
"""
from .buffer_pool import BufferPool, Frame, PoolStats

__all__ = ["BufferPool", "Frame", "PoolStats"]
