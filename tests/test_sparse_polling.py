"""Sparse registered points use exact on-wire IDs and keep polling scoped."""
import pytest
from PySide6.QtCore import QObject, Signal

from warehouse_pc.controller import Controller


class RecordingRepository(QObject):
    ready = Signal()
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.calls = []

    def call(self, method, *args, callback=None, on_error=None, **kwargs):
        self.calls.append((method, args, kwargs))


def registered_device(**changes):
    return {
        "id": 7, "name": "分机七", "port_id": 3, "port_name": "simulator://sparse",
        "address": 7, "protocol": "LC-M3", "source": "simulator",
        "grain_count": 165, "ambient_count": 5,
        "bindings_verified": True, "registration_required": True,
        "registered_points": [
            {"kind": "grain", "point": 165},
            {"kind": "ambient", "point": 5},
            {"kind": "grain", "point": 1},
        ],
        **changes,
    }


@pytest.fixture
def controller(qt_application):
    control = Controller(RecordingRepository(), demo=True)
    control.metadata = {
        "ports": [{"id": 3, "port_name": "simulator://sparse", "baud": 1200, "source": "simulator"}],
        "warehouses": [], "devices": [registered_device()],
    }
    control.connect_all()
    try:
        yield control
    finally:
        control.shutdown()


def test_lcm3_sparse_query_uses_last_ambient_channel_and_actual_grain_ids(controller):
    jobs = controller._requests(controller.device(7))
    assert len(jobs) == 2
    assert [wire[:7] for wire, _ in jobs] == [
        bytes((0xF7, 0, 7, 0xD0, 1, 1, 5)),
        bytes((0xF7, 0, 7, 0xD0, 165, 1, 5)),
    ]
    assert all(expected["ambient_channel"] == 5 for _, expected in jobs)


def test_modbus_sparse_queries_use_actual_value_and_status_addresses(controller):
    jobs = controller._requests(registered_device(protocol="Modbus RTU"))
    assert len(jobs) == 6
    assert all(wire[:2] == bytes((7, 3)) for wire, _ in jobs)
    assert [(int.from_bytes(wire[2:4], "big"), int.from_bytes(wire[4:6], "big")) for wire, _ in jobs] == [
        (0x0001, 1), (0x4000, 1),
        (0x00A5, 1), (0x40A4, 1),
        (0x2008, 2), (0x40A9, 1),
    ]


@pytest.mark.parametrize("verified", [False, True])
def test_registration_required_without_points_does_not_create_empty_cycle(controller, monkeypatch, verified):
    controller.metadata["devices"] = [registered_device(bindings_verified=verified, registered_points=[])]
    submissions, messages = [], []
    monkeypatch.setattr(controller.manager, "submit", lambda *args: submissions.append(args))
    controller.log.connect(messages.append)
    controller.poll()
    controller.poll(device_id=7)
    assert submissions == [] and controller.cycles == {}
    assert not controller.processing_devices
    assert messages and "尚无已登记测点" in messages[-1]


def test_manual_poll_submits_only_selected_device_and_stores_only_registered_rows(controller, monkeypatch):
    controller.metadata["devices"].append(registered_device(id=8, address=8))
    submissions = []
    monkeypatch.setattr(controller.manager, "submit", lambda *args: submissions.append(args))
    controller.poll(device_id=7)
    assert [(profile, wire[2]) for profile, wire, _, _ in submissions] == [("3", 7), ("3", 7)]
    assert len(controller.cycles) == 1
    for profile, wire, _, context in submissions:
        samples = [{"kind": "grain", "point": wire[4], "temperature": 20, "status": 0},
                   {"kind": "ambient", "point": 5, "temperature": 22, "humidity": 60, "status": 0}]
        controller.manager.response.emit(profile, {"context": context, "parsed": {"samples": samples}}, b"")
    assert controller.cycles == {}
    method, (device, rows), _ = controller.repo.calls[-1]
    assert method == "save_samples" and device == 7
    assert {(row["kind"], row["point"], row["status"]) for row in rows} == {
        ("grain", 1, 0), ("grain", 165, 0), ("ambient", 5, 0),
    }


def test_reconnect_requires_fresh_response_even_if_previous_ping_was_in_flight(controller, qtbot):
    device = controller.device(7)
    controller.query_online(device)
    qtbot.waitUntil(lambda: controller.communication_state(device) == "分机已响应")
    qtbot.waitUntil(lambda: not controller.manager.busy("3"))
    controller.query_online(device)
    assert controller.manager.pending_count("3") == 1
    controller.manager.disconnect_port("3")
    assert controller.communication_state(device) == "串口未打开"
    controller.connect_all()
    assert controller.communication_state(device) == "串口已打开 · 待查询"
    qtbot.wait(70)
    assert controller.communication_state(device) == "串口已打开 · 待查询"
    controller.query_online(device)
    qtbot.waitUntil(lambda: controller.communication_state(device) == "分机已响应")
