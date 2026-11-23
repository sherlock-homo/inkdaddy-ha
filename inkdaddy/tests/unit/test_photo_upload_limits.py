from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from os import utime
from pathlib import Path
from struct import pack
from threading import Barrier
from uuid import uuid4
from zlib import compress, crc32

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageCms, ImageDraw, ImageOps, PngImagePlugin
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from inkdaddy_hub.api import photos as photos_api
from inkdaddy_hub.config import HubSettings
from inkdaddy_hub.database import Base, get_db
from inkdaddy_hub.models import Device, Frame, Photo, PhotoRevision, Setting
from inkdaddy_hub.services import photos, playback
from inkdaddy_hub.services.matter_frame_broker import prepare_matter_frame, record_matter_frame_receipt
from inkdaddy_hub.services.palette import raw_frame_from_image, raw_photo_frame
from inkdaddy_hub.services.photo_framing import PhotoFraming, frame_photo


def raster(format="PNG", *, size=(4, 3)):
    with BytesIO() as output:
        Image.new("RGB", size, (150, 40, 30)).save(output, format=format)
        return output.getvalue()


@pytest.fixture
def photo_app(monkeypatch, tmp_path):
    monkeypatch.setattr(photos, "get_settings", lambda: HubSettings(data_dir=tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path / 'photos.db'}")
    Base.metadata.create_all(engine)

    def photo_db():
        with Session(engine, expire_on_commit=False) as db:
            yield db

    app = FastAPI()
    app.include_router(photos_api.router)
    app.dependency_overrides[get_db] = photo_db
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def uploads(*files):
    return [("files", (name, content, "image/png")) for name, content in files]


def test_single_upload_checks_the_limit_on_the_actual_read(monkeypatch):
    monkeypatch.setattr(photos_api, "MAX_UPLOAD_BYTES", 4)

    class Upload:
        size = None

        async def read(self, size):
            assert size == 5
            return b"dummy"

    with pytest.raises(photos.PhotoProcessingError, match="limit"):
        asyncio.run(photos_api._read_image(Upload()))


def test_batch_rejects_aggregate_before_reading_any_images(photo_app, monkeypatch):
    client, engine = photo_app
    image = raster()
    monkeypatch.setattr(photos_api, "MAX_BATCH_BYTES", len(image) * 2 - 1)

    async def read_image(*_args):
        pytest.fail("The aggregate limit must be checked before reading image data")

    monkeypatch.setattr(photos_api, "_read_image", read_image)
    response = client.post("/api/photos/batch", files=uploads(("one.png", image), ("two.png", image)))
    assert response.status_code == 413
    with Session(engine) as db:
        assert db.scalars(select(Photo)).all() == []


def test_batch_keeps_an_allowed_photo_when_another_has_unsupported_content(photo_app, tmp_path):
    client, engine = photo_app
    response = client.post(
        "/api/photos/batch",
        files=uploads(("unsupported.png", raster("GIF")), ("allowed.png", raster())),
    )
    assert response.status_code == 200
    result = response.json()
    assert result["accepted"] == 1
    assert result["errors"] == [
        {"filename": "unsupported.png", "detail": "Uploaded file is not a supported image."},
    ]
    with Session(engine) as db:
        assert [p.original_filename for p in db.scalars(select(Photo))] == ["allowed.png"]
    assert all(len(list((tmp_path / directory).iterdir())) == 1
               for directory in ("originals", "processed", "previews", "frames"))


def test_uploaded_photo_frame_uses_the_photo_renderer(photo_app):
    client, _engine = photo_app
    uploaded = client.post(
        "/api/photos", files={"file": ("tone.png", raster(size=(800, 480)), "image/png")}
    )
    assert uploaded.status_code == 200
    metadata = uploaded.json()
    preview = client.get(metadata["preview_url"])
    with Image.open(BytesIO(preview.content)) as image:
        expected, digest = raw_photo_frame(image)
        assert expected != raw_frame_from_image(image)[0]
    downloaded = client.get(metadata["frame_url"])
    assert downloaded.content == expected
    assert len(downloaded.content) == 192_000
    assert metadata["checksum"] == digest


def _off_center_fixture() -> Image.Image:
    image = Image.new("RGB", (1600, 480), (128, 128, 128))
    draw = ImageDraw.Draw(image)
    draw.ellipse((1260, 70, 1510, 410), fill=(246, 35, 18))
    draw.ellipse((1340, 155, 1430, 245), fill=(38, 25, 19))
    return image


def test_intelligent_crop_keeps_off_center_detail_and_centers_uniform_photos():
    image = _off_center_fixture()
    centered, center = frame_photo(image, (800, 480), PhotoFraming("fill"))
    automatic, focus = frame_photo(image, (800, 480), PhotoFraming("intelligent"))
    manual, manual_focus = frame_photo(image, (800, 480), PhotoFraming("fill", 1, 0.5))
    assert center == (0.5, 0.5)
    assert 0.68 < focus[0] <= 0.75 and focus[1] == 0.5
    assert automatic.size == centered.size == (800, 480)
    assert max(centered.getchannel("R").getextrema()) == 128
    assert max(automatic.getchannel("R").getextrema()) >= 246
    assert manual_focus == (0.75, 0.5) and max(manual.getchannel("R").getextrema()) >= 246
    _frame, uniform = frame_photo(Image.new("RGB", (1600, 480)), (800, 480), PhotoFraming("intelligent"))
    assert uniform == (0.5, 0.5)


def test_legacy_photo_crop_edit_keeps_original_and_previous_derivatives(photo_app, tmp_path, monkeypatch):
    client, engine = photo_app
    monkeypatch.setattr(playback, "get_settings", lambda: HubSettings(data_dir=tmp_path))
    with BytesIO() as output:
        _off_center_fixture().save(output, format="PNG", icc_profile=_DISPLAY_P3.read_bytes())
        original = output.getvalue()
    upload = client.post("/api/photos", files={"file": ("synthetic.png", original, "image/png")})
    assert upload.status_code == 200
    initial = upload.json()
    photo_id = int(initial["id"])
    base = f"/api/photos/{photo_id}"
    before_preview = client.get(initial["preview_url"]).content
    before_frame = client.get(initial["frame_url"]).content
    scheduled_at = datetime(2026, 11, 22, 21, tzinfo=timezone.utc)
    device_uid = "synthetic-crop-frame"
    with Session(engine) as db:
        photo = db.get(Photo, photo_id)
        photo.framing_json = None  # Existing photos have no known framing settings.
        original_path = Path(photo.original_path)
        db.commit()
        device = Device(device_uid=device_uid, name="Synthetic frame", selected_source_type="photo")
        db.add(device)
        db.flush()
        offered = prepare_matter_frame(db, device_uid, now=scheduled_at)
        assert record_matter_frame_receipt(
            db, device_uid, transfer_id=offered.transfer_id, frame_id=offered.frame_id,
            result="ok", now=scheduled_at,
        )
        db.commit()
    draft = client.get(f"{base}/framing-preview", params={"mode": "intelligent"})
    assert draft.status_code == 200
    assert float(draft.headers["X-InkDaddy-Focus-X"]) > 0.68
    assert client.get(initial["preview_url"]).content == before_preview
    invalid = client.put(f"{base}/framing", json={
        "mode": "fill", "zoom": 4, "revision": initial["revision"],
    })
    assert invalid.status_code == 422
    saved = client.put(f"{base}/framing", json={"mode": "intelligent", "revision": initial["revision"]})
    assert saved.status_code == 200
    updated = saved.json()
    assert updated["id"] == initial["id"] and updated["revision"] != initial["revision"]
    assert updated["framing"] == {"mode": "intelligent", "x": None, "y": None, "zoom": 1.0}
    assert client.get(updated["preview_url"]).content == draft.content
    assert client.get(initial["preview_url"]).content == before_preview
    assert client.get(initial["frame_url"]).content == before_frame
    with Image.open(BytesIO(draft.content)) as image:
        assert image.info == {"srgb": 1}
        raw, digest = raw_photo_frame(image)
    assert client.get(updated["frame_url"]).content == raw
    assert updated["checksum"] == digest and len(raw) == 192_000
    assert original_path.read_bytes() == original
    assert client.put(f"{base}/framing", json={"mode": "fit", "revision": initial["revision"]}).status_code == 409
    with Session(engine) as db:
        record = db.scalar(select(PhotoRevision).where(PhotoRevision.photo_id == photo_id))
        assert record is not None and record.checksum == initial["checksum"]
        assert photos.load_color_managed_photo(tmp_path, record.processed_path, str(original_path)) is not None
        active_frame = db.scalar(select(Frame).where(Frame.frame_id == offered.frame_id))
        assert active_frame is not None
        original_offer_path = Path(active_frame.path)
        original_offer_path.unlink()
        repaired = prepare_matter_frame(db, device_uid, now=scheduled_at, repair_cache=True)
        assert repaired.transfer_id == offered.transfer_id and repaired.acknowledged
        assert repaired.payload == offered.payload == original_offer_path.read_bytes()
        next_offer = prepare_matter_frame(db, device_uid, now=scheduled_at + timedelta(hours=1))
        assert next_offer.payload == raw and next_offer.transfer_id != offered.transfer_id
        db.rollback()

    assert client.delete(base).status_code == 200
    assert not original_path.exists()
    for directory in ("processed", "previews", "frames"):
        assert not list((tmp_path / directory).glob("photo-*"))


def test_framing_edit_rejects_a_racing_update_and_discards_its_staged_files(photo_app, tmp_path, monkeypatch):
    client, engine = photo_app
    initial = client.post("/api/photos", files={"file": ("synthetic.png", raster(), "image/png")}).json()
    photo_id = int(initial["id"])
    render = photos.process_photo_file
    barrier = Barrier(2, timeout=5)

    def stage_both(*args, **kwargs):
        result = render(*args, **kwargs)
        barrier.wait()
        return result

    monkeypatch.setattr(photos, "process_photo_file", stage_both)

    def save(mode):
        return client.put(f"/api/photos/{photo_id}/framing", json={"mode": mode, "revision": initial["revision"]})

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(save, ("fill", "intelligent")))
    assert sorted(item.status_code for item in results) == [200, 409]
    with Session(engine) as db:
        assert len(db.scalars(select(PhotoRevision)).all()) == 1
    for directory in ("processed", "previews", "frames"):
        assert len(list((tmp_path / directory).iterdir())) == 2


def test_intelligent_crop_can_be_the_saved_upload_default(photo_app):
    client, engine = photo_app
    with Session(engine) as db:
        db.add(Setting(key="photo_upload_mode", value_json='"intelligent"'))
        db.commit()
    uploaded = client.post("/api/photos", files={"file": ("tone.png", raster(), "image/png")})
    assert uploaded.status_code == 200
    assert uploaded.json()["framing"]["mode"] == "intelligent"


# DisplayP3-v4.icc is CC0 from saucecontrol/Compact-ICC-Profiles.
# Source and license at https://github.com/saucecontrol/Compact-ICC-Profiles/tree/bdd84663061bc4ae95ca70decff54f581e27f702
_DISPLAY_P3 = Path(__file__).resolve().parents[1] / "fixtures" / "DisplayP3-v4.icc"
_WARM_P3 = (190, 116, 102)


def test_transparent_png_focus_edit_and_panel_use_only_visible_artwork(photo_app):
    client, engine = photo_app
    image = Image.new("RGBA", (1600, 480), (255, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for x in range(0, 800, 12):
        draw.rectangle((x, 0, x + 5, 479), fill=(0, 0, 0, 0))
    draw.rectangle((1280, 110, 1450, 350), fill=(0, 0, 0, 255))
    draw.ellipse((1310, 160, 1390, 300), fill=(40, 110, 205, 128))
    white = Image.new("RGBA", image.size, "white")
    white.alpha_composite(image)
    with BytesIO() as output:
        image.save(output, format="PNG")
        original = output.getvalue()
    response = client.post("/api/photos/batch", data={"mode": "intelligent"}, files=uploads(("masked.png", original)))
    assert response.status_code == 200
    record = response.json()["photos"][0]
    base = f"/api/photos/{record['id']}"
    visible = white.convert("RGB")
    for mode in ("intelligent", "fill"):
        expected, focus = frame_photo(visible, (800, 480), PhotoFraming(mode))
        preview = client.get(f"{base}/framing-preview", params={"mode": mode})
        assert preview.status_code == 200
        assert float(preview.headers["X-InkDaddy-Focus-X"]) == pytest.approx(focus[0], abs=1e-6)
        assert Image.open(BytesIO(preview.content)).tobytes() == expected.tobytes()
        if mode == "fill":
            edited = client.put(f"{base}/framing", json={"mode": mode, "revision": record["revision"]})
            assert edited.status_code == 200
            record = edited.json()
        assert client.get(record["preview_url"]).content == preview.content
        assert client.get(record["frame_url"]).content == raw_photo_frame(expected)[0]
    with Session(engine) as db:
        assert Path(db.get(Photo, int(record["id"])).original_path).read_bytes() == original


@pytest.mark.parametrize("mode,key,visible", [
    ("RGB", (110, 30, 92), (80, 88, 96)),
    ("P", 0, 1), ("P", bytes([0, 128]), 1), ("L", 3, 70), ("1", 0, 255),
    ("LA", (12, 0), (84, 140)), ("RGBA", (230, 20, 90, 0), (80, 88, 96, 128)),
    ("I;16", 128, 32896),
])
def test_png_transparency_modes_render_against_white(tmp_path, mode, key, visible):
    keyed = mode not in ("LA", "RGBA")
    image = Image.new(mode, (2, 1), 0 if mode == "P" else key)
    image.putpixel((1, 0), visible)
    if mode == "P":
        image.putpalette([158, 80, 102, 46, 122, 74] + [0] * 762)
    original = tmp_path / "input.png"
    image.save(original, transparency=key if keyed else None)
    with Image.open(original) as uploaded:
        if mode == "I;16":
            expected = (128, 128, 128)
        else:
            rgba = uploaded.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            background.alpha_composite(rgba)
            expected = background.getpixel((1, 0))[:3]
    result, _ = photos._render_framed_photo(
        original, PhotoFraming("fill"), HubSettings(data_dir=tmp_path, default_width=2, default_height=1)
    )
    assert result.getpixel((0, 0)) == (255, 255, 255)
    assert result.getpixel((1, 0)) == expected


def test_transparent_webp_uses_the_same_white_background(tmp_path):
    image = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
    image.putpixel((1, 0), (0, 0, 0, 128))
    with BytesIO() as output:
        image.save(output, format="WEBP", lossless=True)
        output.seek(0)
        result, _ = photos._render_framed_photo(
            output, PhotoFraming("fit"), HubSettings(data_dir=tmp_path, default_width=2, default_height=1)
        )
    assert list(result.get_flattened_data()) == [(255, 255, 255), (127, 127, 127)]


def test_opaque_alpha_keeps_rgb_processing_and_translucent_p3_blends_in_srgb(tmp_path):
    settings = HubSettings(data_dir=tmp_path, default_width=180, default_height=90)
    profile = _DISPLAY_P3.read_bytes()
    color = _off_center_fixture()
    rgba = color.convert("RGBA")
    for name, image in [("rgb", color), ("opaque", rgba)]:
        image.save(tmp_path / f"{name}.png", icc_profile=profile)
    rgb = photos.process_photo_file("rgb", tmp_path / "rgb.png", mode="intelligent", settings=settings)
    opaque = photos.process_photo_file("opaque", tmp_path / "opaque.png", mode="intelligent", settings=settings)
    assert rgb.preview_path.read_bytes() == opaque.preview_path.read_bytes()
    assert rgb.frame_path.read_bytes() == opaque.frame_path.read_bytes()

    translucent = Image.new("RGBA", (180, 90), (*_WARM_P3, 128))
    translucent.save(tmp_path / "translucent.png", icc_profile=profile)
    result = photos.process_photo_file("blend", tmp_path / "translucent.png", settings=settings)
    with Image.open(result.preview_path) as preview:
        assert preview.getpixel((90, 45)) == pytest.approx((228, 183, 176), abs=1)
        assert preview.info == {"srgb": 1}


def _png_with_transparent_key(depth, color_type, key, pixels):
    def chunk(kind, data):
        return pack(">I", len(data)) + kind + data + pack(">I", crc32(kind + data))

    header = pack(">IIBBBBB", 2, 1, depth, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"tRNS", key)
            + chunk(b"IDAT", compress(b"\x00" + pixels)) + chunk(b"IEND", b""))


@pytest.mark.parametrize("depth,key,pixels", [(2, 2, b"\x80"), (4, 8, b"\x80")])
def test_low_bit_png_transparency_matches_the_expanded_gray(tmp_path, depth, key, pixels):
    png = _png_with_transparent_key(depth, 0, pack(">H", key), pixels)
    image, _ = photos._render_framed_photo(BytesIO(png), PhotoFraming("fit"), HubSettings(
        data_dir=tmp_path, default_width=2, default_height=1))
    assert list(image.get_flattened_data()) == [(255, 255, 255), (0, 0, 0)]


@pytest.mark.parametrize("options", [{}, {"transparency": 0}, {"transparency": 20000}])
def test_high_depth_gray_keeps_shades_with_or_without_a_key(tmp_path, options):
    source = Image.frombytes("I;16", (2, 1), pack("<2H", 10000, 20000))
    png = BytesIO()
    source.save(png, format="PNG", **options)
    png.seek(0)
    image, _ = photos._render_framed_photo(png, PhotoFraming("fit"), HubSettings(
        data_dir=tmp_path, default_width=2, default_height=1))
    second = 255 if options.get("transparency") == 20000 else 78
    assert list(image.get_flattened_data()) == [(39, 39, 39), (second, second, second)]


def test_high_depth_rgb_png_key_reports_an_actionable_unsupported_format(photo_app):
    client, _ = photo_app
    # Both colors decode to the same 8-bit triplet, but only the first is transparent.
    key = (6000, 9000, 12000)
    png = _png_with_transparent_key(16, 2, pack(">3H", *key), pack(">6H", *key, 6001, 9001, 12001))
    response = client.post("/api/photos", files={"file": ("key.png", png, "image/png")})
    assert response.status_code == 422
    assert "alpha channel or as an 8-bit PNG" in response.json()["detail"]


@pytest.mark.parametrize("mode", ["fit", "fill", "crop"])
def test_wide_gamut_upload_keeps_preview_and_panel_in_the_same_color_space(
    photo_app, tmp_path, monkeypatch, mode
):
    client, engine = photo_app
    image = Image.new("RGB", (320, 480), _WARM_P3)
    exif = Image.Exif()
    exif[271] = "Synthetic camera"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_itxt("XML:com.adobe.xmp", "Synthetic metadata")
    with BytesIO() as output:
        image.save(
            output, format="PNG", icc_profile=_DISPLAY_P3.read_bytes(),
            exif=exif, pnginfo=metadata, dpi=(72, 72),
        )
        supplied = output.getvalue()
    uploaded = client.post(
        "/api/photos", data={"mode": mode}, files={"file": ("tone.png", supplied, "image/png")}
    )
    assert uploaded.status_code == 200
    record = uploaded.json()
    preview = client.get(record["preview_url"])
    with Image.open(BytesIO(preview.content)) as rendered:
        assert rendered.size == (800, 480)
        assert rendered.info == {"srgb": 1}
        assert not rendered.getexif()
        assert rendered.getpixel((400, 240)) == pytest.approx((202, 111, 98), abs=1)
        assert (rendered.getpixel((10, 240)) == (255, 255, 255)) is (mode == "fit")
        expected, digest = raw_photo_frame(rendered)
    downloaded = client.get(record["frame_url"])
    assert downloaded.content == expected
    assert len(downloaded.content) == 192_000
    pigments = {value for byte in downloaded.content for value in (byte >> 4, byte & 15)}
    assert pigments <= {0, 1, 2, 3, 5, 6}
    assert record["checksum"] == digest

    def forbidden_original(*_args):
        pytest.fail("A normalized derivative must not reopen its original ICC profile.")

    monkeypatch.setattr(photos, "_original_rgb_profile", forbidden_original)
    with Session(engine) as db:
        photo = db.get(Photo, int(record["id"]))
        assert Path(photo.original_path).read_bytes() == supplied
        assert Path(photo.processed_path).read_bytes() == preview.content
        scheduled = photos.load_color_managed_photo(tmp_path, photo.processed_path, photo.original_path)
    assert scheduled is not None
    assert raw_photo_frame(scheduled) == (expected, digest)


@pytest.mark.parametrize("profile_location", ["processed", "original", "outside"])
def test_legacy_photo_normalization_uses_managed_profile_metadata_and_keeps_framing(
    tmp_path, monkeypatch, profile_location
):
    profiles = _DISPLAY_P3.read_bytes()
    originals = tmp_path / "originals"
    originals.mkdir()
    file = originals / "source.png"
    image = Image.new("RGB", (320, 480), _WARM_P3)
    if profile_location == "outside":
        outside = tmp_path / "source.png"
        image.save(outside, icc_profile=profiles)
        file.symlink_to(outside)
    else:
        image.save(file, icc_profile=profiles)
    framed = ImageOps.pad(image, (800, 480), color="white")
    processed = tmp_path / "processed" / "legacy.png"
    processed.parent.mkdir()
    framed.save(processed, icc_profile=profiles if profile_location == "processed" else None)

    open_image = Image.open
    original_opened = []

    def metadata_only(source, *args, **kwargs):
        opened = open_image(source, *args, **kwargs)
        if getattr(source, "name", None) == file.name:
            original_opened.append(True)

            def forbid_decode(*_args):
                pytest.fail("Legacy ICC recovery should not decode the original raster.")

            monkeypatch.setattr(opened, "load", forbid_decode)
        return opened

    monkeypatch.setattr(photos.Image, "open", metadata_only)
    normalized = photos.load_color_managed_photo(tmp_path, str(processed), str(file))
    assert normalized is not None
    assert normalized.size == (800, 480)
    assert normalized.getpixel((10, 240)) == (255, 255, 255)
    assert original_opened == ([True] if profile_location == "original" else [])
    if profile_location == "outside":
        assert normalized.getpixel((400, 240)) == _WARM_P3
    else:
        assert normalized.getpixel((400, 240)) == pytest.approx((202, 111, 98), abs=1)
    legacy = photos.load_processed_photo(tmp_path, str(processed))
    assert legacy is not None and legacy.tobytes() == framed.tobytes()


@pytest.mark.parametrize("condition", ["sRGB", "untagged", "malformed", "mismatched", "oversized", "unavailable"])
def test_color_management_profiles_keep_the_srgb_fallback_deterministic(tmp_path, monkeypatch, condition):
    fixture = tmp_path / "input.png"
    profile = _DISPLAY_P3.read_bytes()
    if condition == "sRGB":
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    elif condition == "untagged":
        profile = None
    elif condition == "mismatched":
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    elif condition == "malformed":
        profile = b"invalid fixture profile"
    elif condition == "oversized":
        monkeypatch.setattr(photos, "MAX_ICC_PROFILE_BYTES", len(profile) - 1)
    elif condition == "unavailable":
        def unavailable(*_args):
            raise ImportError("LittleCMS deliberately disabled.")
        monkeypatch.setattr(photos.ImageCms, "getOpenProfile", unavailable)

    image = Image.new("RGB", (800, 480), _WARM_P3)
    image.save(fixture, icc_profile=profile)
    output = photos.process_photo_file("fixture", fixture, settings=HubSettings(data_dir=tmp_path))
    raw, digest = raw_photo_frame(image)
    assert output.frame_path.read_bytes() == raw
    assert output.checksum == digest
    with Image.open(output.preview_path) as preview:
        assert preview.getpixel((400, 240)) == _WARM_P3
        assert preview.info.get("srgb") == 1 and "icc_profile" not in preview.info


@pytest.mark.parametrize(("start", "field"), [
    (0, b"\0\0\0\0"),  # Declared size.
    (12, b"link"),  # Device-link profile instead of an image color space.
    (36, b"none"),  # Missing ICC signature.
    (128, b"\0\0\0\0"),  # Empty tag table.
    (136, b"\0\0\0\0"),  # Tag points inside the header and table.
    (140, b"\0\0\x10\0"),  # Tag extends beyond the embedded profile.
])
def test_bad_icc_header_and_tag_bounds_fall_back_before_native_parse(tmp_path, monkeypatch, start, field):
    profile = bytearray(_DISPLAY_P3.read_bytes())
    profile[start:start + len(field)] = field
    fixture = tmp_path / "input.png"
    Image.new("RGB", (320, 480), _WARM_P3).save(fixture, icc_profile=bytes(profile))

    def not_parsed(*_args):
        pytest.fail("Invalid ICC metadata should be screened before the native parser.")

    monkeypatch.setattr(photos.ImageCms, "getOpenProfile", not_parsed)
    output = photos.process_photo_file("fixture", fixture, settings=HubSettings(data_dir=tmp_path))
    with Image.open(output.preview_path) as preview:
        assert preview.getpixel((400, 240)) == _WARM_P3
        assert preview.info.get("srgb") == 1 and "icc_profile" not in preview.info


@pytest.mark.parametrize(("limit", "value"), [("MAX_IMAGE_PIXELS", 9), ("MAX_IMAGE_DIMENSION", 3)])
def test_dimensions_are_checked_before_pixel_decode(photo_app, monkeypatch, tmp_path, limit, value):
    client, engine = photo_app
    monkeypatch.setattr(photos, limit, value)

    def decode(*_args):
        pytest.fail("Out-of-bounds image headers must be rejected before pixel decoding")

    monkeypatch.setattr(photos.ImageOps, "exif_transpose", decode)
    response = client.post("/api/photos", files={"file": ("four-by-three.png", raster(), "image/png")})
    assert response.status_code == 422
    assert "dimensions" in response.json()["detail"]
    with Session(engine) as db:
        assert db.scalars(select(Photo)).all() == []
    assert list((tmp_path / "originals").iterdir()) == []


def test_batch_cleans_partial_files_keeps_successes_and_continues(photo_app, monkeypatch, tmp_path):
    client, engine = photo_app
    pack = photos.raw_photo_frame
    calls = 0

    def pack_with_error(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("Synthetic storage failure.")
        return pack(*args)

    monkeypatch.setattr(photos, "raw_photo_frame", pack_with_error)
    image = raster()
    response = client.post(
        "/api/photos/batch",
        files=uploads(("first.png", image), ("failed.png", image), ("last.png", image)),
    )
    assert response.status_code == 200
    result = response.json()
    assert [p["original_filename"] for p in result["photos"]] == ["first.png", "last.png"]
    assert result["errors"] == [
        {"filename": "failed.png", "detail": "Could not store the uploaded photo."},
    ]
    with Session(engine) as db:
        assert [p.original_filename for p in db.scalars(select(Photo).order_by(Photo.id))] == [
            "first.png", "last.png",
        ]
    assert all(len(list((tmp_path / directory).iterdir())) == 2
               for directory in ("originals", "processed", "previews", "frames"))


def _save_photo_metadata(engine, **assets):
    with Session(engine, expire_on_commit=False) as db:
        photo = Photo(
            original_filename="fixture.png",
            stored_filename="fixture.png",
            original_path=assets.pop("original_path", ""),
            **assets,
        )
        db.add(photo)
        db.commit()
        return photo.id


@pytest.mark.parametrize(
    ("route", "folder", "attribute"),
    [("preview", "previews", "preview_path"), ("frame", "frames", "frame_path")],
)
def test_photo_reads_confine_paths_to_the_correct_directory(photo_app, tmp_path, route, folder, attribute):
    client, engine = photo_app
    managed = tmp_path / folder
    managed.mkdir()
    alternate = tmp_path / "other"
    alternate.mkdir()
    private_fixture = alternate / "untouched"
    private_fixture.write_bytes(b"outside fixture")
    valid = managed / "valid"
    valid.write_bytes(b"owned fixture")
    local_link = managed / "link"
    local_link.symlink_to(valid)
    parent_link = managed / "outside"
    parent_link.symlink_to(alternate, target_is_directory=True)

    allowed_id = _save_photo_metadata(engine, **{attribute: str(valid)})
    assert client.get(f"/api/photos/{allowed_id}/{route}").content == b"owned fixture"
    for invalid in (
        str(private_fixture),
        str(managed / ".." / "other" / "untouched"),
        str(local_link),
        str(parent_link / "untouched"),
        str(managed / "bad") + "\x00suffix",
    ):
        photo_id = _save_photo_metadata(engine, **{attribute: invalid})
        assert client.get(f"/api/photos/{photo_id}/{route}").status_code == 404


def test_photo_delete_unlinks_only_owned_regular_files_and_allows_blank_placeholders(photo_app, tmp_path):
    client, engine = photo_app
    for directory in ("originals", "previews", "processed", "frames"):
        (tmp_path / directory).mkdir()
    outside = tmp_path / "keep"
    outside.write_bytes(b"outside fixture")
    proper_preview = tmp_path / "previews" / "owned"
    proper_preview.write_bytes(b"owned fixture")
    wrong_directory = tmp_path / "originals" / "keep"
    wrong_directory.write_bytes(b"other asset")
    parent_link = tmp_path / "processed" / "link"
    parent_link.symlink_to(tmp_path, target_is_directory=True)

    photo_id = _save_photo_metadata(
        engine,
        original_path=str(outside),
        processed_path=str(parent_link / "keep"),
        preview_path=str(proper_preview),
        frame_path=str(wrong_directory),
    )
    assert client.delete(f"/api/photos/{photo_id}").status_code == 200
    assert not proper_preview.exists()
    assert outside.read_bytes() == b"outside fixture"
    assert wrong_directory.read_bytes() == b"other asset"
    assert parent_link.is_symlink()
    assert client.get(f"/api/photos/{photo_id}").status_code == 404

    placeholder = _save_photo_metadata(engine)
    assert client.get(f"/api/photos/{placeholder}/preview").status_code == 404
    assert client.delete(f"/api/photos/{placeholder}").status_code == 200


def test_photo_sweep_removes_only_aged_unreferenced_generated_derivatives(photo_app, tmp_path, monkeypatch):
    _client, engine = photo_app
    monkeypatch.setattr(playback, "get_settings", lambda: HubSettings(data_dir=tmp_path))
    for directory in ("originals", "processed", "previews", "frames"):
        (tmp_path / directory).mkdir()

    def fixture(directory, extension):
        path = tmp_path / directory / f"photo-{uuid4().hex}{extension}"
        path.write_bytes(b"synthetic derivative")
        return path

    active = fixture("processed", ".png")
    archive = fixture("previews", ".png")
    frame_path = fixture("frames", ".raw")
    interrupted = (
        fixture("processed", ".png"), fixture("previews", ".png"), fixture("frames", ".raw")
    )
    fresh = fixture("previews", ".png")
    untouched_original = fixture("originals", ".png")
    legacy = tmp_path / "processed" / "photo-123.png"
    legacy.write_bytes(b"legacy path")
    wrong_extension = fixture("processed", ".raw")
    directory_candidate = tmp_path / "processed" / f"photo-{uuid4().hex}.png"
    directory_candidate.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"external file")
    symlink = tmp_path / "previews" / f"photo-{uuid4().hex}.png"
    symlink.symlink_to(outside)

    now = datetime.now(timezone.utc) + timedelta(days=2)
    last_write = (now - timedelta(minutes=1)).timestamp()
    utime(fresh, (last_write, last_write))
    with Session(engine) as db:
        photo = Photo(
            original_filename="fixture.png", stored_filename="fixture.png",
            original_path=str(untouched_original), processed_path=str(active),
        )
        db.add(photo)
        db.flush()
        db.add(PhotoRevision(photo_id=photo.id, preview_path=str(archive)))
        db.add(Frame(
            frame_id="a" * 24, sha256="a" * 64, source_type="device", source_id="fixture",
            width=800, height=480, palette="waveshare_7color_6", size_bytes=192_000,
            path=str(frame_path),
        ))
        db.commit()
        playback.prune_playback(db, now=now - timedelta(days=2))
        db.commit()
        assert all(path.exists() for path in interrupted)

        playback.prune_playback(db, now=now)
        db.commit()

    assert not any(path.exists() for path in interrupted)
    assert all(path.exists() for path in (
        active, archive, frame_path, fresh, untouched_original, legacy,
        wrong_extension, directory_candidate, symlink,
    ))
    assert outside.read_bytes() == b"external file"
