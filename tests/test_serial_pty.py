"""Exercise QSerialPort itself using a local pseudo-terminal, never a device."""
import os
import time

import pytest
from PySide6.QtCore import QSocketNotifier, QTimer
from PySide6.QtTest import QTest

from warehouse_pc.protocol import build_lc_request, build_modbus_read
from warehouse_pc.simulator import ProtocolSimulator
from warehouse_pc.transport import SerialManager


@pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminals only; Windows needs its own serial bench")
def test_real_qserialport_fragmented_long_frames(qt_application):
    try:
        master, slave = os.openpty()
    except (AttributeError, OSError) as exc:
        pytest.skip(f"Pseudo-terminal unavailable: {exc}")
    os.set_blocking(master, False)
    simulator = ProtocolSimulator()
    manager = SerialManager()
    responses, errors, received = [], [], bytearray()
    jobs = [
        (build_lc_request(1, 0xD0, bytes((1, 165, 1))),
         {"protocol": "lc", "address": 1, "command": 0xD0, "start": 1, "count": 165, "ambient_channel": 1}),
        (build_modbus_read(1, 1, 125),
         {"protocol": "modbus", "address": 1, "start": 1, "count": 125}),
        (build_modbus_read(1, 126, 40),
         {"protocol": "modbus", "address": 1, "start": 126, "count": 40}),
    ]
    server_index = 0
    notifier = QSocketNotifier(master, QSocketNotifier.Type.Read)

    def server_read(*_):
        nonlocal server_index
        try:
            received.extend(os.read(master, 4096))
        except BlockingIOError:
            return
        if server_index >= len(jobs):
            return
        request, expected = jobs[server_index]
        if len(received) < len(request):
            return
        assert bytes(received[:len(request)]) == request
        del received[:len(request)]
        frame = simulator.simulate_request(request, expected)
        # Readiness arrives multiple times and a little noise precedes valid data.
        pieces = [b"\x99\xAA", frame[:2], frame[2:9], frame[9:64], frame[64:]]
        for i, part in enumerate(pieces):
            if part:
                QTimer.singleShot(5 + i * 8, lambda payload=part: os.write(master, payload))
        server_index += 1

    notifier.activated.connect(server_read)
    manager.response.connect(lambda profile, result, wire: responses.append((profile, result, wire)))
    manager.request_failed.connect(lambda profile, context, message: errors.append(message))
    try:
        manager.configure("pty", os.ttyname(slave), 9600)
        manager.connect_port("pty")
        assert manager.is_connected("pty")
        for index, (request, expected) in enumerate(jobs):
            manager.submit("pty", request, expected, {"sequence": index})
        deadline = time.monotonic() + 8
        while len(responses) < len(jobs) and not errors and time.monotonic() < deadline:
            qt_application.processEvents()
            QTest.qWait(5)
        assert not errors
        assert len(responses) == 3
        assert [result["context"]["sequence"] for _, result, _ in responses] == [0, 1, 2]
        assert [len(wire) for _, _, wire in responses] == [343, 255, 85]
        assert responses[0][1]["parsed"]["samples"][0].temperature == -19.0
        assert responses[2][1]["parsed"]["samples"][-1].point == 165
        assert all(not result["parsed"]["simulated"] for _, result, _ in responses)
    finally:
        manager.close_all()
        notifier.setEnabled(False)
        notifier.deleteLater()
        os.close(master)
        os.close(slave)
