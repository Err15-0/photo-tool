#!/usr/bin/env python3
"""
Generate a demo / test batch of images with realistic metadata.

    python tools/make_test_images.py            # writes into ./sample_images/
    python tools/make_test_images.py /some/dir  # custom output directory

Needs Pillow (pip) and ExifTool (apt: libimage-exiftool-perl).
All metadata below is FAKE and written by ExifTool onto generated images.

The batch covers the manual test checklist in README.md:
  - JPEG with EXIF but no GPS
  - JPEGs with GPS in several cities (two from the same phone -> path on map)
  - duplicate filenames in two sub-folders + an exact byte-identical copy
  - PNG without any metadata
  - PNG with text chunks (software, author, AI prompt)
  - corrupted JPEG (truncated) and a text file renamed to .jpg
  - rotated JPEG (EXIF Orientation = 6)
  - WebP and TIFF with metadata
  - "edited" JPEG with Photoshop history + document IDs
  - JPEG with an embedded EXIF thumbnail (classic OPSEC leak)
  - JPEG with bogus 0,0 GPS
  - PNG file misnamed as .jpg
  - HEIC "iPhone" photos with GPS (only if pillow-heif is installed)
  - GPS hemisphere edge cases (CTF-style): S/E and S/W photos, and photos whose
    GPSLatitudeRef / GPSLongitudeRef is missing or malformed, plus an XMP
    coordinate without its N/S/E/W letter -> must show as "ambiguous GPS"
"""

import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

OUTPUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "sample_images")
EXIFTOOL = shutil.which("exiftool")

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:            # very old Pillow
        return ImageFont.load_default()


def make_picture(path, label, color, size=(1200, 800), fmt=None, mode="RGB"):
    """Create a simple gradient picture with a big label."""
    width, height = size
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    for y in range(0, height, 4):
        shade = int(255 * y / height)
        draw.line([(0, y), (width, y)], fill=(color[0], (color[1] + shade) % 256, color[2]), width=4)
    draw.rectangle([40, 40, width - 40, 200], fill=(255, 255, 255))
    draw.text((60, 70), label, fill=(20, 20, 20), font=font(64))
    draw.text((60, height - 90), "TOP-LEFT ARROW ->", fill=(255, 255, 255), font=font(40))
    if mode != "RGB":
        image = image.convert(mode)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    image.save(path, fmt)
    return path


def exiftool(path, *tags):
    """Write tags with ExifTool (argument list, no shell)."""
    subprocess.run([EXIFTOOL, "-q", "-q", "-m", "-overwrite_original", *tags, path], check=True)


def gps_tags(lat, lon, alt=None):
    tags = [
        f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
        f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
    ]
    if alt is not None:
        tags += [f"-GPSAltitude={abs(alt)}", f"-GPSAltitudeRef={0 if alt >= 0 else 1}"]
    return tags


def make_ctf_collection(out):
    """
    An iPhone series in the Shenandoah Valley (Virginia, 38.55 N / 78.33 W) where
    some photos lost their GPS hemisphere references, plus control cases.
    Expected: ambiguous photos get 2 or 4 DERIVED candidates; the N/W candidate is
    supported by resolved photos of the same device minutes apart; the mirrored
    N/E candidate (Xinjiang) only has an UNRELATED Canon photo nearby.
    """
    iphone = ["-Make=Apple", "-Model=iPhone 12", "-Software=15.1"]

    def photo(name, label, color, when, offset, gps, drop_refs=(), extra=()):
        path = make_picture(out(f"ctf_collection/{name}"), label, color, size=(800, 600))
        args = list(extra)
        if when:
            args += [f"-DateTimeOriginal={when}", f"-CreateDate={when}"]
        if offset:
            args += [f"-OffsetTimeOriginal={offset}", f"-OffsetTimeDigitized={offset}"]
        if gps:
            args += gps_tags(*gps)
        exiftool(path, *args)
        if drop_refs:
            exiftool(path, *[f"-{ref}=" for ref in drop_refs])
        return path

    both = ("GPSLatitudeRef", "GPSLongitudeRef")
    photo("IMG_4012.JPG", "07:31 refs missing", (60, 90, 40), "2021:11:20 07:31:12", "-05:00",
          (38.551734, -78.333293), both, iphone)
    photo("IMG_4013.JPG", "07:34 resolved N/W", (70, 110, 50), "2021:11:20 07:34:40", "-05:00",
          (38.560201, -78.340102), (), iphone)
    photo("IMG_4014.JPG", "07:36 CTF case", (80, 120, 60), "2021:11:20 07:36:35", "-05:00",
          (38.551734, -78.333293), both, iphone)
    photo("IMG_4015.JPG", "07:42 resolved N/W", (90, 130, 70), "2021:11:20 07:42:05", "-05:00",
          (38.544917, -78.319844), (), iphone)
    photo("IMG_4016.JPG", "07:50 LongitudeRef missing", (100, 140, 80), "2021:11:20 07:50:30", "-05:00",
          (38.549100, -78.338000), ("GPSLongitudeRef",), iphone)
    # DST-sensitive: two weeks earlier New York was still on -04:00
    photo("IMG_3901.JPG", "Nov 6 (DST) refs missing", (120, 90, 40), "2021:11:06 16:10:00", "-04:00",
          (38.600000, -78.400000), both, iphone)
    # Malformed offset / no capture time at all
    photo("IMG_4020.JPG", "offset +25:00", (120, 60, 60), "2021:11:20 09:00:00", None,
          (38.551734, -78.333293), both, iphone + ["-OffsetTimeOriginal=+25:00"])
    photo("IMG_4021.JPG", "no capture time", (90, 60, 90), None, None,
          (38.551734, -78.333293), both, iphone)
    # Unrelated device, years earlier, close to the MIRRORED N/E candidate (Xinjiang)
    photo("canon_kashgar_2019.jpg", "unrelated Canon", (150, 120, 60), "2019:05:02 12:00:00", "+08:00",
          (38.560000, 78.350000), (), ["-Make=Canon", "-Model=Canon EOS R6"])
    # Southern hemisphere series: Sydney, longitude reference missing
    pixel = ["-Make=Google", "-Model=Pixel 6"]
    photo("PXL_20220115_141000.jpg", "Sydney LongitudeRef missing", (40, 90, 130), "2022:01:15 14:10:00", "+11:00",
          (-33.856800, 151.215300), ("GPSLongitudeRef",), pixel)
    photo("PXL_20220115_142500.jpg", "Sydney resolved", (50, 100, 140), "2022:01:15 14:25:00", "+11:00",
          (-33.859000, 151.214000), (), pixel)


def main():
    if not EXIFTOOL:
        sys.exit("ExifTool not found. Install it: sudo apt install libimage-exiftool-perl")
    os.makedirs(OUTPUT, exist_ok=True)
    out = lambda name: os.path.join(OUTPUT, name)

    # 1. EXIF but no GPS
    p = make_picture(out("01_exif_no_gps.jpg"), "EXIF, no GPS", (90, 60, 140))
    exiftool(p, "-Make=Canon", "-Model=Canon EOS 5D Mark IV", "-LensModel=EF24-70mm f/2.8L II USM",
             "-SerialNumber=032021001234", "-DateTimeOriginal=2024:03:15 09:12:44",
             "-CreateDate=2024:03:15 09:12:44", "-Artist=Jane Photographer",
             "-Copyright=(c) 2024 Jane Photographer", "-Software=Canon EOS 5D Mark IV Firmware 1.3.2")

    # 2-6. GPS photos (two from the same phone in Phoenix -> chronological path)
    gps_photos = [
        ("02_gps_phoenix_morning.jpg", "Phoenix 08:01", (200, 90, 40), "Apple", "iPhone 13 Pro",
         "17.1.2", (33.4484, -112.0740, 331.0), "2024:06:01 08:01:10", "-07:00"),
        ("03_gps_phoenix_noon.jpg", "Phoenix 12:30", (210, 120, 40), "Apple", "iPhone 13 Pro",
         "17.1.2", (33.4255, -111.9400, 360.5), "2024:06:01 12:30:05", "-07:00"),
        ("04_gps_berlin.jpg", "Berlin", (40, 90, 160), "samsung", "SM-G991B",
         "G991BXXU5CVLL", (52.5163, 13.3777, 34.0), "2023:11:20 16:45:00", "+01:00"),
        ("05_gps_paris.jpg", "Paris", (40, 140, 90), "Google", "Pixel 8",
         "HDR+ 1.0.5", (48.8584, 2.2945, 35.0), "2023:08:02 19:20:33", "+02:00"),
        ("06_gps_tokyo.jpg", "Tokyo", (160, 40, 90), "SONY", "ILCE-7M3",
         "ILCE-7M3 v4.01", (35.6586, 139.7454, 40.0), "2022:04:10 07:05:59", "+09:00"),
    ]
    for name, label, color, make, model, software, (lat, lon, alt), when, offset in gps_photos:
        p = make_picture(out(name), label, color)
        exiftool(p, f"-Make={make}", f"-Model={model}", f"-Software={software}",
                 f"-DateTimeOriginal={when}", f"-CreateDate={when}", f"-ModifyDate={when}",
                 f"-OffsetTimeOriginal={offset}", *gps_tags(lat, lon, alt))
    exiftool(out("02_gps_phoenix_morning.jpg"), "-HostComputer=iPhone 13 Pro",
             "-LensModel=iPhone 13 Pro back triple camera 5.7mm f/1.5")

    # 7. Duplicate filenames in two folders + exact byte-identical copy
    p = make_picture(out("folder_a/photo.jpg"), "photo.jpg (folder A)", (120, 120, 30))
    exiftool(p, "-Make=Apple", "-Model=iPhone 13 Pro", "-DateTimeOriginal=2024:06:02 10:00:00",
             *gps_tags(33.5, -112.1))
    p = make_picture(out("folder_b/photo.jpg"), "photo.jpg (folder B)", (30, 120, 120))
    exiftool(p, "-Make=Google", "-Model=Pixel 8", "-DateTimeOriginal=2023:08:03 11:00:00")
    shutil.copyfile(out("folder_a/photo.jpg"), out("folder_b/photo_copy_identical.jpg"))

    # 8. PNG without metadata
    make_picture(out("08_no_metadata.png"), "PNG, no metadata", (70, 70, 70), fmt="PNG")

    # 9. PNG with text chunks (software / author / AI prompt)
    p = make_picture(out("09_png_text_chunks.png"), "PNG text chunks", (20, 100, 120), fmt="PNG")
    exiftool(p, "-PNG:Software=GIMP 2.10.36", "-PNG:Author=j.doe", "-PNG:Comment=meet at the old bridge",
             "-PNG:CreationTime=2024:01:05 22:10:00", "-XMP:CreatorTool=GIMP 2.10.36")

    # 10. Corrupted JPEG (truncated) and a text file pretending to be a JPEG
    with open(out("01_exif_no_gps.jpg"), "rb") as source:
        data = source.read()
    with open(out("10_corrupted_truncated.jpg"), "wb") as broken:
        broken.write(data[: len(data) // 3])
    with open(out("11_not_an_image.jpg"), "w") as fake:
        fake.write("This is plain text, not a JPEG.\n")

    # 12. Rotated JPEG (stored landscape, displayed portrait)
    p = make_picture(out("12_rotated_orientation6.jpg"), "Orientation=6", (150, 150, 200))
    exiftool(p, "-Orientation#=6", "-Make=Apple", "-Model=iPhone 12", "-DateTimeOriginal=2021:12:24 18:00:00")

    # 13. WebP and TIFF with metadata
    p = make_picture(out("13_webp_with_exif.webp"), "WebP + EXIF", (100, 40, 40), fmt="WEBP")
    exiftool(p, "-Make=Xiaomi", "-Model=Redmi Note 12", "-DateTimeOriginal=2024:02:14 14:14:14",
             *gps_tags(41.9028, 12.4964))
    p = make_picture(out("14_tiff_with_artist.tif"), "TIFF + Artist", (40, 40, 100), size=(800, 600), fmt="TIFF")
    exiftool(p, "-Artist=Scanner Operator", "-Software=VueScan 9.8", "-Make=Epson",
             "-Model=Perfection V600", "-DateTime=2020:05:05 05:05:05")

    # 15. "Edited" photo with Photoshop history and document IDs
    p = make_picture(out("15_edited_photoshop.jpg"), "Edited in Photoshop", (180, 160, 60))
    exiftool(p, "-Make=NIKON CORPORATION", "-Model=NIKON D750", "-Software=Adobe Photoshop 25.0 (Windows)",
             "-XMP:CreatorTool=Adobe Photoshop 25.0 (Windows)", "-XMP:DocumentID=xmp.did:4f2a9c1e-77aa",
             "-XMP:InstanceID=xmp.iid:9b0c3e55-1234", "-XMP:HistorySoftwareAgent=Adobe Photoshop 25.0",
             "-XMP:HistoryAction=saved", "-DateTimeOriginal=2022:09:09 09:09:09",
             "-ModifyDate=2024:05:01 23:59:00", "-XMP:Creator=Studio X", "-ImageDescription=client draft")

    # 16. Embedded EXIF thumbnail showing something different from the main image
    secret = make_picture(out("_tmp_secret_thumb.jpg"), "SECRET ORIGINAL", (200, 0, 0), size=(160, 120))
    p = make_picture(out("16_embedded_thumbnail.jpg"), "Cropped (thumb differs)", (0, 120, 0))
    exiftool(p, f"-ThumbnailImage<={secret}", "-Make=Canon", "-Model=Canon EOS R6")
    os.remove(secret)

    # 17. Bogus 0,0 GPS
    p = make_picture(out("17_gps_zero_zero.jpg"), "GPS 0,0", (90, 90, 30))
    exiftool(p, *gps_tags(0.0, 0.0), "-Make=Generic", "-Model=CheapCam")

    # 18. PNG content with a .jpg name
    make_picture(out("18_png_named_as.jpg"), "PNG named .jpg", (10, 160, 160), fmt="PNG")

    # 19-20. HEIC (iPhone) photos with GPS, lens and host computer
    if HEIF_AVAILABLE:
        p = make_picture(out("19_iphone_new_york.heic"), "HEIC New York", (60, 60, 160), size=(1008, 756), fmt="HEIF")
        exiftool(p, "-Make=Apple", "-Model=iPhone 15 Pro", "-Software=17.4.1", "-HostComputer=iPhone 15 Pro",
                 "-LensModel=iPhone 15 Pro back triple camera 6.765mm f/1.78",
                 "-DateTimeOriginal=2024:07:04 21:15:30", "-CreateDate=2024:07:04 21:15:30",
                 "-OffsetTimeOriginal=-04:00", *gps_tags(40.6892, -74.0445, 12.0))
        p = make_picture(out("20_iphone_statue.heif"), "HEIF (.heif)", (90, 30, 120), size=(1008, 756), fmt="HEIF")
        exiftool(p, "-Make=Apple", "-Model=iPhone 15 Pro", "-Software=17.4.1",
                 "-DateTimeOriginal=2024:07:05 09:02:11", *gps_tags(40.7484, -73.9857, 20.0))
    else:
        print("pillow-heif not installed: skipping HEIC samples (pip install pillow-heif)")

    # 21-27. GPS hemisphere edge cases
    p = make_picture(out("21_gps_south_east_sydney.jpg"), "S/E Sydney", (20, 110, 150))
    exiftool(p, "-Make=Apple", "-Model=iPhone 14", *gps_tags(-33.8568, 151.2153))
    p = make_picture(out("22_gps_south_west_rio.jpg"), "S/W Rio", (20, 140, 70))
    exiftool(p, "-Make=Apple", "-Model=iPhone 14", *gps_tags(-22.9519, -43.2105))
    # Written with valid refs first, then the ref tag is deleted -> hemisphere unknown.
    p = make_picture(out("23_gps_missing_lat_ref.jpg"), "No GPSLatitudeRef", (150, 100, 20))
    exiftool(p, "-Make=Generic", "-Model=CTF Cam", *gps_tags(-33.8568, 151.2153))
    exiftool(p, "-GPSLatitudeRef=")
    p = make_picture(out("24_gps_missing_lon_ref.jpg"), "No GPSLongitudeRef", (150, 70, 20))
    exiftool(p, "-Make=Generic", "-Model=CTF Cam", *gps_tags(33.4484, -112.0740))
    exiftool(p, "-GPSLongitudeRef=")
    p = make_picture(out("25_gps_missing_both_refs.jpg"), "No GPS refs at all", (150, 40, 20))
    exiftool(p, "-Make=Generic", "-Model=CTF Cam", *gps_tags(-22.9519, -43.2105))
    exiftool(p, "-GPSLatitudeRef=", "-GPSLongitudeRef=")
    p = make_picture(out("26_gps_malformed_ref.jpg"), "GPSLatitudeRef = X", (120, 40, 60))
    exiftool(p, "-Make=Generic", "-Model=CTF Cam", *gps_tags(33.4484, -112.0740))
    exiftool(p, "-GPSLatitudeRef#=X")
    p = make_picture(out("27_xmp_gps_no_hemisphere_letter.jpg"), "XMP GPS, no N/S/E/W", (90, 40, 120))
    sidecar = out("_tmp_no_letter.xmp")
    with open(sidecar, "w") as handle:
        handle.write(
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:exif="http://ns.adobe.com/exif/1.0/">'
            "<exif:GPSLatitude>22,57.114</exif:GPSLatitude><exif:GPSLongitude>43,12.630</exif:GPSLongitude>"
            "</rdf:Description></rdf:RDF></x:xmpmeta>")
    exiftool(p, f"-XMP<={sidecar}")
    os.remove(sidecar)

    # 28+. ctf_collection/: realistic ambiguous-GPS collection for candidate analysis
    make_ctf_collection(out)

    print(f"Sample images written to {OUTPUT}")
    for root, _dirs, files in os.walk(OUTPUT):
        for name in sorted(files):
            print("  ", os.path.relpath(os.path.join(root, name), OUTPUT))


if __name__ == "__main__":
    main()
