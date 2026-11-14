"""
Metadata sanitization + independent before/after verification.

Two sanitization modes
----------------------
"reencode"  (default, recommended - maximum removal)
    Pillow decodes the original to raw pixels, applies the EXIF rotation to
    the pixels, and writes a BRAND NEW file containing only pixel data.
    Nothing from the original container (EXIF, XMP, IPTC, maker notes,
    embedded thumbnails, MPF extra images, PNG text chunks, comments,
    trailers...) can survive, because none of it is copied.
    Trade-off: JPEG and WebP are re-compressed (quality 95 - visually
    identical for normal use, but not bit-identical pixels).
    PNG and TIFF are lossless, so pixels are preserved exactly.
    HEIC (iPhone) is converted to JPEG by default (heic_output="jpeg") because
    many websites/programs cannot open HEIC; heic_output="heic" keeps HEIC.

"lossless"
    ExifTool deletes all metadata it can write ("-all=") WITHOUT
    re-compressing pixel data. Pixels stay bit-identical. Only the
    Orientation tag is copied back so the photo is not shown rotated.
    Some structural tags required by the file format (especially TIFF)
    cannot be removed; verification will list them.

Verification (both modes)
-------------------------
The sanitized file is analysed AGAIN with ExifTool (a separate run, nothing
is assumed), classified with the same sensitivity rules used for the
originals, and compared field-by-field and category-by-category with the
original. The result is "verified_clean" ONLY if ExifTool finds zero tags in
any sensitive category in the sanitized output.
"""

import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone

from PIL import Image, ImageOps, JpegImagePlugin

from . import config
from . import heif_support  # noqa: F401  (registers the HEIC/HEIF Pillow plugin)
from .downloads import sanitized_download_name
from .metadata import (
    SENSITIVE_CATEGORIES,
    analyze_metadata,
    classify_tag,
    read_metadata_batch,
)

log = logging.getLogger(__name__)

SANITIZE_MODES = ("reencode", "lossless")

# TIFF compressions Pillow can write; anything else falls back to LZW.
_TIFF_WRITABLE_COMPRESSIONS = {"raw", "tiff_lzw", "tiff_adobe_deflate", "tiff_deflate", "packbits"}


# IFD0 tags that hold sensitive text and must be deleted explicitly in TIFF files.
_TIFF_IFD0_TEXT_TAGS = (
    "Make", "Model", "Software", "Artist", "ModifyDate", "Copyright", "HostComputer",
    "ImageDescription", "DocumentName", "PageName", "XPAuthor", "XPComment", "XPTitle",
    "XPSubject", "XPKeywords", "UniqueCameraModel", "LocalizedCameraModel", "CameraSerialNumber",
)


class SanitizeError(Exception):
    pass


# =============================================================================
# Step 1: write the sanitized file
# =============================================================================

def _flatten_for_jpeg(image):
    """JPEG has no transparency: composite RGBA/LA/P images onto white."""
    if image.mode in ("RGB", "L", "CMYK"):
        return image
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return image.convert("RGB")


def choose_output_format(record, mode, heic_output):
    """Which Pillow format the sanitized copy will have."""
    if record.image_format == "HEIF" and mode == "reencode" and heic_output == "jpeg":
        return "JPEG"
    if record.image_format == "MPO":
        return "JPEG"
    return record.image_format


def _sanitize_by_reencoding(original_path, output_path, image_format, output_format, keep_icc):
    """Rebuild the image from pixels only. Returns a list of notes."""
    notes = []
    with Image.open(original_path) as image:
        subsampling = -1
        if image_format in ("JPEG", "MPO"):
            subsampling = JpegImagePlugin.get_sampling(image)
        image.load()
        icc_profile = image.info.get("icc_profile")
        transparency = image.info.get("transparency")
        tiff_compression = image.info.get("compression")

        orientation = image.getexif().get(0x0112, 1)
        oriented = ImageOps.exif_transpose(image)   # rotation baked into pixels
        if orientation not in (1, None):
            notes.append("EXIF orientation was applied to the pixels, so the Orientation tag is no longer needed")

    # Copy pixels into a fresh image object and drop every piece of attached info.
    clean = oriented.copy()
    clean.info = {}

    save_options = {}
    if keep_icc and icc_profile:
        save_options["icc_profile"] = icc_profile
        notes.append("ICC colour profile kept (option enabled)")

    if output_format == "JPEG":
        clean = _flatten_for_jpeg(clean)
        save_options.update(quality=config.JPEG_SANITIZE_QUALITY, optimize=True)
        if subsampling in (0, 1, 2):
            save_options["subsampling"] = subsampling
        clean.save(output_path, "JPEG", **save_options)
        if image_format == "HEIF":
            notes.append(f"HEIC converted to JPEG (quality {config.JPEG_SANITIZE_QUALITY}) for maximum compatibility")
            notes.append("HEIC extras (depth map, auxiliary images, embedded thumbnails, Live Photo link) were dropped")
        else:
            notes.append(f"JPEG re-encoded at quality {config.JPEG_SANITIZE_QUALITY}")
        if image_format == "MPO":
            notes.append("MPO extra embedded pictures were dropped (only the main image is kept)")
    elif output_format == "HEIF":
        save_options.update(quality=config.HEIF_SANITIZE_QUALITY)
        clean.save(output_path, "HEIF", **save_options)
        notes.append(f"HEIC re-encoded as HEIC (quality {config.HEIF_SANITIZE_QUALITY})")
        notes.append("HEIC extras (depth map, auxiliary images, embedded thumbnails, Live Photo link) were dropped")
    elif image_format == "PNG":
        if transparency is not None:
            save_options["transparency"] = transparency
        clean.save(output_path, "PNG", **save_options)
        notes.append("PNG re-written losslessly (pixels identical)")
    elif image_format == "WEBP":
        save_options.update(quality=config.WEBP_SANITIZE_QUALITY, method=4)
        clean.save(output_path, "WEBP", **save_options)
        notes.append(f"WebP re-encoded at quality {config.WEBP_SANITIZE_QUALITY}")
    elif image_format == "TIFF":
        compression = tiff_compression if tiff_compression in _TIFF_WRITABLE_COMPRESSIONS else "tiff_lzw"
        clean.save(output_path, "TIFF", compression=compression, **save_options)
        notes.append(f"TIFF re-written losslessly (compression: {compression})")
    else:
        raise SanitizeError(f"Unsupported format {image_format}")
    return notes


def _sanitize_with_exiftool(original_path, output_path, keep_icc):
    """Strip metadata without touching pixel data. Returns a list of notes."""
    binary = shutil.which(config.EXIFTOOL_BINARY)
    if not binary:
        raise SanitizeError("ExifTool is not installed; lossless mode needs it")
    command = [
        binary, "-config", "", "-m", "-q", "-q",
        "-all=",                 # delete every writable tag
        "-trailer:all=",         # delete data appended after the image (e.g. vendor trailers)
        # In TIFF files IFD0 *is* the image structure, so "-all=" leaves these
        # IFD0 text tags in place. Delete them explicitly (no-op for JPEG etc.).
        *[f"-IFD0:{tag}=" for tag in _TIFF_IFD0_TEXT_TAGS],
        "-tagsFromFile", "@",    # ...then copy back ONLY the following from the original:
        "-EXIF:Orientation",     # keep display rotation (not sensitive)
    ]
    if keep_icc:
        command.append("-ICC_Profile")
    command += ["-o", output_path, original_path]

    try:
        result = subprocess.run(
            command, capture_output=True, timeout=config.EXIFTOOL_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SanitizeError(f"ExifTool could not be run: {exc}")
    if not os.path.exists(output_path):
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise SanitizeError(f"ExifTool did not create the sanitized file: {stderr[:300]}")
    notes = ["Metadata removed with ExifTool without re-compressing pixels (bit-identical image data)",
             "Orientation tag copied back (if present) so the image displays correctly"]
    if keep_icc:
        notes.append("ICC colour profile kept (option enabled)")
    return notes


def _write_sanitized_file(record, workspace, mode, keep_icc, heic_output):
    """
    Create the sanitized copy.
    Returns dict(path, notes, mode, format, extension, mime).
    """
    notes = []
    effective_mode = mode
    if mode == "reencode" and record.frame_count > 1:
        # Re-encoding would keep only the first frame of an animation / multi-page file.
        effective_mode = "lossless"
        notes.append(f"Image has {record.frame_count} frames/pages: switched to lossless mode "
                     "so no frames are lost")
    if record.image_format == "HEIF" and effective_mode == "lossless" and heic_output == "jpeg":
        notes.append("Lossless mode cannot convert formats: the output stays HEIC")

    output_format = choose_output_format(record, effective_mode, heic_output)
    extension, mime_type = config.FORMAT_INFO[output_format]
    output_path = workspace.path_for("sanitized", record.id, extension)
    original_path = record.paths["original"]
    if os.path.abspath(output_path) == os.path.abspath(original_path):
        raise SanitizeError("refusing to overwrite the original")   # defensive, cannot happen

    # Replace a previous sanitized copy (never the original). Its extension may
    # differ (e.g. HEIC -> JPEG last time, HEIC -> HEIC now).
    for old_path in {record.paths.get("sanitized"), output_path}:
        if old_path and os.path.exists(old_path) and old_path != original_path:
            os.remove(old_path)
    record.paths.pop("sanitized", None)

    if effective_mode == "reencode":
        notes += _sanitize_by_reencoding(original_path, output_path, record.image_format, output_format, keep_icc)
    else:
        notes += _sanitize_with_exiftool(original_path, output_path, keep_icc)
        if record.image_format == "HEIF":
            notes.append("HEIC rotation is stored in the container (irot box) and is preserved")
    os.chmod(output_path, 0o600)
    return {"path": output_path, "notes": notes, "mode": effective_mode,
            "format": output_format, "extension": extension, "mime": mime_type}


# =============================================================================
# Step 2: verification / before-after comparison
# =============================================================================

# (label, function(normalized) -> display value or None)
def _gps_text(n):
    if n.get("gps_latitude") is not None and n.get("gps_longitude") is not None:
        return f"{n['gps_latitude']}, {n['gps_longitude']}"
    return None


COMPARISON_FIELDS = [
    ("GPS coordinates", _gps_text),
    ("GPS altitude", lambda n: None if n.get("gps_altitude") is None else f"{n['gps_altitude']} m"),
    ("Date/time original", lambda n: n.get("datetime_original")),
    ("Create date", lambda n: n.get("create_date")),
    ("Modify date", lambda n: n.get("modify_date")),
    ("GPS date/time", lambda n: n.get("gps_datetime")),
    ("Timezone offset", lambda n: n.get("timezone_offset")),
    ("Camera / device", lambda n: n.get("device")),
    ("Lens", lambda n: n.get("lens")),
    ("Serial number", lambda n: n.get("serial_number")),
    ("Software / editor", lambda n: n.get("software")),
    ("Host computer", lambda n: n.get("host_computer")),
    ("Author / artist", lambda n: n.get("artist")),
    ("Copyright", lambda n: n.get("copyright")),
    ("Description / comment", lambda n: n.get("description")),
    ("Orientation", lambda n: n.get("orientation")),
]


def _compare(before_analysis, after_analysis, mode):
    """Build the field and category comparison tables."""
    before_n, after_n = before_analysis["normalized"], after_analysis["normalized"]
    field_rows = []
    for label, getter in COMPARISON_FIELDS:
        before, after = getter(before_n), getter(after_n)
        if before is None and after is None:
            status = "not_present"
        elif after is None:
            status = "removed"
        elif label == "Orientation" and mode == "lossless":
            status = "kept_intentionally"
        else:
            status = "still_present"
        field_rows.append({"field": label, "before": before, "after": after, "status": status})

    category_rows = []
    for category, label in SENSITIVE_CATEGORIES.items():
        before_count = len(before_analysis["sensitive"].get(category, []))
        after_count = len(after_analysis["sensitive"].get(category, []))
        if before_count == 0 and after_count == 0:
            status = "not_present"
        elif after_count == 0:
            status = "removed"
        else:
            status = "still_present"
        category_rows.append({
            "category": category, "label": label,
            "before_count": before_count, "after_count": after_count, "status": status,
        })
    return field_rows, category_rows


def _check_output_image(path, expected_size):
    """Open + fully decode the sanitized file, and compare its pixel size."""
    try:
        with Image.open(path) as image:
            image.load()
            size = image.size
    except Exception as exc:
        return {"decodes": False, "size_ok": False, "message": f"Sanitized file does not decode: {exc}"}
    size_ok = tuple(size) == tuple(expected_size)
    return {
        "decodes": True,
        "size_ok": size_ok,
        "width": size[0],
        "height": size[1],
        "message": "Image decodes and dimensions match" if size_ok
        else f"Dimensions changed: expected {expected_size[0]}x{expected_size[1]}, got {size[0]}x{size[1]}",
    }


def _build_verification(record, output_path, mode, keep_icc, notes, metadata_result):
    """Combine everything we learned about one sanitized file into a result dict."""
    if metadata_result["error"]:
        # Without a successful ExifTool run we CANNOT claim anything was removed.
        return {
            "status": "unverified",
            "verified_clean": False,
            "error": f"Verification failed - could not re-read sanitized file: {metadata_result['error']}",
        }

    after = analyze_metadata(metadata_result["raw"], metadata_result["numeric"])
    before = {"normalized": record.normalized, "sensitive": record.sensitive}
    field_rows, category_rows = _compare(before, after, mode)

    remaining_sensitive = after["sensitive"]
    # In lossless mode we deliberately keep Orientation; it is not sensitive and
    # is not matched by any category, so it does not affect verified_clean.
    remaining_sensitive_count = sum(len(tags) for tags in remaining_sensitive.values())

    before_keys = set(record.raw.keys())
    after_keys = set(after["raw"].keys())
    remaining_tags = [
        {"key": key, "value": value, "category": classify_tag(key)}
        for key, value in after["raw"].items()
    ]

    expected_size = (
        (record.display_width, record.display_height) if mode == "reencode"
        else (record.width, record.height)
    )
    image_check = _check_output_image(output_path, expected_size)

    verified_clean = remaining_sensitive_count == 0 and image_check["decodes"]
    return {
        "status": "ok" if verified_clean else "warning",
        "verified_clean": verified_clean,
        "error": None,
        "notes": notes,
        "field_comparison": field_rows,
        "category_comparison": category_rows,
        "remaining_sensitive": remaining_sensitive,
        "remaining_sensitive_count": remaining_sensitive_count,
        "removed_tags": sorted(before_keys - after_keys),
        "remaining_tags": remaining_tags,
        "before_tag_count": len(before_keys),
        "after_tag_count": len(after_keys),
        "image_check": image_check,
        "file_size": os.path.getsize(output_path),
    }


# =============================================================================
# Public entry point
# =============================================================================

def sanitize_images(records, workspace, mode="reencode", keep_icc=False,
                    heic_output=config.DEFAULT_HEIC_OUTPUT):
    """
    Sanitize several images. Each record gets record.sanitization filled in.
    Verification of all outputs uses ONE batched ExifTool run.
    Returns a list of (record, sanitization_dict).
    """
    if mode not in SANITIZE_MODES:
        raise ValueError(f"mode must be one of {SANITIZE_MODES}")
    if heic_output not in config.HEIC_OUTPUT_CHOICES:
        raise ValueError(f"heic_output must be one of {config.HEIC_OUTPUT_CHOICES}")

    written = []      # (record, output dict from _write_sanitized_file)
    results = []
    for record in records:
        try:
            written.append((record, _write_sanitized_file(record, workspace, mode, keep_icc, heic_output)))
        except Exception as exc:
            log.exception("Sanitization failed for %s", record.id)
            record.sanitization = {
                "status": "error", "verified_clean": False, "mode": mode,
                "error": f"Sanitization failed: {exc}",
                "sanitized_at": _now(),
            }
            record.paths.pop("sanitized", None)
            results.append((record, record.sanitization))

    metadata_by_path = read_metadata_batch([output["path"] for _r, output in written])

    for record, output in written:
        output_path, effective_mode = output["path"], output["mode"]
        verification = _build_verification(
            record, output_path, effective_mode, keep_icc, output["notes"], metadata_by_path[output_path]
        )
        verification.update({
            "mode": effective_mode,
            "requested_mode": mode,
            "keep_icc": keep_icc,
            "heic_output": heic_output,
            "sanitized_at": _now(),
            "output_format": output["format"],
            "output_mime": output["mime"],
            "download_name": sanitized_download_name(record.original_filename, output["extension"]),
        })
        record.sanitization = verification
        record.paths["sanitized"] = output_path
        log.info("Sanitized %s (%s): status=%s remaining_sensitive=%s",
                 record.id, effective_mode, verification["status"],
                 verification.get("remaining_sensitive_count"))
        results.append((record, verification))
    return results


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
