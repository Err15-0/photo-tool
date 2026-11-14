#!/usr/bin/env python3
"""
Automated end-to-end smoke test using Flask's test client (no browser needed).

    python tools/make_test_images.py          # once, creates ./sample_images
    python tools/smoke_test.py                # uses ./sample_images
    python tools/smoke_test.py /path/to/imgs  # or your own folder

It uploads every file, prints what was detected, sanitizes everything in both
modes, prints the verification result and builds a ZIP. Exit code 1 if any
sanitized file still contains sensitive metadata or a step fails.
"""

import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, workspace  # noqa: E402

HEADERS = {"X-Photo-Tool": "1"}


def collect_files(folder):
    paths = []
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            paths.append(os.path.join(root, name))
    return paths


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "sample_images"
    if not os.path.isdir(folder):
        sys.exit(f"Folder {folder!r} not found. Run: python tools/make_test_images.py")
    app.testing = True
    client = app.test_client()
    failures = 0

    paths = collect_files(folder)
    print(f"Uploading {len(paths)} file(s) from {folder}")
    data = {"files": [(open(p, "rb"), os.path.basename(p)) for p in paths]}
    response = client.post("/api/upload", data=data, headers=HEADERS, content_type="multipart/form-data")
    for handle, _ in data["files"]:
        handle.close()
    body = response.get_json()
    print(f"HTTP {response.status_code}: {len(body['images'])} accepted, {len(body['errors'])} rejected\n")

    for error in body["errors"]:
        print(f"  REJECTED {error['filename']}: {error['error']}")
    print()
    for image in body["images"]:
        n = image["normalized"]
        flags = [k for k, v in image["flags"].items() if v]
        gps = f"{n.get('gps_latitude')},{n.get('gps_longitude')}" if image["flags"].get("gps") else "-"
        print(f"  {image['filename']:<32} {image['format']:<5} device={n.get('device') or '-':<24} "
              f"time={n.get('timestamp') or '-':<20} gps={gps:<22} flags={','.join(flags)}")
        for warning in image["warnings"]:
            print(f"      warning: {warning}")

    for mode in ("reencode", "lossless"):
        print(f"\nSanitizing all images, mode={mode}")
        response = client.post("/api/sanitize", json={"all": True, "mode": mode}, headers=HEADERS)
        if response.status_code != 200:
            print("  FAILED:", response.get_json())
            failures += 1
            continue
        for result in response.get_json()["results"]:
            s = result["sanitization"]
            if s.get("error"):
                print(f"  ERROR {result['filename']}: {s['error']}")
                failures += 1
                continue
            status = "CLEAN" if s["verified_clean"] else "NOT CLEAN"
            print(f"  {status:<9} {result['filename']:<32} tags {s['before_tag_count']:>3} -> "
                  f"{s['after_tag_count']:<3} removed={len(s['removed_tags']):<3} "
                  f"image_check={s['image_check']['message']}")
            if not s["verified_clean"]:
                failures += 1
                for category, tags in s["remaining_sensitive"].items():
                    for tag in tags:
                        print(f"      still present [{category}] {tag['key']} = {tag['value']}")

    response = client.post("/download/zip", data={"all": "1"})
    if response.status_code == 200:
        names = zipfile.ZipFile(io.BytesIO(response.data)).namelist()
        print(f"\nZIP OK: {len(names)} files, e.g. {names[:4]}")
    else:
        print("\nZIP FAILED:", response.status_code, response.data[:200])
        failures += 1

    # Originals must be untouched.
    for record in workspace.all():
        with open(record.paths["original"], "rb") as handle:
            import hashlib
            if hashlib.sha256(handle.read()).hexdigest() != record.sha256:
                print("ORIGINAL MODIFIED:", record.original_filename)
                failures += 1

    print("\nRESULT:", "PASS" if failures == 0 else f"{failures} problem(s)")
    workspace.destroy()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
