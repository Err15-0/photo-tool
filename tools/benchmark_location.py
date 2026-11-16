#!/usr/bin/env python3
"""
Benchmark: does the ambiguous-GPS investigation layer slow Photo Tool down?

    python tools/benchmark_location.py            # 10, 100, 1000 images
    python tools/benchmark_location.py 10 100     # custom sizes

Measures
  1. metadata extraction (real ExifTool, real files) per collection size,
     and how many ExifTool PROCESSES were started (must stay batched)
  2. the per-image cost of the new capture-time parsing (always runs)
  3. the on-demand /api/location-analysis computation (only runs when the
     investigator opens the Location tab or enables "Show possible locations")
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from photo_tool import capture_time, location_analysis, metadata, timezones  # noqa: E402
from photo_tool.workspace import ImageRecord, generate_image_id  # noqa: E402

EXIFTOOL = shutil.which("exiftool")


def make_templates(folder):
    """Three template JPEGs: resolved N/W, ambiguous (both refs missing), no GPS."""
    common = ["-Make=Apple", "-Model=iPhone 12", "-DateTimeOriginal=2021:11:20 07:36:35", "-OffsetTimeOriginal=-05:00"]
    gps = ["-GPSLatitude=38.551734", "-GPSLatitudeRef=N", "-GPSLongitude=78.333293", "-GPSLongitudeRef=W"]
    specs = {"resolved": [common + gps], "ambiguous": [common + gps, ["-GPSLatitudeRef=", "-GPSLongitudeRef="]],
             "nogps": [common]}
    paths = {}
    for name, steps in specs.items():
        path = os.path.join(folder, f"template_{name}.jpg")
        Image.new("RGB", (640, 480), (90, 120, 150)).save(path, "JPEG", quality=85)
        for step in steps:
            subprocess.run([EXIFTOOL, "-q", "-q", "-m", "-overwrite_original", *step, path], check=True)
        paths[name] = path
    return paths


def bench_extraction(sizes):
    print("1) Metadata extraction (real ExifTool, real files; 1/3 resolved, 1/3 ambiguous, 1/3 no GPS)")
    folder = tempfile.mkdtemp(prefix="bench-")
    try:
        templates = make_templates(folder)
        kinds = list(templates)
        for size in sizes:
            files = []
            for i in range(size):
                path = os.path.join(folder, f"img_{size}_{i:05d}.jpg")
                shutil.copyfile(templates[kinds[i % 3]], path)
                files.append(path)
            real_run = subprocess.run
            calls = []

            def counting_run(cmd, *args, **kwargs):
                calls.append(cmd[0])
                return real_run(cmd, *args, **kwargs)

            with mock.patch.object(metadata.subprocess, "run", counting_run):
                t0 = time.perf_counter()
                results = metadata.read_metadata_batch(files)
                t_exif = time.perf_counter() - t0
            t0 = time.perf_counter()
            for path in files:
                r = results[path]
                metadata.analyze_metadata(r["raw"], r["numeric"], r["xmp_packet"])
            t_analyze = time.perf_counter() - t0
            print(f"   {size:>5} images: ExifTool {t_exif:6.2f} s in {len(calls)} process(es) | "
                  f"Python analysis {t_analyze * 1000:7.1f} ms ({t_analyze / size * 1e6:5.0f} µs/image)")
            for path in files:
                os.remove(path)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def bench_capture_time():
    raw = {"EXIF:ExifIFD:DateTimeOriginal": "2021:11:20 07:36:35", "EXIF:ExifIFD:OffsetTimeOriginal": "-05:00",
           "EXIF:IFD0:Make": "Apple", "EXIF:IFD0:Model": "iPhone 12"}
    raw.update({f"EXIF:Other:Tag{i}": str(i) for i in range(80)})     # a realistic tag count
    n = 20000
    t0 = time.perf_counter()
    for _ in range(n):
        capture_time.extract_capture_time(raw)
    dt = time.perf_counter() - t0
    print(f"2) New capture-time parsing (always on): {dt / n * 1e6:.1f} µs per image (~80 tags)")


def synthetic_record(i, ambiguous):
    lat, lon = 38.55 + (i % 50) * 0.001, 78.33 + (i % 70) * 0.001
    numeric = {"EXIF:GPS:GPSLatitude": lat, "EXIF:GPS:GPSLongitude": lon, "EXIF:GPS:GPSVersionID": "2 3 0 0"}
    if not ambiguous:
        numeric.update({"EXIF:GPS:GPSLatitudeRef": "N", "EXIF:GPS:GPSLongitudeRef": "W",
                        "Composite:GPSLatitude": lat, "Composite:GPSLongitude": -lon})
    raw = {"EXIF:IFD0:Make": "Apple", "EXIF:IFD0:Model": f"iPhone {12 + i % 3}",
           "EXIF:ExifIFD:DateTimeOriginal": f"2021:11:20 {7 + i % 10:02d}:{i % 60:02d}:00",
           "EXIF:ExifIFD:OffsetTimeOriginal": "-05:00"}
    analysis = metadata.analyze_metadata(raw, numeric)
    record = ImageRecord(id=generate_image_id(), original_filename=f"IMG_{i:05d}.JPG", stored_extension="jpg",
                         image_format="JPEG", mime_type="image/jpeg", file_size=1, sha256=str(i), width=1, height=1,
                         display_width=1, display_height=1, frame_count=1, uploaded_at="")
    record.raw, record.normalized, record.flags = analysis["raw"], analysis["normalized"], analysis["flags"]
    return record


def bench_analysis(sizes):
    print("3) On-demand location analysis (/api/location-analysis), timezone lookup unavailable")
    for size in sizes:
        for share in (0.1, 0.5):
            ambiguous = int(size * share)
            records = [synthetic_record(i, i < ambiguous) for i in range(size)]
            t0 = time.perf_counter()
            result = location_analysis.analyze_collection(records, timezones.UnavailableLookup())
            dt = time.perf_counter() - t0
            print(f"   {size:>5} images ({ambiguous:>4} ambiguous × {size - ambiguous:>4} resolved): {dt * 1000:8.1f} ms, "
                  f"{result['counts']['candidates']} candidates")


if __name__ == "__main__":
    if not EXIFTOOL:
        sys.exit("ExifTool not found")
    sizes = [int(arg) for arg in sys.argv[1:]] or [10, 100, 1000]
    bench_extraction(sizes)
    bench_capture_time()
    bench_analysis(sizes)
