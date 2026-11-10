"""Protocol and Qt-simulator integration tests; never open a hardware port."""
from __future__ import annotations

import struct
import time
import unittest

from warehouse_pc.protocol import (
    DeviceException, ProtocolError, Sample, STATUS_UNAVAILABLE,
    build_lc_request, build_maintenance, build_modbus_read, crc8, crc16,
    parse_lc_response, parse_maintenance, parse_modbus_response, parse_response,
    response_length, with_crc,
)
from warehouse_pc.simulator import ProtocolSimulator, simulate_request


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.simulator = ProtocolSimulator(self.clock)

    def maint(self, op, payload=b"", address=1):
        request = build_maintenance(address, op, payload)
        return parse_maintenance(self.simulator.simulate_request(request), address=address, op=op)

    def test_builders_match_documented_crc(self):
        self.assertEqual(build_modbus_read(2, 5, 31), bytes.fromhex("02 03 00 05 00 1F 14 30"))
        self.assertEqual(crc16(b"123456789"), 0x4B37)
        self.assertEqual(crc8(b"123456789"), 0xA1)
        self.assertEqual(len(build_lc_request(255, 0xD9)), 22)
        self.assertEqual(build_lc_request(1, 0xD0, b"\x01\xA5\x01")[:7],
                         bytes.fromhex("F7 00 01 D0 01 A5 01"))
        for address in (0, 248, -1):
            with self.assertRaises(ProtocolError):
                build_modbus_read(address, 1, 1)
        for start, count in ((65535, 2), (1, 0), (1, 126)):
            with self.assertRaises(ProtocolError):
                build_modbus_read(1, start, count)
        with self.assertRaises(ProtocolError):
            build_lc_request(1, 0xD9, bytes(19))
        with self.assertRaises(ProtocolError):
            build_maintenance(0, 5)

    def test_negative_integer_zero_and_unknown_fault(self):
        frame = with_crc(bytes.fromhex("01 03 08 13 10 13 18 00 00 FF FF"))
        parsed = parse_modbus_response(frame, address=1, start=1, count=4, kind="grain", protocol="modbus")
        self.assertEqual([sample.temperature for sample in parsed["samples"]], [-19.0, -19.8, 0.0, None])
        self.assertEqual(parsed["samples"][-1].status, STATUS_UNAVAILABLE)
        self.assertEqual(parse_modbus_response(frame)["registers"], [0x1310, 0x1318, 0, 0xFFFF])
        # Fixed legacy example: no NTC compensation, decimal ten sign offset.
        legacy = bytes.fromhex("F8 8F 01 D0 00 00 13 0A 13 12 12 07 37 04 00 FD DF")
        parsed = parse_lc_response(legacy, 0xD0, address=1, start=1, count=2, ambient_channel=1)
        self.assertEqual([sample.temperature for sample in parsed["samples"]], [-19.0, -19.8, 18.7])
        self.assertEqual(parsed["samples"][-1].humidity, 55.4)

    def test_full_grain_and_air_maps(self):
        samples = []
        for start, count in ((1, 125), (126, 40)):
            request = build_modbus_read(247, start, count)
            frame = self.simulator.simulate_request(request)
            if count == 125:
                self.assertEqual(len(frame), 255)
            samples.extend(parse_modbus_response(frame, address=247, start=start, count=count)["samples"])
        self.assertEqual(len(samples), 165)
        self.assertEqual([samples[0].point, samples[-1].point], [1, 165])
        self.assertEqual(samples[0].temperature, -19.0)
        request = build_modbus_read(1, 0x2000, 10)
        air = parse_modbus_response(self.simulator.simulate_request(request), start=0x2000, count=10)["samples"]
        self.assertEqual([item.point for item in air], [1, 2, 3, 4, 5])
        self.assertTrue(all(item.humidity is not None for item in air))
        request = build_modbus_read(1, 0x2001, 1)
        with self.assertRaises(ProtocolError):
            parse_modbus_response(self.simulator.simulate_request(request), start=0x2001, count=1)
        self.simulator.set_status(1, "grain", 17, 3)
        request = build_modbus_read(1, 0x4010, 1)
        statuses = parse_modbus_response(self.simulator.simulate_request(request), start=0x4010)["statuses"]
        self.assertEqual(statuses, [{"kind": "grain", "point": 17, "status": 3}])

    def test_long_legacy_and_sparse_span(self):
        request = build_lc_request(1, 0xD0, bytes((1, 165, 1)))
        frame = self.simulator.simulate_request(request)
        self.assertEqual(len(frame), 343)
        expected = {"protocol": "lc", "address": 1, "command": 0xD0,
                    "start": 1, "count": 165, "ambient_channel": 1}
        self.assertEqual(response_length(frame[:10], expected), 343)
        parsed = parse_response(frame, expected)
        self.assertEqual(len(parsed["samples"]), 166)
        self.assertEqual(parsed["samples"][164].point, 165)
        self.assertEqual(parsed["samples"][-1].kind, "ambient")
        board = self.simulator.board(1)
        board.bindings = {key: value for key, value in board.bindings.items()
                          if key[0] != "grain" or key[1] in (1, 12)}
        frame = self.simulator.simulate_request(build_lc_request(1, 0xD8))
        self.assertEqual(parse_lc_response(frame, 0xD8)["grain_span"], 12)
        self.assertEqual(frame[4:8], b"\x0C" * 4)
        frame = self.simulator.simulate_request(build_lc_request(1, 0xD2, b"\x05"))
        self.assertEqual(parse_lc_response(frame, 0xD2, point=5)["samples"][0].point, 5)
        with self.assertRaises(ProtocolError):
            parse_lc_response(frame, 0xD2, point=4)
        frame = self.simulator.simulate_request(build_lc_request(1, 0xD1, bytes((1, 2, 1, 1))))
        self.assertEqual(parse_lc_response(frame, 0xD1, ambient_channel=1)["maximum"], -19.0)

    def test_bad_frames_and_device_mismatch(self):
        frame = self.simulator.simulate_request(build_modbus_read(1, 1, 2))
        for broken in (frame[:-1], frame[:-1] + bytes((frame[-1] ^ 1,)), b"", frame + b"\0"):
            with self.assertRaises(ProtocolError):
                parse_modbus_response(broken)
        with self.assertRaises(ProtocolError):
            parse_modbus_response(frame, address=2)
        with self.assertRaises(ProtocolError):
            parse_modbus_response(frame, count=3)
        with self.assertRaises(ProtocolError):
            parse_modbus_response(with_crc(bytes.fromhex("01 03 02 13 1A")), start=1)
        invalid = self.simulator.simulate_request(build_modbus_read(1, 0x1001, 2))
        with self.assertRaises(DeviceException) as caught:
            parse_modbus_response(invalid)
        self.assertEqual(caught.exception.code, 2)
        legacy = self.simulator.simulate_request(build_lc_request(1, 0xD9))
        with self.assertRaises(ProtocolError):
            parse_lc_response(legacy, 0xD9, address=2)
        with self.assertRaises(ProtocolError):
            parse_lc_response(legacy[:-1] + b"\x00", 0xD9)
        self.assertEqual(self.simulator.simulate_request(bytes.fromhex("00 03 00 01 00 01 D5 DB")), b"")

    def test_scan_all_probes_and_rom_validation(self):
        self.assertEqual(self.maint(1, b"\0")["result"], 0)
        self.assertEqual(self.maint(0)["info"]["scan_status"], 4)
        self.assertEqual(self.maint(2, b"\0\0\x1B")["result"], 4)
        self.clock.now += 0.25
        info = self.maint(0)["info"]
        self.assertEqual((info["scan_status"], info["discovered_count"]), (0, 170))
        records = []
        for offset in range(0, 170, 27):
            page = self.maint(2, struct.pack(">HB", offset, 27))
            records.extend(page["records"])
        self.assertEqual(len(records), 170)
        self.assertEqual(len({record["rom"] for record in records}), 170)
        self.assertTrue(all(crc8(record["rom"][:7]) == record["rom"][7] for record in records))
        request = build_maintenance(1, 2, b"\0\0\x1B")
        frame = bytearray(self.simulator.simulate_request(request))
        self.assertEqual(len(frame), 254)
        frame[10] ^= 1
        frame = with_crc(frame[:-2])
        with self.assertRaises(ProtocolError):
            parse_maintenance(frame)

    def test_maintenance_roundtrips_roms_without_family_list(self):
        field_roms = tuple(map(bytes.fromhex, ("2CEE08640200EEC9", "2CEE49E18AAC1164")))
        old_rom = self.maint(7, bytes((1, 0, 1, 0)))["binding"]["rom"]
        other_roms = []
        for first_byte in (0x00, 0xFF):
            body = bytes((first_byte, 0xA5)) + bytes(5)
            other_roms.append(body + bytes((crc8(body),)))
        roms = (*field_roms, old_rom, *other_roms)
        scan = with_crc(b"\x01\x41\x02\x00" + struct.pack(">HHB", len(roms), 0, len(roms))
                        + b"".join(b"\0" + rom for rom in roms))
        self.assertEqual([record["rom"] for record in parse_maintenance(scan)["records"]], list(roms))

        for point, rom in enumerate((*field_roms, *other_roms), start=2):
            self.assertEqual(self.maint(3, bytes((1, 0, point, 0)) + rom)["result"], 0)
        self.assertEqual(self.maint(3, bytes((1, 0, 7, 0)) + field_roms[0])["result"], 3)
        self.assertEqual(self.maint(5)["result"], 0)
        for point, rom in enumerate((old_rom, *field_roms, *other_roms), start=1):
            self.assertEqual(self.maint(7, bytes((1, 0, point, 0)))["binding"]["rom"], rom)

    def test_maintenance_rejects_invalid_roms_in_requests_and_responses(self):
        key_and_bus = bytes((1, 0, 1, 0))
        for rom in (bytes(8), b"\xff" * 8, bytes.fromhex("2CEE08640200EEC8"), bytes(7)):
            with self.subTest(rom=rom.hex()):
                with self.assertRaises(ProtocolError):
                    build_maintenance(1, 3, key_and_bus + rom)
                raw_request = with_crc(b"\x01\x41\x03" + key_and_bus + rom)
                self.assertEqual(parse_maintenance(self.simulator.simulate_request(raw_request))["result"], 2)
                scan = with_crc(b"\x01\x41\x02\x00\x00\x01\x00\x00\x01\x00" + rom)
                binding = with_crc(b"\x01\x41\x07\x00" + key_and_bus + b"\x00" + rom)
                for response in (scan, binding):
                    with self.assertRaises(ProtocolError):
                        parse_maintenance(response)

    def test_staged_save_discard_duplicate_and_timestamp(self):
        initial = self.maint(0)["info"]
        key = bytes((1, 0, 12))
        self.assertEqual(self.maint(4, key)["result"], 0)
        self.assertEqual(self.maint(7, key + b"\0")["result"], 0)
        self.assertEqual(self.maint(7, key + b"\1")["result"], 7)
        self.assertTrue(self.maint(0)["info"]["dirty"])
        self.assertEqual(self.maint(6)["result"], 0)
        self.assertFalse(self.maint(0)["info"]["dirty"])
        self.assertEqual(self.maint(7, key + b"\1")["result"], 0)
        first = self.maint(7, bytes((1, 0, 1, 0)))["binding"]
        duplicate = key + b"\0" + first["rom"]
        self.assertEqual(self.maint(3, duplicate)["result"], 3)
        self.maint(4, key)
        self.assertEqual(self.maint(5)["result"], 0)
        after = self.maint(0)["info"]
        self.assertEqual(after["active_count"], 169)
        self.assertEqual(after["generation"], initial["generation"] + 1)
        self.maint(5)
        self.assertEqual(self.maint(0)["info"]["generation"], after["generation"])
        self.clock.now += 2.5
        sample = self.maint(8, bytes((1, 0, 1)))
        self.assertEqual(sample["sample"].temperature, -19.0)
        self.assertEqual(sample["updated_ms"], 2000)
        lost = self.maint(8, key)
        self.assertEqual(lost["sample"].status, 1)
        self.assertIsNone(lost["sample"].temperature)

    def test_binding_pages_full_table_and_old_single_read(self):
        first_rom = bytes.fromhex("2C EE 08 64 02 00 EE C9")
        second_rom = bytes.fromhex("2C EE 49 E1 8A AC 11 64")
        for point, rom in enumerate((first_rom, second_rom), 1):
            self.assertEqual(self.maint(3, bytes((1, 0, point, 0)) + rom)["result"], 0)
        self.assertEqual(self.maint(5)["result"], 0)
        expected_revision = self.maint(0)["info"]["generation"]
        records, lengths = [], []
        for offset in range(0, 170, 20):
            expected = dict(protocol="maintenance", address=1, op=9,
                            table=0, offset=offset, limit=20, bus_count=1)
            frame = self.simulator.simulate_request(build_maintenance(1, 9, struct.pack(">BHB", 0, offset, 20)))
            for size in range(10):
                self.assertIsNone(response_length(frame[:size], expected))
            self.assertEqual(response_length(frame[:10], expected), len(frame))
            page = parse_response(frame, expected)
            self.assertEqual((page["table"], page["total"], page["offset"], page["revision"]),
                             (0, 170, offset, expected_revision))
            lengths.append(len(frame))
            records.extend(page["records"])
        self.assertEqual(lengths, [256] * 8 + [136])
        self.assertEqual(len({record["rom"] for record in records}), 170)
        self.assertEqual([record["rom"] for record in records[:2]], [first_rom, second_rom])
        self.assertEqual((records[-1]["kind"], records[-1]["point"]), ("ambient", 5))
        self.assertEqual(self.maint(7, b"\x01\x00\x02\x00")["binding"]["rom"], second_rom)

    def test_binding_page_revisions_and_empty_end(self):
        board = self.simulator.board(1)
        board.bindings.clear()
        board.staged.clear()
        page = lambda table, offset=0: self.maint(9, struct.pack(">BHB", table, offset, 20))
        empty = page(1)
        self.assertEqual((empty["total"], empty["records"], len(empty["data"])), (0, [], 10))
        original_active, original_staged = page(0)["revision"], empty["revision"]
        key = b"\x01\x00\x01"
        payload = key + b"\x00" + bytes.fromhex("2CEE08640200EEC9")
        self.assertEqual(self.maint(3, payload)["result"], 0)
        self.assertEqual(page(1)["revision"], original_staged + 1)
        self.assertEqual(page(0)["revision"], original_active)
        self.assertEqual(self.maint(3, payload)["result"], 0)
        self.assertEqual(page(1)["revision"], original_staged + 1)
        self.assertEqual(page(1, 1)["records"], [])
        self.assertEqual(page(1, 2)["result"], 1)
        self.assertEqual(self.maint(5)["result"], 0)
        self.assertEqual((page(0)["revision"], page(1)["revision"]),
                         (original_active + 1, original_staged + 2))
        self.assertEqual(self.maint(4, key)["result"], 0)
        self.assertEqual(page(1)["revision"], original_staged + 3)
        self.assertEqual(self.maint(6)["result"], 0)
        self.assertEqual((page(1)["total"], page(1)["revision"]), (1, original_staged + 4))

    def test_binding_page_rejects_invalid_framing_and_records(self):
        expected = dict(protocol="maintenance", address=1, op=9, table=0, offset=0, limit=20, bus_count=1)
        for code in (1, 2):
            old_response = with_crc(bytes((1, 0x41, 9, code)))
            self.assertEqual(response_length(old_response, expected), 6)
            self.assertEqual(parse_response(old_response, expected)["result"], code)
        for payload in (b"", b"\x02\x00\x00\x01", b"\x00\x00\x00\x00", b"\x00\x00\x00\x15"):
            with self.assertRaises(ProtocolError):
                build_maintenance(1, 9, payload)
        header = b"\x01\x41\x09\x00" + struct.pack(">BHHBI", 0, 2, 0, 2, 1)
        first = b"\x01\x00\x01\x00" + bytes.fromhex("2CEE08640200EEC9")
        second = b"\x01\x00\x02\x00" + bytes.fromhex("2CEE49E18AAC1164")
        wire = with_crc(header + first + second)
        for index, value in ((4, 1), (6, 171), (8, 1), (9, 21), (14, 3), (16, 0), (17, 1)):
            corrupted = bytearray(wire[:-2])
            corrupted[index] = value
            with self.subTest(index=index), self.assertRaises(ProtocolError):
                parse_response(with_crc(corrupted), expected)
        invalid_pages = [
            with_crc(header + first),
            with_crc(header + first + first),
            with_crc(header + first + second[:4] + first[4:]),
            with_crc(header + first[:4] + bytes(8) + second),
            with_crc(header + first[:-1] + b"\xc8" + second),
            wire[:-1] + bytes((wire[-1] ^ 1,)),
        ]
        for frame in invalid_pages:
            with self.assertRaises(ProtocolError):
                parse_response(frame, expected)

    def test_profiles_and_board_addresses_are_isolated(self):
        one, two = ProtocolSimulator(), ProtocolSimulator()
        unbind = build_maintenance(1, 4, bytes((1, 0, 1)))
        save = build_maintenance(1, 5)
        one.simulate_request(unbind)
        one.simulate_request(save)
        info = build_maintenance(1, 0)
        self.assertEqual(parse_maintenance(one.simulate_request(info))["info"]["active_count"], 169)
        self.assertEqual(parse_maintenance(two.simulate_request(info))["info"]["active_count"], 170)
        info_other_address = build_maintenance(2, 0)
        self.assertEqual(parse_maintenance(one.simulate_request(info_other_address))["info"]["active_count"], 170)
        profile = f"test-{id(self)}"
        simulate_request(unbind, profile_id=profile)
        simulate_request(save, profile_id=profile)
        self.assertEqual(parse_maintenance(simulate_request(info, profile_id=profile))["info"]["active_count"], 169)
        self.assertEqual(parse_maintenance(simulate_request(info, profile_id=profile + "-other"))["info"]["active_count"], 170)


try:
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
    from warehouse_pc.transport import SerialManager
except ImportError:
    SerialManager = None


@unittest.skipIf(SerialManager is None, "PySide6 not installed")
class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.manager = SerialManager()
        self.responses, self.failures, self.request_failures = [], [], []
        self.manager.response.connect(lambda profile, parsed, wire: self.responses.append((profile, parsed, wire)))
        self.manager.failure.connect(lambda profile, message: self.failures.append((profile, message)))
        self.manager.request_failed.connect(lambda profile, context, message: self.request_failures.append((profile, context, message)))
        for profile in ("a", "b"):
            self.manager.configure(profile, "SIMULATOR" if profile == "a" else "simulator://2")
            self.manager.connect_port(profile)

    def tearDown(self):
        self.manager.close_all()
        self.manager.deleteLater()
        self.app.processEvents()

    def until(self, predicate, timeout=2000):
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(5)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        poll.start()
        timer.start(timeout)
        if not predicate():
            loop.exec()
        poll.stop()
        timer.stop()
        self.assertTrue(predicate(), f"timeout; failures={self.failures}")

    def test_parallel_ports_queue_and_callback_submit(self):
        self.manager.submit("a", build_modbus_read(1, 1, 125),
                            {"protocol": "modbus", "address": 1, "start": 1, "count": 125}, "first")
        self.manager.submit("a", build_modbus_read(1, 126, 40),
                            {"protocol": "modbus", "address": 1}, "second")
        self.manager.submit("b", build_lc_request(1, 0xD0, bytes((1, 165, 1))),
                            {"protocol": "lc", "address": 1, "command": 0xD0}, "other-port")
        self.assertTrue(self.manager.busy("a"))
        self.until(lambda: len(self.responses) == 3)
        self.assertFalse(self.failures)
        self.assertEqual([item[1]["context"] for item in self.responses if item[0] == "a"], ["first", "second"])
        self.assertTrue(all(item[1]["parsed"]["simulated"] for item in self.responses))
        self.assertTrue(self.manager.is_simulator("b"))
        self.assertEqual(len(next(item[2] for item in self.responses if item[0] == "b")), 343)
        self.until(lambda: not self.manager.busy("a") and not self.manager.busy("b"))
        # Submitting from a synchronous response callback must not bypass the queue.
        def next_read(profile, result, wire):
            if result["context"] == "callback-first":
                self.manager.submit("a", build_modbus_read(1, 3, 1), {"protocol": "modbus"}, "callback-second")
        self.manager.response.connect(next_read)
        self.manager.submit("a", build_modbus_read(1, 1, 1), {"protocol": "modbus"}, "callback-first")
        self.until(lambda: len(self.responses) == 5)
        self.assertEqual(self.responses[-1][1]["context"], "callback-second")

    def test_simulated_config_stays_with_its_profile(self):
        self.manager.submit("a", build_maintenance(1, 4, bytes((1, 0, 1))), {"protocol": "maintenance", "op": 4})
        self.manager.submit("a", build_maintenance(1, 5), {"protocol": "maintenance", "op": 5})
        self.until(lambda: len(self.responses) == 2)
        self.manager.disconnect_port("a")
        self.manager.connect_port("a")
        for profile in ("a", "b"):
            self.manager.submit(profile, build_maintenance(1, 0), {"protocol": "maintenance", "op": 0})
        self.until(lambda: len(self.responses) == 4)
        counts = {profile: result["parsed"]["info"]["active_count"]
                  for profile, result, _ in self.responses if result["parsed"].get("op") == 0}
        self.assertEqual(counts, {"a": 169, "b": 170})

    def test_wrong_device_bad_crc_fragmentation_and_exception(self):
        channel = self.manager._channels["a"]
        simulator = channel.simulator
        original = simulator.simulate_request
        def mixed(request, expected):
            correct = original(request, expected)
            unrelated = bytearray(correct)
            unrelated[0] = 2
            unrelated = with_crc(unrelated[:-2])
            corrupt = correct[:-1] + bytes((correct[-1] ^ 1,))
            # Matching address/function with an impossible-for-this-request
            # byte count must not make us wait for a fictitious 255-byte reply.
            return b"noise\x01\x03\xFA" + unrelated + corrupt + correct
        simulator.simulate_request = mixed
        self.manager.submit("a", build_modbus_read(1, 1, 1), {"protocol": "modbus"})
        self.until(lambda: bool(self.responses))
        self.assertEqual(self.responses[0][1]["parsed"]["samples"][0].temperature, -19.0)
        simulator.simulate_request = original
        self.manager.submit("a", build_modbus_read(1, 0x1001, 1), {"protocol": "modbus"})
        self.until(lambda: bool(self.failures))
        self.assertIn("寄存器地址", self.failures[0][1])

    def test_no_auto_retry_of_writes_and_bounded_read_retries(self):
        channel = self.manager._channels["a"]
        simulator = channel.simulator
        attempts = []
        def silence(request, expected):
            attempts.append(request)
            return b""
        simulator.simulate_request = silence
        self.manager.submit("a", build_maintenance(1, 5),
                            {"protocol": "maintenance", "op": 5, "timeout_ms": 30, "retries": 2})
        self.until(lambda: len(self.failures) == 1)
        self.assertEqual(len(attempts), 1)
        self.manager.disconnect_port("a")
        self.manager.connect_port("a")
        attempts.clear()
        self.manager.submit("a", build_modbus_read(1, 1, 1),
                            {"protocol": "modbus", "timeout_ms": 30, "retries": 1})
        self.until(lambda: len(self.failures) == 2)
        self.assertEqual(len(attempts), 2)

    def test_cancel_only_queued_and_mismatched_expected_not_sent(self):
        self.manager.submit("a", build_modbus_read(1, 1, 1), {"protocol": "modbus"}, "active")
        self.manager.submit("a", build_modbus_read(1, 2, 1), {"protocol": "modbus"}, "cancelled")
        self.manager.cancel_pending("a")
        self.assertEqual(self.request_failures[-1][1], "cancelled")
        self.until(lambda: len(self.responses) == 1)
        self.until(lambda: not self.manager.busy("a"))
        self.assertEqual(self.responses[0][1]["context"], "active")
        self.manager.submit("a", build_modbus_read(1, 1, 1), {"protocol": "modbus", "address": 2})
        self.assertTrue(self.failures)
        self.assertEqual(self.manager.pending_count("a"), 0)

    def test_continuous_noise_cannot_keep_a_retry_pending_forever(self):
        channel = self.manager._channels["a"]
        channel.simulator.simulate_request = lambda request, expected: b""
        self.manager.submit("a", build_modbus_read(1, 1, 1),
                            {"protocol": "modbus", "timeout_ms": 30, "retries": 1}, "noisy")
        noise = QTimer()
        noise.setInterval(10)
        noise.timeout.connect(lambda: self.manager._receive(channel, b"noise") if channel.draining else None)
        noise.start()
        self.until(lambda: not self.manager.is_connected("a"), timeout=2500)
        noise.stop()
        self.assertEqual(self.request_failures[-1][1], "noisy")
        self.assertIn("静默", self.failures[-1][1])


if __name__ == "__main__":
    unittest.main()
