"""Tests for datapoint decoding, using the handoff's worked examples."""
import json
from pathlib import Path

import pytest

import datapoints as dps
from datapoints import Datapoint, decode_value
from protobuf_helpers import field, raw_field, varint

TABLE = dps.load_table()
EXAMPLES = (Path(__file__).parent / "fixtures" / "offline-payload-examples.json")

# The kind each example's message decodes as.
EXAMPLE_KINDS = {
    "Int16NormFactor10Message": Datapoint(0, "t", "t", "sint", 10),
    "Uint16NormFactor10Message": Datapoint(0, "t", "t", "uint", 10),
    "Uint32NormFactor100Message": Datapoint(0, "t", "t", "uint", 100),
}


def examples():
    return json.loads(EXAMPLES.read_text(encoding="utf-8"))["vectors"]


@pytest.mark.parametrize("vector", examples(), ids=lambda v: v["label"])
def test_handoff_examples(vector):
    body = bytes.fromhex(vector["payload_hex"])
    expected = list(vector["expected_interpretation"].values())
    dp = EXAMPLE_KINDS.get(vector["message"])
    if dp is not None:
        assert decode_value(dp, body) == expected[0]
        return
    # Structured messages: check the raw field values in wire order.
    plain = decode_value(None, body)
    assert list(plain.values()) == list(vector["expected_raw"].values())


def test_table_has_the_key_datapoints():
    assert len(TABLE) == 967
    assert TABLE[0x185D].label == "DriveUnit.motorPower"
    assert TABLE[0x00E2].factor == 100 and TABLE[0x00E2].unit == "Ah"
    assert TABLE[0x1806].kind == "string"
    assert TABLE[0x182D].struct == "BikeSpeedWithValidity"


def test_every_curated_datapoint_exists_and_is_readable():
    curated = set(dps.CURATED_READS) | set(dps.CURATED_SUBSCRIBES) | {
        e.id for e in dps.ENTITIES}
    missing = [hex(i) for i in curated if i not in TABLE or not TABLE[i].readable]
    assert missing == []


def test_flow_app_datapoints_are_all_in_the_table():
    # IDs the app reads/subscribes must exist; some may be phone-side or void.
    observed = set(dps.FLOW_APP_READS) | set(dps.FLOW_APP_SUBSCRIBES)
    assert [hex(i) for i in observed if i not in TABLE] == []


def test_connect_lists_put_curated_first_without_duplicates():
    assert dps.READ_ON_CONNECT[:len(dps.CURATED_READS)] == dps.CURATED_READS
    assert len(dps.READ_ON_CONNECT) == len(set(dps.READ_ON_CONNECT))
    assert len(dps.SUBSCRIBE_ON_CONNECT) == len(set(dps.SUBSCRIBE_ON_CONNECT))


def test_entity_keys_are_unique():
    keys = [e.key for e in dps.ENTITIES]
    assert len(keys) == len(set(keys))


def test_real_table_entries_decode():
    assert decode_value(TABLE[0x1806], raw_field(1, 2, varint(7) + b"21.12.0")) == "21.12.0"
    assert decode_value(TABLE[0x0096], field(1, 1234)) == 123.4
    assert decode_value(TABLE[0x008B], field(1, 99)) == -5.0       # ZigZag
    assert decode_value(TABLE[0x0089], field(1, 1)) is True
    assert decode_value(TABLE[0x1857], field(1, 80) + field(1, 60) + field(1, 40)) == [80, 60, 40]


def test_empty_payload_is_absent_not_zero():
    assert decode_value(TABLE[0x185D], b"") is None


def test_proto3_default_zero_is_a_real_zero():
    # A message with no field 1 means the value is the default: here no value
    # was encoded at all, so we report None rather than guess.
    assert decode_value(TABLE[0x185D], field(2, 5)) is None


def test_assist_mode_shows_its_name():
    mode = next(e for e in dps.ENTITIES if e.id == 0x1809)
    assert dps.entity_state(mode, 2, ["OFF", "ECO", "TOUR+"]) == "TOUR+"
    assert dps.entity_state(mode, 7, ["OFF"]) == 7
