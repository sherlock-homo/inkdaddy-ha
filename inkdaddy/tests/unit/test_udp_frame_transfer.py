from pathlib import Path
from uuid import uuid4

from inkdaddy_hub.config import HubSettings
from inkdaddy_hub.database import SessionLocal, init_db
from inkdaddy_hub.models import Frame
from inkdaddy_hub.services.udp_frame_transfer import (
    PROTOCOL,
    FrameTransferRequest,
    build_chunk_response,
    build_manifest_response,
    handle_frame_transfer_request,
    parse_frame_transfer_request,
)


def test_parse_manifest_request():
    request = parse_frame_transfer_request(
        b"inkdaddy-frame:v1;type=manifest_request;device_uid=xiao-mg24-a1b2c3d4;"
        b"last_frame_id=abc123;refresh_count=4;chunk_size=768;seq=19"
    )

    assert request.request_type == "manifest_request"
    assert request.device_uid == "xiao-mg24-a1b2c3d4"
    assert request.last_frame_id == "abc123"
    assert request.refresh_count == 4
    assert request.chunk_size == 768
    assert request.sequence == "19"


def test_manifest_response_contains_chunk_geometry():
    frame = Frame(
        frame_id="frame-123",
        source_type="device",
        source_id="display-1",
        width=800,
        height=480,
        palette="waveshare_7color_6",
        bits_per_pixel=4,
        packing="high_nibble_first",
        byte_order="row_major",
        size_bytes=192000,
        sha256="a" * 64,
        content_type="application/octet-stream",
        path="/tmp/frame.raw",
    )

    response = build_manifest_response(
        status="changed",
        frame=frame,
        content_kind="normal_frame",
        source_id="photo-1",
        chunk_size=512,
        sequence="20",
    ).decode()

    assert response.startswith(PROTOCOL)
    assert "type=manifest_response" in response
    assert "chunk_size=512" in response
    assert "chunk_count=375" in response
    assert "content_kind=normal_frame" in response
    assert "seq=20" in response


def test_chunk_response_uses_newline_binary_separator():
    response = build_chunk_response(
        frame_id="frame-123",
        chunk_index=2,
        offset=1024,
        payload=b"\x00\x01;\xff",
        sequence="21",
    )
    header, payload = response.split(b"\n", 1)

    assert b"type=chunk_response" in header
    assert b"payload_bytes=4" in header
    assert payload == b"\x00\x01;\xff"


def test_handle_chunk_request_reads_frame_file(tmp_path: Path):
    init_db()
    frame_id = f"test-{uuid4().hex}"
    frame_path = tmp_path / "frames" / "frame.raw"
    frame_path.parent.mkdir()
    frame_path.write_bytes(bytes(range(128)))
    settings = HubSettings(data_dir=tmp_path, udp_frame_chunk_size=64)

    with SessionLocal() as db:
        db.add(
            Frame(
                frame_id=frame_id,
                source_type="device",
                source_id="display-1",
                width=8,
                height=8,
                palette="test",
                bits_per_pixel=4,
                packing="high_nibble_first",
                byte_order="row_major",
                size_bytes=128,
                sha256="b" * 64,
                content_type="application/octet-stream",
                path=str(frame_path),
            )
        )
        db.commit()

        response = handle_frame_transfer_request(
            db,
            FrameTransferRequest(
                request_type="chunk_request",
                device_uid="display-1",
                frame_id=frame_id,
                chunk_index=1,
                chunk_size=64,
                sequence="22",
            ),
            settings,
        )

    header, payload = response.split(b"\n", 1)
    assert b"type=chunk_response" in header
    assert b"chunk_index=1" in header
    assert payload == bytes(range(64, 128))
