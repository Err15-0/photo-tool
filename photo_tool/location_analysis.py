"""
Investigation layer for AMBIGUOUS GPS: derived candidates + contextual evidence.

Evidence model (kept separate everywhere - backend, API and UI):

  1. RAW METADATA       what exists in the file (record.raw; gps raw values, refs)
  2. RESOLVED METADATA  conclusions directly supported by authoritative metadata
                        (normalized.gps_status == "valid", gps_latitude/longitude)
  3. DERIVED CANDIDATES every coordinate geometrically consistent with incomplete
                        GPS metadata ("kind": "derived_candidate")
  4. CONTEXTUAL EVIDENCE timezone consistency, related photos in the collection
                        ("kind": "contextual_evidence")

Hard rules:
  * This module only READS image records. It never changes gps_status, never
    sets flags.gps, never adds candidates to the resolved GPS data.
  * Candidates are not ranked by a made-up score; evidence is listed as facts.
  * Nothing here guesses a hemisphere (no timezone-sign tricks, no locale).
"""

import math
import re
from datetime import datetime

from . import config
from . import timezones

CANDIDATE_DISCLAIMER = ("These locations are derived from ambiguous GPS metadata and are "
                        "not confirmed locations.")
COLLECTION_DISCLAIMER = ("Collection context is contextual evidence, not GPS metadata. "
                         "'Same device' means the same make/model string – it can be a "
                         "different physical camera.")
PATTERN_DISCLAIMER = "Sharing a metadata pattern does not mean the images share a hemisphere."

EARTH_RADIUS_KM = 6371.0088
STATEMENT_LIMIT = 5      # readable statements per candidate (all items are still counted)
HEMISPHERES = {"latitude": ("N", "S"), "longitude": ("E", "W")}


# =============================================================================
# 3. Derived candidates
# =============================================================================

def _axis_options(axis, normalized):
    """Possible (signed value, hemisphere letter, basis text) for one axis."""
    magnitude = normalized.get(f"gps_raw_{axis}")
    if magnitude is None:
        return []
    magnitude = abs(float(magnitude))
    ref = normalized.get(f"gps_{axis}_ref")
    problem = normalized.get(f"gps_{axis}_ref_problem")
    ref_name = normalized.get(f"gps_{axis}_ref_name") or ("GPSLatitudeRef" if axis == "latitude" else "GPSLongitudeRef")
    positive, negative = HEMISPHERES[axis]

    if magnitude == 0.0:
        # 0 degrees: both hemispheres give the same coordinate.
        return [(0.0, "", "0° – hemisphere irrelevant")]
    if problem is None and ref in (positive, negative):
        sign = 1 if ref == positive else -1
        return [(sign * magnitude, ref, f"from metadata: {ref_name} = {ref}")]
    if problem == "invalid":
        why = f"assumed – {ref_name} malformed ({normalized.get(f'gps_{axis}_ref_raw')!r}), not interpreted"
    else:
        why = f"assumed – {ref_name} missing"
    return [(magnitude, positive, why), (-magnitude, negative, why)]


def _fmt_axis(value, letter):
    return f"{abs(value):.6f}°" + (f" {letter}" if letter else "")


def generate_candidates(normalized):
    """
    Every coordinate consistent with the known metadata of an AMBIGUOUS image.
    1 axis unknown -> 2 candidates, both unknown -> 4, zero axes deduplicated.
    Returns [] for any other GPS status (valid, zero, invalid, incomplete, none).
    """
    if normalized.get("gps_status") != "ambiguous":
        return []
    lat_options = _axis_options("latitude", normalized)
    lon_options = _axis_options("longitude", normalized)
    candidates, seen = [], set()
    for lat, lat_letter, lat_basis in lat_options:
        for lon, lon_letter, lon_basis in lon_options:
            key = (round(lat, 9), round(lon, 9))
            if key in seen:
                continue
            seen.add(key)
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                continue
            hemis = f"{lat_letter or '0°'}/{lon_letter or '0°'}"
            candidates.append({
                "id": hemis,
                "kind": "derived_candidate",
                "label": f"Possible location — {hemis}",
                "latitude": round(lat, 7),
                "longitude": round(lon, 7),
                "display": f"{_fmt_axis(lat, lat_letter)}, {_fmt_axis(lon, lon_letter)}",
                "latitude_basis": lat_basis,
                "longitude_basis": lon_basis,
                "status": "Derived candidate – not confirmed",
            })
    return candidates


# =============================================================================
# 4. Contextual evidence: collection context
# =============================================================================

def _unit_vector(lat, lon):
    phi, lam = math.radians(lat), math.radians(lon)
    return (math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi))


def _distance_km(v1, v2):
    dot = max(-1.0, min(1.0, v1[0] * v2[0] + v1[1] * v2[1] + v1[2] * v2[2]))
    return EARTH_RADIUS_KM * math.acos(dot)


_SEQUENCE_RE = re.compile(r"^(.*?)(\d+)(\.[^.]*)?$")


def _sequence(filename):
    match = _SEQUENCE_RE.match(filename or "")
    if not match:
        return None
    prefix, digits, _ext = match.groups()
    return prefix.lower(), len(digits), int(digits)


def _clean(text):
    return text.strip().lower() if isinstance(text, str) and text.strip() else None


def _photo_context(record):
    """The per-image facts used to relate photos (computed once per image)."""
    n = record.normalized or {}
    capture = n.get("capture_time") or {}
    return {
        "id": record.id,
        "filename": record.original_filename,
        "device": n.get("device"),
        "device_key": _clean(n.get("device")),
        "serial_key": _clean(n.get("serial_number")),
        "software_key": _clean(n.get("software")),
        "author_key": _clean(n.get("artist")),
        "utc": datetime.strptime(capture["utc"], "%Y-%m-%dT%H:%M:%SZ") if capture.get("utc") else None,
        "local": datetime.strptime(capture["local"], "%Y-%m-%dT%H:%M:%S") if capture.get("local") else None,
        "sequence": _sequence(record.original_filename),
    }


def _time_delta_minutes(a, b):
    """(signed minutes b - a, basis) or (None, None). UTC when both offsets are known."""
    if a["utc"] and b["utc"]:
        return (b["utc"] - a["utc"]).total_seconds() / 60.0, "UTC"
    if a["local"] and b["local"]:
        return (b["local"] - a["local"]).total_seconds() / 60.0, "local clock (offsets unknown)"
    return None, None


def _describe_delta(minutes):
    if minutes is None:
        return "unknown time difference"
    size = abs(minutes)
    text = f"{size:.0f} minute{'s' if round(size) != 1 else ''}" if size < 120 else f"{size / 60:.1f} hours"
    if size < 0.5:
        return "at the same time"
    return f"{text} {'later' if minutes > 0 else 'earlier'}"


def _relationship(a, b):
    """How photo b relates to ambiguous photo a (independent of candidates)."""
    minutes, basis = _time_delta_minutes(a, b)
    seq_delta = None
    if a["sequence"] and b["sequence"] and a["sequence"][:2] == b["sequence"][:2]:
        seq_delta = b["sequence"][2] - a["sequence"][2]
    return {
        "same_device_model": bool(a["device_key"] and a["device_key"] == b["device_key"]),
        "same_serial": bool(a["serial_key"] and a["serial_key"] == b["serial_key"]),
        "same_software": bool(a["software_key"] and a["software_key"] == b["software_key"]),
        "same_author": bool(a["author_key"] and a["author_key"] == b["author_key"]),
        "time_delta_minutes": None if minutes is None else round(minutes, 1),
        "time_basis": basis,
        "sequence_delta": seq_delta,
    }


def _collection_evidence(candidate_vector, a_ctx, resolved, relationships):
    """
    Facts about resolved photos in the collection relative to ONE candidate.
    `resolved` = [(ctx, unit_vector)], `relationships` = lazily filled cache
    {photo id: relationship} shared by all candidates of the same image.
    """
    window = config.CONTEXT_TIME_WINDOW_HOURS * 60
    supporting, conflicting, related = [], [], []
    closest_geo, closest_geo_dot = None, -2.0
    closest_time = None

    def relationship(ctx):
        rel = relationships.get(ctx["id"])
        if rel is None:
            rel = relationships[ctx["id"]] = _relationship(a_ctx, ctx)
        return rel

    def make_entry(ctx, distance):
        return {"id": ctx["id"], "filename": ctx["filename"], "device": ctx["device"],
                "distance_km": round(distance, 2), "relationship": relationship(ctx)}

    for ctx, vector in resolved:
        dot = candidate_vector[0] * vector[0] + candidate_vector[1] * vector[1] + candidate_vector[2] * vector[2]
        if dot > closest_geo_dot:                    # larger dot product = closer on the sphere
            closest_geo_dot, closest_geo = dot, ctx
        same_device = bool((a_ctx["device_key"] and a_ctx["device_key"] == ctx["device_key"])
                           or (a_ctx["serial_key"] and a_ctx["serial_key"] == ctx["serial_key"]))
        if not same_device:
            continue                                 # only the closest unrelated photo is reported
        rel = relationship(ctx)
        minutes = rel["time_delta_minutes"]
        related.append((math.inf if minutes is None else abs(minutes), ctx, vector))
        if minutes is None or abs(minutes) > window:
            continue
        distance = _distance_km(candidate_vector, vector)
        hours = abs(minutes) / 60.0
        speed = distance / hours if hours > 0 else (math.inf if distance > 1 else 0.0)
        if distance <= config.CONTEXT_DISTANCE_KM:
            supporting.append((abs(minutes), ctx, distance, minutes, rel, speed))
        elif speed > config.CONTEXT_MAX_PLAUSIBLE_SPEED_KMH:
            conflicting.append((abs(minutes), ctx, distance, minutes, rel, speed))

    # Everything is COUNTED; only the 5 closest in time become readable statements.
    supporting_count, conflicting_count = len(supporting), len(conflicting)
    widest_support = max((item[0] for item in supporting), default=None)
    supporting = [dict(make_entry(ctx, distance), statement=(
        f"A photo from the same device model taken {_describe_delta(minutes)} "
        f"({rel['time_basis']}) has a resolved location {distance:.1f} km from this candidate."))
        for _k, ctx, distance, minutes, rel, _s in sorted(supporting, key=lambda item: item[0])[:STATEMENT_LIMIT]]
    conflicting = [dict(make_entry(ctx, distance), statement=(
        f"A photo from the same device model taken {_describe_delta(minutes)} has a resolved "
        f"location {distance:,.0f} km from this candidate (implied travel: "
        f"{'an instantaneous jump' if math.isinf(speed) else f'{speed:,.0f} km/h'} – implausible if it is the same camera)."))
        for _k, ctx, distance, minutes, rel, speed in sorted(conflicting, key=lambda item: item[0])[:STATEMENT_LIMIT]]

    if closest_geo is not None:
        closest_geo = make_entry(closest_geo, EARTH_RADIUS_KM * math.acos(max(-1.0, min(1.0, closest_geo_dot))))
    # Only the 5 same-device photos closest in time are turned into full entries.
    related = [make_entry(ctx, _distance_km(candidate_vector, vector))
               for _key, ctx, vector in sorted(related, key=lambda item: item[0])[:5]]
    closest_time = related[0] if related and related[0]["relationship"]["time_delta_minutes"] is not None else None
    summary = []
    if supporting_count:
        summary.append(f"{supporting_count} photo(s) from the same device model within {widest_support:.0f} minutes have "
                       f"resolved GPS within {config.CONTEXT_DISTANCE_KM} km of this candidate.")
    if conflicting_count > len(conflicting):
        summary.append(f"{conflicting_count} same-device photo(s) in the time window imply implausible travel "
                       f"(the {len(conflicting)} closest in time are listed).")
    if closest_geo and not (closest_geo["relationship"]["same_device_model"] or closest_geo["relationship"]["same_serial"]):
        summary.append(f"Closest resolved photo overall is {closest_geo['distance_km']:,.1f} km away, from a "
                       f"different/unknown device ({closest_geo['device'] or 'no device info'}) – no device "
                       "relationship, weak context.")
    return {
        "kind": "contextual_evidence",
        "supporting": supporting,
        "conflicting": conflicting,
        "same_device_resolved_near": supporting_count,
        "same_device_conflicting": conflicting_count,
        "closest_temporal_match": closest_time,
        "closest_geographic_match": closest_geo,
        "related_resolved": related,
        "summary": summary,
    }


# =============================================================================
# Collection-level patterns across ambiguous images
# =============================================================================

def _pattern_text(n):
    parts = []
    for axis in ("latitude", "longitude"):
        problem = n.get(f"gps_{axis}_ref_problem")
        if problem:
            name = n.get(f"gps_{axis}_ref_name") or axis
            parts.append(f"{name} {'malformed' if problem == 'invalid' else 'missing'}")
    return " + ".join(parts) or "unknown"


def ambiguity_patterns(records):
    groups = {}
    for record in records:
        n = record.normalized or {}
        if n.get("gps_status") != "ambiguous":
            continue
        key = (n.get("device") or "(unknown device)", _pattern_text(n))
        groups.setdefault(key, []).append(record)
    patterns = []
    for (device, pattern), members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        times = sorted(t for t in ((m.normalized.get("capture_time") or {}).get("local") for m in members) if t)
        patterns.append({
            "kind": "collection_pattern",
            "device": device,
            "pattern": pattern,
            "count": len(members),
            "image_ids": [m.id for m in members],
            "filenames": [m.original_filename for m in members],
            "first_capture": times[0] if times else None,
            "last_capture": times[-1] if times else None,
            "statement": (f"{len(members)} image(s) from {device} have ambiguous GPS with the same pattern: {pattern}."),
            "disclaimer": PATTERN_DISCLAIMER,
        })
    return patterns


# =============================================================================
# Public entry point
# =============================================================================

def analyze_collection(records, lookup=None):
    """
    Build the investigation view for every AMBIGUOUS image in `records`.
    Read-only: records are not modified.
    """
    lookup = lookup or timezones.get_lookup()
    contexts = {r.id: _photo_context(r) for r in records}
    resolved = [
        r for r in records
        if (r.flags or {}).get("gps") and (r.normalized or {}).get("gps_status") == "valid"
    ]
    patterns = ambiguity_patterns(records)
    pattern_by_image = {i: p for p in patterns for i in p["image_ids"]}

    images = {}
    total_candidates = 0
    # Unit vectors of resolved photos: computed ONCE per collection.
    resolved_points = [(contexts[r.id], _unit_vector(r.normalized["gps_latitude"], r.normalized["gps_longitude"]))
                       for r in resolved]

    for record in records:
        n = record.normalized or {}
        if n.get("gps_status") != "ambiguous":
            continue
        a_ctx = contexts[record.id]
        relationships = {}      # filled lazily, shared by this image's candidates

        candidates = []
        for candidate in generate_candidates(n):
            tz = timezones.evaluate(candidate["latitude"], candidate["longitude"], n.get("capture_time") or {}, lookup)
            collection = _collection_evidence(_unit_vector(candidate["latitude"], candidate["longitude"]),
                                              a_ctx, resolved_points, relationships)
            support = []
            if tz["status"] == "consistent":
                support.append("timezone consistent")
            if collection["supporting"]:
                support.append(f"{collection['same_device_resolved_near']} nearby same-device photo(s)")
            conflicts = []
            if tz["status"] == "inconsistent":
                conflicts.append("timezone inconsistent")
            if collection["conflicting"]:
                conflicts.append(f"{collection['same_device_conflicting']} same-device photo(s) imply implausible travel")
            candidates.append(dict(candidate, timezone=tz, collection=collection, assessment={
                "kind": "assessment",
                "status": "Derived candidate – not confirmed",
                "has_contextual_support": bool(support),
                "supporting_signals": support,
                "conflicting_signals": conflicts,
            }))
        total_candidates += len(candidates)
        pattern = pattern_by_image.get(record.id)
        images[record.id] = {
            "id": record.id,
            "filename": record.original_filename,
            "raw_gps": {   # 1. RAW METADATA (copied, never altered)
                "kind": "raw_metadata",
                "latitude_magnitude": n.get("gps_raw_latitude"),
                "longitude_magnitude": n.get("gps_raw_longitude"),
                "latitude_ref": n.get("gps_latitude_ref_raw"),
                "longitude_ref": n.get("gps_longitude_ref_raw"),
                "latitude_ref_name": n.get("gps_latitude_ref_name"),
                "longitude_ref_name": n.get("gps_longitude_ref_name"),
                "latitude_ref_problem": n.get("gps_latitude_ref_problem"),
                "longitude_ref_problem": n.get("gps_longitude_ref_problem"),
                "source": n.get("gps_source"),
            },
            "resolved": {  # 2. RESOLVED METADATA: nothing - that is the point
                "kind": "resolved_metadata",
                "gps_status": n.get("gps_status"),
                "location": None,
                "message": n.get("gps_message"),
            },
            "capture_time": n.get("capture_time"),
            "candidates": candidates,   # 3. DERIVED + 4. CONTEXTUAL (nested per candidate)
            "pattern": None if not pattern or pattern["count"] < 2 else {
                "statement": pattern["statement"], "count": pattern["count"], "disclaimer": PATTERN_DISCLAIMER,
            },
        }

    return {
        "kind": "location_investigation",
        "disclaimers": {
            "candidates": CANDIDATE_DISCLAIMER,
            "timezone": timezones.DISCLAIMER,
            "collection": COLLECTION_DISCLAIMER,
            "patterns": PATTERN_DISCLAIMER,
        },
        "timezone_lookup": {"available": lookup.available, "name": lookup.name, "description": lookup.description},
        "rules": {
            "context_time_window_hours": config.CONTEXT_TIME_WINDOW_HOURS,
            "context_distance_km": config.CONTEXT_DISTANCE_KM,
            "max_plausible_speed_kmh": config.CONTEXT_MAX_PLAUSIBLE_SPEED_KMH,
        },
        "counts": {"ambiguous_images": len(images), "candidates": total_candidates, "resolved_images": len(resolved)},
        "images": images,
        "patterns": [p for p in patterns if p["count"] >= 2],
    }
