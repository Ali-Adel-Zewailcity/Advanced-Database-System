import pytest

from coredb.errors import ConfigError, CorruptDatabaseError, PageNotFoundError
from coredb.storage import DiskManager


def test_allocate_write_read_persist(dbfile):
    d = DiskManager(dbfile)
    ids = [d.allocate_page() for _ in range(5)]
    for i in ids:
        d.write_page(i, bytes([i + 1]) * 4096)
    d.close()
    d = DiskManager(dbfile)
    assert d.page_count == 5
    buf = bytearray(4096)
    for i in ids:
        d.read_page(i, buf)
        assert buf == bytes([i + 1]) * 4096
    d.close()


def test_directory_spans_many_directory_pages(dbfile):
    d = DiskManager(dbfile)
    n = d.directory.entries_per_page * 2 + 17          # forces 3 directory pages
    ids = [d.allocate_page() for _ in range(n)]
    d.write_page(ids[-1], b"Z" * 4096)
    d.close()
    d = DiskManager(dbfile)
    assert d.page_count == n
    buf = bytearray(4096)
    d.read_page(ids[-1], buf)
    assert buf[:1] == b"Z"
    d.close()


def test_free_and_reuse_persisted(dbfile):
    d = DiskManager(dbfile)
    a, b, c = (d.allocate_page() for _ in range(3))
    d.free_page(b)
    d.close()
    d = DiskManager(dbfile)
    assert d.page_count == 2
    with pytest.raises(PageNotFoundError):
        d.read_page(b, bytearray(4096))
    assert d.allocate_page() == b            # id (and physical slot) reused
    d.close()


def test_unallocated_never_written_reads_zeros(disk):
    p = disk.allocate_page()
    buf = bytearray(b"\xff" * 4096)
    disk.read_page(p, buf)
    assert buf == bytes(4096)


def test_root_page_persists(dbfile):
    d = DiskManager(dbfile)
    p = d.allocate_page()
    d.set_root_page(p)
    d.close()
    assert DiskManager(dbfile).root_page_id == p


def test_rejects_foreign_file_and_page_size_mismatch(tmp_path, dbfile):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 10)
    with pytest.raises(CorruptDatabaseError):
        DiskManager(str(bad))
    DiskManager(dbfile).close()
    with pytest.raises(ConfigError):
        DiskManager(dbfile, 2048)
