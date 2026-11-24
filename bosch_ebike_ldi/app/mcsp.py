"""
MCSP: the Bosch smart-system channel the Flow app uses over Bluetooth LE.

Two layers, both pure Python (no Bluetooth here):

Transport frames (on characteristics 0011 receive / 0012 send)
    byte0 = channel << 5 | end << 4 | length >> 8,  byte1 = length & 0xFF
    Wire channel 0 carries transport commands; wire channel 1 the messagebus.
    A message can span several frames; one notification can hold several.

Messagebus messages (inside wire channel 1)
    notification  [src | 0x8000 : 2] body
    directed      [src : 2][dst (| 0x8000 = success) : 2][op << 4 | seq]
                  [status, only on a failed response] body
    Addresses are big-endian; bit 15 is a flag, the logical id is & 0x7FFF.

SAFETY: this add-on only ever reads. `check_outgoing` rejects anything that is
not a read, subscribe, unsubscribe, an "unsupported" reply, or one of three
transport setup commands. It is called on every frame before it is sent.
"""
from __future__ import annotations

from typing import NamedTuple

SERVICE_UUID = "00000010-eaa2-11e9-81b4-2a2ae2dbcce4"
RX_UUID = "00000011-eaa2-11e9-81b4-2a2ae2dbcce4"     # bike -> us, notify
TX_UUID = "00000012-eaa2-11e9-81b4-2a2ae2dbcce4"     # us -> bike, write w/o response

COMMAND_CHANNEL = 0
MESSAGEBUS_CHANNEL = 1
MAX_MESSAGE = 16 * 1024          # reassembly limit per channel
DEFAULT_PACKET = 244             # until the link's MTU says otherwise

# Transport commands (wire channel 0): [command id][body]
CMD_VERSION = 1
CMD_ADVANCED_TRANSMIT_WINDOW = 2
CMD_DISABLE_FLOW_CONTROL = 3
CMD_MAX_SEGMENTATION_PACKET = 4
OUR_VERSION = 3

# Messagebus operations
READ, READ_RESPONSE, WRITE, WRITE_RESPONSE = 0, 1, 2, 3
RPC, RPC_RESPONSE, SUBSCRIBE, SUBSCRIBE_RESPONSE = 4, 5, 6, 7
UNSUBSCRIBE, UNSUBSCRIBE_RESPONSE = 8, 9
OP_NAMES = {READ: "READ", READ_RESPONSE: "READ_RESPONSE", WRITE: "WRITE",
            WRITE_RESPONSE: "WRITE_RESPONSE", RPC: "RPC", RPC_RESPONSE: "RPC_RESPONSE",
            SUBSCRIBE: "SUBSCRIBE", SUBSCRIBE_RESPONSE: "SUBSCRIBE_RESPONSE",
            UNSUBSCRIBE: "UNSUBSCRIBE", UNSUBSCRIBE_RESPONSE: "UNSUBSCRIBE_RESPONSE"}

STATUS_NAMES = {0: "SUCCESS", 1: "OVERLOADED", 2: "NO_ROUTE_FOUND", 3: "NOT_READY",
                4: "UNSUPPORTED", 6: "DENIED", 7: "INVALID_VALUE", 8: "MALFORMED",
                9: "TIMEOUT", 10: "TOO_LARGE", 255: "UNKNOWN_ERROR"}
UNSUPPORTED = 4

APP_GATEWAY = 0x4080             # the address our requests come from
FLAG = 0x8000


class UnsafeFrame(RuntimeError):
    """Raised when something other than a read-only frame would be sent."""


# ── Transport frames ─────────────────────────────────────────────────────────

def frame(channel: int, payload: bytes, packet_size: int = DEFAULT_PACKET) -> list[bytes]:
    """Split a channel payload into wire frames that each fit packet_size."""
    chunk = max(1, min(packet_size - 2, 0x0FFF))
    pieces = [payload[i:i + chunk] for i in range(0, len(payload), chunk)] or [b""]
    out = []
    for i, piece in enumerate(pieces):
        end = i == len(pieces) - 1
        header = (channel << 5) | (0x10 if end else 0) | (len(piece) >> 8)
        out.append(bytes([header, len(piece) & 0xFF]) + piece)
    return out


class Deframer:
    """Reassembles channel payloads from notifications. One per connection."""

    def __init__(self) -> None:
        self._pending = b""                  # bytes of an incomplete frame
        self._channels: dict[int, bytes] = {}
        self.dropped_messages = 0            # oversized messages thrown away
        self.dropped_bytes = 0

    def partial_bytes(self) -> int:
        """Bytes held for incomplete frames/messages (lost if reset now)."""
        return len(self._pending) + sum(len(v) for v in self._channels.values())

    def reset(self) -> None:
        self._pending, self._channels = b"", {}

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        """Add received bytes; return completed (channel, payload) pairs."""
        buf, done = self._pending + data, []
        while len(buf) >= 2:
            channel = buf[0] >> 5
            end = bool(buf[0] & 0x10)
            length = ((buf[0] & 0x0F) << 8) | buf[1]
            if len(buf) < 2 + length:
                break                        # rest arrives in a later notification
            piece, buf = buf[2:2 + length], buf[2 + length:]
            message = self._channels.pop(channel, b"") + piece
            if len(message) > MAX_MESSAGE:
                self.dropped_messages += 1     # drop oversized message, but count it
                self.dropped_bytes += len(message)
                continue
            if end:
                done.append((channel, message))
            else:
                self._channels[channel] = message
        if len(buf) > MAX_MESSAGE:
            self.dropped_bytes += len(buf)
            buf = b""
        self._pending = buf
        return done


# ── Transport commands (channel 0) ───────────────────────────────────────────

def parse_commands(payload: bytes) -> tuple[int, bytes]:
    """A channel-0 payload is one command: (command id, body)."""
    return (payload[0], payload[1:]) if payload else (0, b"")


def handshake(packet_size: int) -> list[bytes]:
    """Our transport setup: version, max packet size, flow control off."""
    size = max(20, min(packet_size, 0xFFFF))
    commands = [bytes([CMD_VERSION, OUR_VERSION]),
                bytes([CMD_MAX_SEGMENTATION_PACKET, size >> 8, size & 0xFF])]
    commands += [bytes([CMD_DISABLE_FLOW_CONTROL, ch]) for ch in range(1, 8)]
    return [f for c in commands for f in frame(COMMAND_CHANNEL, c)]


# ── Messagebus ───────────────────────────────────────────────────────────────

class Message(NamedTuple):
    kind: str            # "notify", "request" or "response"
    src: int             # logical address (flag removed)
    dst: int | None
    op: int | None
    seq: int | None
    status: int | None   # responses only; 0 = success
    body: bytes


def parse_message(payload: bytes) -> Message:
    if len(payload) < 2:
        raise ValueError("messagebus payload shorter than 2 bytes")
    src_raw = int.from_bytes(payload[:2], "big")
    if src_raw & FLAG:
        return Message("notify", src_raw & 0x7FFF, None, None, None, None, payload[2:])
    if len(payload) < 5:
        raise ValueError("directed message shorter than 5 bytes")
    dst_raw = int.from_bytes(payload[2:4], "big")
    op, seq = payload[4] >> 4, payload[4] & 0x0F
    if op % 2 == 1:                              # odd operations are responses
        if dst_raw & FLAG:
            return Message("response", src_raw, dst_raw & 0x7FFF, op, seq, 0, payload[5:])
        status = payload[5] if len(payload) > 5 else 255
        return Message("response", src_raw, dst_raw, op, seq, status, payload[6:])
    return Message("request", src_raw, dst_raw & 0x7FFF, op, seq, None, payload[5:])


def request(op: int, dst: int, seq: int, body: bytes = b"", src: int = APP_GATEWAY) -> bytes:
    """A request from us to a bike datapoint."""
    return (src.to_bytes(2, "big") + ((dst & 0x7FFF) | FLAG).to_bytes(2, "big")
            + bytes([(op << 4) | (seq & 0x0F)]) + body)


def unsupported_reply(req: Message) -> bytes:
    """Decline a request the bike sent us (we implement no phone features).

    The reply comes from the addressed object in the 0x41xx range, goes back
    to the requester without the success flag, and carries the status byte.
    """
    src = 0x4100 | (req.dst & 0xFF)
    return (src.to_bytes(2, "big") + (req.src & 0x7FFF).to_bytes(2, "big")
            + bytes([((req.op + 1) << 4) | req.seq, UNSUPPORTED]))


class Sequence:
    """4-bit request sequence numbers, wrapping 1..15 (0 is never used)."""

    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        self._last = self._last % 15 + 1
        return self._last


# ── Safety ───────────────────────────────────────────────────────────────────

SAFE_COMMANDS = {CMD_VERSION, CMD_DISABLE_FLOW_CONTROL, CMD_MAX_SEGMENTATION_PACKET}
SAFE_REQUESTS = {READ, SUBSCRIBE, UNSUBSCRIBE}

# The one call (RPC) allowed: a getter that changes nothing. The System
# Controller is a Bluetooth central for wireless remotes (Mini Remote); the
# Flow app asks it for a remote's battery level with this call, passing only
# the remote's id (a BleCentralPeripheralIdentifier). Any other call, or any
# other argument, is refused.
GET_REMOTE_BATTERY = 0x2220                    # RemoteControl.bleCentralGetBatteryService
SAFE_CALLS = {GET_REMOTE_BATTERY}
REMOTE_DATABASE = 0x2210                       # RemoteControl.bleCentralDatabase
REMOTE_BATTERY_CHANGED = 0x2221                # RemoteControl.bleCentralBatteryServiceChanged


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def remote_id_body(remote_id: int) -> bytes:
    """BleCentralPeripheralIdentifier {value: id}; id 0 is the empty message."""
    return b"" if remote_id == 0 else b"\x08" + _varint(remote_id)


def is_remote_id_body(body: bytes) -> bool:
    """True only for exactly what remote_id_body() produces (ids up to 255)."""
    return any(body == remote_id_body(i) for i in range(256))


def _fields(body: bytes) -> list[tuple[int, int, object]]:
    """Top-level protobuf fields: (number, wire type, int or bytes)."""
    out, pos = [], 0
    while pos < len(body):
        key, pos = _read_varint(body, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(body, pos)
        elif wire == 2:
            size, pos = _read_varint(body, pos)
            value, pos = body[pos:pos + size], pos + size
            if pos > len(body):
                raise ValueError("truncated field")
        else:
            raise ValueError(f"wire type {wire}")
        out.append((number, wire, value))
    return out


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data) or shift > 63:
            raise ValueError("bad varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def remote_ids(body: bytes) -> list[int]:
    """BleCentralDatabase: repeated BleCentralPeripheralIdentifier -> ids."""
    ids = []
    for number, wire, value in _fields(body):
        if number == 1 and wire == 2:
            inner = dict((n, v) for n, w, v in _fields(value) if w == 0)
            ids.append(int(inner.get(1, 0)))
    return ids


def remote_battery_level(body: bytes) -> int | None:
    """BleBatteryServiceNullable {value: BleBatteryService {battery_level}},
    or a bare BleBatteryService {battery_level}. Only 1..100 counts: an empty
    reply (no value, an empty slot, a remote that didn't answer) is None, never
    0 %, so it can't raise a false "battery low"."""
    level = None
    for number, wire, value in _fields(body):
        if number == 1 and wire == 2:
            inner = dict((n, v) for n, w, v in _fields(value) if w == 0)
            level = inner.get(1)
        elif number == 1 and wire == 0:
            level = value
    return int(level) if isinstance(level, int) and 1 <= level <= 100 else None


def check_outgoing(channel: int, payload: bytes) -> None:
    """Raise UnsafeFrame unless the payload is read-only by construction."""
    if channel == COMMAND_CHANNEL:
        if not payload or payload[0] not in SAFE_COMMANDS:
            raise UnsafeFrame(f"transport command {payload[:1].hex()} not allowed")
        return
    if channel != MESSAGEBUS_CHANNEL:
        raise UnsafeFrame(f"channel {channel} not allowed")
    msg = parse_message(payload)
    if msg.kind == "request" and msg.op in SAFE_REQUESTS and not msg.body:
        return
    if msg.kind == "request" and msg.op == RPC and msg.dst in SAFE_CALLS \
            and is_remote_id_body(msg.body):
        return
    if msg.kind == "response" and msg.status == UNSUPPORTED and not msg.body:
        return
    raise UnsafeFrame(f"{msg.kind} {OP_NAMES.get(msg.op, msg.op)} not allowed")
