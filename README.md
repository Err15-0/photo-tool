# Photo Tool

**A batch-first photo metadata investigation and sanitization workspace.**

<img width="2877" height="1484" alt="image" src="https://github.com/user-attachments/assets/4b2ee2f3-ed25-4e0c-8fb9-1f075c37d233" />
<img width="2879" height="1485" alt="Screenshot_20261114_203537" src="https://github.com/user-attachments/assets/cc23be23-ccf6-495f-a6cf-a2c711026063" />



## Why I built this
Photo Tool started with a problem I kept running into during OSINT: when I have a collection of original image files, I don't want to run ExifTool on every photo individually and manually correlate the results.
Instead, I wanted to drop an entire folder into one place and immediately answer questions like:
Which photos contain GPS coordinates?
Where were they taken?
Which images came from the same device?
When were they captured?
Which software touched them?
Are there patterns or inconsistencies across the collection?
Which images are worth investigating further?

## Investigation and OPSEC
Photo Tool turns a collection of original or near-original images into a visual investigation workspace. It combines a thumbnail overview, searchable metadata table, GPS mapping, and metadata correlation so you can move quickly from a large batch to individual images of interest.

The same metadata that can be valuable during an investigation can become a privacy risk when the images are yours.
A photo may contain precise GPS coordinates, capture times, device information, software history, author fields, serial identifiers, embedded thumbnails, and other information that you may not intend to share.

Photo Tool therefore works in both directions:
Investigation: What can I learn from this collection of images?
OPSEC: What could someone else learn from mine?

Before an original image leaves your control, Photo Tool can sanitize its metadata and then re-analyze the output with ExifTool to verify what was actually removed.


## Scope
Photo Tool is designed primarily for original or near-original image collections: device exports, archives, file shares, investigation datasets, email/file attachments, and personal photo libraries.
Many social media and messaging platforms re-encode uploaded images and strip much of their original metadata. Photo Tool cannot recover metadata that is no longer present, and it is not intended to do so.
Instead, its goal is to make metadata-rich image collections faster to investigate—and to help users remove that same information before sharing original files.


## Why batch analysis matters
INTERPOL's *Global Guidelines for Digital Forensics Laboratories* notes
that some exhibits may contain thousands of photos and videos, recommends
thumbnail/gallery-based review for efficient media triage, and highlights
GPS coordinates, timestamps, and capture-device information as useful
image metadata for forensic analysis. (See p. 45, §5.2.3(C), “Pictures and Videos.”)

## Features
You can drop a folder of photos into the browser and then:

* see every photo as a thumbnail with badges (GPS, timestamp, device, software, author, serials, embedded thumbnails)
* look through one table of the metadata (sortable, filterable, searchable)
* see every GPS-tagged photo on a Leaflet/OpenStreetMap map, and optionally connect each device's photos in time order
* click a thumbnail, table row or map marker. The thumbnail wall, table, map and Properties dialog all move to that same image.
* group photos by device, software, lens, capture day or author, and spot identical files and duplicate names
* sanitize one, several or all photos. Each sanitized copy is **re-read with ExifTool** and compared with the original.
* download clean copies one at a time or as a ZIP

Supported formats: **JPEG, PNG, WebP, TIFF and HEIC/HEIF (iPhone photos)**.

Everything runs **locally**. Images, thumbnails, metadata, filenames and GPS coordinates are never sent to an external API or AI service. The only outside requests are OpenStreetMap map tiles, and you can switch those off on the map.

---

## 1. Architecture

```
Browser (vanilla JS)                         Flask (Python)                   Local tools
────────────────────                         ──────────────                   ───────────
drop zone / file picker ─ chunks of 10 ─▶  POST /api/upload
                                             ├─ uploads.py: validate, store,   Pillow
                                             │   thumbnail + preview
                                             └─ metadata.py: ONE batched run ─▶ exiftool -json
gallery ◀─┐                                                                     (x2: readable + numeric)
table   ◀─┼── state (app.js) ◀── JSON ──── GET /api/images, /api/images/<id>
map     ◀─┤        ▲                         GET /images/<id>/thumb|preview
properties◀┘      │
                   └── selectImage(id)
"Sanitize" ──────────────────────────────▶ POST /api/sanitize
                                             └─ sanitize.py: write copy ──────▶ Pillow  or  exiftool -all=
                                                re-read copy (batched) ───────▶ exiftool -json
                                                compare before/after
"Download" ──────────────────────────────▶ GET /images/<id>/sanitized, POST /download/zip
```

### How images move through the app
1. **Browser:** checks extensions and sizes first so obvious junk isn't uploaded. It reads dropped folders recursively, then uploads in chunks of 10 files.
2. **`uploads.py`** checks each file and rejects it if any check fails:
   * the extension is on the allowlist
   * the size is within the limit
   * Pillow detects the **real format from the file's content**
   * `verify()` passes
   * the image fully decodes, which catches truncated files and decompression bombs

   A rejected file never breaks the rest of the batch.
3. The exact uploaded bytes are written to `originals/<random-id>.<ext>`. The file is created in exclusive mode with permissions 0600 and is **never modified afterwards**. The user's filename is kept only as display text.
4. Pillow writes a thumbnail (320 px) and a preview (1600 px). Both are built from pixels only, so they carry no metadata.
5. **`metadata.py`** runs ExifTool **once per batch** (`-json -G0:1 -a -s`), plus once more with `-n` for numeric GPS values. From that output it:
   * picks out the key fields
   * interprets the GPS with explicit confidence states (`photo_tool/gps.py`, see below). For files with XMP GPS coordinates it also reads the raw XMP packet in one extra batched run.
   * sorts every tag into privacy categories
6. The browser keeps the summaries in `state` and draws every panel from that state.

### How metadata is stored internally (`photo_tool/workspace.py → ImageRecord`)
| field | meaning |
|---|---|
| `id` | 32-character random hex string; the **only** identifier. Two files called `photo.jpg` get different IDs. |
| `original_filename` | display only (untrusted text) |
| `sha256`, `file_size`, `width`, `height`, `display_width/height`, `image_format` | file facts |
| `raw` | **complete** ExifTool output as `{"EXIF:IFD0:Make": "Canon", ...}`, with filesystem tags of the temporary copy hidden |
| `normalized` | key fields for the table: `device`, `timestamp`, `timestamp_sortable`, `software`, `gps_status`, `gps_latitude`/`gps_longitude` (signed, **only** when the hemisphere is known), `gps_raw_latitude`/`gps_raw_longitude`, `gps_latitude_ref`/`gps_longitude_ref`, `gps_missing_refs`, `gps_message`, `gps_display`, `gps_altitude`, `artist`, `copyright`, `lens`, `serial_number`, ... (`None` means not present; the UI shows "-") |
| `sensitive` | `{category: [{key, value}]}`. Categories: location, timestamp, device, identifier, software, author, people, text, embedded, maker_notes |
| `flags` | booleans for badges, filters and stats (`gps`, `timestamp`, `device`, `software`, `author`, ...) |
| `sanitization` | the full before/after verification result, once the image is sanitized |
| `paths` | **server-side only** file paths; never sent to the browser |

All records sit in a Python dict in memory. There is no database. Restarting the server starts with an empty workspace.

### How thumbnail, table, map and detail stay in sync (`static/js/app.js`)
There is **one** `state` object, and `state.selectedId` is the shared selection (`null` means "All Images").
Every click goes through the same function, whether it comes from a thumbnail, a table row, a map marker, the keyboard or the results list:

```
selectImage(id, source)
  → state.selectedId = id
  → updateSelectionUI(source)   highlight card + row + marker, scroll the other panels to it, zoom map, open popup
  → renderProperties()          Properties dialog (fetches /api/images/<id> once, then cached)
  → renderChrome()              breadcrumb in the address bar + counts in the status bar
```

Each map marker stores its image ID (`marker.imageId`), and `mapState.markers` maps ID to marker. Filters work the same way: change `state.filters`, then call `renderAll()`, so the gallery, table and map always show the same set of images.

### User interface 

| Area | What it is | Where in the code |
|---|---|---|
| Title bar | `_` rolls the window up, `□` maximizes (or double-click the title), `×` clears the workspace (after a confirmation) | `index.html .titlebar` |
| Menu bar | **File / Analyze / View / Tools / Help** | `index.html .menubar` |
| Toolbar | **Add Images, Add Folder, Analyze, Sanitize ▾, Export ▾** + the sanitize options (Mode, HEIC →, Keep ICC) | `index.html .toolbar` |
| Address bar | breadcrumb `Workspace ▸ All Images ▸ file.jpg` (click to go back) + search box | `app.js → renderAddressBar()` |
| Panes | Add Images, Batch Summary, Thumbnails (Explorer icon view), Details (list view), GPS Locations (map), Correlation (tree) | `index.html .client` |
| Properties dialog | tabs **General, Metadata, Raw EXIF, Privacy, Sanitize** for one image; **General, Privacy, Sanitize** for All Images | `app.js → renderProperties()` |
| Status bar | `20 images | 10 GPS | 16 timestamps | …`, the selected image, upload progress, ExifTool/HEIC status, `Ready` | `app.js → renderStatusBar()`, `setActionStatus()` |

Every menu item, toolbar button and caption button has a `data-command="..."` attribute. The `COMMANDS` table in `app.js` defines what each command does (`run`), when it is greyed out (`enabled`) and when a menu item shows a check mark (`checked`). Option commands such as "Mode: Lossless" only change the real form control (`#sanitize-mode`, `#heic-output`, `#keep-icc`, the map checkboxes). That control stays the single source of truth for the setting.

### How GPS is interpreted (`photo_tool/gps.py`)
**Principle: preserve uncertainty rather than invent certainty.** A confidently wrong map marker is worse than no marker.

EXIF stores latitude and longitude as **unsigned** numbers. The hemisphere is in separate tags: `GPSLatitudeRef` (N = +, S = −) and `GPSLongitudeRef` (E = +, W = −). Photo Tool never assumes a missing hemisphere:

| Status | Meaning | Map | Badge |
|---|---|---|---|
| `valid` | Hemisphere known from an explicit reference (or an explicit sign), values in range | **plotted** | `GPS` |
| `ambiguous` | Latitude/longitude values exist, but the hemisphere of at least one axis is missing or unrecognised | **not plotted**; the raw values and the missing reference are shown everywhere else | `GPS ±?` |
| `zero` | Resolves to 0,0 (almost always a placeholder) | not plotted | `GPS?` |
| `invalid` | Hemisphere known but values out of range | not plotted | `GPS?` |
| `incomplete` | GPS tags exist but no latitude/longitude pair | not plotted | `GPS?` |
| none | No GPS metadata | – | – |

How the hemisphere is decided:
1. **EXIF GPS:** `GPSLatitudeRef`/`GPSLongitudeRef` must be `N`/`S` and `E`/`W` (or North/South/East/West). ExifTool's signed `Composite:GPSLatitude`/`GPSLongitude` is preferred, **but only after** the reference has been checked: ExifTool turns a malformed reference such as `X` into a *positive* composite value.
2. **Missing or unrecognised reference:** the result is `ambiguous`, with the message *"GPS coordinates present, but hemisphere reference is missing (GPSLatitudeRef). Location cannot be determined reliably."* A coordinate of exactly 0° needs no hemisphere.
3. **XMP GPS:** `exif:GPSLatitude` must carry its own letter (`22,54.408S`). Photo Tool checks the raw XMP packet, because ExifTool silently treats a value without a letter as N/E.
4. **EXIF without reference, plus XMP with hemisphere:** the location is `valid` only if the XMP record has the **same** coordinates. Otherwise it stays `ambiguous`.
5. **Never used to guess a hemisphere:** timezone offsets, locale, country names, filenames or any other context. They can still help an investigator, but they are not GPS hemisphere information.

ExifTool traps found while auditing (ExifTool 12.76), all covered by `tests/test_gps.py`:
* `Composite:GPSPosition` is generated even when **both** references are missing and shows the unsigned values (an implicit N/E). Photo Tool does not use it.
* With `GPSLatitudeRef = X`, ExifTool still reports a positive `Composite:GPSLatitude` and prints "N".
* An XMP coordinate without a letter is shown by ExifTool with an invented "N"/"E".

Because of this, the **Raw EXIF** tab warns that ExifTool's own display can look resolved when it is not. The interpreted status is on the **Metadata** tab (GPS status, raw latitude/longitude with their references, source, note).

Where ambiguous GPS shows up in the UI:
* `GPS ±?` badge
* **Ambiguous GPS** tile in Batch Summary, filter in Details, and item in the **Analyze** menu
* `N ambiguous GPS` in the status bar
* raw values with `?` in the Details GPS column
* warnings in Properties
* a note under the map

Sanitize verification also reports ambiguous coordinates as **Removed**.

### Investigating ambiguous GPS: candidates and contextual evidence (`photo_tool/location_analysis.py`)
**Principle: Photo Tool may help you reason about uncertainty, but it never disguises inference as metadata fact.** Ambiguous GPS stays `ambiguous` and stays off the normal map. The investigation layer below is **off by default** and sits on top of that behaviour.

#### Why a missing hemisphere matters: the mirror effect
The CTF image that exposed the bug has `GPSLatitude = 38.551734`, `GPSLongitude = 78.333293`, and **no** `GPSLatitudeRef` / `GPSLongitudeRef`. Those two unsigned numbers fit four places on Earth:

| Candidate | Coordinates | What is there |
|---|---|---|
| N/W | 38.551734° N, 78.333293° W | Shenandoah Valley, Virginia, USA |
| N/E | 38.551734° N, 78.333293° E | Xinjiang, China – about **11,100 km** away |
| S/E | 38.551734° S, 78.333293° E | Southern Indian Ocean |
| S/W | 38.551734° S, 78.333293° W | South Pacific, off Chile |

Older Photo Tool versions (and many tools) silently read a missing reference as N/E, which would have put this Virginia photo in western China. Each missing reference doubles the possibilities: **one missing → 2 candidates, both missing → 2 × 2 = 4**. If an axis is exactly 0°, both hemispheres give the same point, so the duplicates are removed. A malformed reference (e.g. `X`) is treated as unknown: both hemispheres are listed and `X` is never interpreted.

#### The four evidence layers
Kept apart in the backend (`kind` fields in `/api/location-analysis`) and in the UI (Properties → **Location** tab):

| Layer | Meaning | UI label |
|---|---|---|
| Raw metadata | what is literally in the file (magnitudes, the reference tags or "missing") | grey **RAW METADATA** |
| Resolved metadata | conclusions supported by authoritative metadata. For ambiguous GPS: *none* | green **RESOLVED METADATA** |
| Derived candidates | mathematically possible coordinates | amber dashed **DERIVED – NOT METADATA** |
| Contextual evidence | timezone consistency, related photos in the collection | blue dotted **CONTEXTUAL EVIDENCE – INFERENCE** |

#### Showing possible locations
Tick **Show possible locations** in the GPS Locations pane (or View → Show Possible Locations). Then:
* Each candidate appears as a **dashed hollow "?" marker**, never as the solid red dot used for resolved GPS.
* A small **blue dot** (and blue ring) means the candidate has **supporting** contextual evidence.
* **Purple** markers are the candidates of the currently **selected** photo.
* The legend inside the banner uses the same CSS classes as the markers, so it always matches the map. When fitting the view, the map leaves room for the banner so no marker is hidden behind it.
* The tooltip and popup say e.g. *"Possible location — N/W (derived – not confirmed)"*.
* A banner stays on the map while candidates are shown: *"These locations are derived from ambiguous GPS metadata and are not confirmed locations."*

Candidate markers live in their own map layer (`mapState.candidateMarkers`). They never enter the resolved-GPS dataset, the GPS count, the GPS filter, the status bar or the chronological path.

#### Timezone consistency
For each candidate, Photo Tool can compare the UTC offset recorded in the image with the real offset at that place **on the capture date**:
* **Recorded offset:** `OffsetTimeOriginal` paired with `DateTimeOriginal`. Fallbacks are `OffsetTimeDigitized`/`CreateDate`, then `OffsetTime`/`ModifyDate` (labelled as modification time), then XMP dates that contain an offset. Malformed offsets such as `+25:00` are reported and ignored, never repaired.
* **Offset at the candidate:** the IANA rules from Python's `zoneinfo` and the operating system's tz database. This includes DST and historical changes (US rules before and after 2007, Moscow 2011/2014). If the capture time falls in the repeated hour when clocks go back, both offsets count.
* **Labels:** only **Timezone consistent**, **Timezone inconsistent** or **Timezone unknown**. Never "confirmed" or "correct".
* **Open ocean / poles:** only nautical zones exist there (`Etc/GMT+5`, `Etc/GMT`), so the result is *unknown* and is not used as evidence. Otherwise the Pacific mirror of the CTF example (`Etc/GMT+5` = UTC−05:00) would look "consistent".
* **Several local times at one place:** some regions officially or commonly use more than one local time. The boundary data then lists every zone that applies. The recorded offset is compared with each of them, and the result says so ("More than one local time is in use at this location …").
* **No hemisphere from the sign:** the sign of an offset is never used to guess a hemisphere.

**Why timezone does not prove location:** one offset covers a whole band of longitude, and both hemispheres share it. Cameras are also often set to the wrong timezone, or still on home time while travelling. *Timezone consistency is contextual evidence and does not confirm the location.*

#### Optional offline timezone lookup (tzfpy)
Finding which timezone a *coordinate* lies in needs timezone **boundary** data. Photo Tool keeps this **optional**:

| | Without `tzfpy` (default install) | With `tzfpy` (`pip install -r requirements-timezone.txt`) |
|---|---|---|
| Coordinate → zone name | not available | `tzfpy.get_tzs()`: all IANA zones at that point |
| UTC offset at capture time | – | always Python `zoneinfo` + OS tz database (DST, history) |
| Timezone evidence | always **Timezone unknown** with an install hint | consistent / inconsistent / unknown |
| Network | none | none: the boundary data is compiled into the package |
| Size | – | 2.9 MB wheel, ~5 MB installed, precompiled for Linux/macOS/Windows (Python ≥ 3.10) |

* **Selection:** Photo Tool uses tzfpy automatically when it is installed. Set `PHOTO_TOOL_TIMEZONE_LOOKUP=off` to disable it.
* **Where to see it:** the active backend is shown in Help → About, in `/api/status` and in every Location tab.
* **No network:** verified by a unit test that blocks all sockets, and by running the analysis under `unshare -n` and `strace` (no `connect`/`send` calls at all).
* **Cost:** loading the boundary data takes about 50 ms once, at startup. A lookup takes about 1 µs.
* **Updates:** `pip install -U tzfpy` updates the boundaries. The DST rules come from the operating system's tz database (`sudo apt upgrade tzdata` on Debian).

**Known differences between boundary datasets – Xinjiang:** in Xinjiang (China), both Beijing time (UTC+8, `Asia/Shanghai`) and Xinjiang time (UTC+6, `Asia/Urumqi`) are in everyday use. tzfpy reports **both** zones there (`get_tzs` → `['Asia/Shanghai', 'Asia/Urumqi']`), so a photo recording either +08:00 or +06:00 is "consistent". In Beijing only `Asia/Shanghai` applies. The single-zone call `get_tz` and other libraries (e.g. timezonefinder) return only one of the two zones, which can make a correct candidate look "inconsistent". This is one more reason why timezone evidence never resolves a location. The CTF example's N/E mirror lies in Xinjiang.

#### Timezone boundary data – attribution (ODbL)
When the optional tzfpy package is installed, timezone boundaries come from **[timezone-boundary-builder](https://github.com/evansiroky/timezone-boundary-builder)** by Evan Siroky. That data is derived from **© OpenStreetMap contributors** and is available under the **[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/)**. It is packaged and simplified by **[tzf / tzfpy](https://github.com/ringsaturn/tzfpy)** (ringsaturn; code under the MIT licence; boundaries simplified to within ~111 m of the original borders). Photo Tool does not bundle or modify this data; it is installed separately with `pip`. The attribution is also shown in Help → About and next to timezone evidence.

#### Collection context
For each candidate, Photo Tool looks at the photos in the batch that **do** have resolved GPS:
* **Support:** a resolved photo from the **same device model** (or with the same recorded serial number), taken within **6 h**, whose location is within **50 km** of the candidate. Example: *"A photo from the same device model (not necessarily the same physical device) taken 2 minutes earlier (UTC) has a resolved location 1.1 km from this candidate."*
* **Conflict:** a same-model photo within 6 h that would require travelling faster than **1,000 km/h** to reach this candidate. Example: *"… 11,116 km from this candidate (implied travel: 351,023 km/h – implausible only if both photos come from the same physical camera)."*
* **Weak context:** the closest resolved photo of *any* device is shown, but labelled *"different/unknown device – no device relationship, weak context"*.
* **Time differences:** compared in UTC when both photos record an offset, otherwise on the local clock (and labelled that way).
* **Limits:** at most 5 statements are written out per candidate, but every match is counted.

The thresholds are in `photo_tool/config.py` (`CONTEXT_*`) and are shown next to the results.

**Same device model ≠ same physical device.** Photos are related by their make/model string ("Apple iPhone 12"); millions of phones share that string. Every statement says *"same device model (not necessarily the same physical device)"*. A matching recorded **serial number** is described as *"same recorded camera serial number"*, with the caveat that metadata can be copied, so it is not proof of the same physical camera either.

**Patterns across ambiguous images:** e.g. *"5 image(s) from Apple iPhone 12 have ambiguous GPS with the same pattern: GPSLatitudeRef missing + GPSLongitudeRef missing."* This can reveal how the metadata was processed or damaged. Sharing a pattern does **not** mean the images share a hemisphere.

#### Evidence categories and evidence-based ordering (no scores)
Each candidate's contextual evidence is sorted into four categories:

| Category | Examples | Affects the order? |
|---|---|---|
| **Supporting** | timezone consistent; a same-model photo ≤ 6 h and ≤ 50 km away | yes |
| **Conflicting** | timezone inconsistent; a same-model photo implying > 1,000 km/h travel | yes |
| **Weak** | nearest resolved photo is from a *different/unknown* device; same-model photos exist but none within 6 h | no |
| **Unavailable** | timezone unknown (no backend, ocean, no offset/timestamp); no resolved photos in the collection | no |

Candidates are then ordered by a **fixed, visible rule**. There are no scores, weights, percentages or probabilities:
1. **Most contextually supported:** supporting evidence and no conflicting evidence
2. **No contextual evidence either way**
3. **Mixed contextual evidence:** both supporting and conflicting
4. **Only conflicting contextual evidence**

Within a group the generated order (N/E, N/W, S/E, S/W) is kept, so nothing is decided by hidden tie-breaks.
* **Always visible:** every geometrically possible candidate stays in the list and on the map.
* **Explained:** every card shows its position, its group and "Why here", plus a note when several candidates tie.
* **Never a conclusion:** the first candidate is labelled *"Most contextually supported · inference"*, never "correct", "confirmed" or "resolved". The photo itself stays `ambiguous`.

For the CTF collection with tzfpy installed, the order is: N/W (timezone consistent + same-model photos 1.1 km away), then N/E (timezone inconsistent + implausible travel), then the two ocean candidates (implausible travel; their timezone is *unavailable* and is not used as evidence).

#### Privacy, network and performance
* **Network:** no new requests, with or without tzfpy. The analysis uses only the local metadata, the local tz database and (optionally) the boundary data inside the installed tzfpy package. Coordinates, timestamps, filenames and images are never sent anywhere. One caveat: with **online map tiles** on, *viewing* candidates on the map downloads OpenStreetMap tiles for those areas, including the true one. Turn tiles off (View menu) for sensitive cases.
* **Performance:** the analysis runs **only on demand** (Location tab or "Show possible locations"). It is computed in one pass over the collection and cached until the workspace changes. Normal extraction only gained the capture-time parsing, about 8 µs per image. tzfpy lookups add no measurable time. Measured with `python tools/benchmark_location.py`, which measures both lookups when tzfpy is installed:

| Images | ExifTool processes | On-demand analysis (without / with tzfpy) |
|---|---|---|
| 10 | 2 | < 10 ms |
| 100 | 2 | 4–17 ms |
| 1,000 | 20 (batches of 100 × 2) | 0.19–0.26 s (100 ambiguous) · 0.59–0.66 s (500 ambiguous × 500 resolved) |

### How sanitization works (`photo_tool/sanitize.py`)
| Mode | What happens | Pixels |
|---|---|---|
| **HEIC input** | In re-encode mode the output is **JPEG by default** (the "HEIC →" option), because many websites and Windows/Android programs cannot open HEIC. You can choose "keep HEIC" instead. Lossless mode always keeps HEIC. Depth maps, auxiliary images, embedded thumbnails and the Live Photo link are dropped when re-encoding. | JPEG q95 / HEIC q90 |
| **Re-encode** (default) | Pillow decodes the image, bakes the EXIF rotation into the pixels and writes a **brand-new file** from pixel data only. EXIF, XMP, IPTC, maker notes, embedded thumbnails, MPF images, PNG text chunks and trailers cannot survive because nothing is copied from the original. | PNG/TIFF identical; JPEG/WebP re-compressed at quality 95 |
| **Lossless** | `exiftool -all= -trailer:all=` plus explicit deletion of the IFD0 text tags. Only `Orientation` is copied back so the photo isn't shown rotated. | bit-identical |

Then, in both modes:
1. The output is **re-read with ExifTool** in a separate, batched run.
2. It is classified with the **same rules** used for the originals.
3. It is compared field by field and category by category with the original.
4. It is decoded again with Pillow and its dimensions are checked.

An image is shown as **"✔ Verified clean" only if ExifTool finds zero tags in any sensitive category**. Otherwise the UI lists exactly which tags are still present. The original file is never overwritten.

The ICC colour profile is removed by default. A checkbox lets you keep it, because it improves colour accuracy for wide-gamut phone photos. The profile describes colours, not the photo, but it can reveal the device vendor, so it is off by default.

### Temporary files
The server makes one private directory at startup with `tempfile.mkdtemp` (mode 0700), for example `/tmp/photo-tool-abc123/`:

```
originals/  <id>.<ext>   untouched uploads
thumbs/     <id>.jpg     thumbnails
previews/   <id>.jpg     previews
sanitized/  <id>.<ext>   sanitized copies
zips/                    ZIPs exist only while being sent (unlinked immediately)
```
The directory is deleted when the server exits (`atexit`), and also when you click **Clear workspace**. If the process is killed with `kill -9`, delete `/tmp/photo-tool-*` by hand. Werkzeug may also spool large uploads to the system temp directory while a request is being received; it deletes those itself.

### Dependencies
* **System:** Python 3.9 or newer, `python3-venv`, ExifTool (`libimage-exiftool-perl`)
* **Python:** Flask, Pillow and pillow-heif (`requirements.txt`). Pillow wheels already include libjpeg, libpng, libwebp and libtiff, and pillow-heif wheels include libheif, so no `-dev` packages are needed.
* **Optional:** `tzfpy` (`requirements-timezone.txt`) for offline coordinate → timezone lookup in the ambiguous-GPS investigation. The app works without it (timezone evidence = "unknown"). Its boundary data is ODbL-licensed; see *Timezone boundary data – attribution*.
* **Frontend:** Leaflet 1.9.4, **bundled** in `static/vendor/leaflet/` (no CDN). No npm and no build step.

### Debian-specific notes
* Debian 12+ blocks `pip install` into the system Python (PEP 668, "externally-managed-environment"). **Use the virtual environment** shown below.
* `python3 -m venv` needs the separate `python3-venv` package.
* The Debian package for ExifTool is called **`libimage-exiftool-perl`**, not `exiftool`.
* HEIC/HEIF support comes from the pip package `pillow-heif`, which bundles libheif, so it needs no apt package on normal x86_64/arm64 machines. If pip cannot find a pre-built wheel for your machine, install `libheif-dev` with apt and retry. If pillow-heif is missing, the app still runs: the status bar shows "HEIC off" and `.heic` files are rejected with a hint.

---

## 2. Project tree

```
photo-tool/
├── app.py                     Flask app: routes, security headers, startup
├── requirements.txt
├── requirements-timezone.txt  OPTIONAL: tzfpy (offline coordinate → timezone; ODbL data)
├── README.md
├── photo_tool/
│   ├── __init__.py
│   ├── config.py              all limits and settings (env-overridable)
│   ├── workspace.py           ImageRecord + in-memory store + temp directory
│   ├── uploads.py             validation, safe storage, thumbnails/previews
│   ├── metadata.py            ExifTool runner, normalization, sensitivity rules
│   ├── gps.py                 GPS interpretation: valid / ambiguous / incomplete (never guesses a hemisphere)
│   ├── capture_time.py        capture time paired with its UTC offset (validated)
│   ├── timezones.py           timezone consistency (zoneinfo) + pluggable offline lookup
│   ├── location_analysis.py   ambiguous GPS: derived candidates + contextual evidence
│   ├── sanitize.py            sanitization (2 modes) + verification/comparison
│   ├── downloads.py           safe download names, ZIP building
│   └── heif_support.py        enables HEIC/HEIF via pillow-heif (optional)
├── templates/
│   └── index.html             single page layout
├── static/
│   ├── css/style.css
│   ├── js/app.js              all frontend logic (state, rendering, sync, upload)
│   └── vendor/leaflet/        bundled Leaflet 1.9.4 (BSD-2 licence)
├── tests/
│   ├── test_gps.py            GPS hemisphere regression tests (unittest)
│   ├── test_location_analysis.py  candidates, timezone, ordering, collection context, API invariants
│   └── browser/ui_checks.js   optional Playwright checks (UI, candidate separation, filename injection)
└── tools/
    ├── make_test_images.py    generates a demo/test batch with fake metadata
    ├── smoke_test.py          automated end-to-end test (no browser needed)
    └── benchmark_location.py  extraction + candidate-analysis benchmark (10/100/1000 images)
```

---

## 3. Install (Debian 12 / Ubuntu 22.04+)

System packages:
```bash
sudo apt update
```
```bash
sudo apt install -y python3 python3-venv python3-pip libimage-exiftool-perl
```
Check that ExifTool works:
```bash
exiftool -ver
```
Create the virtual environment inside the project:
```bash
cd photo-tool
```
```bash
python3 -m venv .venv
```
```bash
source .venv/bin/activate
```
```bash
pip install --upgrade pip
```
```bash
pip install -r requirements.txt
```
Optional: offline timezone evidence for ambiguous GPS (~3 MB, no network use at runtime):
```bash
pip install -r requirements-timezone.txt
```

## 4. Run

```bash
source .venv/bin/activate
```
```bash
python app.py
```
Open **http://127.0.0.1:5000** in your browser. Stop the server with `Ctrl+C`; the temporary workspace is deleted.

### Optional environment variables
| variable | default | meaning |
|---|---|---|
| `PHOTO_TOOL_PORT` | `5000` | port |
| `PHOTO_TOOL_HOST` | `127.0.0.1` | bind address (there is **no authentication**, so keep it local) |
| `PHOTO_TOOL_ALLOWED_HOSTS` | – | extra allowed `Host` headers, comma-separated (needed if you bind to a LAN IP) |
| `PHOTO_TOOL_MAX_FILE_MB` | `50` | max size per image |
| `PHOTO_TOOL_MAX_IMAGES` | `500` | max images in the workspace |
| `PHOTO_TOOL_DEBUG` | `0` | `1` for debug logging + Flask debug mode |
| `PHOTO_TOOL_EXIFTOOL` | `exiftool` | path to the ExifTool binary |

Example:
```bash
PHOTO_TOOL_PORT=8080 PHOTO_TOOL_DEBUG=1 python app.py
```

### Demo data
Make a test batch with fake metadata. It covers GPS in several cities (all four hemispheres), CTF-style GPS with missing or malformed hemisphere references, duplicate names, a corrupted file, a rotated image, PNG text chunks, WebP, TIFF, Photoshop history, an embedded thumbnail and 0,0 GPS:
```bash
python tools/make_test_images.py
```
Then drag the `sample_images/` folder onto the drop zone.

Automated end-to-end check (upload, both sanitize modes, verification, ZIP, originals unchanged):
```bash
python tools/smoke_test.py
```

Regression tests (79):
* **GPS:** N/E, N/W, S/E, S/W, missing and malformed references, XMP with and without hemisphere letters, no GPS
* **Candidates:** 2 or 4 candidates, zero-axis deduplication, 0,0, incomplete pairs
* **Timezone:** consistent, inconsistent, DST, historical offsets, repeated hour, ocean zones, missing or malformed offset, missing timestamp
* **Collection context:** same device model vs physical device, unrelated device, patterns
* **Ordering:** groups, stable order, ties, nothing hidden, no score/percentage fields, no "correct/confirmed/resolved" wording
* **Optional tzfpy backend:** real boundaries, Xinjiang's two local times, oceans, DST, and lookups in an interpreter where every socket call raises. These 5 tests are skipped when tzfpy is not installed.
* **Frontend guard:** Leaflet popups/tooltips never receive strings, and `innerHTML` is only used with constants
* **Invariants:** context never promotes to `valid`, candidates never enter the resolved dataset or counts, sanitization unchanged

The ExifTool-based tests are skipped automatically if ExifTool is not installed. Run the suite once without and once with `requirements-timezone.txt` to cover both states.
```bash
python -m unittest discover -s tests -v
```

Optional browser checks (Node.js + Playwright with Chromium). They need a running server with an empty workspace and the sample images, adapt to whether tzfpy is active, and include the filename-injection regression:
```bash
node tests/browser/ui_checks.js sample_images http://127.0.0.1:5000
```

Performance check for collections of 10, 100 and 1,000 images:
```bash
python tools/benchmark_location.py
```

The demo set includes `sample_images/ctf_collection/`, an iPhone series near 38.55° N 78.33° W. Some photos have missing references (both, latitude only, longitude only), one is DST-sensitive (6 Nov 2021, −04:00), one has a malformed offset (`+25:00`) and one has no capture time. It also contains an **unrelated** Canon photo near the mirrored N/E candidate and a Sydney Pixel pair with a missing `GPSLongitudeRef`.

---

## 5. Manual testing checklist

Use `sample_images/` (file names below) or your own photos.

| # | Test | Steps | Expected |
|---|---|---|---|
| 1 | JPEG with EXIF, no GPS | Upload `01_exif_no_gps.jpg` | Badges TIME, DEV, SW, AUTH, ID. Table shows the Canon device, timestamp and `-` for GPS. Map says *"No GPS metadata found in this batch."* |
| 2 | JPEG with GPS | Upload `02_gps_phoenix_morning.jpg` | GPS badge, coordinates in the table, marker near Phoenix, popup with thumbnail, filename, lat/lon, time and device |
| 3 | Many at once | Drag the whole `sample_images` folder anywhere into the window | 27 thumbnails (25 without pillow-heif), 2 rejected files listed, Batch Summary and status bar filled in, map fits all markers (12 resolved GPS) |
| 4 | Duplicate filenames | `folder_a/photo.jpg` and `folder_b/photo.jpg` | Two separate cards marked "same name". The byte-identical copy is marked "identical ×2". Each has its own ID and metadata. Correlation lists them. |
| 5 | No EXIF | `08_no_metadata.png` | Badge "none", all fields `-`, no errors |
| 6 | Corrupted / fake | `10_corrupted_truncated.jpg`, `11_not_an_image.jpg` | Both listed under "rejected" with a reason. The rest of the batch is fine. |
| 7 | Thumbnail sync | Click a GPS thumbnail | Pale-blue selection on the thumbnail, Details row highlighted and scrolled into view, Properties dialog shows the image, map zooms to the marker with its popup open, breadcrumb and status bar show the file name |
| 8 | Table sync | Click a row in Details | Same as 7, and the Thumbnails pane scrolls to the card |
| 9 | Map sync | Click **All Images** (or press Esc), then click a marker in GPS Locations | Thumbnail, row and Properties dialog all switch to that image |
| 10 | Sanitize one | Select an image → toolbar **Sanitize ▾ → Sanitize Selected Image** (or the **Sanitize Image** button in Properties) | Properties switches to the **Sanitize** tab: green "Verified clean" shield, an **Original Metadata → Sanitized Metadata** table with a green ✓ **Removed** for GPS, timestamps and device, plus lists of remaining and removed tags. The thumbnail gets ✔ CLEAN. |
| 11 | Manual ExifTool check | Download the sanitized copy, then run the commands in section 6 | No GPS, Make, Model, dates, Software or Artist tags |
| 12 | Several + ZIP | Tick 3 images → **Sanitize ▾ → Sanitize Ticked Images (3)** → **Export ▾ → Ticked Sanitized Images as ZIP** | `sanitized_images.zip` with `name_sanitized.ext` files. Duplicate names become `photo_sanitized.jpg` and `photo_sanitized_2.jpg`. |
| 13 | GPS hemispheres | Upload `21_gps_south_east_sydney.jpg`, `22_gps_south_west_rio.jpg`, `02_gps_phoenix_morning.jpg` (N/W), `04_gps_berlin.jpg` (N/E) | All four plotted in the right place; Sydney has a negative latitude, Rio has negative latitude and longitude |
| 14 | Ambiguous GPS | Upload `23_gps_missing_lat_ref.jpg` … `27_xmp_gps_no_hemisphere_letter.jpg` | **No markers.** `GPS ±?` badge, "Ambiguous GPS" tile = 5, status bar "5 ambiguous GPS". The Details GPS column shows raw values with `?` for the unknown hemisphere. Properties → Metadata shows "reference: MISSING" and names the missing tag. The Privacy tab explains why it is not on the map, with no OpenStreetMap link. |
| 15 | Possible locations | Upload `sample_images/ctf_collection/`, tick **Show possible locations** | Dashed "?" markers appear: 4 for `IMG_4014.JPG`, 2 for `IMG_4016.JPG`. The resolved-marker count, GPS tile, GPS filter and status bar do **not** change, and the warning banner is shown. The N/W candidates of IMG_4012/4014/4016 carry a blue dot (contextual evidence). Untick the box → the map is back to resolved GPS only. |
| 16 | Candidate evidence | Select `IMG_4014.JPG` → Properties → **Location** | RAW (both references "missing") / RESOLVED ("none – not resolved") / DERIVED (4 candidates) / INFERENCE. N/W: "A photo from the same device model taken 2 minutes earlier (UTC) has a resolved location 1.1 km from this candidate." N/E: conflict (implausible travel), and the Canon photo is listed only as weak context. Timezone: "unknown – offline timezone boundary data is not installed". Assessment: "Derived candidate · Not confirmed". |

Extra checks:
* Filters (Details pane or **Analyze** menu), the search box, Batch Summary tiles and Correlation values all filter the thumbnails, table and map together. **All Images** / Esc resets everything.
* Menus: check marks in **Analyze**, **View** and **Tools** follow the real settings. Items are greyed out when they can't be used (e.g. **Export** before anything is sanitized).
* Keyboard: Ctrl+O add images, Ctrl+S save the selected sanitized copy, F1 shortcuts, ← / → previous/next, Esc All Images.
* `12_rotated_orientation6.jpg` displays upright, and the sanitized copy is upright too (in re-encode mode the rotation is baked in).
* `16_embedded_thumbnail.jpg` gets a THUMB badge, and the embedded thumbnail is gone after sanitizing.
* `17_gps_zero_zero.jpg` has a "GPS?" badge, a warning, and no marker.
* Sanitize an ambiguous-GPS image: the Original → Sanitized table shows e.g. `33.4484° N, 112.074° ? (ambiguous)` → ✓ Removed.
* `18_png_named_as.jpg` shows an "extension mismatch" warning.
* Lossless mode on `14_tiff_with_artist.tif` comes out verified clean.
* **HEIC:** `19_iphone_new_york.heic` and `20_iphone_statue.heif` show thumbnails, GPS markers in New York and Apple device info. Sanitize with "HEIC → JPEG": the download is `..._sanitized.jpg` and the before/after panel shows `Format HEIC → JPEG`. Choose "keep HEIC" and sanitize again: the download is `..._sanitized.heic`, also verified clean.
* Untick **Online tiles** (GPS Locations pane, or View → Online Map Tiles): no more requests go to OpenStreetMap, and the markers stay.
* On a phone-width window the toolbar shows icons only, panes stack vertically and selecting an image scrolls to its Properties.

---

## 6. Verify metadata removal yourself with ExifTool

Download a sanitized file, then:

```bash
exiftool -a -G1 -s ~/Downloads/02_gps_phoenix_morning_sanitized.jpg
```
You should only see `[System]`, `[File]`, `[JFIF]` and `[Composite]` (ImageSize/Megapixels) tags. For TIFF you will also see structural `[IFD0]` tags (width, compression, strips).

Quick search for sensitive tags (no output means clean):
```bash
exiftool -a -G1 -s ~/Downloads/*_sanitized.* | grep -iE 'gps|make|model|date/time|createdate|software|artist|serial|copyright|thumbnail|creator|author'
```
`FileModifyDate` and similar `[System]` tags are the **filesystem** dates of your downloaded copy, not metadata inside the image.

Compare with the original:
```bash
exiftool -a -G1 -s sample_images/02_gps_phoenix_morning.jpg | grep -iE 'gps|make|model'
```

Unpack a ZIP and check every file:
```bash
mkdir -p /tmp/check && unzip -o ~/Downloads/sanitized_images.zip -d /tmp/check && exiftool -a -G1 -s -GPS:all -IFD0:Make -IFD0:Model -ExifIFD:DateTimeOriginal /tmp/check
```

---

## 7. Likely problems and where to look

| Symptom | Likely cause | Where to look |
|---|---|---|
| Red "ExifTool NOT found" pill | ExifTool not installed / not on PATH | `sudo apt install libimage-exiftool-perl`; `metadata.py → exiftool_version()` |
| `error: externally-managed-environment` | pip into system Python | Activate `.venv` first (section 3) |
| All uploads fail with "Missing X-Photo-Tool header" | Old cached JS | Hard refresh (Ctrl+Shift+R). Header check is in `app.py → security_checks()` |
| "Unexpected Host header" | Opened via a LAN IP / custom hostname | Use `127.0.0.1`, or set `PHOTO_TOOL_ALLOWED_HOSTS` |
| `.heic` rejected / status bar shows "HEIC off" | pillow-heif not installed in the venv | `pip install -r requirements.txt` in the activated venv, restart; `photo_tool/heif_support.py` |
| A file is rejected that you think is valid | Validation | Server log line `Rejected <name>: <reason>`; `uploads.py → _detect_and_verify()` / `_create_derivatives()` |
| "Request too large" (413) | File > limit | `PHOTO_TOOL_MAX_FILE_MB`; `config.py` |
| A field shows `-` but ExifTool shows a value | Tag name not in the lookup list | `metadata.py → normalize_metadata()` (add the tag name to the list for that field) |
| Badge/category wrong or missing | Classification rules | `metadata.py → classify_tag()` and the regex/sets above it |
| GPS not on the map | `gps_status` is ambiguous/zero/invalid/incomplete (only `valid` is plotted) | Properties → Metadata → GPS status / note; `photo_tool/gps.py → interpret_gps()`; run `python -m unittest discover -s tests -v` |
| Hemisphere looks wrong | Check the reference tags yourself | `exiftool -a -G1 -s -n -GPS:all -XMP-exif:GPS* file.jpg`, and `exiftool -b -XMP file.jpg` for the raw XMP letter |
| Map is grey | Tiles blocked, offline, or toggle off | Browser devtools Network tab; `app.js → setTilesEnabled()`; CSP `img-src` in `app.py` |
| Map is the wrong size / half drawn | Leaflet measured a hidden container | `app.js → ensureMap()` (`invalidateSize`) |
| Clicking X does not update Y | Selection sync | `app.js → selectImage()` / `updateSelectionUI()` |
| A menu item does nothing / is always grey | Command table | `app.js → COMMANDS` (`run` / `enabled`) and the `data-command` name in `index.html` |
| Filters behave oddly | Filter logic | `app.js → passesFilters()` / `FLAG_FILTERS` |
| Sanitized copy "NOT CLEAN" | A tag survived (e.g. a new vendor format in lossless mode) | Try re-encode mode; `sanitize.py → _sanitize_with_exiftool()` |
| Sanitized JPEG looks rotated | Orientation handling | `sanitize.py → _sanitize_by_reencoding()` (`exif_transpose`) |
| ZIP download fails | Nothing sanitized yet / disk space | Server log `ZIP creation failed`; `downloads.py` |
| JavaScript error | – | Browser devtools Console (F12). All UI code is in `static/js/app.js`. |

Server logs go to the terminal. Start with `PHOTO_TOOL_DEBUG=1` to also see ExifTool stderr.

---

## 8. Security design

* Binds to `127.0.0.1` only. The `Host` header is checked to block DNS-rebinding attacks.
* State-changing API calls need a custom `X-Photo-Tool` header, which other websites cannot send without a CORS preflight (blocks CSRF).
* Strict Content-Security-Policy: no inline scripts; images only from self and the OSM tile server. Also `nosniff` and `frame-ancestors 'none'`.
* **Rendering of untrusted text** (metadata values, filenames). In Photo Tool's own UI code (`static/js/app.js`), this text is inserted only as text nodes: the `h()` helper uses `createTextNode`/`textContent`. The only places where `app.js` sets HTML are constant strings that contain no data: the static SVG shield icons (`ICONS`) and the "?" inside candidate markers.
  * **Leaflet:** Leaflet renders **string** popup/tooltip content as HTML (`innerHTML`). Photo Tool therefore always passes popups and tooltips a **DOM node** or a function that returns one. Earlier versions passed filenames as strings, so a file named `<img src=x id=pwn>.jpg` was inserted as markup in map tooltips. The CSP blocked script execution, but HTML injection was possible. This is fixed.
  * **Regression checks:** a static unit test (`FrontendRenderingGuardTests`) fails if a Leaflet content call receives anything other than `h(...)` or `() =>`, or if `innerHTML` is assigned something other than a constant. The browser check (`tests/browser/ui_checks.js`) uploads `<img src=x id=pwn>.jpg` and confirms it appears only as text.
  * **Scope:** this covers Photo Tool's code. Third-party Leaflet controls (zoom, attribution) only show constant text.
* File paths are built only from the server-generated random ID and a canonical extension. The user's filename never touches the filesystem. Download names go through `secure_filename`.
* The file type comes from the content (magic bytes via Pillow), not the extension. Every image is fully decoded at upload, and decompression bombs are refused (`MAX_IMAGE_PIXELS`).
* Limits on file size, files per request, request size and total images.
* ExifTool is always called with an argument list (`shell=False`), with `-config ""` so no user config file is loaded, and with a timeout. Paths are absolute, so they can never be read as options.
* Originals are written in exclusive-create mode and never modified. Sanitized copies are separate files. `smoke_test.py` checks the originals' SHA-256 after sanitizing.

## 9. Known limitations

* **JPEG/WebP re-encode mode is lossy** (quality 95). Use **Lossless** mode when pixels must be bit-identical.
* **RAW** formats (CR2, NEF, DNG...) are not supported.
* **HEIC:** a few rare HEIC variants (image sequences, some 10-bit HDR files) are decoded to 8-bit; the default JPEG output is 8-bit anyway. In lossless mode the EXIF Orientation tag of a HEIC is kept, but it is informational only: HEIC rotation lives in the container and is preserved.
* **Animated WebP / multi-page TIFF:** thumbnails show the first frame. Sanitizing switches to lossless mode automatically so no frames are lost.
* **MPO** (multi-picture JPEG): re-encode keeps only the main picture, which is intentional because the extra pictures are metadata-like.
* **Not covered:** content *inside* the pixels (faces, street signs, screens), steganography and sensor fingerprints (PRNU). Metadata sanitizing does not make a photo anonymous.
* **Map tiles:** the tile server sees which map areas you view, and so could infer where your photos were taken. Switch tiles off for sensitive cases.
* **Single user:** the workspace is in memory and is lost on restart. There is no authentication, so do not expose the tool on a network.
* The sensitivity rules use tag names. Vendor maker notes are flagged as one category rather than parsed one by one.
* **GPS altitude:** a missing `GPSAltitudeRef` is read as "above sea level", which is the EXIF default. Latitude/longitude are never defaulted.
* **GPS sources:** only EXIF and XMP GPS coordinates are interpreted. Video-style QuickTime `GPSCoordinates` are not used for the map.
* **Timezone consistency:** without the optional tzfpy package every result is "Timezone unknown". With it, timezone only narrows candidates: cameras are often set to the wrong or home timezone, one offset covers a wide band of longitude, and boundary data can differ near borders (~111 m simplification) or for regions with two local times (Xinjiang, see above).
* **Collection context:** related photos share a device *model* string (or a recorded serial number). That is not proof of the same physical device: two people with the same phone model look related. The thresholds (6 h, 50 km, 1,000 km/h) and the ordering rule are simple, documented rules, not statistics.
* **GPSDateStamp/GPSTimeStamp** (UTC) are not yet used to derive a capture offset.


## AI Usage

Claude was used extensively for implementation and debugging. I defined the project idea, OSINT/OPSEC workflow, feature requirements, interaction design, security requirements, and testing criteria, then used Claude to implement and iterate on them. I manually tested the application and independently verified metadata sanitization with ExifTool.
