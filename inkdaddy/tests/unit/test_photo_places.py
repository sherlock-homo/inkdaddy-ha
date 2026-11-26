from datetime import datetime, timedelta
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from inkdaddy_hub.api import photo_places
from inkdaddy_hub.config import HubSettings
from inkdaddy_hub.database import Base, get_db
from inkdaddy_hub.models import Photo
from inkdaddy_hub.services import geocoding, photo_places as jobs
from inkdaddy_hub.services.photos import create_photo_from_bytes


def feature(lon, lat, **props):
    return {"geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}


def test_location_selection_handles_competing_venues_and_coordinate_precision():
    common = {"city": "Example Town", "state": "Region"}
    tower = feature(11.00101, 44.00101, osm_key="tourism", name="The Tower", **common)
    cafe = feature(11.0011, 44.0011, osm_key="amenity", name="Cafe", **common)
    street = feature(11.001, 44.001, osm_key="highway", name="Main Street", **common)
    building = feature(11.0011, 44.0011, osm_key="building", name="Art Museum", **common)
    address = feature(11.0012, 44.001, osm_key="building", street="Main Street", housenumber="5", **common)
    def locate(*features, precise=True):
        return geocoding.place_label({"features": list(features)}, 44.001, 11.001, precise=precise)
    assert locate(tower, street) == "Near The Tower, Example Town, Region"
    assert locate(street, tower, cafe) == "Example Town, Region"
    assert locate(cafe, building) == "Example Town, Region"
    assert locate(cafe, tower, address) == "Near 5 Main Street, Example Town, Region"
    assert locate(cafe, address, precise=False) == "Example Town, Region"
    assert locate(feature(20, 20, osm_key="amenity", name="Distant")) == ""
    assert locate(feature(11.001, 44.001, osm_key=["invalid"], **common)) == "Example Town, Region"
    assert locate() == ""
    with pytest.raises(geocoding.ProviderUnavailable):
        geocoding.place_label({"error": "invalid"}, 44, 11, precise=True)


def jpeg(gps=True, *, poor=False):
    image = BytesIO()
    tags = Image.Exif()
    if gps:
        tags[34853] = {1: "N", 2: (44, 1, 13), 3: "E", 4: (11, 2, 23), 31: 75 if poor else 5}
    Image.new("RGB", (8, 8), "red").save(image, "JPEG", exif=tags)
    return image.getvalue()


@pytest.fixture
def library(tmp_path, monkeypatch):
    settings = HubSettings(data_dir=tmp_path)
    settings.ensure_data_dirs()
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    db_url = "sqlite:///" + str(tmp_path / "places.sqlite")
    engine = create_engine(db_url, connect_args={"timeout": 0.5})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(photo_places.router)
    def database():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    def upload(name, **args):
        with sessions() as db:
            return create_photo_from_bytes(db, filename=name, content=jpeg(**args), settings=settings).id
    return TestClient(app), sessions, upload


def test_library_lookup_is_scoped_persistent_and_reused(library, monkeypatch):
    client, sessions, upload = library
    first = upload("one.jpg")
    upload("also-here.jpg")
    upload("no-location.jpg", gps=False)
    now = datetime(2026, 2, 1, 0, 0)
    monkeypatch.setattr(jobs, "_now", lambda: now)
    calls = []
    def provider(lat, lon, *, precise):
        calls.append((lat, lon, precise))
        with sessions() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")  # HTTP holds no writer lock.
        return "Example City, Region"
    monkeypatch.setattr(jobs, "reverse_place", provider)
    assert client.get("/api/photo-places").json() == {"enabled": False, "pending": 0, "resolved": 0, "unavailable": 0}
    assert not jobs.process_one(sessions)
    assert client.post("/api/photo-places/lookup").json()["pending"] == 3
    for _ in range(3):
        assert jobs.process_one(sessions)
    assert not jobs.process_one(sessions) and len(calls) == 1
    assert calls[0] == (44 + 1/60 + 13/3600, 11 + 2/60 + 23/3600, True)
    with sessions() as db:
        photo = db.get(Photo, first)
        assert photo.place_label == "Example City, Region"
    assert client.post("/api/photo-places/lookup").json() == {"enabled": False, "pending": 0, "resolved": 2, "unavailable": 0}
    assert client.put("/api/photo-places", json={"enabled": True}).json()["enabled"] is True
    upload("automatic.jpg", poor=True)
    assert client.get("/api/photo-places").json()["pending"] == 1
    now += timedelta(seconds=5)
    assert jobs.process_one(sessions) and calls[-1][2] is False
    upload("cancelled.jpg")
    after = client.put("/api/photo-places", json={"enabled": False}).json()
    assert after["pending"] == 0 and after["resolved"] == 3
    assert not jobs.process_one(sessions)
    assert client.put("/api/photo-places", json={"enabled": "yes"}).status_code == 422


def test_outage_does_not_block_rendering_or_send_other_points(library, monkeypatch):
    client, sessions, upload = library
    upload("one.jpg")
    upload("two.jpg", poor=True)
    client.post("/api/photo-places/lookup")
    now = datetime(2026, 2, 1)
    monkeypatch.setattr(jobs, "_now", lambda: now)
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(args)
        raise geocoding.ProviderUnavailable(3600)
    monkeypatch.setattr(jobs, "reverse_place", unavailable)
    assert jobs.process_one(sessions) and jobs.process_one(sessions)
    assert len(calls) == 1 and not jobs.process_one(sessions)
    now += timedelta(hours=1)
    monkeypatch.setattr(jobs, "reverse_place", lambda *a, **kw: "")
    assert jobs.process_one(sessions)
    with sessions() as db:
        assert db.scalar(select(Photo).where(Photo.place_lookup == "missing")).place_label is None
    assert client.get("/api/photo-places").json()["unavailable"] == 1


def test_concurrent_workers_share_an_inflight_lookup(library, monkeypatch):
    client, sessions, upload = library
    upload("first.jpg")
    upload("second.jpg")
    client.post("/api/photo-places/lookup")
    now = datetime(2026, 2, 1)
    monkeypatch.setattr(jobs, "_now", lambda: now)
    calls = []

    def lookup(*args, **kwargs):
        nonlocal now
        calls.append(args)
        now += timedelta(seconds=3)
        assert jobs.process_one(sessions)
        return "Example Town, Region"

    monkeypatch.setattr(jobs, "reverse_place", lookup)
    assert jobs.process_one(sessions)
    now += timedelta(seconds=31)
    assert jobs.process_one(sessions)
    assert len(calls) == 1
    assert client.get("/api/photo-places").json()["resolved"] == 2


def test_retry_after_supports_both_http_forms():
    assert geocoding._retry_delay("3600") == 3600
    assert geocoding._retry_delay("Fri, 01 Jan 2100 00:00:00 GMT") > 3600
    assert geocoding._retry_delay("invalid date") == 900
