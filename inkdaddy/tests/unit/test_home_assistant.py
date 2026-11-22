import json
import stat
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from inkdaddy_hub.api import home_assistant as api
from inkdaddy_hub.api.integrations import list_integrations
from inkdaddy_hub.database import Base
from inkdaddy_hub.models import HomeAssistantConfig
from inkdaddy_hub.services import home_assistant as service, secrets
from inkdaddy_hub.services.home_assistant import normalize_state_response, profile_token_url, safe_ha_log_payload


class HomeAssistantTests(unittest.TestCase):
    def test_normalizes_states(self):
        entities = normalize_state_response(
            [
                {
                    "entity_id": "sensor.temp",
                    "state": "72",
                    "attributes": {"friendly_name": "Temperature"},
                }
            ]
        )
        self.assertEqual(entities[0].domain, "sensor")
        self.assertEqual(entities[0].name, "Temperature")

    def test_profile_url(self):
        self.assertEqual(profile_token_url("http://homeassistant.local:8123/"), "http://homeassistant.local:8123/profile")

    def test_redacts_token(self):
        payload = safe_ha_log_payload({"token": "demo", "nested": {"password": "test"}})
        self.assertEqual(payload["token"], "<redacted>")
        self.assertEqual(payload["nested"]["password"], "<redacted>")


@pytest.fixture
def manual_connection(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store_path = tmp_path / "secrets.json"
    monkeypatch.delenv("INKDADDY_HA_MODE", raising=False)
    monkeypatch.setattr(secrets, "get_settings", lambda: SimpleNamespace(secrets_path=store_path))
    secrets.set_secret(api.HA_TOKEN_REF, "existing-demo-token")
    try:
        with Session(engine) as db:
            db.add(HomeAssistantConfig(
                id=api.HA_CONFIG_ID, base_url="https://ha.example.test",
                token_secret_ref=api.HA_TOKEN_REF, enabled=True,
            ))
            db.commit()
            yield db, store_path
    finally:
        engine.dispose()


def test_manual_ha_origin_validation():
    assert service.home_assistant_origin("https://HA.example.test:443/config") == (
        "https", "ha.example.test", 443
    )
    assert service.home_assistant_origin("http://[2001:db8::1]:8123") == ("http", "2001:db8::1", 8123)
    for url in (
        "ftp://ha.example.test",
        "https://user@ha.example.test",
        "https://ha.example.test?view=settings",
        "https://ha.example.test#profile",
    ):
        with pytest.raises(ValueError, match="valid HTTP or HTTPS"):
            service.home_assistant_origin(url)
        assert profile_token_url(url) is None


def test_ha_http_client_disables_redirects_and_proxies(monkeypatch):
    build_opener = Mock()
    monkeypatch.setattr(service.urllib.request, "build_opener", build_opener)
    for base in ("https://ha.example.test", service.SUPERVISOR_CORE_URL):
        service._open_api(base, "example-token", "/api/", 4)
        redirect_handler, proxy_handler = build_opener.call_args.args
        assert redirect_handler.redirect_request(None, None, 302, "", {}, base) is None
        assert proxy_handler.proxies == {}
        request = build_opener.return_value.open.call_args.args[0]
        assert request.full_url == base + "/api/"
        assert request.get_header("Authorization") == "Bearer example-token"


def test_manual_ha_test_uses_credential_for_configured_origin_only(manual_connection, monkeypatch):
    db, _ = manual_connection
    validate = Mock(return_value={"ok": True})
    monkeypatch.setattr(api, "validate_home_assistant_token", validate)
    with pytest.raises(HTTPException) as caught:
        api.test_config({"base_url": "https://second-ha.example.test"}, db)
    assert caught.value.status_code == 422
    validate.assert_not_called()

    assert api.test_config({"base_url": "https://HA.example.test:443"}, db) == {"ok": True}
    validate.assert_called_once_with("https://HA.example.test:443", "existing-demo-token")


def test_manual_ha_origin_change_requires_new_credential_and_prunes_old_references(manual_connection, monkeypatch):
    db, store_path = manual_connection
    secrets.set_secret(f"{api.HA_TOKEN_REF}.obsolete", "old-demo-token")
    secrets.set_secret("other.test_config", "unrelated-demo-value")
    validate = Mock(return_value={"ok": True})
    monkeypatch.setattr(api, "validate_home_assistant_token", validate)
    new_base = "https://second-ha.example.test"
    with pytest.raises(HTTPException) as caught:
        api.save_config({"base_url": new_base}, db)
    assert caught.value.status_code == 422
    validate.assert_not_called()
    assert db.get(HomeAssistantConfig, api.HA_CONFIG_ID).base_url == "https://ha.example.test"

    result = api.save_config({"base_url": new_base, "token": "new-demo-token"}, db)
    validate.assert_called_once_with(new_base, "new-demo-token")
    assert result["configured"] is True
    assert service.get_connection(db) == service.HAConnection(new_base, "new-demo-token")
    active_ref = db.get(HomeAssistantConfig, api.HA_CONFIG_ID).token_secret_ref
    assert json.loads(store_path.read_text()) == {
        active_ref: "new-demo-token", "other.test_config": "unrelated-demo-value",
    }
    assert store_path.stat().st_mode & 0o777 == 0o600
    assert api.save_config({"enabled": False}, db)["base_url"] == new_base


def test_manual_ha_initial_config_does_not_adopt_an_orphaned_token(manual_connection):
    db, _ = manual_connection
    db.delete(db.get(HomeAssistantConfig, api.HA_CONFIG_ID))
    db.commit()
    result = api.save_config({"base_url": "https://new-ha.example.test"}, db)
    assert result["configured"] is False
    assert service.get_connection(db) is None


def test_same_origin_rotation_and_failed_save_preserve_the_correct_token(manual_connection, monkeypatch):
    db, store_path = manual_connection
    before = db.get(HomeAssistantConfig, api.HA_CONFIG_ID)
    with monkeypatch.context() as failure:
        failure.setattr(db, "commit", Mock(side_effect=SQLAlchemyError("synthetic failed commit")))
        with pytest.raises(SQLAlchemyError):
            api.save_config({"token": "discarded-demo-token", "validate": False}, db)
    assert before.token_secret_ref == api.HA_TOKEN_REF
    assert json.loads(store_path.read_text()) == {api.HA_TOKEN_REF: "existing-demo-token"}

    api.save_config({"token": "rotated-demo-token", "validate": False}, db)
    config = db.get(HomeAssistantConfig, api.HA_CONFIG_ID)
    assert config.last_validated_at is None
    assert json.loads(store_path.read_text()) == {config.token_secret_ref: "rotated-demo-token"}


@pytest.mark.skipif(secrets.os.name != "posix", reason="POSIX directory synchronization")
def test_failed_secret_directory_sync_keeps_only_the_current_credential(manual_connection, monkeypatch):
    db, store_path = manual_connection
    real_fsync = secrets.os.fsync
    failed = False

    def fail_once(fd):
        nonlocal failed
        if not failed and stat.S_ISDIR(secrets.os.fstat(fd).st_mode):
            failed = True
            raise OSError("synthetic directory synchronization failure")
        return real_fsync(fd)

    monkeypatch.setattr(secrets.os, "fsync", fail_once)
    with pytest.raises(OSError, match="synthetic directory synchronization"):
        api.save_config({"token": "discarded-demo-token", "validate": False}, db)
    assert service.get_connection(db).token == "existing-demo-token"
    assert json.loads(store_path.read_text()) == {api.HA_TOKEN_REF: "existing-demo-token"}


def test_trial_credentials_do_not_mark_the_saved_token_as_validated(manual_connection, monkeypatch):
    db, _ = manual_connection
    monkeypatch.setattr(api, "validate_home_assistant_token", Mock(return_value={"ok": True}))
    assert api.test_config({"token": "trial-demo-token"}, db) == {"ok": True}
    assert db.get(HomeAssistantConfig, api.HA_CONFIG_ID).last_validated_at is None
    assert api.test_config({}, db) == {"ok": True}
    assert db.get(HomeAssistantConfig, api.HA_CONFIG_ID).last_validated_at is not None


def test_existing_session_reads_the_active_credential_after_rotation(manual_connection):
    writer, _ = manual_connection
    with Session(writer.bind, expire_on_commit=False) as reader:
        old_config = reader.get(HomeAssistantConfig, api.HA_CONFIG_ID)
        assert old_config.token_secret_ref == api.HA_TOKEN_REF
        api.save_config({"token": "rotated-demo-token", "validate": False}, writer)
        assert list_integrations(reader)["integrations"][0]["configured"] is True
        reader.expire(old_config)
        assert service.get_connection(reader) == service.HAConnection(
            "https://ha.example.test", "rotated-demo-token",
        )
        assert api.get_config(reader)["configured"] is True


def test_managed_ha_uses_only_supervisor_origin_and_credential(manual_connection, monkeypatch):
    db, _ = manual_connection
    monkeypatch.setenv("INKDADDY_HA_MODE", "supervisor")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-demo-token")
    validate = Mock(return_value={"ok": True})
    monkeypatch.setattr(api, "validate_home_assistant_token", validate)
    payload = {"base_url": "https://ha.example.test", "token": "manual-demo-token"}
    assert api.test_config(payload, db) == {"ok": True}
    validate.assert_called_once_with(service.SUPERVISOR_CORE_URL, "supervisor-demo-token")
    with pytest.raises(HTTPException) as caught:
        api.save_config(payload, db)
    assert caught.value.status_code == 409
