"""
Download helpers: safe download filenames and ZIP creation.

Download names are derived from the ORIGINAL filename for convenience
("IMG_1234.JPG" -> "IMG_1234_sanitized.jpg") but are passed through
werkzeug's secure_filename, so they can never contain path separators or
other dangerous characters. Inside a ZIP, collisions get a numeric suffix.
"""

import logging
import os
import tempfile
import zipfile

from werkzeug.utils import secure_filename

log = logging.getLogger(__name__)


def sanitized_download_name(original_filename, extension):
    """'My Photo.JPG' -> 'My_Photo_sanitized.jpg' (falls back to 'image')."""
    stem = original_filename.rsplit(".", 1)[0] if "." in original_filename else original_filename
    safe_stem = secure_filename(stem)[:100] or "image"
    return f"{safe_stem}_sanitized.{extension}"


def unique_name(name, used_names):
    """Return `name`, or 'name_2.ext', 'name_3.ext'... if already used."""
    if name.lower() not in used_names:
        used_names.add(name.lower())
        return name
    stem, dot, extension = name.rpartition(".")
    counter = 2
    while True:
        candidate = f"{stem}_{counter}{dot}{extension}"
        if candidate.lower() not in used_names:
            used_names.add(candidate.lower())
            return candidate
        counter += 1


def build_sanitized_zip(records, zip_directory):
    """
    Write a ZIP containing the sanitized copy of every record and return an
    open file object positioned at the start. The ZIP file is unlinked from
    disk immediately (Linux keeps it readable until the handle is closed),
    so nothing is left behind after the download.

    Also returns the list of names written.
    """
    handle = tempfile.NamedTemporaryFile(dir=zip_directory, suffix=".zip", delete=False)
    used_names = set()
    written = []
    try:
        # Images are already compressed, so ZIP_STORED is fast and just as small.
        with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_STORED) as archive:
            for record in records:
                path = record.paths.get("sanitized")
                if not path or not os.path.exists(path):
                    continue
                name = unique_name(record.sanitization["download_name"], used_names)
                archive.write(path, arcname=name)
                written.append(name)
        handle.flush()
        handle.seek(0)
    except Exception:
        handle.close()
        os.unlink(handle.name)
        raise
    os.unlink(handle.name)
    log.info("Built ZIP with %d sanitized file(s)", len(written))
    return handle, written
