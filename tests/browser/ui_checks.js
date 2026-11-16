/*
 * Browser regression checks (optional – needs Node.js + Playwright with Chromium).
 *
 *   python tools/make_test_images.py                 # creates sample_images/
 *   python app.py                                    # in another terminal, EMPTY workspace
 *   npm install playwright@1 && npx playwright install chromium   # once, if not installed
 *   node tests/browser/ui_checks.js [sample_images] [http://127.0.0.1:5000]
 *
 * Covers: upload, synchronized selection, resolved vs ambiguous GPS, derived
 * candidate markers kept separate from resolved markers/counts, evidence-based
 * ordering wording, timezone evidence with AND without the optional tzfpy
 * backend (detected via /api/status), and the Leaflet filename-injection
 * regression (a file named <img src=x id=pwn>.jpg must render as text).
 * Map tiles are blocked so the test never touches the network.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const SAMPLES = path.resolve(process.argv[2] || "sample_images");
const BASE = process.argv[3] || "http://127.0.0.1:5000";
let failures = 0;
const check = (cond, message) => {
  console.log(`${cond ? "PASS" : "FAIL"} ${message}`);
  if (!cond) failures += 1;
};

function listFiles(dir) {
  return fs.readdirSync(dir).flatMap((name) => {
    const full = path.join(dir, name);
    return fs.statSync(full).isDirectory() ? listFiles(full) : [full];
  });
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("https://tile.openstreetmap.org/**", (route) => route.abort());
  await page.goto(BASE);

  const status = await page.evaluate(async () => (await fetch("/api/status")).json());
  const tzAvailable = !!(status.timezone_lookup && status.timezone_lookup.available);
  console.log(`timezone backend: ${status.timezone_lookup ? status.timezone_lookup.description : "?"}`);
  check(status.image_count === 0, "server starts with an empty workspace");

  // ---- upload sample set + the XSS probe (resolved GPS, HTML in the filename)
  const files = listFiles(SAMPLES);
  await page.setInputFiles("#input-files", files);
  await page.waitForFunction((n) => document.querySelectorAll("#gallery .thumb-card").length >= n, files.length - 2, { timeout: 180000 });
  await page.setInputFiles("#input-files", [{
    name: "<img src=x id=pwn>.jpg", mimeType: "image/jpeg",
    buffer: fs.readFileSync(path.join(SAMPLES, "05_gps_paris.jpg")),
  }]);
  await page.waitForFunction((n) => document.querySelectorAll("#gallery .thumb-card").length >= n, files.length - 1, { timeout: 60000 });
  await page.waitForTimeout(800);

  const summaries = (await page.evaluate(async () => (await fetch("/api/images")).json())).images;
  const resolvedCount = summaries.filter((i) => i.flags.gps).length;
  const ambiguousCount = summaries.filter((i) => i.flags.gps_ambiguous).length;
  const resolvedMarkers = () => page.locator("path.leaflet-interactive").count();
  const statusBar = await page.locator("#sb-counts").innerText();

  // ---- default view: conservative
  check(await resolvedMarkers() === resolvedCount, `map shows only resolved GPS (${resolvedCount})`);
  check(await page.locator(".leaflet-marker-icon.candidate-icon").count() === 0, "no candidate markers by default");
  check(statusBar.includes(`${resolvedCount} GPS`) && statusBar.includes(`${ambiguousCount} ambiguous GPS`), `status bar: ${statusBar}`);

  // ---- filename injection regression
  await page.locator("#gallery .thumb-card", { hasText: "<img src=x id=pwn>.jpg" }).click();
  await page.waitForTimeout(500);
  await page.locator("path.leaflet-interactive").evaluateAll((els) => els.forEach((e) => e.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }))));
  await page.waitForTimeout(300);
  check(await page.locator("#pwn").count() === 0, "filename with HTML is never rendered as HTML (tooltip/popup/table/gallery)");
  check((await page.locator(".leaflet-tooltip").allInnerTexts()).some((t) => t.includes("<img src=x id=pwn>.jpg")), "filename shown as literal text in map tooltip");
  await page.keyboard.press("Escape");

  // ---- synchronized selection (thumbnail -> row/map)
  await page.locator("#gallery .thumb-card", { hasText: "04_gps_berlin.jpg" }).click();
  await page.waitForSelector(".properties .tab-page .kv");
  check((await page.locator("#meta-table-body tr.selected").innerText()).includes("04_gps_berlin.jpg"), "thumbnail -> table row");
  // (a previous popup may still be fading out, so look for the right one instead of "the" popup)
  check(await page.locator(".leaflet-popup-content .popup-name", { hasText: "04_gps_berlin.jpg" }).count() === 1, "thumbnail -> map popup");
  await page.keyboard.press("Escape");

  // ---- candidates on
  await page.check("#toggle-candidates");
  await page.waitForFunction(() => document.querySelectorAll(".leaflet-marker-icon.candidate-icon").length > 0, null, { timeout: 30000 });
  await page.waitForTimeout(500);
  const analysis = await page.evaluate(async () => (await fetch("/api/location-analysis")).json());
  check(await page.locator(".leaflet-marker-icon.candidate-icon").count() === analysis.counts.candidates, `candidate markers = ${analysis.counts.candidates}`);
  check(await resolvedMarkers() === resolvedCount, "resolved markers unchanged with candidates shown");
  check(await page.locator("#sb-counts").innerText() === statusBar, "status bar unchanged with candidates shown");
  // warning + legend banner: hidden by default, shown while hovering a marker, never covering it
  check(!(await page.locator("#candidate-banner").isVisible()), "warning banner hidden until a marker is hovered");
  await page.evaluate(() => document.getElementById("map").scrollIntoView({ block: "center" }));
  await page.waitForTimeout(300);
  const visibleMarker = await page.locator(".leaflet-marker-icon.candidate-icon").evaluateAll((els) => els.findIndex((e) => {
    const r = e.getBoundingClientRect();
    return r.top > 0 && r.left > 0 && r.bottom < window.innerHeight && r.right < window.innerWidth;
  }));
  const hovered = page.locator(".leaflet-marker-icon.candidate-icon").nth(visibleMarker);
  const hb = await hovered.boundingBox();
  await page.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2);
  await page.waitForTimeout(300);
  const banner = page.locator("#candidate-banner");
  check(await banner.isVisible() && (await banner.innerText()).includes("not confirmed locations"), "hovering a marker shows the warning banner");
  const bb = await banner.boundingBox();
  check(bb.y > hb.y + hb.height || bb.y + bb.height < hb.y, "banner does not cover the hovered marker");
  const rowTops = await page.locator(".lg-item").evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().top)));
  check(new Set(rowTops).size === rowTops.length && rowTops.length === 4, "legend: one entry per line");
  await page.mouse.move(5, 5);
  await page.waitForTimeout(400);
  check(!(await banner.isVisible()), "banner hides again when the mouse leaves");
  await page.check('input[data-filter="gps"]');
  await page.waitForTimeout(400);
  check(await page.locator(".leaflet-marker-icon.candidate-icon").count() === 0, "resolved-GPS filter never shows candidates");
  await page.uncheck('input[data-filter="gps"]');
  await page.waitForTimeout(400);

  // ---- Location tab of the CTF-style image
  await page.locator("#meta-table-body tr", { hasText: "IMG_4014.JPG" }).click();
  await page.waitForSelector(".properties .tab-page .kv");
  await page.locator(".tabs .tab", { hasText: "Location" }).click();
  await page.waitForSelector(".candidate-card", { timeout: 30000 });
  const tab = await page.locator(".tab-page").innerText();
  const cards = await page.locator(".candidate-card").allInnerTexts();
  check(cards.length === 4, "all 4 geometrically possible candidates are listed");
  check(cards[0].includes("Possible location — N/W") && cards[0].includes("Most contextually supported · inference"), "N/W first, labelled as inference");
  check(tab.includes("Order of the candidates") && tab.includes("No scores, weights or probabilities"), "ordering rule explained");
  check(tab.includes("not necessarily the same physical device"), "model match not presented as the same physical device");
  check(/Supporting \(\d+\)/.test(tab) && /Conflicting \(\d+\)/.test(tab) && /Weak \(not used for ordering\)/.test(tab) && /Unavailable \(not used for ordering\)/.test(tab), "evidence categories shown");
  check(!/\b(correct location|confirmed location)\b/i.test(tab.replace(/not a resolved or confirmed location/gi, "")), "no 'correct/confirmed location' wording");
  if (tzAvailable) {
    check(tab.includes("Timezone consistent") && tab.includes("America/New_York"), "with tzfpy: N/W timezone consistent (America/New_York)");
    check(tab.includes("nautical zone"), "with tzfpy: ocean candidates are not used as evidence");
    check(tab.includes("ODbL"), "with tzfpy: boundary data attribution shown");
  } else {
    check(tab.includes("Timezone unknown") && tab.includes("requirements-timezone.txt"), "without tzfpy: timezone unknown + install hint");
  }
  const detail = await page.evaluate(async () => (await (await fetch("/api/images")).json()).images.find((i) => i.filename === "IMG_4014.JPG"));
  check(detail.normalized.gps_status === "ambiguous" && detail.flags.gps === false && detail.normalized.gps_latitude === null, "IMG_4014 stays ambiguous after the analysis");

  // ---- resolved N/W image still maps correctly
  await page.keyboard.press("Escape");
  await page.locator("#meta-table-body tr", { hasText: "IMG_4013.JPG" }).click();
  await page.waitForTimeout(600);
  const popup = await page.locator(".leaflet-popup-content", { hasText: "IMG_4013.JPG" }).first().innerText();
  check(popup.includes("lat 38.560201") && popup.includes("lon -78.340102"), "resolved N/W image keeps its negative longitude");

  // ---- toggle off
  await page.keyboard.press("Escape");
  await page.uncheck("#toggle-candidates");
  await page.waitForTimeout(400);
  check(await page.locator(".leaflet-marker-icon.candidate-icon").count() === 0 && await resolvedMarkers() === resolvedCount, "toggle off restores the resolved-only map");

  check(errors.length === 0, `no JavaScript errors ${errors.length ? JSON.stringify(errors) : ""}`);
  await browser.close();
  console.log(failures ? `\n${failures} FAILURE(S)` : "\nALL BROWSER CHECKS PASSED");
  process.exit(failures ? 1 : 0);
})();
