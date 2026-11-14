"""
Central configuration for Photo Tool.

Every limit and tunable lives here so you can change behaviour in one place.
Values can be overridden with environment variables (see README.md).
"""

import os


def _env_int(name, default):
    """Read an integer environment variable, falling back to a default."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- Network -----------------------------------------------------------------
# Bind to localhost only. This tool has no authentication, so do NOT expose it
# on a public interface unless you understand the consequences.
HOST = os.environ.get("PHOTO_TOOL_HOST", "127.0.0.1")
PORT = _env_int("PHOTO_TOOL_PORT", 5000)
DEBUG = os.environ.get("PHOTO_TOOL_DEBUG", "0") == "1"

# --- Upload limits -------------------------------------------------------------
# Maximum size of ONE image file (bytes).
MAX_FILE_SIZE = _env_int("PHOTO_TOOL_MAX_FILE_MB", 50) * 1024 * 1024

# Maximum number of images kept in the workspace at the same time.
MAX_IMAGES = _env_int("PHOTO_TOOL_MAX_IMAGES", 500)

# Maximum number of files accepted in ONE upload request. The browser splits big
# batches into several requests of this size (see UPLOAD_CHUNK_FILES in app.js).
MAX_FILES_PER_REQUEST = 25

# Maximum size of one HTTP request body. Flask rejects bigger requests with 413.
MAX_REQUEST_SIZE = MAX_FILE_SIZE * MAX_FILES_PER_REQUEST + 1024 * 1024

# Decompression-bomb protection: Pillow refuses images with more pixels than this
# (it raises an error at 2x this value).
MAX_IMAGE_PIXELS = 120_000_000

# --- Supported formats -----------------------------------------------------------
# Map of allowed file extensions -> the Pillow formats that content may have.
# The extension is only a first filter; the real format is detected from content.
ALLOWED_EXTENSIONS = {
    "jpg": {"JPEG", "MPO"},
    "jpeg": {"JPEG", "MPO"},
    "jpe": {"JPEG", "MPO"},
    "png": {"PNG"},
    "webp": {"WEBP"},
    "tif": {"TIFF"},
    "tiff": {"TIFF"},
    # HEIC/HEIF (iPhone photos) - only if pillow-heif is installed,
    # otherwise heif_support.py removes these two entries at startup.
    "heic": {"HEIF"},
    "heif": {"HEIF"},
}
HEIF_EXTENSIONS = ("heic", "heif")

# Pillow format -> (canonical extension, MIME type) used for stored copies.
# MPO is a JPEG with extra embedded pictures (common for phones/3D cameras).
FORMAT_INFO = {
    "JPEG": ("jpg", "image/jpeg"),
    "MPO": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
    "TIFF": ("tif", "image/tiff"),
    "HEIF": ("heic", "image/heic"),    # Pillow reports HEIC and HEIF as "HEIF"
}

# --- Derived images ----------------------------------------------------------------
THUMBNAIL_SIZE = (320, 320)    # gallery / table / popup thumbnails
PREVIEW_SIZE = (1600, 1600)    # detail-panel preview (never the full-res original)
THUMBNAIL_QUALITY = 80
PREVIEW_QUALITY = 85

# --- Sanitization --------------------------------------------------------------------
JPEG_SANITIZE_QUALITY = 95     # re-encode quality for JPEG output
WEBP_SANITIZE_QUALITY = 95     # re-encode quality for lossy WebP output
HEIF_SANITIZE_QUALITY = 90     # re-encode quality when HEIC output stays HEIC

# What a sanitized HEIC becomes in "reencode" mode:
#   "jpeg" - converted to JPEG (works everywhere: websites, Windows, Android)
#   "heic" - stays HEIC (smaller, but many programs cannot open it)
# In "lossless" mode a HEIC always stays HEIC (metadata stripped by ExifTool).
HEIC_OUTPUT_CHOICES = ("jpeg", "heic")
DEFAULT_HEIC_OUTPUT = "jpeg"

# --- ExifTool ----------------------------------------------------------------------
EXIFTOOL_BINARY = os.environ.get("PHOTO_TOOL_EXIFTOOL", "exiftool")
EXIFTOOL_TIMEOUT_SECONDS = 180
EXIFTOOL_FILES_PER_CALL = 100  # batch size for one ExifTool process

# Raw metadata values longer than this are truncated in the UI (XMP blobs etc.).
RAW_VALUE_MAX_CHARS = 4000
