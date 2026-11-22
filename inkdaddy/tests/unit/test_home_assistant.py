import unittest
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from inkdaddy_hub.api import home_assistant as api
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
def manual_connection(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    stored = {api.HA_TOKEN_REF: "existing-demo-token"}
    monkeypatch.delenv("INKDADDY_HA_MODE", raising=False)
    monkeypatch.setattr(api, "get_secret", stored.get)
    monkeypatch.setattr(secrets, "get_secret", stored.get)

    def save_secret(ref, value):
        stored[ref] = value
        return ref

    monkeypatch.setattr(api, "set_secret", save_secret)
    try:
        with Session(engine) as db:
            db.add(HomeAssistantConfig(
                id=api.HA_CONFIG_ID, base_url="https://ha.example.test",
                token_secret_ref=api.HA_TOKEN_REF, enabled=True,
            ))
            db.commit()
            yield db, stored
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


def test_manual_ha_change_requires_new_credential_and_keeps_old_origin_separate(manual_connection, monkeypatch):
    db, stored = manual_connection
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
    assert stored[api.HA_TOKEN_REF] == "existing-demo-token"
    assert api.save_config({"enabled": False}, db)["base_url"] == new_base


def test_manual_ha_initial_config_does_not_adopt_an_orphaned_token(manual_connection):
    db, _ = manual_connection
    db.delete(db.get(HomeAssistantConfig, api.HA_CONFIG_ID))
    db.commit()
    result = api.save_config({"base_url": "https://new-ha.example.test"}, db)
    assert result["configured"] is False
    assert service.get_connection(db) is None


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
