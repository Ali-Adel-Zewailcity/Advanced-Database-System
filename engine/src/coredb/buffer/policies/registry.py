from __future__ import annotations

from typing import Dict, List, Type

from ...errors import ConfigError
from .base import ReplacementPolicy
from .clock import ClockPolicy
from .lru import LRUPolicy
from .lru_k import LRUKPolicy
from .two_q import TwoQPolicy

_REGISTRY: Dict[str, Type[ReplacementPolicy]] = {
    "lru": LRUPolicy, "clock": ClockPolicy, "lru-k": LRUKPolicy, "lruk": LRUKPolicy,
    "lru_k": LRUKPolicy, "2q": TwoQPolicy, "twoq": TwoQPolicy,
}


def available_policies() -> List[str]:
    return ["lru", "clock", "lru-k", "2q"]


def register_policy(name: str, cls: Type[ReplacementPolicy]) -> None:
    _REGISTRY[name.lower()] = cls


def create_policy(name: str, capacity: int, **options) -> ReplacementPolicy:
    try:
        cls = _REGISTRY[name.lower()]
    except KeyError:
        raise ConfigError(f"unknown replacement policy {name!r}; choose from {available_policies()}") from None
    return cls(capacity, **options)
