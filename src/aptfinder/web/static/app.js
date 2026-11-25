/* Apartment finder dashboard. Plain JS + Leaflet, no build step.
   All scraped text is untrusted: everything rendered goes through esc(). */
"use strict";

const S = {
  meta: null, places: [], points: {}, goal: "all", base: "sat", markers: {}, layers: {},
  selected: null, verifySkip: 0, verifySeed: 0, verifyMode: "conflict", conflictSkip: 0, ownSkip: 0,
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
  const a = S.meta.areas;
  if (a && a.zones) {
    const zl = L.layerGroup(a.zones.map((z) => {
      const on = a.active.includes(z.chunk);
      return L.circle([z.lat, z.lon], { radius: a.radius_km * 1000, color: on ? "#1f9d55" : "#888", weight: on ? 2 : 1,
        dashArray: on ? null : "6 6", fill: on, fillOpacity: 0.04, interactive: false });
    }));
    zl.addTo(map);
    overlays["Station walk zones (25 min)"] = zl;
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
    }).bindTooltip(() => (p.photo ? `<img class="tip-photo" src="${esc(p.photo)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">` : "")
      + `${esc(p.label)}<br>${esc(STATUS_LABEL[st])}`);
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
  const showTodo = $("#todo").checked;
  return S.places.filter((p) => {
    if (!sts.has(placeStatus(p))) return false;
    if (!showInactive && !p.n_active) return false;
    if (!showTodo && p.todo) return false;
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
    price: (p) => [nil(S.goal === "temp" ? p.min_stay_month : S.goal === "lease" ? p.min_lease_month : p.min_price_month)],
    ppb: (p) => [nil(S.goal === "lease" ? p.min_lease_per_bed : p.min_price_per_bed)],
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

// Lease rent and furnished-stay prices are different offers: temp shows stays, lease shows leases, All both.
function priceFacts(p) {
  const lease = p.min_lease_month != null ? `<span><b>${money(p.min_lease_month)}</b>/mo</span><span><b>${money(p.min_lease_per_bed)}</b>/bed</span>` : "";
  const stay = p.min_stay_month != null ? `<span title="furnished short stay">stay <b>${money(p.min_stay_month)}</b>/mo</span>` : "";
  if (S.goal === "temp") return stay || `<span class="muted">no furnished stay</span>`;
  if (S.goal === "lease") return lease || `<span class="muted">no lease price</span>`;
  return (lease + stay) || `<span><b>${money(p.min_price_month)}</b>/mo</span>`;
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
    const chips = Object.entries(p.goals).filter(([g]) => S.goal === "all" || g === S.goal).map(([g, r]) => `<span class="chip ${r.status}" title="${esc(r.reasons.join("\n"))}">${esc(g)}: ${esc(STATUS_LABEL[r.status])}</span>`).join("");
    const bb = p.beds.length ? `${p.beds.map((b) => (b === 0 ? "studio" : b + "bd")).join("/")} · ${p.baths.join("/")}ba` : "beds ?";
    return `<div class="card ${st === "fail" || p.todo ? "dim" : ""} ${S.selected === p.place_key ? "sel" : ""}" data-pk="${esc(p.place_key)}">
      <h4>${esc(p.label)} ${stars(p.rating)}</h4>
      <div class="facts">
        ${priceFacts(p)}
        <span>${esc(bb)}</span>
        <span>${mins ? `<b>${num(mins.minutes)}</b> min ${esc(mode)} to ${esc(tgt)}${mins.provider === "estimate" ? " (est.)" : ""}` : `${esc(mode)} to ${esc(tgt)}: not computed`}</span>
        <span>${esc(p.sources)}</span>${p.n_active ? "" : '<span style="color:var(--bad)">inactive</span>'}
        ${p.user_status ? `<span>status: ${esc(p.user_status)}</span>` : ""}
      </div>
      <div class="chips">${p.todo ? `<span class="chip todo" title="outside the active chunks${p.chunk ? ` (in ${esc(p.chunk)})` : " and every station walk zone"}">TODO area</span>` : ""}${chips}</div>
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
  const d = await api(`api/places/${encodeURIComponent(pk)}`);
  const p = d.place;
  const label = p.label || d.listings.find((l) => l.fields.title)?.fields.title || pk;
  const goals = Object.entries(d.goals).filter(([g, r]) => r && (S.goal === "all" || g === S.goal)).map(([g, r]) => `
    <div><span class="chip ${r.status}">${esc(g)}: ${esc(STATUS_LABEL[r.status])}</span>${r.own_site ? ' <span class="small muted" title="The building\'s own website prices offers here, so only its listings decide the goals; other sources are shown as \'also listed\'.">judged on the own website</span>' : ""}
      ${r.reasons.map(([s, t]) => `<div class="reason ${s}">${s === "pass" ? "✓" : s === "fail" ? "✗" : "?"} ${esc(t)}</div>`).join("")}
      ${r.nice.length ? `<div class="small muted">nice to have: ${r.nice.map(([s, t]) => `${s === "pass" ? "✓" : s === "fail" ? "✗" : "?"} ${esc(t)}`).join(" · ")}</div>` : ""}
    </div>`).join("");
  const photos = (d.photos || []).map((ph) => `<figure class="ph"><a href="${esc(ph.page || ph.url)}" target="_blank" rel="noopener noreferrer" title="Open the ${esc(ph.site || ph.source)} page this photo is from"><img loading="lazy" src="${esc(ph.url)}" alt="photo from ${esc(ph.site || ph.source)}" onerror="this.closest('figure').remove()"></a>${ph.page ? `<a class="src" href="${esc(ph.page)}" target="_blank" rel="noopener noreferrer" title="Open the page this photo came from">${esc(ph.site || ph.source)} ↗</a>` : `<span class="src">${esc(ph.source)}</span>`}</figure>`)
    .concat(d.listings.flatMap((l) => l.media.map((f) => `<a href="media/${encodeURIComponent(f)}" target="_blank" rel="noopener"><img src="media/${encodeURIComponent(f)}" alt="file from ${esc(l.source)}"></a>`)));
  // other sources' prices beside the own site's: "also listed at $X on zillow"
  const alsoListed = (pl) => (pl.also_listed || []).length ? `<br><span class="muted">${pl.also_listed.map((a) =>
    `also listed${a.price_month ? ` at ${money(a.price_month)}` : ""} on ${esc(a.source)}`).join("<br>")}</span>` : "";
  const rng = (a, f) => (!a ? '<span class="muted">?</span>' : a[0] === a[1] ? f(a[0]) : `${f(a[0])}–${f(a[1])}`);
  // leases and furnished offers (the building's own furnished option, operators' furnished stays) never mix
  const planRows = (plans) => `<div class="scroll-x"><table>
    <tr><th>plan</th><th>bd / ba</th><th>sqft</th><th>$/month</th><th>$/bed</th><th>available</th><th>earliest</th><th>sources</th><th></th></tr>
    ${plans.map((pl) => `<tr>
      <td>${esc(pl.name || "")}${pl.offer !== "lease" ? `<br><span class="chip">${esc(pl.offer)}</span>` : ""}</td>
      <td>${pl.beds === 0 ? "studio" : esc(pl.beds ?? "?")} / ${esc(pl.baths ?? "?")}</td>
      <td>${rng(pl.sqft, (v) => num(v))}</td>
      <td>${rng(pl.price_month, money)}</td>
      <td>${pl.price_per_bed ? money(pl.price_per_bed) : ""}</td>
      <td>${pl.plan_only ? '<span class="muted" title="Listed in the community\'s own plan data, with no price: no unit in this plan is known to be offered. Not counted for goals.">plan exists, no unit offered</span>'
        : pl.n_units ? `${pl.n_units} unit${pl.n_units > 1 ? "s" : ""}` : `${pl.n_active} listing${pl.n_active === 1 ? "" : "s"}`}</td>
      <td>${esc(pl.earliest || "")}</td>
      <td>${pl.from_direct ? `<b>own site</b>${alsoListed(pl)}`
        : pl.not_on_own_site ? `<span class="muted" title="The building's own website prices other plans but not this one; it does not count for goals.">not on own site</span>${alsoListed(pl)}`
        : esc(pl.sources.join(", "))}</td>
      <td>${pl.image ? `<a href="${esc(pl.image)}" target="_blank" rel="noopener noreferrer"><img class="plan-img" loading="lazy" src="${esc(pl.image)}" alt="floor plan" onerror="this.parentNode.remove()"></a>` : ""}</td>
    </tr>`).join("")}
  </table></div>`;
  const planTable = (d.plans || []).length ? planRows(d.plans) : '<p class="muted small">No plan information.</p>';
  const isStay = pk.includes("~");
  const related = (d.related || []).map((r) => `<a href="#" data-goto="${esc(r.place_key)}">${esc(r.label)}</a>
      <span class="muted">(${r.n_active} active${r.min_stay_month ? `, stays from ${money(r.min_stay_month)}/mo` : r.min_lease_month ? `, from ${money(r.min_lease_month)}/mo` : ""})</span>`);
  const relatedBox = related.length ? `<div class="related small">${isStay ? "Building:" : "Also in this building (third party, like a hotel):"} ${related.join(" · ")}</div>` : "";
  const unitRows = d.listings.map((l, i) => {
    const f = l.fields;
    const c = (k) => ck(l.checks[k], l.overridden.includes(k));
    return `<tr class="${l.active ? "" : "inactive"}">
      <td><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.source)}</a></td>
      <td>${esc(f.unit || "")}</td><td>${esc(f.floor_plan || "")}</td>
      <td>${fmtField("beds", f.beds)}${c("beds")} / ${fmtField("baths", f.baths)}${c("baths")}</td>
      <td>${fmtField("sqft", f.sqft)}${c("sqft")}</td>
      <td>${fmtField("price", f.price)}${f.price_basis && f.price_basis !== "month" ? ` <span class="muted">/${esc(f.price_basis)}</span>` : ""}${c("price")}</td>
      <td>${esc(f.available_from || "")}${c("available_from")}</td>
      <td class="muted">${esc((l.first_seen || "").slice(5, 10))}→${esc((l.last_seen || "").slice(5, 10))}${l.active ? "" : " inactive"}</td>
      <td class="nowrap"><button class="small" data-raw="${i}" title="Show what we parsed and what the source sent">raw</button>
        <button class="small" data-split="${esc(l.source)}|${esc(l.source_id)}" title="Record that this listing is a different place">split</button></td>
    </tr>
    <tr class="raw-row hidden" data-raw-row="${i}"><td colspan="9">
      <div class="small muted">${esc(l.source)} ${esc(l.source_id)} · latest of ${l.n_obs} observation${l.n_obs === 1 ? "" : "s"} · parsed fields:</div>
      <pre class="raw">${esc(JSON.stringify(l.data, null, 2))}</pre>
      ${l.has_raw ? `<div class="small muted">as the source sent it:</div><pre class="raw" data-raw-src="${esc(l.source)}|${esc(l.source_id)}">loading…</pre>` : ""}
    </td></tr>`;
  }).join("");
  const listingTable = `<details${d.listings.length <= 4 ? " open" : ""}><summary>Units &amp; listings (${d.listings.length})</summary>
    <div class="scroll-x"><table>
    <tr><th>source</th><th>unit</th><th>plan</th><th>bd / ba</th><th>sqft</th><th>price</th><th>available</th><th>seen</th><th></th></tr>
    ${unitRows}</table></div>
    <p class="muted small">✓ checked correct · ✗ checked wrong · ✎ corrected value shown · · not hand-checked. <b>raw</b> opens a listing's parsed and source data.</p></details>`;
  $("#drawer-body").innerHTML = `
    <h2>${esc(label)} ${stars(d.annotations.filter((a) => a.attribute === "overall" && a.rating).slice(-1)[0]?.rating)}</h2>
    ${(d.websites || []).length ? `<div class="website">${d.websites.map((w) => `<a href="${esc(w.url)}" target="_blank" rel="noopener noreferrer">🌐 ${esc(w.site)} ↗</a>`).join(" ")}</div>` : '<div class="website muted small">no website found yet</div>'}
    <div class="muted small">${esc(p.address || "address unknown")} ${p.city ? "· " + esc(p.city) : ""} · ${esc(pk)}</div>
    <h3>Goals</h3>${goals}
    ${photos.length ? `<h3>Photos (${photos.length})</h3><div class="media">${photos.join("")}</div>` : '<p class="muted small">No photos from any source.</p>'}
    ${relatedBox}
    <h3>${isStay ? "Furnished stays" : "Floor plans"}</h3>${planTable}
    ${listingTable}
    <h3>Price history (per month)</h3>${historyChart([...d.listings].sort((a, b) => b.history.length - a.history.length).slice(0, 8))}
    <h3>Commute <button id="route-btn" class="small">${S.meta.google ? "Route with Google" : "Recompute (estimate)"}</button></h3>
    ${S.meta.google ? `<p class="muted small">Routes every target by every mode (about 20 Google calls). Google is metered and capped per month (config/google.toml); when the cap is reached routing stops and the straight-line estimates stay.</p>` : ""}${matrixTable(d)}
    <h3>Nearby</h3>${nearestBlock(d)}
    <h3>Your notes & ratings</h3>${attributesBlock(d, pk)}
    ${pipelineBlock(d.pipeline)}
  `;
  $("#drawer").classList.remove("hidden");
  wireDrawer(pk);
}

function wireDrawer(pk) {
  $$("#drawer .attr").forEach((box) => {
    const attr = box.dataset.attr;
    $$(".rate button", box).forEach((b) => b.addEventListener("click", async () => {
      await post("api/annotations", { entity_key: pk, attribute: attr, rating: +b.dataset.rate, goal_id: S.goal === "all" ? null : S.goal });
      await refreshAfterWrite(pk); toast(`Rated ${attr} ${b.dataset.rate}/5`);
    }));
    $("form", box).addEventListener("submit", async (e) => {
      e.preventDefault();
      const c = e.target.c.value.trim();
      if (!c) return;
      await post("api/annotations", { entity_key: pk, attribute: attr, comment: c, goal_id: S.goal === "all" ? null : S.goal });
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
  $$("#drawer [data-goto]").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openPlace(a.dataset.goto); }));
  $$("#drawer [data-raw]").forEach((b) => b.addEventListener("click", () => {
    const row = $(`#drawer [data-raw-row="${b.dataset.raw}"]`);
    row.classList.toggle("hidden");
    b.classList.toggle("on", !row.classList.contains("hidden"));
    // the source's own item is fetched only when first opened (raw files are large)
    const pre = $("[data-raw-src]", row);
    if (pre && !pre.dataset.loaded && !row.classList.contains("hidden")) {
      pre.dataset.loaded = "1";
      const [source, ...rest] = pre.dataset.rawSrc.split("|");
      api(`api/raw?source=${encodeURIComponent(source)}&source_id=${encodeURIComponent(rest.join("|"))}`)
        .then((r) => { pre.textContent = JSON.stringify(r.item, null, 2); })
        .catch((e) => { pre.textContent = "not available: " + e.message; });
    }
  }));
  $$("#drawer [data-split]").forEach((b) => b.addEventListener("click", async () => {
    const [source, source_id] = b.dataset.split.split("|");
    if (!confirm(`Record that ${source} ${source_id} is a different place from the others here?`)) return;
    const r = await post("api/links", { source, source_id, place_key: null });
    toast("Split recorded"); await loadPlaces(); openPlace(r.place_key);
  }));
  $("#route-btn")?.addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Routing…";
    try {
      const r = await post(`api/trips/${encodeURIComponent(pk)}`);
      toast(`${r.new_trips} new trips (${r.provider}, ${r.api_calls} API calls)`);
      await refreshAfterWrite(pk);
    } catch (err) { toast((/cap/i.test(err.message) ? "Google limit reached, estimates kept: " : "Routing failed: ") + err.message, 6000); e.target.disabled = false; e.target.textContent = "Retry routing"; }
  });
}

async function refreshAfterWrite(pk) {
  await loadPlaces();
  if (pk) await openPlace(pk);
}

/* ---------------------------------------------------------------- verify mode */
async function openVerify() {
  $("#verify").classList.remove("hidden");
  S.verifyMode = "conflict"; S.conflictSkip = 0; S.verifySkip = 0;
  await renderVerify();
}

const CONFLICT_PAGE = 10;
const gotoPlace = (pk) => { $("#verify").classList.add("hidden"); openPlace(pk); };

/* conflicts, ten to a screen: one block per conflict, a row per disagreeing field, a column per source */
function conflictBlock(e) {
  const it = e.item, obs = e.observations;
  const unit = it.unit_key ? it.unit_key.split("/").slice(1).join("/") : "";
  const tags = [
    it.pending_render ? `<span class="chip pending" title="${esc(it.pending_reason || "")}">own-site render pending: will update this</span>` : "",
    it.text ? `<span class="chip">source vs its own text</span>` : "",
  ].join("");
  const textTh = it.text ? `<th>${esc(it.text_label)}<br><span class="muted">read from the text</span></th>` : "";
  const textTd = (f) => (it.text ? `<td>${fmtField(f, it.text[f])}</td>` : "");
  return `<div class="vconf${it.pending_render ? " pending" : ""}">
    <div class="vconf-head"><a href="#" data-goto="${esc(it.place_key)}">${esc(e.label || it.place_key)}</a>
      ${unit ? `<span class="muted small">unit ${esc(unit)}</span>` : ""} ${tags}</div>
    <div class="scroll-x"><table class="vconf-t"><tr><th></th>${obs.map((o) => `<th><a href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">${esc(o.source)} ↗</a> <span class="muted">${esc((o.observed_at || "").slice(0, 10))}</span></th>`).join("")}${textTh}</tr>
    ${e.fields.map((f) => `<tr><th>${esc(f)}</th>${obs.map((o) => `<td><b>${fmtField(f, o.fields[f])}</b> ${vbtns({ observation_id: o.obs_id, field: f, queue: "conflict" }, (o.checked || {})[f])}</td>`).join("")}${textTd(f)}</tr>`).join("")}
    </table></div></div>`;
}

async function renderConflicts() {
  const body = $("#verify-body");
  body.innerHTML = '<p class="muted">Loading…</p>';
  const d = await api(`api/verify/batch?kind=conflict&skip=${S.conflictSkip}&n=${CONFLICT_PAGE}`);
  if (!d.items.length && S.conflictSkip > 0 && d.total > 0) { S.conflictSkip = Math.max(0, d.total - CONFLICT_PAGE); return renderConflicts(); }
  if (!d.items.length) { S.verifyMode = "own"; S.ownSkip = 0; return renderVerify(); }
  const from = S.conflictSkip + 1, to = S.conflictSkip + d.items.length;
  const more = to < d.total;
  body.innerHTML = `<h2>${d.total} conflicts: ${d.actionable} to check, ${d.pending} waiting for an own-site render</h2>
    <p class="small">Sources disagree on these units. Open each listing, then mark every stored value: ✓ correct, ✗ wrong (type the true value), ? cannot tell. Conflicts tagged <i>own-site render pending</i> come last: the building's own site will be read again and will likely settle them.</p>
    <p class="muted small">Showing ${from}–${to} of ${d.total}</p>
    ${d.items.map(conflictBlock).join("")}
    <p style="margin-top:14px"><button id="v-prev" ${S.conflictSkip ? "" : "disabled"}>Previous ${CONFLICT_PAGE}</button>
      ${more ? `<button id="v-more" class="accent">Next ${CONFLICT_PAGE}</button>` : ""}
      <button id="v-rest" class="${more ? "" : "accent"}">${more ? "Skip to" : "Continue:"} buildings whose own site gave nothing usable, then the sample</button></p>`;
  $$(".vbtns", body).forEach(wireVbtns);
  $$("[data-goto]", body).forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); gotoPlace(a.dataset.goto); }));
  $("#v-prev").addEventListener("click", () => { S.conflictSkip = Math.max(0, S.conflictSkip - CONFLICT_PAGE); renderConflicts(); loadStatus(); });
  $("#v-more")?.addEventListener("click", () => {
    // a conflict whose every value got a verdict has left the queue, so the next page starts that much earlier
    const done = $$(".vconf", body).filter((b) => $$(".vbtns", b).every((v) => $("button.on", v))).length;
    S.conflictSkip += d.items.length - done; renderConflicts(); loadStatus();
  });
  $("#v-rest").addEventListener("click", () => { S.verifyMode = "own"; S.ownSkip = 0; renderVerify(); loadStatus(); });
}

async function renderVerify() {
  if (S.verifyMode === "conflict") return renderConflicts();
  if (S.verifyMode === "own") return renderOwnSites();
  const body = $("#verify-body");
  body.innerHTML = '<p class="muted">Loading…</p>';
  const d = await api(`api/verify/next?seed=${S.verifySeed}&skip=${S.verifySkip}&conflicts=false`);
  if (d.done) { body.innerHTML = "<h2>Queue empty</h2><p>Nothing left to check. 🎉</p>"; return; }
  const it = d.item;
  const head = `<p class="muted small">${d.remaining} items in queue (an even sample across sources) · item ${S.verifySkip + 1} · <a href="#" id="v-back">back to the conflicts</a></p>`;
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
    const title = it.kind === "conflict" ? (it.text ? `${obs[0].source} and its own text disagree on ${d.fields.join(", ")}`
      : `Sources disagree on ${d.fields.join(", ")}`) : `Sample check: ${esc(obs[0].source)}`;
    // a text conflict: the listing's text (read by Haiku) as a second column; check the stored value against the page
    const textTh = it.text ? `<th>${esc(it.text_label)}<br><span class="muted">read from the listing text</span></th>` : "";
    const textTd = (f) => (it.text ? `<td>${fmtField(f, it.text[f])}</td>` : "");
    body.innerHTML = `${head}<h2>${esc(title)}</h2>
      <p>${esc(d.place?.property_name || "")} ${esc(d.place?.address || it.place_key || "")}</p>
      <p class="small">Open each listing, then mark every stored value: ✓ correct, ✗ wrong (type the true value), ? cannot tell.</p>
      <div class="scroll-x"><table><tr><th>field</th>${obs.map((o) => `<th><a href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">${esc(o.source)} ↗</a><br><span class="muted">${esc(o.observed_at.slice(0, 10))}</span></th>`).join("")}${textTh}</tr>
      ${d.fields.map((f) => `<tr><th>${esc(f)}</th>${obs.map((o) => `<td>${fmtField(f, o.fields[f])}<br>${vbtns({ observation_id: o.obs_id, field: f, queue: it.kind === "conflict" ? "conflict" : "sample" })}</td>`).join("")}${textTd(f)}</tr>`).join("")}
      </table></div>${nav}`;
  }
  $$(".vbtns", body).forEach(wireVbtns);
  $("#v-back").addEventListener("click", backToConflicts);
  $("#v-skip").addEventListener("click", () => { S.verifySkip++; renderVerify(); });
  $("#v-next").addEventListener("click", () => { renderVerify(); loadStatus(); });
}

/* buildings whose own site gave no usable offer: one list, a reason and every URL tried; add a URL or dismiss */
const OWN_PAGE = 20;
const URL_STATUS = { form: "fail", rejected: "fail", failed: "fail", no_plans: "needs_check", pending: "", priced: "pass" };
async function renderOwnSites() {
  const body = $("#verify-body");
  body.innerHTML = '<p class="muted">Loading…</p>';
  const d = await api(`api/verify/own_sites?skip=${S.ownSkip}&n=${OWN_PAGE}`);
  if (!d.items.length && S.ownSkip > 0 && d.total > 0) { S.ownSkip = Math.max(0, d.total - OWN_PAGE); return renderOwnSites(); }
  const counts = Object.entries(d.by_reason).map(([k, n]) => `${n} ${esc(d.labels[k])}`).join(" · ");
  const block = (it) => `<div class="vconf" data-pk="${esc(it.place_key)}">
    <div class="vconf-head"><a href="#" data-goto="${esc(it.place_key)}">${esc(it.name)}</a> <span class="muted small">${esc(it.address)} · ${it.listings} listing(s)</span>
      <span class="chip">${esc(it.reason_label)}</span></div>
    ${it.urls.length ? `<div class="scroll-x"><table class="vconf-t"><tr><th>URL</th><th>from</th><th>last outcome</th></tr>
      ${it.urls.map((u) => `<tr><td style="word-break:break-all">${u.url.startsWith("http") ? `<a href="${esc(u.url)}" target="_blank" rel="noopener noreferrer">${esc(u.url)}</a>` : esc(u.url)}</td>
        <td class="small">${esc(u.via)}</td><td class="small reason ${URL_STATUS[u.status] || ""}" style="white-space:normal;min-width:240px">${esc(u.label)}</td></tr>`).join("")}</table></div>`
      : `<p class="small"><a href="https://www.google.com/search?q=${encodeURIComponent(`${it.name} ${it.address}`)}" target="_blank" rel="noopener noreferrer">Search Google ↗</a></p>`}
    <p class="small"><input class="own-url" type="url" placeholder="https://… another page with this building's plans" style="width:min(100%,380px)">
      <button class="own-add">Add URL</button>
      <button class="own-dismiss" title="Leave it: no floor plans online is fine">Dismiss: no plans online</button>
      ${it.reason === "no_website" ? '<button class="own-none">No website</button>' : ""}</p></div>`;
  const more = S.ownSkip + d.items.length < d.total;
  body.innerHTML = `<h2>${d.total} buildings whose own site gave nothing usable</h2>
    <p class="small">${counts}. Each leaves the list by itself once one of its sites gives priced offers. <b>Add URL</b> adds one more source for the building (it never replaces the others); the next <code>promote-sites</code> checks its address and <code>render</code> reads it.</p>
    <p class="muted small"><a href="#" id="v-back">back to the conflicts</a></p>
    ${d.items.map(block).join("") || "<p>Nothing here.</p>"}
    <p style="margin-top:14px"><button id="o-prev" ${S.ownSkip ? "" : "disabled"}>Previous ${OWN_PAGE}</button>
      ${more ? `<button id="o-more" class="accent">Next ${OWN_PAGE}</button>` : ""}
      <button id="o-rest" class="${more ? "" : "accent"}">${more ? "Skip to" : "Continue:"} the sample</button></p>`;
  const note = async (pk, attribute, comment, msg) => {
    try { await post("api/annotations", { entity_key: pk, attribute, comment }); toast(msg); renderOwnSites(); loadStatus(); }
    catch (e) { toast("Not saved: " + e.message, 5000); }
  };
  $$(".vconf", body).forEach((box) => {
    const pk = box.dataset.pk;
    $(".own-add", box).addEventListener("click", () => {
      const u = $(".own-url", box).value.trim();
      if (!/^https?:\/\//i.test(u)) { toast("Type a URL starting with http"); return; }
      note(pk, "website", u, "URL added: pending check (promote-sites fetches it)");
    });
    $(".own-dismiss", box).addEventListener("click", () => note(pk, "own_site", "dismissed: no plans online", "Dismissed"));
    $(".own-none", box)?.addEventListener("click", () => note(pk, "website", "none", "Marked: no website"));
  });
  $$("[data-goto]", body).forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); gotoPlace(a.dataset.goto); }));
  $("#v-back").addEventListener("click", backToConflicts);
  $("#o-prev").addEventListener("click", () => { S.ownSkip = Math.max(0, S.ownSkip - OWN_PAGE); renderOwnSites(); });
  $("#o-more")?.addEventListener("click", () => { S.ownSkip += OWN_PAGE; renderOwnSites(); });
  $("#o-rest").addEventListener("click", () => { S.verifyMode = "rest"; S.verifySkip = 0; renderVerify(); });
}

function backToConflicts(e) { e.preventDefault(); S.verifyMode = "conflict"; S.conflictSkip = 0; renderVerify(); }

function vbtns(ctx, verdict) {
  // verdict: one already given (a current check), shown as pressed
  const on = (v) => (v === verdict ? " on" : "");
  return `<span class="vbtns" data-ctx='${esc(JSON.stringify(ctx))}'>
    <button data-v="correct" class="correct${on("correct")}" title="correct">✓</button>
    <button data-v="wrong" class="wrong${on("wrong")}" title="wrong">✗</button>
    <button data-v="cannot_tell" class="cannot_tell${on("cannot_tell")}" title="cannot tell">?</button>
    <input class="hidden" placeholder="true value"><button class="hidden" data-save>save</button></span>`;
}

function wireVbtns(box) {
  const ctx = JSON.parse(box.dataset.ctx);
  const input = $("input", box), save = $("[data-save]", box);
  const send = async (verdict, true_value) => {
    try {
      await post("api/checks", { ...ctx, verdict, true_value });
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

/* ---------------------------------------------------------------- pipeline status */
const STATE_LABEL = { done: "done", stale: "stale", partial: "partial", todo: "todo", blocked: "blocked" };
const stChip = (st, text) => `<span class="chip st-${esc(st)}">${esc(text || STATE_LABEL[st] || st)}</span>`;
const day = (ts) => (ts ? esc(String(ts).slice(0, 10)) : '<span class="muted">never</span>');
const cmdList = (cmds) => (cmds || []).map((c) => `<code>${esc(c)}</code>`).join("");

async function openPipeline() {
  $("#pipeline").classList.remove("hidden");
  const body = $("#pipeline-body");
  body.innerHTML = '<p class="muted">Loading…</p>';
  let d;
  try { d = await api("api/pipeline"); } catch (e) { body.innerHTML = `<p class="reason fail">Failed: ${esc(e.message)}</p>`; return; }
  const stages = `<div class="scroll-x"><table class="stages">
    <tr><th>stage</th><th>state</th><th>where it stands</th><th>last run</th><th>cost</th><th>to run the rest</th></tr>
    ${d.stages.map((s) => `<tr><td><b>${esc(s.label)}</b></td><td>${stChip(s.state)}</td><td>${esc(s.summary)}</td>
      <td class="nowrap">${s.last_run !== undefined ? day(s.last_run) : ""}</td>
      <td class="nowrap">${s.cost ? "$" + (+s.cost).toFixed(2) : ""}</td>
      <td class="cmd">${cmdList(s.commands) || '<span class="muted">nothing left</span>'}</td></tr>`).join("")}
  </table></div>`;
  // intake matrix: chunk x area x source, one table per area kind
  const matrix = Object.entries(d.intake.sources).filter(([, ss]) => ss.length).map(([kind, ss]) => {
    const rows = d.intake.rows.filter((r) => r.kind === kind);
    if (!rows.length) return "";
    const cell = (c) => stChip(c.state, c.state === "todo" ? "todo" : c.empty ? "empty"
      : `${c.state === "partial" ? "partial " : ""}${(c.date || "").slice(5)}`);
    return `<div class="scroll-x"><table>
      <tr><th>chunk</th><th>${esc(kind)} area</th>${ss.map((s) => `<th>${esc(s)}</th>`).join("")}</tr>
      ${rows.map((r) => `<tr class="${r.active ? "" : "off"}"><td>${esc(r.chunk)}${r.active ? "" : ' <span class="muted small">(not active)</span>'}</td><td>${esc(r.area)}</td>
        ${ss.map((s) => `<td class="cell" title="${r.cells[s].run_id ? `run ${r.cells[s].run_id}` : "never covered"}">${cell(r.cells[s])}</td>`).join("")}</tr>`).join("")}
    </table></div>`;
  }).join("");
  const runs = `<div class="scroll-x"><table><tr><th>source</th><th>area kind</th><th>runs</th><th>last ok run</th><th>last status</th><th>cost</th></tr>
    ${d.intake.runs.map((r) => `<tr><td>${esc(r.source)}</td><td>${esc(r.area_kind)}</td><td>${r.runs}</td><td>${day(r.last_run)}</td>
      <td>${r.last_status ? `<span class="${r.last_status === "error" ? "reason fail" : ""}" title="${esc(r.last_error || "")}">${esc(r.last_status)}</span>` : ""}</td>
      <td>$${(+r.cost).toFixed(2)}</td></tr>`).join("")}</table></div>`;
  const web = d.stages.find((s) => s.id === "websites");
  const models = Object.entries(web.no_site_models).map(([m, n]) => `${m}: ${n}`).join(", ");
  const webTable = `<div class="scroll-x"><table><tr><th>website state</th><th>active chunks</th><th>all</th></tr>
    ${Object.entries(web.labels).map(([k, label]) => `<tr><td>${esc(label)}${k === "no_site" && models ? ` <span class="muted">(${esc(models)})</span>` : ""}</td>
      <td>${web.active_by_state[k] || 0}</td><td>${web.by_state[k] || 0}</td></tr>`).join("")}
    <tr><td><b>named buildings</b></td><td><b>${web.active_named}</b></td><td><b>${web.named}</b></td></tr></table></div>`;
  const sites = `<div class="scroll-x"><table><tr><th>site</th><th>chunk</th><th>state</th><th>last scrape</th><th>last render</th><th>rows</th><th>priced</th><th>re-scrape</th></tr>
    ${d.sites.map((s) => `<tr class="${s.active ? "" : "off"}"><td><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.name)}</a>${s.discovered ? ' <span class="muted small">(found by search)</span>' : ""}${s.check && s.check.outcome !== "ok" ? `<br><span class="small ${s.check.outcome === "rejected" ? "reason fail" : "muted"}">address check: ${esc(s.check.label)}</span>` : ""}</td>
      <td>${esc(s.chunk || "?")}</td><td>${stChip(s.state, s.state === "todo" ? "never" : s.state)}</td><td>${day(s.last)}</td>
      <td>${renderCell(s.render)}</td>
      <td>${s.rows}${s.info_only ? ' <span class="muted small">photos/link only</span>' : ""}</td><td>${s.priced}</td><td class="cmd"><code>${esc(s.command)}</code></td></tr>`).join("")}
  </table></div>`;
  const anchors = d.stages.find((s) => s.id === "anchors").anchors;
  const stuck = d.stuck_runs.length ? `<p class="reason fail small">Runs never closed (still 'running'): ${d.stuck_runs.map((r) => `#${r.id} ${esc(r.component)}/${esc(r.source || "")} ${day(r.started_at)}`).join(", ")}</p>` : "";
  body.innerHTML = `<div class="pipe">
    <h2>Pipeline status</h2>
    <p class="muted small">Computed now from the logs (${esc(d.now.slice(0, 16).replace("T", " "))} UTC). Active chunks: ${esc(d.active.join(", ") || "none")}; headline states count only those. Stale = older than ${d.stale_days} days.
      ${["done", "partial", "stale", "todo", "blocked"].map((s) => stChip(s)).join(" ")}</p>
    ${stuck}${stages}
    <h3>Intake coverage (aggregators)</h3>${matrix}
    <details><summary>Runs per source</summary>${runs}</details>
    <h3>Websites of named buildings</h3>${webTable}
    <h3>Own-site scrapes (${d.sites.length})</h3>${sites}
    <details><summary>Station → target bike legs (${anchors.filter((a) => a.routed).length}/${anchors.length} routed)</summary>
      <div class="scroll-x"><table><tr><th>station</th><th>target</th><th>routed</th></tr>
      ${anchors.map((a) => `<tr><td>${esc(a.station)}</td><td>${esc(a.target)}</td><td>${a.routed ? "Google" : '<span class="muted">estimate</span>'}</td></tr>`).join("")}</table></div></details>
  </div>`;
}

// a site's last render, failure or not: "blocked (HTTP 403) on 11-24, will retry"
function renderCell(r) {
  if (!r) return '<span class="muted">never</span>';
  const st = r.outcome === "skipped" ? "done" : r.retry ? "blocked" : r.stale ? "stale" : "done";
  return `${stChip(st, r.outcome === "skipped" ? "done" : r.retry ? "failed" : r.stale ? "stale" : "done")} <span class="small" title="${esc(r.error || "")}">${esc(r.label || "")}</span>`;
}

function pipelineBlock(ps) {
  if (!ps) return "";
  const rows = ps.sources.map((s) => `<tr><td>${esc(s.source === "direct" ? "own site" : s.source)}</td><td>${day(s.seen)}</td>
    <td>${stChip(s.state, s.state === "stale" ? `stale (>${ps.stale_days} d)` : "fresh")}</td>
    <td>${s.active}/${s.listings} active</td>
    <td>${s.text_read == null ? "" : s.text_read ? `text read by Haiku (${s.text_read}/${s.listings})` : '<span class="muted">text not read yet</span>'}</td></tr>`).join("");
  const w = ps.website;
  const t = ps.trips;
  const commute = !Object.keys(t).length ? '<span class="muted">no commute times yet</span>'
    : t.google ? `Google routes to ${t.google} point${t.google === 1 ? "" : "s"}, straight-line estimates for ${t.estimate || 0}`
    : `straight-line estimates only (${t.estimate} points)`;
  return `<h3>Pipeline</h3><div class="pipe-place">
    <div class="scroll-x"><table><tr><th>source</th><th>last seen</th><th></th><th>listings</th><th>text</th></tr>${rows}</table></div>
    <p class="small">Own website: <b>${esc(w.label)}</b>${w.url ? ` · <a href="${esc(w.url)}" target="_blank" rel="noopener noreferrer">${esc(w.url)}</a>` : ""} <span class="muted">${esc(w.detail || "")}</span></p>
    ${(ps.sites || []).map((x) => `<p class="small">Own site <code>${esc(x.id)}</code>: render ${renderCell(x.render)}${x.check ? ` · address check: ${esc(x.check.label)}` : ""}</p>`).join("")}
    <p class="small">Commute: ${commute}</p></div>`;
}

/* ---------------------------------------------------------------- status strip */
async function loadStatus() {
  const s = await api("api/status");
  const intake = s.runs.filter((r) => r.component === "intake");
  const parts = [`<span>${s.counts.places} places · ${s.counts.active}/${s.counts.listings} listings active</span>`];
  for (const r of intake) {
    const c = r.counts || {};
    parts.push(`<span class="${r.status === "error" ? "err" : ""}" title="${esc(JSON.stringify(c))}">${esc(r.source)}: ${esc((r.finished_at || r.started_at || "").slice(0, 16).replace("T", " "))} ${esc(r.status)}${c.observations != null ? ` · ${c.observations} seen` : ""}${c.new != null ? ` · ${c.new} new` : ""}${c.changed != null ? ` · ${c.changed} changed` : ""}</span>`);
  }
  parts.push(`<span>est. spend $${(+s.spend).toFixed(2)}</span>`);
  if (s.google) parts.push(`<span title="${esc(JSON.stringify(s.google.by_mode))}">Google ${esc(s.google.month)}: ${s.google.calls}/${s.google.cap} calls · est. $${s.google.est_cost_usd.toFixed(2)}</span>`);
  if (s.errors.length) parts.push(`<span class="err" title="${esc(s.errors.map((e) => `${e.component}/${e.source}: ${e.error}`).join("\n"))}">${s.errors.length} recent errors</span>`);
  const accBySource = {};
  for (const a of s.accuracy) (accBySource[a.source] ||= []).push(`${a.field} ${a.correct}/${a.n}`);
  for (const [src, list] of Object.entries(accBySource)) parts.push(`<span title="hand-checked accuracy">✓ ${esc(src)}: ${esc(list.join(", "))}</span>`);
  if (!s.accuracy.length) parts.push(`<span>no hand checks yet</span>`);
  $("#status-strip").innerHTML = parts.join("");
  const pend = s.queues.conflict_pending ? ` + ${s.queues.conflict_pending} pending render` : "";
  $("#queue-count").textContent = `(${s.queues.conflict} conflicts${pend} · ${s.queues.own_site ?? 0} own site · ${s.queues.sample})`;
}

/* ---------------------------------------------------------------- boot */
async function loadPlaces() {
  S.places = await api("api/places");
  renderList();
}

async function boot() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  S.meta = await api("api/meta");
  $("#dbname").textContent = S.meta.db;
  const gs = $("#goal-switch");
  // start on the lease goal when there is one, else All
  S.goal = S.meta.goals.some((g) => g.id === "lease") ? "lease" : "all";
  gs.innerHTML = [`<button data-goal="all">All</button>`, ...S.meta.goals.map((g) => `<button data-goal="${esc(g.id)}" title="${esc(g.label)}">${esc(g.id)}</button>`)].join("");
  $$("button", gs).forEach((x) => x.classList.toggle("on", x.dataset.goal === S.goal));
  $$("button", gs).forEach((b) => b.addEventListener("click", () => {
    S.goal = b.dataset.goal;
    $$("button", gs).forEach((x) => x.classList.toggle("on", x === b));
    if (S.goal === "temp") $("#sort").value = "price"; // temp: cheaper is better within the ~$4k budget
    renderList();
    if (S.selected && !$("#drawer").classList.contains("hidden")) openPlace(S.selected); // show only this goal
  }));
  $("#target").innerHTML = S.meta.targets.map((t) => `<option value="${esc(t)}">${esc(t.split(":")[1])}</option>`).join("");
  $("#mode").innerHTML = S.meta.modes.map((m) => `<option value="${esc(m)}">${esc(MODE_LABEL[m])}</option>`).join("");
  if (S.meta.default_target) $("#target").value = S.meta.default_target; // the target a goal commutes to
  $("#mode").value = "bike";
  initMap();
  $$("#base-switch button").forEach((b) => b.addEventListener("click", () => setBase(b.dataset.base)));
  ["#q", "#sort", "#target", "#mode", "#minbeds", "#maxppb", "#inactive", "#todo", ...$$(".st")].forEach((el) => {
    const node = typeof el === "string" ? $(el) : el;
    node.addEventListener("input", renderList);
  });
  $$("[data-close]").forEach((b) => b.addEventListener("click", () => {
    $("#" + b.dataset.close).classList.add("hidden");
    if (b.dataset.close === "verify") { S.verifySkip = 0; loadPlaces(); }
  }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { ["#drawer", "#verify", "#pipeline"].forEach((id) => $(id).classList.add("hidden")); }
  });
  $("#verify-btn").addEventListener("click", openVerify);
  $("#pipeline-btn").addEventListener("click", openPipeline);
  S.points = await api("api/points");
  buildPointLayers();
  await Promise.all([loadPlaces(), loadStatus()]);
  const pts = S.places.filter((p) => p.lat != null).map((p) => [p.lat, p.lon]);
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.3), { maxZoom: 14 });
}

boot().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<p style="color:red;padding:8px">Failed to load: ${esc(e.message)}</p>`); });
