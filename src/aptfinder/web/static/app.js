/* Apartment finder dashboard. Plain JS + Leaflet, no build step.
   All scraped text is untrusted: everything rendered goes through esc(). */
"use strict";

const S = {
  meta: null, places: [], points: {}, goal: "all", base: "sat", markers: {}, layers: {},
  selected: null, verifySkip: 0, verifySeed: 0,
};
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v == null ? "?" : "$" + Math.round(v).toLocaleString());
const num = (v) => (v == null ? "?" : (+v).toLocaleString(undefined, { maximumFractionDigits: 1 }));
const RANK = { pass: 0, needs_check: 1, fail: 2 };
const STATUS_LABEL = { pass: "pass", needs_check: "needs check", fail: "fail" };
const MODE_LABEL = { walk: "walk", bike: "bike", transit: "transit", drive: "drive", bike_train_bike: "bike+train", bike_bus_bike: "bike+bus" };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

function toast(msg, ms = 2500) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

/* ---------------------------------------------------------------- status of a place for the current goal */
function placeStatus(p, goal = S.goal) {
  if (goal !== "all") return p.goals[goal]?.status || "needs_check";
  const sts = Object.values(p.goals).map((g) => g.status);
  return sts.sort((a, b) => RANK[a] - RANK[b])[0] || "needs_check";
}
function minutesFor(p) {
  const v = p.minutes[`${$("#target").value}|${$("#mode").value}`];
  return v ? v : null;
}
const COLORS = () => {
  const cs = getComputedStyle(document.documentElement);
  return { pass: cs.getPropertyValue("--pass").trim(), needs_check: cs.getPropertyValue("--check").trim(), fail: cs.getPropertyValue("--fail").trim() };
};

/* ---------------------------------------------------------------- map */
let map;
function initMap() {
  map = L.map("map", { zoomControl: true }).setView([37.43, -122.12], 12);
  const esri = "https://server.arcgisonline.com/ArcGIS/rest/services";
  S.layers.sat = L.layerGroup([
    L.tileLayer(`${esri}/World_Imagery/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" }),
    L.tileLayer(`${esri}/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 19, opacity: 0.8 }),
    L.tileLayer(`${esri}/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 19 }),
  ]);
  S.layers.osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap contributors" });
  S.layers.sat.addTo(map);
  S.placeLayer = L.layerGroup().addTo(map);

  const legend = L.control({ position: "bottomleft" });
  legend.onAdd = () => {
    const d = L.DomUtil.create("div", "legend");
    const c = COLORS();
    d.innerHTML = `<i style="background:${c.pass}"></i>pass<br><i style="background:${c.needs_check}"></i>needs check<br><i style="background:${c.fail};opacity:.5"></i>fail`;
    return d;
  };
  legend.addTo(map);
}

function setBase(base) {
  S.base = base;
  map.removeLayer(S.layers.sat); map.removeLayer(S.layers.osm);
  S.layers[base].addTo(map);
  $$("#base-switch button").forEach((b) => b.classList.toggle("on", b.dataset.base === base));
}

function buildPointLayers() {
  const overlays = {};
  const style = {
    commute_targets: { color: "#c026d3", radius: 8 }, caltrain_stations: { color: "#dc2626", radius: 7 },
    grocery_chains: { color: "#0891b2", radius: 5 }, grocery_any: { color: "#0e7490", radius: 3 },
    google_bus_stops: { color: "#2563eb", radius: 5 },
  };
  for (const [set, pts] of Object.entries(S.points)) {
    if (!pts.length) continue;
    const st = style[set] || { color: "#555", radius: 4 };
    const g = L.layerGroup(pts.map((p) => {
      let html = `<b>${esc(p.name)}</b>`;
      if (set === "caltrain_stations") html += `<br>service: ${esc(p.service)}<br><span class="small">${esc(p.other_transit || "")}</span>`;
      else if (p.address) html += `<br>${esc(p.address)}`;
      return L.circleMarker([p.lat, p.lon], { radius: st.radius, color: "#fff", weight: 1.5, fillColor: st.color, fillOpacity: 0.95 })
        .bindTooltip(html);
    }));
    const label = S.meta.point_sets[set]?.label || set;
    overlays[label] = g;
    if (set === "commute_targets" || set === "caltrain_stations") g.addTo(map);
  }
  L.control.layers(null, overlays, { collapsed: true, position: "topright" }).addTo(map);
}

function drawMarkers(list) {
  S.placeLayer.clearLayers();
  S.markers = {};
  const c = COLORS();
  for (const p of list) {
    if (p.lat == null) continue;
    const st = placeStatus(p);
    const m = L.circleMarker([p.lat, p.lon], {
      radius: st === "fail" ? 6 : 9, color: "#fff", weight: 2, fillColor: c[st],
      fillOpacity: st === "fail" ? 0.45 : 0.95, opacity: st === "fail" ? 0.5 : 1,
    }).bindTooltip(`${esc(p.label)}<br>${esc(STATUS_LABEL[st])}`);
    m.on("click", () => openPlace(p.place_key));
    m.addTo(S.placeLayer);
    S.markers[p.place_key] = m;
  }
}

/* ---------------------------------------------------------------- list */
function filtered() {
  const q = $("#q").value.trim().toLowerCase();
  const sts = new Set($$(".st").filter((c) => c.checked).map((c) => c.value));
  const minBeds = parseFloat($("#minbeds").value);
  const maxPpb = parseFloat($("#maxppb").value);
  const showInactive = $("#inactive").checked;
  return S.places.filter((p) => {
    if (!sts.has(placeStatus(p))) return false;
    if (!showInactive && !p.n_active) return false;
    if (!isNaN(minBeds) && !(p.max_beds >= minBeds)) return false;
    if (!isNaN(maxPpb) && p.min_price_per_bed != null && p.min_price_per_bed > maxPpb) return false;
    if (q) {
      const hay = [p.label, p.address, p.city, p.sources, p.title, p.operator].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function sorted(list) {
  const key = $("#sort").value;
  const nil = (v) => (v == null ? Infinity : v);
  const fns = {
    status: (p) => [RANK[placeStatus(p)], -(p.goals[S.goal]?.nice_score || 0), nil(p.min_price_per_bed)],
    price: (p) => [nil(p.min_price_month)], ppb: (p) => [nil(p.min_price_per_bed)],
    beds: (p) => [-(p.max_beds || 0)], minutes: (p) => [nil(minutesFor(p)?.minutes)],
    rating: (p) => [-(p.rating || 0)], nice: (p) => [-(Math.max(0, ...Object.values(p.goals).map((g) => g.nice_score || 0)))],
  };
  const f = fns[key];
  return [...list].sort((a, b) => {
    const ka = f(a), kb = f(b);
    for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] < kb[i] ? -1 : 1;
    return 0;
  });
}

function stars(n) { return n ? `<span class="stars" title="${n}/5">${"★".repeat(n)}${"☆".repeat(5 - n)}</span>` : ""; }

function renderList() {
  const list = sorted(filtered());
  drawMarkers(list);
  $("#count").textContent = `${list.length} of ${S.places.length} places`;
  const tgt = $("#target").value.split(":")[1] || "";
  const mode = MODE_LABEL[$("#mode").value];
  $("#cards").innerHTML = list.map((p) => {
    const st = placeStatus(p);
    const mins = minutesFor(p);
    const chips = Object.entries(p.goals).map(([g, r]) => `<span class="chip ${r.status}" title="${esc(r.reasons.join("\n"))}">${esc(g)}: ${esc(STATUS_LABEL[r.status])}</span>`).join("");
    const bb = p.beds.length ? `${p.beds.map((b) => (b === 0 ? "studio" : b + "bd")).join("/")} · ${p.baths.join("/")}ba` : "beds ?";
    return `<div class="card ${st === "fail" ? "dim" : ""} ${S.selected === p.place_key ? "sel" : ""}" data-pk="${esc(p.place_key)}">
      <h4>${esc(p.label)} ${stars(p.rating)}</h4>
      <div class="facts">
        <span><b>${money(p.min_price_month)}</b>/mo</span><span><b>${money(p.min_price_per_bed)}</b>/bed</span>
        <span>${esc(bb)}</span>
        <span>${mins ? `<b>${num(mins.minutes)}</b> min ${esc(mode)} to ${esc(tgt)}${mins.provider === "estimate" ? " (est.)" : ""}` : `${esc(mode)} to ${esc(tgt)}: not computed`}</span>
        <span>${esc(p.sources)}</span>${p.n_active ? "" : '<span style="color:var(--bad)">inactive</span>'}
        ${p.user_status ? `<span>status: ${esc(p.user_status)}</span>` : ""}
      </div>
      <div class="chips">${chips}</div>
    </div>`;
  }).join("");
  $$(".card").forEach((el) => el.addEventListener("click", () => openPlace(el.dataset.pk)));
}

/* ---------------------------------------------------------------- drawer */
function ck(verdict, overridden) {
  if (overridden) return `<span class="ck wrong" title="corrected by hand check">✎</span>`;
  if (!verdict) return `<span class="ck none" title="not hand-checked">·</span>`;
  const sym = { correct: "✓", wrong: "✗", cannot_tell: "?" }[verdict];
  return `<span class="ck ${verdict}" title="hand check: ${verdict}">${sym}</span>`;
}

function fmtField(f, v) {
  if (v == null) return '<span class="muted">?</span>';
  if (f.startsWith("price")) return typeof v === "number" ? money(v) : esc(v);
  if (f === "amenities") { try { return esc(JSON.parse(v).join(", ")); } catch (_) { return esc(v); } }
  if (v === 1 && ["furnished", "private_bath"].includes(f)) return "yes";
  if (v === 0 && ["furnished", "private_bath"].includes(f)) return "no";
  return esc(typeof v === "number" ? num(v) : v);
}

function historyChart(listings) {
  const series = listings.map((l) => ({
    name: l.source,
    pts: l.history.filter((h) => h.price != null).map((h) => ({ t: Date.parse(h.observed_at), v: monthly(h) })).filter((p) => p.v != null),
  })).filter((s) => s.pts.length);
  if (!series.length) return '<p class="muted small">No price history.</p>';
  const all = series.flatMap((s) => s.pts);
  let t0 = Math.min(...all.map((p) => p.t)), t1 = Math.max(...all.map((p) => p.t));
  let v0 = Math.min(...all.map((p) => p.v)), v1 = Math.max(...all.map((p) => p.v));
  if (t0 === t1) { t0 -= 864e5; t1 += 864e5; }
  const pad = (v1 - v0) * 0.15 || v0 * 0.05 || 100; v0 -= pad; v1 += pad;
  const W = 640, H = 160, L0 = 52, B = 22;
  const x = (t) => L0 + ((t - t0) / (t1 - t0)) * (W - L0 - 10);
  const y = (v) => 8 + (1 - (v - v0) / (v1 - v0)) * (H - B - 8);
  const palette = ["#2457c5", "#c026d3", "#0891b2", "#e0a100", "#1f9d55", "#c53030"];
  const lines = series.map((s, i) => {
    const c = palette[i % palette.length];
    const d = s.pts.map((p, j) => `${j ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
    const dots = s.pts.map((p) => `<circle cx="${x(p.t).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="3" fill="${c}"><title>${esc(s.name)} ${new Date(p.t).toLocaleDateString()}: ${money(p.v)}/mo</title></circle>`).join("");
    return `<path d="${d}" fill="none" stroke="${c}" stroke-width="2"/>${dots}<text x="${W - 8}" y="${14 + i * 12}" text-anchor="end" style="fill:${c}">${esc(s.name)}</text>`;
  }).join("");
  const ticks = [v0 + pad, (v0 + v1) / 2, v1 - pad].map((v) => `<text x="${L0 - 6}" y="${y(v) + 3}" text-anchor="end">${money(v)}</text><line x1="${L0}" x2="${W - 10}" y1="${y(v)}" y2="${y(v)}" stroke="currentColor" opacity=".08"/>`).join("");
  const dates = [t0, t1].map((t, i) => `<text x="${x(t)}" y="${H - 6}" text-anchor="${i ? "end" : "start"}">${new Date(t).toLocaleDateString()}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Price history">${ticks}${dates}${lines}</svg>`;
}
function monthly(h) {
  const p = h.price;
  if (h.price_basis === "month") return p;
  if (h.price_basis === "night") return p * 30.4375;
  if (h.price_basis === "week") return (p * 30.4375) / 7;
  if (h.price_basis === "stay" && h.query_checkin && h.query_checkout) {
    const n = (Date.parse(h.query_checkout) - Date.parse(h.query_checkin)) / 864e5;
    return n > 0 ? (p * 30.4375) / n : null;
  }
  return null;
}

function matrixTable(d) {
  const targets = S.meta.targets;
  const modes = S.meta.modes;
  const rows = targets.map((t) => {
    const cells = modes.map((m) => {
      const v = d.matrix[`${t}|${m}`];
      if (!v || v.minutes == null) return `<td class="muted">–</td>`;
      const legs = (v.legs || []).map((l) => `${l.mode}${l.from || l.to ? ` ${l.from || ""}${l.to ? "→" + l.to : ""}` : ""}: ${num(l.minutes)}m`).join("\n");
      const est = v.provider === "estimate";
      return `<td title="${esc(legs)}\nprovider: ${esc(v.provider)}">${est ? "~" : ""}${num(v.minutes)}</td>`;
    }).join("");
    return `<tr><th>${esc(t.split(":")[1])}</th>${cells}</tr>`;
  }).join("");
  return `<div class="scroll-x"><table><tr><th></th>${modes.map((m) => `<th>${esc(MODE_LABEL[m])}</th>`).join("")}</tr>${rows}</table></div>
    <p class="muted small">Minutes. ~ = straight-line estimate (not routed). Hover a cell for legs.</p>`;
}

function nearestBlock(d) {
  const entries = Object.entries(d.nearest);
  if (!entries.length) return '<p class="muted small">Not computed.</p>';
  return entries.map(([k, list]) => {
    const [set, mode] = k.split("|");
    const label = S.meta.point_sets[set]?.label || set;
    return `<div class="small"><b>${esc(label)}</b> (${esc(mode)}): ${list.map((x) => `${esc(x.point)} ${x.provider === "estimate" ? "~" : ""}${num(x.minutes)} min`).join(", ")}</div>`;
  }).join("");
}

function attributesBlock(d, pk) {
  const byAttr = {};
  for (const a of d.annotations) (byAttr[a.attribute] ||= []).push(a);
  const attrs = [...new Set([...S.meta.attributes, ...Object.keys(byAttr)])];
  return attrs.map((attr) => {
    const log = byAttr[attr] || [];
    const lastRating = [...log].reverse().find((a) => a.rating != null)?.rating || 0;
    const btns = [1, 2, 3, 4, 5].map((n) => `<button data-rate="${n}" class="${n <= lastRating ? "on" : ""}" aria-label="${n} of 5">★</button>`).join("");
    const lines = log.slice(-4).map((a) => `<div>${esc(new Date(a.ts).toLocaleString())}: ${a.rating ? a.rating + "/5 " : ""}${esc(a.comment || "")}</div>`).join("");
    return `<div class="attr" data-attr="${esc(attr)}">
      <div class="head"><b>${esc(attr.replaceAll("_", " "))}</b><span class="rate">${btns}</span></div>
      <div class="log">${lines}</div>
      <form><input name="c" placeholder="comment on ${esc(attr.replaceAll("_", " "))}" autocomplete="off"><button>Add</button></form>
    </div>`;
  }).join("") + `<form id="new-attr" class="row small" style="display:flex;gap:6px;margin-top:6px"><input name="a" placeholder="new attribute (e.g. parking)" style="flex:1"><button>Add attribute</button></form>`;
}

async function openPlace(pk) {
  S.selected = pk;
  $$(".card").forEach((c) => c.classList.toggle("sel", c.dataset.pk === pk));
  const m = S.markers[pk];
  if (m) map.panTo(m.getLatLng());
  const d = await api(`/api/places/${encodeURIComponent(pk)}`);
  const p = d.place;
  const label = p.property_name || p.address || d.listings.find((l) => l.fields.title)?.fields.title || pk;
  const goals = Object.entries(d.goals).filter(([, r]) => r).map(([g, r]) => `
    <div><span class="chip ${r.status}">${esc(g)}: ${esc(STATUS_LABEL[r.status])}</span>
      ${r.reasons.map(([s, t]) => `<div class="reason ${s}">${s === "pass" ? "✓" : s === "fail" ? "✗" : "?"} ${esc(t)}</div>`).join("")}
      ${r.nice.length ? `<div class="small muted">nice to have: ${r.nice.map(([s, t]) => `${s === "pass" ? "✓" : s === "fail" ? "✗" : "?"} ${esc(t)}`).join(" · ")}</div>` : ""}
    </div>`).join("");
  const media = d.listings.flatMap((l) => l.media.map((f) => `<a href="/media/${encodeURIComponent(f)}" target="_blank" rel="noopener"><img src="/media/${encodeURIComponent(f)}" alt="photo or floor plan from ${esc(l.source)}"></a>`));
  const fields = ["price", "price_month", "price_per_bed", "beds", "baths", "sqft", "kind", "term", "furnished", "private_bath", "available_from", "available_to", "unit", "amenities"];
  const listingTable = `<div class="scroll-x"><table>
    <tr><th>field</th>${d.listings.map((l) => `<th><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.source)}</a><br><span class="muted">${l.active ? "active" : "inactive"} · ${esc((l.first_seen || "").slice(0, 10))} → ${esc((l.last_seen || "").slice(0, 10))}</span></th>`).join("")}</tr>
    ${fields.map((f) => `<tr><th>${esc(f)}</th>${d.listings.map((l) => {
      const cf = f === "price_month" || f === "price_per_bed" ? "price" : f;
      return `<td>${fmtField(f, l.fields[f])}${f === "price" && l.fields.price_basis ? ` <span class="muted">/${esc(l.fields.price_basis)}</span>` : ""}${["price_month", "price_per_bed"].includes(f) ? "" : ck(l.checks[cf], l.overridden.includes(cf))}</td>`;
    }).join("")}</tr>`).join("")}
    <tr><th>listing</th>${d.listings.map((l) => `<td><button class="small" data-split="${esc(l.source)}|${esc(l.source_id)}" title="Record that this listing is a different place">split off</button></td>`).join("")}</tr>
  </table></div>
  <p class="muted small">✓ checked correct · ✗ checked wrong · ✎ corrected value shown · · not hand-checked. Source shown in each column header.</p>`;
  $("#drawer-body").innerHTML = `
    <h2>${esc(label)} ${stars(d.annotations.filter((a) => a.attribute === "overall" && a.rating).slice(-1)[0]?.rating)}</h2>
    <div class="muted small">${esc(p.address || "address unknown")} ${p.city ? "· " + esc(p.city) : ""} · ${esc(pk)}</div>
    <h3>Goals</h3>${goals}
    ${media.length ? `<h3>Photos & floor plans</h3><div class="media">${media.join("")}</div>` : ""}
    <h3>Listings by source</h3>${listingTable}
    <h3>Price history (per month)</h3>${historyChart(d.listings)}
    <h3>Commute <button id="route-btn" class="small">${S.meta.google ? "Route with Google" : "Recompute (estimate)"}</button></h3>${matrixTable(d)}
    <h3>Nearby</h3>${nearestBlock(d)}
    <h3>Your notes & ratings</h3>${attributesBlock(d, pk)}
    <h3>Raw data</h3>${d.listings.map((l) => `<details><summary>${esc(l.source)} ${esc(l.source_id)} (latest observation, ${l.n_obs} seen)</summary><pre class="raw">${esc(JSON.stringify(l.data, null, 2))}</pre>${l.raw ? `<pre class="raw">${esc(l.raw)}</pre>` : ""}</details>`).join("")}
  `;
  $("#drawer").classList.remove("hidden");
  wireDrawer(pk);
}

function wireDrawer(pk) {
  $$("#drawer .attr").forEach((box) => {
    const attr = box.dataset.attr;
    $$(".rate button", box).forEach((b) => b.addEventListener("click", async () => {
      await post("/api/annotations", { entity_key: pk, attribute: attr, rating: +b.dataset.rate, goal_id: S.goal === "all" ? null : S.goal });
      await refreshAfterWrite(pk); toast(`Rated ${attr} ${b.dataset.rate}/5`);
    }));
    $("form", box).addEventListener("submit", async (e) => {
      e.preventDefault();
      const c = e.target.c.value.trim();
      if (!c) return;
      await post("/api/annotations", { entity_key: pk, attribute: attr, comment: c, goal_id: S.goal === "all" ? null : S.goal });
      await refreshAfterWrite(pk); toast("Comment saved");
    });
  });
  $("#new-attr")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const a = e.target.a.value.trim().toLowerCase().replace(/\s+/g, "_");
    if (!a) return;
    S.meta.attributes.push(a);
    openPlace(pk);
  });
  $$("#drawer [data-split]").forEach((b) => b.addEventListener("click", async () => {
    const [source, source_id] = b.dataset.split.split("|");
    if (!confirm(`Record that ${source} ${source_id} is a different place from the others here?`)) return;
    const r = await post("/api/links", { source, source_id, place_key: null });
    toast("Split recorded"); await loadPlaces(); openPlace(r.place_key);
  }));
  $("#route-btn")?.addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Routing…";
    try {
      const r = await post(`/api/trips/${encodeURIComponent(pk)}`);
      toast(`${r.new_trips} new trips (${r.provider}, ${r.api_calls} API calls)`);
      await refreshAfterWrite(pk);
    } catch (err) { toast("Routing failed: " + err.message, 6000); e.target.disabled = false; e.target.textContent = "Retry routing"; }
  });
}

async function refreshAfterWrite(pk) {
  await loadPlaces();
  if (pk) await openPlace(pk);
}

/* ---------------------------------------------------------------- verify mode */
async function openVerify() {
  $("#verify").classList.remove("hidden");
  await renderVerify();
}

async function renderVerify() {
  const body = $("#verify-body");
  body.innerHTML = '<p class="muted">Loading…</p>';
  const d = await api(`/api/verify/next?seed=${S.verifySeed}&skip=${S.verifySkip}`);
  if (d.done) { body.innerHTML = "<h2>Queue empty</h2><p>Nothing left to check. 🎉</p>"; return; }
  const it = d.item;
  const head = `<p class="muted small">${d.remaining} items in queue (conflicts first, then an even sample across sources) · item ${S.verifySkip + 1}</p>`;
  const nav = `<p style="margin-top:14px"><button id="v-skip">Skip</button> <button id="v-next" class="accent">Next</button></p>`;
  if (it.kind === "trip") {
    const t = d.trip;
    const tm = { walk: "walking", bike: "bicycling", transit: "transit", drive: "driving", bike_train_bike: "transit", bike_bus_bike: "transit" }[t.mode];
    const gm = t.origin && t.dest ? `https://www.google.com/maps/dir/?api=1&origin=${t.origin.lat},${t.origin.lon}&destination=${t.dest.lat},${t.dest.lon}&travelmode=${tm}` : null;
    body.innerHTML = `${head}<h2>Check a computed trip</h2>
      <p>${esc(t.origin?.address || t.place_key)} → <b>${esc(t.dest?.name || t.point_id)}</b> by <b>${esc(MODE_LABEL[t.mode] || t.mode)}</b>: stored <b>${num(t.minutes)} min</b> (provider ${esc(t.provider)}).</p>
      ${gm ? `<p><a href="${esc(gm)}" target="_blank" rel="noopener noreferrer">Open in Google Maps ↗</a></p>` : ""}
      <pre class="raw">${esc(JSON.stringify(t.legs, null, 2))}</pre>
      <table><tr><th>minutes</th><td>${num(t.minutes)}</td><td>${vbtns({ trip_id: t.id, field: "minutes", queue: "sample" })}</td></tr></table>${nav}`;
  } else {
    const obs = d.observations;
    const title = it.kind === "conflict" ? `Sources disagree on ${d.fields.join(", ")}` : `Sample check: ${esc(obs[0].source)}`;
    body.innerHTML = `${head}<h2>${esc(title)}</h2>
      <p>${esc(d.place?.property_name || "")} ${esc(d.place?.address || it.place_key || "")}</p>
      <p class="small">Open each listing, then mark every stored value: ✓ correct, ✗ wrong (type the true value), ? cannot tell.</p>
      <div class="scroll-x"><table><tr><th>field</th>${obs.map((o) => `<th><a href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">${esc(o.source)} ↗</a><br><span class="muted">${esc(o.observed_at.slice(0, 10))}</span></th>`).join("")}</tr>
      ${d.fields.map((f) => `<tr><th>${esc(f)}</th>${obs.map((o) => `<td>${fmtField(f, o.fields[f])}<br>${vbtns({ observation_id: o.obs_id, field: f, queue: it.kind === "conflict" ? "conflict" : "sample" })}</td>`).join("")}</tr>`).join("")}
      </table></div>${nav}`;
  }
  $$(".vbtns", body).forEach(wireVbtns);
  $("#v-skip").addEventListener("click", () => { S.verifySkip++; renderVerify(); });
  $("#v-next").addEventListener("click", () => { renderVerify(); loadStatus(); });
}

function vbtns(ctx) {
  return `<span class="vbtns" data-ctx='${esc(JSON.stringify(ctx))}'>
    <button data-v="correct" class="correct" title="correct">✓</button>
    <button data-v="wrong" class="wrong" title="wrong">✗</button>
    <button data-v="cannot_tell" class="cannot_tell" title="cannot tell">?</button>
    <input class="hidden" placeholder="true value"><button class="hidden" data-save>save</button></span>`;
}

function wireVbtns(box) {
  const ctx = JSON.parse(box.dataset.ctx);
  const input = $("input", box), save = $("[data-save]", box);
  const send = async (verdict, true_value) => {
    try {
      await post("/api/checks", { ...ctx, verdict, true_value });
      $$("button[data-v]", box).forEach((b) => b.classList.toggle("on", b.dataset.v === verdict));
      input.classList.add("hidden"); save.classList.add("hidden");
      toast(`Recorded ${verdict.replace("_", " ")} for ${ctx.field}`);
    } catch (e) { toast("Not saved: " + e.message, 5000); }
  };
  $$("button[data-v]", box).forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.v === "wrong") { input.classList.remove("hidden"); save.classList.remove("hidden"); input.focus(); }
    else send(b.dataset.v);
  }));
  save.addEventListener("click", () => send("wrong", input.value));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send("wrong", input.value); });
}

/* ---------------------------------------------------------------- status strip */
async function loadStatus() {
  const s = await api("/api/status");
  const intake = s.runs.filter((r) => r.component === "intake");
  const parts = [`<span>${s.counts.places} places · ${s.counts.active}/${s.counts.listings} listings active</span>`];
  for (const r of intake) {
    const c = r.counts || {};
    parts.push(`<span class="${r.status === "error" ? "err" : ""}" title="${esc(JSON.stringify(c))}">${esc(r.source)}: ${esc((r.finished_at || r.started_at || "").slice(0, 16).replace("T", " "))} ${esc(r.status)}${c.observations != null ? ` · ${c.observations} seen` : ""}${c.new != null ? ` · ${c.new} new` : ""}${c.changed != null ? ` · ${c.changed} changed` : ""}</span>`);
  }
  parts.push(`<span>est. spend $${(+s.spend).toFixed(2)}</span>`);
  if (s.errors.length) parts.push(`<span class="err" title="${esc(s.errors.map((e) => `${e.component}/${e.source}: ${e.error}`).join("\n"))}">${s.errors.length} recent errors</span>`);
  const accBySource = {};
  for (const a of s.accuracy) (accBySource[a.source] ||= []).push(`${a.field} ${a.correct}/${a.n}`);
  for (const [src, list] of Object.entries(accBySource)) parts.push(`<span title="hand-checked accuracy">✓ ${esc(src)}: ${esc(list.join(", "))}</span>`);
  if (!s.accuracy.length) parts.push(`<span>no hand checks yet</span>`);
  $("#status-strip").innerHTML = parts.join("");
  $("#queue-count").textContent = `(${s.queues.conflict} conflicts · ${s.queues.sample})`;
}

/* ---------------------------------------------------------------- boot */
async function loadPlaces() {
  S.places = await api("/api/places");
  renderList();
}

async function boot() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  S.meta = await api("/api/meta");
  $("#dbname").textContent = S.meta.db;
  const gs = $("#goal-switch");
  gs.innerHTML = [`<button data-goal="all" class="on">All</button>`, ...S.meta.goals.map((g) => `<button data-goal="${esc(g.id)}" title="${esc(g.label)}">${esc(g.id)}</button>`)].join("");
  $$("button", gs).forEach((b) => b.addEventListener("click", () => {
    S.goal = b.dataset.goal;
    $$("button", gs).forEach((x) => x.classList.toggle("on", x === b));
    if (S.goal === "temp") $("#sort").value = "price"; // temp: cheaper is better within the ~$4k budget
    renderList();
  }));
  $("#target").innerHTML = S.meta.targets.map((t) => `<option value="${esc(t)}">${esc(t.split(":")[1])}</option>`).join("");
  $("#mode").innerHTML = S.meta.modes.map((m) => `<option value="${esc(m)}">${esc(MODE_LABEL[m])}</option>`).join("");
  const downtown = S.meta.targets.find((t) => t.includes("Palo Alto"));
  if (downtown) $("#target").value = downtown;
  $("#mode").value = "bike";
  initMap();
  $$("#base-switch button").forEach((b) => b.addEventListener("click", () => setBase(b.dataset.base)));
  ["#q", "#sort", "#target", "#mode", "#minbeds", "#maxppb", "#inactive", ...$$(".st")].forEach((el) => {
    const node = typeof el === "string" ? $(el) : el;
    node.addEventListener("input", renderList);
  });
  $$("[data-close]").forEach((b) => b.addEventListener("click", () => {
    $("#" + b.dataset.close).classList.add("hidden");
    if (b.dataset.close === "verify") { S.verifySkip = 0; loadPlaces(); }
  }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { $("#drawer").classList.add("hidden"); $("#verify").classList.add("hidden"); }
  });
  $("#verify-btn").addEventListener("click", openVerify);
  S.points = await api("/api/points");
  buildPointLayers();
  await Promise.all([loadPlaces(), loadStatus()]);
  const pts = S.places.filter((p) => p.lat != null).map((p) => [p.lat, p.lon]);
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.3), { maxZoom: 14 });
}

boot().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<p style="color:red;padding:8px">Failed to load: ${esc(e.message)}</p>`); });
