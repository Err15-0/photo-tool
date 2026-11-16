"""
Regression tests for the ambiguous-GPS investigation layer:
derived candidates, timezone consistency, collection context, patterns.

Core invariant under test: contextual evidence NEVER turns ambiguous GPS into
resolved GPS, and derived candidates never enter the resolved-GPS dataset.

    python -m unittest discover -s tests -v
"""

import copy
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from photo_tool import capture_time, location_analysis, timezones  # noqa: E402
from photo_tool.metadata import analyze_metadata  # noqa: E402
from photo_tool.workspace import ImageRecord, generate_image_id  # noqa: E402

EXIFTOOL = shutil.which("exiftool")
try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


# ----------------------------------------------------------------------------- helpers

def gps_numeric(lat=None, lat_ref=None, lon=None, lon_ref=None):
    """ExifTool -n -G0:1 style GPS (Composite only where ExifTool 12.76 builds it)."""
    n = {"EXIF:GPS:GPSVersionID": "2 3 0 0"}
    if lat is not None:
        n["EXIF:GPS:GPSLatitude"] = lat
    if lon is not None:
        n["EXIF:GPS:GPSLongitude"] = lon
    if lat_ref is not None:
        n["EXIF:GPS:GPSLatitudeRef"] = lat_ref
        if lat is not None:
            n["Composite:GPSLatitude"] = -lat if lat_ref == "S" else lat
    if lon_ref is not None:
        n["EXIF:GPS:GPSLongitudeRef"] = lon_ref
        if lon is not None:
            n["Composite:GPSLongitude"] = -lon if lon_ref == "W" else lon
    return n


def raw_meta(make="Apple", model="iPhone 12", when=None, offset=None, software=None):
    raw = {}
    if make:
        raw["EXIF:IFD0:Make"] = make
    if model:
        raw["EXIF:IFD0:Model"] = model
    if software:
        raw["EXIF:IFD0:Software"] = software
    if when:
        raw["EXIF:ExifIFD:DateTimeOriginal"] = when
    if offset is not None:
        raw["EXIF:ExifIFD:OffsetTimeOriginal"] = offset
    return raw


def normalized_for(numeric, raw=None):
    return analyze_metadata(raw or {}, numeric)["normalized"]


def make_record(name, numeric, raw):
    analysis = analyze_metadata(raw, numeric)
    record = ImageRecord(
        id=generate_image_id(), original_filename=name, stored_extension="jpg", image_format="JPEG",
        mime_type="image/jpeg", file_size=1, sha256=name, width=1, height=1, display_width=1,
        display_height=1, frame_count=1, uploaded_at="2026-01-01T00:00:00+00:00",
    )
    record.raw, record.normalized = analysis["raw"], analysis["normalized"]
    record.flags, record.sensitive = analysis["flags"], analysis["sensitive"]
    return record


class BoxLookup(timezones.TimezoneLookup):
    """TEST ONLY: crude boxes standing in for real boundary data."""
    available, name, description = True, "test-boxes", "test lookup"

    def timezone_at(self, lat, lon):
        if 25 <= lat <= 49 and -90 <= lon <= -66:
            return "America/New_York"
        if 25 <= lat <= 50 and 70 <= lon <= 100:
            return "Asia/Shanghai"
        if -40 <= lat <= -28 and 140 <= lon <= 155:
            return "Australia/Sydney"
        if 50 <= lat <= 60 and 30 <= lon <= 40:
            return "Europe/Moscow"
        return "Etc/GMT+5" if lon < 0 else "Etc/GMT-5"   # ocean: nautical zones


CTF_LAT, CTF_LON = 38.551734, 78.333293


# ============================================================================ candidates

class CandidateGenerationTests(unittest.TestCase):

    def ids(self, n):
        return [c["id"] for c in location_analysis.generate_candidates(n)]

    def test_both_refs_missing_gives_four(self):
        n = normalized_for(gps_numeric(CTF_LAT, None, CTF_LON, None))
        cands = location_analysis.generate_candidates(n)
        self.assertEqual([c["id"] for c in cands], ["N/E", "N/W", "S/E", "S/W"])
        coords = {(c["latitude"], c["longitude"]) for c in cands}
        self.assertEqual(coords, {(CTF_LAT, CTF_LON), (CTF_LAT, -CTF_LON), (-CTF_LAT, CTF_LON), (-CTF_LAT, -CTF_LON)})
        for c in cands:
            self.assertEqual(c["kind"], "derived_candidate")
            self.assertIn("not confirmed", c["status"])
            self.assertIn("GPSLatitudeRef missing", c["latitude_basis"])

    def test_latitude_ref_missing_gives_two(self):
        n = normalized_for(gps_numeric(CTF_LAT, None, CTF_LON, "W"))
        cands = location_analysis.generate_candidates(n)
        self.assertEqual([c["id"] for c in cands], ["N/W", "S/W"])
        self.assertTrue(all(c["longitude"] == -CTF_LON for c in cands))
        self.assertTrue(all("from metadata: GPSLongitudeRef = W" in c["longitude_basis"] for c in cands))

    def test_longitude_ref_missing_gives_two(self):
        n = normalized_for(gps_numeric(CTF_LAT, "N", CTF_LON, None))
        self.assertEqual(self.ids(n), ["N/E", "N/W"])

    def test_both_refs_present_keeps_resolved_behavior(self):
        n = normalized_for(gps_numeric(CTF_LAT, "N", CTF_LON, "W"))
        self.assertEqual(n["gps_status"], "valid")
        self.assertEqual((n["gps_latitude"], n["gps_longitude"]), (CTF_LAT, -CTF_LON))
        self.assertEqual(location_analysis.generate_candidates(n), [])

    def test_malformed_reference_stays_ambiguous_and_uninterpreted(self):
        n = normalized_for(gps_numeric(CTF_LAT, "X", CTF_LON, "W"))
        self.assertEqual(n["gps_status"], "ambiguous")
        cands = location_analysis.generate_candidates(n)
        self.assertEqual([c["id"] for c in cands], ["N/W", "S/W"])     # 'X' is NOT read as N
        self.assertTrue(all("malformed ('X')" in c["latitude_basis"] for c in cands))

    def test_zero_latitude_dedupes(self):
        n = normalized_for(gps_numeric(0.0, None, CTF_LON, None))
        self.assertEqual(self.ids(n), ["0°/E", "0°/W"])

    def test_zero_longitude_dedupes(self):
        n = normalized_for(gps_numeric(CTF_LAT, None, 0.0, None))
        self.assertEqual(self.ids(n), ["N/0°", "S/0°"])

    def test_zero_zero_has_no_candidates(self):
        n = normalized_for(gps_numeric(0.0, None, 0.0, None))
        self.assertEqual(n["gps_status"], "zero")
        self.assertEqual(location_analysis.generate_candidates(n), [])

    def test_candidates_are_unique(self):
        for numeric in (gps_numeric(0.0, None, 5.0, None), gps_numeric(5.0, None, 0.0, None), gps_numeric(1.0, None, 2.0, None)):
            cands = location_analysis.generate_candidates(normalized_for(numeric))
            coords = [(c["latitude"], c["longitude"]) for c in cands]
            self.assertEqual(len(coords), len(set(coords)))

    def test_no_gps(self):
        self.assertEqual(location_analysis.generate_candidates(normalized_for({"EXIF:IFD0:Make": "Canon"})), [])

    def test_incomplete_pair(self):
        n = normalized_for(gps_numeric(CTF_LAT, None, None, None))
        self.assertEqual(n["gps_status"], "incomplete")
        self.assertEqual(location_analysis.generate_candidates(n), [])


# ============================================================================ capture time

class CaptureTimeTests(unittest.TestCase):

    def test_original_pair_preferred(self):
        raw = {"EXIF:ExifIFD:DateTimeOriginal": "2021:11:20 07:36:35", "EXIF:ExifIFD:OffsetTimeOriginal": "-05:00",
               "EXIF:IFD0:ModifyDate": "2021:11:21 10:00:00", "EXIF:ExifIFD:OffsetTime": "+01:00"}
        c = capture_time.extract_capture_time(raw)
        self.assertEqual((c["local"], c["offset"]), ("2021-11-20T07:36:35", "-05:00"))
        self.assertEqual(c["utc"], "2021-11-20T12:36:35Z")
        self.assertIn("DateTimeOriginal", c["basis"])

    def test_xmp_embedded_offset(self):
        c = capture_time.extract_capture_time({"XMP:XMP-exif:DateTimeOriginal": "2021:11:20 07:36:35-05:00"})
        self.assertEqual(c["offset"], "-05:00")

    def test_offset_parsing(self):
        self.assertEqual(capture_time.parse_offset("-05:00"), (-300, None))
        self.assertEqual(capture_time.parse_offset("+05:45"), (345, None))
        self.assertEqual(capture_time.parse_offset("   :  "), (None, "missing"))
        self.assertEqual(capture_time.parse_offset(None), (None, "missing"))
        self.assertEqual(capture_time.parse_offset("+25:00"), (None, "malformed"))
        self.assertEqual(capture_time.parse_offset("EST"), (None, "malformed"))


# ============================================================================ timezone

class TimezoneConsistencyTests(unittest.TestCase):
    lookup = BoxLookup()

    def evaluate(self, lat, lon, when, offset):
        raw = {}
        if when:
            raw["EXIF:ExifIFD:DateTimeOriginal"] = when
        if offset is not None:
            raw["EXIF:ExifIFD:OffsetTimeOriginal"] = offset
        return timezones.evaluate(lat, lon, capture_time.extract_capture_time(raw), self.lookup)

    def test_consistent_candidate(self):
        r = self.evaluate(CTF_LAT, -CTF_LON, "2021:11:20 07:36:35", "-05:00")
        self.assertEqual((r["status"], r["timezone"], r["candidate_offsets"]), ("consistent", "America/New_York", ["-05:00"]))
        self.assertEqual(r["kind"], "contextual_evidence")
        self.assertIn("does not confirm", r["disclaimer"])

    def test_inconsistent_candidate(self):
        r = self.evaluate(CTF_LAT, CTF_LON, "2021:11:20 07:36:35", "-05:00")
        self.assertEqual((r["status"], r["timezone"]), ("inconsistent", "Asia/Shanghai"))

    def test_missing_capture_offset(self):
        r = self.evaluate(CTF_LAT, -CTF_LON, "2021:11:20 07:36:35", None)
        self.assertEqual(r["status"], "unknown")
        self.assertIn("No UTC offset", r["reason"])

    def test_malformed_capture_offset(self):
        r = self.evaluate(CTF_LAT, -CTF_LON, "2021:11:20 07:36:35", "+25:00")
        self.assertEqual(r["status"], "unknown")
        self.assertIn("malformed", r["reason"])

    def test_missing_capture_timestamp(self):
        r = self.evaluate(CTF_LAT, -CTF_LON, None, "-05:00")
        self.assertEqual(r["status"], "unknown")
        self.assertIn("No capture timestamp", r["reason"])

    def test_dst_sensitive_date(self):
        # New York: DST until 2021-11-07 -> -04:00 on Nov 6, -05:00 on Nov 20
        self.assertEqual(self.evaluate(CTF_LAT, -CTF_LON, "2021:11:06 16:10:00", "-04:00")["status"], "consistent")
        self.assertEqual(self.evaluate(CTF_LAT, -CTF_LON, "2021:11:06 16:10:00", "-05:00")["status"], "inconsistent")
        self.assertEqual(self.evaluate(CTF_LAT, -CTF_LON, "2021:11:20 16:10:00", "-04:00")["status"], "inconsistent")

    def test_historical_offsets(self):
        # US DST rules changed in 2007: Nov 1 was standard time in 2006, still DST in 2007.
        self.assertEqual(self.evaluate(40.7, -74.0, "2006:11:01 12:00:00", "-05:00")["status"], "consistent")
        self.assertEqual(self.evaluate(40.7, -74.0, "2007:11:01 12:00:00", "-04:00")["status"], "consistent")
        # Moscow: permanent +04:00 in 2012, +03:00 since late 2014.
        self.assertEqual(self.evaluate(55.75, 37.6, "2012:07:01 12:00:00", "+04:00")["status"], "consistent")
        self.assertEqual(self.evaluate(55.75, 37.6, "2015:07:01 12:00:00", "+04:00")["status"], "inconsistent")

    def test_repeated_hour_accepts_both_offsets(self):
        for offset in ("-04:00", "-05:00"):
            r = self.evaluate(40.7, -74.0, "2021:11:07 01:30:00", offset)
            self.assertEqual(r["status"], "consistent")
            self.assertIn("repeated hour", r["note"])

    def test_ocean_nautical_zone_is_unknown_not_consistent(self):
        # S/W mirror of the CTF case lies in the Pacific: Etc/GMT+5 == UTC-05:00, the same as
        # the recorded offset. That must NOT be reported as "consistent".
        r = self.evaluate(-CTF_LAT, -CTF_LON, "2021:11:20 07:36:35", "-05:00")
        self.assertEqual(r["status"], "unknown")
        self.assertIn("nautical", r["reason"])

    def test_without_optional_backend_everything_is_unknown(self):
        raw = {"EXIF:ExifIFD:DateTimeOriginal": "2021:11:20 07:36:35", "EXIF:ExifIFD:OffsetTimeOriginal": "-05:00"}
        r = timezones.evaluate(CTF_LAT, -CTF_LON, capture_time.extract_capture_time(raw), timezones.UnavailableLookup())
        self.assertEqual(r["status"], "unknown")
        self.assertIn("not installed", r["reason"])
        self.assertIn("requirements-timezone.txt", r["reason"])

    def test_default_backend_matches_installation(self):
        try:
            import tzfpy  # noqa: F401
            installed = True
        except ImportError:
            installed = False
        self.assertEqual(timezones.default_lookup().available, installed)
        self.assertEqual(timezones.default_lookup().name, "tzfpy" if installed else "unavailable")

    def test_backend_can_be_disabled(self):
        old = os.environ.get("PHOTO_TOOL_TIMEZONE_LOOKUP")
        os.environ["PHOTO_TOOL_TIMEZONE_LOOKUP"] = "off"
        try:
            self.assertFalse(timezones.default_lookup().available)
        finally:
            if old is None:
                del os.environ["PHOTO_TOOL_TIMEZONE_LOOKUP"]
            else:
                os.environ["PHOTO_TOOL_TIMEZONE_LOOKUP"] = old

    def test_multiple_zones_any_match_is_consistent(self):
        class TwoZones(BoxLookup):
            def timezones_at(self, lat, lon):
                return ["Asia/Shanghai", "Asia/Urumqi"]
        raw = {"EXIF:ExifIFD:DateTimeOriginal": "2021:06:01 12:00:00", "EXIF:ExifIFD:OffsetTimeOriginal": "+06:00"}
        r = timezones.evaluate(CTF_LAT, CTF_LON, capture_time.extract_capture_time(raw), TwoZones())
        self.assertEqual((r["status"], r["matching_timezones"]), ("consistent", ["Asia/Urumqi"]))
        self.assertIn("More than one local time", r["note"])
        raw["EXIF:ExifIFD:OffsetTimeOriginal"] = "-05:00"
        r = timezones.evaluate(CTF_LAT, CTF_LON, capture_time.extract_capture_time(raw), TwoZones())
        self.assertEqual(r["status"], "inconsistent")

    def test_offset_sign_is_not_a_hemisphere(self):
        """A negative offset must not make a western candidate 'supported' without a timezone lookup."""
        record = make_record("a.jpg", gps_numeric(CTF_LAT, None, CTF_LON, None),
                             raw_meta(when="2021:11:20 07:36:35", offset="-05:00"))
        result = location_analysis.analyze_collection([record], timezones.UnavailableLookup())
        for c in result["images"][record.id]["candidates"]:
            self.assertEqual(c["timezone"]["status"], "unknown")
            self.assertFalse(c["assessment"]["has_contextual_support"])


# ============================================================================ collection

class CollectionContextTests(unittest.TestCase):

    def build(self):
        iphone = dict(make="Apple", model="iPhone 12")
        self.ambiguous = make_record("IMG_4014.JPG", gps_numeric(CTF_LAT, None, CTF_LON, None),
                                     raw_meta(when="2021:11:20 07:36:35", offset="-05:00", **iphone))
        self.near_same = make_record("IMG_4013.JPG", gps_numeric(38.560201, "N", 78.340102, "W"),
                                     raw_meta(when="2021:11:20 07:34:40", offset="-05:00", **iphone))
        self.unrelated = make_record("canon.jpg", gps_numeric(38.56, "N", 78.35, "E"),
                                     raw_meta(make="Canon", model="Canon EOS R6", when="2019:05:02 12:00:00", offset="+08:00"))
        return [self.ambiguous, self.near_same, self.unrelated]

    def candidates(self, result):
        return {c["id"]: c for c in result["images"][self.ambiguous.id]["candidates"]}

    def test_same_device_nearby_time_supports_one_candidate(self):
        result = location_analysis.analyze_collection(self.build(), timezones.UnavailableLookup())
        cands = self.candidates(result)
        nw = cands["N/W"]["collection"]
        self.assertEqual(nw["same_device_resolved_near"], 1)
        statement = nw["supporting"][0]["statement"]
        self.assertIn("same device model (not necessarily the same physical device) taken 2 minutes earlier", statement)
        self.assertLess(nw["supporting"][0]["distance_km"], 2.0)
        self.assertTrue(cands["N/W"]["assessment"]["has_contextual_support"])
        # mirrored candidates: the same-device photo implies impossible travel -> conflict, not support
        for mirror in ("N/E", "S/E", "S/W"):
            self.assertEqual(cands[mirror]["collection"]["supporting"], [])
            self.assertTrue(cands[mirror]["collection"]["conflicting"])

    def test_resolved_photo_close_to_one_candidate(self):
        result = location_analysis.analyze_collection(self.build(), timezones.UnavailableLookup())
        cands = self.candidates(result)
        self.assertEqual(cands["N/W"]["collection"]["closest_geographic_match"]["filename"], "IMG_4013.JPG")
        self.assertLess(cands["N/W"]["collection"]["closest_geographic_match"]["distance_km"], 2.0)

    def test_unrelated_device_is_not_strong_context(self):
        result = location_analysis.analyze_collection(self.build(), timezones.UnavailableLookup())
        ne = self.candidates(result)["N/E"]
        geo = ne["collection"]["closest_geographic_match"]
        self.assertEqual(geo["filename"], "canon.jpg")          # it IS geographically close...
        self.assertLess(geo["distance_km"], 3.0)
        self.assertEqual(ne["collection"]["supporting"], [])    # ...but gives no support
        self.assertEqual(ne["collection"]["same_device_resolved_near"], 0)
        self.assertFalse(any("nearby same-device" in s for s in ne["assessment"]["supporting_signals"]))
        self.assertTrue(any("different/unknown device" in s and "weak" in s for s in ne["collection"]["summary"]))

    def test_unrelated_device_only_gives_no_support(self):
        lone = make_record("lone.jpg", gps_numeric(CTF_LAT, None, CTF_LON, None),
                           raw_meta(when="2021:11:20 07:36:35", offset="-05:00"))
        other = make_record("other.jpg", gps_numeric(38.56, "N", 78.34, "W"),
                            raw_meta(make="Google", model="Pixel 6", when="2021:11:20 07:37:00", offset="-05:00"))
        result = location_analysis.analyze_collection([lone, other], timezones.UnavailableLookup())
        for c in result["images"][lone.id]["candidates"]:
            self.assertFalse(c["assessment"]["has_contextual_support"], c["id"])

    def test_context_never_promotes_ambiguous_gps(self):
        records = self.build()
        before = [(copy.deepcopy(r.normalized), copy.deepcopy(r.flags)) for r in records]
        result = location_analysis.analyze_collection(records, BoxLookup())
        self.assertEqual(before, [(r.normalized, r.flags) for r in records])   # nothing mutated
        self.assertEqual(self.ambiguous.normalized["gps_status"], "ambiguous")
        self.assertFalse(self.ambiguous.flags["gps"])
        image = result["images"][self.ambiguous.id]
        self.assertIsNone(image["resolved"]["location"])
        nw = self.candidates(result)["N/W"]
        self.assertEqual(nw["timezone"]["status"], "consistent")         # strongest possible context...
        self.assertTrue(nw["assessment"]["has_contextual_support"])
        self.assertEqual(nw["kind"], "derived_candidate")                 # ...still only a candidate
        self.assertIn("not confirmed", nw["assessment"]["status"])
        self.assertNotIn("valid", str(nw["assessment"]).lower().replace("invalid", ""))

    def test_pattern_across_ambiguous_images(self):
        records = [make_record(f"IMG_{i}.JPG", gps_numeric(CTF_LAT, "N", CTF_LON, None),
                               raw_meta(when=f"2021:11:20 07:3{i}:00", offset="-05:00")) for i in range(5)]
        records.append(make_record("PXL.jpg", gps_numeric(CTF_LAT, "N", CTF_LON, None), raw_meta(make="Google", model="Pixel 6")))
        result = location_analysis.analyze_collection(records, timezones.UnavailableLookup())
        self.assertEqual(len(result["patterns"]), 1)       # the single Pixel image is not a pattern
        pattern = result["patterns"][0]
        self.assertEqual(pattern["count"], 5)
        self.assertIn("5 image(s) from Apple iPhone 12", pattern["statement"])
        self.assertIn("GPSLongitudeRef missing", pattern["statement"])
        self.assertIn("does not mean", pattern["disclaimer"])


# ============================================================================ ordering

def _cand(cid, support=(), conflict=()):
    return {"id": cid, "assessment": {"supporting_signals": list(support), "conflicting_signals": list(conflict)}}


class OrderingTests(unittest.TestCase):

    def test_groups_and_stable_order(self):
        cands = [_cand("N/E", conflict=["tz"]), _cand("N/W", support=["tz"]), _cand("S/E"),
                 _cand("S/W", support=["photo"], conflict=["travel"])]
        ordering = location_analysis.order_candidates(cands)
        self.assertEqual([c["id"] for c in cands], ["N/W", "S/E", "S/W", "N/E"])
        self.assertEqual([c["order"]["group"] for c in cands], [1, 2, 3, 4])
        self.assertEqual(ordering["most_supported"], ["N/W"])
        self.assertIn("NOT a resolved or confirmed location", ordering["note"])

    def test_every_candidate_is_kept(self):
        cands = [_cand(x, conflict=["c"]) for x in ("N/E", "N/W", "S/E", "S/W")]
        location_analysis.order_candidates(cands)
        self.assertEqual(sorted(c["id"] for c in cands), ["N/E", "N/W", "S/E", "S/W"])

    def test_no_support_means_no_most_supported_label(self):
        cands = [_cand("N/E"), _cand("N/W")]
        ordering = location_analysis.order_candidates(cands)
        self.assertEqual(ordering["most_supported"], [])
        self.assertEqual([c["id"] for c in cands], ["N/E", "N/W"])      # generated order kept
        self.assertIn("none is marked", ordering["note"])

    def test_ties_are_reported(self):
        cands = [_cand("N/E", support=["a"]), _cand("N/W", support=["b"])]
        ordering = location_analysis.order_candidates(cands)
        self.assertEqual(ordering["most_supported"], ["N/E", "N/W"])
        self.assertIn("does not distinguish", ordering["note"])

    def test_no_scores_and_no_confirmation_wording(self):
        records = CollectionContextTests().build()
        result = location_analysis.analyze_collection(records, BoxLookup())
        image = result["images"][records[0].id]
        self.assertEqual([c["id"] for c in image["candidates"]][0], "N/W")
        for c in image["candidates"]:
            keys = " ".join(_all_keys(c)).lower()
            for forbidden in ("score", "probab", "confidence", "percent", "likelihood", "weight"):
                self.assertNotIn(forbidden, keys)
            # The candidate's OWN labels (statements about other, truly resolved photos may say "resolved").
            own = " | ".join([c["label"], c["status"], c["assessment"]["status"], c["order"]["label"],
                              c["order"]["reason"]]).lower()
            for forbidden in ("correct", "confirmed location", "resolved", "valid", "true location"):
                self.assertNotIn(forbidden, own.replace("not confirmed", ""))
            self.assertIn("not confirmed", c["assessment"]["status"])

    def test_evidence_categories(self):
        records = CollectionContextTests().build()
        result = location_analysis.analyze_collection(records, BoxLookup())
        cands = {c["id"]: c for c in result["images"][records[0].id]["candidates"]}
        cats = lambda cid: {e["category"] for e in cands[cid]["evidence"]}
        self.assertEqual(cats("N/W"), {"supporting"})
        self.assertIn("conflicting", cats("N/E"))
        self.assertIn("weak", cats("N/E"))                  # unrelated Canon nearby = weak only
        self.assertIn("unavailable", cats("S/W"))           # ocean: timezone not used
        self.assertTrue(all(e["category"] in ("supporting", "conflicting", "weak", "unavailable")
                            for c in cands.values() for e in c["evidence"]))


def _all_keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from _all_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _all_keys(inner)


# ============================================================================ optional tzfpy backend

try:
    import tzfpy  # noqa: F401
    HAVE_TZFPY = True
except ImportError:
    HAVE_TZFPY = False


@unittest.skipUnless(HAVE_TZFPY, "optional tzfpy not installed (pip install -r requirements-timezone.txt)")
class TzfpyBackendTests(unittest.TestCase):

    def setUp(self):
        self.lookup = timezones.default_lookup()

    def evaluate(self, lat, lon, when, offset):
        raw = {"EXIF:ExifIFD:DateTimeOriginal": when, "EXIF:ExifIFD:OffsetTimeOriginal": offset}
        return timezones.evaluate(lat, lon, capture_time.extract_capture_time(raw), self.lookup)

    def test_backend_metadata_and_attribution(self):
        info = self.lookup.info()
        self.assertTrue(info["available"])
        self.assertEqual(info["name"], "tzfpy")
        self.assertIn("ODbL", info["attribution"])
        self.assertIn("OpenStreetMap", info["attribution"])

    def test_ctf_candidates(self):
        when = "2021:11:20 07:36:35"
        nw = self.evaluate(CTF_LAT, -CTF_LON, when, "-05:00")
        self.assertEqual((nw["status"], nw["timezone"]), ("consistent", "America/New_York"))
        self.assertEqual(self.evaluate(CTF_LAT, CTF_LON, when, "-05:00")["status"], "inconsistent")
        for lat, lon in ((-CTF_LAT, CTF_LON), (-CTF_LAT, -CTF_LON)):      # oceans
            r = self.evaluate(lat, lon, when, "-05:00")
            self.assertEqual(r["status"], "unknown")
            self.assertIn("nautical", r["reason"])

    def test_dst_with_real_boundaries(self):
        self.assertEqual(self.evaluate(CTF_LAT, -CTF_LON, "2021:11:06 16:10:00", "-04:00")["status"], "consistent")
        self.assertEqual(self.evaluate(-33.8568, 151.2153, "2022:01:15 14:10:00", "+11:00")["timezone"], "Australia/Sydney")

    def test_xinjiang_two_local_times(self):
        when = "2021:06:01 12:00:00"
        for offset in ("+08:00", "+06:00"):
            r = self.evaluate(39.47, 75.99, when, offset)                  # Kashgar
            self.assertEqual(r["status"], "consistent", offset)
            self.assertEqual(set(r["timezones"]), {"Asia/Shanghai", "Asia/Urumqi"})
            self.assertIn("More than one local time", r["note"])
        self.assertEqual(self.evaluate(39.47, 75.99, when, "+05:00")["status"], "inconsistent")
        beijing = self.evaluate(39.90, 116.40, when, "+06:00")
        self.assertEqual(beijing["status"], "inconsistent")              # only Asia/Shanghai there

    def test_lookup_needs_no_network(self):
        """Run lookups in a fresh interpreter in which every socket operation raises."""
        script = "\n".join([
            "import socket",
            "def blocked(*a, **k): raise RuntimeError('NETWORK ACCESS ATTEMPTED')",
            "socket.socket.connect = blocked; socket.socket.connect_ex = blocked",
            "socket.create_connection = blocked; socket.getaddrinfo = blocked",
            f"import sys; sys.path.insert(0, {ROOT!r})",
            "from photo_tool import timezones",
            "lookup = timezones.default_lookup()",
            "assert lookup.available, 'tzfpy not active'",
            "assert lookup.timezones_at(38.551734, -78.333293) == ['America/New_York']",
            "print('OK', lookup.timezones_at(39.47, 75.99))",
        ])
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)


# ============================================================================ frontend safety (static)

class FrontendRenderingGuardTests(unittest.TestCase):
    """
    Leaflet renders STRING popup/tooltip content as HTML (innerHTML). Untrusted text
    (filenames, metadata) must therefore reach Leaflet as DOM nodes. This static check
    guards the code; the browser test (tests/browser/ui_checks.js) proves it with a
    real filename '<img src=x id=pwn>.jpg'.
    """

    def setUp(self):
        with open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8") as handle:
            self.source = handle.read()

    def test_leaflet_content_is_never_a_string(self):
        import re
        calls = re.findall(r"\.(bindTooltip|bindPopup|setContent|setTooltipContent|setPopupContent)\(\s*(.{0,40})", self.source)
        self.assertTrue(calls)
        for method, argument in calls:
            argument = argument.strip()
            self.assertTrue(argument.startswith("h(") or argument.startswith("() =>"),
                            f"{method}({argument}...) must receive a DOM node or a function returning one")

    def test_innerhtml_only_with_constants(self):
        import re
        for match in re.finditer(r"innerHTML\s*=\s*([^;\n]+)", self.source):
            self.assertRegex(match.group(1).strip(), r"^ICONS\[", f"innerHTML assignment: {match.group(0)}")
        for match in re.finditer(r"html:\s*([^,}\n]+)", self.source):
            self.assertRegex(match.group(1).strip(), r'^"[^"]*"$', "divIcon html must be a constant string")


# ============================================================================ API / end-to-end

def _write_jpeg(path, *exiftool_steps):
    Image.new("RGB", (32, 24), (120, 120, 120)).save(path, "JPEG")
    for step in exiftool_steps:
        subprocess.run([EXIFTOOL, "-q", "-q", "-m", "-overwrite_original", *step, path], check=True)


@unittest.skipUnless(EXIFTOOL and HAVE_PIL, "needs ExifTool and Pillow")
class ApiTests(unittest.TestCase):

    def setUp(self):
        from app import app
        self.client = app.test_client()
        app.testing = True
        self.headers = {"X-Photo-Tool": "1"}
        self.client.post("/api/clear", headers=self.headers)
        self.tmp = tempfile.mkdtemp(prefix="loc-api-")
        iphone = ["-Make=Apple", "-Model=iPhone 12"]
        gps_nw = ["-GPSLatitude=38.551734", "-GPSLatitudeRef=N", "-GPSLongitude=78.333293", "-GPSLongitudeRef=W"]
        files = {
            "ambiguous.jpg": [iphone + gps_nw + ["-DateTimeOriginal=2021:11:20 07:36:35", "-OffsetTimeOriginal=-05:00"],
                              ["-GPSLatitudeRef=", "-GPSLongitudeRef="]],
            "resolved_nw.jpg": [iphone + ["-GPSLatitude=38.560201", "-GPSLatitudeRef=N", "-GPSLongitude=78.340102",
                                          "-GPSLongitudeRef=W", "-DateTimeOriginal=2021:11:20 07:34:40", "-OffsetTimeOriginal=-05:00"]],
        }
        uploads = []
        for name, steps in files.items():
            path = os.path.join(self.tmp, name)
            _write_jpeg(path, *steps)
            with open(path, "rb") as handle:
                uploads.append((io.BytesIO(handle.read()), name))
        response = self.client.post("/api/upload", data={"files": uploads}, headers=self.headers,
                                    content_type="multipart/form-data")
        self.images = {i["filename"]: i for i in response.get_json()["images"]}

    def tearDown(self):
        self.client.post("/api/clear", headers=self.headers)
        shutil.rmtree(self.tmp, ignore_errors=True)
        timezones.set_lookup(None)

    def test_candidates_never_enter_resolved_dataset_or_counts(self):
        before = self.client.get("/api/images").get_json()["images"]
        timezones.set_lookup(BoxLookup())
        analysis = self.client.get("/api/location-analysis").get_json()
        after = self.client.get("/api/images").get_json()["images"]
        self.assertEqual(before, after)                      # summaries untouched
        self.assertEqual(sum(1 for i in after if i["flags"]["gps"]), 1)   # only the truly resolved image
        self.assertNotIn("candidates", str(after))
        amb = self.images["ambiguous.jpg"]
        self.assertEqual(analysis["counts"], {"ambiguous_images": 1, "candidates": 4, "resolved_images": 1})
        cands = {c["id"]: c for c in analysis["images"][amb["id"]]["candidates"]}
        self.assertEqual(cands["N/W"]["timezone"]["status"], "consistent")
        self.assertEqual(cands["N/W"]["collection"]["same_device_resolved_near"], 1)
        detail = self.client.get(f"/api/images/{amb['id']}").get_json()
        self.assertEqual(detail["normalized"]["gps_status"], "ambiguous")
        self.assertIsNone(detail["normalized"]["gps_latitude"])
        self.assertFalse(detail["flags"]["gps"])

    def test_sanitization_unchanged(self):
        amb = self.images["ambiguous.jpg"]
        response = self.client.post("/api/sanitize", json={"ids": [amb["id"]], "mode": "reencode"}, headers=self.headers)
        result = response.get_json()["results"][0]["sanitization"]
        self.assertTrue(result["verified_clean"])
        row = [r for r in result["field_comparison"] if r["field"] == "GPS coordinates"][0]
        self.assertEqual(row["status"], "removed")
        # analysis still describes the ORIGINAL evidence; the sanitized copy carries no GPS
        analysis = self.client.get("/api/location-analysis").get_json()
        self.assertEqual(len(analysis["images"][amb["id"]]["candidates"]), 4)
        self.assertEqual(self.client.get(f"/api/images/{amb['id']}").get_json()["normalized"]["gps_status"], "ambiguous")


if __name__ == "__main__":
    unittest.main()
