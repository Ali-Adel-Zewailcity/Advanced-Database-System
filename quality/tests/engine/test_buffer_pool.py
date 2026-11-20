import pytest

from coredb.buffer import BufferPool
from coredb.errors import BufferPoolFullError, PageNotFoundError
from coredb.storage import DiskManager


def fill_pages(disk, n):
    ids = [disk.allocate_page() for _ in range(n)]
    for i in ids:
        disk.write_page(i, bytes([i % 250 + 1]) * 4096)
    return ids


def test_hit_and_miss_accounting(disk, policy_name):
    ids = fill_pages(disk, 6)
    pool = BufferPool(disk, 3, policy_name)
    for i in ids[:3]:
        pool.fetch_page(i); pool.unpin_page(i)
    assert (pool.stats.hits, pool.stats.misses) == (0, 3)
    pool.fetch_page(ids[0]); pool.unpin_page(ids[0])
    assert pool.stats.hits == 1
    for i in ids[3:]:
        pool.fetch_page(i); pool.unpin_page(i)
    assert pool.stats.evictions == 3
    assert pool.stats.hit_ratio == pytest.approx(1 / 7)


def test_page_content_round_trip_through_eviction(disk, policy_name):
    pool = BufferPool(disk, 2, policy_name)
    pages = []
    for k in range(6):
        f = pool.new_page(); f.data[0] = k + 10; pages.append(f.page_id)
        pool.unpin_page(f.page_id, True)
    for k, pid in enumerate(pages):
        with pool.pin(pid) as f:
            assert f.data[0] == k + 10


def test_write_on_eviction_only(disk):
    ids = fill_pages(disk, 4)
    disk.stats.reset()
    pool = BufferPool(disk, 2, "lru")
    f = pool.fetch_page(ids[0]); f.data[0] = 99; pool.unpin_page(ids[0], True)
    assert disk.stats.writes == 0                       # dirty page stays in memory
    for i in ids[1:]:
        pool.fetch_page(i); pool.unpin_page(i)          # evicts ids[0] eventually
    assert disk.stats.writes == 1 and pool.stats.dirty_evictions == 1
    buf = bytearray(4096); disk.read_page(ids[0], buf)
    assert buf[0] == 99


def test_clean_eviction_does_not_write(disk):
    ids = fill_pages(disk, 4)
    disk.stats.reset()
    pool = BufferPool(disk, 2, "lru")
    for i in ids:
        pool.fetch_page(i); pool.unpin_page(i)
    assert disk.stats.writes == 0


def test_pinned_pages_block_eviction(disk, policy_name):
    ids = fill_pages(disk, 3)
    pool = BufferPool(disk, 2, policy_name)
    pool.fetch_page(ids[0]); pool.fetch_page(ids[1])       # both pinned
    with pytest.raises(BufferPoolFullError):
        pool.fetch_page(ids[2])
    pool.unpin_page(ids[1])
    pool.fetch_page(ids[2])                                # now ids[1] is the victim
    assert pool.is_resident(ids[0]) and not pool.is_resident(ids[1])


def test_pin_count_nesting(disk):
    ids = fill_pages(disk, 1)
    pool = BufferPool(disk, 2)
    pool.fetch_page(ids[0]); pool.fetch_page(ids[0])
    assert pool.pin_count(ids[0]) == 2
    pool.unpin_page(ids[0]); assert pool.pin_count(ids[0]) == 1
    pool.unpin_page(ids[0])
    with pytest.raises(ValueError):
        pool.unpin_page(ids[0])


def test_flush_all_persists(dbfile):
    d = DiskManager(dbfile); pool = BufferPool(d, 4)
    f = pool.new_page(); pid = f.page_id; f.data[:5] = b"hello"; pool.unpin_page(pid, True)
    pool.checkpoint(); d.close()
    d = DiskManager(dbfile); buf = bytearray(4096); d.read_page(pid, buf)
    assert bytes(buf[:5]) == b"hello"


def test_delete_page(disk):
    pool = BufferPool(disk, 2)
    f = pool.new_page(); pid = f.page_id
    assert pool.delete_page(pid) is False                  # still pinned
    pool.unpin_page(pid, True)
    assert pool.delete_page(pid) is True
    with pytest.raises(PageNotFoundError):
        pool.fetch_page(pid)


def test_unknown_page_raises(pool):
    with pytest.raises(PageNotFoundError):
        pool.fetch_page(12345)


def test_runtime_policy_switch(disk):
    ids = fill_pages(disk, 5)
    pool = BufferPool(disk, 3, "lru")
    for i in ids[:3]:
        pool.fetch_page(i); pool.unpin_page(i)
    pool.set_policy("2q")
    for i in ids[3:]:
        pool.fetch_page(i); pool.unpin_page(i)
    assert pool.policy_name == "2q" and len(pool.resident_pages()) == 3


def test_page_size_limit_enforced():
    from coredb import Config
    from coredb.errors import ConfigError
    with pytest.raises(ConfigError):
        Config(page_size=8192).validate()


def test_fixed_size_hash_table():
    from coredb.buffer.hash_table import FixedSizeHashTable
    h = FixedSizeHashTable(8)
    n_buckets = len(h._buckets)
    for k in range(0, 800, 7):
        h[k] = k * 2
    assert len(h) == len(range(0, 800, 7)) and h[70] == 140 and 71 not in h
    h[70] = 1
    assert h.get(70) == 1 and h.pop(70) == 1 and h.pop(70, None) is None
    with pytest.raises(KeyError):
        h[70]
    assert len(h._buckets) == n_buckets                 # never resizes
    assert sorted(h) == sorted(k for k in range(0, 800, 7) if k != 70)
