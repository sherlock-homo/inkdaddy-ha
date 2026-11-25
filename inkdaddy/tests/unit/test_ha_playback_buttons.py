from contextlib import nullcontext

import pytest

from inkdaddy_hub.services import ha_playback_buttons as buttons
from inkdaddy_hub.services.ha_matter_share import HAMatterShareError


UID = "xiao-mg24-00000001"
DEVICE = "a" * 32
ROOT = "0000000000000001-0000000000000002-MatterNodeDevice"


class Core:
    def __init__(self, monkeypatch):
        self.devices = [{"model": "inkDaddy", "id": DEVICE, "identifiers": [
            ["other"], ["matter", "deviceid_" + ROOT], ["matter", "serial_" + UID]
        ]}]
        self.switches = [{
            "device_id": DEVICE, "platform": "matter", "id": str(n) * 32, "entity_id": f"switch.renamed_{n}",
            "unique_id": f"{ROOT}-{n}-MatterPlug-6-0", "name": f"Owner name {n}",
        } for n in range(1, 5)]
        self.entries = {}
        self.entities = [*self.switches,
                         {"device_id": DEVICE, "platform": "matter", "entity_id": "button.identify"},
                         dict(self.switches[0], device_id="b"*32, entity_id="switch.other")]
        self.flows = {}
        self.hidden = []
        self.fail = None
        monkeypatch.setattr(buttons, "_authenticated_core", lambda: nullcontext("ws"))
        monkeypatch.setattr(buttons, "_command", self.command)
        monkeypatch.setattr(buttons, "_flow_api", self.api)
        monkeypatch.setattr(buttons.time, "sleep", lambda _: None)

    def command(self, ws, number, command, timeout, **data):
        if command.endswith("device_registry/list"):
            return self.devices
        if command.endswith("entity_registry/list"):
            return self.entities
        assert command.endswith("entity_registry/update")
        entity = next(row for row in self.entities if row["entity_id"] == data["entity_id"])
        assert entity in self.switches
        entity["hidden_by"] = data["hidden_by"]
        self.hidden.append(entity["entity_id"])
        return {}

    def api(self, path, data, *, method="POST"):
        if path in {"flow", "options/flow"}:
            flow_id = f"{len(self.flows) + len(self.entries) + 1:032x}"
            self.flows[path + "/" + flow_id] = data["handler"]
            if path == "flow":
                assert data == {"handler": "template"}
                return {"flow_id": flow_id, "type": "menu", "menu_options": ["button"]}
            return {"flow_id": flow_id, "type": "form", "step_id": "button", "data_schema": [
                {"name": k, "description": {"suggested_value": v}}
                for k, v in self.entries[data["handler"]].items()]}
        assert path in self.flows
        if method == "DELETE":
            self.flows.pop(path)
            return {}
        if data == {"next_step_id": "button"}:
            return {"type": "form", "step_id": "button"}
        if self.fail == data["name"]:
            raise HAMatterShareError("Service interrupted")
        entry = f"{len(self.entries)+1:026}"
        self.entries[entry] = data
        self.flows.pop(path)
        self.entities.append({"device_id": DEVICE, "platform": "template", "entity_id": f"button.example_{entry}",
                              "config_entry_id": entry, "unique_id": entry})
        return {"type": "create_entry", "result": {"domain": "template", "entry_id": entry}}


def test_buttons_are_native_device_helpers_and_retry_preserves_names(monkeypatch):
    core = Core(monkeypatch)
    assert buttons.install_ha_playback_buttons(UID) == {"buttons": 4, "created": 4}
    assert sorted(core.hidden) == sorted(row["entity_id"] for row in core.switches)
    for index, data in enumerate(core.entries.values()):
        assert data["name"] == list(buttons._NAMES.values())[index]
        assert data["press"] == [{"domain": "switch", "type": "toggle", "device_id": DEVICE,
                                 "entity_id": core.switches[index]["id"]}]
    core.entities[-1]["name"] = "Keep my name"
    core.switches[0]["entity_id"] = "switch.my_name"
    assert buttons.install_ha_playback_buttons(UID) == {"buttons": 4, "created": 0}
    assert len(core.hidden) == 4 and not core.flows
    assert core.entities[-1]["name"] == "Keep my name"


def test_interrupted_creation_reuses_buttons_before_hiding(monkeypatch):
    core = Core(monkeypatch)
    core.fail = "Previous"
    with pytest.raises(HAMatterShareError):
        buttons.install_ha_playback_buttons(UID)
    assert len(core.entries) == 1 and not core.hidden and not core.flows
    core.fail = None
    assert buttons.install_ha_playback_buttons(UID)["created"] == 3


@pytest.mark.parametrize("broken", ["identity", "source", "disabled"])
def test_wrong_identity_disabled_or_missing_source_blocks_write(monkeypatch, broken):
    core = Core(monkeypatch)
    if broken == "identity":
        core.devices.append(dict(core.devices[0], id="c"*32))
    elif broken == "source":
        core.switches[0]["unique_id"] = "unexpected"
    else:
        core.switches[0]["disabled_by"] = "user"
    with pytest.raises(HAMatterShareError):
        buttons.install_ha_playback_buttons(UID)
    assert not core.hidden and not core.entries
