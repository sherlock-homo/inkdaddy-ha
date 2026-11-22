from uuid import uuid4

from inkdaddy_hub.database import SessionLocal, init_db
from sqlalchemy import select

from inkdaddy_hub.models import Device, DeviceStatusHistory, Playback
from inkdaddy_hub.services.udp_heartbeat import (
    ParsedHeartbeat,
    parse_udp_heartbeat,
    record_udp_heartbeat,
)


def test_parse_key_value_udp_heartbeat():
    heartbeat = parse_udp_heartbeat(
        b"inkdaddy:v1;device_uid=xiao-mg24-a1b2c3d4;firmware=inkdaddy-sisdk-udp-0.1.0;"
        b"hardware=Seeed XIAO MG24;battery=87;voltage=3.91;status=awake;seq=12"
    )

    assert heartbeat.device_uid == "xiao-mg24-a1b2c3d4"
    assert heartbeat.firmware_version == "inkdaddy-sisdk-udp-0.1.0"
    assert heartbeat.hardware_version == "Seeed XIAO MG24"
    assert heartbeat.battery_percent == 87
    assert heartbeat.battery_voltage == 3.91
    assert heartbeat.status == "awake"
    assert heartbeat.mesh_connected is True
    assert heartbeat.provisioned is True
    assert heartbeat.sequence == "12"


def test_parse_json_udp_heartbeat():
    heartbeat = parse_udp_heartbeat(
        b'{"device_uid":"display-1","mesh_connected":false,"provisioned":true,"battery_percent":42}'
    )

    assert heartbeat.device_uid == "display-1"
    assert heartbeat.mesh_connected is False
    assert heartbeat.provisioned is True
    assert heartbeat.battery_percent == 42


def test_parse_manual_next_content_action():
    heartbeat = parse_udp_heartbeat(
        b"inkdaddy:v1;device_uid=xiao-mg24-a1b2c3d4;status=manual_refresh_requested;"
        b"action=next_content;seq=25"
    )

    assert heartbeat.status == "manual_refresh_requested"
    assert heartbeat.action == "next_content"
    assert heartbeat.sequence == "25"


def test_reject_missing_uid():
    try:
        parse_udp_heartbeat(b"inkdaddy:v1;firmware=test")
    except ValueError as exc:
        assert "device_uid" in str(exc)
    else:
        raise AssertionError("missing device_uid should fail")


def test_duplicate_udp_history_is_keyed_by_sequence():
    init_db()
    device_uid = f"display-{uuid4().hex}"
    action = ParsedHeartbeat(
        device_uid=device_uid, sequence="8", action="next_content", status="manual_refresh_requested"
    )
    record_udp_heartbeat(action)
    record_udp_heartbeat(ParsedHeartbeat(device_uid=device_uid, sequence="9"))
    record_udp_heartbeat(action)  # The second border-router path can arrive after other status.

    with SessionLocal() as db:
        device = db.scalar(select(Device).where(Device.device_uid == device_uid))
        playback = db.get(Playback, device.id)
        history = db.scalars(select(DeviceStatusHistory).where(DeviceStatusHistory.device_id == device.id)).all()
        assert playback.anchor_position == 1
        assert len(history) == 2


def test_displayed_hub_frame_updates_device_refresh_summary():
    init_db()
    suffix = uuid4().hex
    device_uid = f"display-{suffix}"

    record_udp_heartbeat(
        ParsedHeartbeat(
            device_uid=device_uid,
            name="Display",
            hardware_target="xiao_mg24",
            status="manual_refresh_requested",
            action="next_content",
            sequence="1",
        )
    )
    record_udp_heartbeat(
        ParsedHeartbeat(
            device_uid=device_uid,
            name="Display",
            hardware_target="xiao_mg24",
            status="displayed_hub_frame",
            sequence="2",
        )
    )

    with SessionLocal() as db:
        device = db.scalar(select(Device).where(Device.device_uid == device_uid))
        assert device is not None
        assert device.last_refresh_result == "displayed_hub_frame"
