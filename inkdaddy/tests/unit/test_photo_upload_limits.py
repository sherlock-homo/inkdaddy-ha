from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
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
