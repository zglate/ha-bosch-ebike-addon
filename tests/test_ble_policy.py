"""Tests for the shared-adapter safety rules."""
import pytest

import ble_policy as bp


def test_address_from_device_path():
    path = "/org/bluez/hci0/dev_a4_0d_bc_01_23_45"
    assert bp.address_from_path(path) == "A4:0D:BC:01:23:45"


@pytest.mark.parametrize("path", [
    "/org/bluez/hci0",
    "/org/bluez/hci0/dev_A4_0D_BC_01_23_45/service0010/char0011",
    "/org/bluez/hci0/dev_A4_0D",
    "",
])
def test_non_device_paths_have_no_address(path):
    assert bp.address_from_path(path) is None


def test_explorer_inventory_from_plain_objects():
    pytest.importorskip("dbus")          # runs in the Linux test container
    import explorer
    dev = "/org/bluez/hci0/dev_A4_0D_BC_01_23_45"
    objects = {
        f"{dev}/service0010": {"org.bluez.GattService1": {"UUID": "svc-mcsp"}},
        f"{dev}/service0010/char0011": {"org.bluez.GattCharacteristic1": {
            "UUID": "00000011-EAA2-11e9-81b4-2a2ae2dbcce4", "Flags": ["notify"], "MTU": 247}},
        f"{dev}/service0010/char0011/desc0013": {"org.bluez.GattDescriptor1": {
            "UUID": "00002902-0000-1000-8000-00805f9b34fb"}},
        f"{dev}/service0020/char0021": {"org.bluez.GattCharacteristic1": {
            "UUID": "00002a26-0000-1000-8000-00805f9b34fb", "Flags": ["read"]}},
    }
    chars = explorer.inventory(objects)
    mcsp_rx = explorer.find_char(chars, "00000011-eaa2-11e9-81b4-2a2ae2dbcce4")
    assert mcsp_rx["service"] == "svc-mcsp" and mcsp_rx["mtu"] == 247
    assert [d["uuid"] for d in mcsp_rx["descriptors"]] == [
        "00002902-0000-1000-8000-00805f9b34fb"]
    fw = explorer.find_char(chars, "00002a26-0000-1000-8000-00805f9b34fb")
    assert fw["name"] == "Firmware Revision" and fw["service"] is None


def test_only_connection_changes_are_relevant():
    assert bp.is_relevant_change({"Connected": False})
    assert bp.is_relevant_change({"ServicesResolved": True, "RSSI": -60})
    assert not bp.is_relevant_change({"RSSI": -60, "ManufacturerData": {}})


def test_ldi_service_detection_is_case_insensitive():
    assert bp.advertises_ldi(["0000180F-0000-1000-8000-00805F9B34FB",
                              bp.LDI_SERVICE_UUID.upper()])
    assert not bp.advertises_ldi(["0000180f-0000-1000-8000-00805f9b34fb"])
    assert not bp.advertises_ldi(None)


def test_pairing_accepted_while_window_open():
    assert bp.should_accept_pairing("AA:BB:CC:DD:EE:FF", True, set())
    assert bp.should_accept_pairing(None, True, set())


def test_pairing_outside_window_only_for_known_bikes():
    known = {"AA:BB:CC:DD:EE:FF"}
    assert bp.should_accept_pairing("aa:bb:cc:dd:ee:ff", False, known)
    assert not bp.should_accept_pairing("11:22:33:44:55:66", False, known)
    assert not bp.should_accept_pairing(None, False, known)


def test_bosch_advert_fields():
    info = bp.parse_bosch_advert(bytes([0xAA, 0xBB, 2, 0x21, 1, 1]))
    assert info == {"raw": "aabb02210101", "length": 6, "component_id_version": 2,
                    "component_id": 0x21, "data_format_version": 1, "pairable": True}
    assert bp.parse_bosch_advert(bytes([0, 0, 2, 0x21, 1, 0]))["pairable"] is False


def test_unknown_bosch_advert_layout_is_kept_raw():
    assert bp.parse_bosch_advert(b"\x01\x02") == {"raw": "0102", "length": 2}


ADAPTERS = ["/org/bluez/hci1", "/org/bluez/hci0"]


def test_adapter_by_name():
    assert bp.adapter_path_for(ADAPTERS, "hci1") == "/org/bluez/hci1"


def test_adapter_auto_picks_first_sorted():
    assert bp.adapter_path_for(ADAPTERS, "auto") == "/org/bluez/hci0"


def test_missing_adapter():
    assert bp.adapter_path_for(ADAPTERS, "hci5") is None
    assert bp.adapter_path_for([], "auto") is None
