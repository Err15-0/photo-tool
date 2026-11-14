/* ==========================================================================
   Photo Tool – frontend (vanilla JavaScript, no framework)

   HOW THE UI STAYS SYNCHRONIZED
   -----------------------------
   There is ONE global `state` object (below). Every component – gallery,
   table, map, inspector (detail panel), stats, correlation – is drawn FROM
   that state. Components never talk to each other directly.

     user clicks thumbnail / table row / map marker
                 │
                 ▼
         selectImage(id, source)        <- the ONLY way to change selection
                 │  sets state.selectedId
                 ▼
         updateSelectionUI(source)      <- highlights thumb + row + marker,
                 │                         scrolls them into view, moves map
                 ▼
         renderInspector()              <- detail panel for the selected image

   Filters work the same way: change state.filters, then call renderAll().

   SECURITY: all metadata/filenames are untrusted. They are ONLY inserted via
   textContent / createTextNode (see the h() helper) – never via innerHTML.
   ========================================================================== */

"use strict";

// --------------------------------------------------------------------------
// Constants
// --------------------------------------------------------------------------
const UPLOAD_CHUNK_FILES = 10;                    // files per upload request
const UPLOAD_CHUNK_BYTES = 150 * 1024 * 1024;     // ...or this many bytes
const ALLOWED_EXTENSIONS = ["jpg", "jpeg", "jpe", "png", "webp", "tif", "tiff"];
const OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

// Same ids/labels as SENSITIVE_CATEGORIES in photo_tool/metadata.py
const CATEGORY_LABELS = {
  location: "GPS / location",
  timestamp: "Timestamps",
  device: "Camera / device",
  identifier: "Serial numbers / unique IDs",
  software: "Software / editing history",
  author: "Author / owner / copyright",
  people: "People / face regions",
  text: "Descriptions / comments / keywords",
  embedded: "Embedded thumbnails / previews",
  maker_notes: "Maker notes (vendor-specific data)",
};

// Filters available as checkboxes / stat cards.
const FLAG_FILTERS = {
  gps: (img) => !!img.flags.gps,
  timestamp: (img) => !!img.flags.timestamp,
  device: (img) => !!img.flags.device,
  software: (img) => !!img.flags.software,
  author: (img) => !!img.flags.author,
  unsanitized: (img) => !img.sanitization || img.sanitization.status === "error",
};

// Correlation groups: how to read the grouping value from an image summary.
const GROUP_FIELDS = {
  device: { label: "Camera / device", get: (img) => img.normalized.device },
  software: { label: "Software / editor", get: (img) => img.normalized.software },
  lens: { label: "Lens", get: (img) => img.normalized.lens },
  day: { label: "Capture day", get: (img) => (img.normalized.timestamp_sortable || "").slice(0, 10) || null },
  author: { label: "Author / copyright", get: (img) => img.normalized.artist || img.normalized.copyright },
  sha256: { label: "Identical files (same SHA-256)", get: (img) => img.sha256, onlyDuplicates: true },
  filename: { label: "Duplicate filenames", get: (img) => img.filename, onlyDuplicates: true },
};

// --------------------------------------------------------------------------
// Global state – the single source of truth for the whole UI
// --------------------------------------------------------------------------
const state = {
  images: [],              // image summaries from the server, in upload order
  byId: new Map(),         // id -> summary (same objects as in `images`)
  selectedId: null,        // null means "All Images"
  checked: new Set(),      // ids ticked for batch sanitize / ZIP download
  filters: {
    search: "",
    flags: new Set(),      // keys of FLAG_FILTERS
    group: null,           // { field, value } from the correlation panel
  },
  sort: { key: null, dir: 1 },   // key null = upload order
  details: new Map(),      // id -> full detail incl. raw metadata (cache)
  lastBatchResults: null,  // results of the last sanitize call
  uploadErrors: [],        // [{filename, error}]
  busy: false,             // true while uploading / sanitizing
  scrollToVerification: false,
  nameCounts: new Map(),   // derived: filename -> count (duplicate names)
  hashCounts: new Map(),   // derived: sha256 -> count (identical files)
};

// Leaflet objects (created lazily the first time the workspace is shown)
const mapState = {
  map: null,
  tiles: null,
  markerLayer: null,
  pathLayer: null,
  markers: new Map(),      // id -> L.circleMarker
};

// ==========================================================================
// Small utilities
// ==========================================================================

/** Create a DOM element. Strings become TEXT nodes (safe for untrusted data). */
function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (typeof value === "boolean") node[key] = value;      // checked, disabled, open...
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const $ = (id) => document.getElementById(id);
const dash = (value) => (value === null || value === undefined || value === "" ? "-" : String(value));

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatGps(img) {
  const n = img.normalized;
  if (n.gps_latitude === null || n.gps_latitude === undefined) return null;
  return `${Number(n.gps_latitude).toFixed(5)}, ${Number(n.gps_longitude).toFixed(5)}`;
}

function extensionOf(name) {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "" : name.slice(dot + 1).toLowerCase();
}

let toastTimer = null;
function toast(message, kind = "info") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast toast-${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), kind === "error" ? 7000 : 3500);
}

/** fetch() wrapper: adds the anti-CSRF header, parses JSON, throws on errors. */
async function api(path, options = {}) {
  const headers = Object.assign({ "X-Photo-Tool": "1" }, options.headers || {});
  const response = await fetch(path, Object.assign({}, options, { headers }));
  let data = null;
  try {
    data = await response.json();
  } catch (_) {
    data = null;
  }
  if (!response.ok) {
    const message = (data && data.error) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return data;
}

function safeLocalStorageGet(key, fallback) {
  try {
    const value = window.localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (_) {
    return fallback;
  }
}
function safeLocalStorageSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (_) { /* ignore */ }
}

/** Scroll `child` into view inside its scrollable `container` only (not the page). */
function scrollIntoContainer(container, child) {
  if (!container || !child) return;
  const c = container.getBoundingClientRect();
  const r = child.getBoundingClientRect();
  if (r.top < c.top + 30) container.scrollTop -= (c.top + 30 - r.top);
  else if (r.bottom > c.bottom) container.scrollTop += (r.bottom - c.bottom + 4);
}

// ==========================================================================
// Data helpers
// ==========================================================================

function storeImages(summaries) {
  for (const summary of summaries) {
    if (state.byId.has(summary.id)) {
      const index = state.images.findIndex((img) => img.id === summary.id);
      state.images[index] = summary;
    } else {
      state.images.push(summary);
    }
    state.byId.set(summary.id, summary);
    state.details.delete(summary.id);       // cached detail is now outdated
  }
  recomputeDerived();
}

function recomputeDerived() {
  state.nameCounts = new Map();
  state.hashCounts = new Map();
  for (const img of state.images) {
    state.nameCounts.set(img.filename, (state.nameCounts.get(img.filename) || 0) + 1);
    state.hashCounts.set(img.sha256, (state.hashCounts.get(img.sha256) || 0) + 1);
  }
}

function searchText(img) {
  const n = img.normalized;
  return [img.filename, img.format, n.device, n.software, n.timestamp, n.artist, n.copyright,
    n.lens, n.description, n.serial_number, img.sha256].filter(Boolean).join(" ").toLowerCase();
}

function passesFilters(img) {
  const f = state.filters;
  if (f.search && !searchText(img).includes(f.search)) return false;
  for (const key of f.flags) {
    if (!FLAG_FILTERS[key](img)) return false;
  }
  if (f.group) {
    const value = GROUP_FIELDS[f.group.field].get(img);
    if (value !== f.group.value) return false;
  }
  return true;
}

function visibleImages() {
  return state.images.filter(passesFilters);
}

function sortValue(img, key) {
  const n = img.normalized;
  switch (key) {
    case "filename": return img.filename.toLowerCase();
    case "timestamp": return n.timestamp_sortable || null;
    case "device": return n.device ? n.device.toLowerCase() : null;
    case "software": return n.software ? n.software.toLowerCase() : null;
    case "gps": return img.flags.gps ? 1 : null;
    case "pixels": return img.width * img.height;
    case "size": return img.file_size;
    case "sensitivity": return img.sensitive_categories.length;
    case "sanitized": {
      const s = img.sanitization;
      if (!s) return null;
      return s.verified_clean ? 2 : 1;
    }
    default: return null;
  }
}

/** Visible images in table order (also used for keyboard navigation). */
function sortedVisibleImages() {
  const list = visibleImages();
  const { key, dir } = state.sort;
  if (!key) return list;
  return list
    .map((img, index) => ({ img, index, value: sortValue(img, key) }))
    .sort((a, b) => {
      // Missing values always go to the bottom, whatever the direction.
      if (a.value === null && b.value === null) return a.index - b.index;
      if (a.value === null) return 1;
      if (b.value === null) return -1;
      if (a.value < b.value) return -dir;
      if (a.value > b.value) return dir;
      return a.index - b.index;
    })
    .map((entry) => entry.img);
}

function isSanitizedOk(img) {
  return !!(img && img.sanitization && img.sanitization.status !== "error" && img.sanitization.status !== "unverified");
}

// ==========================================================================
// Badges (shared by gallery, table, inspector)
// ==========================================================================

function badge(text, cls, title) {
  return h("span", { class: `badge ${cls}`, title: title || null }, text);
}

function exposureBadges(img) {
  const f = img.flags || {};
  const out = [];
  if (img.metadata_error) out.push(badge("ERR", "b-dirty", img.metadata_error));
  if (f.gps) out.push(badge("GPS", "b-gps", "Usable GPS coordinates"));
  else if (f.location_tags) out.push(badge("GPS?", "b-gpsx", "Location tags present but no usable coordinates"));
  if (f.timestamp) out.push(badge("TIME", "b-time", "Timestamp metadata"));
  if (f.device) out.push(badge("DEV", "b-dev", "Camera / device metadata"));
  if (f.software) out.push(badge("SW", "b-sw", "Software / editor metadata"));
  if (f.author) out.push(badge("AUTH", "b-auth", "Author / copyright"));
  if (f.identifier) out.push(badge("ID", "b-id", "Serial number / unique ID"));
  if (f.people) out.push(badge("FACES", "b-people", "People / face regions"));
  if (f.embedded) out.push(badge("THUMB", "b-emb", "Embedded thumbnail/preview (may show the uncropped original!)"));
  if (!img.metadata_error && img.sensitive_categories.length === 0) out.push(badge("none", "b-none", "No sensitive metadata found"));
  return out;
}

function sanitizationBadge(img) {
  const s = img.sanitization;
  if (!s) return null;
  if (s.status === "error") return badge("✖ FAILED", "b-dirty", s.error || "Sanitization failed");
  if (s.status === "unverified") return badge("? UNVERIFIED", "b-dirty", s.error || "Could not verify");
  if (s.verified_clean) return badge("✔ CLEAN", "b-clean", "Sanitized copy verified with ExifTool");
  return badge(`⚠ ${s.remaining_sensitive_count} LEFT`, "b-warn", "Sanitized copy still contains sensitive tags");
}

// ==========================================================================
// Rendering – everything
// ==========================================================================

function renderAll() {
  const hasImages = state.images.length > 0;
  for (const id of ["summary-section", "gallery-section", "action-section", "workspace"]) {
    $(id).classList.toggle("hidden", !hasImages);
  }
  if (!hasImages) {
    renderUploadErrors();
    return;
  }
  ensureMap();
  renderStats();
  renderGallery();
  renderTable();
  renderMapMarkers();
  renderCorrelation();
  renderFilterControls();
  updateSelectionUI(null);
  renderInspector();
  updateActionButtons();
  renderUploadErrors();
}

// --------------------------------------------------------------------------
// Stats
// --------------------------------------------------------------------------
function renderStats() {
  const all = state.images;
  const total = all.length;
  const count = (fn) => all.filter(fn).length;
  const pct = (n) => (total ? `${Math.round((n / total) * 100)}%` : "");
  const unique = (getter) => new Set(all.map(getter).filter(Boolean));
  const devices = unique((img) => img.normalized.device);
  const software = unique((img) => img.normalized.software);
  const sanitized = all.filter(isSanitizedOk);
  const clean = all.filter((img) => img.sanitization && img.sanitization.verified_clean).length;

  const cards = [
    { label: "Total images", value: total, sub: `${formatBytes(all.reduce((s, i) => s + i.file_size, 0))}`, onclick: () => selectAllImages() },
    { label: "With GPS", value: count(FLAG_FILTERS.gps), sub: pct(count(FLAG_FILTERS.gps)), cls: "s-gps", filter: "gps" },
    { label: "With timestamps", value: count(FLAG_FILTERS.timestamp), sub: pct(count(FLAG_FILTERS.timestamp)), cls: "s-time", filter: "timestamp" },
    { label: "With camera/device", value: count(FLAG_FILTERS.device), sub: pct(count(FLAG_FILTERS.device)), cls: "s-dev", filter: "device" },
    { label: "With software/editor", value: count(FLAG_FILTERS.software), sub: pct(count(FLAG_FILTERS.software)), cls: "s-sw", filter: "software" },
    { label: "With author/copyright", value: count(FLAG_FILTERS.author), sub: pct(count(FLAG_FILTERS.author)), cls: "s-auth", filter: "author" },
    { label: "Unique devices", value: devices.size, sub: [...devices].slice(0, 2).join(", "), onclick: () => $("correlation-section").scrollIntoView({ behavior: "smooth" }) },
    { label: "Unique software", value: software.size, sub: [...software].slice(0, 2).join(", "), onclick: () => $("correlation-section").scrollIntoView({ behavior: "smooth" }) },
    { label: "Sanitized / verified clean", value: `${sanitized.length} / ${clean}`, sub: "click: show not yet sanitized", cls: "s-clean", filter: "unsanitized" },
    { label: "Rejected files", value: state.uploadErrors.length, sub: state.uploadErrors.length ? "click for details" : "", cls: state.uploadErrors.length ? "s-bad" : "", onclick: () => { const d = $("upload-errors"); d.open = true; d.scrollIntoView({ behavior: "smooth" }); } },
  ];

  const grid = $("stats-grid");
  grid.replaceChildren(...cards.map((card) => {
    const active = card.filter && state.filters.flags.has(card.filter);
    return h("button", {
      type: "button",
      class: `stat ${card.cls || ""} ${active ? "active" : ""}`,
      title: card.filter ? "Click to toggle this filter" : null,
      onclick: card.filter ? () => toggleFlagFilter(card.filter) : card.onclick,
    },
    h("div", { class: "stat-value" }, String(card.value)),
    h("div", { class: "stat-label" }, card.label),
    h("div", { class: "stat-sub" }, card.sub || " "));
  }));
}

// --------------------------------------------------------------------------
// Gallery
// --------------------------------------------------------------------------
function renderGallery() {
  const list = sortedVisibleImages();
  const gallery = $("gallery");
  $("gallery-count").textContent = list.length === state.images.length
    ? `${list.length} images`
    : `${list.length} of ${state.images.length} images (filtered)`;

  gallery.replaceChildren(...list.map((img) => {
    const duplicateHash = state.hashCounts.get(img.sha256) > 1;
    const duplicateName = state.nameCounts.get(img.filename) > 1;
    const checkbox = h("input", {
      type: "checkbox", checked: state.checked.has(img.id), title: "Tick for batch sanitize / ZIP",
      onclick: (event) => { event.stopPropagation(); toggleChecked(img.id, event.target.checked); },
    });
    return h("div", {
      class: `thumb-card ${state.selectedId === img.id ? "selected" : ""} ${state.checked.has(img.id) ? "checked" : ""}`,
      dataset: { id: img.id },
      title: img.filename,
      onclick: () => selectImage(img.id, "gallery"),
    },
    h("div", { class: "thumb-check" }, checkbox),
    duplicateHash ? h("span", { class: "thumb-flag", title: "Byte-identical file uploaded more than once" }, `identical ×${state.hashCounts.get(img.sha256)}`)
      : duplicateName ? h("span", { class: "thumb-flag", title: "Another uploaded file has the same name" }, "same name") : null,
    h("div", { class: "thumb-img-wrap" },
      h("img", { src: `/images/${img.id}/thumb`, alt: "", loading: "lazy", decoding: "async" })),
    h("div", { class: "thumb-name" }, img.filename),
    h("div", { class: "thumb-badges" }, exposureBadges(img), sanitizationBadge(img)));
  }));
}

// --------------------------------------------------------------------------
// Table
// --------------------------------------------------------------------------
function renderTable() {
  const list = sortedVisibleImages();
  $("table-count").textContent = `${list.length} row(s)`;

  // sort indicators
  for (const th of document.querySelectorAll("#meta-table th[data-sort]")) {
    th.classList.toggle("sorted-asc", state.sort.key === th.dataset.sort && state.sort.dir === 1);
    th.classList.toggle("sorted-desc", state.sort.key === th.dataset.sort && state.sort.dir === -1);
  }

  const body = $("meta-table-body");
  if (list.length === 0) {
    body.replaceChildren(h("tr", { class: "empty-row" }, h("td", { colspan: "11" }, "No images match the current filters.")));
    return;
  }
  body.replaceChildren(...list.map((img) => {
    const n = img.normalized;
    const gps = formatGps(img);
    let gpsCell = dash(gps);
    if (!img.flags.gps && n.gps_status === "zero") gpsCell = "0, 0 (ignored)";
    else if (!img.flags.gps && n.gps_status) gpsCell = `(${n.gps_status})`;
    const checkbox = h("input", {
      type: "checkbox", checked: state.checked.has(img.id),
      onclick: (event) => { event.stopPropagation(); toggleChecked(img.id, event.target.checked); },
    });
    return h("tr", {
      class: state.selectedId === img.id ? "selected" : "",
      dataset: { id: img.id },
      onclick: () => selectImage(img.id, "table"),
    },
    h("td", { class: "col-check" }, checkbox),
    h("td", { class: "col-thumb" }, h("img", { class: "table-thumb", src: `/images/${img.id}/thumb`, alt: "", loading: "lazy" })),
    h("td", { title: img.warnings.join("\n") || null },
      img.filename,
      img.warnings.length ? h("span", { class: "badge b-warn", style: "margin-left:4px" }, "!") : null),
    h("td", { title: n.timestamp_source ? `from ${n.timestamp_source}` : null }, dash(n.timestamp)),
    h("td", null, dash(n.device)),
    h("td", null, dash(n.software)),
    h("td", { class: "mono nowrap" }, gps ? [h("div", null, Number(n.gps_latitude).toFixed(5)), h("div", null, Number(n.gps_longitude).toFixed(5))] : gpsCell),
    h("td", null, `${img.width}×${img.height}`),
    h("td", null, formatBytes(img.file_size)),
    h("td", null, h("div", { class: "cell-badges" }, exposureBadges(img))),
    h("td", null, sanitizationBadge(img) || "-"));
  }));
}

// --------------------------------------------------------------------------
// Map
// --------------------------------------------------------------------------
function ensureMap() {
  if (mapState.map) return;
  mapState.map = L.map("map", { worldCopyJump: true, zoomControl: true }).setView([20, 0], 2);
  mapState.markerLayer = L.layerGroup().addTo(mapState.map);
  mapState.pathLayer = L.layerGroup();
  const tilesEnabled = safeLocalStorageGet("photoTool.tiles", "1") === "1";
  $("toggle-map-tiles").checked = tilesEnabled;
  setTilesEnabled(tilesEnabled);
  // The map container was hidden until now; Leaflet must re-measure it.
  setTimeout(() => mapState.map.invalidateSize(), 50);
}

function setTilesEnabled(enabled) {
  const map = mapState.map;
  if (!map) return;
  if (enabled && !mapState.tiles) {
    mapState.tiles = L.tileLayer(OSM_TILE_URL, {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);
  } else if (!enabled && mapState.tiles) {
    map.removeLayer(mapState.tiles);
    mapState.tiles = null;
  }
  safeLocalStorageSet("photoTool.tiles", enabled ? "1" : "0");
  updateMapNote();
}

function markerStyle(selected) {
  return selected
    ? { radius: 11, color: "#1b1300", weight: 3, fillColor: "#ffb020", fillOpacity: 1 }
    : { radius: 7, color: "#ffffff", weight: 2, fillColor: "#ff5d5d", fillOpacity: 0.9 };
}

function buildPopup(img) {
  const n = img.normalized;
  const sameSpot = state.images.filter((other) => other.id !== img.id && other.flags.gps &&
    Math.abs(other.normalized.gps_latitude - n.gps_latitude) < 1e-5 &&
    Math.abs(other.normalized.gps_longitude - n.gps_longitude) < 1e-5).length;
  return h("div", { class: "popup" },
    h("img", { src: `/images/${img.id}/thumb`, alt: "" }),
    h("div", null,
      h("div", { class: "popup-name" }, img.filename),
      h("div", { class: "popup-line mono" }, `lat ${n.gps_latitude}`),
      h("div", { class: "popup-line mono" }, `lon ${n.gps_longitude}`),
      n.gps_altitude !== null ? h("div", { class: "popup-line" }, `alt ${n.gps_altitude} m`) : null,
      h("div", { class: "popup-line" }, `time: ${dash(n.timestamp)}`),
      h("div", { class: "popup-line" }, `device: ${dash(n.device)}`),
      sameSpot ? h("div", { class: "popup-line", style: "color:#ffb020" }, `+${sameSpot} other image(s) at the same coordinates`) : null));
}

function renderMapMarkers() {
  if (!mapState.map) return;
  mapState.markerLayer.clearLayers();
  mapState.pathLayer.clearLayers();
  mapState.markers.clear();

  const withGps = visibleImages().filter((img) => img.flags.gps);
  for (const img of withGps) {
    const marker = L.circleMarker([img.normalized.gps_latitude, img.normalized.gps_longitude], markerStyle(false));
    marker.imageId = img.id;                         // association marker -> image
    marker.bindPopup(() => buildPopup(img), { maxWidth: 320 });
    marker.bindTooltip(img.filename, { direction: "top", offset: [0, -6] });
    marker.on("click", () => selectImage(img.id, "map"));
    marker.addTo(mapState.markerLayer);
    mapState.markers.set(img.id, marker);
  }

  renderMapPaths(withGps);
  const anyGpsAtAll = state.images.some((img) => img.flags.gps);
  $("map-empty").textContent = anyGpsAtAll
    ? "No images with GPS match the current filters."
    : "No GPS metadata found in this batch.";
  $("map-empty").classList.toggle("hidden", withGps.length > 0);
  if (!state.selectedId) fitMapToMarkers();
  updateMapNote();
}

/** Optional: connect each device's GPS points in chronological order. */
function renderMapPaths(withGps) {
  const map = mapState.map;
  if (!$("toggle-map-path").checked) {
    map.removeLayer(mapState.pathLayer);
    return;
  }
  const colors = ["#4da3ff", "#b18cff", "#2ec4b6", "#f2a93b", "#ff7ab8", "#e0e05a"];
  const byDevice = new Map();
  for (const img of withGps) {
    if (!img.normalized.timestamp_sortable) continue;
    const key = img.normalized.device || "(unknown device)";
    if (!byDevice.has(key)) byDevice.set(key, []);
    byDevice.get(key).push(img);
  }
  let colorIndex = 0;
  for (const [device, imgs] of byDevice) {
    if (imgs.length < 2) continue;
    imgs.sort((a, b) => (a.normalized.timestamp_sortable < b.normalized.timestamp_sortable ? -1 : 1));
    const line = L.polyline(imgs.map((img) => [img.normalized.gps_latitude, img.normalized.gps_longitude]), {
      color: colors[colorIndex++ % colors.length], weight: 2, dashArray: "6 6", opacity: 0.9,
    });
    line.bindTooltip(`${device}: ${imgs.length} photos, ${imgs[0].normalized.timestamp} → ${imgs[imgs.length - 1].normalized.timestamp}`, { sticky: true });
    line.addTo(mapState.pathLayer);
  }
  mapState.pathLayer.addTo(map);
}

function fitMapToMarkers() {
  const markers = [...mapState.markers.values()];
  if (markers.length === 0) return;
  const bounds = L.latLngBounds(markers.map((m) => m.getLatLng()));
  mapState.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 13 });
}

function updateMapNote() {
  const notes = [];
  const total = state.images.filter((img) => img.flags.gps).length;
  if (total) notes.push(`${total} image(s) with GPS.`);
  const ignored = state.images.filter((img) => !img.flags.gps && img.normalized.gps_status).length;
  if (ignored) notes.push(`${ignored} image(s) have GPS tags that could not be plotted (0,0 / invalid / incomplete).`);
  if (!mapState.tiles) notes.push("Online tiles are OFF: markers are shown on a blank background and no requests go to OpenStreetMap.");
  const sel = state.selectedId && state.byId.get(state.selectedId);
  if (sel && !sel.flags.gps) notes.push("The selected image has no GPS coordinates.");
  $("map-note").textContent = notes.join(" ");
}

// --------------------------------------------------------------------------
// Correlation panel
// --------------------------------------------------------------------------
function renderCorrelation() {
  const container = $("correlation");
  const groups = [];
  for (const [field, config] of Object.entries(GROUP_FIELDS)) {
    const counts = new Map();
    for (const img of state.images) {
      const value = config.get(img);
      if (!value) continue;
      counts.set(value, (counts.get(value) || 0) + 1);
    }
    let entries = [...counts.entries()];
    if (config.onlyDuplicates) entries = entries.filter(([, c]) => c > 1);
    entries.sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])));
    const max = entries.length ? entries[0][1] : 1;

    const items = entries.map(([value, count]) => {
      const active = state.filters.group && state.filters.group.field === field && state.filters.group.value === value;
      let label = value;
      if (field === "sha256") {
        const names = state.images.filter((img) => img.sha256 === value).map((img) => img.filename);
        label = `${value.slice(0, 12)}… (${[...new Set(names)].join(", ")})`;
      }
      return h("button", {
        type: "button", class: `corr-item ${active ? "active" : ""}`, title: String(value),
        onclick: () => setGroupFilter(active ? null : { field, value }),
      },
      h("span", { class: "corr-value" }, label),
      h("span", { class: "corr-count" }, String(count)),
      h("div", { class: "corr-bar" }, h("div", { style: `width:${Math.max(4, (count / max) * 100)}%` })));
    });
    const missing = config.onlyDuplicates ? 0 : state.images.filter((img) => !config.get(img)).length;
    groups.push(h("div", { class: "corr-group" },
      h("h4", null, `${config.label} (${entries.length})`),
      items.length ? items : h("div", { class: "corr-empty" }, config.onlyDuplicates ? "none found" : "no values"),
      missing && items.length ? h("div", { class: "corr-empty" }, `${missing} image(s) without a value`) : null));
  }
  container.replaceChildren(...groups);
}

// --------------------------------------------------------------------------
// Filter controls (checkboxes, chip) – keep them in sync with state
// --------------------------------------------------------------------------
function renderFilterControls() {
  for (const box of document.querySelectorAll("input[data-filter]")) {
    box.checked = state.filters.flags.has(box.dataset.filter);
  }
  const chip = $("group-filter-chip");
  if (state.filters.group) {
    const { field, value } = state.filters.group;
    const shown = field === "sha256" ? `${value.slice(0, 12)}…` : value;
    chip.replaceChildren(
      h("span", null, `${GROUP_FIELDS[field].label}: ${shown}`),
      h("button", { type: "button", title: "Remove this filter", onclick: () => setGroupFilter(null) }, "×"));
    chip.classList.remove("hidden");
  } else {
    chip.classList.add("hidden");
  }
  const anyFilter = state.filters.search || state.filters.flags.size || state.filters.group;
  $("btn-all-images").classList.toggle("active", !state.selectedId && !anyFilter);
}

function toggleFlagFilter(key) {
  if (state.filters.flags.has(key)) state.filters.flags.delete(key);
  else state.filters.flags.add(key);
  renderAll();
}

function setGroupFilter(group) {
  state.filters.group = group;
  renderAll();
}

function clearFilters() {
  state.filters.search = "";
  state.filters.flags.clear();
  state.filters.group = null;
  $("filter-search").value = "";
}

// ==========================================================================
// SELECTION – the heart of the synchronization
// ==========================================================================

/**
 * Select one image (or null for All Images).
 * `source` = "gallery" | "table" | "map" | "keyboard" | "inspector" | null.
 */
function selectImage(id, source) {
  if (id !== null && !state.byId.has(id)) return;
  state.selectedId = id;
  updateSelectionUI(source);
  renderInspector();
  updateActionButtons();
  updateMapNote();
}

/** "All Images": no selection, no filters, everything visible. */
function selectAllImages() {
  state.selectedId = null;
  clearFilters();
  renderAll();
  if (mapState.map) mapState.map.closePopup();
}

/** Highlight the selected image in gallery, table and map. */
function updateSelectionUI(source) {
  const id = state.selectedId;

  // Gallery
  let selectedCard = null;
  for (const card of $("gallery").children) {
    const isSelected = card.dataset.id === id;
    card.classList.toggle("selected", isSelected);
    if (isSelected) selectedCard = card;
  }
  // Table
  let selectedRow = null;
  for (const row of $("meta-table-body").children) {
    const isSelected = row.dataset.id === id;
    row.classList.toggle("selected", isSelected);
    if (isSelected) selectedRow = row;
  }
  // Scroll the OTHER components so the user sees the linked item.
  if (source && source !== "gallery") scrollIntoContainer($("gallery"), selectedCard);
  if (source && source !== "table") scrollIntoContainer($("table-wrap"), selectedRow);

  // Map
  for (const [markerId, marker] of mapState.markers) {
    marker.setStyle(markerStyle(markerId === id));
    if (markerId === id) marker.bringToFront();
  }
  if (source && mapState.map) {
    const marker = id && mapState.markers.get(id);
    if (marker) {
      const zoom = Math.max(mapState.map.getZoom(), 13);
      mapState.map.setView(marker.getLatLng(), zoom, { animate: true });
      marker.openPopup();
    } else if (!id) {
      mapState.map.closePopup();
      fitMapToMarkers();
    } else {
      mapState.map.closePopup();
    }
  }

  const anyFilter = state.filters.search || state.filters.flags.size || state.filters.group;
  $("btn-all-images").classList.toggle("active", !id && !anyFilter);
}

/** Move selection to previous/next image in table order (keyboard ← →). */
function selectRelative(step) {
  const list = sortedVisibleImages();
  if (list.length === 0) return;
  let index = list.findIndex((img) => img.id === state.selectedId);
  if (index === -1) index = step > 0 ? -1 : list.length;
  const next = list[Math.min(list.length - 1, Math.max(0, index + step))];
  selectImage(next.id, "keyboard");
}

// ==========================================================================
// Ticked (multi-select) images
// ==========================================================================
function toggleChecked(id, checked) {
  if (checked) state.checked.add(id);
  else state.checked.delete(id);
  updateCheckedUI();
}

function updateCheckedUI() {
  for (const card of $("gallery").children) {
    const on = state.checked.has(card.dataset.id);
    card.classList.toggle("checked", on);
    const box = card.querySelector("input[type=checkbox]");
    if (box) box.checked = on;
  }
  for (const row of $("meta-table-body").children) {
    const box = row.querySelector("input[type=checkbox]");
    if (box) box.checked = state.checked.has(row.dataset.id);
  }
  const visible = visibleImages();
  const allTicked = visible.length > 0 && visible.every((img) => state.checked.has(img.id));
  $("table-check-all").checked = allTicked;
  updateActionButtons();
  const inspectorBox = $("inspector-check");
  if (inspectorBox) inspectorBox.checked = state.checked.has(state.selectedId);
}

// ==========================================================================
// Action buttons (sanitize / download)
// ==========================================================================
function updateActionButtons() {
  const selected = state.selectedId && state.byId.get(state.selectedId);
  const tickedImages = [...state.checked].map((id) => state.byId.get(id)).filter(Boolean);
  const anySanitized = state.images.some(isSanitizedOk);

  $("checked-count").textContent = tickedImages.length ? `${tickedImages.length} ticked` : "";
  $("btn-sanitize-selected").disabled = state.busy || !selected;
  $("btn-sanitize-checked").disabled = state.busy || tickedImages.length === 0;
  $("btn-sanitize-checked").textContent = `Sanitize ticked (${tickedImages.length})`;
  $("btn-sanitize-all").disabled = state.busy || state.images.length === 0;
  $("btn-download-selected").disabled = !isSanitizedOk(selected);
  $("btn-zip-checked").disabled = !tickedImages.some(isSanitizedOk);
  $("btn-zip-all").disabled = !anySanitized;
}

function setActionStatus(text, spinning) {
  const el = $("action-status");
  el.replaceChildren();
  if (spinning) el.append(h("span", { class: "spinner" }), " ");
  if (text) el.append(text);
}

// ==========================================================================
// Inspector (detail panel)
// ==========================================================================
function renderInspector() {
  const panel = $("inspector");
  if (!state.selectedId) {
    panel.replaceChildren(...buildOverview());
    if (state.scrollToVerification) {
      state.scrollToVerification = false;
      const target = $("verification-section");
      if (target) target.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    return;
  }
  const id = state.selectedId;
  const detail = state.details.get(id);
  if (detail) {
    panel.replaceChildren(...buildImageDetail(detail));
    if (state.scrollToVerification) {
      state.scrollToVerification = false;
      const target = $("verification-section");
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    return;
  }
  // Not cached yet: show basic info immediately, then load raw metadata.
  const summary = state.byId.get(id);
  panel.replaceChildren(...buildImageHeader(summary), h("div", { class: "muted" }, h("span", { class: "spinner" }), " Loading metadata…"));
  api(`/api/images/${id}`)
    .then((data) => {
      state.details.set(id, data);
      if (state.selectedId === id) renderInspector();   // ignore if user moved on
    })
    .catch((error) => {
      if (state.selectedId === id) {
        panel.append(h("div", { class: "errors" }, `Could not load metadata: ${error.message}`));
      }
    });
}

function buildOverview() {
  const all = state.images;
  const total = all.length;
  const n = (fn) => all.filter(fn).length;
  const lines = [
    [n(FLAG_FILTERS.gps), "expose precise GPS location"],
    [n(FLAG_FILTERS.timestamp), "expose when they were taken / edited"],
    [n(FLAG_FILTERS.device), "identify the camera / phone model"],
    [n((img) => img.flags.identifier), "contain serial numbers or unique IDs"],
    [n(FLAG_FILTERS.software), "reveal software / editing history"],
    [n(FLAG_FILTERS.author), "name an author / owner / copyright holder"],
    [n((img) => img.flags.embedded), "carry an embedded thumbnail (may show the uncropped original)"],
  ].filter(([count]) => count > 0);

  const nodes = [
    h("div", { class: "inspector-head" }, h("div", { class: "inspector-title" }, "All Images – batch overview")),
    h("p", { class: "muted small" }, "Click a thumbnail, a table row or a map marker to focus the whole workspace on one image. Press Esc or click \"All Images\" to come back."),
    h("h3", null, "What this batch exposes"),
    lines.length
      ? h("ul", { class: "overview-steps" }, lines.map(([count, text]) => h("li", null, h("strong", null, `${count} of ${total}`), ` images ${text}`)))
      : h("p", { class: "finding-none" }, "No sensitive metadata was found in this batch."),
    h("h3", null, "OPSEC workflow"),
    h("ol", { class: "overview-steps" },
      h("li", null, "Tick images (or use Sanitize ALL)."),
      h("li", null, "Sanitize – a NEW copy is created, originals are never touched."),
      h("li", null, "Each copy is re-analysed with ExifTool and compared with the original."),
      h("li", null, "Download single files or a ZIP.")),
  ];

  const results = state.lastBatchResults;
  if (results && results.length) {
    const clean = results.filter((r) => r.sanitization.verified_clean).length;
    const failed = results.filter((r) => ["error", "unverified"].includes(r.sanitization.status)).length;
    const warn = results.length - clean - failed;
    nodes.push(
      h("h3", { id: "verification-section" }, "Last sanitization run"),
      h("div", { class: `verify-banner ${failed ? "verify-bad" : warn ? "verify-warn" : "verify-ok"}` },
        `${results.length} processed · ${clean} verified clean · ${warn} with remaining sensitive tags · ${failed} failed`),
      h("p", { class: "muted small" }, "Click an image to see its before/after comparison."),
      h("div", { class: "results-list" }, results.map((r) => {
        const img = state.byId.get(r.id);
        return h("button", { type: "button", class: "result-item", onclick: () => selectImage(r.id, "inspector") },
          h("img", { src: `/images/${r.id}/thumb`, alt: "" }),
          h("span", { class: "result-name" }, r.filename),
          img ? sanitizationBadge(img) : null);
      })));
  }
  return nodes;
}

function buildImageHeader(img) {
  const n = img.normalized;
  const osmLink = img.flags.gps
    ? h("a", {
      class: "btn btn-small btn-ghost",
      href: `https://www.openstreetmap.org/?mlat=${encodeURIComponent(n.gps_latitude)}&mlon=${encodeURIComponent(n.gps_longitude)}#map=16/${encodeURIComponent(n.gps_latitude)}/${encodeURIComponent(n.gps_longitude)}`,
      target: "_blank", rel: "noopener noreferrer",
      title: "Opens openstreetmap.org in a new tab (sends these coordinates to OSM)",
    }, "Open in OSM ↗")
    : null;

  return [
    h("div", { class: "inspector-head" },
      h("div", { class: "inspector-title" }, img.filename),
      h("div", { class: "inspector-nav" },
        h("button", { class: "btn btn-small", type: "button", title: "Previous (←)", onclick: () => selectRelative(-1) }, "←"),
        h("button", { class: "btn btn-small", type: "button", title: "Next (→)", onclick: () => selectRelative(1) }, "→"),
        h("button", { class: "btn btn-small", type: "button", title: "Back to All Images (Esc)", onclick: () => selectAllImages() }, "✕"))),
    h("div", { class: "preview-wrap" }, h("img", { src: `/images/${img.id}/preview`, alt: `Preview of ${img.filename}` })),
    h("div", { class: "cell-badges" }, exposureBadges(img), sanitizationBadge(img)),
    h("div", { class: "inspector-actions" },
      h("button", { class: "btn btn-primary btn-small", type: "button", disabled: state.busy, onclick: () => sanitize({ ids: [img.id] }) },
        img.sanitization ? "Sanitize again" : "Sanitize image"),
      h("button", { class: "btn btn-small", type: "button", disabled: !isSanitizedOk(img), onclick: () => downloadSanitized(img.id) }, "Download sanitized copy"),
      h("label", { class: "small" }, h("input", { type: "checkbox", id: "inspector-check", checked: state.checked.has(img.id), onchange: (e) => toggleChecked(img.id, e.target.checked) }), " ticked"),
      osmLink),
  ];
}

function buildImageDetail(detail) {
  const img = state.byId.get(detail.id) || detail;
  const n = detail.normalized || {};
  const nodes = buildImageHeader(img);

  if (detail.metadata_error) nodes.push(h("div", { class: "errors" }, `Metadata extraction problem: ${detail.metadata_error}`));
  if (detail.warnings && detail.warnings.length) {
    nodes.push(h("div", { class: "warnings small" }, h("ul", { style: "margin:0;padding-left:16px" }, detail.warnings.map((w) => h("li", null, w)))));
  }

  // --- privacy exposure findings
  nodes.push(h("h3", null, "Privacy exposure"));
  const categories = Object.keys(CATEGORY_LABELS).filter((c) => detail.sensitive && detail.sensitive[c]);
  if (categories.length === 0) {
    nodes.push(h("div", { class: "finding-none small" }, detail.metadata_error ? "Unknown (metadata could not be read)" : "No sensitive metadata found by ExifTool."));
  } else {
    nodes.push(h("div", { class: "findings" }, categories.map((c) => {
      const tags = detail.sensitive[c];
      return h("details", { class: "finding" },
        h("summary", null, h("strong", null, CATEGORY_LABELS[c]), ` – ${tags.length} tag(s)`),
        h("ul", null, tags.slice(0, 60).map((t) => h("li", null, h("span", { class: "raw-tag" }, t.key), " = ", t.value)),
          tags.length > 60 ? h("li", null, `… ${tags.length - 60} more (see raw metadata)`) : null));
    })));
  }

  // --- normalized key fields
  const gpsText = formatGps(img);
  const fields = [
    ["File type", n.file_type || img.format],
    ["MIME type", n.mime_type || img.mime_type],
    ["File size", formatBytes(img.file_size)],
    ["Dimensions", `${img.width} × ${img.height}` + (img.display_width !== img.width ? ` (displayed ${img.display_width} × ${img.display_height})` : "")],
    ["Date/time original", n.datetime_original],
    ["Create date", n.create_date],
    ["Modify date", n.modify_date],
    ["Timezone offset", n.timezone_offset],
    ["Make", n.make],
    ["Model", n.model],
    ["Lens", n.lens],
    ["Serial number", n.serial_number],
    ["Software / editor", n.software],
    ["Host computer", n.host_computer],
    ["GPS latitude / longitude", gpsText || (n.gps_status ? `not usable (${n.gps_status})` : null)],
    ["GPS altitude", n.gps_altitude !== null && n.gps_altitude !== undefined ? `${n.gps_altitude} m` : null],
    ["GPS date/time", n.gps_datetime],
    ["Orientation", n.orientation],
    ["Author / artist", n.artist],
    ["Copyright", n.copyright],
    ["Description / comment", n.description],
    ["SHA-256", img.sha256],
    ["Metadata tags", String(detail.tag_count)],
  ];
  nodes.push(h("h3", null, "Key fields"),
    h("table", { class: "kv" }, h("tbody", null, fields.map(([label, value]) =>
      h("tr", null, h("th", null, label), h("td", { class: value ? "" : "absent" }, dash(value)))))));

  // --- before/after verification
  if (detail.sanitization) nodes.push(...buildVerification(detail));

  // --- raw metadata
  nodes.push(...buildRawMetadata(detail));
  return nodes;
}

function statusLabel(status) {
  return {
    removed: "Removed",
    still_present: "STILL PRESENT",
    kept_intentionally: "Kept (not sensitive)",
    not_present: "Not present",
  }[status] || status;
}

function buildVerification(detail) {
  const s = detail.sanitization;
  const nodes = [h("h3", { id: "verification-section" }, "Before / after verification")];

  if (s.status === "error" || s.status === "unverified") {
    nodes.push(h("div", { class: "verify-banner verify-bad" }, s.status === "error" ? "✖ Sanitization failed" : "✖ Could not verify the sanitized file"),
      h("div", { class: "errors small" }, dash(s.error)));
    return nodes;
  }

  nodes.push(h("div", { class: `verify-banner ${s.verified_clean ? "verify-ok" : "verify-warn"}` },
    s.verified_clean
      ? "✔ Verified: ExifTool re-read the sanitized copy and found NO sensitive metadata."
      : `⚠ ${s.remaining_sensitive_count} sensitive tag(s) are STILL PRESENT in the sanitized copy.`));

  nodes.push(h("table", { class: "kv" }, h("tbody", null,
    h("tr", null, h("th", null, "Mode"), h("td", null, s.mode === "reencode" ? "Re-encode from pixels" : "Lossless ExifTool strip")),
    h("tr", null, h("th", null, "Metadata tags"), h("td", null, `${s.before_tag_count} → ${s.after_tag_count} (${s.removed_tags.length} removed)`)),
    h("tr", null, h("th", null, "File size"), h("td", null, `${formatBytes(detail.file_size)} → ${formatBytes(s.file_size)}`)),
    h("tr", null, h("th", null, "Image check"), h("td", null, s.image_check ? s.image_check.message : "-")),
    h("tr", null, h("th", null, "Download name"), h("td", null, s.download_name)),
    h("tr", null, h("th", null, "Sanitized at"), h("td", null, s.sanitized_at)))));

  if (s.notes && s.notes.length) nodes.push(h("ul", { class: "notes" }, s.notes.map((note) => h("li", null, note))));

  // Field-by-field comparison (only fields that existed before or after)
  const rows = s.field_comparison.filter((r) => r.status !== "not_present");
  nodes.push(h("table", { class: "compare" },
    h("thead", null, h("tr", null, h("th", null, "Field"), h("th", null, "Original"), h("th", null, "Sanitized"), h("th", null, "Result"))),
    h("tbody", null, rows.length ? rows.map((r) => h("tr", null,
      h("td", null, r.field),
      h("td", null, dash(r.before)),
      h("td", null, r.after === null ? "—" : r.after),
      h("td", { class: `st-${r.status}` }, statusLabel(r.status))))
      : h("tr", null, h("td", { colspan: "4", class: "muted" }, "The original had none of the key fields.")))));

  // Category comparison
  const catRows = s.category_comparison.filter((r) => r.status !== "not_present");
  if (catRows.length) {
    nodes.push(h("table", { class: "compare", style: "margin-top:8px" },
      h("thead", null, h("tr", null, h("th", null, "Category"), h("th", null, "Tags before"), h("th", null, "Tags after"), h("th", null, "Result"))),
      h("tbody", null, catRows.map((r) => h("tr", null,
        h("td", null, r.label), h("td", null, String(r.before_count)), h("td", null, String(r.after_count)),
        h("td", { class: `st-${r.status}` }, statusLabel(r.status)))))));
  }

  if (s.remaining_sensitive_count > 0) {
    const list = [];
    for (const [cat, tags] of Object.entries(s.remaining_sensitive)) {
      for (const t of tags) list.push(h("li", null, `[${CATEGORY_LABELS[cat] || cat}] `, h("span", { class: "raw-tag" }, t.key), " = ", t.value));
    }
    nodes.push(h("div", { class: "errors small", style: "margin-top:8px" }, h("strong", null, "Still present:"), h("ul", null, list)));
  }

  nodes.push(
    h("details", { class: "collapsible" },
      h("summary", null, `Metadata remaining in sanitized file (${s.remaining_tags.length} tags – structural/technical)`),
      h("table", { class: "kv" }, h("tbody", null, s.remaining_tags.map((t) => h("tr", { class: t.category ? "sensitive-row" : "" },
        h("th", { class: "raw-tag" }, t.key, t.category ? h("span", { class: "cat-label" }, CATEGORY_LABELS[t.category]) : null),
        h("td", null, t.value)))))),
    h("details", { class: "collapsible" },
      h("summary", null, `Removed tags (${s.removed_tags.length})`),
      h("ul", { class: "tag-list" }, s.removed_tags.map((key) => h("li", null, key)))),
    h("div", { class: "inspector-actions" },
      h("button", { class: "btn btn-primary btn-small", type: "button", disabled: !isSanitizedOk(detail), onclick: () => downloadSanitized(detail.id) }, `Download ${s.download_name}`)),
  );
  return nodes;
}

function buildRawMetadata(detail) {
  const raw = detail.raw || {};
  const keys = Object.keys(raw);
  const sensitiveKeys = new Map();
  for (const [cat, tags] of Object.entries(detail.sensitive || {})) {
    for (const t of tags) sensitiveKeys.set(t.key, cat);
  }
  const tbody = h("tbody");
  const fill = (query) => {
    const q = query.trim().toLowerCase();
    const rows = [];
    let lastGroup = null;
    for (const key of keys) {
      const value = raw[key];
      if (q && !key.toLowerCase().includes(q) && !String(value).toLowerCase().includes(q)) continue;
      const cut = key.lastIndexOf(":");
      const group = cut === -1 ? "" : key.slice(0, cut);
      const tag = cut === -1 ? key : key.slice(cut + 1);
      if (group !== lastGroup) {
        rows.push(h("tr", { class: "group-row" }, h("th", { colspan: "2" }, group || "(no group)")));
        lastGroup = group;
      }
      const cat = sensitiveKeys.get(key);
      rows.push(h("tr", { class: cat ? "sensitive-row" : "" },
        h("th", { class: "raw-tag", title: key }, tag, cat ? h("span", { class: "cat-label" }, CATEGORY_LABELS[cat]) : null),
        h("td", null, value)));
    }
    if (rows.length === 0) rows.push(h("tr", null, h("td", { colspan: "2", class: "muted" }, keys.length ? "No tag matches." : "No metadata.")));
    tbody.replaceChildren(...rows);
  };
  fill("");
  return [
    h("h3", null, `Raw metadata (${keys.length} tags, ExifTool)`),
    h("div", { class: "muted small" }, "Group = ExifTool family-0:family-1 group. Rows marked red belong to a sensitive category. Filesystem tags of the temporary server copy are hidden."),
    h("input", { type: "search", class: "raw-filter", placeholder: "Filter tags or values…", oninput: (e) => fill(e.target.value) }),
    h("div", { class: "raw-scroll" }, h("table", { class: "kv" }, tbody)),
  ];
}

// ==========================================================================
// Upload
// ==========================================================================

/** Read files from a drop event, including whole folders (recursively). */
async function filesFromDrop(dataTransfer) {
  const items = [...(dataTransfer.items || [])];
  // webkitGetAsEntry must be called synchronously, before any await.
  const entries = items.map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null)).filter(Boolean);
  if (entries.length === 0) return [...dataTransfer.files];

  const files = [];
  async function walk(entry) {
    if (entry.isFile) {
      const file = await new Promise((resolve) => entry.file(resolve, () => resolve(null)));
      if (file) files.push(file);
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      // readEntries returns results in pages: keep calling until it returns nothing.
      for (;;) {
        const batch = await new Promise((resolve) => reader.readEntries(resolve, () => resolve([])));
        if (batch.length === 0) break;
        for (const child of batch) await walk(child);
      }
    }
  }
  for (const entry of entries) await walk(entry);
  return files;
}

function preValidate(files) {
  const accepted = [];
  const rejected = [];
  const maxBytes = window.PHOTO_TOOL_MAX_FILE_SIZE || 50 * 1024 * 1024;
  for (const file of files) {
    const ext = extensionOf(file.name);
    if (file.name.startsWith(".")) continue;                      // hidden files (.DS_Store …)
    if (!ALLOWED_EXTENSIONS.includes(ext)) rejected.push({ filename: file.name, error: `Skipped: unsupported extension '.${ext}'` });
    else if (file.size === 0) rejected.push({ filename: file.name, error: "Skipped: file is empty" });
    else if (file.size > maxBytes) rejected.push({ filename: file.name, error: `Skipped: larger than ${Math.round(maxBytes / 1048576)} MB` });
    else accepted.push(file);
  }
  return { accepted, rejected };
}

function chunkFiles(files) {
  const chunks = [];
  let current = [];
  let bytes = 0;
  for (const file of files) {
    if (current.length && (current.length >= UPLOAD_CHUNK_FILES || bytes + file.size > UPLOAD_CHUNK_BYTES)) {
      chunks.push(current);
      current = [];
      bytes = 0;
    }
    current.push(file);
    bytes += file.size;
  }
  if (current.length) chunks.push(current);
  return chunks;
}

async function uploadFiles(fileList) {
  if (state.busy) { toast("Please wait for the current operation to finish."); return; }
  const files = [...fileList];
  if (files.length === 0) { toast("No files selected.", "error"); return; }
  const { accepted, rejected } = preValidate(files);
  state.uploadErrors.push(...rejected);
  if (accepted.length === 0) {
    renderUploadErrors();
    renderAll();
    toast("None of the files is a supported image.", "error");
    return;
  }

  state.busy = true;
  updateActionButtons();
  const progress = $("upload-progress");
  progress.classList.remove("hidden");
  let done = 0;
  let added = 0;
  const setProgress = () => {
    $("upload-progress-fill").style.width = `${Math.round((done / accepted.length) * 100)}%`;
    $("upload-progress-text").textContent = `Uploading & analyzing locally: ${done} / ${accepted.length} files processed, ${added} added`;
  };
  setProgress();

  for (const chunk of chunkFiles(accepted)) {
    const form = new FormData();
    for (const file of chunk) form.append("files", file, file.name);
    try {
      const data = await api("/api/upload", { method: "POST", body: form });
      storeImages(data.images);
      added += data.images.length;
      state.uploadErrors.push(...data.errors);
    } catch (error) {
      for (const file of chunk) state.uploadErrors.push({ filename: file.name, error: `Upload failed: ${error.message}` });
    }
    done += chunk.length;
    setProgress();
    renderAll();             // thumbnails appear progressively
  }

  state.busy = false;
  $("upload-progress-text").textContent = `Done: ${added} image(s) added, ${accepted.length - added + rejected.length} rejected/skipped.`;
  setTimeout(() => progress.classList.add("hidden"), 4000);
  renderAll();
  toast(`${added} image(s) analyzed locally.`, added ? "ok" : "error");
}

function renderUploadErrors() {
  const box = $("upload-errors");
  if (state.uploadErrors.length === 0) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  $("upload-errors-summary").textContent = `${state.uploadErrors.length} file(s) rejected or skipped – click for details`;
  $("upload-errors-list").replaceChildren(...state.uploadErrors.map((e) =>
    h("li", null, h("strong", null, e.filename), ": ", e.error)));
}

// ==========================================================================
// Sanitize + download
// ==========================================================================

async function sanitize(target) {
  if (state.busy) return;
  const payload = {
    mode: $("sanitize-mode").value,
    keep_icc: $("keep-icc").checked,
  };
  let count;
  if (target.all) { payload.all = true; count = state.images.length; } else { payload.ids = target.ids; count = target.ids.length; }
  if (count === 0) return;

  state.busy = true;
  updateActionButtons();
  setActionStatus(`Sanitizing ${count} image(s) and re-verifying with ExifTool…`, true);
  try {
    const data = await api("/api/sanitize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    storeImages(data.images);
    state.lastBatchResults = data.results;
    const clean = data.results.filter((r) => r.sanitization.verified_clean).length;
    const failed = data.results.filter((r) => ["error", "unverified"].includes(r.sanitization.status)).length;
    const message = `Sanitized ${data.results.length}: ${clean} verified clean, ${data.results.length - clean - failed} with remaining tags, ${failed} failed.`;
    setActionStatus(message, false);
    toast(message, failed ? "error" : "ok");
    state.scrollToVerification = true;
  } catch (error) {
    setActionStatus(`Sanitization failed: ${error.message}`, false);
    toast(`Sanitization failed: ${error.message}`, "error");
  } finally {
    state.busy = false;
    renderAll();
  }
}

function downloadSanitized(id) {
  const link = h("a", { href: `/images/${id}/sanitized`, download: "" });
  document.body.append(link);
  link.click();
  link.remove();
}

/** ZIP download via a normal form POST so the browser handles the file. */
function downloadZip(ids) {
  const form = h("form", { method: "POST", action: "/download/zip", style: "display:none" });
  if (ids === "all") form.append(h("input", { type: "hidden", name: "all", value: "1" }));
  else form.append(h("input", { type: "hidden", name: "ids", value: ids.join(",") }));
  document.body.append(form);
  form.submit();
  form.remove();
}

// ==========================================================================
// Event wiring
// ==========================================================================

function wireEvents() {
  // Upload
  const dropZone = $("drop-zone");
  $("btn-pick-files").addEventListener("click", (e) => { e.stopPropagation(); $("input-files").click(); });
  $("btn-pick-folder").addEventListener("click", (e) => { e.stopPropagation(); $("input-folder").click(); });
  dropZone.addEventListener("click", () => $("input-files").click());
  dropZone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("input-files").click(); } });
  for (const id of ["input-files", "input-folder"]) {
    $(id).addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });
  }
  dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("dragover"); });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
  dropZone.addEventListener("drop", async (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    const files = await filesFromDrop(e.dataTransfer);
    uploadFiles(files);
  });
  // Avoid the browser opening a file that is dropped outside the drop zone.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  // Gallery toolbar
  $("btn-all-images").addEventListener("click", selectAllImages);
  $("btn-check-visible").addEventListener("click", () => { for (const img of visibleImages()) state.checked.add(img.id); updateCheckedUI(); });
  $("btn-uncheck-all").addEventListener("click", () => { state.checked.clear(); updateCheckedUI(); });
  $("toggle-gallery-expand").addEventListener("change", (e) => $("gallery").classList.toggle("expanded", e.target.checked));

  // Filters
  let searchTimer = null;
  $("filter-search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.filters.search = e.target.value.trim().toLowerCase(); renderAll(); }, 150);
  });
  for (const box of document.querySelectorAll("input[data-filter]")) {
    box.addEventListener("change", () => toggleFlagFilter(box.dataset.filter));
  }
  $("btn-clear-filters").addEventListener("click", () => { clearFilters(); renderAll(); });

  // Table sorting + tick-all
  for (const th of document.querySelectorAll("#meta-table th[data-sort]")) {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.sort.key === key) {
        if (state.sort.dir === 1) state.sort.dir = -1;
        else state.sort = { key: null, dir: 1 };          // third click: back to upload order
      } else {
        state.sort = { key, dir: 1 };
      }
      renderGallery();
      renderTable();
      updateSelectionUI(null);
    });
  }
  $("table-check-all").addEventListener("change", (e) => {
    for (const img of visibleImages()) {
      if (e.target.checked) state.checked.add(img.id);
      else state.checked.delete(img.id);
    }
    updateCheckedUI();
  });

  // Map toolbar
  $("btn-map-fit").addEventListener("click", fitMapToMarkers);
  $("toggle-map-path").addEventListener("change", () => renderMapMarkers());
  $("toggle-map-tiles").addEventListener("change", (e) => setTilesEnabled(e.target.checked));

  // Sanitize / download
  $("btn-sanitize-selected").addEventListener("click", () => state.selectedId && sanitize({ ids: [state.selectedId] }));
  $("btn-sanitize-checked").addEventListener("click", () => sanitize({ ids: [...state.checked] }));
  $("btn-sanitize-all").addEventListener("click", () => sanitize({ all: true }));
  $("btn-download-selected").addEventListener("click", () => state.selectedId && downloadSanitized(state.selectedId));
  $("btn-zip-checked").addEventListener("click", () => downloadZip([...state.checked]));
  $("btn-zip-all").addEventListener("click", () => downloadZip("all"));

  // Clear workspace
  $("btn-clear-workspace").addEventListener("click", async () => {
    if (!confirm("Delete all uploaded images, thumbnails and sanitized copies from this workspace?")) return;
    try {
      await api("/api/clear", { method: "POST" });
      state.images = [];
      state.byId.clear();
      state.details.clear();
      state.checked.clear();
      state.selectedId = null;
      state.lastBatchResults = null;
      state.uploadErrors = [];
      clearFilters();
      recomputeDerived();
      if (mapState.markerLayer) { mapState.markerLayer.clearLayers(); mapState.markers.clear(); }
      setActionStatus("", false);
      renderAll();
      toast("Workspace cleared.", "ok");
    } catch (error) {
      toast(`Could not clear workspace: ${error.message}`, "error");
    }
  });

  // Keyboard navigation
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (["input", "select", "textarea"].includes(tag) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (state.images.length === 0) return;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); selectRelative(1); }
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); selectRelative(-1); }
    else if (e.key === "Escape") { selectAllImages(); }
  });

  window.addEventListener("resize", () => mapState.map && mapState.map.invalidateSize());
}

// ==========================================================================
// Start-up
// ==========================================================================
async function init() {
  wireEvents();
  try {
    const status = await api("/api/status");
    window.PHOTO_TOOL_MAX_FILE_SIZE = status.max_file_size;
    // Restore the workspace if the page was reloaded while the server kept running.
    const data = await api("/api/images");
    storeImages(data.images);
  } catch (error) {
    toast(`Could not reach the local server: ${error.message}`, "error");
  }
  renderAll();
}

document.addEventListener("DOMContentLoaded", init);
