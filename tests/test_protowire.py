"""Tests for the schema-less protobuf decoder."""
import pytest

import protowire as pw
from protobuf_helpers import field, raw_field, varint


def test_zigzag():
    assert [pw.zigzag(v) for v in (0, 1, 2, 3, 99)] == [0, -1, 1, -2, -50]


def test_decode_all_wire_types():
    data = (field(1, 300) + raw_field(2, 2, varint(3) + b"abc")
            + raw_field(3, 5, b"\x01\x00\x00\x00") + raw_field(4, 1, b"\x02" + b"\x00" * 7))
    assert pw.decode(data) == [pw.Field(1, 0, 300), pw.Field(2, 2, b"abc"),
                               pw.Field(3, 5, 1), pw.Field(4, 1, 2)]


@pytest.mark.parametrize("data", [
    b"\x08",                     # tag without value
    b"\x08\xff",                 # unfinished varint
    b"\x12\x05ab",               # length past the end
    b"\x0d\x01\x00",             # fixed32 too short
    b"\x0b",                     # wire type 3 (groups) unsupported
    b"\x00\x01",                 # field number 0
    b"\x08" + b"\xff" * 10 + b"\x01",   # varint too long
])
def test_malformed_input_is_rejected(data):
    with pytest.raises(pw.DecodeError):
        pw.decode(data)


def test_to_plain_handles_text_nesting_repeats_and_bytes():
    nested = field(1, 7)
    data = (raw_field(1, 2, varint(5) + b"ECO  ") + raw_field(2, 2, varint(len(nested)) + nested)
            + field(3, 1) + field(3, 2) + raw_field(4, 2, b"\x02\xff\xfe"))
    assert pw.to_plain(data) == {1: "ECO  ", 2: {1: 7}, 3: [1, 2], 4: "fffe"}
