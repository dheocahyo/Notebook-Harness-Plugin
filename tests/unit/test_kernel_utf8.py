"""a7's receive-path fix (design §6.13): the kernel websocket's text frames are checked as UTF-8
by the C decoder, not websocket-client's pure-Python loop (~0.2 s per MB without ``wsaccel``).

nh's check must answer as websocket-client's own does, input for input (a sequence cut at the end
passes it, and ``WebSocketApp``'s decode of a text frame refuses it later, as before), and
websocket-client's frame reader must still call it. Both rest on websocket-client's private
modules, so the gateway installs it only when they are as expected and says so otherwise; the
drift job runs these tests against websocket-client's latest release.
"""

from __future__ import annotations

import itertools
import logging
import struct
import sys

import pytest
import websocket._abnf
import websocket._utils
from websocket._abnf import ABNF, continuous_frame
from websocket._exceptions import WebSocketPayloadException, WebSocketProtocolException

from nh_gateway.backend import kernel

VALID = [
    b"",
    b"plain ascii",
    "café 😀 ✓ ∑".encode(),
    b'{"msg_type": "stream", "text": "' + b"x" * 100_000 + b'"}',
    b"\xf4\x8f\xbf\xbf",  # U+10FFFF, the last code point
]
INVALID = [
    b"\xff",  # never valid
    b"\xc0\xaf",  # overlong "/"
    b"\xe0\x80\xaf",  # overlong, three bytes
    b"\xed\xa0\x80",  # a UTF-16 surrogate
    b"ok \x80 then",  # a stray continuation byte
    b"\xf4\x90\x80\x80",  # past U+10FFFF
    b"\xed\xa0",  # a surrogate, cut: no third byte can make it valid
    b"\xe0\x80",  # an overlong form, cut
    b"\xf4\x90",  # past U+10FFFF, cut
]
CUT = [b"\xe2\x82", b"ok \xf0\x9f\x98", b"\xc3"]  # valid so far, cut at the end


def text_frame(data: bytes) -> tuple[int, ABNF]:
    """One whole text frame through websocket-client's own reader, as a kernel message arrives."""
    frames = continuous_frame(fire_cont_frame=False, skip_utf8_validation=False)
    frame = ABNF(fin=1, opcode=ABNF.OPCODE_TEXT, data=data)
    frames.validate(frame)
    frames.add(frame)
    return frames.extract(frame)


def close_frame(reason: bytes) -> None:
    """A close frame's checks, as websocket-client's reader runs them."""
    ABNF(fin=1, opcode=ABNF.OPCODE_CLOSE, data=struct.pack("!H", 1000) + reason).validate(False)


def test_nh_s_check_is_installed_where_the_frame_reader_calls_it(monkeypatch):
    assert kernel.UTF8_CHECK_NOTE is None
    assert websocket._abnf.validate_utf8 is kernel.validate_utf8
    calls: list[bytes] = []

    def recorded(data):
        calls.append(bytes(data))
        return True

    monkeypatch.setattr(websocket._abnf, "validate_utf8", recorded)
    text_frame(b"a kernel message")
    close_frame(b"bye")
    assert calls == [b"a kernel message", b"bye"]


@pytest.mark.parametrize("data", VALID, ids=lambda data: repr(data[:24]))
def test_valid_text_frames_pass(data: bytes):
    assert kernel.validate_utf8(data) and websocket._utils.validate_utf8(data)
    opcode, frame = text_frame(data)
    assert opcode == ABNF.OPCODE_TEXT and frame.data == data


@pytest.mark.parametrize("data", INVALID)
def test_invalid_text_frames_are_refused_as_before(data: bytes):
    assert not websocket._utils.validate_utf8(data)
    assert not kernel.validate_utf8(data)
    with pytest.raises(WebSocketPayloadException):
        text_frame(data)


@pytest.mark.parametrize("data", CUT)
def test_a_cut_sequence_passes_the_check_and_fails_the_decode_as_before(data: bytes):
    assert kernel.validate_utf8(data) and websocket._utils.validate_utf8(data)
    _opcode, frame = text_frame(data)
    with pytest.raises(UnicodeDecodeError):  # WebSocketApp decodes a text frame's data
        frame.data.decode("utf-8")
    close_frame(data)  # a close reason is decoded with errors="replace": no error, as before


def test_close_frames_with_an_invalid_reason_are_refused_as_before():
    with pytest.raises(WebSocketProtocolException):
        close_frame(b"bye \xff")


def test_the_answers_are_websocket_clients_own():
    """Every 1- and 2-byte input, and 3- and 4-byte ones whose lead narrows the second byte."""
    upstream = websocket._utils.validate_utf8
    inputs = [bytes(t) for size in (1, 2) for t in itertools.product(range(256), repeat=size)]
    for lead in (0xE0, 0xED, 0xF0, 0xF4, 0xC2, 0xEF):
        for second, third in itertools.product(range(256), (0x41, 0x80, 0xBF, 0xC0)):
            inputs += [bytes((lead, second, third)), bytes((lead, second, 0x80, third))]
    differ = [data for data in inputs if kernel.validate_utf8(data) != upstream(data)]
    assert not differ, differ[:10]


def test_other_buffers_and_str_are_checked_like_websocket_client_does():
    for data in (b"\xe2\x82\xac", b"\xe2\x82", b"\xed\xa0", b"\xff"):
        expected = websocket._utils.validate_utf8(data)
        assert kernel.validate_utf8(bytearray(data)) is expected
        assert kernel.validate_utf8(memoryview(data)) is expected
    assert kernel.validate_utf8("é")
    assert not kernel.validate_utf8("\ud800")  # a lone surrogate
    assert not websocket._utils.validate_utf8("\ud800")


def test_a_call_nh_does_not_know_goes_to_websocket_clients_check(monkeypatch, caplog):
    monkeypatch.setattr(kernel, "_unknown_call_logged", False)
    caplog.set_level(logging.WARNING, logger="nh_gateway.kernel")
    assert kernel.validate_utf8([0x41]) is True  # a list of byte values: its loop takes it
    assert kernel.validate_utf8([0xFF]) is False
    assert kernel.validate_utf8(memoryview(b"a-b-")[::2]) is True  # not contiguous: "ab"
    assert kernel.validate_utf8(memoryview(b"\xff-")[::2]) is False
    with pytest.raises(TypeError):
        kernel.validate_utf8(b"x", "an argument websocket-client's check doesn't take")
    assert [r.getMessage() for r in caplog.records] == [
        "websocket-client called nh's UTF-8 check in a new way; its own is used"
    ]


def test_a_changed_websocket_client_keeps_its_own_check_and_the_gateway_says_so(
    monkeypatch, caplog
):
    assert kernel.install_utf8_check() is None  # installed already: nothing to do

    def renamed(data):
        return True

    monkeypatch.setattr(websocket._abnf, "validate_utf8", renamed)
    assert kernel.install_utf8_check() == (
        "websocket-client's frame reader no longer calls websocket._utils.validate_utf8"
    )
    assert websocket._abnf.validate_utf8 is renamed  # left alone
    monkeypatch.setitem(sys.modules, "websocket._abnf", None)  # the module is gone
    reason = kernel.install_utf8_check()
    assert reason is not None and reason.startswith("websocket-client's modules changed")

    monkeypatch.setattr(kernel, "UTF8_CHECK_NOTE", reason)
    monkeypatch.setattr(kernel, "_utf8_noted", False)
    caplog.set_level(logging.WARNING, logger="nh_gateway.kernel")
    kernel.note_utf8_check()
    kernel.note_utf8_check()
    assert [r.getMessage() for r in caplog.records] == [
        f"nh's fast UTF-8 check for kernel messages is not installed ({reason}): kernel output "
        "of many MB is received more slowly"
    ]
