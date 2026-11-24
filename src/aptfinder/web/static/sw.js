/* Service worker.
   - Map tiles and CDN assets: cache-first (the area is small; after one pass the map works offline).
   - Same-origin GETs (app shell, media, API): network-first with cache fallback, so a reload with the
     network off still shows the last data, and code changes show up immediately when online.
   Writes (POST) always go to the network. */
const TILE_CACHE = "tiles-v1";
const APP_CACHE = "app-v1";
const TILE_HOSTS = ["server.arcgisonline.com", "tile.openstreetmap.org", "cdnjs.cloudflare.com"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(APP_CACHE).then((c) => c.addAll(["/", "/static/app.js", "/static/style.css"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  if (TILE_HOSTS.includes(url.hostname)) {
    e.respondWith(caches.open(TILE_CACHE).then(async (cache) => {
      const hit = await cache.match(req);
      if (hit) return hit;
      const resp = await fetch(req);
      if (resp.ok || resp.type === "opaque") cache.put(req, resp.clone());
      return resp;
    }));
    return;
  }
  if (url.origin !== self.location.origin) return;

  // same origin (app shell, media, API): network-first, cache as the offline fallback
  e.respondWith(caches.open(APP_CACHE).then(async (cache) => {
    try {
      const resp = await fetch(req);
      if (resp.ok) cache.put(req, resp.clone());
      return resp;
    } catch (err) {
      const hit = await cache.match(req);
      if (hit) return hit;
      throw err;
    }
  }));
});
