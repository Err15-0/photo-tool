"""
Photo Tool - batch photo metadata analysis (OSINT) and sanitization (OPSEC).

Run:   python app.py      then open http://127.0.0.1:5000

This file contains ONLY the Flask routes. The real work happens in:
    photo_tool/uploads.py    validation, storage, thumbnails
    photo_tool/metadata.py   ExifTool, normalization, sensitivity rules
    photo_tool/sanitize.py   sanitization + verification
    photo_tool/downloads.py  download names and ZIP files
    photo_tool/workspace.py  in-memory store + temporary directory

API overview (all JSON unless noted):
    GET  /                              the single-page UI
    GET  /api/status                    ExifTool version, limits, image count
    POST /api/upload                    multipart "files" (several) -> new images + per-file errors
    GET  /api/images                    summaries of all images (no raw metadata)
    GET  /api/images/<id>               full detail incl. raw metadata + sanitization result
    GET  /api/location-analysis         ambiguous-GPS investigation: derived candidates +
                                        contextual evidence (computed on demand, cached)
    GET  /images/<id>/thumb             thumbnail JPEG
    GET  /images/<id>/preview           preview JPEG (max 1600 px)
    POST /api/sanitize                  {"ids": [...]} or {"all": true}, "mode", "keep_icc", "heic_output"
    GET  /images/<id>/sanitized         download one sanitized file
    POST /download/zip                  form field "ids" (comma separated) or "all=1" -> ZIP
    POST /api/clear                     delete everything in the workspace
"""

import logging
import os

from flask import Flask, abort, jsonify, render_template, request, send_file

from photo_tool import config
from photo_tool import heif_support
from photo_tool import location_analysis, timezones
from photo_tool.downloads import build_sanitized_zip
from photo_tool.metadata import analyze_metadata, exiftool_version, read_metadata_batch
from photo_tool.sanitize import SANITIZE_MODES, sanitize_images
from photo_tool.uploads import UploadRejected, display_filename, store_uploaded_file
from photo_tool.workspace import Workspace, is_valid_image_id

logging.basicConfig(
    level=logging.DEBUG if config.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("photo_tool")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = config.MAX_REQUEST_SIZE
app.json.sort_keys = False

workspace = Workspace()
EXIFTOOL_VERSION = exiftool_version()
if EXIFTOOL_VERSION:
    log.info("ExifTool %s found", EXIFTOOL_VERSION)
else:
    log.error("ExifTool NOT found - install it: sudo apt install libimage-exiftool-perl")


# =============================================================================
# Security: host check, CSRF-style header check, response headers
# =============================================================================

ALLOWED_HOSTS = {
    f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}", f"[::1]:{config.PORT}",
    "127.0.0.1", "localhost", "localhost.localdomain",
}
EXTRA_HOSTS = os.environ.get("PHOTO_TOOL_ALLOWED_HOSTS", "")
ALLOWED_HOSTS.update(h.strip() for h in EXTRA_HOSTS.split(",") if h.strip())


@app.before_request
def security_checks():
    # Reject unexpected Host headers (protects against DNS-rebinding attacks
    # where a malicious website tries to talk to this local server).
    if request.host not in ALLOWED_HOSTS and not app.testing:
        log.warning("Rejected request with Host header %r", request.host)
        abort(400, description="Unexpected Host header")

    # State-changing API calls must carry a custom header. Browsers do not allow
    # other websites to add custom headers to cross-site requests without a
    # CORS preflight (which we never approve), so this blocks CSRF.
    if request.method == "POST" and request.path.startswith("/api/"):
        if request.headers.get("X-Photo-Tool") != "1":
            abort(403, description="Missing X-Photo-Tool header")


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "            # Leaflet sets inline styles
        "img-src 'self' data: https://tile.openstreetmap.org; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(500)
def json_error(error):
    code = getattr(error, "code", 500)
    messages = {
        413: f"Request too large. Each file may be at most {config.MAX_FILE_SIZE // (1024 * 1024)} MB.",
    }
    message = messages.get(code) or getattr(error, "description", None) or "Internal server error"
    if request.path.startswith("/api/") or request.path.startswith("/download/"):
        return jsonify({"error": message}), code
    return f"{code}: {message}", code


# =============================================================================
# Helpers
# =============================================================================

def get_record_or_404(image_id):
    if not is_valid_image_id(image_id):
        abort(404, description="Unknown image id")
    record = workspace.get(image_id)
    if record is None:
        abort(404, description="Unknown image id")
    return record


def resolve_requested_records(ids, select_all):
    """Turn a list of ids (or 'all') into records, ignoring unknown ids."""
    if select_all:
        return workspace.all()
    records = []
    for image_id in ids or []:
        record = workspace.get(str(image_id))
        if record is not None:
            records.append(record)
    return records


# =============================================================================
# Pages
# =============================================================================

@app.get("/")
def index():
    return render_template(
        "index.html",
        exiftool_version=EXIFTOOL_VERSION,
        max_file_mb=config.MAX_FILE_SIZE // (1024 * 1024),
        max_images=config.MAX_IMAGES,
        allowed_extensions=sorted(config.ALLOWED_EXTENSIONS),
        heif_available=heif_support.HEIF_AVAILABLE,
        heif_version=heif_support.HEIF_VERSION,
    )


@app.get("/api/status")
def api_status():
    return jsonify({
        "exiftool_version": EXIFTOOL_VERSION,
        "image_count": workspace.count(),
        "max_images": config.MAX_IMAGES,
        "max_file_size": config.MAX_FILE_SIZE,
        "max_files_per_request": config.MAX_FILES_PER_REQUEST,
        "allowed_extensions": sorted(config.ALLOWED_EXTENSIONS),
        "sanitize_modes": list(SANITIZE_MODES),
        "heif_available": heif_support.HEIF_AVAILABLE,
        "heic_output_choices": list(config.HEIC_OUTPUT_CHOICES),
        "timezone_lookup": timezones.get_lookup().info(),
    })


# =============================================================================
# Upload + analysis
# =============================================================================

@app.post("/api/upload")
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files were uploaded", "images": [], "errors": []}), 400
    if len(files) > config.MAX_FILES_PER_REQUEST:
        return jsonify({"error": f"At most {config.MAX_FILES_PER_REQUEST} files per request"}), 400

    errors = []
    stored = []
    for file_storage in files:
        name = display_filename(file_storage.filename)
        if workspace.count() + len(stored) >= config.MAX_IMAGES:
            errors.append({"filename": name, "error": f"Workspace limit of {config.MAX_IMAGES} images reached"})
            continue
        try:
            stored.append(store_uploaded_file(file_storage, workspace))
        except UploadRejected as exc:
            log.warning("Rejected %s: %s", name, exc)
            errors.append({"filename": name, "error": str(exc)})
        except Exception as exc:  # never let one file break the whole batch
            log.exception("Unexpected error while storing %s", name)
            errors.append({"filename": name, "error": f"Unexpected error: {exc}"})

    # ONE batched ExifTool run (x2: readable + numeric) for all stored files.
    metadata_by_path = read_metadata_batch([r.paths["original"] for r in stored])
    for record in stored:
        result = metadata_by_path[record.paths["original"]]
        if result["error"]:
            record.metadata_error = result["error"]
        if result["raw"]:
            analysis = analyze_metadata(result["raw"], result["numeric"], result["xmp_packet"])
            record.raw = analysis["raw"]
            record.normalized = analysis["normalized"]
            record.sensitive = analysis["sensitive"]
            record.flags = analysis["flags"]
            record.tag_count = analysis["tag_count"]
            record.warnings.extend(analysis["warnings"])
        else:
            record.flags = {}
        workspace.add(record)

    log.info("Upload: %d stored, %d rejected", len(stored), len(errors))
    return jsonify({
        "images": [r.to_summary() for r in stored],
        "errors": errors,
        "image_count": workspace.count(),
    })


@app.get("/api/images")
def api_images():
    return jsonify({"images": [r.to_summary() for r in workspace.all()]})


@app.get("/api/images/<image_id>")
def api_image_detail(image_id):
    return jsonify(get_record_or_404(image_id).to_detail())


# Cache of the last location analysis: (workspace revision, lookup name) -> result.
# Only computed when the investigator asks for it (Location tab / "Show possible
# locations"), so normal metadata extraction pays nothing for this feature.
_location_cache = {"key": None, "result": None}


@app.get("/api/location-analysis")
def api_location_analysis():
    """
    Derived candidates + contextual evidence for every image with AMBIGUOUS GPS.
    A separate document on purpose: it never changes image summaries, gps_status,
    flags.gps or any resolved-GPS count.
    """
    key = (workspace.revision, timezones.get_lookup().name)
    if _location_cache["key"] != key:
        _location_cache["result"] = location_analysis.analyze_collection(workspace.all())
        _location_cache["key"] = key
    return jsonify(_location_cache["result"])


@app.get("/images/<image_id>/thumb")
def image_thumbnail(image_id):
    record = get_record_or_404(image_id)
    return send_file(record.paths["thumb"], mimetype="image/jpeg", max_age=0)


@app.get("/images/<image_id>/preview")
def image_preview(image_id):
    record = get_record_or_404(image_id)
    return send_file(record.paths["preview"], mimetype="image/jpeg", max_age=0)


# =============================================================================
# Sanitization + downloads
# =============================================================================

@app.post("/api/sanitize")
def api_sanitize():
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode", "reencode")
    if mode not in SANITIZE_MODES:
        return jsonify({"error": f"mode must be one of {list(SANITIZE_MODES)}"}), 400
    keep_icc = bool(payload.get("keep_icc", False))
    heic_output = payload.get("heic_output", config.DEFAULT_HEIC_OUTPUT)
    if heic_output not in config.HEIC_OUTPUT_CHOICES:
        return jsonify({"error": f"heic_output must be one of {list(config.HEIC_OUTPUT_CHOICES)}"}), 400
    ids = payload.get("ids") or []
    if not isinstance(ids, list):
        return jsonify({"error": "ids must be a list"}), 400

    records = resolve_requested_records(ids, bool(payload.get("all")))
    if not records:
        return jsonify({"error": "No valid images selected for sanitization"}), 400

    results = sanitize_images(records, workspace, mode=mode, keep_icc=keep_icc, heic_output=heic_output)
    return jsonify({
        "results": [
            {"id": record.id, "filename": record.original_filename, "sanitization": result}
            for record, result in results
        ],
        "images": [record.to_summary() for record, _ in results],
    })


@app.get("/images/<image_id>/sanitized")
def download_sanitized(image_id):
    record = get_record_or_404(image_id)
    path = record.paths.get("sanitized")
    if not path or not os.path.exists(path) or not record.sanitization:
        abort(404, description="This image has not been sanitized yet")
    return send_file(
        path,
        mimetype=record.sanitization.get("output_mime", record.mime_type),
        as_attachment=True,
        download_name=record.sanitization["download_name"],
    )


@app.post("/download/zip")
def download_zip():
    if request.form.get("all") == "1":
        records = resolve_requested_records(None, True)
    else:
        ids = [i.strip() for i in request.form.get("ids", "").split(",") if i.strip()]
        records = resolve_requested_records(ids, False)
    records = [r for r in records if r.paths.get("sanitized") and r.sanitization]
    if not records:
        abort(400, description="None of the selected images has been sanitized yet")
    try:
        handle, names = build_sanitized_zip(records, workspace.directory("zips"))
    except Exception as exc:
        log.exception("ZIP creation failed")
        abort(500, description=f"ZIP creation failed: {exc}")
    if not names:
        handle.close()
        abort(400, description="No sanitized files found")
    return send_file(handle, mimetype="application/zip", as_attachment=True,
                     download_name="sanitized_images.zip")


@app.post("/api/clear")
def api_clear():
    workspace.clear()
    return jsonify({"ok": True, "image_count": 0})


if __name__ == "__main__":
    if config.HOST not in ("127.0.0.1", "localhost", "::1"):
        log.warning("Listening on %s - this tool has NO authentication!", config.HOST)
    log.info("Open http://%s:%d in your browser", config.HOST, config.PORT)
    # use_reloader=False: the reloader would start a second process with its own workspace.
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, use_reloader=False, threaded=True)
