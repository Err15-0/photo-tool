"""
HEIC / HEIF support (iPhone photos) via the optional `pillow-heif` package.

Importing this module registers pillow-heif as a Pillow plugin, so that
Image.open() can read .heic/.heif files and Image.save(..., "HEIF") can
write them. ExifTool reads/writes HEIC metadata natively.

If pillow-heif is not installed, the app still works: HEIC/HEIF is simply
removed from the allowed upload formats and the UI says so.

Orientation note: in HEIF files the rotation is stored in the container
("irot"/"imir" boxes) and libheif applies it while decoding. The EXIF
Orientation tag inside a HEIC is informational only and must NOT be applied
a second time - pillow-heif therefore reports EXIF orientation as 1. The
decoded image is already upright.
"""

import logging

from . import config

log = logging.getLogger(__name__)

HEIF_AVAILABLE = False
HEIF_VERSION = None

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
    HEIF_VERSION = getattr(pillow_heif, "__version__", "unknown")
    log.info("HEIC/HEIF support enabled (pillow-heif %s)", HEIF_VERSION)
except Exception as exc:  # ImportError, or a broken native library
    log.warning("HEIC/HEIF support disabled (pip install pillow-heif): %s", exc)
    for extension in config.HEIF_EXTENSIONS:
        config.ALLOWED_EXTENSIONS.pop(extension, None)
