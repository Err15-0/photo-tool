"""
Timezone consistency: does a candidate location's local UTC offset AT THE
CAPTURE DATE match the offset recorded in the image?

Two separate problems:

1. coordinate -> IANA timezone name ("America/New_York")
   Needs timezone BOUNDARY data. This is pluggable (TimezoneLookup). The
   default is UnavailableLookup: Photo Tool ships no boundary dataset yet and
   NEVER calls an online service. Result: "Timezone unknown" with a reason.

2. IANA name + local date/time -> UTC offset, with historical DST rules
   Python's standard library (zoneinfo) + the operating system's IANA tz
   database (Debian package "tzdata", installed by default). No new
   dependency. Handles DST, rule changes (e.g. US 2007) and offset changes
   (e.g. Moscow 2011/2014), and the repeated hour when clocks go back.

Outcome labels are deliberately neutral:
    "consistent" / "inconsistent" / "unknown"
Timezone consistency is CONTEXTUAL EVIDENCE. It never resolves a coordinate,
and the sign of an offset is never used to guess a hemisphere.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .capture_time import format_offset

DISCLAIMER = "Timezone consistency is contextual evidence and does not confirm the location."


class TimezoneLookup:
    """Interface: coordinate -> IANA timezone name (or None). Must work OFFLINE."""

    available = False
    name = "none"
    description = ""

    def timezone_at(self, latitude, longitude):
        raise NotImplementedError


class UnavailableLookup(TimezoneLookup):
    """Default: no boundary dataset installed -> every lookup is 'unknown'."""

    available = False
    name = "unavailable"
    description = ("Offline timezone boundary data is not installed, so candidate "
                   "timezones cannot be determined (see README: Timezone consistency).")

    def timezone_at(self, latitude, longitude):
        return None


_lookup = UnavailableLookup()


def get_lookup():
    return _lookup


def set_lookup(lookup):
    """Install a lookup backend (used by tests; a real offline backend can be added later)."""
    global _lookup
    _lookup = lookup or UnavailableLookup()


def offsets_at(tz_name, local_time):
    """
    UTC offsets (minutes) the zone uses at this LOCAL wall-clock time.
    Usually one value; two when the local time occurs twice (clocks go back).
    Returns (set_of_minutes, note) or (None, reason) if the zone is unknown.
    """
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None, f"timezone {tz_name!r} not found in the local tz database"
    offsets = set()
    for fold in (0, 1):
        delta = local_time.replace(tzinfo=zone, fold=fold).utcoffset()
        offsets.add(int(delta.total_seconds() // 60))
    note = None
    if len(offsets) > 1:
        note = "capture time falls in a repeated hour (DST change): both offsets are possible"
    return offsets, note


def evaluate(latitude, longitude, capture, lookup=None):
    """
    Compare one candidate coordinate with the image's capture time/offset.
    `capture` is the dict from capture_time.extract_capture_time().
    """
    lookup = lookup or get_lookup()
    result = {
        "status": "unknown", "reason": None, "timezone": None,
        "candidate_offsets": [], "capture_offset": capture.get("offset"),
        "capture_local": capture.get("local"), "capture_basis": capture.get("basis"),
        "note": None, "kind": "contextual_evidence", "disclaimer": DISCLAIMER,
    }
    if capture.get("local") is None:
        result["reason"] = "No capture timestamp – the offset at the capture date cannot be evaluated"
        return result
    if capture.get("offset_minutes") is None:
        malformed = [p for p in capture.get("problems", []) if "malformed" in p]
        result["reason"] = malformed[0] if malformed else "No UTC offset recorded in the image"
        return result
    if not lookup.available:
        result["reason"] = lookup.description
        return result

    tz_name = lookup.timezone_at(latitude, longitude)
    result["timezone"] = tz_name
    if not tz_name:
        result["reason"] = "No timezone found for this coordinate"
        return result
    if tz_name.startswith("Etc/"):
        # Ocean areas only have theoretical nautical zones (Etc/GMT+5 = UTC-05:00).
        # Matching one would look like support for an open-ocean candidate.
        result["reason"] = f"Open ocean / no civil timezone ({tz_name} is a nautical zone) – not evaluated"
        return result

    local_time = datetime.strptime(capture["local"], "%Y-%m-%dT%H:%M:%S")
    offsets, note = offsets_at(tz_name, local_time)
    if offsets is None:
        result["reason"] = note
        return result
    result["candidate_offsets"] = [format_offset(m) for m in sorted(offsets)]
    result["note"] = note
    result["status"] = "consistent" if capture["offset_minutes"] in offsets else "inconsistent"
    return result
