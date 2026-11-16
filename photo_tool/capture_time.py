"""
Capture time + UTC offset, paired correctly and validated.

Why a separate module: the old normalized "timezone_offset" field simply took
the first of OffsetTimeOriginal / OffsetTime / TimeZone, without saying which
timestamp it belongs to and without validating it. For timezone-consistency
analysis we need the PAIR (local capture time, offset of that same clock).

EXIF pairs (EXIF 2.31+):
    DateTimeOriginal  <-> OffsetTimeOriginal   (moment of capture)   preferred
    CreateDate        <-> OffsetTimeDigitized  (moment of digitizing)
    ModifyDate        <-> OffsetTime           (last modification)    weakest
XMP / IPTC dates may carry the offset inside the value ("...07:36:35-05:00").

Malformed offsets (ExifTool happily passes "+25:00" through) are reported,
never silently corrected. Nothing here looks at GPS.
"""

import re
from datetime import datetime, timedelta

# (timestamp tag, offset tag, label). Searched in this order.
EXIF_PAIRS = [
    ("DateTimeOriginal", "OffsetTimeOriginal", "capture time (DateTimeOriginal + OffsetTimeOriginal)"),
    ("CreateDate", "OffsetTimeDigitized", "digitized time (CreateDate + OffsetTimeDigitized)"),
    ("ModifyDate", "OffsetTime", "modification time (ModifyDate + OffsetTime) – may differ from capture time"),
]
# Dates that may contain their own offset (checked when no EXIF pair resolves).
EMBEDDED_OFFSET_TAGS = [
    ("XMP", "DateTimeOriginal"), ("XMP", "DateCreated"), ("XMP", "CreateDate"),
]

_TIME_RE = re.compile(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*(.*)$")
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")

MAX_OFFSET_MINUTES = 14 * 60     # real-world offsets range from -12:00 to +14:00


def _split_key(key):
    parts = key.split(":")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[0], parts[1]
    return "", "", key


def _find(raw, family0, tag):
    """(value, key) of the first tag in group family0 with this name, else (None, None)."""
    for key, value in raw.items():
        f0, _f1, name = _split_key(key)
        if f0 == family0 and name == tag:
            return value, key
    return None, None


def parse_offset(value):
    """
    "-05:00" -> (-300, None); "Z" -> (0, None)
    missing/blank/placeholder -> (None, "missing"); garbage or > 14h -> (None, "malformed")
    """
    if value is None:
        return None, "missing"
    text = str(value).strip()
    if not text.strip(" :"):          # "", "   :  " (EXIF placeholder for 'unknown')
        return None, "missing"
    if text.upper() == "Z":
        return 0, None
    match = _OFFSET_RE.match(text)
    if not match:
        return None, "malformed"
    sign, hours, minutes = match.groups()
    hours, minutes = int(hours), int(minutes)
    if minutes >= 60:
        return None, "malformed"
    total = hours * 60 + minutes
    if total > MAX_OFFSET_MINUTES:
        return None, "malformed"
    return (-total if sign == "-" else total), None


def format_offset(minutes):
    if minutes is None:
        return None
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def parse_local_time(value):
    """'2021:11:20 07:36:35' -> (datetime, rest_of_string) or (None, None). Zero dates -> None."""
    if value is None:
        return None, None
    match = _TIME_RE.match(str(value).strip())
    if not match:
        return None, None
    year, month, day, hour, minute, second, rest = match.groups()
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
    except ValueError:
        return None, None
    return moment, rest.strip()


def extract_capture_time(raw):
    """
    Returns:
        local          "2021-11-20T07:36:35" (naive local clock time) or None
        local_source   tag the local time came from
        offset         "-05:00" or None
        offset_minutes -300 or None
        offset_source  tag the offset came from
        basis          human label of the pair used
        utc            "2021-11-20T12:36:35Z" if local + offset are known
        problems       list of human readable problems (missing / malformed ...)
    """
    result = {
        "local": None, "local_source": None, "offset": None, "offset_minutes": None,
        "offset_source": None, "basis": None, "utc": None, "problems": [],
    }

    # 1. EXIF timestamp/offset pairs
    for time_tag, offset_tag, label in EXIF_PAIRS:
        time_value, time_key = _find(raw, "EXIF", time_tag)
        moment, _rest = parse_local_time(time_value)
        if moment is None:
            continue
        if result["local"] is None:
            result["local"], result["local_source"], result["basis"] = moment, time_key, label
        offset_value, offset_key = _find(raw, "EXIF", offset_tag)
        minutes, problem = parse_offset(offset_value)
        if problem == "malformed":
            result["problems"].append(f"{offset_tag} is malformed ({offset_value!r}) – ignored")
            continue
        if minutes is None:
            continue
        # Use this pair (timestamp + its own offset).
        result.update(local=moment, local_source=time_key, offset_minutes=minutes,
                      offset_source=offset_key, basis=label)
        break

    # 2. Dates with an embedded offset (XMP)
    if result["offset_minutes"] is None:
        for family0, tag in EMBEDDED_OFFSET_TAGS:
            value, key = _find(raw, family0, tag)
            moment, rest = parse_local_time(value)
            if moment is None or not rest:
                continue
            minutes, problem = parse_offset(rest)
            if problem == "malformed":
                result["problems"].append(f"{tag} has a malformed offset ({rest!r}) – ignored")
                continue
            if minutes is None:
                continue
            if result["local"] is None or result["local"] == moment:
                result.update(local=moment, local_source=key, offset_minutes=minutes,
                              offset_source=key, basis=f"{key} (offset embedded in the value)")
                break

    if result["local"] is None:
        result["problems"].append("No capture timestamp")
    if result["offset_minutes"] is None and not any("malformed" in p for p in result["problems"]):
        result["problems"].append("No UTC offset recorded (OffsetTimeOriginal / OffsetTime / OffsetTimeDigitized missing)")

    local = result["local"]
    result["offset"] = format_offset(result["offset_minutes"])
    if local is not None and result["offset_minutes"] is not None:
        result["utc"] = (local - timedelta(minutes=result["offset_minutes"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    result["local"] = local.strftime("%Y-%m-%dT%H:%M:%S") if local else None
    return result
