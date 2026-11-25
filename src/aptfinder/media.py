"""aptf-media: our own copy of every listing photo and floor-plan drawing (photo URLs change or vanish; some hosts
refuse hotlinking). Downloads only image files at URLs the sources already gave us (AGENTS.md rule 6 exception,
2026-11-24), re-encodes each to a small JPEG and stores it content-addressed under data/media/<sha[:2]>/<sha>.jpg.

Deduplicated in the write path: before writing, a download whose bytes are already stored, or whose pHash is within
PHOTO_MAX_DISTANCE bits of a file already stored for the same place (drawings: only identical drawings), is recorded
as `same_as` that file and nothing is written. Every attempt, failures included, is a row in the append-only `media`
table. A hard cap (config/media.toml `[media] max_gb`, default 5) stops the run before a write would pass it, so URLs
are fetched in priority order: active listings, places that pass or need a check on a goal, drawings, the first
FIRST_N photos of each listing, then the rest."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import settings

PHOTO_PX, PHOTO_Q = 800, 75  # long side, JPEG quality
PLAN_PX, PLAN_Q = 1280, 80  # drawings keep their text readable
PHOTO_MAX_DISTANCE = 6  # pHash bits: at or under this, two photos of one place are the same picture. 2026-11-25:
# every pair at 0-6 was one picture; at 8-10, pairs within one listing included different shots of one room (and
# two different rooms), so 10 was lowered back to 6. A merge made at a larger distance than this is not used:
# url_files drops it and the next fetch downloads that URL again.
FIRST_N = 12  # photos per listing fetched before any listing's later photos
PER_HOST, DELAY_S, WORKERS = 4, 0.25, 24
HOST_GIVE_UP = 20  # consecutive 401/403 from a host: skip its other URLs this run (they stay "to fetch")
MAX_DOWNLOAD = 25 * 1024 * 1024
# Hosts that refuse this machine's downloads go through Apify's residential proxy (config/media.toml
# `residential_hosts`); it bills by traffic, so bytes through it are counted and capped (`residential_max_mb`).
PROXY_USD_PER_GB = 8.0
# a smaller variant the host serves, tried first (Furnished Finder: '-650-570' next to '-full' originals)
SMALLER = [(re.compile(r"^(https://staticproperties\.furnishedfinder\.com/.+)-full(\.\w+)$"), r"\1-650-570\2")]
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"


# ---------------------------------------------------------------------------------------------------- images
def _urls(v: Any) -> list[str]:
    """Photo fields come as a URL, a list of URLs, or dicts with url/src."""
    if not v:
        return []
    if isinstance(v, (str, dict)):
        v = [v]
    out = []
    for x in v:
        u = x if isinstance(x, str) else (x.get("url") or x.get("src")) if isinstance(x, dict) else None
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            out.append(u)
    return out


def listing_images(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(url, kind) of one observation's photos and floor-plan drawings, in source order."""
    extra = data.get("extra") or {}
    out = [(u, "plan") for k in ("floorplan_image", "plan_image") for u in _urls(extra.get(k))]
    if isinstance(pd := extra.get("plan_drawings"), dict):  # own-site drawings by label
        out += [(u, "plan") for u in _urls(list(pd.values()))]
    out += [(u, "photo") for u in _urls(data.get("media")) + _urls(extra.get("images"))]
    seen: set[str] = set()
    return [x for x in out if not (x[0] in seen or seen.add(x[0]))]


def smaller_variant(url: str) -> str | None:
    for rx, repl in SMALLER:
        if rx.match(url):
            return rx.sub(repl, url)
    return None


def residential_proxy() -> tuple[str, str]:
    """(proxy URL, password) for Apify's US residential proxy; the password is read from the Apify API with
    APIFY_TOKEN at run time and never printed or stored."""
    import httpx

    token = settings.secret("APIFY_TOKEN")
    if not token:
        raise SystemExit("APIFY_TOKEN is not set in .env (needed for residential_hosts)")
    r = httpx.get("https://api.apify.com/v2/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    pw = r.json()["data"]["proxy"]["password"]
    return f"http://groups-RESIDENTIAL,country-US:{pw}@proxy.apify.com:8000", pw


def distance(a: str, b: str) -> int:
    """Hamming distance of two 64-bit pHashes (hex)."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def same_picture(a: str, b: str, kind: str) -> int | None:
    """The distance when two images count as one picture, else None. Drawings only when identical: two plans of
    one building can hash close."""
    d = distance(a, b)
    return d if d <= (0 if kind == "plan" else PHOTO_MAX_DISTANCE) else None


def is_svg(content: bytes) -> bool:
    head = content[:2048].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith((b"<?xml", b"<!doctype svg", b"<!--")) and b"<svg" in head)


def rasterise_svg(content: bytes) -> bytes:
    """PNG of an SVG at PLAN_PX wide. `unsafe=False`: no XML entities, and external references (images, CSS)
    are never fetched (cairosvg's safe_fetch allows data: URLs only), so the drawing's own shapes only."""
    import cairosvg

    return cairosvg.svg2png(bytestring=content, unsafe=False, output_width=PLAN_PX, background_color="white")


class CurlClient:
    """`client.get` through the curl binary, for hosts that refuse Python's HTTP client (config/media.toml
    `curl_hosts`; Furnished Finder answers 403 to httpx and 200 to curl from the same machine, 2026-11-25)."""

    class Response:
        def __init__(self, status_code: int, content: bytes):
            self.status_code, self.content = status_code, content

    def get(self, url: str, headers: dict[str, str] | None = None) -> "CurlClient.Response":
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile() as f:
            cmd = ["curl", "-s", "-L", "--fail", "--max-time", "30", "-A", UA, "-o", f.name, "-w", "%{http_code}"]
            for k, v in (headers or {}).items():
                cmd += ["-H", f"{k}: {v}"]
            r = subprocess.run(cmd + ["--", url], capture_output=True, timeout=60)
            code = int(r.stdout.decode().strip() or 0)
            if r.returncode not in (0, 22):  # 22: HTTP error (--fail), its status is in `code`
                raise OSError(f"curl exit {r.returncode}")
            return CurlClient.Response(code, Path(f.name).read_bytes() if code == 200 else b"")

    def close(self) -> None:
        pass


def encode(content: bytes, kind: str) -> dict[str, Any]:
    """Decode, re-encode to a JPEG at most PHOTO_PX (drawings PLAN_PX) on the long side, never upscaled. Returns
    the JPEG bytes, their sha256, the pHash of the re-encoded image, and the original size. Raises on non-images."""
    import imagehash
    from PIL import Image, ImageOps

    if is_svg(content):  # vector drawings (Prometheus): rasterised, then stored like any other image
        content = rasterise_svg(content)
    im = Image.open(io.BytesIO(content))
    im.load()
    w, h = im.size
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P", "PA"):  # transparent PNG drawings would turn black
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    else:
        im = im.convert("RGB")
    px, q = (PLAN_PX, PLAN_Q) if kind == "plan" else (PHOTO_PX, PHOTO_Q)
    im.thumbnail((px, px), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=q, optimize=True)
    jpeg = buf.getvalue()
    return {"jpeg": jpeg, "sha256": hashlib.sha256(jpeg).hexdigest(), "phash": str(imagehash.phash(im)),
            "width": w, "height": h, "bytes": len(content)}


def media_path(sha: str, root: Path | None = None) -> Path:
    return (root or settings.MEDIA_DIR) / sha[:2] / f"{sha}.jpg"


# ---------------------------------------------------------------------------------------------------- state
def photo_jobs(conn: sqlite3.Connection, ranked: bool = True) -> list[dict[str, Any]]:
    """Every distinct image URL of every listing, once, in fetch priority order (`ranked`). `group` is the place
    (or the listing, before it has one): dedupe compares images within a group."""
    good: set[str] = set()
    if ranked:
        from .filter import evaluate_all

        for pres in evaluate_all(conn)["places"].values():
            good |= {pk for pk, r in pres.items() if r["status"] in ("pass", "needs_check")}
    jobs: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT source, source_id, place_key, url, active, data FROM listings"):
        group = r["place_key"] or f"{r['source']}|{r['source_id']}"
        n = 0
        for u, kind in listing_images(json.loads(r["data"])):
            rank = (not r["active"], group not in good, kind != "plan", n >= FIRST_N, n)
            n += kind == "photo"
            j = jobs.get(u)
            if j is None or rank < j["rank"]:
                jobs[u] = {"url": u, "kind": kind, "group": group, "page": r["url"], "rank": rank,
                           "groups": (j["groups"] if j else set()) | {group}}
            else:
                j["groups"].add(group)
    out = list(jobs.values())
    return sorted(out, key=lambda j: j["rank"]) if ranked else out


def url_files(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """url -> the stored file it is served from ({file, phash (the file's), width, height, kind}), for URLs
    fetched OK (the latest OK row of each URL). A URL whose latest row merged it into another file at more than
    PHOTO_MAX_DISTANCE bits is left out, so it is shown from its source and fetched again."""
    rows = [dict(r) for r in conn.execute(
        "SELECT url, kind, COALESCE(same_as, sha256) AS file, same_as, sha256, phash, distance, width, height "
        "FROM media WHERE status = 200 AND sha256 IS NOT NULL ORDER BY id")]
    file_phash = {r["sha256"]: r["phash"] for r in rows if r["same_as"] is None}
    latest = {r["url"]: r for r in rows}
    return {u: {"file": r["file"], "phash": file_phash.get(r["file"], r["phash"]), "kind": r["kind"],
                "width": r["width"], "height": r["height"]} for u, r in latest.items()
            if not (r["same_as"] and (r["distance"] or 0) > PHOTO_MAX_DISTANCE)}


def _failed(conn: sqlite3.Connection) -> dict[str, int]:
    """url -> last status of URLs never fetched OK."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT url, status FROM media m WHERE id = (SELECT MAX(id) FROM media WHERE url = m.url) "
        "AND NOT EXISTS (SELECT 1 FROM media x WHERE x.url = m.url AND x.status = 200 AND x.sha256 IS NOT NULL)")}


def stored_bytes(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(SUM(stored_bytes), 0) FROM media").fetchone()[0]


def max_bytes() -> int:
    from .config import load_media

    return int(float(load_media().get("max_gb", 5)) * 1e9)


def host(url: str) -> str:
    return urlparse(url).netloc


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts for the pipeline's Photos stage and `aptf-media status`."""
    jobs = photo_jobs(conn, ranked=False)
    files, failed = url_files(conn), _failed(conn)
    per_host: dict[str, Counter] = defaultdict(Counter)
    for j in jobs:
        u = j["url"]
        per_host[host(u)]["ok" if u in files else f"failed {failed[u]}" if u in failed else "to fetch"] += 1
    ok = [files[j["url"]] for j in jobs if j["url"] in files]
    last = conn.execute("SELECT counts, status, started_at FROM runs WHERE component = 'media' ORDER BY id DESC "
                        "LIMIT 1").fetchone()
    counts = json.loads(last["counts"]) if last and last["counts"] else {}
    return {"urls": len(jobs), "downloaded": len(ok), "files": len({f["file"] for f in ok}),
            "failed": sum(1 for j in jobs if j["url"] in failed and j["url"] not in files),
            "to_fetch": sum(1 for j in jobs if j["url"] not in files and j["url"] not in failed),
            "disk_bytes": stored_bytes(conn), "max_bytes": max_bytes(), "full": bool(counts.get("full")),
            "last_run": dict(last) if last else None,
            "hosts": {h: dict(c) for h, c in sorted(per_host.items(), key=lambda kv: -sum(kv[1].values()))}}


# ---------------------------------------------------------------------------------------------------- fetch
def _get(client: Any, url: str, referer: str | None, sem: threading.Semaphore,
         meter: dict[str, Any] | None) -> tuple[int, str | None, bytes]:
    """GET with retries on 429/5xx/network errors. `meter`: bytes through the residential proxy, capped."""
    err, code, body = None, 0, b""
    for attempt in range(4):
        if attempt:
            time.sleep(min(60, 2 ** attempt + random.random()))
        if meter is not None and meter["bytes"] >= meter["cap"]:
            return -1, "residential proxy cap reached", b""
        with sem:
            try:
                r = client.get(url, headers={"Referer": referer} if referer else {})
                code, err, body = r.status_code, None, r.content
            except Exception as e:  # network error: retried, then recorded as status 0
                code, err, body = 0, f"{type(e).__name__}: {e}"[:300], b""
            finally:
                time.sleep(DELAY_S)
        if meter is not None:
            with meter["lock"]:
                meter["bytes"] += len(body) + 500  # body plus about 500 bytes of headers
        if not (code == 429 or code >= 500 or code == 0):
            break
    return code, err, body


def _download(client: Any, job: dict[str, Any], sems: dict[str, threading.Semaphore],
              sem_lock: threading.Lock, given_up: set[str], meter: dict[str, Any] | None = None) -> dict[str, Any]:
    """One URL (its smaller variant first, if the host has one), then encode. Runs in a worker thread."""
    h = host(job["url"])
    if h in given_up:
        return {"skipped": True}
    with sem_lock:
        sem = sems.setdefault(h, threading.Semaphore(PER_HOST))
    referer = job["page"] if job["page"] and job["page"].startswith("http") else None
    code = 0
    if small := smaller_variant(job["url"]):
        code, err, body = _get(client, small, referer, sem, meter)
    if code != 200:
        code, err, body = _get(client, job["url"], referer, sem, meter)
    if code == -1:
        return {"skipped": True}
    if code != 200:
        return {"status": code, "error": err or f"HTTP {code}"}
    if len(body) > MAX_DOWNLOAD:
        return {"status": code, "error": f"too large ({len(body)} bytes)"}
    try:
        return {"status": 200, **encode(body, job["kind"])}
    except Exception as e:
        return {"status": 200, "error": f"not an image: {type(e).__name__}: {e}"[:300]}


def decide(res: dict[str, Any], kind: str, stored: dict[str, tuple[str, str]],
           root: Path | None = None) -> tuple[str | None, int | None]:
    """(same_as, distance) for a download: the stored file it duplicates (the same bytes anywhere, or the nearest
    same-kind picture of its place), else (None, None) and the caller writes it."""
    sha = res["sha256"]
    if media_path(sha, root).exists():
        return sha, 0
    if kind == "plan":  # drawings: exact copies only (two plans of one building can hash close)
        return None, None
    best: tuple[str | None, int | None] = (None, None)
    for fsha, (fph, fkind) in stored.items():
        d = same_picture(res["phash"], fph, kind) if fkind == kind else None
        if d is not None and (best[1] is None or d < best[1]):
            best = (fsha, d)
    return best


def write_file(res: dict[str, Any], root: Path | None = None) -> None:
    p = media_path(res["sha256"], root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(res["jpeg"])
    tmp.replace(p)


def fetch(conn: sqlite3.Connection, *, retry_failed: bool = False, limit: int | None = None,
          max_distance: int | None = None, root: Path | None = None, hosts: list[str] | None = None,
          log=print) -> dict[str, Any]:
    """Download every image URL not yet stored, in priority order; resumable (fetched URLs are skipped)."""
    import httpx

    from .store import finish_run, now_iso, start_run

    global PHOTO_MAX_DISTANCE
    if max_distance is not None:
        PHOTO_MAX_DISTANCE = max_distance
    root = root or settings.MEDIA_DIR
    jobs = photo_jobs(conn)
    files, failed = url_files(conn), _failed(conn)
    phash_of = {r[0]: r[1] for r in conn.execute(
        "SELECT sha256, phash FROM media WHERE status = 200 AND sha256 IS NOT NULL AND same_as IS NULL")}
    by_group: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)  # place -> stored file -> (phash, kind)
    for j in jobs:
        f = files.get(j["url"])
        if f and f["file"] in phash_of:
            for g in j["groups"]:
                by_group[g][f["file"]] = (phash_of[f["file"]], f["kind"])
    todo = [j for j in jobs if j["url"] not in files and (retry_failed or j["url"] not in failed)
            and (not hosts or host(j["url"]) in hosts)]
    if limit:
        todo = todo[:limit]
    cap, disk = max_bytes(), stored_bytes(conn)
    run = start_run(conn, "media", None, {"retry_failed": retry_failed, "limit": limit,
                                          "max_distance": PHOTO_MAX_DISTANCE, "todo": len(todo)})
    log(f"media: {len(jobs)} image URLs, {len(files)} stored, {len(failed)} failed before; fetching {len(todo)}; "
        f"store {disk / 1e9:.2f} of {cap / 1e9:.1f} GB; near-duplicate photos at <= {PHOTO_MAX_DISTANCE} bits")
    n: Counter = Counter()
    per_host: dict[str, Counter] = defaultdict(Counter)
    streak: Counter = Counter()  # consecutive 401/403 per host
    given_up: set[str] = set()
    sems: dict[str, threading.Semaphore] = {}
    sem_lock = threading.Lock()
    t0, full = time.monotonic(), False
    from .config import load_media

    hdrs = {"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"}
    client = httpx.Client(headers=hdrs, timeout=30, follow_redirects=True)
    res_hosts = set(load_media().get("residential_hosts", []))
    curl_hosts = set(load_media().get("curl_hosts", []))
    curl = CurlClient()
    meter: dict[str, Any] = {"bytes": 0, "cap": float(load_media().get("residential_max_mb", 300)) * 1e6,
                             "lock": threading.Lock()}
    proxied, secret = None, None
    if any(host(j["url"]) in res_hosts for j in todo):
        proxy_url, secret = residential_proxy()
        proxied = httpx.Client(headers=hdrs, timeout=60, follow_redirects=True, proxy=proxy_url)
        log(f"media: {sum(host(j['url']) in res_hosts for j in todo)} URLs on {sorted(res_hosts)} go through "
            f"Apify's residential proxy (cap {meter['cap'] / 1e6:.0f} MB, about ${PROXY_USD_PER_GB:.0f}/GB)")
    pool = ThreadPoolExecutor(WORKERS)
    try:
        futs = {pool.submit(_download, *((proxied, j, sems, sem_lock, given_up, meter) if host(j["url"]) in res_hosts
                                         else (curl if host(j["url"]) in curl_hosts else client, j, sems, sem_lock,
                                               given_up))): j for j in todo}
        for fut in as_completed(futs):
            j, res = futs[fut], fut.result()
            h = host(j["url"])
            if res.get("skipped"):
                n["skipped"] += 1
                continue
            err = res.get("error")
            if err and secret:
                err = err.replace(secret, "***")
            row: dict[str, Any] = {"status": res["status"], "error": err}
            if "sha256" in res:
                same, dist = decide(res, j["kind"], by_group[j["group"]], root)
                if not same and disk + len(res["jpeg"]) > cap:
                    full = True
                    log(f"media store full ({cap / 1e9:.1f} GB): stopping")
                    break
                if not same:
                    write_file(res, root)
                    disk += len(res["jpeg"])
                    for g in j["groups"]:
                        by_group[g][res["sha256"]] = (res["phash"], j["kind"])
                row.update({k: res[k] for k in ("sha256", "phash", "width", "height", "bytes")},
                           same_as=same, distance=dist, stored_bytes=None if same else len(res["jpeg"]))
                n["written" if not same else "exact" if same == res["sha256"] else "near"] += 1
                per_host[h]["ok"] += 1
                streak[h] = 0
            else:
                n["failed"] += 1
                per_host[h][f"failed {res['status']}"] += 1
                if res["status"] in (401, 403):
                    streak[h] += 1
                    if streak[h] >= HOST_GIVE_UP and h not in given_up:
                        given_up.add(h)
                        log(f"media: {h} refused {HOST_GIVE_UP} in a row (HTTP {res['status']}): skipping its "
                            f"other URLs this run")
            conn.execute(
                "INSERT INTO media (fetched_at, url, kind, status, error, sha256, phash, same_as, distance, width, "
                "height, bytes, stored_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now_iso(), j["url"], j["kind"], row["status"], row["error"], row.get("sha256"), row.get("phash"),
                 row.get("same_as"), row.get("distance"), row.get("width"), row.get("height"), row.get("bytes"),
                 row.get("stored_bytes")))
            conn.commit()  # per row: never hold the write lock while waiting for the next download
            done = sum(n.values()) - n["skipped"]
            if done % 500 == 0:
                rate = done / (time.monotonic() - t0)
                top = sorted(per_host.items(), key=lambda kv: -sum(kv[1].values()))[:6]
                log(f"media: {done}/{len(todo)} ({rate:.1f}/s, ETA {(len(todo) - done) / rate / 60:.0f} min) "
                    f"{dict(n)} store {disk / 1e9:.2f} GB; " + "; ".join(f"{k}: {dict(v)}" for k, v in top))
        conn.commit()
    except BaseException as e:
        conn.commit()
        pool.shutdown(wait=False, cancel_futures=True)
        finish_run(conn, run, "aborted", {**n, "full": full, "disk_bytes": disk}, error=repr(e)[:300])
        raise
    pool.shutdown(wait=False, cancel_futures=True)
    client.close()
    if proxied:
        proxied.close()
        n["proxy_bytes"] = meter["bytes"]
        log(f"media: residential proxy {meter['bytes'] / 1e6:.1f} MB, about "
            f"${meter['bytes'] / 1e9 * PROXY_USD_PER_GB:.2f}" + (" (cap reached)" if meter["bytes"] >= meter["cap"] else ""))
    counts = {**n, "full": full, "disk_bytes": disk, "hosts": {k: dict(v) for k, v in per_host.items()},
              "given_up": sorted(given_up)}
    finish_run(conn, run, "ok", counts, est_cost=0.0)
    log(f"media: done {dict(n)}; store {disk / 1e9:.2f} GB" + (f"; media store full ({cap / 1e9:.1f} GB)" if full else ""))
    return counts


def main(argv: list[str] | None = None) -> None:
    from .store import connect, init_db

    ap = argparse.ArgumentParser(prog="aptf-media", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--db", default=str(settings.DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download every image URL not stored yet (resumable)")
    f.add_argument("--retry-failed", action="store_true", help="also retry URLs that failed before")
    f.add_argument("--limit", type=int, help="at most this many URLs (a pilot)")
    f.add_argument("--host", action="append", help="only URLs on this host (repeatable)")
    f.add_argument("--max-distance", type=int, help=f"pHash bits for near-duplicate photos (default "
                                                    f"{PHOTO_MAX_DISTANCE}; 0 = exact copies only)")
    sub.add_parser("status", help="downloaded / failed / to fetch, per host")
    args = ap.parse_args(argv)
    conn = connect(args.db)
    init_db(conn)
    if args.cmd == "fetch":
        fetch(conn, retry_failed=args.retry_failed, limit=args.limit, max_distance=args.max_distance, hosts=args.host,
              log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    else:
        s = status(conn)
        print(f"{s['urls']} image URLs: {s['downloaded']} downloaded ({s['files']} files), {s['failed']} failed, "
              f"{s['to_fetch']} to fetch; store {s['disk_bytes'] / 1e9:.2f} of {s['max_bytes'] / 1e9:.1f} GB"
              + ("; media store full" if s["full"] else ""))
        for h, c in s["hosts"].items():
            print(f"  {h}: {c}")


if __name__ == "__main__":
    main()
