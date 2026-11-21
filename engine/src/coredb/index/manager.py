"""Compatibility imports for workload-based index management."""
from .index_manager import (
    IndexManager,
    IndexSelectionPolicy,
    SelectionDecision,
    WorkloadProfile,
)

__all__ = [
    "IndexManager",
    "IndexSelectionPolicy",
    "SelectionDecision",
    "WorkloadProfile",
]
