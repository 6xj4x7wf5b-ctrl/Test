import json
import os

import pytest

from warehouse_pc.protocol import build_maintenance, with_crc
from warehouse_pc.transport import DIAGNOSTIC_EVENT_LIMIT, SerialManager


@pytest.fixture
def manager():
    transport = SerialManager()
    transport.configure("probe", "SIMULATOR", 1200)
    transport.connect_port("probe")
    yield transport
    transport.close_all()
    transport.deleteLater()


def test_paged_request_context_and_successful_trace(manager):
    request = build_maintenance(1, 9, b"\x01\x00\x14\x03")
    expected = {"protocol": "maintenance", "address": 1, "op": 9}
    manager.submit("probe", request, expected, context=object())
    channel = manager._channels["probe"]
    assert channel.active.retries == 1
    assert tuple(channel.active.expected[key] for key in ("table", "offset", "limit")) == (1, 20, 3)
    reply = channel.simulator.simulate_request(request)
    manager._receive(channel, reply[:5])
    manager._receive(channel, reply[5:])
    snapshot = manager.diagnostics_snapshot()
    assert snapshot["channels"][0]["baud"] == 1200
    assert [event["event"] for event in snapshot["events"]] == ["TX_SUBMIT", "RX_CHUNK", "RX_CHUNK", "PARSE_OK"]
    assert snapshot["events"][0]["bytes_hex"] == request.hex(" ").upper()
    assert snapshot["events"][-1]["received_bytes"] == len(reply)
    assert "not wire timing" in snapshot["time_basis"]
    json.dumps(snapshot)
    for mismatch in ({"offset": 0}, {"table": 0}, {"limit": 2}):
        manager.submit("probe", request, expected | mismatch)
        assert manager.diagnostics_snapshot()["events"][-1]["reason"] == "invalid_request"
    manager.submit("probe", with_crc(bytes.fromhex("01 41 09 00 00 00 15")), expected)
    assert manager.diagnostics_snapshot()["events"][-1]["reason"] == "invalid_request"


@pytest.mark.parametrize(
    ("incoming", "reason", "crc_errors", "mismatches", "partial"),
    [
        (b"", "no_data", False, False, 0),
        (b"\xfe\xaa", "unmatched_data", False, True, 0),
        (b"\x01\x41\x00\x00\x01", "unmatched_data", False, False, 5),
        (with_crc(bytes.fromhex("01 41 00 00") + bytes(18))[:-1] + b"\x00",
         "unmatched_data", True, True, 0),
    ],
)
def test_failure_trace_distinguishes_no_bytes_from_invalid_bytes(manager, incoming, reason, crc_errors, mismatches, partial):
    manager.submit("probe", build_maintenance(1, 0), {"protocol": "maintenance", "retries": 0})
    channel = manager._channels["probe"]
    manager._receive(channel, incoming)
    manager._on_timeout(channel)
    failure = manager.diagnostics_snapshot()["events"][-1]
    assert failure["event"] == "FAIL" and failure["reason"] == reason
    assert failure["received_bytes"] == len(incoming)
    assert bool(failure["crc_errors"]) == crc_errors
    assert bool(failure["mismatch_errors"]) == mismatches
    assert failure["partial_bytes"] == partial


def test_diagnostics_keep_recent_events_and_limit_byte_previews(manager):
    channel = manager._channels["probe"]
    manager._receive(channel, b"\x77")
    for _ in range(DIAGNOSTIC_EVENT_LIMIT):
        manager._receive(channel, b"\xaa" * 300)
    snapshot = manager.diagnostics_snapshot()
    assert len(snapshot["events"]) == DIAGNOSTIC_EVENT_LIMIT
    assert all(event["byte_count"] == 300 for event in snapshot["events"])
    event = snapshot["events"][-1]
    assert event["truncated"] and len(bytes.fromhex(event["bytes_hex"])) == 256
    event["byte_count"] = 0
    assert manager.diagnostics_snapshot()["events"][-1]["byte_count"] == 300


def test_retry_logs_late_data_without_accepting_it(manager):
    manager.submit("probe", build_maintenance(1, 0), {"protocol": "maintenance"})
    channel = manager._channels["probe"]
    manager._on_timeout(channel)
    manager._receive(channel, b"\x01\x41")
    assert channel.active is not None and not channel.buffer
    channel.quiet.stop()
    manager._end_drain(channel)
    manager._on_timeout(channel)
    failure = manager.diagnostics_snapshot()["events"][-1]
    assert failure["reason"] == "unmatched_data"
    assert failure["received_bytes"] == failure["late_bytes"] == 2


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="POSIX pseudo terminals are unavailable")
def test_diagnostics_report_open_qt_port_parameters(manager):
    master, slave = os.openpty()
    try:
        manager.configure("pty", os.ttyname(slave), 1200)
        manager.connect_port("pty")
        channel = next(row for row in manager.diagnostics_snapshot()["channels"] if row["profile_id"] == "pty")
        assert channel["settings_source"] == "open_qt_port"
        assert channel["connected"]
        assert tuple(channel[key] for key in ("baud", "data_bits", "parity", "stop_bits")) == (1200, 8, "N", 1)
    finally:
        manager.disconnect_port("pty")
        os.close(slave)
        os.close(master)
