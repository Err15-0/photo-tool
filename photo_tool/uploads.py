"""
Upload handling: validation, safe storage, thumbnails and previews.

Validation steps for every uploaded file (any failure rejects only THAT file):

  1. the original filename's extension must be in config.ALLOWED_EXTENSIONS
  2. the file must not be empty and not exceed config.MAX_FILE_SIZE
  3. Pillow must detect a supported format from the CONTENT (magic bytes),
     not from the filename. If the extension and the content disagree
     (e.g. a PNG renamed to .jpg) the file is accepted but a warning is shown
     and the real format is used.
  4. Pillow's verify() must pass, and the image must fully decode
     (catches truncated/corrupted files and decompression bombs).

Then the exact uploaded bytes are written to  originals/<random-id>.<ext>
(exclusive create, mode 0600). The user-supplied filename is kept ONLY as a
display string and is never used to build a path.
"""

import hashlib
import io
import logging
import os
from datetime import datetime, timezone

from PIL import Image, ImageOps, UnidentifiedImageError

from . import config
from .workspace import ImageRecord, generate_image_id

log = logging.getLogger(__name__)

# Decompression-bomb protection (Pillow raises DecompressionBombError above 2x).
Image.MAX_IMAGE_PIXELS = config.MAX_IMAGE_PIXELS


class UploadRejected(Exception):
    """A single file was rejected; the message is shown to the user."""


def display_filename(raw_name):
    """
    Make the user's filename safe to *display* (it is never used as a path):
    keep only the last path component, drop control characters, limit length.
    """
    name = (raw_name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable())
    name = name.strip() or "unnamed"
    if len(name) > 200:
        name = name[:200] + "..."
    return name


def extension_of(filename):
    """'Photo.JPG' -> 'jpg'  ('' if there is no extension)."""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def _read_limited(file_storage):
    """Read an uploaded file but never more than MAX_FILE_SIZE + 1 bytes."""
    data = file_storage.stream.read(config.MAX_FILE_SIZE + 1)
    if len(data) > config.MAX_FILE_SIZE:
        raise UploadRejected(
            f"File is larger than the {config.MAX_FILE_SIZE // (1024 * 1024)} MB limit"
        )
    if not data:
        raise UploadRejected("File is empty")
    return data


def _detect_and_verify(data, filename):
    """
    Detect the real image format from content and verify the image.
    Returns (pillow_format, warnings).
    """
    extension = extension_of(filename)
    if extension not in config.ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(config.ALLOWED_EXTENSIONS))
        raise UploadRejected(f"Unsupported file extension '.{extension}' (allowed: {allowed})")

    warnings = []
    try:
        with Image.open(io.BytesIO(data)) as probe:
            detected = probe.format
            probe.verify()          # structural check (does not decode pixels)
    except Image.DecompressionBombError as exc:
        raise UploadRejected(f"Image is too large (decompression bomb protection): {exc}")
    except UnidentifiedImageError:
        raise UploadRejected("Not a recognised image (content does not match any supported format)")
    except Exception as exc:  # Pillow raises many different exception types
        raise UploadRejected(f"Corrupted or malformed image: {exc}")

    if detected not in config.FORMAT_INFO:
        raise UploadRejected(f"Image format {detected} is not supported")

    if detected not in config.ALLOWED_EXTENSIONS[extension]:
        warnings.append(
            f"Extension mismatch: file is named .{extension} but its content is {detected}"
        )
    return detected, warnings


def _make_rgb_for_jpeg(image):
    """Convert any mode to RGB for JPEG thumbnails (alpha composited on light grey)."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (235, 235, 235))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _create_derivatives(data, image_format, thumb_path, preview_path):
    """
    Fully decode the image (this is also the "is it really intact?" check),
    then write a thumbnail and a preview. Both are re-encoded from pixels
    only, so they never carry the original metadata.

    Returns (width, height, display_width, display_height, frame_count).
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            frame_count = getattr(image, "n_frames", 1)
            if image_format in ("JPEG", "MPO"):
                # Let libjpeg decode at reduced size: much faster for big photos.
                image.draft("RGB", config.PREVIEW_SIZE)
            try:
                orientation = int(image.getexif().get(0x0112, 1))
            except Exception:
                orientation = 1
            image.load()                                    # full decode
            oriented = ImageOps.exif_transpose(image)       # apply EXIF rotation
    except Image.DecompressionBombError as exc:
        raise UploadRejected(f"Image is too large (decompression bomb protection): {exc}")
    except Exception as exc:
        raise UploadRejected(f"Image could not be decoded (truncated or corrupted?): {exc}")

    # EXIF orientations 5-8 rotate by 90/270 degrees: displayed width/height swap.
    if orientation in (5, 6, 7, 8):
        display_width, display_height = height, width
    else:
        display_width, display_height = width, height

    preview = _make_rgb_for_jpeg(oriented)
    preview.thumbnail(config.PREVIEW_SIZE)
    preview.save(preview_path, "JPEG", quality=config.PREVIEW_QUALITY)

    thumb = preview.copy()
    thumb.thumbnail(config.THUMBNAIL_SIZE)
    thumb.save(thumb_path, "JPEG", quality=config.THUMBNAIL_QUALITY)
    return width, height, display_width, display_height, frame_count


def _write_exclusive(path, data):
    """Write bytes to a NEW file (fails if it exists - we never overwrite)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def store_uploaded_file(file_storage, workspace):
    """
    Validate and store ONE uploaded file. Returns an ImageRecord (metadata not
    yet extracted) or raises UploadRejected with a user-friendly message.
    """
    filename = display_filename(file_storage.filename)
    data = _read_limited(file_storage)
    image_format, warnings = _detect_and_verify(data, filename)
    extension, mime_type = config.FORMAT_INFO[image_format]

    image_id = generate_image_id()
    original_path = workspace.path_for("originals", image_id, extension)
    thumb_path = workspace.path_for("thumbs", image_id, "jpg")
    preview_path = workspace.path_for("previews", image_id, "jpg")

    try:
        width, height, display_w, display_h, frames = _create_derivatives(
            data, image_format, thumb_path, preview_path
        )
        _write_exclusive(original_path, data)
    except UploadRejected:
        for path in (thumb_path, preview_path, original_path):
            if os.path.exists(path):
                os.remove(path)
        raise

    if frames > 1:
        warnings.append(f"Image has {frames} frames/pages; thumbnails show the first one")

    record = ImageRecord(
        id=image_id,
        original_filename=filename,
        stored_extension=extension,
        image_format=image_format,
        mime_type=mime_type,
        file_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        width=width,
        height=height,
        display_width=display_w,
        display_height=display_h,
        frame_count=frames,
        uploaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        warnings=warnings,
        paths={"original": original_path, "thumb": thumb_path, "preview": preview_path},
    )
    log.info("Stored %s as %s (%s, %d bytes)", filename, image_id, image_format, len(data))
    return record
