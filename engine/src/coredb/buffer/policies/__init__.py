from .base import ReplacementPolicy
from .lru import LRUPolicy
from .clock import ClockPolicy
from .lru_k import LRUKPolicy
from .two_q import TwoQPolicy
from .registry import available_policies, create_policy, register_policy

__all__ = ["ReplacementPolicy", "LRUPolicy", "ClockPolicy", "LRUKPolicy", "TwoQPolicy",
           "available_policies", "create_policy", "register_policy"]
