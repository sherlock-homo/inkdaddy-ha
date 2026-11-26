"""Read capture details from a managed original when a photo widget needs them."""

from __future__ import annotations

import math
import re
import struct
import warnings
import zlib
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache
from os import fstat
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree  # nosec B405 - DTDs rejected during parsing

from PIL import ExifTags, Image

from .asset_storage import asset_stat, open_asset
from .photos import MAX_UPLOAD_BYTES

_MAX_BLOCK = 256 * 1024
_XMP_JPEG = b"http://ns.adobe.com/xap/1.0/\0"
_XMP_PNG = b"XML:com.adobe.xmp\0"
_IPTC = "{http://iptc.org/std/Iptc4xmpExt/2008-02-29/}"


@dataclass(frozen=True, repr=False)
class CaptureDetails:
    taken: date | None = None
    location: str = ""
    coordinates: tuple[float, float] | None = None
    accuracy_m: float | None = None

    def with_cached_place(self, label: str | None) -> CaptureDetails:
        return replace(self, location=label) if self.coordinates is not None and label else self


def _png_xmp(payload: bytes) -> bytes:
    if payload[:2] not in (b"\0\0", b"\x01\0"):
        return b""
    fields = payload[2:].split(b"\0", 2)  # language, translated keyword, text
    if len(fields) != 3:
        return b""
    if payload[0] == 0:
        return fields[2]
    try:
        decoder = zlib.decompressobj()
        data = decoder.decompress(fields[2], _MAX_BLOCK + 1)
        return data if len(data) <= _MAX_BLOCK and decoder.eof and not decoder.unused_data else b""
    except zlib.error:
        return b""


def _metadata_blocks(stream: BinaryIO, size: int, *, want_location: bool) -> tuple[bytes, bytes]:
    """Seek past pixels; bound both chunk traversal and retained metadata."""
    header = stream.read(12)
    jpeg = header.startswith(b"\xff\xd8")
    png = header.startswith(b"\x89PNG\r\n\x1a\n")
    webp = header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if not (jpeg or png or webp):
        return b"", b""
    stream.seek(2 if jpeg else 8 if png else 12)
    exif = xmp = b""
    for _ in range(4096):
        if jpeg:
            marker = stream.read(2)
            if len(marker) < 2 or marker[0] != 255 or marker[1] in (0xda, 0xd9):
                break
            if marker[1] == 255:  # Legal padding between marker segments.
                stream.seek(-1, 1)
                continue
            length = stream.read(2)
            if len(length) < 2:
                break
            count = int.from_bytes(length, "big") - 2
            end = stream.tell() + count
            tag = b"APP1" if marker[1] == 0xe1 else b""
        else:
            block = stream.read(8)
            if len(block) < 8:
                break
            if png:
                count, tag = struct.unpack(">I4s", block)
                end = stream.tell() + count + 4  # CRC follows each payload.
                if tag == b"IEND":
                    break
            else:
                tag, count = struct.unpack("<4sI", block)
                end = stream.tell() + count + count % 2
        if count < 0 or end > size:
            break
        start = stream.tell()
        if tag == b"APP1":
            prefix = stream.read(min(count, len(_XMP_JPEG)))
            if prefix.startswith(b"Exif\0\0"):
                tag = b"EXIF"
            elif prefix == _XMP_JPEG:
                tag = b"XMP "
                start += len(_XMP_JPEG)
                count -= len(_XMP_JPEG)
        if 0 < count <= _MAX_BLOCK:
            if tag in (b"eXIf", b"EXIF") and not exif:
                stream.seek(start)
                exif = stream.read(count)
            elif want_location and not xmp:
                if tag == b"XMP ":
                    stream.seek(start)
                    xmp = stream.read(count)
                elif tag == b"iTXt" and stream.read(min(count, len(_XMP_PNG))) == _XMP_PNG:
                    xmp = _png_xmp(stream.read(count - len(_XMP_PNG)))
        if exif and (xmp or not want_location):
            break
        stream.seek(end)
    return exif, xmp


class _NoDoctype(ElementTree.TreeBuilder):
    def doctype(self, name: str, public_id: str, system_id: str) -> None:
        raise ValueError("Unsupported metadata declaration.")


def _label(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in value[:160]).split())[:80]


def _created_place(xmp: bytes) -> str:
    if not xmp:
        return ""
    try:
        root = ElementTree.fromstring(xmp, parser=ElementTree.XMLParser(target=_NoDoctype()))  # nosec B314
    except (ElementTree.ParseError, ValueError, LookupError):
        return ""
    location = root.find(f".//{_IPTC}LocationCreated")
    if location is None:
        return ""
    fields = {}
    for node in location.iter():
        for field in ("City", "ProvinceState", "CountryName", "Sublocation"):
            tag = _IPTC + field
            value = node.text if node.tag == tag else node.get(tag)
            if label := _label(value):
                fields.setdefault(field, label)
    parts = [fields.get("Sublocation"), fields.get("City"), fields.get("ProvinceState"), fields.get("CountryName")]
    return ", ".join(dict.fromkeys(part for part in parts if part))[:100]


def _gps_area(gps: dict, endian: str | None) -> str:
    value = gps.get(ExifTags.GPS.GPSAreaInformation)
    if isinstance(value, bytes):
        if value.startswith(b"ASCII\0\0\0"):
            value = value[8:].decode("ascii", errors="replace")
        elif value.startswith(b"UNICODE\0"):
            data = value[8:]
            codec = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else (
                "utf-16-le" if endian == "<" else "utf-16-be"
            )
            value = data.decode(codec, errors="replace")
        else:
            return ""
    return _label(value)


def _coordinate(value: object, ref: object, directions: tuple[str, str], limit: int) -> float | None:
    if not isinstance(value, (tuple, list)) or len(value) != 3 or ref not in directions:
        return None
    try:
        degrees, minutes, seconds = map(float, value)
        result = degrees + minutes / 60 + seconds / 3600
        if not (0 <= degrees and 0 <= minutes < 60 and 0 <= seconds < 60 and math.isfinite(result) and result <= limit):
            return None
        return -result if ref == directions[1] else result
    except (TypeError, ValueError, ArithmeticError):
        return None


def _capture_date(value: object) -> date | None:
    if isinstance(value, str):
        value = value.strip("\0 ")
    if isinstance(value, str) and re.fullmatch(r"\d{4}:\d\d:\d\d(?: [\d: ]{8})?", value):
        try:
            return date.fromisoformat(value[:10].replace(":", "-"))
        except ValueError:
            pass
    return None


def capture_details(data_dir: Path, original_path: str, *, want_date: bool, want_location: bool) -> CaptureDetails:
    try:
        stat = asset_stat(data_dir, "originals", original_path)
        if not stat or not 0 < stat.st_size <= MAX_UPLOAD_BYTES:
            return CaptureDetails()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        return _cached_details(data_dir, original_path, signature, want_date, want_location)
    except OSError:
        return CaptureDetails()


@lru_cache(maxsize=128)
def _cached_details(
    data_dir: Path, original_path: str, signature: tuple[int, ...], want_date: bool, want_location: bool
) -> CaptureDetails:
    with open_asset(data_dir, "originals", original_path) as asset:
        if asset is None:
            return CaptureDetails()
        stat = fstat(asset.fileno())
        if signature != (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns):
            return CaptureDetails()
        raw, xmp = _metadata_blocks(asset, stat.st_size, want_location=want_location)
    place = _created_place(xmp) if want_location else ""
    taken = None
    coordinates = None
    accuracy = None
    # Damaged optional tags should leave a usable picture. Pillow reports some malformed
    # TIFF fields as warnings and omits them, so avoid logging private metadata.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        try:
            exif = Image.Exif()
            exif.load(raw)
            if want_date:
                taken = _capture_date(exif.get_ifd(ExifTags.IFD.Exif).get(ExifTags.Base.DateTimeOriginal))
                taken = taken or _capture_date(exif.get(ExifTags.Base.DateTimeOriginal))
            if want_location and not place:
                gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
                place = _gps_area(gps, exif.endian)
                if not place:
                    lat = _coordinate(gps.get(2), gps.get(1), ("N", "S"), 90)
                    lon = _coordinate(gps.get(4), gps.get(3), ("E", "W"), 180)
                    if lat is not None and lon is not None:
                        coordinates = (lat, lon)
                        place = f"{abs(lat):.2f}° {gps.get(1)}, {abs(lon):.2f}° {gps.get(3)}"
                        if 31 in gps:  # EXIF GPSHPositioningError, metres
                            accuracy = math.inf
                            accuracy = float(gps[31])
        except (OSError, ValueError, TypeError, KeyError, IndexError, ArithmeticError, struct.error, SyntaxError, AttributeError):
            pass
    return CaptureDetails(taken, place, coordinates, accuracy)


def photo_age(taken: date, rendered: date) -> str:
    days = (rendered - taken).days
    if days < 0:
        return "Future date"
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    years = rendered.year - taken.year - ((rendered.month, rendered.day) < (taken.month, taken.day))
    if years == 0:
        return f"{days} days ago"
    return "1 year ago" if years == 1 else f"{years} years ago"
