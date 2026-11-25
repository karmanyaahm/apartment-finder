"""Zillow detail pages: fetched per zpid (raw only), read by `extract zillow`, stale statuses to Verify."""

from __future__ import annotations

import json

import pytest

from aptfinder.intake import base
from aptfinder.intake import extract as ex
from aptfinder.intake import zillow_detail as zd
from aptfinder.intake.apify import ActorRun
from aptfinder.store import append_observation, finish_run, start_run

# trimmed from the probe (Apify run q1FIQJc3BZcd1FTeO, 2026-11-25)
BUILDING = {  # Harken: a building zpid returns one representative unit's page (2 bd, $6,853)
    "zillow_id": 466640639, "home_status": "FOR_RENT", "home_type": "APARTMENT", "bedrooms": 2,
    "listing_price": 6853,
    "description": "Harken is a modern apartment community that blends nostalgic charm with contemporary comfort.",
    "whats_special": ["Warm finishes", "More space", "Resort-style pool", "Natural light"],
    "reso_facts": {"feesAndDues": [{"type": "Deposit", "fee": "$500"}, {"type": "Application", "fee": "$0"},
                                   {"type": "Pet", "fee": "$65 monthly"}],
                   "allowedPets": ["Cats", "Dogs", "Small Dogs"],
                   "communityFeatures": ["Clubhouse", "Fitness Center", "Pool", "Smoke Free"],
                   "poolFeatures": ["Pool"], "leaseTerm": "Contact For Details", "furnished": False,
                   "bedrooms": 2, "bathrooms": 2,
                   "atAGlanceFacts": [{"factLabel": "Date available", "factValue": "Mon Jan 4 2027"}]}}
UNIT = {  # a single-family house
    "zillow_id": 122197342, "home_status": "FOR_RENT", "home_type": "SINGLE_FAMILY", "bedrooms": 6,
    "listing_price": 40000,
    "description": "The owner leases the home to a tenant who pays for their own electricity, water, gas, propane, "
                   "and internet expenses.",
    "whats_special": [],
    "reso_facts": {"feesAndDues": [{"type": "Deposit", "fee": "$40,000"}], "allowedPets": ["No Pets"],
                   "laundryFeatures": ["In Unit"], "cooling": ["Air Conditioner", "Central Air"],
                   "parkingFeatures": ["Attached", "Garage", "Off Street"], "leaseTerm": "6 Month",
                   "furnished": True}}
FOR_SALE = {  # 104 Evandale Ave: held as an active rental, the page now says for sale
    "zillow_id": 124743044, "home_status": "FOR_SALE", "home_type": "TOWNHOUSE", "bedrooms": 3,
    "listing_price": 1798000,
    "description": "Sophisticated interiors, refined finishes, and effortless functionality define this townhome.",
    "whats_special": [], "reso_facts": {"feesAndDues": [{"type": "HOA", "fee": "$270 monthly"}], "furnished": False}}


def _zillow(conn):
    """One full Zillow search run: Harken's 1bd and 2bd rows (one zpid), the house, the townhouse."""
    run = start_run(conn, "intake", "zillow", {"scope": "full"})
    for sid, zpid, beds, price in (("466640639:1bd", 466640639, 1, 3900), ("466640639:2bd", 466640639, 2, 5100),
                                   ("122197342", 122197342, 6, 40000), ("124743044", 124743044, 3, 5200)):
        append_observation(conn, run, "zillow", sid, {"address": f"{zpid} Main St, Palo Alto, CA 94301",
                                                       "beds": beds, "lat": 37.44, "lon": -122.16, "kind": "apartment",
                                                       "extra": {"zpid": zpid}},
                           url="https://x", price=price, price_basis="month")
    finish_run(conn, run, "ok", {})


@pytest.fixture
def fake(monkeypatch, tmp_path):
    calls = []
    pages = {"466640639": BUILDING, "122197342": UNIT, "124743044": FOR_SALE}

    def run_actor(actor, run_input, *, max_items, max_charge_usd, **kw):
        calls.append(run_input["zpids"])
        items = [pages[z] for z in run_input["zpids"] if z in pages]
        return ActorRun(actor, f"r{len(calls)}", "SUCCEEDED", items, 0.0, {"page": len(items)}, "u")

    monkeypatch.setattr("aptfinder.intake.apify.run_actor", run_actor)
    monkeypatch.setattr(base, "event_cost", lambda actor, ev: 0.001 * sum((ev or {}).values()))
    monkeypatch.setattr(base, "account_remaining_usd", lambda: 5.0)
    monkeypatch.setattr(base, "load_sources", lambda: {"spend": {"max_usd_per_run": 1.0, "reserve_usd": 1.0}})
    monkeypatch.setattr(zd, "load_sources", base.load_sources)
    monkeypatch.setattr(zd.settings, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(zd.settings, "DATA_DIR", tmp_path)
    return calls


def test_one_page_per_zpid_raw_only_never_coverage_or_retiring(conn, fake):
    _zillow(conn)
    before = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    assert zd.fetch(conn, max_usd=1.0, dry_run=True)["to_fetch"] == 3
    res = zd.fetch(conn, max_usd=1.0, batch=2)
    assert fake == [["122197342", "124743044"], ["466640639"]]  # the building's two rows share one page
    assert res["fetched"] == 3 and res["failed"] == 0 and res["cost_usd"] == 0.003
    assert res["stale"] == {"124743044": "FOR_SALE"}
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == before  # no listing, no place
    run = conn.execute("SELECT * FROM runs WHERE source = 'zillow_detail'").fetchone()
    assert json.loads(run["params"])["scope"] == "targeted" and run["status"] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE source = 'zillow' AND active = 1").fetchone()[0] == 4
    assert not [k for k in base.coverage(conn) if k[0] == "zillow_detail"]
    # a second run fetches nothing: every zpid has a page
    assert zd.fetch(conn, max_usd=1.0)["to_fetch"] == 0 and len(fake) == 2


def test_budget_guard_stops_before_the_reserve(conn, fake, monkeypatch):
    _zillow(conn)
    monkeypatch.setattr(base, "account_remaining_usd", lambda: 1.01)  # reserve 1.00
    res = zd.fetch(conn, max_usd=1.0)
    assert fake == [] and res["status"] == "error" and "reserve" in res["warnings"][0]


def test_detail_lines_leave_out_unit_values_and_placeholders():
    lines = ex.raw_lines("zillow", BUILDING)
    text = "\n".join(lines)
    assert "Community features: Clubhouse; Fitness Center; Pool; Smoke Free" in lines
    assert "Fees: Application: $0; Pet: $65 monthly" in lines  # the unit's deposit is left out
    assert "Pets allowed: Cats; Dogs; Small Dogs" in lines
    assert "6853" not in text and "Jan 4" not in text and "Contact For Details" not in text
    assert "Furnished" not in text  # false is a default on every page, never a statement
    assert "Furnished: yes" in ex.raw_lines("zillow", UNIT)
    assert "Pets allowed: No Pets" in ex.raw_lines("zillow", UNIT)


def test_extract_reads_the_page_and_a_building_sends_one_text(conn, fake, monkeypatch):
    _zillow(conn)
    zd.fetch(conn, max_usd=1.0)
    seen = []

    def stub(batch, model, cap):
        seen.extend(b["text"] for b in batch)
        return [{"key": b["key"], "facts": {"gym": {"value": True, "evidence": "Fitness Center"}}
                 if "Fitness Center" in b["text"] else {"in_unit_laundry": {"value": True, "evidence": "In Unit"}}}
                for b in batch], 0.001

    res = ex.extract(conn, "zillow", ask_fn=stub)
    assert res["listings"] == 4 and res["to_ask"] == 3  # Harken's 1bd and 2bd rows share one text
    assert any("Description: The owner leases the home" in t for t in seen)
    d = json.loads(conn.execute("SELECT data FROM latest_obs WHERE source_id = '466640639:2bd'").fetchone()["data"])
    assert d["beds"] == 2 and "Gym / fitness center (read from text)" in d["amenities"]
    assert d["extra"]["extracted"]["facts"]["gym"]["evidence"] == "Fitness Center"
    p = conn.execute("SELECT price FROM latest_obs WHERE source_id = '466640639:1bd'").fetchone()["price"]
    assert p == 3900  # the page's $6,853 / 2 bd never reaches our rows


def test_for_sale_page_is_a_verify_item_until_checked(conn, fake):
    from aptfinder import verify
    from aptfinder.store import append_check

    _zillow(conn)
    zd.fetch(conn, max_usd=1.0)
    items = verify.stale_queue(conn)
    assert [i["listings"][0]["source_id"] for i in items] == ["124743044"]
    assert items[0]["fields"] == ["price_month"] and "for sale" in items[0]["reason"]
    assert items[0] in verify.all_conflicts(conn)
    append_check(conn, "user", "conflict", "price", "cannot_tell", observation_id=items[0]["listings"][0]["obs_id"],
                 stored_value=5200, note="sold")
    assert verify.stale_queue(conn) == []
