"""
Regression tests for GPS hemisphere handling (photo_tool/gps.py).

Principle under test: preserve uncertainty rather than invent certainty.
Coordinates whose hemisphere cannot be determined must be "ambiguous",
must keep their raw values, and must NEVER be plotted (flags.gps False).

Run from the project root:

    python -m unittest discover -s tests -v

Level 1 (InterpretGpsUnitTests) needs nothing but Python.
Level 2/3 (ExifToolIntegrationTests, ApiTests) write real JPEGs with ExifTool
and are skipped automatically if ExifTool or Pillow is not installed.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from photo_tool.gps import interpret_gps  # noqa: E402

EXIFTOOL = shutil.which("exiftool")
try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def exif_numeric(lat=None, lat_ref=None, lon=None, lon_ref=None, composite=True):
    """
    Build ExifTool `-json -n -G0:1` style output for EXIF GPS.

    Mirrors ExifTool 12.76: Composite:GPSLatitude/Longitude only exist when the
    Ref tag exists (and a malformed Ref still yields a POSITIVE composite).
    """
    numeric = {"EXIF:GPS:GPSVersionID": "2 3 0 0"}
    if lat is not None:
        numeric["EXIF:GPS:GPSLatitude"] = lat
    if lat_ref is not None:
        numeric["EXIF:GPS:GPSLatitudeRef"] = lat_ref
    if lon is not None:
        numeric["EXIF:GPS:GPSLongitude"] = lon
    if lon_ref is not None:
        numeric["EXIF:GPS:GPSLongitudeRef"] = lon_ref
    if composite:
        if lat is not None and lat_ref is not None:
            numeric["Composite:GPSLatitude"] = -lat if str(lat_ref).upper().startswith("S") else lat
        if lon is not None and lon_ref is not None:
            numeric["Composite:GPSLongitude"] = -lon if str(lon_ref).upper().startswith("W") else lon
    if lat is not None and lon is not None:
        # ExifTool builds GPSPosition even WITHOUT refs (implicit N/E) - must be ignored.
        numeric["Composite:GPSPosition"] = f"{numeric.get('Composite:GPSLatitude', lat)} {numeric.get('Composite:GPSLongitude', lon)}"
    return numeric


XMP_PACKET = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="" xmlns:exif="http://ns.adobe.com/exif/1.0/">'
    "<exif:GPSLatitude>{lat}</exif:GPSLatitude><exif:GPSLongitude>{lon}</exif:GPSLongitude>"
    "</rdf:Description></rdf:RDF></x:xmpmeta>"
)


class InterpretGpsUnitTests(unittest.TestCase):
    """Pure logic, synthetic ExifTool output."""

    def assertResolved(self, result, lat, lon):
        self.assertEqual(result["status"], "valid", result)
        self.assertAlmostEqual(result["latitude"], lat, places=6)
        self.assertAlmostEqual(result["longitude"], lon, places=6)
        self.assertEqual(result["missing_refs"], [])

    def assertAmbiguous(self, result, missing, raw_lat, raw_lon):
        self.assertEqual(result["status"], "ambiguous", result)
        self.assertIsNone(result["latitude"], "ambiguous GPS must not have a signed latitude")
        self.assertIsNone(result["longitude"], "ambiguous GPS must not have a signed longitude")
        self.assertEqual(result["missing_refs"], missing)
        self.assertAlmostEqual(result["raw_latitude"], raw_lat)      # raw values stay visible
        self.assertAlmostEqual(result["raw_longitude"], raw_lon)
        self.assertIn("hemisphere reference is missing", result["message"])
        self.assertIn("cannot be determined reliably", result["message"])

    # ---- the four hemispheres -------------------------------------------------
    def test_north_east(self):
        self.assertResolved(interpret_gps(exif_numeric(52.5163, "N", 13.3777, "E")), 52.5163, 13.3777)

    def test_north_west(self):
        self.assertResolved(interpret_gps(exif_numeric(33.4484, "N", 112.074, "W")), 33.4484, -112.074)

    def test_south_east(self):
        self.assertResolved(interpret_gps(exif_numeric(33.8688, "S", 151.2093, "E")), -33.8688, 151.2093)

    def test_south_west(self):
        self.assertResolved(interpret_gps(exif_numeric(22.9068, "S", 43.1729, "W")), -22.9068, -43.1729)

    def test_full_word_references(self):
        self.assertResolved(interpret_gps(exif_numeric(22.9068, "South", 43.1729, "West", composite=False)),
                            -22.9068, -43.1729)

    # ---- missing references: NEVER default to N/E -------------------------------
    def test_missing_latitude_ref(self):
        result = interpret_gps(exif_numeric(33.4484, None, 112.074, "W"))
        self.assertAmbiguous(result, ["GPSLatitudeRef"], 33.4484, 112.074)
        self.assertEqual(result["longitude_ref"], "W")
        self.assertIsNone(result["latitude_ref"])

    def test_missing_longitude_ref(self):
        result = interpret_gps(exif_numeric(33.4484, "N", 112.074, None))
        self.assertAmbiguous(result, ["GPSLongitudeRef"], 33.4484, 112.074)
        self.assertEqual(result["latitude_ref"], "N")

    def test_both_refs_missing(self):
        result = interpret_gps(exif_numeric(33.4484, None, 112.074, None))
        self.assertAmbiguous(result, ["GPSLatitudeRef", "GPSLongitudeRef"], 33.4484, 112.074)
        self.assertIn("?", result["display"])

    def test_empty_ref_counts_as_missing(self):
        self.assertAmbiguous(interpret_gps(exif_numeric(33.4484, "", 112.074, "W", composite=False)),
                             ["GPSLatitudeRef"], 33.4484, 112.074)

    def test_composite_without_ref_is_ignored(self):
        """Even if some ExifTool version produced a composite without a Ref, it is not trusted."""
        numeric = exif_numeric(33.4484, None, 112.074, None)
        numeric["Composite:GPSLatitude"] = 33.4484
        numeric["Composite:GPSLongitude"] = 112.074
        self.assertEqual(interpret_gps(numeric)["status"], "ambiguous")

    def test_malformed_ref_is_ambiguous_even_with_composite(self):
        """ExifTool turns Ref 'X' into a POSITIVE composite (and prints 'N'): must not be trusted."""
        numeric = exif_numeric(33.4484, "X", 112.074, "W")
        self.assertEqual(numeric["Composite:GPSLatitude"], 33.4484)   # the trap exists in the input
        result = interpret_gps(numeric)
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["latitude"])
        self.assertTrue(any("'X'" in item for item in result["invalid_refs"]))

    def test_zero_axis_needs_no_hemisphere(self):
        """0 degrees is unambiguous: the equator needs no N/S."""
        result = interpret_gps(exif_numeric(0.0, None, 78.5, "W"))
        self.assertResolved(result, 0.0, -78.5)

    # ---- no GPS / incomplete / invalid / zero ---------------------------------
    def test_no_gps(self):
        result = interpret_gps({"EXIF:IFD0:Make": "Canon", "File:FileType": "JPEG"})
        self.assertIsNone(result["status"])
        self.assertIsNone(result["latitude"])
        self.assertIsNone(result["raw_latitude"])

    def test_only_latitude_is_incomplete(self):
        result = interpret_gps(exif_numeric(33.4484, "N", None, None))
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["latitude"])

    def test_gps_tags_without_coordinates_are_incomplete(self):
        self.assertEqual(interpret_gps({"EXIF:GPS:GPSVersionID": "2 3 0 0"})["status"], "incomplete")

    def test_out_of_range_is_invalid(self):
        self.assertEqual(interpret_gps(exif_numeric(95.0, "N", 10.0, "E"))["status"], "invalid")

    def test_zero_zero_placeholder(self):
        self.assertEqual(interpret_gps(exif_numeric(0.0, "N", 0.0, "E"))["status"], "zero")

    # ---- signed / composite sources -------------------------------------------
    def test_signed_composite_is_preferred_when_reference_verified(self):
        result = interpret_gps(exif_numeric(22.9068, "S", 43.1729, "W"))
        self.assertResolved(result, -22.9068, -43.1729)
        self.assertIn("Composite", result["source"])

    def test_reference_used_when_composite_absent(self):
        result = interpret_gps(exif_numeric(22.9068, "S", 43.1729, "W", composite=False))
        self.assertResolved(result, -22.9068, -43.1729)
        self.assertEqual(result["source"], "EXIF GPS + reference tags")

    def test_xmp_signed_with_hemisphere_letters(self):
        numeric = {"XMP:XMP-exif:GPSLatitude": -22.9068, "XMP:XMP-exif:GPSLongitude": -43.1729}
        packet = XMP_PACKET.format(lat="22,54.408S", lon="43,10.374W")
        result = interpret_gps(numeric, packet)
        self.assertResolved(result, -22.9068, -43.1729)
        self.assertTrue(result["source"].startswith("XMP"))

    def test_xmp_without_hemisphere_letter_is_ambiguous(self):
        """ExifTool reports this as positive and prints an invented 'N'/'E'."""
        numeric = {"XMP:XMP-exif:GPSLatitude": 22.9068, "XMP:XMP-exif:GPSLongitude": 43.1729}
        packet = XMP_PACKET.format(lat="22,54.408", lon="43,10.374")
        result = interpret_gps(numeric, packet)
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["latitude"])

    def test_xmp_attribute_form(self):
        numeric = {"XMP:XMP-exif:GPSLatitude": -22.9068, "XMP:XMP-exif:GPSLongitude": -43.1729}
        packet = '<rdf:Description exif:GPSLatitude="22,54.408S" exif:GPSLongitude="43,10.374W"/>'
        self.assertResolved(interpret_gps(numeric, packet), -22.9068, -43.1729)

    def test_xmp_without_packet_is_not_trusted(self):
        numeric = {"XMP:XMP-exif:GPSLatitude": -22.9068, "XMP:XMP-exif:GPSLongitude": -43.1729}
        self.assertEqual(interpret_gps(numeric, None)["status"], "ambiguous")

    def test_ambiguous_exif_resolved_by_matching_xmp_record(self):
        numeric = exif_numeric(22.9068, None, 43.1729, None)
        numeric.update({"XMP:XMP-exif:GPSLatitude": -22.9068, "XMP:XMP-exif:GPSLongitude": -43.1729})
        result = interpret_gps(numeric, XMP_PACKET.format(lat="22,54.408S", lon="43,10.374W"))
        self.assertResolved(result, -22.9068, -43.1729)
        self.assertTrue(any("XMP" in note for note in result["notes"]))

    def test_ambiguous_exif_not_resolved_by_different_xmp_coordinates(self):
        numeric = exif_numeric(33.4484, None, 112.074, None)
        numeric.update({"XMP:XMP-exif:GPSLatitude": -22.9068, "XMP:XMP-exif:GPSLongitude": -43.1729})
        result = interpret_gps(numeric, XMP_PACKET.format(lat="22,54.408S", lon="43,10.374W"))
        self.assertEqual(result["status"], "ambiguous")

    def test_no_inference_from_context(self):
        """Timezone / locale / filename-like metadata must not resolve a hemisphere."""
        numeric = exif_numeric(33.4484, None, 112.074, None)
        numeric.update({
            "EXIF:ExifIFD:OffsetTimeOriginal": "-07:00",
            "XMP:XMP-photoshop:Country": "United States",
            "XMP:XMP-iptcCore:CountryCode": "US",
            "MakerNotes:Apple:Locale": "en_US",
        })
        self.assertEqual(interpret_gps(numeric)["status"], "ambiguous")


def _write_jpeg(path):
    Image.new("RGB", (32, 24), (120, 120, 120)).save(path, "JPEG")


def _exiftool(path, *args):
    subprocess.run([EXIFTOOL, "-q", "-q", "-m", "-overwrite_original", *args, path], check=True)


def gps_args(lat, lat_ref, lon, lon_ref):
    return [f"-GPSLatitude={lat}", f"-GPSLatitudeRef={lat_ref}", f"-GPSLongitude={lon}", f"-GPSLongitudeRef={lon_ref}"]


# name -> (exiftool write args, exiftool args applied afterwards (deleting refs), expected)
REAL_FILE_CASES = {
    "ne": (gps_args(52.5163, "N", 13.3777, "E"), [], ("valid", 52.5163, 13.3777)),
    "nw": (gps_args(33.4484, "N", 112.074, "W"), [], ("valid", 33.4484, -112.074)),
    "se": (gps_args(33.8688, "S", 151.2093, "E"), [], ("valid", -33.8688, 151.2093)),
    "sw": (gps_args(22.9068, "S", 43.1729, "W"), [], ("valid", -22.9068, -43.1729)),
    "missing_lat_ref": (gps_args(33.4484, "N", 112.074, "W"), ["-GPSLatitudeRef="], ("ambiguous", None, None)),
    "missing_lon_ref": (gps_args(33.4484, "N", 112.074, "W"), ["-GPSLongitudeRef="], ("ambiguous", None, None)),
    "missing_both_refs": (gps_args(33.4484, "N", 112.074, "W"), ["-GPSLatitudeRef=", "-GPSLongitudeRef="], ("ambiguous", None, None)),
    "malformed_ref": (gps_args(33.4484, "N", 112.074, "W"), ["-GPSLatitudeRef#=X"], ("ambiguous", None, None)),
    "xmp_signed": (["-XMP-exif:GPSLatitude=-22.9068", "-XMP-exif:GPSLongitude=-43.1729"], [], ("valid", -22.9068, -43.1729)),
    "no_gps": (["-Make=Canon"], [], (None, None, None)),
}


@unittest.skipUnless(EXIFTOOL and HAVE_PIL, "needs ExifTool and Pillow")
class ExifToolIntegrationTests(unittest.TestCase):
    """Real files -> real ExifTool -> read_metadata_batch -> analyze_metadata."""

    @classmethod
    def setUpClass(cls):
        from photo_tool.metadata import analyze_metadata, read_metadata_batch
        cls.tmp = tempfile.mkdtemp(prefix="gps-test-")
        paths = {}
        for name, (args, after, _expected) in REAL_FILE_CASES.items():
            path = os.path.join(cls.tmp, f"{name}.jpg")
            _write_jpeg(path)
            _exiftool(path, *args)
            if after:
                _exiftool(path, *after)
            paths[name] = path
        # XMP coordinate without hemisphere letter: only possible via a raw packet.
        path = os.path.join(cls.tmp, "xmp_no_letter.jpg")
        _write_jpeg(path)
        xmp_file = os.path.join(cls.tmp, "no_letter.xmp")
        with open(xmp_file, "w") as handle:
            handle.write(XMP_PACKET.format(lat="22,54.408", lon="43,10.374"))
        _exiftool(path, f"-XMP<={xmp_file}")
        paths["xmp_no_letter"] = path

        metadata = read_metadata_batch(list(paths.values()))
        cls.analysis = {}
        for name, path in paths.items():
            result = metadata[path]
            cls.analysis[name] = analyze_metadata(result["raw"], result["numeric"], result["xmp_packet"])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_all_cases(self):
        for name, (_args, _after, (status, lat, lon)) in REAL_FILE_CASES.items():
            with self.subTest(case=name):
                a = self.analysis[name]
                n = a["normalized"]
                self.assertEqual(n["gps_status"], status)
                if status == "valid":
                    self.assertAlmostEqual(n["gps_latitude"], lat, places=4)
                    self.assertAlmostEqual(n["gps_longitude"], lon, places=4)
                    self.assertTrue(a["flags"]["gps"])
                else:
                    self.assertIsNone(n["gps_latitude"])
                    self.assertFalse(a["flags"]["gps"], "only resolved GPS may be plotted")
                if status == "ambiguous":
                    self.assertTrue(a["flags"]["gps_ambiguous"])
                    self.assertIsNotNone(n["gps_raw_latitude"])
                    self.assertIsNotNone(n["gps_raw_longitude"])
                    self.assertTrue(any("cannot be determined reliably" in w for w in a["warnings"]))

    def test_missing_ref_names_are_reported(self):
        self.assertEqual(self.analysis["missing_lat_ref"]["normalized"]["gps_missing_refs"], ["GPSLatitudeRef"])
        self.assertEqual(self.analysis["missing_lon_ref"]["normalized"]["gps_missing_refs"], ["GPSLongitudeRef"])
        self.assertEqual(self.analysis["missing_both_refs"]["normalized"]["gps_missing_refs"],
                         ["GPSLatitudeRef", "GPSLongitudeRef"])

    def test_xmp_without_letter_is_ambiguous(self):
        n = self.analysis["xmp_no_letter"]["normalized"]
        self.assertEqual(n["gps_status"], "ambiguous")
        self.assertFalse(self.analysis["xmp_no_letter"]["flags"]["gps"])


@unittest.skipUnless(EXIFTOOL and HAVE_PIL, "needs ExifTool and Pillow")
class ApiTests(unittest.TestCase):
    """Through the Flask app: summaries drive the map (flags.gps) and sanitize verification."""

    def setUp(self):
        from app import app, workspace
        self.app, self.workspace = app, workspace
        self.app.testing = True
        self.client = app.test_client()
        self.client.post("/api/clear", headers={"X-Photo-Tool": "1"})
        self.tmp = tempfile.mkdtemp(prefix="gps-api-")

    def tearDown(self):
        self.client.post("/api/clear", headers={"X-Photo-Tool": "1"})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _upload(self, name, *exiftool_steps):
        path = os.path.join(self.tmp, name)
        _write_jpeg(path)
        for step in exiftool_steps:
            _exiftool(path, *step)
        with open(path, "rb") as handle:
            data = {"files": [(io.BytesIO(handle.read()), name)]}
        response = self.client.post("/api/upload", data=data, headers={"X-Photo-Tool": "1"},
                                    content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["images"][0]

    def test_ambiguous_upload_is_not_mappable_and_removal_is_verified(self):
        valid = self._upload("valid.jpg", gps_args(22.9068, "S", 43.1729, "W"))
        ambiguous = self._upload("ambiguous.jpg", gps_args(33.4484, "N", 112.074, "W"), ["-GPSLatitudeRef="])

        self.assertTrue(valid["flags"]["gps"])
        self.assertAlmostEqual(valid["normalized"]["gps_latitude"], -22.9068, places=4)

        # The map only creates markers for flags.gps (static/js/app.js renderMapMarkers).
        self.assertFalse(ambiguous["flags"]["gps"])
        self.assertTrue(ambiguous["flags"]["gps_ambiguous"])
        self.assertIsNone(ambiguous["normalized"]["gps_latitude"])
        self.assertIsNone(ambiguous["normalized"]["gps_longitude"])
        self.assertIn("?", ambiguous["normalized"]["gps_display"])

        # Sanitizing must report the ambiguous GPS values as REMOVED (not "not present").
        response = self.client.post("/api/sanitize", json={"ids": [ambiguous["id"]], "mode": "reencode"},
                                    headers={"X-Photo-Tool": "1"})
        result = response.get_json()["results"][0]["sanitization"]
        self.assertTrue(result["verified_clean"])
        gps_row = [r for r in result["field_comparison"] if r["field"] == "GPS coordinates"][0]
        self.assertEqual(gps_row["status"], "removed")
        self.assertIn("ambiguous", gps_row["before"])


if __name__ == "__main__":
    unittest.main()
