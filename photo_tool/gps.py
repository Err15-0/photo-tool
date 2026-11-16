"""
GPS interpretation with explicit confidence states.

Principle: PRESERVE UNCERTAINTY RATHER THAN INVENT CERTAINTY.
A confidently wrong map marker is worse than no marker.

Why this module exists (verified against ExifTool 12.76)
--------------------------------------------------------
EXIF stores GPS latitude/longitude as UNSIGNED values; the hemisphere lives in
separate tags: GPSLatitudeRef (N/S) and GPSLongitudeRef (E/W). ExifTool's own
output has several traps when those references are missing or malformed:

  * Composite:GPSLatitude / GPSLongitude (signed) are only generated when the
    matching Ref tag exists - good - BUT a malformed Ref (e.g. "X") still
    produces a POSITIVE composite value and the readable output even prints "N".
  * Composite:GPSPosition is generated even when BOTH Ref tags are missing and
    simply shows the unsigned values (an implicit N/E).
  * XMP exif:GPSLatitude must end with a hemisphere letter ("22,54.408S"). If the
    letter is missing, ExifTool returns a positive number and its readable output
    invents "N"/"E". Only the raw XMP packet shows the truth.

So this module never trusts a sign it cannot trace back to an explicit
hemisphere reference (or an explicit +/- sign in the raw XMP value).
Nothing else (timezone, locale, filename, ...) is ever used to guess.

Status values returned by interpret_gps():
    "valid"       hemisphere known, coordinates in range -> plotted on the map
    "zero"        resolved to 0,0 - almost always a placeholder, not plotted
    "ambiguous"   latitude/longitude values exist but the hemisphere of at least
                  one axis cannot be determined reliably -> NOT plotted, but the
                  raw values are shown to the investigator
    "invalid"     hemisphere known but values out of range -> not plotted
    "incomplete"  GPS tags exist but no latitude/longitude pair -> not plotted
    None          no GPS metadata at all
"""

import math
import re

LAT_REFS = {"N": 1, "NORTH": 1, "S": -1, "SOUTH": -1}
LON_REFS = {"E": 1, "EAST": 1, "W": -1, "WEST": -1}

# Two coordinate records are "the same place" if they differ by less than this
# (degrees, roughly 1 m).
SAME_POSITION_TOLERANCE = 1e-5

STATUS_LABELS = {
    "valid": "Resolved",
    "zero": "0,0 placeholder (not plotted)",
    "ambiguous": "Ambiguous – hemisphere unknown (not plotted)",
    "invalid": "Out of range (not plotted)",
    "incomplete": "Incomplete – no latitude/longitude pair (not plotted)",
    None: "No GPS metadata",
}


def _to_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _split_key(key):
    """'EXIF:GPS:GPSLatitude' -> ('EXIF', 'GPS', 'GPSLatitude')."""
    parts = key.split(":")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[0], parts[1]
    return "", "", key


def _find(numeric, family0, tag, family1=None):
    """First value whose key matches family-0 group (+ optional family-1) and tag."""
    for key, value in numeric.items():
        f0, f1, name = _split_key(key)
        if f0 == family0 and name == tag and (family1 is None or f1 == family1):
            return value
    return None


# =============================================================================
# Hemisphere references
# =============================================================================

def parse_reference(value, table):
    """
    Interpret a GPSLatitudeRef / GPSLongitudeRef value.
    Returns (letter, problem): letter is "N"/"S"/"E"/"W" or None;
    problem is None, "missing" or "invalid". Nothing is ever defaulted.
    """
    if value is None:
        return None, "missing"
    text = str(value).strip().upper()
    if not text:
        return None, "missing"
    if text in table:
        return text[0], None
    return None, "invalid"


def _resolve_axis(raw_value, ref_value, table, ref_name):
    """
    Combine one unsigned coordinate with its reference.
    Returns None if there is no coordinate value at all.
    """
    value = _to_float(raw_value)
    if value is None:
        return None
    letter, problem = parse_reference(ref_value, table)
    axis = {
        "raw": value,
        "ref": letter,
        "ref_raw": None if ref_value is None else str(ref_value),
        "ref_name": ref_name,
        "problem": problem,
        "signed": None,
    }
    if problem is None:
        axis["signed"] = table[letter] * abs(value)
    elif value == 0.0:
        # 0 degrees is the equator / prime meridian: no hemisphere needed.
        axis["signed"] = 0.0
        axis["problem"] = None
    return axis


# =============================================================================
# XMP: hemisphere letter lives inside the value ("22,54.408S")
# =============================================================================

_XMP_COORD_RE = re.compile(
    r"[\w.-]+:(GPSLatitude|GPSLongitude)\s*(?:=\s*[\"']([^\"']*)[\"']|>\s*([^<]*?)\s*<)"
)


def xmp_coordinate_strings(packet):
    """Raw exif:GPSLatitude / exif:GPSLongitude strings from an XMP packet."""
    found = {}
    if not packet:
        return found
    for match in _XMP_COORD_RE.finditer(packet):
        name = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        found.setdefault(name, value.strip())
    return found


def _xmp_reference(raw_string, table):
    """
    Hemisphere of a raw XMP coordinate string.
    "22,54.408S" -> ("S", None); "22,54.408" -> (None, "missing");
    an explicit leading sign ("-22.9") is accepted as unambiguous.
    """
    if raw_string is None:
        return None, "missing"
    text = raw_string.strip().upper()
    if not text:
        return None, "missing"
    letters = {key[0] for key in table}
    if text[-1] in letters:
        return text[-1], None
    if text[-1].isalpha():
        return None, "invalid"
    if text[0] == "-":
        return [k for k, v in table.items() if v == -1][0][0], None
    if text[0] == "+":
        return [k for k, v in table.items() if v == 1][0][0], None
    return None, "missing"


def _xmp_record(numeric, xmp_packet):
    lat_value = _find(numeric, "XMP", "GPSLatitude")
    lon_value = _find(numeric, "XMP", "GPSLongitude")
    if lat_value is None and lon_value is None:
        return None
    strings = xmp_coordinate_strings(xmp_packet)
    record = {"source": "XMP (exif:GPSLatitude / exif:GPSLongitude)", "kind": "xmp", "notes": []}
    if xmp_packet is None:
        record["notes"].append("Raw XMP packet unavailable: hemisphere letters could not be verified")
    for axis_name, value, table, tag in (
        ("lat", lat_value, LAT_REFS, "GPSLatitude"),
        ("lon", lon_value, LON_REFS, "GPSLongitude"),
    ):
        number = _to_float(value)
        if number is None:
            record[axis_name] = None
            continue
        raw_string = strings.get(tag)
        letter, problem = _xmp_reference(raw_string, table)
        axis = {
            "raw": abs(number),
            "ref": letter,
            "ref_raw": raw_string,
            "ref_name": f"hemisphere letter in XMP exif:{tag}",
            "problem": problem,
            "signed": None,
        }
        if problem is None:
            axis["signed"] = table[letter] * abs(number)
        elif number == 0.0:
            axis["signed"] = 0.0
            axis["problem"] = None
        record[axis_name] = axis
    return record


def _exif_record(numeric):
    lat_value = _find(numeric, "EXIF", "GPSLatitude", "GPS")
    lon_value = _find(numeric, "EXIF", "GPSLongitude", "GPS")
    if lat_value is None and lon_value is None:
        return None
    record = {"source": "EXIF GPS", "kind": "exif", "notes": []}
    record["lat"] = _resolve_axis(lat_value, _find(numeric, "EXIF", "GPSLatitudeRef", "GPS"), LAT_REFS, "GPSLatitudeRef")
    record["lon"] = _resolve_axis(lon_value, _find(numeric, "EXIF", "GPSLongitudeRef", "GPS"), LON_REFS, "GPSLongitudeRef")

    # Prefer ExifTool's own signed Composite value - but ONLY after we confirmed
    # the reference is explicit and valid (ExifTool turns a malformed Ref into +).
    used_composite = []
    for axis_name, composite_key in (("lat", "Composite:GPSLatitude"), ("lon", "Composite:GPSLongitude")):
        axis = record[axis_name]
        composite = _to_float(numeric.get(composite_key))
        if axis is None or axis["problem"] is not None or axis["ref"] is None or composite is None:
            continue
        if abs(composite - axis["signed"]) <= 1e-9:
            axis["signed"] = composite
            used_composite.append(axis_name)
        else:
            record["notes"].append(
                f"ExifTool {composite_key} ({composite}) disagrees with the explicit reference; "
                f"using the reference ({axis['signed']})"
            )
    unusable = [a["ref_name"] for a in (record["lat"], record["lon"]) if a is not None and a["problem"]]
    if unusable:
        record["source"] = f"EXIF GPS ({' and '.join(unusable)} missing or unusable)"
    elif used_composite:
        record["source"] = "EXIF GPS (ExifTool signed Composite, reference tags verified)"
    else:
        record["source"] = "EXIF GPS + reference tags"
    return record


def _record_state(record):
    if record is None:
        return None
    if record.get("lat") is None or record.get("lon") is None:
        return "incomplete"
    if record["lat"]["problem"] or record["lon"]["problem"]:
        return "ambiguous"
    return "resolved"


def _same_position(a, b):
    return (abs(abs(a["lat"]["raw"]) - abs(b["lat"]["raw"])) < SAME_POSITION_TOLERANCE
            and abs(abs(a["lon"]["raw"]) - abs(b["lon"]["raw"])) < SAME_POSITION_TOLERANCE)


def _ambiguity_message(record):
    missing, invalid = [], []
    for axis in (record["lat"], record["lon"]):
        if axis["problem"] == "missing":
            missing.append(axis["ref_name"])
        elif axis["problem"] == "invalid":
            invalid.append(f"{axis['ref_name']} has unrecognised value '{axis['ref_raw']}'")
    parts = []
    if missing:
        parts.append("hemisphere reference is missing (" + " and ".join(missing) + ")")
    if invalid:
        parts.append("; ".join(invalid))
    return ("GPS coordinates present, but " + "; ".join(parts)
            + ". Location cannot be determined reliably."), missing, invalid


def _axis_display(axis, digits=6):
    if axis is None:
        return "?"
    letter = axis["ref"] or "?"
    return f"{round(axis['raw'], digits)}° {letter}"


# =============================================================================
# Public entry point
# =============================================================================

def interpret_gps(numeric, xmp_packet=None):
    """
    Interpret GPS metadata from ExifTool's NUMERIC (-n, -G0:1) output.

    `xmp_packet` is the raw XMP packet text (or None); it is needed to check
    the hemisphere letters of XMP coordinates.

    Returns a dict - see the module docstring for `status`:
        status, latitude, longitude   (signed; only for "valid"/"zero")
        raw_latitude, raw_longitude   (as recorded; unsigned for EXIF)
        latitude_ref, longitude_ref   ("N"/"S"/"E"/"W" or None)
        missing_refs, invalid_refs    (lists of human readable names)
        altitude, source, message, notes, display
    """
    exif = _exif_record(numeric)
    xmp = _xmp_record(numeric, xmp_packet)
    exif_state, xmp_state = _record_state(exif), _record_state(xmp)
    notes = []

    # ---- choose the record to report (EXIF is primary) ---------------------
    if exif_state == "resolved":
        chosen = exif
        if xmp_state == "resolved" and (
                abs(exif["lat"]["signed"] - xmp["lat"]["signed"]) > SAME_POSITION_TOLERANCE
                or abs(exif["lon"]["signed"] - xmp["lon"]["signed"]) > SAME_POSITION_TOLERANCE):
            notes.append(f"XMP GPS ({xmp['lat']['signed']}, {xmp['lon']['signed']}) differs from EXIF GPS")
    elif exif_state == "ambiguous":
        chosen = exif
        if xmp_state == "resolved":
            if _same_position(exif, xmp):
                # A second, explicitly signed GPS record of the SAME coordinates.
                chosen = xmp
                notes.append("EXIF GPS has no usable hemisphere reference; the hemisphere comes from the "
                             "XMP GPS record of the same coordinates, which includes it")
            else:
                notes.append("XMP GPS has a hemisphere but different coordinates, so it cannot resolve the EXIF GPS")
    elif xmp_state is not None:
        chosen = xmp
    else:
        chosen = exif          # incomplete EXIF record or None

    for record in (exif, xmp):
        if record:
            notes.extend(record["notes"])

    # ---- altitude (EXIF spec: missing GPSAltitudeRef means above sea level) --
    altitude = _to_float(numeric.get("Composite:GPSAltitude"))
    if altitude is None:
        altitude = _to_float(_find(numeric, "EXIF", "GPSAltitude", "GPS"))
        if altitude is not None and str(_find(numeric, "EXIF", "GPSAltitudeRef", "GPS")) == "1":
            altitude = -altitude
    altitude = None if altitude is None else round(altitude, 2)

    result = {
        "status": None, "latitude": None, "longitude": None, "altitude": altitude,
        "raw_latitude": None, "raw_longitude": None, "latitude_ref": None, "longitude_ref": None,
        # per axis: None (ok) / "missing" / "invalid", the raw ref value and the tag name
        "latitude_ref_problem": None, "longitude_ref_problem": None,
        "latitude_ref_raw": None, "longitude_ref_raw": None,
        "latitude_ref_name": None, "longitude_ref_name": None,
        "missing_refs": [], "invalid_refs": [], "source": None, "message": None,
        "notes": notes, "display": None,
    }

    has_gps_tags = any(
        _split_key(key)[1] == "GPS" or _split_key(key)[2].startswith("GPS") for key in numeric
    )
    state = _record_state(chosen)

    if chosen is not None:
        result["source"] = chosen["source"]
        for axis_name, prefix in (("lat", "latitude"), ("lon", "longitude")):
            axis = chosen.get(axis_name)
            if axis is not None:
                result[f"raw_{prefix}"] = axis["raw"]
                result[f"{prefix}_ref"] = axis["ref"]
                result[f"{prefix}_ref_problem"] = axis["problem"]
                result[f"{prefix}_ref_raw"] = axis["ref_raw"]
                result[f"{prefix}_ref_name"] = axis["ref_name"]

    if state is None:
        result["status"] = "incomplete" if has_gps_tags else None
        if has_gps_tags:
            result["message"] = "Some GPS tags exist but no latitude/longitude values."
        return result

    if state == "incomplete":
        result["status"] = "incomplete"
        present = "latitude" if chosen.get("lat") else "longitude"
        result["message"] = f"Only GPS {present} is present; no complete latitude/longitude pair."
        result["display"] = f"{_axis_display(chosen.get('lat'))}, {_axis_display(chosen.get('lon'))}"
        return result

    if state == "ambiguous":
        message, missing, invalid = _ambiguity_message(chosen)
        result.update(status="ambiguous", message=message, missing_refs=missing, invalid_refs=invalid)
        result["display"] = f"{_axis_display(chosen['lat'])}, {_axis_display(chosen['lon'])}"
        return result

    lat, lon = chosen["lat"]["signed"], chosen["lon"]["signed"]
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        result["status"] = "invalid"
        result["message"] = f"GPS coordinates out of range ({lat}, {lon})."
        result["display"] = f"{lat}, {lon}"
        return result
    result["latitude"], result["longitude"] = round(lat, 7), round(lon, 7)
    result["display"] = f"{result['latitude']}, {result['longitude']}"
    if lat == 0.0 and lon == 0.0:
        result["status"] = "zero"
        result["message"] = "GPS coordinates are 0,0 (usually a placeholder)."
    else:
        result["status"] = "valid"
    return result
