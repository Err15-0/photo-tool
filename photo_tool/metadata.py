"""
Metadata extraction with ExifTool + normalization + sensitivity classification.

How it works
------------
1. ExifTool is run ONCE per batch of files (not once per file), as a
   subprocess with an argument list (never shell=True), with JSON output:

       exiftool -config "" -json -G0:1 -a -s <file1> <file2> ...

   -G0:1 prefixes every tag with its groups, e.g.  "EXIF:IFD0:Make"
   ( family-0 group : family-1 group : tag name ). When both groups are the
   same ExifTool prints only one, e.g. "Composite:GPSLatitude".

2. ExifTool is run a SECOND time with -n (numeric output) on the same batch.
   This gives machine-readable GPS values (signed decimal degrees) instead of
   strings like 33 deg 24' 0.00" N.

3. normalize_metadata() picks the important fields (camera, timestamps,
   GPS, software...) for the main table.

4. classify_sensitive_tags() sorts every tag into privacy categories
   (location, timestamps, device, serial numbers, ...). The SAME function is
   used on sanitized files, which is how verification works.

Everything here treats metadata as untrusted text. Nothing is executed or
interpreted; the browser renders it with textContent (never innerHTML).
"""

import json
import logging
import math
import re
import shutil
import subprocess

from . import config

log = logging.getLogger(__name__)


class ExifToolError(Exception):
    """Raised when ExifTool cannot be run at all (missing binary, timeout...)."""


# =============================================================================
# Running ExifTool
# =============================================================================

def exiftool_version():
    """Return the ExifTool version string, or None if ExifTool is not usable."""
    binary = shutil.which(config.EXIFTOOL_BINARY)
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "-ver"],
            capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ExifTool version check failed: %s", exc)
    return None


def _run_exiftool_json(paths, extra_args):
    """
    Run ExifTool on `paths` and return {source_path: metadata_dict}.

    `paths` must be server-generated absolute paths (they always start with
    "/", so they can never be mistaken for ExifTool options).
    """
    binary = shutil.which(config.EXIFTOOL_BINARY)
    if not binary:
        raise ExifToolError(
            "ExifTool is not installed or not on PATH "
            "(Debian: sudo apt install libimage-exiftool-perl)"
        )

    # "-config ''" must be the first argument: do not load any user config file.
    command = [binary, "-config", "", "-json", "-a", "-s", "-G0:1", *extra_args]
    command.extend(paths)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=config.EXIFTOOL_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExifToolError("ExifTool timed out") from exc
    except OSError as exc:
        raise ExifToolError(f"Could not start ExifTool: {exc}") from exc

    # ExifTool returns exit code 1 if *some* files failed, but still prints
    # JSON for the others, so we always try to parse stdout first.
    stdout = result.stdout.decode("utf-8", errors="replace").strip()
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    if not stdout:
        raise ExifToolError(
            f"ExifTool produced no output (exit code {result.returncode}): {stderr[:500]}"
        )
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ExifToolError(f"Could not parse ExifTool JSON output: {exc}") from exc
    if stderr:
        log.debug("ExifTool stderr: %s", stderr[:2000])

    by_path = {}
    for entry in parsed:
        if isinstance(entry, dict) and "SourceFile" in entry:
            by_path[entry["SourceFile"]] = entry
    return by_path


def read_metadata_batch(paths):
    """
    Extract metadata for many files with as few ExifTool processes as possible.

    Returns {path: {"raw": {...}, "numeric": {...}, "error": str or None}}.
    A failure for one file never affects the others.
    """
    results = {path: {"raw": {}, "numeric": {}, "error": None} for path in paths}

    for start in range(0, len(paths), config.EXIFTOOL_FILES_PER_CALL):
        chunk = paths[start:start + config.EXIFTOOL_FILES_PER_CALL]
        try:
            readable = _run_exiftool_json(chunk, [])
            numeric = _run_exiftool_json(chunk, ["-n"])
        except ExifToolError as exc:
            log.error("ExifTool failed for %d file(s): %s", len(chunk), exc)
            for path in chunk:
                results[path]["error"] = str(exc)
            continue

        for path in chunk:
            if path not in readable:
                results[path]["error"] = "ExifTool returned no data for this file"
                continue
            results[path]["raw"] = readable[path]
            results[path]["numeric"] = numeric.get(path, {})
            exiftool_error = readable[path].get("ExifTool:Error")
            if exiftool_error:
                results[path]["error"] = f"ExifTool: {exiftool_error}"
    return results


# =============================================================================
# Helpers for ExifTool keys like "EXIF:IFD0:Make"
# =============================================================================

def split_key(key):
    """
    "EXIF:IFD0:Make"         -> ("EXIF", "IFD0", "Make")
    "Composite:GPSLatitude"  -> ("Composite", "Composite", "GPSLatitude")
    "SourceFile"             -> ("", "", "SourceFile")
    """
    parts = key.split(":")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[0], parts[1]
    return "", "", key


# Tags that describe OUR temporary copy (path, permissions, file times) rather
# than the uploaded image. They are hidden because they are meaningless and
# would expose server paths.
def _is_hidden_key(key):
    family0, family1, tag = split_key(key)
    if key == "SourceFile":
        return True
    if family1 == "System":          # File:System:FileName, FileModifyDate, ...
        return True
    if family0 == "ExifTool" and tag not in ("Warning", "Error"):
        return True
    return False


def _value_to_text(value):
    """Convert any ExifTool JSON value into a display string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_value_to_text(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def clean_raw_metadata(raw):
    """
    Produce the dict shown in the "Raw metadata" panel:
    hidden keys removed, every value converted to a (possibly truncated) string.
    """
    cleaned = {}
    for key, value in raw.items():
        if _is_hidden_key(key):
            continue
        text = _value_to_text(value)
        if len(text) > config.RAW_VALUE_MAX_CHARS:
            text = (
                text[:config.RAW_VALUE_MAX_CHARS]
                + f" ... [truncated for display, {len(text)} characters total]"
            )
        cleaned[key] = text
    return cleaned


# When the same tag exists in several groups we prefer the most "authoritative".
FAMILY_PRIORITY = ["EXIF", "XMP", "IPTC", "PNG", "QuickTime", "Photoshop", "MakerNotes", "Composite"]


def _family_rank(family0):
    try:
        return FAMILY_PRIORITY.index(family0)
    except ValueError:
        return len(FAMILY_PRIORITY)


def _is_meaningful(text):
    """Reject empty strings and placeholder dates like 0000:00:00 00:00:00."""
    stripped = text.strip()
    if not stripped:
        return False
    if re.fullmatch(r"[0: ]+", stripped):
        return False
    return True


def find_tag_value(raw, tag_names, skip_families=("File", "ExifTool")):
    """
    Return the first meaningful value of any tag in `tag_names` (checked in
    order), preferring groups listed in FAMILY_PRIORITY. Returns None if absent.
    """
    for wanted in tag_names:
        candidates = []
        for key, value in raw.items():
            if _is_hidden_key(key):
                continue
            family0, _family1, tag = split_key(key)
            if tag != wanted or family0 in skip_families:
                continue
            text = _value_to_text(value)
            if _is_meaningful(text):
                candidates.append((_family_rank(family0), key, text))
        if candidates:
            candidates.sort()
            _rank, key, text = candidates[0]
            return text.strip(), key
    return None, None


# =============================================================================
# GPS
# =============================================================================

def _to_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def extract_gps(numeric):
    """
    Read GPS coordinates from the NUMERIC (-n) ExifTool output.

    Returns dict(latitude, longitude, altitude, status) where status is:
        "valid"       usable coordinates (will be shown on the map)
        "zero"        0,0 - almost always a bogus placeholder, not plotted
        "invalid"     out of range / unparseable
        "incomplete"  some GPS tags exist but no full coordinate pair
        None          no GPS tags at all
    """
    def pick(*keys):
        for key in keys:
            if key in numeric:
                return numeric[key]
        return None

    lat = _to_float(pick("Composite:GPSLatitude"))
    lon = _to_float(pick("Composite:GPSLongitude"))

    # Fallback 1: XMP stores signed decimal degrees directly.
    if lat is None or lon is None:
        for key, value in numeric.items():
            family0, _f1, tag = split_key(key)
            if family0 == "XMP" and tag == "GPSLatitude" and lat is None:
                lat = _to_float(value)
            if family0 == "XMP" and tag == "GPSLongitude" and lon is None:
                lon = _to_float(value)

    # Fallback 2: raw EXIF GPS values + N/S/E/W reference tags.
    if lat is None or lon is None:
        exif_lat = _to_float(pick("EXIF:GPS:GPSLatitude"))
        exif_lon = _to_float(pick("EXIF:GPS:GPSLongitude"))
        if exif_lat is not None and exif_lon is not None:
            if str(pick("EXIF:GPS:GPSLatitudeRef") or "N").upper().startswith("S"):
                exif_lat = -abs(exif_lat)
            if str(pick("EXIF:GPS:GPSLongitudeRef") or "E").upper().startswith("W"):
                exif_lon = -abs(exif_lon)
            lat, lon = exif_lat, exif_lon

    altitude = _to_float(pick("Composite:GPSAltitude"))
    if altitude is None:
        altitude = _to_float(pick("EXIF:GPS:GPSAltitude"))
        if altitude is not None and str(pick("EXIF:GPS:GPSAltitudeRef")) == "1":
            altitude = -altitude

    has_gps_tags = any(
        split_key(key)[1] == "GPS" or split_key(key)[2].startswith("GPS")
        for key in numeric
    )

    if lat is None or lon is None:
        status = "incomplete" if has_gps_tags else None
        return {"latitude": None, "longitude": None, "altitude": altitude, "status": status}
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return {"latitude": None, "longitude": None, "altitude": altitude, "status": "invalid"}
    if lat == 0.0 and lon == 0.0:
        return {"latitude": lat, "longitude": lon, "altitude": altitude, "status": "zero"}
    return {
        "latitude": round(lat, 7),
        "longitude": round(lon, 7),
        "altitude": None if altitude is None else round(altitude, 2),
        "status": "valid",
    }


# =============================================================================
# Normalization: the important fields for the main table
# =============================================================================

TIMESTAMP_PATTERN = re.compile(
    r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?"
)


def timestamp_to_sortable(text):
    """'2023:05:01 12:34:56+02:00' -> '2023-05-01T12:34:56' (None if unparseable)."""
    if not text:
        return None
    match = TIMESTAMP_PATTERN.match(text.strip())
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    if year == "0000":
        return None
    return f"{year}-{month}-{day}T{hour}:{minute}:{second or '00'}"


def _combine_make_model(make, model):
    """'Apple' + 'iPhone 12' -> 'Apple iPhone 12'; 'NIKON CORPORATION' + 'NIKON D750' -> 'NIKON D750'."""
    if make and model:
        first_word = make.split()[0].lower()
        if model.lower().startswith(first_word):
            return model
        return f"{make} {model}"
    return model or make or None


def normalize_metadata(raw, numeric):
    """
    Build the normalized dict used by the table/gallery/map.
    Missing values are None (the UI shows "-"). Nothing is invented.
    """
    def value(*names):
        return find_tag_value(raw, names)[0]

    datetime_original, dto_key = find_tag_value(raw, ["DateTimeOriginal"])
    create_date, _ = find_tag_value(raw, ["CreateDate", "DateTimeDigitized", "DateCreated", "CreationTime"])
    modify_date, _ = find_tag_value(raw, ["ModifyDate", "DateTime", "MetadataDate"])
    gps_datetime, _ = find_tag_value(raw, ["GPSDateTime"], skip_families=("File", "ExifTool"))

    # Best timestamp for sorting / timeline: original capture time first.
    best_timestamp, timestamp_source = None, None
    for label, candidate in (
        ("DateTimeOriginal", datetime_original),
        ("CreateDate", create_date),
        ("ModifyDate", modify_date),
        ("GPSDateTime", gps_datetime),
    ):
        if candidate:
            best_timestamp, timestamp_source = candidate, label
            break

    make = value("Make")
    model = value("Model", "CameraModelName", "UniqueCameraModel")
    gps = extract_gps(numeric)

    normalized = {
        "file_type": value_from_file_group(raw, "FileType"),
        "mime_type": value_from_file_group(raw, "MIMEType"),
        "datetime_original": datetime_original,
        "create_date": create_date,
        "modify_date": modify_date,
        "gps_datetime": gps_datetime,
        "timestamp": best_timestamp,
        "timestamp_source": timestamp_source,
        "timestamp_sortable": timestamp_to_sortable(best_timestamp),
        "timezone_offset": value("OffsetTimeOriginal", "OffsetTime", "TimeZone"),
        "make": make,
        "model": model,
        "device": _combine_make_model(make, model),
        "lens": value("LensModel", "LensID", "Lens", "LensInfo"),
        "serial_number": value("SerialNumber", "BodySerialNumber", "CameraSerialNumber", "InternalSerialNumber"),
        "software": value("Software", "CreatorTool", "ProcessingSoftware", "HistorySoftwareAgent"),
        "host_computer": value("HostComputer"),
        "artist": value("Artist", "Creator", "Author", "By-line", "XPAuthor", "OwnerName"),
        "copyright": value("Copyright", "Rights", "CopyrightNotice"),
        "description": value("ImageDescription", "Description", "Caption-Abstract", "UserComment", "Comment", "XPComment"),
        "orientation": value("Orientation"),
        "gps_latitude": gps["latitude"],
        "gps_longitude": gps["longitude"],
        "gps_altitude": gps["altitude"],
        "gps_status": gps["status"],
        "date_time_original_key": dto_key,
    }
    return normalized


def value_from_file_group(raw, tag):
    """Read a tag from ExifTool's 'File' group (FileType, MIMEType...)."""
    text = raw.get(f"File:{tag}")
    return _value_to_text(text) if text is not None else None


# =============================================================================
# Sensitivity classification (used for analysis AND for verification)
# =============================================================================

# category id -> human readable label (order = display order)
SENSITIVE_CATEGORIES = {
    "location": "GPS / location",
    "timestamp": "Timestamps",
    "device": "Camera / device",
    "identifier": "Serial numbers / unique IDs",
    "software": "Software / editing history",
    "author": "Author / owner / copyright",
    "people": "People / face regions",
    "text": "Descriptions / comments / keywords",
    "embedded": "Embedded thumbnails / previews",
    "maker_notes": "Maker notes (vendor-specific data)",
}

# Groups that never hold privacy-relevant data about the photo itself:
#   File       = facts ExifTool derives from the file structure (type, size...)
#   Composite  = values ExifTool computes from other tags (would double count)
#   ICC_Profile= colour profile; describes the profile, not the photo
#   JFIF       = only pixel density
SKIPPED_FAMILIES = {"File", "Composite", "ExifTool", "ICC_Profile", "JFIF"}

_RE_LOCATION = re.compile(
    r"^GPS|City$|^City|Country|Sub-?location|Province|^State$|^Location|^Sublocation|WorldRegion",
    re.IGNORECASE,
)
_RE_IDENTIFIER = re.compile(
    r"Serial|UniqueID|ImageUniqueID|OwnerName|DocumentID|InstanceID|CameraID|BodyID|DeviceID",
    re.IGNORECASE,
)
_TIMESTAMP_TAGS = {
    "TimeCreated", "DigitalCreationTime", "CreationTime", "SubSecTime", "SubSecTimeOriginal",
    "SubSecTimeDigitized", "OffsetTime", "OffsetTimeOriginal", "OffsetTimeDigitized",
    "TimeZone", "TimeZoneOffset", "DateTime",
}
_DEVICE_TAGS = {
    "Make", "Model", "LensMake", "LensModel", "Lens", "LensID", "LensInfo", "LensSpec",
    "UniqueCameraModel", "LocalizedCameraModel", "CameraModelName", "CameraLabel",
}
_SOFTWARE_TAGS = {
    "Software", "CreatorTool", "ProcessingSoftware", "HostComputer", "XMPToolkit",
    "FirmwareVersion", "Firmware", "OriginatingProgram", "ProgramVersion",
}
_AUTHOR_RE = re.compile(
    r"^(Artist|Creator|Author|By-line|XPAuthor|Copyright|Rights|CopyrightNotice|Credit|"
    r"Writer-Editor|CaptionWriter|Contact|Owner|Publisher|Marked|UsageTerms|WebStatement)",
    re.IGNORECASE,
)
_PEOPLE_RE = re.compile(r"^Region|PersonInImage|^FaceName|^Faces?Detected", re.IGNORECASE)
_TEXT_TAGS = {
    "ImageDescription", "UserComment", "XPComment", "XPTitle", "XPSubject", "XPKeywords",
    "Comment", "Caption-Abstract", "Description", "Title", "Keywords", "Subject",
    "Headline", "ObjectName", "Label", "Instructions", "SpecialInstructions",
    "Parameters",  # Stable Diffusion / AI generator prompts stored in PNG text chunks
    "Prompt", "Workflow",
}
_EMBEDDED_RE = re.compile(r"ThumbnailImage|PreviewImage|JpgFromRaw|OtherImage|^MPImage\d|ThumbnailTIFF")


def classify_tag(key):
    """Return the sensitive category of one ExifTool key, or None."""
    if _is_hidden_key(key):
        return None
    family0, family1, tag = split_key(key)
    if family0 in SKIPPED_FAMILIES:
        return None

    if family0 == "MakerNotes":
        # Vendor maker notes: flag serial numbers explicitly, everything else
        # is summarised as "maker notes".
        if _RE_IDENTIFIER.search(tag):
            return "identifier"
        return "maker_notes"

    if _EMBEDDED_RE.search(tag):
        return "embedded"
    if family1 == "GPS" or _RE_LOCATION.search(tag):
        return "location"
    if _RE_IDENTIFIER.search(tag):
        return "identifier"
    if tag in _DEVICE_TAGS:
        return "device"
    if tag in _SOFTWARE_TAGS or tag.startswith("History"):
        return "software"
    if _PEOPLE_RE.search(tag):
        return "people"
    if "Date" in tag or tag in _TIMESTAMP_TAGS:
        return "timestamp"
    if _AUTHOR_RE.search(tag):
        return "author"
    if tag in _TEXT_TAGS:
        return "text"
    return None


def classify_sensitive_tags(raw):
    """
    Group every privacy-relevant tag by category.
    Returns {category: [{"key": "EXIF:IFD0:Make", "value": "Canon"}, ...]}
    Empty categories are omitted.
    """
    result = {}
    for key, value in raw.items():
        category = classify_tag(key)
        if category is None:
            continue
        text = _value_to_text(value)
        if len(text) > 300:
            text = text[:300] + " ..."
        result.setdefault(category, []).append({"key": key, "value": text})
    return result


def compute_flags(normalized, sensitive):
    """Booleans used for thumbnail badges, filters and statistics."""
    return {
        "gps": normalized.get("gps_status") == "valid",
        "location_tags": "location" in sensitive,
        "timestamp": bool(normalized.get("timestamp")) or "timestamp" in sensitive,
        "device": bool(normalized.get("device")) or "device" in sensitive,
        "software": bool(normalized.get("software")) or "software" in sensitive,
        "author": bool(normalized.get("artist") or normalized.get("copyright")) or "author" in sensitive,
        "identifier": "identifier" in sensitive,
        "people": "people" in sensitive,
        "embedded": "embedded" in sensitive,
    }


def analyze_metadata(raw, numeric):
    """
    Convenience wrapper: everything we derive from one file's ExifTool output.
    Returns dict(raw, normalized, sensitive, flags, tag_count, warnings).
    """
    cleaned = clean_raw_metadata(raw)
    normalized = normalize_metadata(raw, numeric)
    sensitive = classify_sensitive_tags(raw)
    flags = compute_flags(normalized, sensitive)
    warnings = []
    for key in ("ExifTool:Warning",):
        if key in raw:
            warnings.append(f"ExifTool warning: {_value_to_text(raw[key])}")
    if normalized["gps_status"] == "zero":
        warnings.append("GPS coordinates are 0,0 (usually a placeholder) - not plotted on the map")
    elif normalized["gps_status"] == "invalid":
        warnings.append("GPS coordinates are out of range - not plotted on the map")
    elif normalized["gps_status"] == "incomplete":
        warnings.append("Some GPS tags exist but no complete latitude/longitude pair")
    return {
        "raw": cleaned,
        "normalized": normalized,
        "sensitive": sensitive,
        "flags": flags,
        "tag_count": len(cleaned),
        "warnings": warnings,
    }
