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
         renderProperties()             <- Properties dialog for the selected image

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
// Replaced at start-up with the server's list (/api/status), which includes
// heic/heif only when pillow-heif is installed.
let ALLOWED_EXTENSIONS = ["jpg", "jpeg", "jpe", "png", "webp", "tif", "tiff", "heic", "heif"];
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
  gps: (img) => !!img.flags.gps,                       // hemisphere known -> on the map
  gps_ambiguous: (img) => !!img.flags.gps_ambiguous,   // values present, hemisphere unknown
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
  propertiesTab: "general",  // active tab of the Properties dialog
  corrCollapsed: new Set(),  // correlation tree groups the user collapsed
  uploadPaneHidden: false,   // View → Add Images Pane
  summaryPaneHidden: false,  // View → Batch Summary Pane
  serverStatus: null,        // /api/status (ExifTool version, limits...)
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

/** Signed coordinates - ONLY for resolved ("valid") GPS. Never for ambiguous GPS. */
function formatGps(img) {
  const n = img.normalized;
  if (n.gps_status !== "valid") return null;
  return `${Number(n.gps_latitude).toFixed(5)}, ${Number(n.gps_longitude).toFixed(5)}`;
}

// Human labels for normalized.gps_status (same states as photo_tool/gps.py)
const GPS_STATUS_LABELS = {
  valid: "Resolved (hemisphere known)",
  zero: "0,0 placeholder – not plotted",
  ambiguous: "Ambiguous – hemisphere unknown, not plotted",
  invalid: "Out of range – not plotted",
  incomplete: "Incomplete – not plotted",
};

/** One-line GPS description for any status (raw values shown for ambiguous GPS). */
function gpsSummary(img) {
  const n = img.normalized;
  if (!n.gps_status) return null;
  if (n.gps_status === "valid") return formatGps(img);
  if (n.gps_status === "zero") return "0, 0 (placeholder – not plotted)";
  if (n.gps_status === "ambiguous") return `${n.gps_display} – hemisphere unknown, not plotted`;
  return `${n.gps_display ? `${n.gps_display} – ` : ""}${GPS_STATUS_LABELS[n.gps_status] || n.gps_status}`;
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
    case "gps": return img.flags.gps ? 2 : img.flags.gps_ambiguous ? 1 : null;
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
  if (f.gps) out.push(badge("GPS", "b-gps", "GPS coordinates with known hemisphere (shown on the map)"));
  else if (f.gps_ambiguous) out.push(badge("GPS ±?", "b-gpsamb", img.normalized.gps_message || "GPS coordinates present, hemisphere unknown"));
  else if (f.location_tags) out.push(badge("GPS?", "b-gpsx", "Location tags present but no usable coordinates (0,0 / invalid / incomplete / place names)"));
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
  $("workspace").classList.toggle("hidden", !hasImages);
  $("summary-section").classList.toggle("hidden", !hasImages || state.summaryPaneHidden);
  // The Add Images pane is always shown while the workspace is empty.
  $("upload-section").classList.toggle("hidden", hasImages && state.uploadPaneHidden);
  $("upload-section").classList.toggle("compact", hasImages);
  renderUploadErrors();
  if (hasImages) {
    ensureMap();
    renderStats();
    renderGallery();
    renderTable();
    renderMapMarkers();
    renderCorrelation();
    renderFilterControls();
    updateSelectionUI(null);
    renderProperties();
  }
  updateActionButtons();
  renderChrome();
}

// --------------------------------------------------------------------------
// Stats (Batch Summary pane)
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
  const scrollToCorrelation = () => $("correlation-section").scrollIntoView({ block: "nearest" });

  const cards = [
    { label: "Total images", value: total, sub: formatBytes(all.reduce((s, i) => s + i.file_size, 0)), onclick: () => selectAllImages() },
    { label: "With GPS (resolved)", value: count(FLAG_FILTERS.gps), sub: pct(count(FLAG_FILTERS.gps)), cls: "s-gps", filter: "gps" },
    { label: "Ambiguous GPS", value: count(FLAG_FILTERS.gps_ambiguous), sub: count(FLAG_FILTERS.gps_ambiguous) ? "hemisphere unknown" : "", cls: count(FLAG_FILTERS.gps_ambiguous) ? "s-amb" : "", filter: "gps_ambiguous" },
    { label: "With timestamps", value: count(FLAG_FILTERS.timestamp), sub: pct(count(FLAG_FILTERS.timestamp)), cls: "s-time", filter: "timestamp" },
    { label: "With camera/device", value: count(FLAG_FILTERS.device), sub: pct(count(FLAG_FILTERS.device)), cls: "s-dev", filter: "device" },
    { label: "With software/editor", value: count(FLAG_FILTERS.software), sub: pct(count(FLAG_FILTERS.software)), cls: "s-sw", filter: "software" },
    { label: "With author/copyright", value: count(FLAG_FILTERS.author), sub: pct(count(FLAG_FILTERS.author)), cls: "s-auth", filter: "author" },
    { label: "Unique devices", value: devices.size, sub: [...devices].slice(0, 2).join(", "), onclick: scrollToCorrelation },
    { label: "Unique software", value: software.size, sub: [...software].slice(0, 2).join(", "), onclick: scrollToCorrelation },
    { label: "Sanitized / clean", value: `${sanitized.length} / ${clean}`, sub: "filter: not yet sanitized", cls: "s-clean", filter: "unsanitized" },
    {
      label: "Rejected files", value: state.uploadErrors.length, sub: state.uploadErrors.length ? "click for details" : "",
      cls: state.uploadErrors.length ? "s-bad" : "",
      onclick: () => {
        state.uploadPaneHidden = false;
        renderAll();
        const box = $("upload-errors");
        box.open = true;
        box.scrollIntoView({ block: "nearest" });
      },
    },
  ];

  $("stats-grid").replaceChildren(...cards.map((card) => {
    const active = card.filter && state.filters.flags.has(card.filter);
    return h("button", {
      type: "button",
      class: `stat ${card.cls || ""} ${active ? "active" : ""}`,
      title: card.filter ? "Click to toggle this filter" : null,
      onclick: card.filter ? () => toggleFlagFilter(card.filter) : card.onclick,
    },
    h("span", { class: "stat-line" },
      h("span", { class: "stat-value" }, String(card.value)),
      card.sub ? h("span", { class: "stat-sub" }, card.sub) : null),
    h("span", { class: "stat-label" }, card.label));
  }));
}

// --------------------------------------------------------------------------
// Window chrome: address bar (breadcrumb) + status bar
// --------------------------------------------------------------------------
function renderChrome() {
  renderAddressBar();
  renderStatusBar();
}

function renderAddressBar() {
  const box = $("address-box");
  const total = state.images.length;
  const visible = total ? visibleImages().length : 0;
  const selected = state.selectedId && state.byId.get(state.selectedId);
  const sep = () => h("span", { class: "crumb-sep" }, "▸");
  const parts = [
    h("span", { class: "crumb-folder", "aria-hidden": "true" }),
    h("button", { type: "button", class: "crumb", onclick: () => selectAllImages(), title: "Back to All Images" }, "Workspace"),
    sep(),
    h("button", { type: "button", class: "crumb", onclick: () => selectAllImages(), title: "Show the whole batch (Esc)" }, "All Images"),
  ];
  if (total && visible !== total) {
    parts.push(sep(), h("span", { class: "crumb" }, `Filtered (${visible} of ${total})`));
  }
  if (selected) {
    parts.push(sep(), h("span", { class: "crumb", title: selected.filename }, selected.filename));
  }
  if (!total) parts.push(h("span", { class: "crumb-note" }, "empty – add images to start"));
  box.replaceChildren(...parts);
}

function renderStatusBar() {
  const all = state.images;
  const total = all.length;
  const gps = all.filter(FLAG_FILTERS.gps).length;
  const ambiguous = all.filter(FLAG_FILTERS.gps_ambiguous).length;
  const timestamps = all.filter(FLAG_FILTERS.timestamp).length;
  const clean = all.filter((img) => img.sanitization && img.sanitization.verified_clean).length;
  const ticked = tickedImages().length;

  const counts = total
    ? [`${total} image${total === 1 ? "" : "s"}`, `${gps} GPS`, `${timestamps} timestamps`]
    : ["No images"];
  if (ambiguous) counts.splice(2, 0, `${ambiguous} ambiguous GPS`);
  if (clean) counts.push(`${clean} sanitized`);
  if (ticked) counts.push(`${ticked} ticked`);
  $("sb-counts").textContent = counts.join("  |  ");

  const selected = state.selectedId && state.byId.get(state.selectedId);
  let info = "";
  if (selected) {
    const n = selected.normalized;
    info = [`Selected: ${selected.filename}`, n.device, n.timestamp, `${selected.width} × ${selected.height}`, formatBytes(selected.file_size)]
      .filter(Boolean).join("  ·  ");
  } else if (total) {
    const visible = visibleImages().length;
    info = visible === total ? "All Images" : `${visible} of ${total} images shown (filtered)`;
  }
  $("sb-selection").textContent = info;
}

let statusTimer = null;
/** Right-most status bar field: "Ready", or a message / progress spinner. */
function setActionStatus(text, spinning) {
  const el = $("action-status");
  clearTimeout(statusTimer);
  el.replaceChildren();
  if (spinning) el.append(h("span", { class: "spinner" }));
  el.append(text || "Ready");
  if (!spinning && text) {
    statusTimer = setTimeout(() => { if (!state.busy) el.replaceChildren("Ready"); }, 8000);
  }
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
    if (n.gps_status === "ambiguous") {
      // Raw values stay visible; "?" marks the axis whose hemisphere is unknown.
      const axis = (raw, ref) => `${raw === null || raw === undefined ? "?" : Number(raw).toFixed(5)} ${ref || "?"}`;
      gpsCell = h("span", { title: n.gps_message },
        h("div", null, axis(n.gps_raw_latitude, n.gps_latitude_ref)),
        h("div", null, axis(n.gps_raw_longitude, n.gps_longitude_ref)),
        h("span", { class: "badge b-gpsamb" }, "hemisphere?"));
    } else if (n.gps_status === "zero") gpsCell = "0, 0 (ignored)";
    else if (!img.flags.gps && n.gps_status) gpsCell = h("span", { title: n.gps_message || "" }, `(${n.gps_status})`);
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
    ? { radius: 11, color: "#0b3a6e", weight: 3, fillColor: "#3399ff", fillOpacity: 1 }
    : { radius: 7, color: "#ffffff", weight: 2, fillColor: "#d9534a", fillOpacity: 0.95 };
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
  const ambiguousCount = state.images.filter((img) => img.flags.gps_ambiguous).length;
  $("map-empty").textContent = anyGpsAtAll
    ? "No images with resolved GPS match the current filters."
    : ambiguousCount
      ? `No resolvable GPS locations. ${ambiguousCount} image(s) have GPS coordinates with a missing hemisphere reference – not plotted.`
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
  const colors = ["#1f6fd1", "#7b4fc4", "#1f8f84", "#c97a00", "#c2387a", "#5a8a00"];
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
  if (total) notes.push(`${total} image(s) with resolved GPS on the map.`);
  const ambiguous = state.images.filter((img) => img.flags.gps_ambiguous).length;
  if (ambiguous) notes.push(`${ambiguous} image(s) have GPS coordinates with an unknown hemisphere – NOT plotted (a wrong marker would be worse than none).`);
  const ignored = state.images.filter((img) => !img.flags.gps && !img.flags.gps_ambiguous && img.normalized.gps_status).length;
  if (ignored) notes.push(`${ignored} image(s) have GPS tags that could not be plotted (0,0 / invalid / incomplete).`);
  if (!mapState.tiles) notes.push("Online tiles are OFF: markers are shown on a blank background and no requests go to OpenStreetMap.");
  const sel = state.selectedId && state.byId.get(state.selectedId);
  if (sel && sel.flags.gps_ambiguous) notes.push("The selected image has GPS coordinates but no usable hemisphere reference, so it is not on the map.");
  else if (sel && !sel.flags.gps) notes.push("The selected image has no resolved GPS coordinates.");
  $("map-note").textContent = notes.join(" ");
}

// --------------------------------------------------------------------------
// Correlation panel (tree view)
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
    groups.push(h("details", {
      class: "corr-group",
      open: !state.corrCollapsed.has(field),
      ontoggle: (event) => {
        if (event.target.open) state.corrCollapsed.delete(field);
        else state.corrCollapsed.add(field);
      },
    },
    h("summary", null, `${config.label} (${entries.length})`),
    h("div", { class: "corr-items" },
      items.length ? items : h("div", { class: "corr-empty" }, config.onlyDuplicates ? "none found" : "no values"),
      missing && items.length ? h("div", { class: "corr-empty" }, `${missing} image(s) without a value`) : null)));
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
  renderProperties();
  updateActionButtons();
  updateMapNote();
  // On narrow screens the Properties panel sits below everything else: bring it into view.
  if (id && ["gallery", "table", "map"].includes(source) && window.matchMedia("(max-width: 1000px)").matches) {
    $("inspector").scrollIntoView({ block: "start" });
  }
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
  renderChrome();
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
function tickedImages() {
  return [...state.checked].map((id) => state.byId.get(id)).filter(Boolean);
}

function toggleChecked(id, checked) {
  if (checked) state.checked.add(id);
  else state.checked.delete(id);
  updateCheckedUI();
}

function tickVisible() {
  for (const img of visibleImages()) state.checked.add(img.id);
  updateCheckedUI();
}

function untickAll() {
  state.checked.clear();
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
  const propertiesBox = $("prop-check");
  if (propertiesBox) propertiesBox.checked = state.checked.has(state.selectedId);
}

// ==========================================================================
// COMMANDS – every menu item, toolbar button and caption button
// ==========================================================================
// Elements carry data-command="name". COMMANDS[name] says:
//   run()      what happens on click
//   enabled()  optional, false -> element is disabled
//   checked()  optional, true  -> menu item shows a check mark / radio dot
//   label()    optional, dynamic menu label text
// Option commands (mode, HEIC output, map toggles...) only flip the REAL
// form controls and fire their "change" event, so there is exactly one
// source of truth for every setting.

const hasImages = () => state.images.length > 0;
const selectedImage = () => (state.selectedId ? state.byId.get(state.selectedId) : null);

function toggleControl(id) {
  const box = $(id);
  box.checked = !box.checked;
  box.dispatchEvent(new Event("change"));
}

function setControlValue(id, value) {
  const control = $(id);
  control.value = value;
  control.dispatchEvent(new Event("change"));
}

function filterCommand(key) {
  return { run: () => toggleFlagFilter(key), checked: () => state.filters.flags.has(key), enabled: hasImages };
}

const COMMANDS = {
  // File
  "add-images": { run: () => $("input-files").click(), enabled: () => !state.busy },
  "add-folder": { run: () => $("input-folder").click(), enabled: () => !state.busy },
  "download-selected": { run: () => downloadSanitized(state.selectedId), enabled: () => isSanitizedOk(selectedImage()) },
  "zip-ticked": {
    run: () => downloadZip([...state.checked]),
    enabled: () => tickedImages().some(isSanitizedOk),
    label: () => `Export Ticked Sanitized as ZIP (${tickedImages().filter(isSanitizedOk).length})`,
  },
  "zip-all": { run: () => downloadZip("all"), enabled: () => state.images.some(isSanitizedOk) },
  "clear-workspace": { run: () => clearWorkspace(), enabled: () => !state.busy },
  // Analyze
  analyze: { run: () => showBatchAnalysis(), enabled: hasImages },
  "prev-image": { run: () => selectRelative(-1), enabled: hasImages },
  "next-image": { run: () => selectRelative(1), enabled: hasImages },
  "filter-gps": filterCommand("gps"),
  "filter-gps-ambiguous": filterCommand("gps_ambiguous"),
  "filter-timestamp": filterCommand("timestamp"),
  "filter-device": filterCommand("device"),
  "filter-software": filterCommand("software"),
  "filter-author": filterCommand("author"),
  "filter-unsanitized": filterCommand("unsanitized"),
  "clear-filters": { run: () => { clearFilters(); renderAll(); }, enabled: hasImages },
  // View
  "toggle-upload-pane": { run: () => { state.uploadPaneHidden = !state.uploadPaneHidden; renderAll(); }, checked: () => !state.uploadPaneHidden },
  "toggle-summary-pane": { run: () => { state.summaryPaneHidden = !state.summaryPaneHidden; renderAll(); }, checked: () => !state.summaryPaneHidden },
  "toggle-expand-thumbs": { run: () => toggleControl("toggle-gallery-expand"), checked: () => $("toggle-gallery-expand").checked },
  "toggle-map-path": { run: () => toggleControl("toggle-map-path"), checked: () => $("toggle-map-path").checked },
  "toggle-map-tiles": { run: () => toggleControl("toggle-map-tiles"), checked: () => $("toggle-map-tiles").checked },
  "fit-map": { run: () => fitMapToMarkers(), enabled: () => mapState.markers.size > 0 },
  "window-maximize": { run: () => toggleMaximize(), checked: () => $("app-window").classList.contains("maximized") },
  "window-minimize": { run: () => $("app-window").classList.toggle("rolled-up") },
  // Tools
  "sanitize-selected": { run: () => sanitize({ ids: [state.selectedId] }), enabled: () => !state.busy && !!selectedImage() },
  "sanitize-ticked": {
    run: () => sanitize({ ids: [...state.checked] }),
    enabled: () => !state.busy && tickedImages().length > 0,
    label: () => `Sanitize Ticked Images (${tickedImages().length})`,
  },
  "sanitize-all": { run: () => sanitize({ all: true }), enabled: () => !state.busy && hasImages() },
  "mode-reencode": { run: () => setControlValue("sanitize-mode", "reencode"), checked: () => $("sanitize-mode").value === "reencode" },
  "mode-lossless": { run: () => setControlValue("sanitize-mode", "lossless"), checked: () => $("sanitize-mode").value === "lossless" },
  "heic-jpeg": { run: () => setControlValue("heic-output", "jpeg"), checked: () => $("heic-output").value === "jpeg", enabled: () => !$("heic-output").disabled },
  "heic-keep": { run: () => setControlValue("heic-output", "heic"), checked: () => $("heic-output").value === "heic", enabled: () => !$("heic-output").disabled },
  "keep-icc": { run: () => toggleControl("keep-icc"), checked: () => $("keep-icc").checked },
  "tick-visible": { run: () => tickVisible(), enabled: hasImages },
  "untick-all": { run: () => untickAll(), enabled: () => state.checked.size > 0 },
  // Help
  "help-shortcuts": { run: () => showShortcutsDialog() },
  "help-privacy": { run: () => showPrivacyDialog() },
  "help-about": { run: () => showAboutDialog() },
};

function runCommand(name) {
  const command = COMMANDS[name];
  if (!command) return;
  closeMenus();
  if (command.enabled && !command.enabled()) return;
  command.run();
  refreshCommandStates();
}

/** Re-evaluate enabled / checked / label for every [data-command] element. */
function refreshCommandStates() {
  for (const el of document.querySelectorAll("[data-command]")) {
    const command = COMMANDS[el.dataset.command];
    if (!command) continue;
    el.disabled = command.enabled ? !command.enabled() : false;
    if (command.checked) {
      const on = !!command.checked();
      el.classList.toggle("checked", on);
      el.setAttribute("aria-checked", on ? "true" : "false");
    }
    if (command.label) {
      const label = el.querySelector(".mi-label");
      if (label) label.textContent = command.label();
    }
  }
}

/** Kept under its old name: called after anything that changes what is possible. */
function updateActionButtons() {
  const ticked = tickedImages().length;
  $("checked-count").textContent = ticked ? `${ticked} ticked` : "";
  refreshCommandStates();
  renderStatusBar();
}

function showBatchAnalysis() {
  state.propertiesTab = "general";
  selectAllImages();
  $("client").scrollTop = 0;
}

function toggleMaximize() {
  const maximized = $("app-window").classList.toggle("maximized");
  document.querySelector(".desktop").classList.toggle("maximized", maximized);
  if (mapState.map) setTimeout(() => mapState.map.invalidateSize(), 50);
}

async function clearWorkspace() {
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
    setActionStatus("Workspace cleared", false);
    renderAll();
    toast("Workspace cleared.", "ok");
  } catch (error) {
    toast(`Could not clear workspace: ${error.message}`, "error");
  }
}

// --------------------------------------------------------------------------
// Menus (menu bar + toolbar drop-downs)
// --------------------------------------------------------------------------
function openMenu(menu) {
  refreshCommandStates();
  menu.classList.add("open");
  // Keep the drop-down inside the viewport (small screens / right-hand menus).
  const dropdown = menu.querySelector(".menu-dropdown");
  dropdown.style.left = "";
  const rect = dropdown.getBoundingClientRect();
  const overflow = rect.right - (window.innerWidth - 6);
  if (overflow > 0) dropdown.style.left = `${-Math.min(overflow, rect.left - 6)}px`;
}

function closeMenus() {
  for (const menu of document.querySelectorAll(".menu.open")) menu.classList.remove("open");
}

function wireMenus() {
  for (const title of document.querySelectorAll(".menu > .menu-title")) {
    const menu = title.parentElement;
    title.addEventListener("click", (event) => {
      event.stopPropagation();
      const wasOpen = menu.classList.contains("open");
      closeMenus();
      if (!wasOpen) openMenu(menu);
    });
    // Like Windows: once a menu-bar menu is open, hovering another title switches to it.
    title.addEventListener("mouseenter", () => {
      const open = document.querySelector(".menubar .menu.open");
      if (open && open !== menu && menu.closest(".menubar")) {
        closeMenus();
        openMenu(menu);
      }
    });
  }
  document.addEventListener("click", (event) => {
    const commandEl = event.target.closest("[data-command]");
    if (commandEl && !commandEl.disabled) {
      runCommand(commandEl.dataset.command);
      return;
    }
    if (!event.target.closest(".menu")) closeMenus();
  });
}

// --------------------------------------------------------------------------
// Dialogs (Help / About)
// --------------------------------------------------------------------------
function showDialog(title, nodes) {
  closeMenus();
  $("dialog-title").textContent = title;
  $("dialog-content").replaceChildren(...nodes);
  $("dialog-backdrop").classList.remove("hidden");
  $("dialog-ok").focus();
}

function closeDialog() {
  $("dialog-backdrop").classList.add("hidden");
}

const isDialogOpen = () => !$("dialog-backdrop").classList.contains("hidden");

function showShortcutsDialog() {
  const rows = [
    ["Ctrl+O", "Add images"],
    ["Ctrl+S", "Save sanitized copy of the selected image"],
    ["← / →", "Previous / next image (table order)"],
    ["Esc", "All Images (clears selection and filters); closes menus"],
    ["F1", "This help"],
    ["Drag & drop", "Drop files or whole folders anywhere into the window"],
    ["Double-click title bar", "Maximize / restore the window"],
  ];
  showDialog("Keyboard Shortcuts", [
    h("h3", null, "Keyboard shortcuts"),
    h("table", null, h("tbody", null, rows.map(([keys, action]) => h("tr", null, h("td", null, h("kbd", null, keys)), h("td", null, action))))),
  ]);
}

function showPrivacyDialog() {
  showDialog("Privacy & Local Processing", [
    h("h3", null, "Everything stays on this machine"),
    h("p", null, "Uploaded images, thumbnails, extracted metadata, filenames and GPS coordinates are processed by the local Flask server and ExifTool. Nothing is sent to external APIs or AI services."),
    h("p", null, "The only outside requests are OpenStreetMap map tiles. The tile server can infer which map area you look at – switch them off with View → Online Map Tiles for sensitive cases."),
    h("p", null, "Originals are never modified. Sanitizing creates NEW copies, re-reads them with ExifTool and only reports \"verified clean\" when no sensitive tag remains."),
    h("p", { class: "muted" }, "The temporary workspace is deleted when the server stops or when you choose File → Clear Workspace."),
  ]);
}

function showAboutDialog() {
  const status = state.serverStatus || {};
  showDialog("About Photo Tool", [
    h("h3", null, "Photo Metadata / OSINT Workbench"),
    h("p", null, "Batch photo metadata analysis for OSINT, with one-click metadata sanitization for OPSEC."),
    h("table", null, h("tbody", null,
      h("tr", null, h("td", null, "ExifTool"), h("td", null, status.exiftool_version || "not found")),
      h("tr", null, h("td", null, "HEIC / HEIF"), h("td", null, status.heif_available ? "enabled (pillow-heif)" : "disabled – pip install pillow-heif")),
      h("tr", null, h("td", null, "Formats"), h("td", null, (status.allowed_extensions || ALLOWED_EXTENSIONS).join(", "))),
      h("tr", null, h("td", null, "Limits"), h("td", null, `${Math.round((status.max_file_size || 0) / 1048576)} MB per file, ${status.max_images || "?"} images`)),
      h("tr", null, h("td", null, "Map"), h("td", null, "Leaflet · © OpenStreetMap contributors")))),
  ]);
}

// ==========================================================================
// PROPERTIES DIALOG (detail panel) – tabs: General | Metadata | Raw EXIF | Privacy | Sanitize
// ==========================================================================
const IMAGE_TABS = [["general", "General"], ["metadata", "Metadata"], ["raw", "Raw EXIF"], ["privacy", "Privacy"], ["sanitize", "Sanitize"]];
const OVERVIEW_TABS = [["general", "General"], ["privacy", "Privacy"], ["sanitize", "Sanitize"]];

/**
 * Small static icons. The markup below is a CONSTANT written in this file
 * (never data from images), so using innerHTML here is safe.
 */
const ICONS = {
  shieldOk: '<svg viewBox="0 0 32 32" class="shield"><path d="M16 2l12 4v9c0 7-5 12-12 15C9 27 4 22 4 15V6z" fill="#3aa63a" stroke="#1f6b1f"/><path d="M16 2v28C9 27 4 22 4 15V6z" fill="#6cc96c"/><path d="M10 16l4 4 8-9" stroke="#fff" stroke-width="2.6" fill="none"/></svg>',
  shieldWarn: '<svg viewBox="0 0 32 32" class="shield"><path d="M16 2l12 4v9c0 7-5 12-12 15C9 27 4 22 4 15V6z" fill="#e6b021" stroke="#946d0a"/><path d="M16 2v28C9 27 4 22 4 15V6z" fill="#f5cf55"/><path d="M16 9v9" stroke="#3a2a00" stroke-width="3"/><circle cx="16" cy="22.5" r="1.8" fill="#3a2a00"/></svg>',
  shieldBad: '<svg viewBox="0 0 32 32" class="shield"><path d="M16 2l12 4v9c0 7-5 12-12 15C9 27 4 22 4 15V6z" fill="#c42b1c" stroke="#7a160c"/><path d="M16 2v28C9 27 4 22 4 15V6z" fill="#e0614f"/><path d="M11 11l10 10M21 11L11 21" stroke="#fff" stroke-width="2.6"/></svg>',
  shieldIdle: '<svg viewBox="0 0 32 32" class="shield"><path d="M16 2l12 4v9c0 7-5 12-12 15C9 27 4 22 4 15V6z" fill="#3b8de0" stroke="#1d5397"/><path d="M16 2v28C9 27 4 22 4 15V6z" fill="#69adf0"/><path d="M16 9v9" stroke="#fff" stroke-width="3"/><circle cx="16" cy="22.5" r="1.8" fill="#fff"/></svg>',
};

function icon(name) {
  const span = document.createElement("span");
  span.style.display = "inline-flex";
  span.innerHTML = ICONS[name];   // constant markup, see comment above
  return span;
}

function securityBanner(kind, title, text) {
  const iconName = { ok: "shieldOk", warn: "shieldWarn", bad: "shieldBad", idle: "shieldIdle" }[kind];
  return h("div", { class: `security-banner sb-${kind}` }, icon(iconName), h("div", null, h("strong", null, title), text ? h("span", null, text) : null));
}

function groupTitle(text) {
  return h("div", { class: "group-title" }, text);
}

function renderProperties() {
  const panel = $("inspector");
  const id = state.selectedId;
  if (!id) {
    panel.replaceChildren(...buildPropertiesFrame("All Images – Batch Properties", OVERVIEW_TABS,
      (tab) => buildOverviewTab(tab), buildOverviewButtons(), false));
    return;
  }
  const summary = state.byId.get(id);
  const detail = state.details.get(id);
  if (detail) {
    panel.replaceChildren(...buildPropertiesFrame(`${summary.filename} Properties`, IMAGE_TABS,
      (tab) => buildImageTab(tab, detail), buildImageButtons(summary), true));
    return;
  }
  // Not cached yet: show the frame immediately, then load raw metadata.
  panel.replaceChildren(...buildPropertiesFrame(`${summary.filename} Properties`, IMAGE_TABS,
    () => [h("div", { class: "muted" }, h("span", { class: "spinner" }), " Loading metadata…")], buildImageButtons(summary), true));
  api(`/api/images/${id}`)
    .then((data) => {
      state.details.set(id, data);
      if (state.selectedId === id) renderProperties();   // ignore if the user moved on
    })
    .catch((error) => {
      if (state.selectedId === id) {
        panel.querySelector(".tab-page").replaceChildren(h("div", { class: "errors" }, `Could not load metadata: ${error.message}`));
      }
    });
}

function buildPropertiesFrame(title, tabs, buildTab, buttons, withNav) {
  const active = tabs.some(([key]) => key === state.propertiesTab) ? state.propertiesTab : "general";
  const page = h("div", { class: "tab-page", role: "tabpanel" });
  page.replaceChildren(...buildTab(active));
  return [
    h("div", { class: "prop-caption" },
      h("span", { class: "prop-caption-text", title }, title),
      withNav ? h("button", { class: "btn btn-small", type: "button", title: "Previous image (←)", onclick: () => selectRelative(-1) }, "◀") : null,
      withNav ? h("button", { class: "btn btn-small", type: "button", title: "Next image (→)", onclick: () => selectRelative(1) }, "▶") : null),
    h("div", { class: "tabs", role: "tablist" }, tabs.map(([key, label]) => h("button", {
      type: "button",
      class: `tab ${key === active ? "active" : ""}`,
      role: "tab",
      "aria-selected": key === active ? "true" : "false",
      onclick: () => { state.propertiesTab = key; renderProperties(); },
    }, label))),
    page,
    h("div", { class: "prop-buttons" }, buttons),
  ];
}

function buildImageButtons(img) {
  return [
    h("button", { class: "btn", type: "button", disabled: state.busy, onclick: () => sanitize({ ids: [img.id] }) },
      img.sanitization ? "Sanitize Again" : "Sanitize Image"),
    h("button", { class: "btn", type: "button", disabled: !isSanitizedOk(img), onclick: () => downloadSanitized(img.id) }, "Download Copy"),
    h("span", { class: "spacer" }),
    h("button", { class: "btn btn-default", type: "button", title: "Back to All Images (Esc)", onclick: () => selectAllImages() }, "Close"),
  ];
}

function buildOverviewButtons() {
  return [
    h("button", { class: "btn", type: "button", disabled: state.busy || !hasImages(), onclick: () => sanitize({ all: true }) }, "Sanitize All"),
    h("button", { class: "btn", type: "button", disabled: !state.images.some(isSanitizedOk), onclick: () => downloadZip("all") }, "Export All as ZIP"),
  ];
}

// ---------------------------------------------------------------- overview tabs
function buildOverviewTab(tab) {
  const all = state.images;
  const total = all.length;
  const n = (fn) => all.filter(fn).length;

  if (tab === "privacy") {
    const rows = Object.entries(CATEGORY_LABELS).map(([category, label]) => {
      const count = n((img) => img.sensitive_categories.includes(category));
      return h("tr", null, h("th", null, label), h("td", { class: count ? "" : "absent" }, count ? `${count} of ${total} images` : "-"));
    });
    const exposed = n((img) => img.sensitive_categories.length > 0);
    return [
      exposed
        ? securityBanner("warn", `${exposed} of ${total} images expose sensitive metadata`, "Sanitize them before sharing.")
        : securityBanner("ok", "No sensitive metadata found in this batch", null),
      groupTitle("Exposure by category"),
      h("table", { class: "kv" }, h("tbody", null, rows)),
    ];
  }

  if (tab === "sanitize") {
    const results = state.lastBatchResults;
    if (!results || !results.length) {
      return [
        securityBanner("idle", "No sanitization has been run yet", "Use Sanitize → Selected / Ticked / All. Originals are never modified."),
        groupTitle("How it works"),
        h("ol", { class: "overview-list" },
          h("li", null, "A NEW copy of each image is written without metadata."),
          h("li", null, "ExifTool re-reads every copy (nothing is assumed)."),
          h("li", null, "Original metadata is compared with sanitized metadata, field by field."),
          h("li", null, "Download single copies or a ZIP via Export.")),
      ];
    }
    const clean = results.filter((r) => r.sanitization.verified_clean).length;
    const failed = results.filter((r) => ["error", "unverified"].includes(r.sanitization.status)).length;
    const warn = results.length - clean - failed;
    const kind = failed ? "bad" : warn ? "warn" : "ok";
    return [
      securityBanner(kind, `Last run: ${clean} of ${results.length} verified clean`,
        `${warn} with remaining sensitive tags · ${failed} failed. Click an image for its Original → Sanitized comparison.`),
      groupTitle("Results"),
      h("div", { class: "results-list" }, results.map((r) => {
        const img = state.byId.get(r.id);
        return h("button", { type: "button", class: "result-item", onclick: () => selectImage(r.id, "inspector") },
          h("img", { src: `/images/${r.id}/thumb`, alt: "" }),
          h("span", { class: "result-name" }, r.filename),
          img ? sanitizationBadge(img) : null);
      })),
    ];
  }

  // general
  const lines = [
    [n(FLAG_FILTERS.gps), "expose precise GPS location (hemisphere known, on the map)"],
    [n(FLAG_FILTERS.gps_ambiguous), "contain GPS coordinates with an unknown hemisphere (shown in Details, not on the map)"],
    [n(FLAG_FILTERS.timestamp), "expose when they were taken / edited"],
    [n(FLAG_FILTERS.device), "identify the camera / phone model"],
    [n((img) => img.flags.identifier), "contain serial numbers or unique IDs"],
    [n(FLAG_FILTERS.software), "reveal software / editing history"],
    [n(FLAG_FILTERS.author), "name an author / owner / copyright holder"],
    [n((img) => img.flags.embedded), "carry an embedded thumbnail (may show the uncropped original)"],
  ].filter(([count]) => count > 0);
  const visible = visibleImages().length;
  return [
    h("table", { class: "kv" }, h("tbody", null,
      h("tr", null, h("th", null, "Images"), h("td", null, `${total}${visible !== total ? ` (${visible} shown by filters)` : ""}`)),
      h("tr", null, h("th", null, "Total size"), h("td", null, formatBytes(all.reduce((s, i) => s + i.file_size, 0)))),
      h("tr", null, h("th", null, "Rejected files"), h("td", null, String(state.uploadErrors.length))),
      h("tr", null, h("th", null, "Ticked"), h("td", null, String(tickedImages().length))))),
    groupTitle("What this batch exposes"),
    lines.length
      ? h("ul", { class: "overview-list" }, lines.map(([count, text]) => h("li", null, h("strong", null, `${count} of ${total}`), ` images ${text}`)))
      : h("div", { class: "finding-none" }, h("span", { class: "ib-icon ok" }, "✓"), "No sensitive metadata was found in this batch."),
    groupTitle("Tip"),
    h("p", { class: "muted" }, "Click a thumbnail, a row in Details or a marker in GPS Locations to open that image's properties. Esc returns to All Images."),
  ];
}

// ---------------------------------------------------------------- image tabs
function buildImageTab(tab, detail) {
  const img = state.byId.get(detail.id) || detail;
  if (tab === "metadata") return buildMetadataTab(img, detail);
  if (tab === "raw") return buildRawMetadata(detail);
  if (tab === "privacy") return buildPrivacyTab(img, detail);
  if (tab === "sanitize") return buildSanitizeTab(img, detail);
  return buildGeneralTab(img, detail);
}

function buildGeneralTab(img, detail) {
  const n = detail.normalized || {};
  const nodes = [
    h("div", { class: "general-head" },
      h("img", { src: `/images/${img.id}/thumb`, alt: "" }),
      h("div", null,
        h("div", { class: "general-name" }, img.filename),
        h("div", { class: "muted" }, `${img.format === "HEIF" ? "HEIC" : img.format} image · ${formatBytes(img.file_size)}`))),
    h("div", { class: "preview-wrap" }, h("img", { src: `/images/${img.id}/preview`, alt: `Preview of ${img.filename}` })),
  ];
  if (detail.metadata_error) nodes.push(h("div", { class: "errors" }, `Metadata extraction problem: ${detail.metadata_error}`));
  if (detail.warnings && detail.warnings.length) {
    nodes.push(h("div", { class: "warnings" }, h("ul", null, detail.warnings.map((w) => h("li", null, w)))));
  }
  nodes.push(
    h("table", { class: "kv" }, h("tbody", null,
      kvRow("Type of file", `${n.file_type || img.format} (${n.mime_type || img.mime_type})`),
      kvRow("Size", `${formatBytes(img.file_size)} (${img.file_size.toLocaleString()} bytes)`),
      kvRow("Dimensions", `${img.width} × ${img.height}` + (img.display_width !== img.width ? ` (displayed ${img.display_width} × ${img.display_height})` : "")),
      kvRow("Date taken", n.timestamp ? `${n.timestamp}${n.timestamp_source ? ` (${n.timestamp_source})` : ""}` : null),
      kvRow("Camera / device", n.device),
      kvRow("GPS", gpsSummary(img)),
      kvRow("Uploaded", img.uploaded_at),
      kvRow("Metadata tags", String(detail.tag_count)))),
    groupTitle("Indicators"),
    h("div", { class: "cell-badges" }, exposureBadges(img), sanitizationBadge(img)),
    h("div", { class: "inline-actions" },
      h("label", null, h("input", { type: "checkbox", id: "prop-check", checked: state.checked.has(img.id), onchange: (e) => toggleChecked(img.id, e.target.checked) }), "Ticked for batch sanitize / ZIP")),
  );
  return nodes;
}

function kvRow(label, value) {
  return h("tr", null, h("th", null, label), h("td", { class: value ? "" : "absent" }, dash(value)));
}

function buildMetadataTab(img, detail) {
  const n = detail.normalized || {};
  const gpsText = formatGps(img);
  // Every normalized field – grouped like a Windows "Details" property page.
  const groups = [
    ["File", [
      ["File type", n.file_type || img.format],
      ["MIME type", n.mime_type || img.mime_type],
      ["File size", formatBytes(img.file_size)],
      ["Dimensions", `${img.width} × ${img.height}` + (img.display_width !== img.width ? ` (displayed ${img.display_width} × ${img.display_height})` : "")],
      ["Orientation", n.orientation],
      ["SHA-256", img.sha256],
      ["Metadata tags", String(detail.tag_count)],
    ]],
    ["Date / time", [
      ["Date/time original", n.datetime_original],
      ["Create date", n.create_date],
      ["Modify date", n.modify_date],
      ["Timezone offset", n.timezone_offset],
    ]],
    ["Camera", [
      ["Make", n.make],
      ["Model", n.model],
      ["Lens", n.lens],
      ["Serial number", n.serial_number],
      ["Software / editor", n.software],
      ["Host computer", n.host_computer],
    ]],
    ["GPS", [
      ["GPS status", n.gps_status ? GPS_STATUS_LABELS[n.gps_status] || n.gps_status : null],
      ["GPS latitude / longitude", gpsText || (n.gps_status ? "not resolved – see raw values below" : null)],
      ["Raw latitude", n.gps_raw_latitude !== null && n.gps_raw_latitude !== undefined
        ? `${n.gps_raw_latitude}  (reference: ${n.gps_latitude_ref || "MISSING / unusable"})` : null],
      ["Raw longitude", n.gps_raw_longitude !== null && n.gps_raw_longitude !== undefined
        ? `${n.gps_raw_longitude}  (reference: ${n.gps_longitude_ref || "MISSING / unusable"})` : null],
      ["GPS source", n.gps_source],
      ["GPS note", [n.gps_status !== "valid" ? n.gps_message : null, ...(n.gps_notes || [])].filter(Boolean).join(" ") || null],
      ["GPS altitude", n.gps_altitude !== null && n.gps_altitude !== undefined ? `${n.gps_altitude} m` : null],
      ["GPS date/time", n.gps_datetime],
    ]],
    ["Origin", [
      ["Author / artist", n.artist],
      ["Copyright", n.copyright],
      ["Description / comment", n.description],
    ]],
  ];
  const nodes = [];
  for (const [title, rows] of groups) {
    nodes.push(groupTitle(title), h("table", { class: "kv" }, h("tbody", null, rows.map(([label, value]) => kvRow(label, value)))));
  }
  return nodes;
}

function buildPrivacyTab(img, detail) {
  const n = detail.normalized || {};
  const categories = Object.keys(CATEGORY_LABELS).filter((c) => detail.sensitive && detail.sensitive[c]);
  const nodes = [];
  if (detail.metadata_error) {
    nodes.push(securityBanner("bad", "Exposure unknown", "Metadata could not be read."));
  } else if (categories.length === 0) {
    nodes.push(securityBanner("ok", "No sensitive metadata found", "ExifTool found nothing privacy-relevant in this file."));
  } else {
    nodes.push(securityBanner("warn", `${categories.length} sensitive categor${categories.length === 1 ? "y" : "ies"} exposed`,
      "Expand a category to see the exact tags. Use Sanitize to remove them."));
  }
  if (categories.length) {
    nodes.push(groupTitle("Privacy exposure"), h("div", { class: "findings" }, categories.map((c) => {
      const tags = detail.sensitive[c];
      return h("details", { class: "finding" },
        h("summary", null, h("strong", null, CATEGORY_LABELS[c]), ` – ${tags.length} tag(s)`),
        h("ul", null, tags.slice(0, 60).map((t) => h("li", null, h("span", { class: "raw-tag" }, t.key), " = ", t.value)),
          tags.length > 60 ? h("li", null, `… ${tags.length - 60} more (see Raw EXIF)`) : null));
    })));
  }
  if (img.flags.gps_ambiguous) {
    nodes.push(groupTitle("Location"),
      h("div", { class: "warnings" },
        h("strong", null, "GPS coordinates present, but the hemisphere is unknown."), " ",
        n.gps_message, " Not placed on the map. The hemisphere is deliberately NOT inferred from timezone, locale, filename or other context."),
      h("table", { class: "kv" }, h("tbody", null,
        kvRow("Raw latitude", n.gps_raw_latitude !== null ? `${n.gps_raw_latitude}  (${n.gps_latitude_ref || "reference missing / unusable"})` : null),
        kvRow("Raw longitude", n.gps_raw_longitude !== null ? `${n.gps_raw_longitude}  (${n.gps_longitude_ref || "reference missing / unusable"})` : null),
        kvRow("Source", n.gps_source))));
  }
  if (img.flags.gps) {
    nodes.push(groupTitle("Location"),
      h("table", { class: "kv" }, h("tbody", null,
        kvRow("Coordinates", formatGps(img)),
        kvRow("Altitude", n.gps_altitude !== null && n.gps_altitude !== undefined ? `${n.gps_altitude} m` : null))),
      h("div", { class: "inline-actions" }, h("a", {
        href: `https://www.openstreetmap.org/?mlat=${encodeURIComponent(n.gps_latitude)}&mlon=${encodeURIComponent(n.gps_longitude)}#map=16/${encodeURIComponent(n.gps_latitude)}/${encodeURIComponent(n.gps_longitude)}`,
        target: "_blank", rel: "noopener noreferrer",
        title: "Opens openstreetmap.org in a new tab (sends these coordinates to OSM)",
      }, "Open in OpenStreetMap ↗"), h("span", { class: "muted small" }, "(external site)")));
  }
  return nodes;
}

function buildSanitizeTab(img, detail) {
  if (!detail.sanitization) {
    const mode = $("sanitize-mode").value === "reencode" ? "Re-encode from pixels" : "Lossless ExifTool strip";
    return [
      securityBanner("idle", "This image has not been sanitized yet",
        "Sanitizing writes a NEW copy without metadata and verifies it with ExifTool. The original is never modified."),
      h("table", { class: "kv" }, h("tbody", null,
        kvRow("Mode", mode),
        kvRow("HEIC output", img.format === "HEIF" ? ($("heic-output").value === "jpeg" ? "Convert to JPEG" : "Keep HEIC") : "n/a"),
        kvRow("ICC profile", $("keep-icc").checked ? "Keep" : "Remove"))),
      h("div", { class: "inline-actions" },
        h("button", { class: "btn btn-default", type: "button", disabled: state.busy, onclick: () => sanitize({ ids: [img.id] }) }, "Sanitize Image")),
    ];
  }
  return buildVerification(detail);
}

function statusLabel(status) {
  return {
    removed: "Removed",
    still_present: "Still present",
    kept_intentionally: "Kept (not sensitive)",
    not_present: "Not present",
  }[status] || status;
}

const STATUS_GLYPH = { removed: "✓", still_present: "!", kept_intentionally: "i", not_present: "" };

function statusCell(status, value) {
  const showValue = status !== "removed" && value !== null && value !== undefined;
  return h("span", { class: `status-cell st-${status}` },
    h("span", { class: "st-icon" }, STATUS_GLYPH[status] || ""),
    showValue ? `${value} – ${statusLabel(status)}` : statusLabel(status));
}

function buildVerification(detail) {
  const s = detail.sanitization;
  if (s.status === "error" || s.status === "unverified") {
    return [
      securityBanner("bad", s.status === "error" ? "Sanitization failed" : "Could not verify the sanitized file", dash(s.error)),
      h("div", { class: "inline-actions" },
        h("button", { class: "btn", type: "button", disabled: state.busy, onclick: () => sanitize({ ids: [detail.id] }) }, "Try Again")),
    ];
  }

  const nodes = [
    s.verified_clean
      ? securityBanner("ok", "Verified clean", "ExifTool re-read the sanitized copy and found NO sensitive metadata.")
      : securityBanner("warn", `${s.remaining_sensitive_count} sensitive tag(s) still present`, "See the comparison below. Try the other sanitize mode."),
    h("table", { class: "kv" }, h("tbody", null,
      kvRow("Mode", s.mode === "reencode" ? "Re-encode from pixels" : "Lossless ExifTool strip"),
      kvRow("Format", `${detail.format === "HEIF" ? "HEIC" : detail.format} → ${s.output_format === "HEIF" ? "HEIC" : s.output_format}`),
      kvRow("Metadata tags", `${s.before_tag_count} → ${s.after_tag_count} (${s.removed_tags.length} removed)`),
      kvRow("File size", `${formatBytes(detail.file_size)} → ${formatBytes(s.file_size)}`),
      kvRow("Image check", s.image_check ? s.image_check.message : null),
      kvRow("Download name", s.download_name),
      kvRow("Sanitized at", s.sanitized_at))),
  ];
  if (s.notes && s.notes.length) nodes.push(h("ul", { class: "notes" }, s.notes.map((note) => h("li", null, note))));

  // Original Metadata -> Sanitized Metadata, field by field
  const rows = s.field_comparison.filter((r) => r.status !== "not_present");
  const notPresent = s.field_comparison.length - rows.length;
  nodes.push(groupTitle("Original Metadata → Sanitized Metadata"),
    h("table", { class: "compare" },
      h("thead", null, h("tr", null,
        h("th", { class: "c-field" }, "Field"),
        h("th", null, "Original Metadata"),
        h("th", { class: "c-arrow" }, "→"),
        h("th", null, "Sanitized Metadata"))),
      h("tbody", null, rows.length
        ? rows.map((r) => h("tr", null,
          h("td", { class: "c-field" }, r.field),
          h("td", { class: "c-before" }, dash(r.before)),
          h("td", { class: "c-arrow" }, "→"),
          h("td", null, statusCell(r.status, r.after))))
        : h("tr", null, h("td", { colspan: "4", class: "muted" }, "The original had none of the key fields.")))));
  if (notPresent) nodes.push(h("div", { class: "muted small" }, `${notPresent} other key field(s) were not present in the original.`));

  // Category comparison
  const catRows = s.category_comparison.filter((r) => r.status !== "not_present");
  if (catRows.length) {
    nodes.push(groupTitle("Sensitive categories"),
      h("table", { class: "compare" },
        h("thead", null, h("tr", null,
          h("th", { class: "c-field" }, "Category"),
          h("th", null, "Original"),
          h("th", { class: "c-arrow" }, "→"),
          h("th", null, "Sanitized"))),
        h("tbody", null, catRows.map((r) => h("tr", null,
          h("td", { class: "c-field" }, r.label),
          h("td", { class: "c-before" }, `${r.before_count} tag(s)`),
          h("td", { class: "c-arrow" }, "→"),
          h("td", null, statusCell(r.status, r.after_count ? `${r.after_count} tag(s)` : null)))))));
  }

  if (s.remaining_sensitive_count > 0) {
    const list = [];
    for (const [cat, tags] of Object.entries(s.remaining_sensitive)) {
      for (const t of tags) list.push(h("li", null, `[${CATEGORY_LABELS[cat] || cat}] `, h("span", { class: "raw-tag" }, t.key), " = ", t.value));
    }
    nodes.push(h("div", { class: "errors small", style: "margin-top:6px" }, h("strong", null, "Still present:"), h("ul", null, list)));
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
    h("div", { class: "inline-actions" },
      h("button", { class: "btn btn-default", type: "button", disabled: !isSanitizedOk(detail), onclick: () => downloadSanitized(detail.id) }, `Download ${s.download_name}`)),
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
  const n = detail.normalized || {};
  const gpsCaveat = ["ambiguous", "invalid", "incomplete"].includes(n.gps_status)
    ? h("div", { class: "warnings small" },
      `GPS status: ${GPS_STATUS_LABELS[n.gps_status]}. Careful: ExifTool's own display can look resolved when it is not – `,
      "Composite:GPSPosition is printed without hemispheres, and XMP coordinates without an N/S/E/W letter are printed with an assumed N/E. ",
      "Use Metadata → GPS for the interpreted status.")
    : null;
  return [
    gpsCaveat,
    h("div", { class: "muted small" }, `${keys.length} tags from ExifTool. Group = family-0:family-1. Red rows belong to a sensitive category. Filesystem tags of the temporary server copy are hidden.`),
    h("input", { type: "search", class: "raw-filter", placeholder: "Filter tags or values…", oninput: (e) => fill(e.target.value) }),
    h("table", { class: "kv" }, tbody),
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
    if (!ALLOWED_EXTENSIONS.includes(ext)) {
      const hint = (ext === "heic" || ext === "heif") ? " (HEIC support is off: pip install pillow-heif, then restart)" : "";
      rejected.push({ filename: file.name, error: `Skipped: unsupported extension '.${ext}'${hint}` });
    }
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
    $("upload-progress-text").textContent = `${done} / ${accepted.length}`;
  };
  setProgress();
  setActionStatus("Analyzing locally…", true);

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
  setActionStatus(`${added} added, ${accepted.length - added + rejected.length} rejected/skipped`, false);
  setTimeout(() => progress.classList.add("hidden"), 2500);
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
    heic_output: $("heic-output").value,
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
    state.propertiesTab = "sanitize";       // show Original → Sanitized comparison
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
  wireMenus();

  // Upload: buttons + hidden file inputs
  $("btn-pick-files").addEventListener("click", (e) => { e.stopPropagation(); $("input-files").click(); });
  $("btn-pick-folder").addEventListener("click", (e) => { e.stopPropagation(); $("input-folder").click(); });
  const dropZone = $("drop-zone");
  dropZone.addEventListener("click", () => $("input-files").click());
  dropZone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("input-files").click(); } });
  for (const id of ["input-files", "input-folder"]) {
    $(id).addEventListener("change", (e) => { uploadFiles(e.target.files); e.target.value = ""; });
  }

  // Drag & drop ANYWHERE in the window (files or whole folders).
  const hasFiles = (event) => [...((event.dataTransfer && event.dataTransfer.types) || [])].includes("Files");
  let dragDepth = 0;
  document.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    dragDepth += 1;
    $("client").classList.add("drag-active");
  });
  document.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) $("client").classList.remove("drag-active");
  });
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", async (e) => {
    e.preventDefault();
    dragDepth = 0;
    $("client").classList.remove("drag-active");
    if (!hasFiles(e)) return;
    const files = await filesFromDrop(e.dataTransfer);
    uploadFiles(files);
  });

  // Thumbnails pane
  $("btn-all-images").addEventListener("click", selectAllImages);
  $("btn-check-visible").addEventListener("click", tickVisible);
  $("btn-uncheck-all").addEventListener("click", untickAll);
  $("toggle-gallery-expand").addEventListener("change", (e) => $("gallery").classList.toggle("expanded", e.target.checked));

  // Filters (search box lives in the address bar)
  let searchTimer = null;
  $("filter-search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.filters.search = e.target.value.trim().toLowerCase(); renderAll(); }, 150);
  });
  for (const box of document.querySelectorAll("input[data-filter]")) {
    box.addEventListener("change", () => toggleFlagFilter(box.dataset.filter));
  }
  $("btn-clear-filters").addEventListener("click", () => { clearFilters(); renderAll(); });

  // Details list: column sorting + tick-all
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

  // Map pane
  $("btn-map-fit").addEventListener("click", fitMapToMarkers);
  $("toggle-map-path").addEventListener("change", () => renderMapMarkers());
  $("toggle-map-tiles").addEventListener("change", (e) => setTilesEnabled(e.target.checked));

  // Sanitize options: keep menus + Sanitize tab in sync
  for (const id of ["sanitize-mode", "heic-output", "keep-icc"]) {
    $(id).addEventListener("change", () => {
      refreshCommandStates();
      if (state.selectedId && state.propertiesTab === "sanitize") renderProperties();
    });
  }

  // Window chrome
  document.querySelector("#app-window > .titlebar").addEventListener("dblclick", (e) => {
    if (!e.target.closest(".caption-buttons")) toggleMaximize();
  });
  $("dialog-ok").addEventListener("click", closeDialog);
  $("dialog-close").addEventListener("click", closeDialog);
  $("dialog-backdrop").addEventListener("click", (e) => { if (e.target === $("dialog-backdrop")) closeDialog(); });

  // Keyboard
  document.addEventListener("keydown", (e) => {
    const key = e.key;
    if (key === "Escape") {
      if (isDialogOpen()) { closeDialog(); return; }
      if (document.querySelector(".menu.open")) { closeMenus(); return; }
    }
    if (isDialogOpen()) return;
    const ctrl = e.ctrlKey || e.metaKey;
    if (ctrl && key.toLowerCase() === "o") { e.preventDefault(); runCommand("add-images"); return; }
    if (ctrl && key.toLowerCase() === "s") { e.preventDefault(); runCommand("download-selected"); return; }
    if (key === "F1") { e.preventDefault(); runCommand("help-shortcuts"); return; }
    if (key === "Alt") { $("menubar").classList.toggle("show-access-keys"); return; }

    const tag = (e.target.tagName || "").toLowerCase();
    if (["input", "select", "textarea"].includes(tag) || ctrl || e.altKey) return;
    if (state.images.length === 0) return;
    if (key === "ArrowRight" || key === "ArrowDown") { e.preventDefault(); selectRelative(1); }
    else if (key === "ArrowLeft" || key === "ArrowUp") { e.preventDefault(); selectRelative(-1); }
    else if (key === "Escape") { selectAllImages(); }
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
    state.serverStatus = status;
    window.PHOTO_TOOL_MAX_FILE_SIZE = status.max_file_size;
    ALLOWED_EXTENSIONS = status.allowed_extensions;
    if (!status.heif_available) {
      const heicSelect = $("heic-output");
      heicSelect.disabled = true;
      heicSelect.title = "HEIC support is off: pip install pillow-heif";
    }
    // Restore the workspace if the page was reloaded while the server kept running.
    const data = await api("/api/images");
    storeImages(data.images);
  } catch (error) {
    toast(`Could not reach the local server: ${error.message}`, "error");
  }
  setActionStatus("Ready", false);
  renderAll();
}

document.addEventListener("DOMContentLoaded", init);
