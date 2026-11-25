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
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)  # never geocode for real (extract geocodes new strings)


@pytest.fixture(autouse=True)
def no_network_credit(monkeypatch):
    """The Apify credit guard (`apify.guard`) never reads the live account in tests: $100 left, reserve $0.30."""
    monkeypatch.setattr("aptfinder.intake.apify.credit",
                        lambda tries=6: {"limit": 100.0, "used": 0.0, "left": 100.0, "reserve": 0.30})
    monkeypatch.setattr("aptfinder.intake.apify.reread_usd", lambda run_id: None)  # settle keeps the at-end value


def pytest_configure(config):
    config.addinivalue_line("markers", "real_precheck: run intake.precheck.precheck itself (stubs passed in)")


@pytest.fixture(autouse=True)
def no_network_precheck(request, monkeypatch):
    """The own-site precheck (DNS + one GET) never touches the network in tests: every site proceeds, unless a
    test is marked real_precheck (it then passes its own getaddrinfo and httpx transport)."""
    if "real_precheck" not in request.keywords:
        monkeypatch.setattr("aptfinder.intake.precheck.precheck",
                            lambda url, **kw: {"outcome": "proceed", "http_status": 200, "error": None})


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_db(c)
    yield c
    c.close()
