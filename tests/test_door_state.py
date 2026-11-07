"""Door-state attribute compatibility across Query and push snapshots."""

from itertools import combinations

import pytest

from utec_client.devices.lock import Lock

# Ordered by attribute preference first, then capability spelling.
ALIASES = (
    ("st.DoorSensor", "doorState"),
    ("st.doorSensor", "doorState"),
    ("st.DoorSensor", "sensorState"),
    ("st.doorSensor", "sensorState"),
)


def state(alias, value):
    return {"capability": alias[0], "name": alias[1], "value": value}


@pytest.fixture(params=["utec-lock", "utec-lock-sensor"])
def lock(request, discovery_dict, mock_api):
    return Lock(discovery_dict(handle_type=request.param, category="SmartLock"), mock_api)


@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("value", ["Open", "Closed", "Unrecognized"])
def test_attribute_values_and_sensor_detection(lock, alias, value):
    lock._state_data = {"states": [state(alias, value)]}
    assert lock.has_door_sensor is True
    assert lock.door_state == value
    assert lock.is_door_open is (value == "Open")


@pytest.mark.parametrize("preferred, fallback", list(combinations(ALIASES, 2)))
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("value", ["Open", "Closed", "Unrecognized"])
def test_alias_precedence_ignores_payload_order(lock, preferred, fallback, reverse, value):
    states = [state(preferred, value), state(fallback, "Closed" if value == "Open" else "Open")]
    lock._state_data = {"states": states[::-1] if reverse else states}
    assert lock.door_state == value
    assert lock.is_door_open is (value == "Open")


@pytest.mark.parametrize("winner", range(len(ALIASES)))
@pytest.mark.parametrize("falsey", [None, "", 0, False])
@pytest.mark.parametrize("missing_value", [False, True])
def test_falsey_preferred_aliases_fall_through(lock, winner, falsey, missing_value):
    states = [state(alias, falsey) for alias in ALIASES[:winner]]
    if missing_value:
        for entry in states:
            del entry["value"]
    states.append(state(ALIASES[winner], "Open"))
    lock._state_data = {"states": states}
    assert lock.door_state == "Open"
    assert lock.is_door_open is True


@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("value", [None, "", 0, False])
def test_falsey_only_preserves_final_lookup_return(lock, alias, value):
    lock._state_data = {"states": [state(alias, value)]}
    expected = value if alias == ALIASES[-1] else None
    assert lock.has_door_sensor is True
    assert lock.door_state is expected
    assert lock.is_door_open is False


@pytest.mark.parametrize("snapshot", [None, {}, {"states": []}])
def test_no_sensor_state_preserves_discovery_semantics(lock, snapshot):
    lock._state_data = snapshot
    assert lock.door_state is None
    assert lock.is_door_open is (False if lock.has_door_sensor else None)


@pytest.mark.parametrize("alias", ALIASES)
def test_sensor_entry_without_value(lock, alias):
    lock._state_data = {"states": [{"capability": alias[0], "name": alias[1]}]}
    assert lock.has_door_sensor is True
    assert lock.door_state is None
    assert lock.is_door_open is False


@pytest.mark.parametrize("alias", ALIASES)
def test_duplicate_pair_retains_first_lookup_match(lock, alias):
    lock._state_data = {"states": [state(alias, "Closed"), state(alias, "Open")]}
    assert lock.door_state == "Closed"
    assert lock.is_door_open is False


@pytest.mark.parametrize("entry_point", ["query", "push"])
@pytest.mark.parametrize("alias", ALIASES)
async def test_update_paths_track_door_transitions(lock, mock_api, entry_point, alias):
    for value in ("Open", "Closed", "Open"):
        snapshot = {"id": lock.device_id, "states": [state(alias, value)]}
        if entry_point == "query":
            mock_api.query_device.return_value = {"payload": {"devices": [snapshot]}}
            await lock.update()
            mock_api.query_device.assert_awaited_with(lock.device_id)
        else:
            await lock.update_state_data(snapshot)
        assert lock.has_door_sensor is True
        assert lock.door_state == value
        assert lock.is_door_open is (value == "Open")
