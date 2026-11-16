"""Tiny protobuf encoder used to build test packets for the LDI decoder."""


def varint(value: int) -> bytes:
    """Encode a non-negative int as a base-128 varint."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field(number: int, value: int) -> bytes:
    """A varint field (wire type 0). Negative ints use 64-bit two's complement."""
    if value < 0:
        value += 1 << 64
    return varint(number << 3) + varint(value)


def raw_field(number: int, wire_type: int, payload: bytes) -> bytes:
    """A non-varint field with an already-encoded payload."""
    return varint((number << 3) | wire_type) + payload
