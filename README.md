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
   * validates the GPS
   * sorts every tag into privacy categories
6. The browser keeps the summaries in `state` and draws every panel from that state.

### How metadata is stored internally (`photo_tool/workspace.py → ImageRecord`)
| field | meaning |
|---|---|
| `id` | 32-character random hex string; the **only** identifier. Two files called `photo.jpg` get different IDs. |
| `original_filename` | display only (untrusted text) |
| `sha256`, `file_size`, `width`, `height`, `display_width/height`, `image_format` | file facts |
| `raw` | **complete** ExifTool output as `{"EXIF:IFD0:Make": "Canon", ...}`, with filesystem tags of the temporary copy hidden |
| `normalized` | key fields for the table: `device`, `timestamp`, `timestamp_sortable`, `software`, `gps_latitude`/`gps_longitude`/`gps_altitude`, `gps_status`, `artist`, `copyright`, `lens`, `serial_number`, ... (`None` means not present; the UI shows "-") |
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
├── README.md
├── photo_tool/
│   ├── __init__.py
│   ├── config.py              all limits and settings (env-overridable)
│   ├── workspace.py           ImageRecord + in-memory store + temp directory
│   ├── uploads.py             validation, safe storage, thumbnails/previews
│   ├── metadata.py            ExifTool runner, normalization, GPS, sensitivity rules
│   ├── sanitize.py            sanitization (2 modes) + verification/comparison
│   ├── downloads.py           safe download names, ZIP building
│   └── heif_support.py        enables HEIC/HEIF via pillow-heif (optional)
├── templates/
│   └── index.html             single page layout
├── static/
│   ├── css/style.css
│   ├── js/app.js              all frontend logic (state, rendering, sync, upload)
│   └── vendor/leaflet/        bundled Leaflet 1.9.4 (BSD-2 licence)
└── tools/
    ├── make_test_images.py    generates a demo/test batch with fake metadata
    └── smoke_test.py          automated end-to-end test (no browser needed)
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
Make a test batch with fake metadata. It covers GPS in several cities, duplicate names, a corrupted file, a rotated image, PNG text chunks, WebP, TIFF, Photoshop history, an embedded thumbnail and 0,0 GPS:
```bash
python tools/make_test_images.py
```
Then drag the `sample_images/` folder onto the drop zone.

Automated end-to-end check (upload, both sanitize modes, verification, ZIP, originals unchanged):
```bash
python tools/smoke_test.py
```

---

## 5. Manual testing checklist

Use `sample_images/` (file names below) or your own photos.

| # | Test | Steps | Expected |
|---|---|---|---|
| 1 | JPEG with EXIF, no GPS | Upload `01_exif_no_gps.jpg` | Badges TIME, DEV, SW, AUTH, ID. Table shows the Canon device, timestamp and `-` for GPS. Map says *"No GPS metadata found in this batch."* |
| 2 | JPEG with GPS | Upload `02_gps_phoenix_morning.jpg` | GPS badge, coordinates in the table, marker near Phoenix, popup with thumbnail, filename, lat/lon, time and device |
| 3 | Many at once | Drag the whole `sample_images` folder anywhere into the window | 20 thumbnails (18 without pillow-heif), 2 rejected files listed, Batch Summary and status bar filled in, map fits all markers |
| 4 | Duplicate filenames | `folder_a/photo.jpg` and `folder_b/photo.jpg` | Two separate cards marked "same name". The byte-identical copy is marked "identical ×2". Each has its own ID and metadata. Correlation lists them. |
| 5 | No EXIF | `08_no_metadata.png` | Badge "none", all fields `-`, no errors |
| 6 | Corrupted / fake | `10_corrupted_truncated.jpg`, `11_not_an_image.jpg` | Both listed under "rejected" with a reason. The rest of the batch is fine. |
| 7 | Thumbnail sync | Click a GPS thumbnail | Pale-blue selection on the thumbnail, Details row highlighted and scrolled into view, Properties dialog shows the image, map zooms to the marker with its popup open, breadcrumb and status bar show the file name |
| 8 | Table sync | Click a row in Details | Same as 7, and the Thumbnails pane scrolls to the card |
| 9 | Map sync | Click **All Images** (or press Esc), then click a marker in GPS Locations | Thumbnail, row and Properties dialog all switch to that image |
| 10 | Sanitize one | Select an image → toolbar **Sanitize ▾ → Sanitize Selected Image** (or the **Sanitize Image** button in Properties) | Properties switches to the **Sanitize** tab: green "Verified clean" shield, an **Original Metadata → Sanitized Metadata** table with a green ✓ **Removed** for GPS, timestamps and device, plus lists of remaining and removed tags. The thumbnail gets ✔ CLEAN. |
| 11 | Manual ExifTool check | Download the sanitized copy, then run the commands in section 6 | No GPS, Make, Model, dates, Software or Artist tags |
| 12 | Several + ZIP | Tick 3 images → **Sanitize ▾ → Sanitize Ticked Images (3)** → **Export ▾ → Ticked Sanitized Images as ZIP** | `sanitized_images.zip` with `name_sanitized.ext` files. Duplicate names become `photo_sanitized.jpg` and `photo_sanitized_2.jpg`. |

Extra checks:
* Filters (Details pane or **Analyze** menu), the search box, Batch Summary tiles and Correlation values all filter the thumbnails, table and map together. **All Images** / Esc resets everything.
* Menus: check marks in **Analyze**, **View** and **Tools** follow the real settings. Items are greyed out when they can't be used (e.g. **Export** before anything is sanitized).
* Keyboard: Ctrl+O add images, Ctrl+S save the selected sanitized copy, F1 shortcuts, ← / → previous/next, Esc All Images.
* `12_rotated_orientation6.jpg` displays upright, and the sanitized copy is upright too (in re-encode mode the rotation is baked in).
* `16_embedded_thumbnail.jpg` gets a THUMB badge, and the embedded thumbnail is gone after sanitizing.
* `17_gps_zero_zero.jpg` has a "GPS?" badge, a warning, and no marker.
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
| GPS not on the map | `gps_status` is zero/invalid/incomplete | Warnings on the Properties → General tab; `metadata.py → extract_gps()` |
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
* Every metadata value and filename is rendered with `textContent`, never `innerHTML`, so a malicious EXIF string cannot inject HTML or JavaScript.
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


## AI Usage

Claude was used extensively for implementation and debugging. I defined the project idea, OSINT/OPSEC workflow, feature requirements, interaction design, security requirements, and testing criteria, then used Claude to implement and iterate on them. I manually tested the application and independently verified metadata sanitization with ExifTool.
