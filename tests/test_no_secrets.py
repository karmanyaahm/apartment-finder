"""No tracked file carries the live dashboard's address (AGENTS.md rule 10, scripts/check_secrets.py)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_secrets", ROOT / "scripts" / "check_secrets.py")
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


def test_tracked_files_carry_no_secrets():
    assert cs.scan_tree(ROOT) == []


def test_checks_fire():
    h = {cs._sha("SomeLongPhrase"), cs._sha("app.example.org")}
    assert cs.findings("a", "GET /SomeLongPhrase/api/places", h) == ["a:1: a string listed in scripts/secret_hashes.txt"]
    assert cs.findings("a", "see https://app.example.org/.", h)
    assert cs.findings("a", "host: dash." + "malhotra" + ".cc", set())  # split so this file passes its own check
    assert cs.findings("a", "token eyJ" + "a" * 50, set())
    assert cs.findings("a", "nothing to see at app.example.com or /OtherPhrase", h) == []


def test_hash_list_is_hashes_only():
    hashes = cs.load_hashes()
    assert len(hashes) >= 2 and all(len(x) == 64 and set(x) <= set("0123456789abcdef") for x in hashes)
