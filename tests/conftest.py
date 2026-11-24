from __future__ import annotations

import pytest

from aptfinder import settings
from aptfinder.store import connect, init_db


@pytest.fixture(autouse=True)
def example_config(monkeypatch):
    """Tests always read the committed config.example/, never a private config/."""
    monkeypatch.setattr(settings, "CONFIG_DIR", settings.ROOT / "config.example")
    # nor the live address cache (identity/addresses.py): tests see the hand-written parser only
    monkeypatch.setattr("aptfinder.identity.addresses.load_geocodes", lambda: {})


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_db(c)
    yield c
    c.close()
