from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageCms, ImageOps, PngImagePlugin
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from inkdaddy_hub.api import photos as photos_api
from inkdaddy_hub.config import HubSettings
from inkdaddy_hub.database import Base, get_db
from inkdaddy_hub.models import Photo
from inkdaddy_hub.services import photos
from inkdaddy_hub.services.palette import raw_frame_from_image, raw_photo_frame


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


# DisplayP3-v4.icc is CC0 from saucecontrol/Compact-ICC-Profiles.
# Source and license at https://github.com/saucecontrol/Compact-ICC-Profiles/tree/bdd84663061bc4ae95ca70decff54f581e27f702
_DISPLAY_P3 = Path(__file__).resolve().parents[1] / "fixtures" / "DisplayP3-v4.icc"
_WARM_P3 = (190, 116, 102)


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
