"""
Schema-less protobuf wire-format decoding.

Bosch messagebus payloads are small protobuf messages. Decoding them without
generated classes keeps every field visible, including ones we don't know.
"""
from __future__ import annotations

from typing import NamedTuple

VARINT, FIXED64, LENGTH, FIXED32 = 0, 1, 2, 5


class Field(NamedTuple):
    number: int
    wire_type: int
    value: int | bytes


class DecodeError(ValueError):
    """Malformed or truncated protobuf data."""


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a base-128 varint at pos. Returns (value, next position)."""
    result = shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift >= 64:
            raise DecodeError("varint longer than 64 bits")
    raise DecodeError("truncated varint")


def zigzag(value: int) -> int:
    """Decode a protobuf sint32/sint64 (ZigZag) value."""
    return (value >> 1) ^ -(value & 1)


def decode(data: bytes) -> list[Field]:
    """Split a protobuf message into its fields, in wire order."""
    fields, pos = [], 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number, wire_type = tag >> 3, tag & 0x07
        if number == 0:
            raise DecodeError("field number 0")
        if wire_type == VARINT:
            value, pos = read_varint(data, pos)
        elif wire_type == LENGTH:
            length, pos = read_varint(data, pos)
            if pos + length > len(data):
                raise DecodeError("length-delimited field runs past the end")
            value, pos = data[pos:pos + length], pos + length
        elif wire_type in (FIXED64, FIXED32):
            size = 8 if wire_type == FIXED64 else 4
            if pos + size > len(data):
                raise DecodeError("fixed-width field runs past the end")
            value, pos = int.from_bytes(data[pos:pos + size], "little"), pos + size
        else:
            raise DecodeError(f"unsupported wire type {wire_type}")
        fields.append(Field(number, wire_type, value))
    return fields


def to_plain(data: bytes, depth: int = 0) -> dict:
    """Best-effort readable form of an unknown message: {field: value(s)}.

    Length-delimited values become text if they are printable UTF-8, a nested
    message if they parse as one, otherwise hex.
    """
    out: dict = {}
    for f in decode(data):
        value = _plain_value(f, depth) if f.wire_type == LENGTH else f.value
        if f.number in out:
            existing = out[f.number]
            out[f.number] = (existing if isinstance(existing, list) else [existing]) + [value]
        else:
            out[f.number] = value
    return out


def _plain_value(f: Field, depth: int):
    raw = f.value
    try:
        text = raw.decode("utf-8")
        if text.isprintable():
            return text
    except UnicodeDecodeError:
        pass
    if depth < 4 and raw:
        try:
            return to_plain(raw, depth + 1)
        except DecodeError:
            pass
    return raw.hex()
