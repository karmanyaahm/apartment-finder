"""Photo store (aptfinder.media): re-encoding, content addressing, dedupe in the write path, the cap, serving."""

from __future__ import annotations

import hashlib
import io

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from aptfinder import media, settings
from aptfinder.store import append_link, append_observation, finish_run, start_run
from aptfinder.store.seed import seed_db
from aptfinder.web.app import create_app, gather_photos


def picture(seed: int, size=(1600, 1200), quality=95) -> bytes:
    """A deterministic 'room': gradient wall, a few blocks whose layout depends on the seed."""
    w, h = 1600, 1200
    im = Image.new("RGB", (w, h))
    px = im.load()
    for x in range(0, w, 4):
        for y in range(0, h, 4):
            c = (x * 255 // w, y * 255 // h, (x + y) * 127 // (w + h))
            for dx in range(4):
                for dy in range(4):
                    px[x + dx, y + dy] = c
    d = ImageDraw.Draw(im)
    import random

    r = random.Random(seed)
    for _ in range(6):
        x0, y0 = r.randrange(0, w - 400), r.randrange(0, h - 300)
        d.rectangle([x0, y0, x0 + r.randrange(150, 400), y0 + r.randrange(100, 300)],
                    fill=tuple(r.randrange(256) for _ in range(3)))
    im = im.resize(size)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def test_same_photo_recompressed_and_resized_is_one_picture_a_different_one_is_not():
    a = media.encode(picture(1), "photo")
    a_small = media.encode(picture(1, size=(640, 480), quality=40), "photo")
    b = media.encode(picture(2), "photo")
    assert max(a["width"], a["height"]) == 1600  # original size recorded
    assert media.same_picture(a["phash"], a_small["phash"], "photo") is not None
    assert media.same_picture(a["phash"], b["phash"], "photo") is None
    # drawings dedupe only when identical
    assert media.same_picture(a["phash"], a_small["phash"], "plan") in (None, 0)


def test_encode_downscales_and_names_the_file_by_its_hash(tmp_path):
    r = media.encode(picture(1), "photo")
    im = Image.open(io.BytesIO(r["jpeg"]))
    assert max(im.size) == media.PHOTO_PX
    assert max(Image.open(io.BytesIO(media.encode(picture(1), "plan")["jpeg"])).size) == media.PLAN_PX
    small = media.encode(picture(1, size=(300, 200)), "photo")
    assert Image.open(io.BytesIO(small["jpeg"])).size == (300, 200)  # never upscaled
    media.write_file(r, tmp_path)
    p = media.media_path(r["sha256"], tmp_path)
    assert p == tmp_path / r["sha256"][:2] / f"{r['sha256']}.jpg"
    assert hashlib.sha256(p.read_bytes()).hexdigest() == r["sha256"]
    assert media.decide(r, "photo", {}, tmp_path) == (r["sha256"], 0)  # the same bytes: nothing new to write


def test_listing_images_reads_every_shape():
    data = {"media": ["https://a/1.jpg"], "extra": {"images": [{"url": "https://a/2.jpg"}, {"src": "https://a/1.jpg"},
                                                               "not a url"], "floorplan_image": "https://a/p.png"}}
    assert media.listing_images(data) == [("https://a/p.png", "plan"), ("https://a/1.jpg", "photo"),
                                          ("https://a/2.jpg", "photo")]


@pytest.fixture
def place(conn, monkeypatch, tmp_path):
    """One place with four photo URLs on two sites: A, A re-sized on another site, B, and a dead link."""
    imgs = {"https://cdn1/a.jpg": picture(1), "https://cdn2/a_small.jpg": picture(1, (640, 480), 40),
            "https://cdn1/b.jpg": picture(2)}
    run = start_run(conn, "intake", "zillow")
    base = {"lat": 37.4, "lon": -122.1, "beds": 3, "baths": 2, "kind": "apartment", "address": "1 Test St"}
    append_observation(conn, run, "zillow", "z1", {**base, "extra": {"images": ["https://cdn1/a.jpg", "https://cdn1/b.jpg"]}},
                       url="https://zillow.example/z1", price=3000, price_basis="month")
    append_observation(conn, run, "zumper", "u1", {**base, "extra": {"images": ["https://cdn2/a_small.jpg", "https://cdn2/gone.jpg"]}},
                       url="https://zumper.example/u1", price=3000, price_basis="month")
    finish_run(conn, run, "ok", {})
    for src, sid in (("zillow", "z1"), ("zumper", "u1")):
        append_link(conn, "rule", src, sid, "p1", "p1#u")

    def handler(req: httpx.Request) -> httpx.Response:
        body = imgs.get(str(req.url))
        return httpx.Response(200, content=body) if body else httpx.Response(404)

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(media, "DELAY_S", 0)
    monkeypatch.setattr(settings, "MEDIA_DIR", tmp_path / "media")
    return conn


def test_fetch_dedupes_in_the_write_path_records_failures_and_resumes(place):
    conn = place
    c = media.fetch(conn, log=lambda m: None)
    assert c["written"] == 2 and c["near"] == 1 and c["failed"] == 1
    rows = {r["url"]: dict(r) for r in conn.execute("SELECT * FROM media")}
    a, small = rows["https://cdn1/a.jpg"], rows["https://cdn2/a_small.jpg"]
    # whichever copy arrived first is stored; the other points at it (downloads run in parallel)
    kept, dup = (a, small) if small["same_as"] else (small, a)
    assert kept["same_as"] is None and kept["stored_bytes"]
    assert dup["same_as"] == kept["sha256"] and dup["stored_bytes"] is None and dup["distance"] <= media.PHOTO_MAX_DISTANCE and dup["sha256"] != kept["sha256"]
    assert a["width"] == 1600 and small["width"] == 640  # original sizes: the better source is derivable
    assert rows["https://cdn2/gone.jpg"]["status"] == 404
    assert len(list((settings.MEDIA_DIR).rglob("*.jpg"))) == 2
    # rerun: nothing to do; the failure is retried only on request
    assert not media.fetch(conn, log=lambda m: None).get("written")
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 4
    assert media.fetch(conn, retry_failed=True, log=lambda m: None)["failed"] == 1
    s = media.status(conn)
    assert (s["downloaded"], s["files"], s["failed"], s["to_fetch"]) == (3, 2, 1, 0)
    # the drawer: two local photos (A shown once), the dead link falls back to its URL
    listings = [{"source": r["source"], "url": r["url"], "data": __import__("json").loads(r["data"])}
                for r in conn.execute("SELECT * FROM listings")]
    photos = gather_photos(listings, files=media.url_files(conn))
    local = [p["url"] for p in photos if p["url"].startswith("media/")]
    assert sorted(local) == sorted(f"media/{rows[u]['sha256']}.jpg" for u in (kept["url"], "https://cdn1/b.jpg"))
    assert [p["url"] for p in photos if not p["url"].startswith("media/")] == ["https://cdn2/gone.jpg"]


def test_fetch_stops_at_the_cap(place, monkeypatch):
    monkeypatch.setattr(media, "max_bytes", lambda: 1000)
    c = media.fetch(place, log=lambda m: None)
    assert c["full"] and not c.get("written")
    assert media.status(place)["full"]


def test_media_files_are_served_cacheable_also_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_DIR", tmp_path / "media")
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=False)
    r = media.encode(picture(3), "photo")
    media.write_file(r)
    for app in (create_app(db), create_app(db, redact={"Showers": "S"})):
        c = TestClient(app)
        resp = c.get(f"/media/{r['sha256']}.jpg")
        assert resp.status_code == 200 and resp.content == r["jpeg"]
        assert "immutable" in resp.headers["cache-control"]
        assert c.get("/media/" + "0" * 64 + ".jpg").status_code == 404


def test_drawer_collapses_near_copies_stored_as_separate_files():
    """Files stored before the threshold was tuned (or under an exact-only pilot) still show once."""
    a, a2, b = (media.encode(x, "photo") for x in (picture(1), picture(1, (640, 480), 40), picture(2)))
    files = {u: {"file": r["sha256"], "phash": r["phash"], "kind": "photo"}
             for u, r in (("https://x/a", a), ("https://y/a2", a2), ("https://x/b", b))}
    listings = [{"source": "zillow", "url": "https://z", "data": {"extra": {"images": ["https://x/a", "https://x/b"]}}},
                {"source": "zumper", "url": "https://u", "data": {"extra": {"images": ["https://y/a2"]}}}]
    assert [p["original"] for p in gather_photos(listings, files=files)] == ["https://x/a", "https://x/b"]


def test_plan_drawings_are_served_from_the_store():
    from aptfinder.web.app import local_plan_images

    plans = [{"image": "https://x/plan.png"}, {"image": "https://x/other.png"}, {"image": None}]
    out = local_plan_images(plans, {"https://x/plan.png": {"file": "ab" * 32}})
    assert [p["image"] for p in out] == [f"media/{'ab' * 32}.jpg", "https://x/other.png", None]


def test_residential_hosts_go_through_the_proxy_smaller_variant_first(conn, monkeypatch, tmp_path):
    full = "https://staticproperties.furnishedfinder.com/1001/1/1001_1_99-full.jpg"
    assert media.smaller_variant(full) == "https://staticproperties.furnishedfinder.com/1001/1/1001_1_99-650-570.jpg"
    assert media.smaller_variant("https://cdn1/a.jpg") is None
    run = start_run(conn, "intake", "furnishedfinder")
    append_observation(conn, run, "furnishedfinder", "f1", {"lat": 37.4, "lon": -122.1, "extra": {"images": [full]}},
                       url="https://ff.example/f1")
    finish_run(conn, run, "ok", {})
    seen, made = [], []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, content=picture(5)) if "-650-570" in str(req.url) else httpx.Response(403)

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: made.append(kw.get("proxy")) or real(
        transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "proxy"}))
    monkeypatch.setattr(media, "residential_proxy", lambda: ("http://user:s3cret@proxy.example:8000", "s3cret"))
    monkeypatch.setattr("aptfinder.config.load_media", lambda: {"residential_hosts": ["staticproperties.furnishedfinder.com"]})
    monkeypatch.setattr(media, "DELAY_S", 0)
    monkeypatch.setattr(settings, "MEDIA_DIR", tmp_path / "media")
    c = media.fetch(conn, log=lambda m: None)
    assert c["written"] == 1 and c["proxy_bytes"] > 0
    assert seen == [media.smaller_variant(full)]  # the -full original was never fetched
    assert "http://user:s3cret@proxy.example:8000" in made
    assert conn.execute("SELECT url, status FROM media").fetchall()[0][:] == (full, 200)


def test_blocked_host_drawing_shows_from_the_store_and_site_drawings_are_fetched():
    from aptfinder.plans import plan_image

    u = "https://images1.apartments.com/i2/x/plan.jpg"
    ls = [{"source": "apartments_com", "data": {"extra": {"floorplan_image": u}}, "fields": {}}]
    assert plan_image(ls, {}, skip=("images1.apartments.com",)) is None
    assert plan_image(ls, {}, skip=("images1.apartments.com",), local={u: {"file": "f"}}) == u
    data = {"extra": {"plan_drawings": {"Plan A": "https://site/a.svg", "Plan B": "https://site/b.png"}}}
    assert media.listing_images(data) == [("https://site/a.svg", "plan"), ("https://site/b.png", "plan")]


def test_a_merge_above_the_threshold_is_dropped_and_fetched_again(place, monkeypatch):
    conn = place
    media.fetch(conn, log=lambda m: None)
    dup = conn.execute("SELECT url, distance FROM media WHERE same_as IS NOT NULL AND same_as <> sha256").fetchone()
    assert dup and dup["url"] in media.url_files(conn)
    monkeypatch.setattr(media, "PHOTO_MAX_DISTANCE", dup["distance"] - 1)  # threshold lowered below that merge
    assert dup["url"] not in media.url_files(conn) and media.status(conn)["to_fetch"] == 1
    c = media.fetch(conn, log=lambda m: None)
    assert c["written"] == 1 and dup["url"] in media.url_files(conn)


SVG = b"""<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
 width="300" height="200"><rect x="10" y="10" width="280" height="180" fill="none" stroke="black" stroke-width="4"/>
<line x1="120" y1="10" x2="120" y2="190" stroke="black" stroke-width="3"/>
<image xlink:href="http://example.invalid/tracker.png" width="10" height="10"/></svg>"""


def test_svg_drawings_are_rasterised_without_fetching_anything():
    assert media.is_svg(SVG) and not media.is_svg(picture(1)[:100])
    r = media.encode(SVG, "plan")
    im = Image.open(io.BytesIO(r["jpeg"]))
    assert im.format == "JPEG" and max(im.size) == media.PLAN_PX
    assert im.getpixel((5, 5)) > (240, 240, 240)  # white background, not black


def test_drawings_dedupe_only_on_identical_bytes(tmp_path):
    a = media.encode(SVG, "plan")
    other = {"sha256": "0" * 64, "phash": a["phash"]}  # same pHash, different bytes
    assert media.decide(other, "plan", {a["sha256"]: (a["phash"], "plan")}, tmp_path) == (None, None)


def test_curl_hosts_download_through_curl(place, monkeypatch):
    conn = place
    calls = []

    def fake_get(self, url, headers=None):
        calls.append(url)
        return media.CurlClient.Response(200, picture(7))

    monkeypatch.setattr(media.CurlClient, "get", fake_get)
    monkeypatch.setattr("aptfinder.config.load_media", lambda: {"curl_hosts": ["cdn2"]})
    c = media.fetch(conn, log=lambda m: None)
    assert sorted(calls) == ["https://cdn2/a_small.jpg", "https://cdn2/gone.jpg"]  # only cdn2 went through curl
    assert not c.get("failed")
