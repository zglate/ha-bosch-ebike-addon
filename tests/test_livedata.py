"""Tests for the LDI protobuf decoder and the charge-time estimator."""
import pytest

from livedata import ChargeEstimator, decode_live_data, estimated_energy_wh
from protobuf_helpers import field, raw_field, varint


def test_encoder_matches_hand_computed_bytes():
    # Guard the test helper itself: 300 = 0xAC 0x02, field 1 tag = 0x08.
    assert varint(300) == b"\xac\x02"
    assert field(1, 300) == b"\x08\xac\x02"


def test_full_snapshot_decodes_every_documented_field():
    packet = (
        field(1, 2534)        # 25.34 km/h
        + field(2, 78)        # rpm
        + field(5, 180)       # rider power W
        + field(9, 12500)     # 12.5 lx
        + field(10, 87)       # battery %
        + field(11, 1763300000)
        + field(12, 1234567)  # metres -> 1234.567 km
        + field(17, 2)        # light on
        + field(21, 1)
        + field(22, 1)
        + field(23, 0)
        + field(24, 0)
        + field(25, 1)        # bike_not_driving -> not in motion
    )
    assert decode_live_data(packet) == {
        "speed_kmh": 25.34,
        "cadence": 78,
        "rider_power": 180,
        "ambient_lux": 12.5,
        "battery_soc": 87,
        "timestamp": 1763300000,
        "odometer_km": 1234.567,
        "bike_light": True,
        "system_locked": True,
        "charger_connected": True,
        "light_reserve": False,
        "diagnosis_active": False,
        "in_motion": False,
    }


def test_sparse_notification_only_contains_sent_fields():
    assert decode_live_data(field(10, 55)) == {"battery_soc": 55}


def test_negative_cadence_is_signed():
    assert decode_live_data(field(2, -12)) == {"cadence": -12}


@pytest.mark.parametrize("raw, expected", [(0, None), (1, False), (2, True), (9, None)])
def test_light_on_off_and_invalid_is_unknown(raw, expected):
    assert decode_live_data(field(17, raw)) == {"bike_light": expected}


@pytest.mark.parametrize("soc, expected", [
    (87, 696), (100, 800), (0, 0), (None, None), (120, 800), (-5, 0),
])
def test_estimated_energy_from_soc(soc, expected):
    assert estimated_energy_wh(soc, 800) == expected


def test_estimated_energy_needs_a_battery_size():
    assert estimated_energy_wh(50, 0) is None


def test_unknown_fields_are_skipped_and_reported():
    packet = (
        field(10, 50)
        + raw_field(30, 1, b"\x00" * 8)          # 64-bit fixed
        + raw_field(31, 2, varint(3) + b"abc")   # length-delimited
        + raw_field(32, 5, b"\x00" * 4)          # 32-bit fixed
        + field(40, 7)                           # unknown varint
        + field(12, 1000)
    )
    unknown = {}
    assert decode_live_data(packet, unknown) == {"battery_soc": 50, "odometer_km": 1.0}
    assert unknown == {30: None, 31: None, 32: None, 40: 7}


def test_truncated_packet_keeps_fields_decoded_so_far():
    packet = field(10, 50) + b"\x60\xff"  # field 12 tag, then an unfinished varint
    assert decode_live_data(packet) == {"battery_soc": 50}


def test_empty_packet():
    assert decode_live_data(b"") == {}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance_minutes(self, minutes):
        self.now += minutes * 60


def test_charge_estimator_needs_two_samples_before_estimating():
    clock = FakeClock()
    est = ChargeEstimator(clock)
    est.update(50.0, charging=True)
    assert est.eta_to(50.0, True, 80.0) is None


def test_charge_estimator_linear_eta_below_cv_knee():
    clock = FakeClock()
    est = ChargeEstimator(clock)
    est.update(50.0, charging=True)
    clock.advance_minutes(2)          # 1 % per 2 min = 0.5 %/min
    est.update(51.0, charging=True)
    assert est.eta_to(51.0, True, 80.0) == 58   # 29 % / 0.5


def test_charge_estimator_slows_down_above_cv_knee():
    clock = FakeClock()
    est = ChargeEstimator(clock)
    est.update(50.0, charging=True)
    clock.advance_minutes(2)
    est.update(51.0, charging=True)
    # 34 % at 0.5 %/min to 85 %, then 15 % at 0.2 %/min.
    assert est.eta_to(51.0, True, 100.0) == round(68 + 75)


def test_charge_estimator_ignores_connect_time_soc_jump():
    clock = FakeClock()
    est = ChargeEstimator(clock)
    est.update(40.0, charging=True)
    clock.advance_minutes(0.1)        # +5 % in 6 s is a sync jump, not a rate
    est.update(45.0, charging=True)
    assert est.eta_to(45.0, True, 80.0) is None


def test_charge_estimator_does_nothing_when_not_charging():
    est = ChargeEstimator(FakeClock())
    est.update(50.0, charging=False)
    assert est.eta_to(50.0, False, 80.0) is None
