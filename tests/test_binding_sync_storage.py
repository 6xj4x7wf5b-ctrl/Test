import sqlite3
from contextlib import closing

import pytest

from warehouse_pc.storage import Store


GRAIN = {"kind": "grain", "point": 1, "bus": 0, "rom": "2CEE08640200EEC9"}
AMBIENT = {"kind": "ambient", "point": 5, "bus": 0, "rom": "2CEE49E18AAC1164"}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "live.sqlite3")) as database:
        yield database


def add_device(store, port_name="COM7", required=False):
    warehouse = store.warehouses()[0]["id"] if store.warehouses() else store.save_warehouse("1号仓")
    port = store.save_port(port_name, port_name)
    return store.save_device("测温分机", warehouse, port, registration_required=required)


def device(store, device_id):
    return next(row for row in store.devices() if row["id"] == device_id)


def test_reused_device_id_does_not_inherit_registration_state(store):
    device_id = add_device(store, required=True)
    store.sync_verified_bindings(device_id, [], 1)
    old = device(store, device_id)
    assert old["registered_points"] == []
    assert old["registration_required"]
    store.delete_device(device_id)
    assert store.get_setting(f"binding_sync:{device_id}") is None
    assert store.get_setting(f"registration_required:{device_id}") is None

    # A database from an older build can already contain orphan settings.
    store.set_setting(f"binding_sync:{device_id}", {"identity": [old["port_id"], "COM7", 1], "generation": 1})
    store.set_setting(f"registration_required:{device_id}", True)
    new_id = store.save_device("替换分机", old["warehouse_id"], old["port_id"])
    assert new_id == device_id
    new = device(store, new_id)
    assert not new["bindings_verified"]
    assert new["registered_points"] is None
    assert not new["registration_required"]


def test_import_invalidates_affected_device_and_remaps_registration_policy(store, tmp_path):
    other_id = add_device(store, "COM8")
    target_id = add_device(store)
    store.sync_verified_bindings(other_id, [], 99)
    store.sync_verified_bindings(target_id, [GRAIN], 21)
    store.save_threshold(target_id, "grain", temp_high=25)
    sample = store.save_samples(target_id, [{**GRAIN, "temperature": 30, "status": "ok"}])
    store.evaluate_alarms(target_id, sample)
    history, alarms = store.history(target_id), store.alarms()
    version = store.get_binding_version(target_id)

    with closing(Store(tmp_path / "source.sqlite3")) as source:
        source_id = add_device(source, required=True)
        assert source_id == other_id and source_id != target_id
        source.sync_verified_bindings(source_id, [GRAIN], 11)
        config = source.export_config()
        assert not any(key.startswith(("binding_sync:", "registration_required:")) for key in config["settings"])
        assert "bindings_verified" not in config["devices"][0]
    other = device(store, other_id)
    target = device(store, target_id)
    config["settings"].update({
        f"registration_required:{source_id}": True,
        f"binding_sync:{source_id}": {"identity": [other["port_id"], "COM8", 1], "generation": 11},
        f" binding_sync:{target_id} ": {"identity": [target["port_id"], "COM7", 1], "generation": 11},
    })

    store.import_config(config)

    target = device(store, target_id)
    assert target["registration_required"]
    assert not target["bindings_verified"]
    assert target["registered_points"] is None
    assert store.bindings(target_id)[0]["rom"] == GRAIN["rom"]
    assert store.get_binding_version(target_id) == version
    assert not device(store, other_id)["registration_required"]
    assert device(store, other_id)["config_generation"] == 99
    assert store.history(target_id) == history
    assert store.alarms() == alarms


def test_failed_import_rolls_back_binding_changes_and_verification(store):
    device_id = add_device(store)
    store.sync_verified_bindings(device_id, [GRAIN], 8)
    before = device(store, device_id)
    config = store.export_config()
    config["devices"][0]["registration_required"] = True
    config["bindings"][0]["rom"] = AMBIENT["rom"]
    config["settings"]["point_layouts"] = {"999": {}}

    with pytest.raises(ValueError, match="未导入的设备"):
        store.import_config(config)

    assert device(store, device_id) == before


def test_snapshot_failure_rolls_back_and_empty_snapshot_retains_history(store):
    device_id = add_device(store, required=True)
    sparse_grain = {**GRAIN, "point": 165}
    assert store.sync_verified_bindings(device_id, [sparse_grain], 3) == 1
    store.save_samples(device_id, [{**sparse_grain, "temperature": 18, "status": "ok"}])
    before, history = device(store, device_id), store.history(device_id)
    store._db.executescript("""
        CREATE TRIGGER reject_snapshot BEFORE INSERT ON settings
        WHEN NEW.key LIKE 'binding_sync:%'
        BEGIN SELECT RAISE(ABORT, 'verification write failed'); END;
    """)

    with pytest.raises(sqlite3.IntegrityError, match="verification write failed"):
        store.sync_verified_bindings(device_id, [sparse_grain, AMBIENT], 4)
    assert device(store, device_id) == before

    store._db.execute("DROP TRIGGER reject_snapshot")
    assert store.sync_verified_bindings(device_id, [sparse_grain, AMBIENT], 4) == 2
    sparse = device(store, device_id)
    assert {(row["kind"], row["point"]) for row in sparse["registered_points"]} == {("grain", 165), ("ambient", 5)}
    assert store.sync_verified_bindings(device_id, [], 5) == 0
    empty = device(store, device_id)
    assert empty["registered_points"] == []
    assert empty["config_generation"] == 5
    assert (empty["grain_count"], empty["ambient_count"]) == (165, 5)
    assert store.history(device_id) == history


def test_local_binding_changes_and_endpoint_edits_clear_verification(store):
    device_id = add_device(store)
    store.sync_verified_bindings(device_id, [GRAIN], 0)
    version = store.get_binding_version(device_id)
    assert store.set_bindings(device_id, [GRAIN]) == version
    assert device(store, device_id)["bindings_verified"]

    store.set_bindings(device_id, [{**GRAIN, "rom": AMBIENT["rom"]}])
    assert device(store, device_id)["registered_points"] is None
    store.sync_verified_bindings(device_id, [GRAIN], 1)
    store.bump_binding_version(device_id)
    assert device(store, device_id)["registered_points"] is None

    store.sync_verified_bindings(device_id, [GRAIN], 2)
    current = device(store, device_id)
    for address in (2, 1):
        store.save_device(current["name"], current["warehouse_id"], current["port_id"],
                          address=address, id=device_id)
    assert not device(store, device_id)["bindings_verified"]
    store.sync_verified_bindings(device_id, [GRAIN], 3)
    for port_name in ("COM9", "COM7"):
        store.save_port(port_name, port_name, id=current["port_id"])
    assert not device(store, device_id)["bindings_verified"]


def test_non_object_verification_setting_does_not_break_devices(store):
    device_id = add_device(store)
    store.set_setting(f"binding_sync:{device_id}", ["invalid"])
    current = device(store, device_id)
    assert not current["bindings_verified"]
    assert current["registered_points"] is None
    assert current["config_generation"] is None
