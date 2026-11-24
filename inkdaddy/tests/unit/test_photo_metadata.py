import struct
import zlib
from datetime import date
from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from inkdaddy_hub.services import photo_metadata as metadata


def photo_tags(taken="2012:03:09 20:07:22"):
    tags = Image.Exif()
    tags[34665] = {36867: taken}
    tags[34853] = {1: "N", 2: (20, 5, 0), 3: "W", 4: (45, 9, 10)}
    return tags


def original(tmp_path, ext, **options):
    path = tmp_path / "originals" / ("fixture." + ext)
    path.parent.mkdir(exist_ok=True)
    Image.new("RGB", (4, 4)).save(path, **options)
    return path


def details(path, want_date=True, want_location=True):
    return metadata.capture_details(path.parent.parent, str(path), want_date=want_date, want_location=want_location)


@pytest.mark.parametrize("ext", ["jpg", "png", "webp"])
def test_capture_from_managed_original_and_only_requested_fields(tmp_path, ext):
    path = original(tmp_path, ext, exif=photo_tags())
    assert details(path) == metadata.CaptureDetails(date(2012, 3, 9), "20.08° N, 45.15° W")
    assert details(path, want_location=False) == metadata.CaptureDetails(date(2012, 3, 9))
    assert details(path, want_date=False) == metadata.CaptureDetails(location="20.08° N, 45.15° W")


XMP = """<metadata xmlns:i="http://iptc.org/std/Iptc4xmpExt/2008-02-29/">
<i:LocationShown i:City="Wrong city" />
<i:LocationCreated><record i:ProvinceState="Region"><i:City>Fixture City</i:City>
<i:CountryName>Country</i:CountryName></record></i:LocationCreated></metadata>"""


@pytest.mark.parametrize("fmt", ["jpg", "webp", "png", "compressed_png"])
def test_embedded_capture_place_precedes_rounded_coordinates(tmp_path, fmt):
    if fmt in ("png", "compressed_png"):
        info = PngImagePlugin.PngInfo()
        info.add_itxt("XML:com.adobe.xmp", XMP, zip=fmt == "compressed_png")
        path = original(tmp_path, "png", exif=photo_tags(), pnginfo=info)
    else:
        path = original(tmp_path, fmt, exif=photo_tags(), xmp=XMP.encode())
    assert details(path).location == "Fixture City, Region, Country"


def test_invalid_optional_tags_and_managed_paths(tmp_path):
    tags = photo_tags("2012:02:35 12:00:00")
    tags[34853] = {1: "N", 2: (90, 1, 0), 3: "W", 4: (12, 0, 0)}
    tags[306] = "2020:03:01 11:00:00"
    tags[315] = "Private author"
    path = original(tmp_path, "jpg", exif=tags, xmp=b'<!DOCTYPE x [<!ENTITY z "secret">]>' + XMP.encode())
    assert details(path) == metadata.CaptureDetails()
    assert details(original(tmp_path, "bmp")) == metadata.CaptureDetails()
    link = path.with_name("linked.jpg")
    link.symlink_to(path)
    assert details(link) == metadata.CaptureDetails()
    path.unlink()
    assert details(path) == metadata.CaptureDetails()


def test_cache_revalidates_replaced_original(tmp_path, monkeypatch):
    path = original(tmp_path, "jpg", exif=photo_tags())
    real_read = metadata._metadata_blocks
    reads = []

    def read(*args, **kwargs):
        reads.append(True)
        return real_read(*args, **kwargs)

    monkeypatch.setattr(metadata, "_metadata_blocks", read)
    assert details(path).taken == date(2012, 3, 9)
    assert details(path).taken == date(2012, 3, 9)
    incoming = path.with_name("replacement.jpg")
    tags = Image.Exif()
    tags[36867] = "2010:02:01 23:45:10"
    tags[34853] = {28: b"ASCII\0\0\0Test area"}
    Image.new("RGB", (4, 4)).save(incoming, exif=tags)
    incoming.replace(path)
    assert details(path) == metadata.CaptureDetails(date(2010, 2, 1), "Test area")
    assert len(reads) == 2


def test_seek_skips_pixel_payload_and_bounds_decompressed_xmp():
    class CountingStream(BytesIO):
        bytes_read = 0

        def read(self, n=-1):
            result = super().read(n)
            self.bytes_read += len(result)
            return result

    def chunk(tag, payload):
        return struct.pack(">I", len(payload)) + tag + payload + bytes(4)

    raw = photo_tags().tobytes()[6:]
    stream = CountingStream(b"\x89PNG\r\n\x1a\n" + chunk(b"IDAT", bytes(1024 * 1024)) + chunk(b"eXIf", raw))
    assert metadata._metadata_blocks(stream, len(stream.getbuffer()), want_location=False) == (raw, b"")
    assert stream.bytes_read < 1024
    large = zlib.compress(bytes(metadata._MAX_BLOCK + 1))
    assert metadata._png_xmp(b"\x01\0\0\0" + large) == b""
    assert metadata._png_xmp(b"\x01\0\0\0broken") == b""


@pytest.mark.parametrize(("rendered", "expected"), [
    ("2024-02-28", "Future date"), ("2024-02-29", "Today"), ("2024-03-01", "Yesterday"),
    ("2024-03-05", "5 days ago"), ("2025-02-28", "365 days ago"),
    ("2025-03-01", "1 year ago"), ("2026-03-01", "2 years ago"),
])
def test_completed_years_at_render_date(rendered, expected):
    assert metadata.photo_age(date(2024, 2, 29), date.fromisoformat(rendered)) == expected
