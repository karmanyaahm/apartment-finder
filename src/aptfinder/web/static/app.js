/* Apartment finder dashboard. Plain JS + Leaflet, no build step.
   All scraped text is untrusted: everything rendered goes through esc(). */
"use strict";

const S = {
  meta: null, places: [], points: {}, goal: "all", base: "sat", markers: {}, layers: {},
  selected: null, verifySkip: 0, verifySeed: 0, verifyMode: "conflict", conflictSkip: 0, ownSkip: 0, dupSkip: 0,
};
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v == null ? "?" : "$" + Math.round(v).toLocaleString());
const num = (v) => (v == null ? "?" : (+v).toLocaleString(undefined, { maximumFractionDigits: 1 }));
// commute times are shown in whole minutes (user, 2026-11-25); the database and the rules keep full precision
const mins = (v) => (v == null ? "?" : String(Math.round(+v)));
const RANK = { pass: 0, needs_check: 1, fail: 2 };
const STATUS_LABEL = { pass: "pass", needs_check: "needs check", fail: "fail" };
const MODE_LABEL = { walk: "walk", bike: "bike", transit: "transit", drive: "drive", bike_train_bike: "bike+train", bike_bus_bike: "bike+bus" };

// Errors always name the request and its HTTP status: over HTTP/2 (Cloudflare) statusText is empty, and an
// error page from the edge (502, 524 timeout, 530 tunnel down) is HTML, not JSON with a `detail`.
// A GET cut by a restart (a deploy stops the server and the tunnel for a few seconds: the edge answers 502/530 or
// the connection drops) is retried twice, 3 and 6 s later, before it fails.
const RETRY_STATUS = new Set([502, 503, 504, 520, 521, 522, 523, 524, 530]);
async function api(path, opts = {}, attempt = 0) {
  const name = path.split("?")[0];
  const retry = !opts.method && attempt < 2;
  const again = () => new Promise((ok) => setTimeout(ok, 3000 * (attempt + 1))).then(() => api(path, opts, attempt + 1));
  let r;
  try { r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts }); }
  catch (e) {
    if (retry) return again();
    throw new Error(`${name}: ${e.message || "network error"}`);
  }
  if (retry && RETRY_STATUS.has(r.status)) return again();
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch (_) { /* not json: an edge error page */ }
    if (typeof detail !== "string") detail = JSON.stringify(detail);
    throw new Error(`${name}: HTTP ${r.status}${r.statusText ? " " + r.statusText : ""}${detail ? ": " + detail : ""}`);
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
    const zl = L.layerGroup(a.zones.map((z) => L.circle([z.lat, z.lon], {
      radius: a.radius_km * 1000, color: "#1f9d55", weight: 2, fill: true, fillOpacity: 0.04, interactive: false })));
    zl.addTo(map);
    overlays["Station walk zones (25 min)"] = zl;
  }
  L.control.layers(null, overlays, { collapsed: true, position: "topright" }).addTo(map);
}

// Hover link between the list and the map: a card under the mouse enlarges and outlines its marker; a marker under
// the mouse outlines its card and, after 150 ms, scrolls it into view when it is off-screen. Mouse only, so a tap
// does not leave anything highlighted. The marker's own style (status colour, fail dimming) is kept on it to restore.
const CAN_HOVER = window.matchMedia("(hover: hover)").matches;
let hoverScroll = null;
function hiMarker(pk, on) {
  const m = S.markers[pk];
  if (!m) return;
  if (!on) { m.setStyle(m.baseStyle); return; }
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
  m.setStyle({ radius: 13, weight: 4, color: accent, opacity: 1, fillOpacity: 0.95 });
  m.bringToFront();
}
function hiCard(pk, on) {
  const card = [...$$(".card")].find((c) => c.dataset.pk === pk);
  clearTimeout(hoverScroll);
  if (!card) return;
  card.classList.toggle("hl", on);
  if (!on) return;
  hoverScroll = setTimeout(() => {
    const box = $("#cards").getBoundingClientRect(), r = card.getBoundingClientRect();
    if (r.top < box.top || r.bottom > box.bottom) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, 150);
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
    m.baseStyle = { radius: m.options.radius, weight: m.options.weight, color: m.options.color,
      opacity: m.options.opacity, fillOpacity: m.options.fillOpacity };
    m.on("click", () => openPlace(p.place_key));
    if (CAN_HOVER) {
      m.on("mouseover", () => hiCard(p.place_key, true));
      m.on("mouseout", () => hiCard(p.place_key, false));
    }
    m.addTo(S.placeLayer);
    S.markers[p.place_key] = m;
  }
}

/* ---------------------------------------------------------------- view controls (this browser only) */
// Sort, net, the status ticks and inactive are view controls, kept per browser in
// localStorage. What a place must have is the selected goal's rules (the goal editor below), stored on the server.
const VIEW_KEY = "aptf.view";
const lsGet = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
const lsSet = (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* blocked */ } };
function readView() {
  return {
    sort: $("#sort").value, agg: $("#agg").value,
    statuses: $$(".st").filter((c) => c.checked).map((c) => c.value),
    inactive: $("#inactive").checked,
  };
}
function applyView(f) {
  const has = (sel, v) => v != null && [...$(sel).options].some((o) => o.value === v);
  if (has("#sort", f.sort)) $("#sort").value = f.sort;
  if (has("#agg", f.agg)) $("#agg").value = f.agg;
  if (Array.isArray(f.statuses)) $$(".st").forEach((c) => { c.checked = f.statuses.includes(c.value); });
  if (f.inactive != null) $("#inactive").checked = !!f.inactive;
}
function loadView() {
  let v = null;
  try { v = JSON.parse(lsGet(VIEW_KEY) || "null"); } catch { v = null; }
  if (v) applyView(v);
}
const saveView = () => lsSet(VIEW_KEY, JSON.stringify(readView()));

// A commute row's point: one commute target (`commute_targets:Meta HQ`) or the nearest point of a set
// (`set:google_bus_stops`); a goal stores the first as a `commute_any` rule and the second as a `near` rule.
function limitOptions() {
  return [...S.meta.targets.map((t) => [t, t.split(":")[1]]),
    ...Object.entries(S.meta.point_sets).filter(([k]) => k !== "commute_targets").map(([k, v]) => [`set:${k}`, `nearest ${v.label}`])];
}
function limitName(pt, mode) {
  const where = pt.startsWith("set:") ? `nearest ${S.meta.point_sets[pt.slice(4)]?.label || pt.slice(4)}` : pt.split(":")[1];
  return `${MODE_LABEL[mode] || mode} to ${where}`;
}
function limitValue(p, pt, mode) {
  return pt.startsWith("set:") ? (p.nearest || {})[`${pt.slice(4)}|${mode}`] : p.minutes[`${pt}|${mode}`];
}
// a commute time coloured by the provider that computed it, the provider named on hover ("~" marks an estimate)
const SRC = { google: ["src-google", "Google Maps"], knn: ["src-knn", "range of up to 3 nearby Google routes"],
  estimate: ["src-estimate", "straight-line estimate (not routed)"] };
const srcOf = (prov) => SRC[prov] || ["", prov || "unknown source"];
// the text of a time: a KNN range "17-19" (one number when both ends round alike), "~27" for an estimate
const srcText = (v) => v.provider === "knn" && v.min != null
  ? (Math.round(v.min) === Math.round(v.max) ? mins(v.min) : `${Math.round(v.min)}–${Math.round(v.max)}`)
  : `${v.provider === "estimate" ? "~" : ""}${mins(v.minutes)}`;
const srcTitle = (v) => v.provider === "knn" && v.neighbors
  ? `from ${v.neighbors.length} nearby Google route${v.neighbors.length === 1 ? "" : "s"}: ${v.neighbors.map((n) => mins(n.minutes)).join(", ")} min (${v.neighbors.map((n) => n.km).join(", ")} km away)`
  : srcOf(v.provider)[1];
const srcMin = (v) => `<b class="${srcOf(v.provider)[0]}" title="${esc(srcTitle(v))}">${srcText(v)}</b>`;
const SRC_LEGEND = `<span class="src-key"><b class="src-google">■</b> Google Maps · <b class="src-knn">■ a–b</b> range of up to 3 nearby places' Google routes · <b class="src-estimate">■ ~</b> straight-line estimate</span>`;

// the selected goal's commute rules as (point, mode) facts on each card, so the requirement is visible:
// "walk to nearest Google bus 6 min (stop name, est.)"; the goal's fails colour them
function commuteRows() {
  const g = S.goal !== "all" && S.draft && S.draft.id === S.goal ? S.draft : null;
  if (!g) return [];
  return [...g.rules, ...g.nice].flatMap((r) => r.type === "commute_any" ? (r.options || []).map((o) => [r.target, o.mode])
    : r.type === "near" ? [[`set:${r.set}`, r.mode]] : []);
}
function limitFacts(p, fails = []) {
  return commuteRows().map(([pt, mode]) => {
    const v = limitValue(p, pt, mode);
    const name = limitName(pt, mode);
    const at = v && v.point ? ` (${v.point})` : "";
    const mk = markFor(fails, (x) => x.modes?.includes(mode) && (pt.startsWith("set:") ? x.f === "near" && x.set === pt.slice(4) : x.f === "commute" && x.pt === pt));
    return v && v.minutes != null ? `<span${mk}>${esc(name)} ${srcMin(v)} min${esc(at)}</span>`
      : `<span${mk}>${esc(name)}: no time</span>`;
  }).join("");
}

/* ---------------------------------------------------------------- net commute: the goal's commute rules together */
// Each place's goal result carries `commute`: [minutes or null, estimate] per hard commute rule of the goal (one
// per roommate), the same time each rule checks. The Net selector picks how they combine: total, mean, median or
// spread (standard deviation: low = everyone's commute is alike). Unknown terms are never counted as 0: the card
// says how many are unknown and the sort puts such places after the complete ones. All shows no net commute.
function aggregate(vals, agg) {
  if (!vals.length) return null;
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
  if (agg === "total") return mean * vals.length;
  if (agg === "mean") return mean;
  if (agg === "median") { const v = [...vals].sort((a, b) => a - b), n = v.length; return n % 2 ? v[(n - 1) / 2] : (v[n / 2 - 1] + v[n / 2]) / 2; }
  return Math.sqrt(vals.reduce((a, x) => a + (x - mean) ** 2, 0) / vals.length); // spread
}
function netCommute(p) {
  const terms = S.goal === "all" ? null : p.goals[S.goal]?.commute;
  if (!terms || !terms.length) return null;
  const known = terms.filter((t) => t[0] != null).map((t) => t[0]);
  return { v: aggregate(known, $("#agg").value), sd: aggregate(known, "spread"), n: terms.length, known: known.length,
    est: terms.some((t) => t[0] != null && t[1]) };
}
// the trips behind the net commute, one line each, from the same terms the number is computed from (the third
// element of each term, filter.rules.commute_trips): "drive to Meta HQ 20 min (Google) · rule to Meta HQ: drive <= 30"
function netTripLine(d) {
  const where = d.set ? `nearest ${S.meta.point_sets?.[d.set]?.label || d.set}${d.point ? ` (${d.point.split(":").slice(1).join(":")})` : ""}`
    : (d.point || "").split(":").slice(1).join(":");
  const head = `${d.mode ? `${MODE_LABEL[d.mode] || d.mode} ` : ""}to ${where}`;
  if (d.minutes == null) return `✗ ${head}: skipped, ${d.why || "no time"} (not counted) · rule ${d.rule}`;
  const src = d.provider === "knn"
    ? `KNN range ${mins(d.min)}–${mins(d.max)} of ${d.n || "?"} nearby Google route${d.n === 1 ? "" : "s"}; uses their median ${mins(d.minutes)}`
    : d.provider === "google" ? "Google" : d.provider === "estimate" ? "straight-line estimate" : d.provider;
  return `✓ ${head} ${mins(d.minutes)} min (${src}) · rule ${d.rule}`;
}
function netTrips(terms) {
  return (terms || []).filter((t) => t[2]).map((t) => netTripLine(t[2]));
}
function netTip(p) {
  const terms = S.goal === "all" ? null : p.goals[S.goal]?.commute;
  const lines = netTrips(terms);
  return lines.length ? `net commute (${$("#agg").value}) over these trips:\n${lines.join("\n")}` : "";
}
function netFact(p, fails) {
  const n = netCommute(p);
  if (!n) return "";
  const agg = $("#agg").value;
  // the rule's verdict colours the number only when the card shows the rule's own aggregate; else it stays a chip
  const mk0 = markFor(fails, (x) => x.f === "net_commute" && x.agg === agg);
  // hover lists the trips that went into the number (the rule's verdict, if any, first)
  const tip = esc(netTip(p));
  const mk = mk0 ? mk0.replace(/title="([^"]*)"/, (_, t) => `title="${t}${tip ? `\n\n${tip}` : ""}"`) : (tip ? ` title="${tip}"` : "");
  const trips = `${n.n} trip${n.n > 1 ? "s" : ""}${n.est ? ", est." : ""}`;
  if (n.v == null) return `<span${mk0 ? mk : `${mk} data-ff="needs_check"`}>net commute: no times yet (${trips})</span>`;
  const unk = n.known < n.n ? (agg === "total" ? ` + ? (${n.n - n.known} unknown)` : ` of ${n.known} known (${n.n - n.known} unknown)`) : "";
  const main = agg === "spread" ? `spread ±<b>${mins(n.v)}</b> min${unk}` : `${agg} <b>${mins(n.v)}</b> min${unk}${n.known > 1 ? ` · spread ±${mins(n.sd)}` : ""}`;
  return `<span${mk}>net commute ${main} (${trips})</span>`;
}

/* ---------------------------------------------------------------- goals: the filter panel edits the selected goal */
// A goal is {id, label, rules, nice} (filter/rules.py rule dicts). The panel edits a draft of the selected goal;
// every change is judged on the server (POST api/goals/preview, writes nothing) and shown in the list at once.
// "Save" stores the goal for everyone (an append-only annotation row), "Save as new goal" stores a copy,
// "Revert" drops the draft. All shows each place's best status over every goal and edits nothing.
S.goalsData = null; // GET api/goals
S.draft = null; // {id, label, rules, nice}: the selected goal being edited
S.previewMsg = "";
const clone = (o) => JSON.parse(JSON.stringify(o));
const savedGoal = (id) => S.goalsData?.goals.find((g) => g.id === id);
const draftDirty = () => S.draft && JSON.stringify({ l: S.draft.label, r: S.draft.rules, n: S.draft.nice })
  !== JSON.stringify((({ label, rules, nice }) => ({ l: label, r: rules, n: nice }))(savedGoal(S.draft.id) || {}));
// the offer a goal makes: "short" term = furnished stay prices, "long" = lease prices, neither = both
function goalTerm(id) {
  const g = S.draft && S.draft.id === id ? S.draft : savedGoal(id);
  return g?.rules.find((r) => r.type === "equals" && r.field === "term")?.value || null;
}

// "+ add" menu: [label, the new rule]
function ruleTemplates() {
  const pt = S.meta.default_target || S.meta.targets[0];
  return [
    ["price / bed at most", { type: "max", field: "price_per_bed", value: 2500 }],
    ["price / month at most", { type: "max", field: "price_month", value: 4000 }],
    ["beds at least", { type: "min", field: "beds", value: 3 }],
    ["beds and baths", { type: "size_options", options: [{ beds: 3, min_baths: 2 }] }],
    ["commute", { type: "commute_any", target: pt, options: [{ mode: "bike", max: 25 }] }],
    ["nearest point of a set", { type: "near", set: Object.keys(S.meta.point_sets).find((k) => k !== "commute_targets"), mode: "walk", max: 15 }],
    ["amenity", { type: "amenity", name: "gym", match: ["gym", "fitness"] }],
    ["lease term", { type: "equals", field: "term", value: "long" }],
    ["furnished", { type: "equals", field: "furnished", value: true }],
    ["private bath", { type: "equals", field: "private_bath", value: true }],
    ["no restrictions (who may rent)", { type: "equals", field: "restriction", value: "none" }],
    ["parking included", { type: "equals", field: "parking_included", value: true }],
    ["parking spots at least", { type: "min", field: "parking_spots", value: 1 }],
    ["parking cost at most ($/month)", { type: "max", field: "parking_usd_month", value: 100 }],
    ["available for dates", { type: "dates_cover", start: new Date().toISOString().slice(0, 10), end: new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10) }],
    ["field known", { type: "known", field: "sqft" }],
    ["net commute (the must-have commute rules)", { type: "net_commute", agg: $("#agg").value || "total", max: 90 }],
  ];
}
const opt = (v, label, sel) => `<option value="${esc(v)}"${String(v) === String(sel) ? " selected" : ""}>${esc(label)}</option>`;
const FIELD_LABEL = { price: "price", price_month: "price / month", price_per_bed: "price / bed", beds: "beds", baths: "baths",
  sqft: "sqft", term: "term", furnished: "furnished", private_bath: "private bath", kind: "kind", restriction: "who may rent",
  parking_included: "parking included", parking_spots: "parking spots", parking_usd_month: "parking $/month" };
const fieldName = (f) => FIELD_LABEL[f] || f.replace(/_/g, " ");
// one rule row's inputs; each input names the path it sets in data-p ("value", "options.0.max", ...)
function ruleInputs(r) {
  const G = S.goalsData;
  // the width is per kind (style.css .num-*): room for the largest realistic value plus the spinner
  const numIn = (p, v, kind, step = "any") => `<input data-p="${p}" data-num type="number" class="num-${kind}" step="${step}" value="${v ?? ""}">`;
  if (r.type === "max" || r.type === "min") {
    const op = (r.type === "max" ? (r.strict ? "<" : "≤") : (r.strict ? ">" : "≥"));
    return `<select data-p="field">${G.numeric_fields.map((f) => opt(f, fieldName(f), r.field)).join("")}</select>
      <select data-op>${["≤", "<", "≥", ">"].map((o) => opt(o, o, op)).join("")}</select>${numIn("value", r.value, "money")}`;
  }
  if (r.type === "equals") {
    const vals = G.equals_fields[r.field] || [];
    return `<select data-p="field">${Object.keys(G.equals_fields).map((f) => opt(f, fieldName(f), r.field)).join("")}</select> =
      <select data-p="value" data-json>${vals.map((v) => opt(JSON.stringify(v), v === true ? "yes" : v === false ? "no" : v, JSON.stringify(r.value))).join("")}</select>`;
  }
  if (r.type === "net_commute") {
    return `<select data-p="agg">${G.aggs.map((a) => opt(a, a, r.agg)).join("")}</select> of the commute rules ≤ ${numIn("max", r.max, "min", 1)} min`;
  }
  if (r.type === "known") return `<select data-p="field">${G.known_fields.map((f) => opt(f, fieldName(f), r.field)).join("")}</select> known`;
  if (r.type === "size_options") {
    return r.options.map((o, i) => `<span class="sub">${numIn(`options.${i}.beds`, o.beds, "bed", 1)} bd, ≥ ${numIn(`options.${i}.min_baths`, o.min_baths, "bed", 0.5)} ba
      ${r.options.length > 1 ? `<button type="button" class="small" data-rm-opt="${i}" title="remove this size">×</button>` : ""}</span>`).join(" or ")
      + ` <button type="button" class="small" data-add-opt="size" title="another size that also counts">+ or</button>`;
  }
  if (r.type === "dates_cover") return `from <input data-p="start" type="date" value="${esc(r.start)}"> to <input data-p="end" type="date" value="${esc(r.end)}">`;
  if (r.type === "commute_any" || r.type === "near") {
    const pt = r.type === "near" ? `set:${r.set}` : r.target;
    // a mode no longer offered stays selectable where a rule already uses it, so saving keeps it
    const mOpts = (sel) => [...S.meta.modes, ...(sel && !S.meta.modes.includes(sel) ? [sel] : [])]
      .map((m) => opt(m, MODE_LABEL[m] || m, sel)).join("");
    const rows = r.type === "near" ? [{ mode: r.mode, max: r.max }] : r.options;
    return `<select data-pt>${limitOptions().map(([v, l]) => opt(v, l, pt)).join("")}</select> `
      + rows.map((o, i) => `<span class="sub"><select data-p="${r.type === "near" ? "mode" : `options.${i}.mode`}">${mOpts(o.mode)}</select>
        ≤ ${numIn(r.type === "near" ? "max" : `options.${i}.max`, o.max, "min", 1)} min${rows.length > 1 ? ` <button type="button" class="small" data-rm-opt="${i}" title="remove this mode">×</button>` : ""}</span>`).join(" or ")
      + (r.type === "commute_any" ? ` <button type="button" class="small" data-add-opt="mode" title="another mode that also counts">+ or</button>` : "");
  }
  if (r.type === "amenity") {
    const preset = Object.keys(G.amenities).includes(r.name) ? r.name : "";
    return `<select data-amenity>${Object.keys(G.amenities).map((k) => opt(k, k.replace(/_/g, " "), preset)).join("")}${opt("", "other…", preset)}</select>
      ${preset ? "" : `<input data-p="name" value="${esc(r.name)}" placeholder="name" style="width:7em">`}
      <input data-words="match" value="${esc((r.match || []).join(", "))}" placeholder="words, comma separated" title="matches when an amenity entry contains any of these words" style="width:11em">
      not <input data-words="exclude" value="${esc((r.exclude || []).join(", "))}" placeholder="except words" title="an entry containing any of these words never matches (e.g. no parking)" style="width:8em">`;
  }
  return `<code>${esc(JSON.stringify(r))}</code>`;
}
function setPath(o, path, v) {
  const ks = path.split(".");
  const last = ks.pop();
  ks.reduce((a, k) => a[k], o)[last] = v;
}
function renderRuleList(part) {
  return S.draft[part].map((r, i) => `<div class="rule" data-part="${part}" data-i="${i}">
      <span class="rule-kind">${esc(S.goalsData.types[r.type]?.label || r.type)}</span> ${ruleInputs(r)}
      <button type="button" class="small rm" data-rm title="remove">×</button></div>`).join("")
    || `<div class="muted small">none</div>`;
}
function renderGoalEditor() {
  const box = $("#goal-editor");
  if (!S.goalsData) { box.innerHTML = ""; return; }
  const ro = S.meta.readonly;
  if (S.goal === "all" || !S.draft) {
    box.innerHTML = `<div class="row small"><span class="muted">All goals: each place shows its best status. Pick a goal to see and edit its rules.</span>
      ${ro ? "" : `<button type="button" id="g-new" class="small">+ new goal</button>`}</div>`;
  } else {
    const g = S.draft, dirty = draftDirty();
    const tmpl = ruleTemplates().map(([l], i) => `<option value="${i}">${esc(l)}</option>`).join("");
    const add = (part) => ro ? "" : `<select class="g-add" data-part="${part}"><option value="">+ add…</option>${tmpl}</select>`;
    box.innerHTML = `<details id="g-details"${S.editorOpen ? " open" : ""}>
      <summary><b>${esc(g.label)}</b> <span class="muted small">${g.rules.length} must, ${g.nice.length} nice${dirty ? " · unsaved draft" : ""}</span></summary>
      <div class="row small"><label>Name <input id="g-label" value="${esc(g.label)}" style="width:16em"></label>
        <span class="muted">id ${esc(g.id)} · ${esc(savedGoal(g.id)?.origin || "")}</span></div>
      <div class="small"><b>Must have</b> <span class="muted">(fail = any rule fails; needs check = a value is unknown)</span></div>
      ${renderRuleList("rules")}${add("rules")}
      <div class="small"><b>Nice to have</b> <span class="muted">(counted, never excludes)</span></div>
      ${renderRuleList("nice")}${add("nice")}
      ${ro ? "" : `<div class="row small">
        <button type="button" id="g-save" class="accent small"${dirty ? "" : " disabled"}>Save</button>
        <button type="button" id="g-saveas" class="small">Save as new goal</button>
        <button type="button" id="g-revert" class="small"${dirty ? "" : " disabled"}>Revert</button>
        <button type="button" id="g-delete" class="small">Delete goal</button>
        <button type="button" id="g-new" class="small">+ new goal</button></div>`}
      <div id="g-msg" class="small ${S.previewMsg.startsWith("!") ? "err" : "muted"}">${esc(S.previewMsg.replace(/^!/, ""))}</div>
    </details>`;
    $("#g-details").addEventListener("toggle", (e) => { S.editorOpen = e.target.open; });
    if (ro) $$("#goal-editor input, #goal-editor select").forEach((el) => { el.disabled = true; });
    // no re-render on a typed change: it fires on blur, e.g. while the Save button is being clicked
    $("#g-label").addEventListener("change", (e) => { g.label = e.target.value; draftChanged(false); });
    $$("#goal-editor .rule").forEach(wireRule);
    $$("#goal-editor .g-add").forEach((s) => s.addEventListener("change", () => {
      if (s.value === "") return;
      g[s.dataset.part].push(clone(ruleTemplates()[+s.value][1]));
      draftChanged(true);
    }));
  }
  $("#g-save")?.addEventListener("click", saveGoal);
  $("#g-saveas")?.addEventListener("click", saveGoalAsNew);
  $("#g-revert")?.addEventListener("click", () => { S.draft = clone(savedGoal(S.goal)); draftChanged(true); });
  $("#g-delete")?.addEventListener("click", deleteGoal);
  $("#g-new")?.addEventListener("click", newGoal);
}
function wireRule(row) {
  const r = S.draft[row.dataset.part][+row.dataset.i];
  const changed = (rerender) => draftChanged(rerender);
  $("[data-rm]", row)?.addEventListener("click", () => { S.draft[row.dataset.part].splice(+row.dataset.i, 1); changed(true); });
  $$("[data-p]", row).forEach((el) => el.addEventListener("change", () => {
    let v = el.value;
    if (el.dataset.num !== undefined) v = el.value === "" ? null : +el.value;
    if (el.dataset.json !== undefined) v = JSON.parse(v);
    setPath(r, el.dataset.p, v);
    // an equals rule's field picks its first allowed value
    if (r.type === "equals" && el.dataset.p === "field") { r.value = S.goalsData.equals_fields[r.field][0]; return changed(true); }
    changed(false);
  }));
  $("[data-op]", row)?.addEventListener("change", (e) => {
    const o = e.target.value;
    r.type = o === "≤" || o === "<" ? "max" : "min";
    if (o === "<" || o === ">") r.strict = true; else delete r.strict;
    changed(true);
  });
  $("[data-pt]", row)?.addEventListener("change", (e) => {
    const pt = e.target.value;
    const first = r.type === "near" ? { mode: r.mode, max: r.max } : r.options[0];
    for (const k of Object.keys(r)) delete r[k];
    Object.assign(r, pt.startsWith("set:") ? { type: "near", set: pt.slice(4), mode: first.mode, max: first.max }
      : { type: "commute_any", target: pt, options: [first] });
    changed(true);
  });
  $$("[data-add-opt]", row).forEach((b) => b.addEventListener("click", () => {
    r.options.push(b.dataset.addOpt === "size" ? { beds: 3, min_baths: 2 } : { mode: "walk", max: 25 });
    changed(true);
  }));
  $$("[data-rm-opt]", row).forEach((b) => b.addEventListener("click", () => { r.options.splice(+b.dataset.rmOpt, 1); changed(true); }));
  $("[data-amenity]", row)?.addEventListener("change", (e) => {
    const k = e.target.value;
    const preset = S.goalsData.amenities[k];
    for (const key of Object.keys(r)) if (key !== "type") delete r[key];
    Object.assign(r, preset ? { name: k, match: [...preset.match], ...(preset.exclude.length ? { exclude: [...preset.exclude] } : {}) } : { name: "", match: [] });
    changed(true);
  });
  $$("[data-words]", row).forEach((el) => el.addEventListener("change", () => {
    const words = el.value.split(",").map((w) => w.trim()).filter(Boolean);
    if (words.length) r[el.dataset.words] = words; else delete r[el.dataset.words];
    changed(false);
  }));
}
// a draft change: preview it in the list (debounced), re-render the panel when its shape changed
function draftChanged(rerender = true) {
  if (rerender) renderGoalEditor(); else refreshEditorState();
  clearTimeout(draftChanged._t);
  draftChanged._t = setTimeout(previewDraft, 400);
}
function refreshEditorState() {
  const dirty = draftDirty();
  if ($("#g-save")) $("#g-save").disabled = !dirty;
  if ($("#g-revert")) $("#g-revert").disabled = !dirty;
  const m = $("#g-msg");
  if (m) { m.textContent = S.previewMsg.replace(/^!/, ""); m.className = `small ${S.previewMsg.startsWith("!") ? "err" : "muted"}`; }
  const sum = $("#g-details summary .small");
  if (S.draft && $("#g-details summary b")) $("#g-details summary b").textContent = S.draft.label;
  if (sum && S.draft) sum.textContent = `${S.draft.rules.length} must, ${S.draft.nice.length} nice${dirty ? " · unsaved draft" : ""}`;
}
// every place's result for the draft replaces its saved result for that goal (p.saved keeps the server's)
async function previewDraft() {
  const g = S.draft;
  if (!g) return;
  if (!draftDirty()) { applyGoalResults(g.id, null); S.previewMsg = ""; refreshEditorState(); renderList(); return; }
  const seq = (previewDraft.seq = (previewDraft.seq || 0) + 1);
  S.previewMsg = "previewing the draft…"; refreshEditorState();
  try {
    const r = await post("api/goals/preview", { label: g.label, rules: g.rules, nice: g.nice });
    if (seq !== previewDraft.seq || S.draft !== g) return; // a newer edit is on its way
    applyGoalResults(g.id, r.places);
    S.previewMsg = "draft shown in the list; not saved";
  } catch (e) {
    if (seq !== previewDraft.seq) return;
    S.previewMsg = "!" + e.message.replace(/^api\/goals\/preview: HTTP 400[^:]*: /, "");
  }
  refreshEditorState();
  renderList();
}
function applyGoalResults(gid, places) {
  for (const p of S.places) {
    p.saved = p.saved || p.goals;
    if (!places) { p.goals = p.saved; continue; }
    p.goals = { ...p.saved };
    if (places[p.place_key]) p.goals[gid] = places[p.place_key]; else delete p.goals[gid];
  }
}
function selectGoal(id) {
  if (S.draft && draftDirty() && S.draft.id !== id && !confirm(`Drop the unsaved changes to "${S.draft.label}"?`)) return false;
  S.goal = id;
  S.draft = id === "all" ? null : clone(savedGoal(id) || { id, label: id, rules: [], nice: [] });
  S.previewMsg = "";
  if (S.places.length) applyGoalResults(null, null);
  renderGoalSwitch();
  if (goalTerm(S.goal) === "short") $("#sort").value = "price"; // a stay: cheaper is better within its budget
  renderGoalEditor();
  renderList();
  if (S.selected && !$("#drawer").classList.contains("hidden")) openPlace(S.selected); // show only this goal
  return true;
}
function renderGoalSwitch() {
  const gs = $("#goal-switch");
  gs.innerHTML = [`<option value="all">All goals</option>`, ...S.goalsData.goals.map((g) => `<option value="${esc(g.id)}">${esc(g.label || g.id)}</option>`)].join("");
  gs.value = S.goal;
  if (!gs.dataset.bound) {
    gs.dataset.bound = "1";
    gs.addEventListener("change", () => { if (!selectGoal(gs.value)) gs.value = S.goal; }); // kept unsaved edits: stay put
  }
}
// after a save: the goal list, the meta (default target), and every place's saved results
async function reloadGoals(selectId) {
  S.goalsData = await api("api/goals");
  S.meta = await api("api/meta");
  S.previewMsg = "saved; recomputing the list…";
  S.draft = null;
  S.places = []; // nothing to restore: the new list is the saved truth
  renderGoalEditor();
  selectGoal(savedGoal(selectId) ? selectId : "all");
  await loadPlaces();
  S.previewMsg = "";
  refreshEditorState();
}
async function saveGoal() {
  const g = S.draft;
  try {
    await api(`api/goals/${encodeURIComponent(g.id)}`, { method: "PUT", body: JSON.stringify({ label: g.label, rules: g.rules, nice: g.nice }) });
    toast(`Saved "${g.label}" for everyone`);
    await reloadGoals(g.id);
  } catch (e) { toast("Not saved: " + e.message, 6000); }
}
async function createGoal(body) {
  try {
    const r = await post("api/goals", body);
    toast(`Created "${body.label}"`);
    S.draft = null; // the old draft is kept as the new goal; nothing to confirm
    await reloadGoals(r.goal);
  } catch (e) { toast("Not created: " + e.message, 6000); }
}
async function saveGoalAsNew() {
  const label = prompt("Name of the new goal", `${S.draft.label} (copy)`);
  if (!label) return;
  const g = S.draft;
  await createGoal({ label, rules: g.rules, nice: g.nice });
}
async function newGoal() {
  const label = prompt("Name of the new goal (it starts with no rules: every place passes)");
  if (!label) return;
  if (S.draft && draftDirty() && !confirm(`Drop the unsaved changes to "${S.draft.label}"?`)) return;
  await createGoal({ label, rules: [], nice: [] });
}
async function deleteGoal() {
  const g = S.draft;
  if (!confirm(`Delete the goal "${g.label}" for everyone? (It stays in the log and can be created again.)`)) return;
  try {
    await api(`api/goals/${encodeURIComponent(g.id)}`, { method: "DELETE" });
    toast(`Deleted "${g.label}"`);
    S.draft = null;
    await reloadGoals("all");
  } catch (e) { toast("Not deleted: " + e.message, 6000); }
}
// Which attribute a place fails on: each non-pass goal rule arrives as {s, f, v, lim?, pt?/set?, modes?}
// (filter.rules.fact, parallel to reasons). The card marks that attribute red (fail) or amber (needs_check);
// one the card does not show becomes a small chip. Only the selected goal, or every goal under All.
function cardFails(p) {
  const out = [];
  for (const [g, r] of Object.entries(p.goals)) {
    if (S.goal !== "all" && g !== S.goal) continue;
    (r.fails || []).forEach((x, i) => out.push({ ...x, goal: g, text: r.reasons[i] || "", used: false }));
  }
  return out;
}
// ` data-ff="fail|needs_check" title=...` for a fact some failure is about ("" when none), marking those used
function markFor(fails, pred) {
  const hit = fails.filter(pred);
  if (!hit.length) return "";
  hit.forEach((x) => (x.used = true));
  const st = hit.some((x) => x.s === "fail") ? "fail" : "needs_check";
  return ` data-ff="${st}" title="${esc(hit.map((x) => `${x.goal}: ${x.text}`).join("\n"))}"`;
}
const FACT_LABEL = { price_month: "price", price_per_bed: "$/bed", private_bath: "private bath", available: "available from" };
function failChip(x) {
  const fmtv = (v) => (v == null ? "?" : typeof v === "number" ? num(v) : v === true ? "yes" : v === false ? "no" : String(v));
  let t;
  if (x.f === "commute") t = `${x.modes.map((m) => MODE_LABEL[m] || m).join("/")} to ${x.pt.split(":").slice(1).join(":")}`;
  else if (x.f === "near") t = `${x.modes.map((m) => MODE_LABEL[m] || m).join("/")} to nearest ${S.meta.point_sets?.[x.set]?.label || x.set}`;
  else if (x.f === "amenity") t = `no ${x.v}`;
  else if (x.f === "net_commute") t = `net commute (${x.agg}) > ${mins(x.lim)}`;
  else t = `${FACT_LABEL[x.f] || x.f.replace(/_/g, " ")} ${fmtv(x.v)}${x.lim != null && x.s === "fail" && x.v != null ? ` ${x.min ? (x.strict ? "≤" : "<") : (x.strict ? "≥" : ">")} ${num(x.lim)}` : ""}`;
  return `<span class="chip ff-chip" data-ff="${x.s}" title="${esc(`${x.goal}: ${x.text}`)}">${esc(t)}</span>`;
}

/* ---------------------------------------------------------------- list */
function filtered() {
  const q = $("#q").value.trim().toLowerCase();
  const sts = new Set($$(".st").filter((c) => c.checked).map((c) => c.value));
  const showInactive = $("#inactive").checked;
  // what a place must have is the goal's rules; the list shows the places whose status is ticked
  return S.places.filter((p) => {
    if (!sts.has(placeStatus(p))) return false;
    if (!showInactive && !p.n_active) return false;
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
    price: (p) => [nil(goalTerm(S.goal) === "short" ? p.min_stay_month : goalTerm(S.goal) === "long" ? p.min_lease_month : p.min_price_month)],
    ppb: (p) => [nil(goalTerm(S.goal) === "long" ? p.min_lease_per_bed : p.min_price_per_bed)],
    beds: (p) => [-(p.max_beds || 0)],
    // complete sums first, then those with unknown terms (by what is known), then none
    net: (p) => { const n = netCommute(p); return n && n.v != null ? [n.known < n.n ? 1 : 0, n.v] : [2, 0]; },
    rating: (p) => [-(p.rating || 0)], nice: (p) => [-(Math.max(0, ...Object.values(p.goals).map((g) => g.nice_score || 0)))],
  };
  const f = fns[key];
  return [...list].sort((a, b) => {
    const ka = f(a), kb = f(b);
    for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] < kb[i] ? -1 : 1;
    return 0;
  });
}

// Lease rent and furnished-stay prices are different offers: a goal whose term rule is short shows stays, long
// shows leases, any other goal and All both. A price failure marks the price shown for its goal's offer (a
// short-term goal: the stay, otherwise the lease); when the
// deciding listing's price differs from the minimum on the card, its own price follows in brackets.
function priceFacts(p, fails = []) {
  const term = goalTerm(S.goal);
  const showLease = p.min_lease_month != null && term !== "short";
  const showStay = p.min_stay_month != null && term !== "long";
  const forStay = (x) => (goalTerm(x.goal) === "short" || !showLease) && showStay;
  const mark = (field, shown, pick) => {
    const mk = markFor(fails, (x) => x.f === field && pick(x));
    const own = fails.find((x) => x.f === field && pick(x) && x.v != null && Math.round(x.v) !== Math.round(shown));
    return [mk, own ? ` <small>(${money(own.v)})</small>` : ""];
  };
  let lease = "", stay = "";
  if (showLease) {
    const [lm, lmv] = mark("price_month", p.min_lease_month, (x) => !forStay(x));
    const [lb, lbv] = mark("price_per_bed", p.min_lease_per_bed, (x) => !forStay(x));
    lease = `<span${lm}><b>${money(p.min_lease_month)}</b>/mo${lmv}</span><span${lb}><b>${money(p.min_lease_per_bed)}</b>/bed${lbv}</span>`;
  }
  if (showStay) {
    const [sm, smv] = mark("price_month", p.min_stay_month, forStay);
    stay = `<span${sm || ' title="furnished short stay"'}>stay <b>${money(p.min_stay_month)}</b>/mo${smv}</span>`;
  }
  if (term === "short") return stay || `<span class="muted">no furnished stay</span>`;
  if (term === "long") return lease || `<span class="muted">no lease price</span>`;
  if (lease + stay) return lease + stay;
  const [am, amv] = mark("price_month", p.min_price_month, () => true);
  return `<span${am}><b>${money(p.min_price_month)}</b>/mo${amv}</span>`;
}

// A limit on who may rent that a listing here states (extract's reading, filter.place_restriction): a badge with the
// quote on hover; red / amber when the selected goal's restriction rule fails on it (`fails`, like other facts)
const RESTRICTION_LABEL = { income: "income-restricted", senior: "seniors only", veterans: "veterans only",
  student: "students only", employer: "employees only", other: "restricted" };
function restrictionBadge(rx, fails = []) {
  if (!rx) return "";
  const mk = markFor(fails, (x) => x.f === "restriction");
  return `<span class="chip rx-chip"${mk || ` title="${esc(`"${rx.evidence || ""}" (${rx.source})`)}"`}>${esc(RESTRICTION_LABEL[rx.value] || rx.value)}</span>`;
}

// Your answer on who may rent this place (annotation place / restriction, the same one Verify writes): it beats
// every source's reading in the goals' restriction rule. The values are the rule's own (api/goals equals_fields).
function whoMayRent(d, p) {
  const vals = S.goalsData?.equals_fields?.restriction;
  if (!vals || S.meta.readonly) return "";
  const mine = [...d.annotations].reverse().find((a) => a.attribute === "restriction" && a.comment)?.comment;
  const read = p.restriction ? `read: ${RESTRICTION_LABEL[p.restriction.value] || p.restriction.value} ("${p.restriction.evidence || ""}", ${p.restriction.source})` : "no limit read from any source";
  return `<div class="small">Who may rent: <select id="rx-set"><option value="">${mine ? "" : "not answered"}</option>
    ${vals.map((v) => opt(v, v === "none" ? "no limit (anyone)" : RESTRICTION_LABEL[v] || v, mine)).join("")}</select>
    <span class="muted">${mine ? "your answer" : esc(read)}</span></div>`;
}

// Parking as the most informative listing here states it (filter.place_parking): "🅿 1 covered, $75/mo", the quotes on
// hover; red / amber when the selected goal's parking rule fails or needs a check on it (`fails`, like other facts)
const PARKING_KIND = { off_street: "off-street", uncovered: "open", ev_charging: "EV" };
function parkingText(pk) {
  const kinds = (pk.kinds || []).filter((k) => k !== "street").map((k) => PARKING_KIND[k] || k).join("/");
  if (pk.offered === "no") return (pk.kinds || []).includes("street") ? "street parking only" : "no parking";
  const what = [pk.spots != null ? pk.spots : "", kinds].filter((x) => x !== "").join(" ") || "parking";
  const cost = pk.cost === "included" ? "included" : pk.cost === "paid" ? (pk.usd_month != null ? `${money(pk.usd_month)}/mo` : "paid")
    : "cost unspecified";
  return `${what}, ${cost}${pk.offered == null ? " (offered?)" : ""}`;
}
function parkingFact(pk, fails = []) {
  const mk = markFor(fails, (x) => x.f.startsWith("parking_"));
  if (!pk) return mk ? `<span${mk}>🅿 ?</span>` : "";
  return `<span${mk || ` title="${esc(`"${(pk.evidence || []).join(" | ")}" (${pk.source})`)}"`}>🅿 ${esc(parkingText(pk))}</span>`;
}

function stars(n) { return n ? `<span class="stars" title="${n}/5">${"★".repeat(n)}${"☆".repeat(5 - n)}</span>` : ""; }

function renderList() {
  const list = sorted(filtered());
  drawMarkers(list);
  // totals by status for the selected goal (All: each place's best), then how many the list shows if some are hidden
  const tot = { pass: 0, needs_check: 0, fail: 0 };
  S.places.forEach((p) => { tot[placeStatus(p)]++; });
  $("#count").innerHTML = `${num(S.places.length)} places · <b class="n-pass">${num(tot.pass)}</b> pass · `
    + `<b class="n-check">${num(tot.needs_check)}</b> check · <b class="n-fail">${num(tot.fail)}</b> fail`
    + (list.length < S.places.length ? ` · showing ${num(list.length)}` : "");
  $("#cards").innerHTML = list.map((p) => {
    const st = placeStatus(p);
    const chips = Object.entries(p.goals).filter(([g]) => S.goal === "all" || g === S.goal).map(([g, r]) => `<span class="chip ${r.status}" title="${esc(r.reasons.join("\n"))}">${esc(g)}: ${esc(STATUS_LABEL[r.status])}</span>`).join("");
    const fails = cardFails(p);
    const bm = markFor(fails, (x) => x.f === "beds"), am = markFor(fails, (x) => x.f === "baths");
    const bb = p.beds.length ? `<span${bm}>${esc(p.beds.map((b) => (b === 0 ? "studio" : b + "bd")).join("/"))}</span> · <span${am}>${esc(p.baths.join("/"))}ba</span>`
      : `<span${bm || am}>beds ?</span>`;
    const facts = `${priceFacts(p, fails)}
        <span>${bb}</span>
        ${limitFacts(p, fails)}${netFact(p, fails)}${parkingFact(p.parking, fails)}`;
    const rxb = restrictionBadge(p.restriction, fails);
    const extra = rxb + fails.filter((x) => !x.used).map(failChip).join("");
    return `<div class="card ${st === "fail" ? "dim" : ""} ${S.selected === p.place_key ? "sel" : ""}" data-pk="${esc(p.place_key)}">
      <h4>${esc(p.label)} ${stars(p.rating)}</h4>
      <div class="facts">
        ${facts}
        <span>${esc(p.sources)}</span>${p.n_active ? "" : '<span style="color:var(--bad)">inactive</span>'}
        ${p.user_status ? `<span>status: ${esc(p.user_status)}</span>` : ""}
      </div>
      <div class="chips">${chips}${extra}</div>
    </div>`;
  }).join("");
  $$(".card").forEach((el) => {
    el.addEventListener("click", () => openPlace(el.dataset.pk));
    if (CAN_HOVER) {
      el.addEventListener("mouseenter", () => hiMarker(el.dataset.pk, true));
      el.addEventListener("mouseleave", () => hiMarker(el.dataset.pk, false));
    }
  });
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
      const legs = (v.legs || []).map((l) => `${l.mode}${l.from || l.to ? ` ${l.from || ""}${l.to ? "→" + l.to : ""}` : ""}: ${mins(l.minutes)}m`).join("\n");
      return `<td class="${srcOf(v.provider)[0]}" title="${esc(srcTitle(v))}${legs ? "\n" + esc(legs) : ""}">${srcText(v)}</td>`;
    }).join("");
    return `<tr><th>${esc(t.split(":")[1])}</th>${cells}</tr>`;
  }).join("");
  return `<div class="scroll-x"><table><tr><th></th>${modes.map((m) => `<th>${esc(MODE_LABEL[m])}</th>`).join("")}</tr>${rows}</table></div>
    <p class="muted small">Minutes, coloured by source: ${SRC_LEGEND}. Hover a cell for its source and legs.</p>`;
}

function nearestBlock(d) {
  const entries = Object.entries(d.nearest);
  if (!entries.length) return '<p class="muted small">Not computed.</p>';
  return entries.map(([k, list]) => {
    const [set, mode] = k.split("|");
    const label = S.meta.point_sets[set]?.label || set;
    return `<div class="small"><b>${esc(label)}</b> (${esc(mode)}): ${list.map((x) => `${esc(x.point)} ${srcMin(x)} min`).join(", ")}</div>`;
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

// off the market (view withdrawals): the user's mark, with its undo, or a Zillow page that says removed or sold
function goneNote(l) {
  if (!l.gone_at) return "";
  const who = l.gone_by === "user" ? `you marked it gone on ${esc(l.gone_at.slice(0, 10))}`
    : `Zillow's page: ${esc(l.gone_why || "removed")} (read ${esc(l.gone_at.slice(0, 10))})`;
  return `<br><span class="small">${who} <a href="#" data-gone="${esc(l.source)}|${esc(l.source_id)}" data-gone-set="0">${l.gone_by === "user" ? "undo" : "still for rent"}</a></span>`;
}

async function openPlace(pk) {
  S.selected = pk;
  $$(".card").forEach((c) => c.classList.toggle("sel", c.dataset.pk === pk));
  const m = S.markers[pk];
  if (m) map.panTo(m.getLatLng());
  const d = await api(`api/places/${encodeURIComponent(pk)}`);
  const p = d.place;
  const label = p.label || d.listings.find((l) => l.fields.title)?.fields.title || pk;
  // the net commute's trips (the list's own terms for this place, so the drawer and the card read the same list)
  const lp = S.places.find((x) => x.place_key === pk);
  const goals = Object.entries(d.goals).filter(([g, r]) => r && (S.goal === "all" || g === S.goal)).map(([g, r]) => {
    const nt = netTrips(lp?.goals?.[g]?.commute);
    const ntTip = nt.length ? ` title="${esc(`trips in the net commute:\n${nt.join("\n")}`)}"` : "";
    return `
    <div><span class="chip ${r.status}">${esc(g)}: ${esc(STATUS_LABEL[r.status])}</span>${r.own_site ? ' <span class="small muted" title="The building\'s own website prices offers here, so only its listings decide the goals; other sources are shown as \'also listed\'.">judged on the own website</span>' : ""}
      ${r.reasons.map(([s, t, ty]) => `<div class="reason ${s}"${ty === "net_commute" ? ntTip : ""}>${s === "pass" ? "✓" : s === "fail" ? "✗" : "?"} ${esc(t)}</div>`).join("")}
      ${nt.length ? `<details class="net-trips small"><summary>net commute: ${nt.length} trip${nt.length > 1 ? "s" : ""}</summary>${nt.map((x) => `<div>${esc(x)}</div>`).join("")}</details>` : ""}
      ${r.nice.length ? `<div class="small muted">nice to have: ${r.nice.map(([s, t]) => `${s === "pass" ? "✓" : s === "fail" ? "✗" : "?"} ${esc(t)}`).join(" · ")}</div>` : ""}
    </div>`;
  }).join("");
  const photos = (d.photos || []).map((ph) => `<figure class="ph"><a href="${esc(ph.page || ph.url)}" target="_blank" rel="noopener noreferrer" title="Open the ${esc(ph.site || ph.source)} page this photo is from"><img loading="lazy" src="${esc(ph.url)}" alt="photo from ${esc(ph.site || ph.source)}" onerror="this.closest('figure').remove()"></a>${ph.page ? `<a class="src" href="${esc(ph.page)}" target="_blank" rel="noopener noreferrer" title="Open the page this photo came from">${esc(ph.site || ph.source)} ↗</a>` : `<span class="src">${esc(ph.source)}</span>`}</figure>`)
    .concat(d.listings.flatMap((l) => l.media.map((f) => `<a href="media/${encodeURIComponent(f)}" target="_blank" rel="noopener"><img src="media/${encodeURIComponent(f)}" alt="file from ${esc(l.source)}"></a>`)));
  // other sources' prices beside the own site's: "also listed at $X on zillow"
  const alsoListed = (pl) => (pl.also_listed || []).length ? `<br><span class="muted">${pl.also_listed.map((a) =>
    `also listed${a.price_month ? ` at ${money(a.price_month)}` : ""} on ${esc(a.source)}`).join("<br>")}</span>` : "";
  const rng = (a, f) => (!a ? '<span class="muted">?</span>' : a[0] === a[1] ? f(a[0]) : `${f(a[0])}–${f(a[1])}`);
  // leases and furnished offers (the building's own furnished option, operators' furnished stays) never mix
  const planRows = (plans) => `<div class="scroll-x"><table>
    <tr><th>plan</th><th>bd / ba</th><th>sqft</th><th>$/month</th><th>$/bed</th><th>available</th><th>earliest</th><th>sources</th><th>drawing</th></tr>
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
      <td class="muted">${esc((l.first_seen || "").slice(5, 10))}→${esc((l.last_seen || "").slice(5, 10))}${l.active ? "" : " inactive"}${goneNote(l)}</td>
      <td class="nowrap"><button class="small" data-raw="${i}" title="Show what we parsed and what the source sent">raw</button>
        ${l.active && f.kind !== "site_info" ? `<button class="small" data-gone="${esc(l.source)}|${esc(l.source_id)}" data-gone-set="1" title="Record that this listing is withdrawn: it leaves the list until a newer scrape sees it again">no longer for rent</button>` : ""}
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
    <h2>${esc(label)} ${restrictionBadge(p.restriction)} ${stars(d.annotations.filter((a) => a.attribute === "overall" && a.rating).slice(-1)[0]?.rating)}</h2>
    ${(d.websites || []).length ? `<div class="website">${d.websites.map((w) => `<a href="${esc(w.url)}" target="_blank" rel="noopener noreferrer">🌐 ${esc(w.site)} ↗</a>`).join(" ")}</div>` : '<div class="website muted small">no website found yet</div>'}
    <div class="muted small">${esc(p.address || "address unknown")} ${p.city ? "· " + esc(p.city) : ""} · ${esc(pk)}</div>
    ${p.parking ? `<div class="small facts">${parkingFact(p.parking)}</div>` : ""}
    <h3>Goals</h3>${goals}
    ${whoMayRent(d, p)}
    ${photos.length ? `<h3>Photos (${photos.length})</h3><div class="media">${photos.join("")}</div>` : '<p class="muted small">No photos from any source.</p>'}
    ${relatedBox}
    ${(d.nearby || []).length ? `<details class="merge small"><summary>Same building as a nearby place? Merge…</summary>
      ${d.nearby.map((n) => `<div><button class="small" data-merge-into="${esc(n.place_key)}">merge into</button>
        ${esc(n.label)} <span class="muted">${esc(n.address || "")} · ${n.metres} m · ${n.n_listings ?? 0} listings</span></div>`).join("")}
    </details>` : ""}
    <h3>${isStay ? "Furnished stays" : "Floor plans"}</h3>${planTable}
    ${listingTable}
    <h3>Price history (per month)</h3>${historyChart([...d.listings].sort((a, b) => b.history.length - a.history.length).slice(0, 8))}
    <h3>Commute <button id="route-btn" class="small">${S.meta.google ? "Route with Google" : "Recompute (estimate)"}</button></h3>
    <p id="route-status" class="small">${esc(S.routeMsg?.[pk] || "")}</p>
    ${S.meta.google ? "" : '<p class="muted small">No GOOGLE_MAPS_API_KEY on the server: only straight-line estimates can be computed here.</p>'}
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
  $("#rx-set")?.addEventListener("change", async (e) => {
    if (!e.target.value) return;
    try {
      await post("api/annotations", { entity_type: "place", entity_key: pk, attribute: "restriction", comment: e.target.value });
      toast(`Who may rent: ${e.target.selectedOptions[0].textContent}`); await refreshAfterWrite(pk);
    } catch (err) { toast("Not saved: " + err.message, 6000); }
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
  $$("#drawer [data-merge-into]").forEach((b) => b.addEventListener("click", async () => {
    const into = b.dataset.mergeInto;
    if (!confirm(`Merge every listing here into "${b.parentElement.textContent.replace("merge into", "").trim().split("\n")[0]}"? (undo per listing with split)`)) return;
    try {
      const r = await post(`api/places/${encodeURIComponent(pk)}/merge`, { into });
      toast(`Merged ${r.merged} listings`); await loadPlaces(); openPlace(into);
    } catch (err) { toast("Merge failed: " + err.message, 6000); }
  }));
  $$("#drawer [data-gone]").forEach((b) => b.addEventListener("click", async (ev) => {
    ev.preventDefault();
    const [source, ...rest] = b.dataset.gone.split("|"), gone = b.dataset.goneSet === "1";
    try {
      await post("api/listings/gone", { source, source_id: rest.join("|"), gone, queue: "adhoc" });
      toast(gone ? "Marked no longer for rent" : "Undone: back on the list"); await loadPlaces(); openPlace(pk);
    } catch (err) { toast("Failed: " + err.message, 6000); }
  }));
  $$("#drawer [data-split]").forEach((b) => b.addEventListener("click", async () => {
    const [source, source_id] = b.dataset.split.split("|");
    if (!confirm(`Record that ${source} ${source_id} is a different place from the others here?`)) return;
    const r = await post("api/links", { source, source_id, place_key: null });
    toast("Split recorded"); await loadPlaces(); openPlace(r.place_key);
  }));
  // the outcome stays on the drawer (S.routeMsg) across its re-render; the button always leaves "Routing…"
  $("#route-btn")?.addEventListener("click", async (e) => {
    const btn = e.target, label = btn.textContent;
    const say = (msg) => { (S.routeMsg ||= {})[pk] = msg; const el = $("#route-status"); if (el && S.selected === pk) el.textContent = msg; };
    btn.disabled = true; btn.textContent = "Routing…"; say("");
    let r;
    try {
      r = await post(`api/trips/${encodeURIComponent(pk)}${S.meta.google ? "" : "?provider=estimate"}`);
    } catch (err) {
      say((/cap/i.test(err.message) ? "Google limit reached, estimates kept: " : "Routing failed: ") + err.message);
      toast(S.routeMsg[pk], 6000);
      btn.disabled = false; btn.textContent = "Retry routing";
      return;
    }
    const src = r.provider === "google" ? "Google" : "estimate";
    say(r.new_trips ? `Routed with ${src}: ${r.new_trips} new trips, ${r.api_calls} Google calls.`
      : `Routed with ${src}: no changes (${r.api_calls} Google calls; cached routes already shown).`);
    toast(S.routeMsg[pk]);
    try { await refreshAfterWrite(pk); }
    catch (err) { say(S.routeMsg[pk] + " Reloading the drawer failed: " + err.message); }
    finally { const b = $("#route-btn"); if (b && b.textContent === "Routing…") { b.disabled = false; b.textContent = label; } }
  });
}

async function refreshAfterWrite(pk) {
  await loadPlaces();
  if (pk) await openPlace(pk);
}

/* ---------------------------------------------------------------- verify mode */
// Verify keeps its screen. An answer changes only its own item, at once (the request runs behind it; a failure puts
// the item back with the error next to it); nothing in the background redraws the screen; closing it (x, Escape, a
// place link) only hides it, so the Verify button shows the same screen at the same scroll again. A screen is fetched
// only by Next / Previous / Refresh / a filter / a jump to another list, and the old one stays (dimmed) until it is in.
const V = { seq: 0, inflight: new Set(), scroll: 0, shown: false, dirty: false };
const vTrack = (p) => { V.inflight.add(p); p.catch(() => {}).finally(() => V.inflight.delete(p)); return p; };
const vSettle = () => Promise.allSettled([...V.inflight]);
async function vLoad(path) {
  const body = $("#verify-body"), my = ++V.seq;
  $(".v-loaderr", body)?.remove();
  if (body.children.length) body.classList.add("loading"); else body.innerHTML = '<p class="muted v-loading">Loading…</p>';
  try {
    await vSettle();  // answers still in flight change what the next screen holds
    const d = await api(path);
    return my === V.seq ? d : null;  // a newer request owns the screen
  } catch (e) {
    if (my === V.seq) { $(".v-loading", body)?.remove(); body.insertAdjacentHTML("afterbegin", `<p class="reason fail v-loaderr">Not loaded: ${esc(e.message)}</p>`); }
    return null;
  } finally { if (my === V.seq) body.classList.remove("loading"); }
}
// a move to another screen (asked for): wait for answers in flight, fetch it, start at its top, update the header counts
async function vPage(go) { await vSettle(); await go(); $("#verify").scrollTop = 0; loadQueueStatus().catch(() => {}); }
async function openVerify() {
  const ov = $("#verify");
  ov.classList.remove("hidden");
  if (V.shown) { ov.scrollTop = V.scroll; return; }  // the screen as it was left
  V.shown = true;
  S.verifyMode = "conflict"; S.conflictSkip = 0; S.verifySkip = 0;
  await renderVerify();
}
function closeVerify() {
  const ov = $("#verify");
  if (ov.classList.contains("hidden")) return;
  V.scroll = ov.scrollTop;
  ov.classList.add("hidden");
  if (V.dirty) { V.dirty = false; loadPlaces().catch(() => {}); loadQueueStatus().catch(() => {}); }  // the list and map pick up the answers
}
// an inline error right after an item's buttons ("" clears it)
function vErr(after, msg) {
  let el = after.nextElementSibling;
  if (!el?.classList.contains("v-err")) {
    if (!msg) return;
    el = document.createElement("span"); el.className = "reason fail small v-err"; after.after(el);
  }
  if (msg) el.textContent = " " + msg; else el.remove();
}
// keyboard focus to the next item still open (its first unanswered value), else the Next / Continue button; never scrolls
function vFocusNext(from) {
  const all = $$("#verify-body .vconf"), i = all.indexOf(from);
  const open = (b) => !b.classList.contains("done") && !b.classList.contains("answered") && !b.classList.contains("gone");
  const next = [...all.slice(i + 1), ...all.slice(0, Math.max(i, 0))].find(open);
  const vb = next && $$(".vbtns:not(.opt)", next).find((v) => !$("button.on", v));
  const target = next ? (vb ? $("button", vb) : $("button:not([disabled])", next)) : $("#v-more, #d-more, #o-more") || $("#v-rest, #d-rest, #o-rest");
  target?.focus({ preventScroll: true });
}
// a conflict block is checked once each value it needs has a verdict: dimmed, and focus moves on
function vMark(block) {
  if (!block) return;
  const was = block.classList.contains("done"), now = $$(".vbtns:not(.opt)", block).every((v) => $("button.on", v));
  block.classList.toggle("done", now);
  if (now && !was) vFocusNext(block);
}
// a whole item answered in place: it folds to a one-line stub at once, the request runs behind it, a failure unfolds
// it with the error; `stays`: the answer does not take it off its queue (paging counts it as still there)
function vAnswer(box, stub, req, { stays = false, then } = {}) {
  vErr(box, "");
  const s = document.createElement("div"); s.className = "v-stub small"; s.innerHTML = stub; box.prepend(s);
  box.classList.add("answered"); box.classList.toggle("stays", stays); V.dirty = true;
  vFocusNext(box);
  vTrack(req()).then((r) => then?.(r, s), (e) => { s.remove(); box.classList.remove("answered", "stays"); vErr(box, "Not saved: " + e.message); });
}
const vLeft = (body) => $$(".vconf.answered:not(.stays)", body).length;

const CONFLICT_PAGE = 10;
const gotoPlace = (pk) => { closeVerify(); openPlace(pk); };

/* a too-cheap item's third answer: the rent is real because the place limits who may rent (annotation place /
   restriction, read by the goals' `restriction` rule); the item leaves the queue */
const rxAsk = (it) => (it.cheap && it.place_key ? `<div class="small">Or the place limits who may rent:
  <button class="small" data-rx="${esc(it.place_key)}|income" title="Income-restricted / affordable housing (BMR, AMI limits, tax credit): sets this place's restriction to income">income-restricted</button>
  <button class="small" data-rx="${esc(it.place_key)}|other" title="Another limit on who may rent (seniors, students, employees, ...): sets this place's restriction to other">other restriction</button></div>` : "");

/* conflicts, ten to a screen: one block per conflict, a row per disagreeing field, a column per source */
function conflictBlock(e) {
  const it = e.item, obs = e.observations;
  const unit = it.unit_key ? it.unit_key.split("/").slice(1).join("/") : "";
  const tags = [
    it.pending_render ? `<span class="chip pending" title="${esc(it.pending_reason || "")}">own-site render pending: will update this</span>` : "",
    it.text ? `<span class="chip">source vs its own text</span>` : "",
    it.cheap ? `<span class="chip needs_check">too cheap?</span>` : "",
    it.stale ? `<span class="chip needs_check">no longer for rent?</span>` : "",
  ].join("");
  const cheap = it.cheap ? `<div class="small">${money(it.price_month)} for ${it.beds ? `${num(it.beds)} bd` : "a studio"} = <b>${money(it.per_bed)}/bed</b> · ${esc(it.reason)}</div>` : it.stale ? `<div class="small">${esc(it.reason)} <button class="small" data-gone="${esc(obs[0].source)}|${esc(obs[0].source_id)}" title="Record that this listing is withdrawn: it leaves the list until a newer scrape sees it again">no longer for rent</button></div>` : "";
  const textTh = it.text ? `<th>${esc(it.text_label)}<br><span class="muted">read from the text</span></th>` : "";
  const textTd = (f) => (it.text ? `<td>${fmtField(f, it.text[f])}</td>` : "");
  return `<div class="vconf${it.pending_render ? " pending" : ""}">
    <div class="vconf-head"><a href="#" data-goto="${esc(it.place_key)}">${esc(e.label || it.place_key)}</a>
      ${unit ? `<span class="muted small">unit ${esc(unit)}</span>` : ""} ${tags}</div>${cheap}${rxAsk(it)}
    <div class="scroll-x"><table class="vconf-t"><tr><th></th>${obs.map((o) => `<th><a href="${esc(o.url)}" target="_blank" rel="noopener noreferrer">${esc(o.source)} ↗</a> <span class="muted">${esc((o.observed_at || "").slice(0, 10))}</span></th>`).join("")}${textTh}</tr>
    ${e.fields.map((f) => `<tr><th>${esc(f)}</th>${obs.map((o) => `<td><b>${fmtField(f, o.fields[f])}</b> ${vbtns({ observation_id: o.obs_id, field: f, queue: "conflict" }, (o.checked || {})[f], it.cheap && f !== "price")}</td>`).join("")}${textTd(f)}</tr>`).join("")}
    </table></div></div>`;
}

/* the conflict filter: a goal ("" = all conflicts) and the walk zones; remembered per browser, else the header goal */
function conflictFilter() {
  const goals = (S.goalsData?.goals || []).map((g) => g.id);
  let goal = lsGet("aptf.conflictGoal");
  if (goal == null) goal = S.goal && S.goal !== "all" ? S.goal : "";
  if (goal && !goals.includes(goal)) goal = "";  // a goal since deleted
  return { goal, zone: lsGet("aptf.conflictZone") || "" };
}

async function renderConflicts() {
  const body = $("#verify-body");
  const cf = conflictFilter();
  const d = await vLoad(`api/verify/batch?kind=conflict&skip=${S.conflictSkip}&n=${CONFLICT_PAGE}&goal=${encodeURIComponent(cf.goal)}&zone=${cf.zone}`);
  if (!d) return;
  if (!d.items.length && S.conflictSkip > 0 && d.total > 0) { S.conflictSkip = Math.max(0, d.total - CONFLICT_PAGE); return renderConflicts(); }
  const filtered = cf.goal || cf.zone;
  if (!d.items.length && !filtered) { S.verifyMode = "dup"; S.dupSkip = 0; return renderVerify(); }
  const from = d.items.length ? S.conflictSkip + 1 : 0, to = S.conflictSkip + d.items.length;
  const more = to < d.total;
  const gname = (id) => { const g = (S.goalsData?.goals || []).find((x) => x.id === id); return g ? g.label || g.id : id; };
  const h = d.hidden || {}, nGoal = (h.irrelevant || 0) + (h.settled || 0) + (h.same || 0);
  const why = ["irrelevant", "settled", "same"].filter((k) => h[k]).map((k) => `${h[k]} ${esc(d.hidden_labels[k])}`).join("; ");
  const hid = [cf.goal ? `${nGoal} hidden as irrelevant to ${esc(gname(cf.goal))}${why ? ` (${why})` : ""}` : "",
    cf.zone ? `${h.zone || 0} outside the station walk zones` : ""].filter(Boolean).join("; ");
  const filterBar = `<p class="small v-filter">Relevant to goal: <select id="v-goal"><option value="">All conflicts</option>${(S.goalsData?.goals || []).map((g) => `<option value="${esc(g.id)}"${g.id === cf.goal ? " selected" : ""}>${esc(g.label || g.id)}</option>`).join("")}</select>
    <label><input type="checkbox" id="v-zone"${cf.zone === "walk" ? " checked" : ""}> only within the station walk zones</label><br>
    <span class="muted">Showing ${d.total} of ${d.all_total} conflicts${hid ? `: ${hid}` : ""}. A goal keeps a conflict only if picking a different source's value for the disputed fields could change the listing's result for that goal (pass, needs check or fail; its commute limits included).</span></p>`;
  body.innerHTML = `<h2>${d.total} conflicts: ${d.actionable} to check (${d.cheap} priced too low per bedroom), ${d.pending} waiting for an own-site render</h2>
    ${filterBar}
    <p class="small">Sources disagree on these units. Open each listing, then mark every stored value: ✓ correct, ✗ wrong (type the true value), ? cannot tell. Listings tagged <i>no longer for rent?</i>: ✓ the price if still offered, else <b>no longer for rent</b>. Listings tagged <i>too cheap?</i> are priced below the per-bedroom floor: a per-room or misread price? Mark the price ✓ if it is real, ✗ with the true monthly rent if not. Conflicts tagged <i>own-site render pending</i> come last: the building's own site will be read again and will likely settle them.</p>
    <p class="muted small">Showing ${from}–${to} of ${d.total} · <a href="#" id="v-dups">possible duplicate buildings (${S.status?.queues?.duplicates ?? "…"})</a></p>
    ${d.items.map(conflictBlock).join("")}
    <p style="margin-top:14px"><button id="v-prev" ${S.conflictSkip ? "" : "disabled"}>Previous ${CONFLICT_PAGE}</button>
      ${more ? `<button id="v-more" class="accent">Next ${CONFLICT_PAGE}</button>` : ""}
      <button id="v-rest" class="${more ? "" : "accent"}">${more ? "Skip to" : "Continue:"} possible duplicate buildings, then own sites and the sample</button></p>`;
  $$("[data-rx]", body).forEach((b) => b.addEventListener("click", async () => {
    const [pk, value] = b.dataset.rx.split("|");
    try {
      await post("api/annotations", { entity_type: "place", entity_key: pk, attribute: "restriction", comment: value });
      $$("[data-rx]", body).filter((x) => x.dataset.rx.split("|")[0] === pk).forEach((x) => x.closest(".vconf").classList.add("gone"));
      b.parentElement.replaceChildren(Object.assign(document.createElement("b"), { textContent: `marked ${value === "income" ? "income-restricted" : "restricted (other)"}: this place's restriction is now ${value}` }));
      loadQueueStatus();
    } catch (err) { toast("Not saved: " + err.message, 6000); }
  }));
  $$(".vbtns", body).forEach(wireVbtns);
  $$("[data-goto]", body).forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); gotoPlace(a.dataset.goto); }));
  // a stale item answered "it's gone": a user check on `active` (POST api/listings/gone); the item leaves the queue
  $$("[data-gone]", body).forEach((b) => b.addEventListener("click", async () => {
    const [source, ...rest] = b.dataset.gone.split("|");
    try {
      await post("api/listings/gone", { source, source_id: rest.join("|"), gone: true, queue: "conflict" });
      b.closest(".vconf").classList.add("gone");
      b.replaceWith(Object.assign(document.createElement("b"), { textContent: "marked no longer for rent (undo in the place drawer)" }));
      loadQueueStatus();
    } catch (err) { toast("Failed: " + err.message, 6000); }
  }));
  $("#v-goal").addEventListener("change", (ev) => { lsSet("aptf.conflictGoal", ev.target.value); S.conflictSkip = 0; renderConflicts(); });
  $("#v-zone").addEventListener("change", (ev) => { lsSet("aptf.conflictZone", ev.target.checked ? "walk" : ""); S.conflictSkip = 0; renderConflicts(); });
  $("#v-prev").addEventListener("click", () => vPage(() => { S.conflictSkip = Math.max(0, S.conflictSkip - CONFLICT_PAGE); return renderConflicts(); }));
  $("#v-more")?.addEventListener("click", () => vPage(() => {
    // a conflict whose every value got a verdict has left the queue, so the next page starts that much earlier
    // (counted after the answers in flight settle: a failed one is unpressed again)
    const done = $$(".vconf", body).filter((b) => b.classList.contains("gone") || $$(".vbtns:not(.opt)", b).every((v) => $("button.on", v))).length;
    S.conflictSkip += d.items.length - done; return renderConflicts();
  }));
  const toDups = (ev) => { ev?.preventDefault(); vPage(() => { S.verifyMode = "dup"; S.dupSkip = 0; return renderVerify(); }); };
  $("#v-rest").addEventListener("click", toDups);
  $("#v-dups").addEventListener("click", toDups);
}

async function renderVerify() {
  if (S.verifyMode === "conflict") return renderConflicts();
  if (S.verifyMode === "own") return renderOwnSites();
  if (S.verifyMode === "dup") return renderDuplicates();
  const body = $("#verify-body");
  const d = await vLoad(`api/verify/next?seed=${S.verifySeed}&skip=${S.verifySkip}`);
  if (!d) return;
  if (d.done) { body.innerHTML = "<h2>Queue empty</h2><p>Nothing left to check. 🎉</p>"; return; }
  const it = d.item;
  const head = `<p class="muted small">${d.remaining} items in queue (an even sample across sources) · item ${S.verifySkip + 1} · <a href="#" id="v-back">back to the conflicts</a></p>`;
  const nav = `<p style="margin-top:14px"><button id="v-skip">Skip</button> <button id="v-next" class="accent">Next</button></p>`;
  if (it.kind === "trip") {
    const t = d.trip;
    const tm = { walk: "walking", bike: "bicycling", transit: "transit", drive: "driving", bike_train_bike: "transit", bike_bus_bike: "transit" }[t.mode];
    const gm = t.origin && t.dest ? `https://www.google.com/maps/dir/?api=1&origin=${t.origin.lat},${t.origin.lon}&destination=${t.dest.lat},${t.dest.lon}&travelmode=${tm}` : null;
    body.innerHTML = `${head}<h2>Check a computed trip</h2>
      <p>${esc(t.origin?.address || t.place_key)} → <b>${esc(t.dest?.name || t.point_id)}</b> by <b>${esc(MODE_LABEL[t.mode] || t.mode)}</b>: stored <b>${mins(t.minutes)} min</b> (provider ${esc(t.provider)}).</p>
      ${gm ? `<p><a href="${esc(gm)}" target="_blank" rel="noopener noreferrer">Open in Google Maps ↗</a></p>` : ""}
      <pre class="raw">${esc(JSON.stringify(t.legs, null, 2))}</pre>
      <table><tr><th>minutes</th><td>${mins(t.minutes)}</td><td>${vbtns({ trip_id: t.id, field: "minutes", queue: "sample" })}</td></tr></table>${nav}`;
  } else {
    const obs = d.observations;
    const title = it.kind === "conflict" ? (it.cheap || it.stale ? `${obs[0].source}: ${it.reason}` : it.text ? `${obs[0].source} and its own text disagree on ${d.fields.join(", ")}`
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
  $("#v-skip").addEventListener("click", () => vPage(() => { S.verifySkip++; return renderVerify(); }));
  $("#v-next").addEventListener("click", () => vPage(renderVerify));
}

/* pairs of places that are probably one building: merge either way (into the bigger by default) or say they differ */
const DUP_PAGE = 20;
async function renderDuplicates() {
  const body = $("#verify-body");
  const d = await vLoad(`api/verify/duplicates?skip=${S.dupSkip}&n=${DUP_PAGE}`);
  if (!d) return;
  if (!d.items.length && S.dupSkip > 0 && d.total > 0) { S.dupSkip = Math.max(0, d.total - DUP_PAGE); return renderDuplicates(); }
  const counts = Object.entries(d.by_reason).map(([k, n]) => `${n} ${esc(d.labels[k])}`).join(" · ");
  // the two places are A and B (names are often identical, so the buttons name the letter)
  const side = (p, l) => `<div style="flex:1 1 220px;min-width:0"><b>${l}</b> <a href="#" data-goto="${esc(p.place_key)}">${esc(p.label)}</a>
    <div class="small">${esc(p.address || "(no address)")}</div>
    <div class="muted small">${p.n_listings} listing(s) · ${esc(p.sources.join(", ") || "no sources")}</div></div>`;
  const block = (e) => {
    const [a, b] = e.places;
    a.l = "A"; b.l = "B";
    const [big, small] = b.n_listings > a.n_listings ? [b, a] : [a, b];  // merge into the bigger place by default
    const btn = (from, into, accent) => `<button class="${accent ? "accent " : ""}dup-merge" data-from="${esc(from.place_key)}" data-into="${esc(into.place_key)}" title="every listing of ${esc(from.label)} moves to ${esc(into.label)}">Merge ${from.l} into ${into.l}</button>`;
    return `<div class="vconf" data-pair="${esc(e.pair_key)}" data-a="${esc(a.place_key)}" data-b="${esc(b.place_key)}">
      <div class="vconf-head"><label class="dup-pick" title="select for the bar above"><input type="checkbox" class="dup-sel"></label> ${e.reasons.map((r) => `<span class="chip" title="${esc(r.detail)}">${esc(r.label)}</span>`).join(" ")}
        <span class="muted small">${e.metres} m apart</span></div>
      <div style="display:flex;gap:12px;flex-wrap:wrap">${side(a, "A")}${side(b, "B")}</div>
      <p class="small muted" style="word-break:break-word">${e.reasons.map((r) => `${esc(r.label)}: ${esc(r.detail)}`).join("<br>")}</p>
      <p class="small">${btn(small, big, true)} ${btn(big, small, false)} <button class="dup-no">Not the same building</button></p></div>`;
  };
  const more = S.dupSkip + d.items.length < d.total;
  body.innerHTML = `<h2>Possible duplicate buildings (${d.total})</h2>
    <p class="small">${counts || "none"}. Pairs of places that look like one building, strongest evidence first. A merge moves every listing of one place to the other (each keeps its unit; undo per listing with split in the drawer).</p>
    <p class="muted small"><a href="#" id="v-back">back to the conflicts</a></p>
    ${d.items.length ? `<div class="dup-bar"><label><input type="checkbox" id="dup-all"> select all on this page</label>
      <span><b id="dup-n">0</b> selected:</span>
      <button id="dup-bm" class="accent" disabled>Merge each smaller into bigger</button>
      <button id="dup-bn" disabled>Not the same building</button> <button id="dup-bc" disabled>Clear</button>
      <span id="dup-prog" class="small muted"></span></div>` : ""}
    ${d.items.map(block).join("") || "<p>Nothing here.</p>"}
    <p style="margin-top:14px"><button id="d-prev" ${S.dupSkip ? "" : "disabled"}>Previous ${DUP_PAGE}</button>
      ${more ? `<button id="d-more" class="accent">Next ${DUP_PAGE}</button>` : ""}
      <button id="d-rest" class="${more ? "" : "accent"}">${more ? "Skip to" : "Continue:"} buildings whose own site gave nothing usable, then the sample</button></p>`;
  // answered in place (the list is not fetched again): the pair folds to a stub; after a merge, the other pairs on
  // this page with the merged-away place fold too (that place is gone; Refresh shows what is left)
  $$(".vconf", body).forEach((box) => {
    $$(".dup-merge", box).forEach((b) => b.addEventListener("click", () => {
      const from = b.dataset.from;
      vAnswer(box, `${esc(b.textContent)}: merging…`, () => post(`api/places/${encodeURIComponent(from)}/merge`, { into: b.dataset.into }), {
        then: (r, s) => { s.textContent = `${b.textContent}: merged ${r.merged} listing(s)`; dupFoldMerged(body, box, from); },
      });
    }));
    $(".dup-no", box).addEventListener("click", () => vAnswer(box, "Recorded: not the same building",
      () => post("api/annotations", { entity_type: "place_pair", entity_key: box.dataset.pair, attribute: "same_building", comment: "no" })));
  });
  $$("[data-goto]", body).forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); gotoPlace(a.dataset.goto); }));
  $("#v-back").addEventListener("click", backToConflicts);
  wireDupBatch(body);
  $("#d-prev").addEventListener("click", () => vPage(() => { S.dupSkip = Math.max(0, S.dupSkip - DUP_PAGE); return renderDuplicates(); }));
  // answered pairs have left the queue, so the next page starts that much earlier
  $("#d-more")?.addEventListener("click", () => vPage(() => { S.dupSkip += d.items.length - vLeft(body); return renderDuplicates(); }));
  $("#d-rest").addEventListener("click", () => vPage(() => { S.verifyMode = "own"; S.ownSkip = 0; return renderVerify(); }));
}

// after a merge, the other open pairs on this page with the merged-away place fold too (that place is gone;
// Refresh shows what is left)
function dupFoldMerged(body, box, from) {
  $$(".vconf", body).filter((x) => x !== box && !x.classList.contains("answered") && $$("[data-goto]", x).some((a) => a.dataset.goto === from))
    .forEach((x) => vAnswer(x, "One of these two places was just merged into another; Refresh shows this pair again if it still applies", () => Promise.resolve()));
}
/* the duplicate screen's bulk bar: tick pairs, then merge each (the highlighted direction: smaller into bigger) or
   say 'not the same building' for each, one pair per request in order, with progress. Each pair folds in place like
   a single answer; a failure unfolds with its error and stays ticked for another try */
function wireDupBatch(body) {
  const all = $("#dup-all", body);
  if (!all) return;
  const open = () => $$(".vconf", body).filter((b) => !b.classList.contains("answered"));
  const sel = () => open().filter((b) => $(".dup-sel", b).checked);
  const bar = ["#dup-bm", "#dup-bn", "#dup-bc"].map((s) => $(s, body));
  const sync = () => {
    const n = sel().length, total = open().length;
    $("#dup-n", body).textContent = n;
    bar.forEach((b) => { b.disabled = !n; });
    all.checked = n > 0 && n === total;
    all.indeterminate = n > 0 && n < total;
  };
  $$(".dup-sel", body).forEach((c) => c.addEventListener("change", sync));
  all.addEventListener("change", () => { open().forEach((b) => { $(".dup-sel", b).checked = all.checked; }); sync(); });
  bar[2].addEventListener("click", () => { $$(".dup-sel", body).forEach((c) => { c.checked = false; }); sync(); });
  const run = async (merge) => {
    const boxes = sel();
    if (!boxes.length) return;
    [all, ...bar].forEach((x) => { x.disabled = true; });
    const prog = $("#dup-prog", body);
    let ok = 0, failed = 0, skipped = 0;
    for (const [i, box] of boxes.entries()) {
      if (box.classList.contains("answered")) { $(".dup-sel", box).checked = false; skipped++; continue; }  // folded by an earlier merge
      prog.textContent = `${merge ? "merging" : "recording"} ${i + 1}/${boxes.length}…`;
      const acc = $(".dup-merge.accent", box);  // the highlighted button's direction: smaller into bigger
      const item = { a: box.dataset.a, b: box.dataset.b, into: merge ? acc.dataset.into : null };
      await new Promise((settled) => vAnswer(box, merge ? `${esc(acc.textContent)}: merging…` : "Recorded: not the same building", () => {
        const p = post("api/verify/duplicates/batch", { items: [item] })
          .then(({ results: [r] }) => { if (!r.ok) throw new Error(r.error); return r; });
        p.then(() => { ok++; $(".dup-sel", box).checked = false; if (merge) dupFoldMerged(body, box, acc.dataset.from); },
          () => { failed++; }).finally(settled);
        return p;
      }, { then: (r, s) => { if (merge) s.textContent = `${acc.textContent}: merged ${r.merged} listing(s)`; } }));
    }
    prog.textContent = `${ok} done${failed ? `, ${failed} failed (error shown on each, still ticked)` : ""}${skipped ? `, ${skipped} skipped (a place already merged away)` : ""}`;
    [all, ...bar].forEach((x) => { x.disabled = false; });
    sync();
  };
  bar[0].addEventListener("click", () => run(true));
  bar[1].addEventListener("click", () => run(false));
  sync();
}

/* buildings whose own site gave no usable offer: one list, a reason and every URL tried; add a URL or dismiss */
const OWN_PAGE = 20;
const URL_STATUS = { form: "fail", rejected: "fail", failed: "fail", dead: "fail", no_plans: "needs_check", pending: "", priced: "pass" };
async function renderOwnSites() {
  const body = $("#verify-body");
  const d = await vLoad(`api/verify/own_sites?skip=${S.ownSkip}&n=${OWN_PAGE}`);
  if (!d) return;
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
  // answered in place: the building folds to a stub (an added URL keeps it on the list until a site gives offers)
  const note = (box, attribute, comment, msg, stays = false) =>
    vAnswer(box, esc(msg), () => post("api/annotations", { entity_key: box.dataset.pk, attribute, comment }), { stays });
  $$(".vconf", body).forEach((box) => {
    $(".own-add", box).addEventListener("click", (ev) => {
      const u = $(".own-url", box).value.trim();
      if (!/^https?:\/\//i.test(u)) { vErr(ev.target, "Type a URL starting with http"); return; }
      note(box, "website", u, `URL added: ${u} (pending check: promote-sites fetches it)`, true);
    });
    $(".own-dismiss", box).addEventListener("click", () => note(box, "own_site", "dismissed: no plans online", "Dismissed: no plans online"));
    $(".own-none", box)?.addEventListener("click", () => note(box, "website", "none", "Marked: no website"));
  });
  $$("[data-goto]", body).forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); gotoPlace(a.dataset.goto); }));
  $("#v-back").addEventListener("click", backToConflicts);
  $("#o-prev").addEventListener("click", () => vPage(() => { S.ownSkip = Math.max(0, S.ownSkip - OWN_PAGE); return renderOwnSites(); }));
  $("#o-more")?.addEventListener("click", () => vPage(() => { S.ownSkip += d.items.length - vLeft(body); return renderOwnSites(); }));
  $("#o-rest").addEventListener("click", () => vPage(() => { S.verifyMode = "rest"; S.verifySkip = 0; return renderVerify(); }));
}

function backToConflicts(e) { e.preventDefault(); vPage(() => { S.verifyMode = "conflict"; S.conflictSkip = 0; return renderVerify(); }); }

function vbtns(ctx, verdict, optional) {
  // verdict: one already given (a current check), shown as pressed; optional: not needed to settle the item
  const on = (v) => (v === verdict ? " on" : "");
  return `<span class="vbtns${optional ? " opt" : ""}" data-ctx='${esc(JSON.stringify(ctx))}'>
    <button data-v="correct" class="correct${on("correct")}" title="correct">✓</button>
    <button data-v="wrong" class="wrong${on("wrong")}" title="wrong">✗</button>
    <button data-v="cannot_tell" class="cannot_tell${on("cannot_tell")}" title="cannot tell">?</button>
    <input class="hidden" placeholder="true value"><button class="hidden" data-save>save</button></span>`;
}

function wireVbtns(box) {
  const ctx = JSON.parse(box.dataset.ctx);
  const input = $("input", box), save = $("[data-save]", box), btns = $$("button[data-v]", box);
  const block = box.closest(".vconf");
  let seq = 0;
  const mark = (v) => { btns.forEach((b) => b.classList.toggle("on", b.dataset.v === v)); vMark(block); };
  // in place: pressed at once, saved behind it; a failed save un-presses it and says why next to the buttons
  const send = (verdict, true_value) => {
    const before = btns.find((b) => b.classList.contains("on"))?.dataset.v, my = ++seq;
    input.classList.add("hidden"); save.classList.add("hidden"); vErr(box, "");
    V.dirty = true; mark(verdict);
    vTrack(post("api/checks", { ...ctx, verdict, true_value })).catch((e) => {
      if (my !== seq) return;  // a newer answer to this value owns it
      mark(before); vErr(box, "Not saved: " + e.message);
    });
  };
  btns.forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.v === "wrong") { input.classList.remove("hidden"); save.classList.remove("hidden"); input.focus(); }
    else send(b.dataset.v);
  }));
  save.addEventListener("click", () => send("wrong", input.value));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") send("wrong", input.value);
    if (e.key === "Escape") { e.stopPropagation(); input.classList.add("hidden"); save.classList.add("hidden"); }  // not the whole panel
  });
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
  await loadQueueStatus().catch(() => {});
  const stages = `<p class="small">${apifyCredit(d.apify)}</p><div class="scroll-x"><table class="stages">
    <tr><th>stage</th><th>state</th><th>where it stands</th><th>last run</th><th>cost</th><th>to run the rest</th></tr>
    ${d.stages.map((s) => `<tr><td><b>${esc(s.label)}</b></td><td>${stChip(s.state)}</td><td>${esc(s.summary)}</td>
      <td class="nowrap">${s.last_run !== undefined ? day(s.last_run) : ""}</td>
      <td class="nowrap">${s.cost ? "$" + (+s.cost).toFixed(2) : ""}</td>
      <td class="cmd">${cmdList(s.commands) || '<span class="muted">nothing left</span>'}</td></tr>`).join("")}
  </table></div>`;
  // intake matrix: area x source, one table per area kind
  const matrix = Object.entries(d.intake.sources).filter(([, ss]) => ss.length).map(([kind, ss]) => {
    const rows = d.intake.rows.filter((r) => r.kind === kind);
    if (!rows.length) return "";
    const cell = (c) => stChip(c.state, c.state === "todo" ? "todo" : c.empty ? "empty"
      : `${c.state === "partial" ? "partial " : ""}${(c.date || "").slice(5)}`);
    return `<div class="scroll-x"><table>
      <tr><th>${esc(kind)} area</th>${ss.map((s) => `<th>${esc(s)}</th>`).join("")}</tr>
      ${rows.map((r) => `<tr><td>${esc(r.area)}</td>
        ${ss.map((s) => `<td class="cell" title="${r.cells[s].run_id ? `run ${r.cells[s].run_id}` : "never covered"}">${cell(r.cells[s])}</td>`).join("")}</tr>`).join("")}
    </table></div>`;
  }).join("");
  const runs = `<div class="scroll-x"><table><tr><th>source</th><th>area kind</th><th>runs</th><th>last ok run</th><th>last status</th><th>cost</th></tr>
    ${d.intake.runs.map((r) => `<tr><td>${esc(r.source)}</td><td>${esc(r.area_kind)}</td><td>${r.runs}</td><td>${day(r.last_run)}</td>
      <td>${r.last_status ? `<span class="${r.last_status === "error" ? "reason fail" : ""}" title="${esc(r.last_error || "")}">${esc(r.last_status)}</span>` : ""}</td>
      <td>$${(+r.cost).toFixed(2)}${r.apify_usd != null ? ` <span class="muted small" title="Apify's usageTotalUsd, re-read when each of our runs closed (runs since 2026-11-25); residential proxy can post later">Apify-reported $${(+r.apify_usd).toFixed(2)}</span>` : ""}</td></tr>`).join("")}</table></div>`;
  const web = d.stages.find((s) => s.id === "websites");
  const models = Object.entries(web.no_site_models).map(([m, n]) => `${m}: ${n}`).join(", ");
  const webTable = `<div class="scroll-x"><table><tr><th>website state</th><th>in walk zones</th><th>all</th></tr>
    ${Object.entries(web.labels).map(([k, label]) => `<tr><td>${esc(label)}${k === "no_site" && models ? ` <span class="muted">(${esc(models)})</span>` : ""}</td>
      <td>${web.zone_by_state[k] || 0}</td><td>${web.by_state[k] || 0}</td></tr>`).join("")}
    <tr><td><b>named buildings</b></td><td><b>${web.zone_named}</b></td><td><b>${web.named}</b></td></tr></table></div>`;
  const sites = `<div class="scroll-x"><table><tr><th>site</th><th>state</th><th>last scrape</th><th>last render</th><th>rows</th><th>priced</th><th>re-scrape</th></tr>
    ${d.sites.map((s) => `<tr><td><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.name)}</a>${s.discovered ? ' <span class="muted small">(found by search)</span>' : ""}${s.check && s.check.outcome !== "ok" ? `<br><span class="small ${s.check.outcome === "rejected" ? "reason fail" : "muted"}">address check: ${esc(s.check.label)}</span>` : ""}</td>
      <td>${stChip(s.state, s.state === "todo" ? "never" : s.state)}</td><td>${day(s.last)}</td>
      <td>${renderCell(s.render)}</td>
      <td>${s.rows}${s.info_only ? ' <span class="muted small">photos/link only</span>' : ""}</td><td>${s.priced}</td><td class="cmd"><code>${esc(s.command)}</code></td></tr>`).join("")}
  </table></div>`;
  const anchors = d.stages.find((s) => s.id === "anchors").anchors;
  const stuck = d.stuck_runs.length ? `<p class="reason fail small">Runs never closed (still 'running'): ${d.stuck_runs.map((r) => `#${r.id} ${esc(r.component)}/${esc(r.source || "")} ${day(r.started_at)}`).join(", ")}</p>` : "";
  body.innerHTML = `<div class="pipe">
    <h2>Pipeline status</h2>
    <p class="muted small">Computed now from the logs (${esc(d.now.slice(0, 16).replace("T", " "))} UTC). Website, own-site and Google counts are over places inside a station walk zone. Stale = older than ${d.stale_days} days.
      ${["done", "partial", "stale", "todo", "blocked"].map((s) => stChip(s)).join(" ")}</p>
    ${stuck}${statusBlock(S.status)}
    <h3>Commute times: Google samples, KNN for the rest <button id="k-help" class="help-btn" title="How this works" aria-label="How this works">?</button></h3>${computeBlock()}
    <h3>Search areas</h3><div id="areas-map" class="areas-map"></div><div id="areas-legend" class="small"></div>
    <h3>Stages</h3>${stages}
    <h3>Intake coverage (aggregators)</h3>${matrix}
    <details><summary>Runs per source</summary>${runs}</details>
    <h3>Websites of named buildings</h3>${webTable}
    <h3>Own-site scrapes (${d.sites.length})</h3>${sites}
    <details><summary>Station → target bike legs (${anchors.filter((a) => a.routed).length}/${anchors.length} routed)</summary>
      <div class="scroll-x"><table><tr><th>station</th><th>target</th><th>routed</th></tr>
      ${anchors.map((a) => `<tr><td>${esc(a.station)}</td><td>${esc(a.target)}</td><td>${a.routed ? esc(a.routed === true ? "Google" : a.routed) : '<span class="muted">estimate</span>'}</td></tr>`).join("")}</table></div></details>
  </div>`;
  wireCompute();
  drawAreasMap().catch((e) => { $("#areas-legend").textContent = `Area map failed: ${e.message}`; });
}

/* ---------------------------------------------------------------- compute one attribute (Pipeline panel) */
// Pick a point or set, a mode, a provider and which places; a dry run shows the places, the uncached Google calls
// and their cost against the monthly caps; only then does Run appear. Google never starts without that click, and
// the run is capped at the dry run's call count.
// The KNN grid: destinations x modes, the checked cells saved on the server (shared). Checked cells get KNN ranges
// from nearby Google routes; the sampling buttons route a covering set, a random % or the ambiguous places of the
// chosen scope for every checked cell with Google Route Matrix (dry run first, then a typed confirm for big runs).
function computeBlock() {
  const n = filtered().length;
  return `<div class="compute small" id="compute">
    <div id="k-status" class="scroll-x muted">checking what still needs a sample…</div>
    <div id="k-grid" class="scroll-x">loading cells…</div>
    <p class="muted">A checked cell gets KNN times: a place without its own Google route reads the routes of up to 3 places
      within <span id="k-radius">400</span> m (shown as a range, <b class="src-knn">violet</b>); with none that close it keeps the straight-line estimate.</p>
    <div id="k-acc" class="scroll-x muted"></div>
    <div>Sample: <label><input type="radio" name="s-method" value="cover" checked> cover: every place within
      <select id="s-radius"><option>100</option><option>250</option><option selected>400</option></select> m of a routed place</label>
      <label><input type="radio" name="s-method" value="random"> random <input id="s-pct" type="number" value="10" min="1" max="100" class="num-count">%</label>
      <label><input type="radio" name="s-method" value="refine"> refine: candidates whose range straddles a goal's limit or is over 2 min wide</label></div>
    <div>Of: <label><input type="radio" name="s-scope" value="list" checked> current search results (${n})</label>
      <label><input type="radio" name="s-scope" value="zone"> every place in a walk zone</label>
      <label><input type="radio" name="s-scope" value="candidates"> search candidates (in a walk zone, can still pass a goal)</label></div>
    <button id="c-dry">Dry run</button>
    <span id="c-confirm" class="hidden">to run, type the elements (<b id="c-n-want"></b>) <input id="c-n" type="number" class="num-count">
      and the elements left this month (<b id="c-left-want"></b>) <input id="c-left" type="number" class="num-count"></span>
    <button id="c-run" class="accent hidden">Run</button> <span id="c-hint" class="muted hidden">type both numbers to unlock</span>
    <div id="c-out" class="muted"></div>
    <div id="c-job" class="muted"></div>
    <details id="c-log-box"><summary>Recent computations (each Google call and matrix element is also logged)</summary><div id="c-log">…</div></details></div>`;
}
// "Do I still need to sample?" (/api/knn/status): per ticked cell, for the current search results and for every place
// in a walk zone, the worst-10% / worst-1% expected error, the places whose goal status waits on that commute, and a
// verdict: done, the cheapest sample that would change something, or settled (no sample picks a place)
const cellLabel = (k) => { const [p, md] = cellParts(k); return `${p.replace(/^commute_targets:/, "").replace(/^set:/, "nearest ")} × ${MODE_LABEL[md] || md}`; };
function verdictHtml(v) {
  if (!v) return "";
  if (v.state === "done") return '<span class="chip pass">done</span>';
  if (v.state === "run") return `<span class="chip needs_check">run ${esc(v.method)}: ${v.places} place${v.places === 1 ? "" : "s"}, ${v.elements} element${v.elements === 1 ? "" : "s"}</span>`;
  return `<span class="muted" title="${esc(v.why || "")}">${esc(v.state)}</span>`;
}
function statusHtml(d) {
  const SCOPES = [["list", "current search results"], ["zone", "all places (walk zones)"]];
  const err = (c) => (c.p90 == null ? "error unknown" : `p90 ${mins(c.p90)} · p99 ${mins(c.p99)} min`);
  const cell = (c) => `<td>${err(c)} · ${c.undecided} undecided<br>${verdictHtml(c.verdict)}</td>`;
  const keys = Object.keys(d.zone.cells);
  const goals = (x) => Object.entries(x.by_goal).map(([g, n]) => `${esc(g)} ${n}`).join(" · ") || "none";
  const sampleable = new Set(KNN_META?.modes || []);
  const untickedNote = (x) => Object.entries(x.unticked).map(([k, n]) => {
    const md = cellParts(k)[1];
    return `${n} wait on ${esc(cellLabel(k))}${sampleable.has(md) ? " (tick it to sample it)" : " (not a grid column: no sample can route it; check by hand or change the goal's option)"}`;
  }).join("; ");
  const total = (x) => `<td><b>${err(x)} · ${x.undecided} undecided</b><br>${x.done ? '<span class="chip pass">done</span>'
    : x.undecided && !Object.values(x.cells).some((c) => c.verdict.state === "run") ? '<span class="muted">settled for the ticked cells</span>'
    : '<span class="chip needs_check">not done</span>'}${x.undecided ? `<br><span class="small">by goal: ${goals(x)}${Object.keys(x.unticked).length ? `; ${untickedNote(x)}` : ""}</span>` : ""}</td>`;
  return `<table><tr><th>ticked cell</th>${SCOPES.map(([, l]) => `<th>${l}</th>`).join("")}</tr>
    ${keys.map((k) => `<tr><td>${esc(cellLabel(k))}</td>${SCOPES.map(([s]) => cell(d[s].cells[k] || {})).join("")}</tr>`).join("")}
    <tr><td><b>all cells</b></td>${SCOPES.map(([s]) => total(d[s])).join("")}</tr></table>
    <p class="small">p90 / p99: the error a place's shown time may have, worst 10% / worst 1% of the scope's places (own Google
    route 0; KNN range: leave-one-out error of ranges as wide; estimate only: measured estimate vs Google). Undecided: places
    whose goal status waits only on this commute. Done = 0 undecided and p99 ≤ ${d.done_p99} min.</p>`;
}
async function loadKnnStatus() {
  const box = $("#k-status");
  if (!box) return;
  box.textContent = "checking what still needs a sample…";
  try {
    const d = await post("api/knn/status", { place_keys: filtered().map((p) => p.place_key) });
    box.innerHTML = Object.keys(d.zone.cells).length ? statusHtml(d) : "Tick a cell below to see whether it still needs a sample.";
  } catch (e) { box.textContent = "Status failed to load: " + e.message; }
}
// Per checked cell over the chosen scope: how places get their time, and KNN accuracy by leave-one-out (/api/knn/accuracy)
function accuracyHtml(cells, scope, pad = 0.5, R = KNN_META?.radius_m ?? 400) {
  const rows = Object.entries(cells || {});
  if (!rows.length) return "";
  const m = (x) => (x == null ? "–" : `${mins(x)} min`);
  return `<table><tr><th>cell</th><th title="places in the scope whose shown time is their own Google route">own Google</th>
    <th title="a Google-routed place within ${R} m: shown as a range">KNN</th><th>estimate only</th>
    <th title="leave-one-out: each place with its own Google route predicted from its other routed neighbours within ${R} m">n</th>
    <th title="minutes outside the range padded by ${pad} min each side (0 inside)">median error</th>
    <th title="95th percentile of that error: the worst 5% are off by more">p95 error</th><th>inside range</th></tr>
    ${rows.map(([k, v]) => {
      const [p, md] = cellParts(k), l = v.loo || {};
      return `<tr><td>${esc(p.replace(/^commute_targets:/, "").replace(/^set:/, "nearest "))} × ${esc(MODE_LABEL[md] || md)}</td>
        <td>${v.google_pct}%</td><td>${v.knn_pct}%</td><td>${v.estimate_pct}%</td><td>${l.n ?? 0}</td>
        <td>${l.n ? m(l.median) : "–"}</td><td>${l.n ? m(l.p95) : "–"}</td><td>${l.n ? `${l.inside_pct}%` : "–"}</td></tr>`;
    }).join("")}</table><p class="small">Shares are of the ${scope} places in the scope. Accuracy: leave-one-out over every place
    with its own Google route (n); error = minutes outside the neighbours' range padded by ${pad} min each side.</p>`;
}
async function loadAccuracy() {
  const box = $("#k-acc");
  if (!box) return;
  try {
    const d = await post("api/knn/accuracy", computeSpec());
    box.innerHTML = accuracyHtml(d.cells, d.scope, d.pad_min, d.radius_m);
  } catch (e) { box.textContent = "Accuracy failed to load: " + e.message; }
}
const cellParts = (k) => { const i = k.lastIndexOf("|"); return [k.slice(0, i), k.slice(i + 1)]; };
let KNN_META = null;  // the last /api/knn/cells answer: rows (with each set's k), k_stations, matrix cap and free tier
// Google routes one place costs in a grid cell, from config: a target by a plain mode 1; bike+train up to
// k_stations + 1 (engine._bike_train_bike); a point set its k nearest (k = 0: every point)
function cellRoutes(rowId, mode, d = KNN_META) {
  const r = d?.rows.find((x) => x.id === rowId);
  if (mode === "bike_train_bike") return { n: (d?.k_stations ?? 2) + 1, upTo: true };
  if (!r || r.kind !== "set") return { n: 1, upTo: false };
  return { n: r.k > 0 ? Math.min(r.k, r.points || r.k) : r.points, upTo: false };
}
function rowNote(r) {
  if (r.kind !== "set") return "1 route per place per mode";
  const n = r.k > 0 ? Math.min(r.k, r.points || r.k) : r.points;
  const pick = r.k > 0 ? `${r.k} nearest points by straight line` : `all ${r.points} points`;
  if (!r.paid) return `estimate only (paid = false): ${pick}, no Google routes`;
  if (r.k === 1) return "route to the nearest point only (straight line) — 1 route per place";
  return `routes to the ${pick}; shows the fastest — ${n} routes per place`;
}
const TRANSIT_NOTE = "Google transit, weekday morning departure; no straight-line estimate, so a place with no routed neighbour stays –";
const btbNote = (ks) => `per place: bike to the ${ks} nearest stations + bike the whole way for comparison = up to ${ks + 1} routes
  (the whole-way leg is the bike column's route); train time from the timetable plus wait; station→destination bike legs
  are shared by every place (routed once, then reused)`;
// "How this works" for the commute grid: plain-language help, numbers from /api/knn/cells (config and constants)
function knnHelpHtml(d = KNN_META) {
  const num = (x) => Number(x).toLocaleString("en-US");
  const R = d?.radius_m ?? 400, K = d?.knn_k ?? 3, ks = d?.k_stations ?? 2, over = d?.typed_over ?? 20;
  const sets = (d?.rows || []).filter((r) => r.kind === "set").map((r) => `${esc(r.label)} ×${r.k > 0 ? r.k : r.points}${
    r.paid ? "" : " (estimate only, never billed)"}`).join(", ");
  return `<h3>How commute times work</h3>
  <p><b>Ticking a cell</b> (destination × mode) turns on KNN times for it. Free: it only changes what is shown.
    A place with its own Google route shows it (<b class="src-google">blue</b>). A place without one borrows the routes of up
    to ${K} Google-routed places within ${R} m and shows their range (<b class="src-knn">violet</b>). With none that close it
    keeps the straight-line estimate (<span class="src-estimate">grey ~</span>).</p>
  <p><b>Accuracy table</b>: per ticked cell, how the scope's places get their time (own Google route, KNN range,
    estimate only), and how good KNN is: each place that has its own Google route is predicted from its other routed
    neighbours within ${R} m (leave-one-out); error is the minutes outside their range padded by 0.5 min each side
    (median, and p95 = the worst 5% are off by more), n = places scored.</p>
  <p><b>Sample</b> picks places to route with Google, for every ticked cell:</p>
  <ul><li><b>cover</b>: the fewest places so that every place has a routed place within R m (places already routed count).
      The best first step.</li>
    <li><b>random X%</b>: X% of <i>all</i> places in the scope, one draw shared by every cell. A pick already routed for a
      cell is dropped for that cell and costs nothing. Leaves gaps in sparse areas.</li>
    <li><b>refine</b>, after a first sample: only places that can still pass a goal apart from commute rules, and only
      for cells that goal has a rule on: those whose KNN range straddles the goal's limit, or is more than 2 min wide.
      A fixed bar, so it runs out: a routed place is never picked again. Places with no routed neighbour (estimate only)
      are cover's job; the dry run counts them.</li></ul>
  <p><b>Do I still need to sample?</b> The table at the top, per ticked cell, for the current search results and for every
    place in a walk zone: <i>p90 / p99</i> = the error a shown time may have for the worst 10% / worst 1% of places
    (own Google route: 0; KNN range: the leave-one-out error of ranges about as wide, up to or over 2 min; estimate only:
    the measured estimate-vs-Google error), whole minutes. <i>Undecided</i> = places whose goal status waits only on
    this commute (every other rule passes). <b>done</b> (green) = 0 undecided and p99 ≤ 2 min. Otherwise it names the cheaper
    of cover (${R} m) and refine that would pick something, with its elements; <i>settled</i> = no sample picks a
    place (what is left is KNN's error at places that cannot pass a goal). The total row lists places waiting on a
    commute no ticked cell covers (tick it). The dry run shows undecided now → after.</p>
  <p><b>Of</b>: the current search results; every place in a walk zone; or search candidates (in a walk zone and can
    still pass a goal), usually the right one.</p>
  <p><b>Reading the dry run.</b> The first line's "N not yet routed, over all cells" adds up each cell's picks that
    cell has no Google route for yet (a place counts once per cell). Per cell: <i>places</i> = picks minus those already
    routed for that cell, shown × routes per place (bike+train adds its ${ks} shared station legs per destination once, on top); <i>elements</i> = Route Matrix elements still to pay for, one per
    place × destination (a target ×1; ${sets || "point sets ×k nearest"}; bike+train ×up to ${ks + 1}: bike to the ${ks}
    nearest stations + bike the whole way). Fewer elements than places × routes means some legs are cached or shared.
    "With a routed place within ${R} m: before → after" is how many places in the scope get a KNN time.</p>
  <p><b>Billing</b>: Google Route Matrix Essentials, ${num(d?.matrix_free ?? 10000)} elements free a month,
    ${d?.matrix_cap ? `our cap ${num(d.matrix_cap)}` : "no cap set, so no matrix calls"}. A run of more than ${over} elements
    asks you to type the element count and the elements left this month before Run unlocks, while fewer than 5,000 are left.</p>
  <p><b>Tip</b>: bike+train already routes bike-the-whole-way, so also ticking plain bike for the same destination
    shows that leg in both rows of the dry run, but it is routed and billed once (the total counts it once).</p>
  <form method="dialog"><button class="accent">Close</button></form>`;
}
function openKnnHelp() {
  let dlg = $("#k-help-dlg");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "k-help-dlg"; dlg.className = "help-dlg";
    // a click on the backdrop (outside the dialog's box) closes it; Esc closes a modal dialog natively
    dlg.addEventListener("click", (e) => {
      const r = dlg.getBoundingClientRect();
      if (e.target === dlg && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) dlg.close();
    });
    document.body.appendChild(dlg);
  }
  dlg.innerHTML = knnHelpHtml();
  dlg.showModal();
}
async function drawKnnGrid() {
  const box = $("#k-grid");
  const d = await api("api/knn/cells").catch((e) => { box.textContent = "Cells failed to load: " + e.message; return null; });
  if (!d) return;
  KNN_META = d;
  $("#k-radius").textContent = d.radius_m;
  const on = new Set(d.cells.map(([p, m]) => `${p}|${m}`));
  const num = (x) => Number(x).toLocaleString("en-US");
  const bill = `All sampling bills as Google Route Matrix Essentials: 1 element = 1 place × 1 destination; ${num(d.matrix_free)} free a month, `
    + (d.matrix_cap ? `cap ${num(d.matrix_cap)}.` : "no cap set in google.toml, so no matrix calls.");
  const colNote = (m) => m === "bike_train_bike" ? btbNote(d.k_stations) : m === "transit" ? TRANSIT_NOTE : "";
  box.innerHTML = `<table class="knn-grid"><tr><th></th>${d.modes.map((m) => `<th${colNote(m) ? ` title="${esc(colNote(m))}"` : ""}>${esc(MODE_LABEL[m] || m)}${
      colNote(m) ? `<div class="muted small knn-note">${esc(colNote(m))}</div>` : ""}</th>`).join("")}</tr>
    ${d.rows.map((r) => `<tr><th title="${esc(rowNote(r))}">${esc(r.label)}<div class="muted small knn-note">${esc(rowNote(r))}</div></th>${d.modes.map((m) => {
      if ((m === "bike_train_bike" || m === "transit") && r.kind === "set") return "<td></td>";
      const why = !r.paid ? "paid = false in point_sets.toml: no Google routing for this set"
        : colNote(m) || rowNote(r);
      return `<td title="${esc(why)}"><input type="checkbox" data-cell="${esc(r.id)}|${esc(m)}" title="${esc(why)}"
        ${on.has(`${r.id}|${m}`) ? "checked" : ""} ${r.paid ? "" : "disabled"}></td>`;
    }).join("")}</tr>`).join("")}</table><p class="muted small">${esc(bill)}</p>`;
  $$("#k-grid input[data-cell]").forEach((cb) => cb.addEventListener("change", async () => {
    const cells = $$("#k-grid input[data-cell]:checked").map((x) => cellParts(x.dataset.cell));
    try { await post("api/knn/cells", { cells }); toast(`${cells.length} cell${cells.length === 1 ? "" : "s"} saved`); loadAccuracy(); loadKnnStatus(); await loadPlaces(); }
    catch (e) { toast("Saving cells failed: " + e.message, 6000); cb.checked = !cb.checked; }
  }));
}
function computeSpec() {
  const scope = $("input[name=s-scope]:checked").value;
  return { method: $("input[name=s-method]:checked").value, pct: +$("#s-pct").value, radius_m: +$("#s-radius").value,
    scope, place_keys: scope === "list" ? filtered().map((p) => p.place_key) : null };
}
function wireCompute() {
  let dry = null;
  const out = $("#c-out");
  const reset = () => { dry = null; ["#c-run", "#c-confirm", "#c-hint"].forEach((id) => $(id).classList.add("hidden")); };
  // a run of more than 20 elements unlocks only when both numbers are typed exactly
  const typedOk = () => !dry?.typed || (+$("#c-n").value === dry.elements && +$("#c-left").value === dry.left);
  const lock = () => {
    $("#c-run").disabled = !typedOk();
    $("#c-hint").classList.toggle("hidden", !$("#c-run").disabled || $("#c-run").classList.contains("hidden"));
  };
  ["#c-n", "#c-left"].forEach((id) => $(id).addEventListener("input", lock));
  $("#k-help").addEventListener("click", openKnnHelp);
  drawKnnGrid().then(() => { loadAccuracy(); loadKnnStatus(); });
  $$("input[name=s-scope]").forEach((el) => el.addEventListener("change", loadAccuracy));
  loadComputeLog();
  ["#s-radius", "#s-pct", ...$$("input[name=s-method]"), ...$$("input[name=s-scope]")].forEach((el) =>
    (typeof el === "string" ? $(el) : el).addEventListener("input", () => { reset(); out.textContent = ""; }));
  $("#k-grid").addEventListener("change", () => { reset(); out.textContent = ""; });
  $("#c-dry").addEventListener("click", async () => {
    reset();
    const spec = computeSpec();
    out.textContent = "Planning and counting uncached Route Matrix elements (no calls are made)…";
    try {
      const d = await post("api/sample/dry_run", spec);
      const u = d.usage;
      const lens = Object.values(d.plan).map((v) => v.length);
      const places = lens.reduce((a, b) => a + b, 0);
      const head = spec.method === "random" ? `${spec.pct}% of ${d.scope} places = ${Math.round(d.scope * spec.pct / 100)} places (${places} not yet routed, over all cells)`
        : spec.method === "cover" ? `cover within ${spec.radius_m} m of ${d.scope} places: ${places} picks`
        : `refine over ${d.scope} places: ${places} places`;
      const far = spec.method === "refine" ? Object.values(d.pairs).reduce((a, v) => a + (v.no_neighbour || 0), 0) : 0;
      const h = d.hinge, hk = (k) => h.by_pair[k] || 0, hka = (k) => h.after.by_pair[k] || 0;
      const rows = Object.entries(d.pairs).map(([k, v]) => {
        const [p, m] = cellParts(k);
        // places x routes per place = the nominal elements; fewer are billed when legs are cached or shared
        const c = cellRoutes(p, m), nominal = v.places * c.n;
        const el = v.elements === nominal ? `${v.elements}` : `${v.elements} <span class="muted">(${nominal - v.elements > 0
          ? `${nominal - v.elements} cached or shared` : `${v.elements - nominal} shared legs not cached yet`})</span>`;
        return `<tr><td>${esc(p.replace(/^commute_targets:/, "").replace(/^set:/, "nearest "))} × ${esc(MODE_LABEL[m] || m)}</td>
          <td>${v.places} × ${c.upTo ? "up to " : ""}${c.n} = ${nominal}</td><td>${el}</td><td>${v.covered_before} → ${v.covered_after} of ${d.scope}</td>
          <td>${hk(k)} → ${hka(k)}</td></tr>`;
      }).join("");
      out.innerHTML = `${esc(head)}, <b>${d.elements}</b> Route Matrix elements (${esc(d.modes.join(", ") || "none")})
        · this month ${u.matrix_elements}/${u.matrix_cap} elements used
        <div class="scroll-x"><table><tr><th>cell</th><th>places × routes per place</th><th>elements (uncached)</th><th>with a routed place within ${d.radius_m} m</th>
          <th title="places whose goal status waits only on this commute; after: if each routed place's time settles it (an upper bound)">undecided: now → after</th></tr>${rows}</table></div>
        <div>Undecided on any commute: ${h.total} → ${h.after.total}${Object.keys(h.by_goal).length ? ` (${Object.entries(h.by_goal).map(([g, n]) => `${esc(g)} ${n} → ${h.after.by_goal[g] || 0}`).join(", ")})` : ""}</div>
        ${far ? `<div class="reason needs_check">${far} candidate place-cells have no routed neighbour within ${d.radius_m} m (estimate only): refine skips them, use cover.</div>` : ""}
        ${d.accuracy ? `<div class="scroll-x">${accuracyHtml(d.accuracy, d.scope)}</div>` : ""}
        ${d.skipped.length ? `<span class="reason needs_check">Skipped: ${esc(d.skipped.join("; "))}</span>` : ""}
        ${d.elements > d.left ? `<br><span class="reason fail">Over the monthly matrix cap (${d.left} left): the run stops at the cap.</span>` : ""}`;
      if (d.elements > 0) {
        dry = { plan: d.plan, label: d.label, elements: d.elements, left: d.left, typed: d.typed };
        $("#c-run").textContent = `Run: up to ${d.elements} Route Matrix elements`;
        $("#c-run").classList.remove("hidden");
        $("#c-n").value = ""; $("#c-left").value = "";
        $("#c-n-want").textContent = d.elements; $("#c-left-want").textContent = d.left;
        $("#c-confirm").classList.toggle("hidden", !dry.typed);
        lock();
        if (dry.typed) out.innerHTML += `<br>More than 20 elements: type <b>${d.elements}</b> and <b>${d.left}</b> above to unlock Run.`;
      } else out.innerHTML += "<br>Nothing to route: every leg is cached.";
    } catch (e) { out.textContent = "Dry run failed: " + e.message; }
  });
  $("#c-run").addEventListener("click", async () => {
    if (!dry) return;
    $("#c-run").classList.add("hidden"); $("#c-hint").classList.add("hidden");
    try {
      if (!typedOk()) { $("#c-run").classList.remove("hidden"); return; }
      await post("api/sample/run", { plan: dry.plan, label: dry.label, confirm_elements: dry.elements,
        confirm_left: dry.typed ? +$("#c-left").value : null });
      dry = null;
      pollCompute();
    } catch (e) { out.textContent = "Not started: " + e.message; }
  });
  // a running job reports on its own line, so it never overwrites the dry run's numbers
  // a running job, or one a dashboard restart cut off (closed out on start: 'interrupted')
  api("api/compute/status").then((s) => { if (s.state === "running" || s.interrupted) pollCompute(); }).catch(() => {});
}
async function pollCompute() {
  const s = await api("api/compute/status").catch((e) => ({ state: "error", result: { error: e.message } }));
  const out = $("#c-job");  // looked up each time: the panel may have been redrawn since
  if (!out) return;
  const what = s.spec ? `${s.spec.mode} to ${s.spec.point} (${s.spec.provider})` : "";
  if (s.state === "running") {
    out.textContent = `Running ${what}: ${s.done}/${s.places} places, ${s.new_trips} new trips…${s.note ? ` (${s.note})` : ""}`;
    setTimeout(pollCompute, 1500);
    return;
  }
  const r = s.result || {};
  if (s.state === "idle") { out.textContent = ""; return; }  // nothing has run since the dashboard started
  out.innerHTML = s.state === "done"
    ? `Done ${esc(what)}: ${r.places_done}/${r.places} places, ${r.new_trips} new trips${r.matrix_elements ? `, ${r.matrix_elements} matrix elements` : ""}${r.api_calls ? `, ${r.api_calls} Google calls` : ""}.`
    : `<span class="reason fail">Stopped ${esc(what)}: ${esc(r.error || "unknown error")}${r.new_trips ? ` (${r.new_trips} new trips kept)` : ""}</span>`;
  await loadPlaces().catch(() => {});
  loadComputeLog();
}
async function loadComputeLog() {
  const box = $("#c-log");
  if (!box) return;
  const rows = await api("api/compute/log").catch(() => []);
  box.innerHTML = rows.length ? `<div class="scroll-x"><table><tr><th>run</th><th>started</th><th>what</th><th>provider</th>
      <th>places</th><th>new trips</th><th>Google calls / elements</th><th>status</th></tr>
    ${rows.map((r) => `<tr><td>${r.id}</td><td class="nowrap">${esc((r.started_at || "").slice(0, 16).replace("T", " "))}</td>
      <td>${esc((r.params.sample ? r.params.sample + ": " : "") + (r.params.only ? r.params.only.join(" · ") : r.params.from || "full table"))}</td><td>${esc(r.source || "")}</td>
      <td>${r.params.places ?? r.counts.places ?? ""}</td><td>${r.counts.new_trips ?? ""}</td><td>${r.counts.api_calls ?? ""}${r.counts.matrix_elements ? ` / ${r.counts.matrix_elements}` : ""}</td>
      <td>${esc(r.status)}${r.error ? `: ${esc(r.error.slice(0, 80))}` : ""}</td></tr>`).join("")}</table></div>`
    : '<span class="muted">none yet</span>';
}

/* ---------------------------------------------------------------- search areas map (Pipeline panel) */
// One colour per area kind: what each source actually searches. ZIP and city outlines are drawn around the
// listings those searches returned (or, for areas scraped before rows were tagged, the listings whose address
// names them); a station box is its rectangle, with the walk circle it was drawn around.
const AREA_STYLE = {
  box: { color: "#2563eb", name: "station box" },
  zip: { color: "#ea580c", name: "ZIP code" },
  city: { color: "#16a34a", name: "city", plural: "cities" },
};
let areasMap = null;

async function drawAreasMap() {
  const d = await api("api/areas");
  if (areasMap) { areasMap.remove(); areasMap = null; }
  areasMap = L.map("areas-map", { scrollWheelZoom: false });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { attribution: "© OpenStreetMap contributors", maxZoom: 19, opacity: 0.6 }).addTo(areasMap);
  const groups = {};
  const bounds = [];
  for (const kind of ["city", "zip", "box"]) groups[kind] = L.featureGroup();
  const stations = L.featureGroup();
  for (const a of d.areas) {
    const st = AREA_STYLE[a.kind];
    const who = a.searched_by.length ? a.searched_by.join(", ") : "not searched yet";
    const tip = `<b>${esc(a.label)}</b> · ${st.name}<br>${a.listings} listings · ${esc(who)}`
      + (a.outside ? `<br>${a.outside} stray listings far away not drawn` : "")
      + (a.outline === "by address" ? "<br><i>outline from listing addresses (scraped before areas were tagged)</i>" : "");
    let shape = null;
    if (a.bbox) {
      const [s, w, n, e] = a.bbox;
      shape = L.rectangle([[s, w], [n, e]], { color: st.color, weight: 2, fillOpacity: 0.06 });
      L.circle([a.station.lat, a.station.lon], { radius: a.station.radius_m, color: "#555", weight: 1,
        dashArray: "4 4", fill: false }).bindTooltip(`${esc(a.label)}: 25-minute walk`).addTo(stations);
    } else if (a.hull.length >= 3) {
      shape = L.polygon(a.hull, { color: st.color, weight: 2, fillOpacity: 0.08,
        dashArray: a.outline === "by address" ? "6 4" : null });
    } else if (a.hull.length) {
      shape = L.circleMarker(a.hull[0], { color: st.color, radius: 6 });
    }
    if (!shape) continue;
    shape.bindTooltip(tip, { sticky: true });
    shape.addTo(groups[a.kind]);
    bounds.push(shape.getBounds ? shape.getBounds() : L.latLngBounds([shape.getLatLng()]));
  }
  const overlays = {};
  for (const kind of ["box", "zip", "city"]) {
    const st = AREA_STYLE[kind];
    overlays[`<span style="color:${st.color}">■</span> ${st.plural || st.name + "s"} (${esc(d.sources[kind].join(", "))})`] = groups[kind];
  }
  overlays["┅ station walk circles"] = stations;
  groups.box.addTo(areasMap);
  groups.zip.addTo(areasMap);
  stations.addTo(areasMap);
  L.control.layers(null, overlays, { collapsed: false }).addTo(areasMap);
  if (bounds.length) areasMap.fitBounds(bounds.reduce((b, x) => b.extend(x), L.latLngBounds(bounds[0])), { padding: [10, 10] });
  $("#areas-legend").innerHTML = ["box", "zip", "city"].map((k) =>
    `<span class="area-key"><span style="background:${AREA_STYLE[k].color}"></span>${AREA_STYLE[k].name}: searched by ${esc(d.sources[k].join(", "))}</span>`).join("")
    + ' <span class="muted">Hover a shape for its listings and sources. Dashed outline: drawn from listing addresses. Toggle kinds at top right.</span>'
    + d.areas.filter((a) => a.scattered).map((a) => `<p class="reason fail">${esc(a.label)} (${AREA_STYLE[a.kind].name}): `
      + `${esc(a.searched_by.join(", "))} returned ${a.listings} listings spread across the region, so the search ignored `
      + `the location; not drawn.</p>`).join("");
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
// The header shows only the counts; each source's last run, spend, Google usage, errors and hand-check accuracy
// live at the top of the Pipeline panel (statusBlock).
async function loadQueueStatus() {
  const s = await api("api/status");
  S.status = s;
  const parts = [`<span>${s.counts.places} places · ${s.counts.active}/${s.counts.listings} listings active</span>`];
  if (s.errors.length) parts.push(`<a href="#" class="err" id="status-errors">${s.errors.length} recent errors</a>`);
  $("#status-strip").innerHTML = parts.join("");
  const el = $("#status-errors");
  if (el) el.addEventListener("click", (e) => { e.preventDefault(); openPipeline(); });
  const pend = s.queues.conflict_pending ? ` + ${s.queues.conflict_pending} pending render` : "";
  $("#queue-count").textContent = `(${s.queues.conflict} conflicts${pend} · ${s.queues.duplicates ?? 0} duplicates · ${s.queues.own_site ?? 0} own site · ${s.queues.sample})`;
}

function statusBlock(s) {
  if (!s) return "";
  const intake = s.runs.filter((r) => r.component === "intake");
  const rows = intake.map((r) => {
    const c = r.counts || {};
    return `<tr class="${r.status === "error" ? "err" : ""}"><td>${esc(r.source)}</td>
      <td class="nowrap">${esc((r.finished_at || r.started_at || "").slice(0, 16).replace("T", " "))}</td><td>${esc(r.status)}</td>
      <td>${c.observations ?? ""}</td><td>${c.new ?? ""}</td><td>${c.changed ?? ""}</td></tr>`;
  }).join("");
  const acc = s.accuracy.length ? s.accuracy.map((a) => `${esc(a.source)} ${esc(a.field)} ${a.correct}/${a.n}`).join(" · ")
    : "no hand checks yet";
  const errs = s.errors.length ? `<p class="small"><b class="err">Recent errors</b></p><ul class="small">${s.errors.map((e) =>
    `<li><code>${esc(e.component)}/${esc(e.source || "")}</code> run ${e.id}, ${esc((e.started_at || "").slice(0, 16).replace("T", " "))}: ${esc(e.error || "")}</li>`).join("")}</ul>` : "";
  const g = s.google ? `Google ${esc(s.google.month)}: ${s.google.calls}/${s.google.cap} calls · est. $${s.google.est_cost_usd.toFixed(2)}` : "";
  return `<h3>Latest runs</h3><div class="scroll-x"><table>
    <tr><th>source</th><th>last run</th><th>status</th><th>seen</th><th>new</th><th>changed</th></tr>${rows}</table></div>
    <p class="small">${apifyCredit(s.apify)} · Apify-reported charges $${(+s.apify_real).toFixed(2)} (read when each run closed, runs since 2026-11-25; residential proxy can post later; older runs carry only estimates) · est. spend, all runs $${(+s.spend).toFixed(2)}${g ? " · " + g : ""} · hand checks: ${acc}</p>${errs}`;
}

// "Apify credit: $X left (reserve $Y)", from the live API (cached 60 s by the server)
function apifyCredit(a) {
  if (!a || a.error || a.left == null) return `<span class="reason fail">Apify credit: unknown${a && a.error ? ` (${esc(a.error)})` : ""}</span>`;
  const low = a.left - a.reserve < 0.02;
  return `<span class="${low ? "reason fail" : ""}" title="monthly limit $${(+a.limit).toFixed(2)}, used $${(+a.used).toFixed(2)}; every Apify run is refused below the reserve">Apify credit: $${(+a.left).toFixed(2)} left (reserve $${(+a.reserve).toFixed(2)})</span>`;
}

/* ---------------------------------------------------------------- boot */
async function loadPlaces() {
  S.places = await api("api/places");
  const sc = $("#cards").scrollTop;  // a reload (after a compute run, or closing Verify) keeps the list where it was
  if (S.draft && draftDirty()) previewDraft(); // a draft keeps showing over the fresh saved results
  renderList();
  $("#cards").scrollTop = sc;
}

async function boot() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  S.meta = await api("api/meta");
  $("#dbname").textContent = S.meta.db;
  loadView();
  S.goalsData = await api("api/goals");
  initMap();
  // start on the lease goal when there is one, else All
  selectGoal(savedGoal("lease") ? "lease" : "all");
  $$("#base-switch button").forEach((b) => b.addEventListener("click", () => setBase(b.dataset.base)));
  $("#q").addEventListener("input", renderList);
  ["#sort", "#agg", "#inactive", ...$$(".st")].forEach((el) => {
    const node = typeof el === "string" ? $(el) : el;
    node.addEventListener("input", () => { saveView(); renderList(); });
  });
  $$("[data-close]").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.close === "verify") { closeVerify(); return; }  // hidden, kept as it is for the next open
    $("#" + b.dataset.close).classList.add("hidden");
  }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { ["#drawer", "#pipeline"].forEach((id) => $(id).classList.add("hidden")); closeVerify(); }
  });
  $("#verify-btn").addEventListener("click", openVerify);
  $("#verify-refresh").addEventListener("click", () => renderVerify());  // this screen again, same scroll
  $("#pipeline-btn").addEventListener("click", openPipeline);
  S.points = await api("api/points");
  buildPointLayers();
  // the header counts are secondary: a failed /api/status says so in the strip instead of failing the page
  await Promise.all([loadPlaces(), loadQueueStatus().catch((e) => {
    $("#status-strip").innerHTML = `<span class="err">status not loaded: ${esc(e.message)}</span>`;
  })]);
  const pts = S.places.filter((p) => p.lat != null).map((p) => [p.lat, p.lon]);
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.3), { maxZoom: 14 });
}

boot().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<p style="color:red;padding:8px">Failed to load: ${esc(e.message)}</p>`); });
