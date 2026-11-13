"""Exercise fragmented 1200-baud traffic, CRC validation and request matching."""

import os
import sys
import threading
import time

import pytest
from PySide6.QtTest import QTest

from warehouse_numbering.protocol import (
    CAPABILITY, CAPABILITY_BYTES, READ_SINGLE, FixtureError, ReplyBuffer, decode_reply, make_request,
)
from warehouse_numbering.transport import FixtureTransport
from warehouse_pc.domain import crc8
from warehouse_pc.protocol import with_crc


def response(op, data, result=0):
    return with_crc(bytes((1, 0x41, op, result)) + data)


def probe_payload(token):
    scratch = b"\x50\x05\x01\x14\x7f\xff\x60\x00"
    return token + bytes.fromhex("2CEE08640200EEC9") + scratch + bytes((crc8(scratch),))


def test_parser_ignores_wrong_token_and_handles_crc_and_chunks():
    token = b"ABCD"
    valid = response(READ_SINGLE, probe_payload(token))
    wrong = response(READ_SINGLE, probe_payload(b"WXYZ"))
    broken = valid[:-1] + bytes((valid[-1] ^ 1,))
    parser = ReplyBuffer(1, READ_SINGLE, token)
    assert parser.feed(b"\xaa" + wrong + broken + valid[:5]) is None
    assert parser.bad_crc
    assert parser.feed(valid[5:14]) is None
    result, data = parser.feed(valid[14:] + wrong)
    probe = decode_reply(READ_SINGLE, result, data, token)
    assert (probe.rom_hex, probe.root, probe.point, probe.configuration) == ("2CEE08640200EEC9", 1, 20, 127)

    invalid_scratch = data[:-1] + bytes((data[-1] ^ 1,))
    with pytest.raises(FixtureError, match="CRC"):
        decode_reply(READ_SINGLE, 0, invalid_scratch, token)
    with pytest.raises(FixtureError, match="工装能力"):
        decode_reply(CAPABILITY, 0, b"\x01\x00\x00\x01\x01\xfe")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
def test_qserialport_at_1200_accepts_fragmented_actual_frames():
    import pty
    import select

    master, slave = pty.openpty()
    transport = FixtureTransport()
    responses, errors = [], []
    token = bytes((9, 8, 7, 6))

    def emulate():
        try:
            for op, payload, reply in ((CAPABILITY, b"", response(CAPABILITY, CAPABILITY_BYTES)),
                                       (READ_SINGLE, token, response(READ_SINGLE, probe_payload(token)))):
                expected = make_request(1, op, payload)
                seen = bytearray()
                while len(seen) < len(expected):
                    assert select.select([master], [], [], 2)[0]
                    seen.extend(os.read(master, 64))
                assert seen == expected
                for octet in reply:
                    os.write(master, bytes((octet,)))
                    time.sleep(10 / 1200)
        except (OSError, AssertionError) as error:
            errors.append(error)

    def capability(value, error):
        responses.append((value, error))
        if not error:
            transport.request(1, READ_SINGLE, token, token, lambda *args: responses.append(args))

    thread = threading.Thread(target=emulate, daemon=True)
    try:
        transport.open(os.ttyname(slave), 1200, "N", 1)
        thread.start()
        transport.request(1, CAPABILITY, b"", None, capability)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(responses) < 2 and not errors:
            QTest.qWait(5)
        assert not errors
        assert len(responses) == 2
        assert responses[0] == (None, None)
        assert responses[1][1] is None and responses[1][0].point == 20
    finally:
        transport.close()
        if thread.ident is not None:
            thread.join(2.5)
        os.close(master)
        os.close(slave)
