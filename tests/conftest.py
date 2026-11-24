from __future__ import annotations

import pytest

from aptfinder.store import connect, init_db


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_db(c)
    yield c
    c.close()
