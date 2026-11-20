import sys
from pathlib import Path

import pytest

# run straight from the repository: no installation needed
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine" / "src"))

from coredb import Config, Database
from coredb.buffer import BufferPool
from coredb.storage import DiskManager


@pytest.fixture
def dbfile(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def disk(dbfile):
    d = DiskManager(dbfile, 4096)
    yield d
    d.close()


def make_pool(disk, frames=4, policy="lru", **opts):
    return BufferPool(disk, frames, policy, **opts)


@pytest.fixture
def pool(disk):
    return make_pool(disk, 4)


@pytest.fixture(params=["lru", "clock", "lru-k", "2q"])
def policy_name(request):
    return request.param
