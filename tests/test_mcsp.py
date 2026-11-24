"""Tests for MCSP framing, messages and the read-only safety check."""
import pytest

import mcsp


# ── Frames ───────────────────────────────────────────────────────────────────

def test_frame_header_layout():
    # Channel 1, end, length 4 -> 0x30 0x04: "0x30" is not a magic byte.
    assert mcsp.frame(1, b"\x80\x88\x08\x57") == [b"\x30\x04\x80\x88\x08\x57"]
    assert mcsp.frame(0, b"\x01\x03") == [b"\x10\x02\x01\x03"]


def test_long_payload_is_split_with_end_only_on_the_last_frame():
    frames = mcsp.frame(1, bytes(range(250)), packet_size=100)
    assert [f[0] for f in frames] == [0x20, 0x20, 0x30]
    assert sum(len(f) - 2 for f in frames) == 250


def test_twelve_bit_lengths():
    frames = mcsp.frame(1, bytes(300), packet_size=400)
    assert frames[0][:2] == bytes([0x31, 0x2C])        # 0x12C = 300
    assert mcsp.Deframer().feed(frames[0]) == [(1, bytes(300))]


def test_deframer_handles_several_frames_in_one_notification():
    data = mcsp.frame(1, b"\x80\x88\x08\x50")[0] + mcsp.frame(0, b"\x01\x03")[0]
    assert mcsp.Deframer().feed(data) == [(1, b"\x80\x88\x08\x50"), (0, b"\x01\x03")]


def test_deframer_reassembles_across_notifications_and_frames():
    payload = bytes(range(200))
    frames = mcsp.frame(1, payload, packet_size=64)
    d = mcsp.Deframer()
    out = []
    joined = b"".join(frames)
    for i in range(0, len(joined), 17):                # arbitrary notification split
        out += d.feed(joined[i:i + 17])
    assert out == [(1, payload)]


def test_interleaved_channels_reassemble_separately():
    a = mcsp.frame(1, b"A" * 12, packet_size=7)       # 3 frames of <= 5 bytes
    b = mcsp.frame(2, b"B" * 8, packet_size=7)        # 2 frames
    assert (len(a), len(b)) == (3, 2)
    d = mcsp.Deframer()
    out = d.feed(a[0] + b[0] + a[1]) + d.feed(b[1] + a[2])
    assert sorted(out) == [(1, b"A" * 12), (2, b"B" * 8)]


def test_empty_end_frame_completes_a_message():
    d = mcsp.Deframer()
    assert d.feed(b"\x20\x02ab") == []
    assert d.feed(b"\x30\x00") == [(1, b"ab")]


def test_reset_drops_partial_state():
    d = mcsp.Deframer()
    d.feed(b"\x20\x02ab" + b"\x30\x05xy")             # partial message + partial frame
    d.reset()
    assert d.feed(b"\x30\x01z") == [(1, b"z")]


def test_oversized_message_is_dropped():
    d = mcsp.Deframer()
    chunk = b"\x2f\xff" + bytes(0xFFF)                 # max-length, not ended
    for _ in range(5):
        d.feed(chunk)
    assert d.feed(b"\x30\x01z") == [(1, b"z")]         # earlier data was dropped
    assert d.dropped_messages == 1 and d.dropped_bytes > mcsp.MAX_MESSAGE


def test_partial_bytes_are_reported():
    d = mcsp.Deframer()
    d.feed(b"\x20\x02ab" + b"\x30\x05xy")
    assert d.partial_bytes() == 2 + 4                  # "ab" + incomplete frame


# ── Handshake ────────────────────────────────────────────────────────────────

def test_handshake_commands():
    frames = mcsp.handshake(244)
    assert frames[0] == b"\x10\x02\x01\x03"              # VERSION 3
    assert frames[1] == b"\x10\x03\x04\x00\xf4"          # MAX_SEGMENTATION 244, big-endian
    assert frames[2:] == [bytes([0x10, 2, 3, ch]) for ch in range(1, 8)]


def test_parse_command():
    assert mcsp.parse_commands(b"\x02\x01\x00\x00\x10\x00") == (2, b"\x01\x00\x00\x10\x00")


# ── Messages ─────────────────────────────────────────────────────────────────

def test_notification_source_has_flag_removed():
    msg = mcsp.parse_message(b"\x80\x88\x08\x57")
    assert (msg.kind, msg.src, msg.body) == ("notify", 0x0088, b"\x08\x57")


def test_request_round_trip():
    raw = mcsp.request(mcsp.READ, 0x1806, seq=5)
    assert raw == b"\x40\x80\x98\x06\x05"
    msg = mcsp.parse_message(raw)
    assert (msg.kind, msg.src, msg.dst, msg.op, msg.seq) == ("request", 0x4080, 0x1806, 0, 5)


def test_successful_response():
    msg = mcsp.parse_message(b"\x18\x06\xc0\x80\x15" + b"\x0a\x02ab")
    assert (msg.kind, msg.src, msg.dst, msg.op, msg.seq, msg.status, msg.body) == (
        "response", 0x1806, 0x4080, mcsp.READ_RESPONSE, 5, 0, b"\x0a\x02ab")


def test_failed_response_carries_status():
    msg = mcsp.parse_message(b"\x18\x06\x40\x80\x15\x06")
    assert (msg.status, mcsp.STATUS_NAMES[msg.status], msg.body) == (6, "DENIED", b"")


def test_bike_request_to_the_app_is_declined_as_unsupported():
    req = mcsp.parse_message(b"\x21\x00\xc0\xaa\x03")      # bike READs 0x40AA, seq 3
    reply = mcsp.unsupported_reply(req)
    assert reply == b"\x41\xaa\x21\x00\x13\x04"
    parsed = mcsp.parse_message(reply)
    assert (parsed.kind, parsed.op, parsed.seq, parsed.status) == (
        "response", mcsp.READ_RESPONSE, 3, mcsp.UNSUPPORTED)


@pytest.mark.parametrize("data", [b"\x40", b"\x40\x80\x98"])
def test_short_messages_are_rejected(data):
    with pytest.raises(ValueError):
        mcsp.parse_message(data)


def test_sequence_wraps_within_four_bits_and_never_uses_zero():
    seq = mcsp.Sequence()
    values = [seq.next() for _ in range(31)]
    assert values[:15] == list(range(1, 16)) and values[15] == 1
    assert all(1 <= v <= 15 for v in values)


# ── Safety ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("op", [mcsp.READ, mcsp.SUBSCRIBE, mcsp.UNSUBSCRIBE])
def test_reads_and_subscribes_are_allowed(op):
    mcsp.check_outgoing(1, mcsp.request(op, 0x185D, 1))


@pytest.mark.parametrize("op", [mcsp.WRITE, mcsp.RPC])
def test_writes_and_rpcs_are_refused(op):
    with pytest.raises(mcsp.UnsafeFrame):
        mcsp.check_outgoing(1, mcsp.request(op, 0x2112, 1))


def test_a_read_with_a_body_is_refused():
    with pytest.raises(mcsp.UnsafeFrame):
        mcsp.check_outgoing(1, mcsp.request(mcsp.READ, 0x2112, 1, body=b"\x08\x01"))


def test_only_unsupported_replies_are_allowed():
    req = mcsp.parse_message(b"\x21\x00\xc0\xaa\x03")
    mcsp.check_outgoing(1, mcsp.unsupported_reply(req))
    success_reply = b"\x41\xaa\xa1\x00\x13\x18\x01"     # a real answer with data
    with pytest.raises(mcsp.UnsafeFrame):
        mcsp.check_outgoing(1, success_reply)


def test_only_setup_transport_commands_are_allowed():
    for f in mcsp.handshake(244):
        mcsp.check_outgoing(0, f[2:])
    with pytest.raises(mcsp.UnsafeFrame):
        mcsp.check_outgoing(0, b"\x02\x01\x00\x00\x10\x00")   # transmit window
    with pytest.raises(mcsp.UnsafeFrame):
        mcsp.check_outgoing(2, b"\x00")                       # resource channel


# ── The one allowed call: a wireless remote's battery ───────────────────────

@pytest.mark.parametrize("remote", [0, 1, 7, 255])
def test_the_remote_battery_getter_is_allowed_with_a_remote_id(remote):
    mcsp.check_outgoing(1, mcsp.request(mcsp.RPC, mcsp.GET_REMOTE_BATTERY, 1,
                                        body=mcsp.remote_id_body(remote)))


@pytest.mark.parametrize("body", [b"\x08\x01\x10\x01", b"\x10\x01", b"\x0a\x00",
                                  b"\x08\x80\x02", b"\x08"])
def test_the_remote_battery_getter_is_refused_with_anything_else(body):
    with pytest.raises(mcsp.UnsafeFrame):
        mcsp.check_outgoing(1, mcsp.request(mcsp.RPC, mcsp.GET_REMOTE_BATTERY, 1, body=body))


@pytest.mark.parametrize("dp", [0x2289, 0x2288, 0x220A, 0x220B, 0x2211, 0x221E, 0x2043])
def test_every_other_call_is_still_refused(dp):
    """Play sound, buzzer, connect/disconnect/clear remotes, device info,
    simulated button presses: refused even with a harmless-looking argument."""
    with pytest.raises(mcsp.UnsafeFrame):
        mcsp.check_outgoing(1, mcsp.request(mcsp.RPC, dp, 1, body=mcsp.remote_id_body(1)))
    with pytest.raises(mcsp.UnsafeFrame):
        mcsp.check_outgoing(1, mcsp.request(mcsp.RPC, dp, 1))


def test_writes_to_the_remote_battery_datapoint_are_refused():
    with pytest.raises(mcsp.UnsafeFrame):
        mcsp.check_outgoing(1, mcsp.request(mcsp.WRITE, mcsp.GET_REMOTE_BATTERY, 1,
                                            body=mcsp.remote_id_body(1)))


def test_the_real_bikes_remote_list_and_battery_replies():
    assert mcsp.remote_ids(bytes.fromhex("0a000a020801")) == [0, 1]     # captured 24 Nov
    assert mcsp.remote_battery_level(bytes.fromhex("0a020855")) == 85   # nullable wrapper
    assert mcsp.remote_battery_level(bytes.fromhex("0855")) == 85       # bare service
    # Empty, zero or nonsense is "no value", never a false "battery low".
    assert mcsp.remote_battery_level(bytes.fromhex("0a00")) is None
    assert mcsp.remote_battery_level(b"") is None
    assert mcsp.remote_battery_level(bytes.fromhex("0a0308c801")) is None   # 200 %
