import pytest

from coredb.buffer.policies import (ClockPolicy, LRUKPolicy, LRUPolicy, TwoQPolicy,
                                    available_policies, create_policy)
from coredb.errors import ConfigError


def load(p, pid):          # what the pool does on a fresh load, then unpin
    p.record_access(pid); p.set_evictable(pid, False); p.set_evictable(pid, True)


def touch(p, pid):         # hit on a resident, unpinned page
    p.record_access(pid); p.set_evictable(pid, False); p.set_evictable(pid, True)


def test_registry():
    assert set(available_policies()) == {"lru", "clock", "lru-k", "2q"}
    with pytest.raises(ConfigError):
        create_policy("nope", 4)


def test_lru_order():
    p = LRUPolicy(3)
    for i in (1, 2, 3): load(p, i)
    touch(p, 1)
    assert p.evict() == 2 and p.evict() == 3 and p.evict() == 1 and p.evict() is None


def test_pinned_never_evicted(policy_name):
    p = create_policy(policy_name, 4)
    for i in (1, 2, 3): load(p, i)
    p.set_evictable(1, False); p.set_evictable(2, False)
    assert p.evict() == 3
    assert p.evict() is None
    p.set_evictable(1, True)
    assert p.evict() == 1


def test_remove_forgets(policy_name):
    p = create_policy(policy_name, 4)
    for i in (1, 2): load(p, i)
    p.remove(1)
    assert p.evict() == 2 and p.evict() is None


def test_clock_second_chance():
    p = ClockPolicy(3)
    for i in (1, 2, 3): load(p, i)       # all ref bits set
    # first sweep clears every bit, second takes the first page at the hand
    assert p.evict() == 1
    load(p, 4)                            # takes the freed slot, ref=1
    touch(p, 3)                           # re-reference 3
    assert p.evict() == 2                 # 2 has no ref bit
    assert p.size() == 2


def test_lru_k_prefers_pages_with_fewer_than_k_refs():
    p = LRUKPolicy(4, k=2)
    for i in (1, 2, 3): load(p, i)
    touch(p, 1); touch(p, 2)             # 1,2 have 2 refs; 3 only one -> infinite distance
    assert p.evict() == 3


def test_lru_k_largest_backward_k_distance():
    p = LRUKPolicy(4, k=2)
    load(p, 1); load(p, 2)
    touch(p, 1); touch(p, 2)             # times: 1:[1,3]  2:[2,4]
    touch(p, 2); touch(p, 2)             # 2:[5,6]  -> 1 has the oldest 2nd-last access
    assert p.evict() == 1


def test_lru_k_scan_resistance_vs_lru():
    """A hot page referenced twice survives a one-shot sequential scan under LRU-2 but not LRU."""
    def run(p):
        for pid in (100, 100):
            load(p, pid) if pid == 100 and not getattr(p, "_seen", False) else touch(p, 100)
            p._seen = True
        resident = {100}
        for s in range(1, 8):           # scan of cold pages through a 3-page cache
            if len(resident) == 3:
                resident.discard(p.evict())
            load(p, s); resident.add(s)
        return 100 in resident
    assert run(LRUKPolicy(3, k=2)) is True
    assert run(LRUPolicy(3)) is False


def test_lru_k_keeps_history_of_evicted_pages():
    p = LRUKPolicy(2, k=2)
    load(p, 1); touch(p, 1)
    load(p, 2)
    load(p, 3)
    assert p.evict() in (2, 3)           # infinite-distance pages first
    p.evict()
    load(p, 1) if 1 not in p._resident else None
    assert len(p._hist[1]) == 2


def test_2q_ghost_promotion_to_am():
    p = TwoQPolicy(4)                    # kin = 1, kout = 2
    load(p, 1); load(p, 2)
    assert p.evict() == 1                # A1in FIFO victim, id goes to A1out
    load(p, 1)                           # seen in A1out -> straight to Am
    assert p._where[1] is p._am
    load(p, 3)                           # A1in = [2, 3] > kin
    assert p.evict() == 2                # A1in over quota: evict from it, not hot page 1
    # now A1in has 1 page (not over quota): the Am page may go
    assert p.evict() in (1, 3)


def test_2q_scan_does_not_flush_hot_set():
    p = TwoQPolicy(4)
    load(p, 1); load(p, 2); p.evict(); p.evict()
    load(p, 1); load(p, 2)               # promoted to Am
    for s in range(10, 20):              # long scan: pages enter A1in only
        load(p, s)
        if p.size() > 4:
            p.evict()
    assert 1 in p._am and 2 in p._am
