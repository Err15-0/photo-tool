"""
Timezone consistency: does a candidate location's local UTC offset AT THE
CAPTURE DATE match the offset recorded in the image?

Two separate problems:

1. coordinate -> IANA timezone name(s) ("America/New_York")
   Needs timezone BOUNDARY data. Pluggable (TimezoneLookup):
     * TzfpyLookup - used automatically when the OPTIONAL package tzfpy is
       installed (pip install -r requirements-timezone.txt). Fully offline:
       the boundary data is compiled into the package. Boundary data:
       timezone-boundary-builder / OpenStreetMap contributors, ODbL 1.0.
     * UnavailableLookup - when tzfpy is missing (or disabled with
       PHOTO_TOOL_TIMEZONE_LOOKUP=off): every result is "Timezone unknown".
   Photo Tool NEVER calls an online timezone/geolocation service.
   Some places use more than one local time (e.g. Xinjiang: Beijing time
   Asia/Shanghai UTC+8 and Xinjiang time Asia/Urumqi UTC+6). tzfpy returns
   ALL overlapping zones and every one of them is checked.

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

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .capture_time import format_offset

DISCLAIMER = "Timezone consistency is contextual evidence and does not confirm the location."


class TimezoneLookup:
    """Interface: coordinate -> IANA timezone names. Must work OFFLINE."""

    available = False
    name = "none"
    description = ""
    version = None
    data_version = None
    attribution = None

    def timezones_at(self, latitude, longitude):
        """All IANA zone names that apply at this point ([] if none / unknown)."""
        single = self.timezone_at(latitude, longitude)
        return [single] if single else []

    def timezone_at(self, latitude, longitude):
        raise NotImplementedError

    def info(self):
        return {"available": self.available, "name": self.name, "description": self.description,
                "version": self.version, "data_version": self.data_version, "attribution": self.attribution}


class UnavailableLookup(TimezoneLookup):
    """No boundary data -> every lookup is 'unknown'. Default when tzfpy is missing."""

    available = False
    name = "unavailable"

    def __init__(self, description=None):
        self.description = description or (
            "Optional offline timezone support is not installed, so candidate timezones cannot be "
            "determined. Install it with: pip install -r requirements-timezone.txt")

    def timezone_at(self, latitude, longitude):
        return None


TZFPY_ATTRIBUTION = ("Timezone boundaries: © OpenStreetMap contributors, timezone-boundary-builder "
                     "(Evan Siroky), via tzf/tzfpy – Open Database License (ODbL) 1.0")


class TzfpyLookup(TimezoneLookup):
    """
    Offline coordinate -> IANA zone lookup with the optional `tzfpy` package.
    Only zone NAMES come from tzfpy; UTC offsets always come from zoneinfo.
    The finder is built lazily on the first lookup (~50 ms).
    """

    available = True
    name = "tzfpy"
    attribution = TZFPY_ATTRIBUTION

    def __init__(self, module):
        import importlib.metadata
        self._tzfpy = module
        try:
            self.version = importlib.metadata.version("tzfpy")
        except importlib.metadata.PackageNotFoundError:
            self.version = getattr(module, "__version__", "unknown")
        try:
            self.data_version = module.data_version()
        except Exception:
            self.data_version = "unknown"
        self.description = (f"Offline lookup with tzfpy {self.version} (boundary data {self.data_version}); "
                            "no network access")

    def timezones_at(self, latitude, longitude):
        try:
            # NOTE: tzfpy takes (longitude, latitude).
            names = self._tzfpy.get_tzs(longitude, latitude)
        except Exception:
            return []
        return [name for name in names if name]

    def timezone_at(self, latitude, longitude):
        names = self.timezones_at(latitude, longitude)
        return names[0] if names else None


def default_lookup():
    """tzfpy if installed (and not disabled), otherwise 'unavailable'."""
    if os.environ.get("PHOTO_TOOL_TIMEZONE_LOOKUP", "").strip().lower() in ("off", "none", "0", "false"):
        return UnavailableLookup("Timezone lookup disabled (PHOTO_TOOL_TIMEZONE_LOOKUP=off).")
    try:
        import tzfpy
    except ImportError:
        return UnavailableLookup()
    return TzfpyLookup(tzfpy)


_lookup = default_lookup()


def get_lookup():
    return _lookup


def set_lookup(lookup):
    """Install a lookup backend. None -> back to the default (tzfpy if installed)."""
    global _lookup
    _lookup = lookup or default_lookup()


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
        "timezones": [], "zones": [], "matching_timezones": [],
        "lookup": lookup.name, "attribution": lookup.attribution,
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

    names = lookup.timezones_at(latitude, longitude)
    civil = [name for name in names if not name.startswith("Etc/")]
    result["timezones"] = names
    result["timezone"] = civil[0] if civil else (names[0] if names else None)
    if not names:
        result["reason"] = "No timezone found for this coordinate"
        return result
    if not civil:
        # Ocean / polar areas only have theoretical nautical zones (Etc/GMT+5 = UTC-05:00).
        # Matching one would look like support for an open-ocean candidate -> not used as evidence.
        result["reason"] = (f"Open ocean / no civil timezone ({', '.join(names)} is a nautical zone) – "
                            "not used as evidence")
        return result

    local_time = datetime.strptime(capture["local"], "%Y-%m-%dT%H:%M:%S")
    per_zone, notes, matching = [], [], []
    for name in civil:
        offsets, note = offsets_at(name, local_time)
        if offsets is None:
            notes.append(note)
            continue
        if note:
            notes.append(f"{name}: {note}")
        per_zone.append({"timezone": name, "offsets": [format_offset(m) for m in sorted(offsets)]})
        if capture["offset_minutes"] in offsets:
            matching.append(name)
    if not per_zone:
        result["reason"] = "; ".join(notes)
        return result
    result["zones"] = per_zone
    result["candidate_offsets"] = sorted({o for zone in per_zone for o in zone["offsets"]})
    if len(per_zone) > 1:
        notes.append("More than one local time is in use at this location (" + ", ".join(
            f"{zone['timezone']} UTC{'/'.join(zone['offsets'])}" for zone in per_zone)
            + "); the recorded offset is compared with each of them.")
    result["note"] = " ".join(notes) or None
    result["matching_timezones"] = matching
    if matching:
        result["timezone"] = matching[0]
    result["status"] = "consistent" if matching else "inconsistent"
    return result
