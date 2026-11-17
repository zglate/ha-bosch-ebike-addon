#!/usr/bin/env python3
"""
Bosch eBike Live Data Interface – Raspberry Pi BLE → MQTT bridge.

Architecture
------------
The Bosch eBike acts as a BLE *central*: it scans for peripherals that
advertise a Service Solicitation UUID matching its own LDI GATT service.
When it finds one it connects, pairs (Just-Works LE Secure Connections),
and the bridge then acts as a GATT *client* on that connection to subscribe
to notifications on the Live Data characteristic (eb21).

On the Pi:
  1. BlueZ LE advertising with SolicitUUIDs = LDI service UUID  →  eBike connects
  2. JustWorksAgent accepts pairing automatically
  3. When ServicesResolved fires, find eb21 and call StartNotify()
  4. PropertiesChanged(Value) delivers each notification payload
  5. Decode protobuf → publish JSON to MQTT → HA auto-discovery

Both bikes can be active simultaneously; BlueZ handles two concurrent
connections on the Pi 4's CYW43455 BT 5.0 controller.

Home Assistant add-on version
-----------------------------
Vendored from possm/ha-bosch-ebike-pibridge (Apache-2.0, commit 82aa562) and
changed to share the Bluetooth adapter with Home Assistant's own Bluetooth
integration. Changes are marked "ADD-ON:" and summarised in NOTICE.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

import threading

import ble_policy
import datapoints
import firmware
import mcsp
import status
from nearby import Nearby
from capture import Capture
from explorer import BikeExplorer, Hooks, find_char, gatt_objects, inventory
from explorer import plain as explorer_plain
from livedata import decode_live_data, estimated_energy_wh, ChargeEstimator
from ha_mqtt import HaMqttPublisher
import supervisor
import webui
from options import ConfigError, MqttMissing, battery_wh_for, build_config, load_options

log = logging.getLogger(__name__)

# ── BlueZ D-Bus interface names ───────────────────────────────────────────────
BLUEZ_SERVICE    = "org.bluez"
ADAPTER_IFACE    = "org.bluez.Adapter1"
DEVICE_IFACE     = "org.bluez.Device1"
GATT_CHR_IFACE   = "org.bluez.GattCharacteristic1"
LE_ADV_MGR_IFACE = "org.bluez.LEAdvertisingManager1"
LE_ADV_IFACE     = "org.bluez.LEAdvertisement1"
AGENT_MGR_IFACE  = "org.bluez.AgentManager1"
AGENT_IFACE      = "org.bluez.Agent1"
OM_IFACE         = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE      = "org.freedesktop.DBus.Properties"

# Bosch LDI GATT UUIDs (spec V1.0: 0000xxxx-eaa2-11e9-81b4-2a2ae2dbcce4)
LDI_SERVICE_UUID = ble_policy.LDI_SERVICE_UUID
LDI_CHAR_UUID    = "0000eb21-eaa2-11e9-81b4-2a2ae2dbcce4"

# ADD-ON: re-register the advertisement this often while a bike is missing.
# Xunil99's ESP32 bridge found that advertising can silently stop (issue #59);
# on HA OS, HA's Bluetooth recovery may also power-cycle the shared adapter.
ADVERTISING_WATCHDOG_SEC = 5 * 60

OPTIONS_PATH = "/data/options.json"

AGENT_PATH       = "/org/bosch_ebike_bridge/agent"
ADV_PATH         = "/org/bosch_ebike_bridge/advertisement0"

# How long the discoverable pairing window stays open (seconds) after boot or
# when reopened via the HA button. Outside the window the bridge advertises
# privately (no LDI solicitation) so strangers' Flow apps can't see it.
# ADD-ON: overridable so the integration tests don't wait 5 minutes.
PAIRING_WINDOW_SEC = int(os.environ.get("LDI_PAIRING_WINDOW_SEC", 5 * 60))

# After this many consecutive disconnects that happen before services resolved
# (the stale-bond signature), the bond for that address is dropped so the bike
# used to be dropped automatically. We only log it now (a busy bike or a
# refused central connection looks the same); see _on_disconnected().
STALE_BOND_THRESHOLD = 3

# Central connections (to reach MCSP): minimum time between attempts to the
# same device, and between any two attempts on the shared adapter.
CONNECT_RETRY_SEC = float(os.environ.get("BOSCH_CONNECT_RETRY_SEC", 60))      # env: tests
CONNECT_GLOBAL_GAP_SEC = float(os.environ.get("BOSCH_CONNECT_GAP_SEC", 30))   # env: tests

# Our own Bluetooth scanning, so bikes are found even if Home Assistant's
# Bluetooth integration isn't scanning on this adapter: a short scan every
# SCAN_EVERY_SEC while no bike is connected, and continuously during pairing.
SCAN_EVERY_SEC = int(os.environ.get("BOSCH_SCAN_EVERY_SEC", 60))              # env: tests
SCAN_FOR_SEC = int(os.environ.get("BOSCH_SCAN_FOR_SEC", 15))                  # env: tests

# Firmware check: first one shortly after start, then daily.
FIRMWARE_FIRST_CHECK_SEC = int(os.environ.get("BOSCH_FIRMWARE_FIRST_SEC", 60))  # env: tests
FIRMWARE_STALE_SEC = 7 * 24 * 3600   # older published versions are not trusted
# "Pair selected bike": pair at once if the bike advertised this recently,
# else wait (scanning) up to PAIR_WAIT_SEC for its next advertisement. A
# Bosch bike may advertise only briefly after switching on.
PAIR_RECENT_SEC = float(os.environ.get("BOSCH_PAIR_RECENT_SEC", 5))      # env: tests
PAIR_WAIT_SEC = int(os.environ.get("BOSCH_PAIR_WAIT_SEC", 180))          # env: tests
# "Connect like the Flow app": the longest the add-on pauses LDI to try MCSP
# over a connection it opens itself (the phone's role).
WINDOW_SEC = int(os.environ.get("BOSCH_WINDOW_SEC", 300))                # env: tests
WINDOW_TICK_SEC = 600      # how often to check whether a repeat check is due
WAITING = "waiting for the bike to advertise"
_UNSET = object()                    # "nothing shown yet" for the setup notice

# Where the debug capture and report go (the add-on maps /share), and where
# the explorer remembers which firmware it has already swept.
CAPTURE_DIR = "/share/bosch_ebike"
STATE_FILE = Path(os.environ.get("BOSCH_STATE_DIR", "/data")) / "explorer_state.json"


class _BikeHooks(Hooks):
    """Connects one bike's explorer to the bridge."""

    def __init__(self, bridge: "EbikeBridge", addr: str) -> None:
        self._bridge, self._addr = bridge, addr

    def on_value(self, dp_id, value):
        self._bridge._on_mcsp_value(self._addr, dp_id, value)

    def on_status(self, status):
        self._bridge._on_mcsp_status(self._addr, status)

    def should_sweep(self, firmware):
        return self._bridge._should_sweep(self._addr, firmware)

    def on_sweep_done(self, firmware):
        self._bridge._sweep_done(self._addr, firmware)

    def is_moving(self):
        return self._bridge._is_moving(self._addr)

    def on_release(self):
        self._bridge._release_bike(self._addr)

    def on_silent(self):
        self._bridge._on_mcsp_silent(self._addr)


# ── Pairing agent ─────────────────────────────────────────────────────────────

class PairingRejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class JustWorksAgent(dbus.service.Object):
    """Accept BLE Just-Works (NoInputNoOutput) pairing requests.

    ADD-ON: `allow(device_path) -> bool` decides each request, because on a
    shared HA host an accept-everything agent would let any nearby device
    bond with the server (see ble_policy.should_accept_pairing).
    """

    def __init__(self, bus: dbus.SystemBus, allow, record=lambda **event: None) -> None:
        dbus.service.Object.__init__(self, bus, AGENT_PATH)
        self._allow = allow
        self._record = record          # writes each decision to the debug capture

    def _check(self, request: str, device: str) -> None:
        accepted = self._allow(str(device))
        self._record(request=request, device=str(device), accepted=accepted)
        if accepted:
            log.info("%s %s – accepted", request, device)
            return
        log.warning("%s %s – rejected (pairing window closed, unknown device). "
                    "Press 'Start pairing' in Home Assistant first.", request, device)
        raise PairingRejected("Pairing window closed")

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        pass

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device: str, uuid: str) -> None:
        self._check(f"AuthorizeService uuid={uuid}", device)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device: str) -> None:
        self._check("RequestAuthorization", device)

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def DisplayPasskey(self, device: str, passkey: int) -> None:
        log.info("DisplayPasskey %s passkey=%06d", device, passkey)

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device: str, passkey: int) -> None:
        # Just-Works: confirm without checking the number
        self._check(f"RequestConfirmation passkey={passkey:06d}", device)

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device: str) -> str:
        self._check("RequestPinCode", device)
        return "0000"

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device: str) -> dbus.UInt32:
        self._check("RequestPasskey", device)
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self) -> None:
        pass


# ── LE Advertisement ──────────────────────────────────────────────────────────

class LDIAdvertisement(dbus.service.Object):
    """
    LE peripheral advertisement with Service Solicitation UUID.

    BlueZ's SolicitUUIDs property maps directly to AD type 0x15 (128-bit
    Service Solicitation), which is exactly what the eBike scans for.

    Appearance 0x0480 = Cycling Generic.

    Name placement note: BlueZ's high-level LEAdvertisement1 D-Bus API always
    puts the LocalName in the SCAN RESPONSE, not the primary advertising PDU —
    and offers no way to control this (verified with btmon). A scanner only
    receives the name if it does an ACTIVE scan (sends a scan request). Phones
    and the Bosch Flow app do active scans, so they show the bridge name fine.
    The eBike's own accessory-discovery scan appears to be passive, so during
    that one-time pairing step it may show only the MAC address. This is
    cosmetic: once bonded, the bike reconnects automatically and the correct
    per-bike names appear in Home Assistant and the dashboard. Forcing the name
    into the primary PDU would require bypassing BlueZ with raw HCI commands,
    which is fragile and conflicts with bluetoothd — deliberately not done.
    """

    def __init__(self, bus: dbus.SystemBus, local_name: str,
                 pairing: bool = True) -> None:
        dbus.service.Object.__init__(self, bus, ADV_PATH)
        self._local_name = local_name
        # pairing=True  → discoverable + LDI solicitation, so a Flow app can add
        #                 the bridge (used during the 5-min pairing window).
        # pairing=False → private reconnect: name only, NO solicitation, not
        #                 discoverable, so other people's Flow apps don't see us.
        #                 A bonded bike still reconnects by bond/address.
        self._pairing = pairing

    def _props(self) -> dict:
        props = {
            "Type":         dbus.String("peripheral"),
            "LocalName":    dbus.String(self._local_name),
            "Discoverable": dbus.Boolean(bool(self._pairing)),
        }
        if self._pairing:
            # Only the pairing window carries the solicitation UUID + Cycling
            # appearance — that's exactly what makes a Flow app recognise us as
            # a pairable Bosch accessory.
            props["SolicitUUIDs"] = dbus.Array([LDI_SERVICE_UUID], signature="s")
            props["Appearance"] = dbus.UInt16(0x0480)
        return props

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface: str, prop: str):
        return self._props()[prop]

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str) -> dict:
        return self._props()

    @dbus.service.method(LE_ADV_IFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        log.info("Advertisement released by BlueZ")


# ── Main bridge ───────────────────────────────────────────────────────────────

class EbikeBridge:
    def __init__(self, config: dict) -> None:
        self._cfg = config
        self._bus = dbus.SystemBus()
        self._loop = GLib.MainLoop()

        # BLE advertisement handle + adapter path (set in run()).
        self._adv = None
        self._adapter_path = None

        # Pairing window: monotonic deadline (seconds) until which the bridge
        # advertises discoverable WITH the LDI solicitation, so a Flow app can
        # add it. Outside the window it advertises privately (no solicitation).
        # Opened on boot and by the HA "Start pairing" button. 0 = never opened.
        self._pairing_until = 0.0
        # Whether the pairing window may be opened at all. When a config sets
        # privacy off, the bridge always advertises with the solicitation (old
        # behaviour) so nothing changes for users who want it.
        self._privacy = bool(config.get("private_advertising", True))

        # Consecutive failed-pairing counter per address. A "failed pairing" is
        # a disconnect that happened before services ever resolved — the classic
        # symptom of a stale bond after a reboot (SMP "Authentication Failed").
        # After STALE_BOND_THRESHOLD such failures we drop the bond so the bike
        # can re-pair automatically. Reset to 0 on every successful resolve.
        self._connect_fails: dict[str, int] = {}

        # Per-bike state: addr (upper) → {name, state, char_path}
        self._bikes: dict[str, dict] = {}
        for bike in config.get("bikes", []):
            addr = bike["address"].upper()
            self._bikes[addr] = {
                "name":      bike.get("name", f"eBike {addr[-5:].replace(':', '')}"),
                "state":     {},
                "char_path": None,
            }

        # ADD-ON: addresses known to be Bosch bikes (configured, seen with the
        # LDI service, or bonded with it cached). Everything else on the shared
        # adapter belongs to Home Assistant and is left alone.
        self._known_bikes: set[str] = set(self._bikes)

        # ADD-ON: undocumented LDI fields already logged (log each once).
        self._unknown_fields_seen: set[int] = set()

        # ADD-ON: pairing agent object (created once) and the last seen
        # bluetoothd bus owner (to detect bluetoothd restarts).
        self._agent: JustWorksAgent | None = None
        self._bluez_owner: str | None = None
        # ADD-ON: adapter Pairable as found, restored when the window closes.
        self._pairable_initial = False

        # Explorer: GATT inventory, MCSP, adverts and the debug capture.
        self._table = datapoints.load_table()
        capture_dir = os.environ.get("BOSCH_CAPTURE_DIR", CAPTURE_DIR)   # env: tests
        self._capture = Capture(capture_dir if config.get("capture", True) else None)
        self._use_mcsp = bool(config.get("mcsp", True))
        self._mcsp_connect = bool(config.get("mcsp_connect", True))
        self._auto_sweep = bool(config.get("mcsp_sweep", True))
        self._gatt_listen = bool(config.get("gatt_listen", True))
        self._raw_entities = bool(config.get("raw_entities", True))
        self._adverts_seen: dict[str, str] = {}      # device path -> last payload hex
        self._connect_tried: dict[str, float] = {}   # device path -> monotonic time
        self._hold_off: dict[str, float] = {}        # device path -> no connect before
        self._connect_in_flight = False
        self._last_connect = -1e9
        self._skipped_foreign: set[str] = set()      # other people's bikes, logged once
        self._bosch_paths: set[str] = set()          # every Bosch-related device
        self._reconnect_hours = float(config.get("mcsp_reconnect_hours", 6))
        self._open_pairing_on_start = bool(config.get("open_pairing_on_start", False))
        self._entity_by_id = {e.id: e for e in datapoints.ENTITIES}
        try:
            self._explorer_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._explorer_state = {}
        # Bikes paired through "Start pairing" stay known across restarts.
        self._known_bikes |= {a for a, s in self._explorer_state.items()
                              if isinstance(s, dict) and s.get("adopted")}
        self._scanning = False
        self._scan_stop_timer = None
        self._scan_heard: dict[str, dict] = {}       # address -> rssi/pairable this scan
        self._scan_paths: set[str] = set()           # every device heard this scan
        # Last Bosch manufacturer data per device. BlueZ sends ManufacturerData
        # once (InterfacesAdded, or when it changes); after that a bike that
        # keeps advertising the same thing only shows up as RSSI updates.
        self._bosch_adverts: dict[str, bytes | None] = {}
        self._adopting: set[str] = set()             # device paths being paired
        # Bikes heard nearby (dropdown), the user's pick, and consent to pair it.
        self._nearby = Nearby()
        self._selected: str | None = None
        self._consented: set[str] = set()
        self._last_scan_heard: int | None = None
        self._setup_note = _UNSET                    # key of the setup notice shown
        self._fw_removed: set[str] = set()           # bikes whose update entities were removed
        self._armed: dict[str, float] = {}           # picked bike -> wait for it until
        self._window: dict | None = None             # "Connect like the Flow app" running
        # Who opened a link: we did only if our Connect/Pair call came before
        # BlueZ reported it connected (else the bike connected to us: LDI).
        self._connect_called: dict[str, float] = {}  # device path -> monotonic
        self._connected_at: dict[str, float] = {}    # device path -> monotonic
        self._abandoned: set[str] = set()            # window connects to drop if they land
        self._bridge_link: str | None = None         # markdown link to the bridge device
        # Firmware: Bosch's published versions (cached across restarts).
        self._firmware_check = bool(config.get("firmware_check", True))
        self._firmware_page = config.get("firmware_page") or firmware.DEFAULT_PAGE

        mqtt_cfg = config["mqtt"]
        self._mqtt = HaMqttPublisher(
            broker=mqtt_cfg["broker"],
            port=int(mqtt_cfg.get("port", 1883)),
            username=mqtt_cfg.get("username"),
            password=mqtt_cfg.get("password"),
            base_topic=mqtt_cfg.get("base_topic", "bosch_ebike"),
            version=config.get("version"),
        )

    # ── BlueZ helpers ─────────────────────────────────────────────────────────

    def _get_adapter_path(self) -> str:
        # On a cold boot the controller (hci0) may not be enumerated on D-Bus
        # yet when the service starts. Poll briefly instead of failing outright
        # so the bridge survives a reboot on slower boards (Pi 3) as well as
        # faster ones (Pi 4).
        # ADD-ON: pick the adapter named in the options (default hci0), so a
        # dedicated USB dongle later is a config change, not a code change.
        wanted = self._cfg.get("adapter", "hci0")
        for attempt in range(1, 16):
            om = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/"), OM_IFACE)
            adapters = [str(path) for path, ifaces in om.GetManagedObjects().items()
                        if ADAPTER_IFACE in ifaces]
            path = ble_policy.adapter_path_for(adapters, wanted)
            if path:
                return path
            log.warning(
                "Bluetooth adapter '%s' not found yet (attempt %d/15, present: %s)",
                wanted, attempt, ", ".join(adapters) or "none",
            )
            time.sleep(2)
        raise RuntimeError(
            f"Bluetooth adapter '{wanted}' not found. Check the 'adapter' option "
            "and Settings → System → Hardware."
        )

    @staticmethod
    def _rfkill_unblock_bluetooth() -> None:
        """Best-effort `rfkill unblock bluetooth`, model/path agnostic.

        On a fresh boot systemd-rfkill may restore the adapter to a SOFT-BLOCKED
        state, so powering it on via BlueZ fails with org.bluez.Error.Failed.
        The systemd unit already calls rfkill via ExecStartPre, but on slower
        boards (e.g. Pi 3) the unblock can land before the controller is fully
        up. Calling it again here, with the binary looked up across the usual
        locations, closes that race on both Pi 3 and Pi 4.
        """
        for binary in ("rfkill", "/usr/sbin/rfkill", "/sbin/rfkill"):
            try:
                subprocess.run(
                    [binary, "unblock", "bluetooth"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
                return
            except (FileNotFoundError, subprocess.SubprocessError):
                continue
        # ADD-ON: expected inside the add-on container (no rfkill, no
        # privileges); Home Assistant OS manages rfkill on the host.
        log.debug("rfkill not available — leaving rfkill to the host")

    def _setup_adapter(self, adapter_path: str) -> None:
        adapter = self._bus.get_object(BLUEZ_SERVICE, adapter_path)
        props = dbus.Interface(adapter, PROPS_IFACE)

        # Powering on can fail right after boot if the adapter is still
        # rfkill-soft-blocked or bluetoothd hasn't finished bringing hci0 up.
        # Retry with an rfkill unblock between attempts instead of crashing —
        # this self-heals across reboots on any board (Pi 3 / Pi 4 / etc.).
        last_exc: Exception | None = None
        for attempt in range(1, 11):
            try:
                props.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
                last_exc = None
                break
            except dbus.DBusException as exc:
                last_exc = exc
                log.warning(
                    "Adapter power-on attempt %d/10 failed (%s) — "
                    "unblocking rfkill and retrying",
                    attempt, exc.get_dbus_name(),
                )
                self._rfkill_unblock_bluetooth()
                time.sleep(2)
        if last_exc is not None:
            raise last_exc

        # ADD-ON: upstream made the whole adapter Discoverable + Pairable for
        # good. On a shared HA host that exposes the server to classic
        # Bluetooth pairing. The LE advertisement carries its own Discoverable
        # flag (BlueZ: it "will override adapter Discoverable property"), so
        # adapter Discoverable is not touched at all, and Pairable is only
        # switched on during the pairing window, then restored.
        try:
            self._pairable_initial = bool(props.Get(ADAPTER_IFACE, "Pairable"))
        except dbus.DBusException:
            self._pairable_initial = False
        log.info("Adapter %s powered (pairable was %s)",
                 adapter_path, self._pairable_initial)

    def _register_agent(self) -> JustWorksAgent:
        if self._agent is None:
            self._agent = JustWorksAgent(
                self._bus, self._pairing_allowed,
                lambda **event: self._capture.event("pairing_request", **event))
        mgr = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, "/org/bluez"), AGENT_MGR_IFACE
        )
        try:
            mgr.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
        except dbus.DBusException as exc:
            if exc.get_dbus_name() != "org.bluez.Error.AlreadyExists":
                raise
        # ADD-ON: BlueZ only routes incoming pairing requests to the DEFAULT
        # agent. If something else on the host holds that role, the bike's
        # pairing stalls silently — so make this failure loud.
        try:
            mgr.RequestDefaultAgent(AGENT_PATH)
        except dbus.DBusException as exc:
            log.error(
                "Could not become the default Bluetooth pairing agent (%s). "
                "Another program on the host handles pairing, so the bike may "
                "not be able to pair. See DOCS → Troubleshooting.", exc,
            )
        log.info("Just-Works pairing agent registered")
        return self._agent

    def _pairing_allowed(self, device_path: str) -> bool:
        """ADD-ON: agent callback — accept only during the window or known bikes."""
        address = self._device_address(device_path)
        if self._window is not None:
            log.warning("Refusing pairing from %s while checking MCSP", address)
            return False
        # Known bikes, and a bike the user explicitly picked in Home Assistant.
        return ble_policy.should_accept_pairing(
            address, self._window_open(), self._known_bikes | self._consented
        )

    def _device_address(self, device_path: str) -> str | None:
        """ADD-ON: a device's current address.

        Read from BlueZ rather than the object path: once BlueZ learns a
        bike's identity key it updates Device1.Address, but the object path
        keeps the (random) address it was created with.
        """
        try:
            props = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, device_path), PROPS_IFACE)
            return str(props.Get(DEVICE_IFACE, "Address")).upper()
        except dbus.DBusException:
            return ble_policy.address_from_path(device_path)

    def _set_pairable(self, pairable: bool) -> None:
        """ADD-ON: switch adapter pairing on for the window, back off after."""
        try:
            dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, self._adapter_path), PROPS_IFACE
            ).Set(ADAPTER_IFACE, "Pairable", dbus.Boolean(pairable))
        except dbus.DBusException as exc:
            log.warning("Could not set adapter Pairable=%s: %s", pairable, exc)

    def _window_open(self) -> bool:
        """ADD-ON: True while the user-opened pairing window is running.

        Pairing is only accepted in the window, whatever the privacy setting.
        """
        return self._pairing_until > time.monotonic()

    def _pairing_open(self) -> bool:
        """True while the bridge should advertise the LDI solicitation.

        When privacy is disabled this is always True, so the bridge always
        advertises with the solicitation (the pre-privacy behaviour).
        """
        if not self._privacy:
            return True
        return self._window_open()

    def _register_advertisement(self, adapter_path: str) -> LDIAdvertisement:
        local_name = self._cfg.get("bridge_name", "HA eBike Bridge")
        pairing = self._pairing_open()
        adv = LDIAdvertisement(self._bus, local_name, pairing=pairing)
        mgr = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, adapter_path), LE_ADV_MGR_IFACE
        )
        mode = ("PAIRING: discoverable + solicitation" if pairing
                else "private reconnect: no solicitation, not discoverable")
        mgr.RegisterAdvertisement(
            ADV_PATH, {},
            reply_handler=lambda: log.info("Advertising '%s' (%s)", local_name, mode),
            error_handler=lambda e: log.error("RegisterAdvertisement failed: %s", e),
        )
        return adv

    def _start_pairing_idle(self) -> bool:
        """GLib idle wrapper so an MQTT-thread button press runs on the loop."""
        self.start_pairing()
        return False  # run once

    def start_pairing(self) -> None:
        """Open the discoverable pairing window (HA button / boot)."""
        self._pairing_until = time.monotonic() + PAIRING_WINDOW_SEC
        log.info("Pairing window open for %d min — bridge is discoverable",
                 PAIRING_WINDOW_SEC // 60)
        self._set_pairable(True)
        self._publish_pairing_state()
        self._readvertise()
        self._refresh_setup()
        # Look for bikes in pairing mode for the whole window.
        if self._adapter_path:
            self._start_scan(PAIRING_WINDOW_SEC)
        # Re-advertise privately again once the window elapses, so we don't stay
        # discoverable forever. GLib timeout fires on the main loop thread.
        GLib.timeout_add_seconds(PAIRING_WINDOW_SEC + 1, self._pairing_window_elapsed)

    # ── Finding bikes ourselves ──────────────────────────────────────────────

    def _scan_tick(self) -> bool:
        """Every SCAN_EVERY_SEC: a short scan while no bike is connected, plus
        housekeeping (forget bikes not heard lately, keep Status texts current)."""
        connected = any(b.get("char_path") for b in self._bikes.values())
        if not connected and not self._scanning and not self._adopting \
                and self._window is None:
            self._start_scan(SCAN_FOR_SEC)
        if self._nearby.prune():
            self._publish_nearby()
            self._refresh_setup()
        for addr in self._bikes:
            self._refresh_bike(addr)
        return True

    def _start_scan(self, seconds: int) -> None:
        """Bluetooth LE discovery from our own D-Bus client.

        BlueZ keeps discovery per client and merges filters, so this doesn't
        change Home Assistant's own scanning.
        """
        adapter = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, self._adapter_path), ADAPTER_IFACE)
        if self._scan_stop_timer is not None:
            GLib.source_remove(self._scan_stop_timer)
        self._scan_stop_timer = GLib.timeout_add_seconds(seconds, self._stop_scan)
        if self._scanning:
            return
        self._scanning = True
        self._scan_heard: dict[str, dict] = {}
        self._scan_paths = set()
        try:
            # An RSSI (proximity) filter, even one letting everything through,
            # makes BlueZ report every advertisement. Without it BlueZ only
            # reports a device again when its signal moves by 8 dBm or more
            # (device.c RSSI_THRESHOLD), so a bike standing still goes silent.
            adapter.SetDiscoveryFilter({"Transport": "le", "RSSI": dbus.Int16(-127)})
        except dbus.DBusException as exc:
            log.debug("SetDiscoveryFilter: %s", exc)

        def started():
            log.info("Scanning for Bosch eBikes (%d s)", seconds)
            self._capture.event("scan_start", seconds=seconds)

        def failed(e):
            log.warning("Bluetooth scan could not start: %s", e)
            self._capture.event("scan_error", error=str(e))

        adapter.StartDiscovery(reply_handler=started, error_handler=failed)

    def _stop_scan(self) -> bool:
        self._scan_stop_timer = None
        if self._scanning:
            self._scanning = False
            heard = self._scan_heard
            if heard:
                log.info("Scan done: heard %s", ", ".join(
                    f"{a} (signal {i.get('rssi')} dBm, {ble_policy.pairing_text(i.get('pairable'))}"
                    f"{', yours' if a in self._known_bikes else ''})" for a, i in heard.items()))
            else:
                log.info("Scan done: no Bosch eBike heard (is it switched on and within "
                         "range of the Home Assistant host?)")
            self._capture.event("scan_result", heard=heard)
            self._log_scan_devices()
            self._last_scan_heard = len(heard)
            if self._nearby.prune():
                self._publish_nearby()
            self._refresh_setup()
            dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, self._adapter_path),
                           ADAPTER_IFACE).StopDiscovery(
                reply_handler=lambda: self._capture.event("scan_stop"),
                error_handler=lambda e: self._capture.event("scan_stop_error", error=str(e)))
        return False

    def _log_scan_devices(self) -> None:
        """Everything the scan heard, Bosch or not: tells "the Pi hears nothing"
        apart from "the bike doesn't advertise the way we expect"."""
        try:
            objects = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/"),
                                     OM_IFACE).GetManagedObjects()
        except dbus.DBusException as exc:
            log.debug("Scan summary: %s", exc)
            return
        devices = []
        for path in sorted(self._scan_paths):
            props = objects.get(dbus.ObjectPath(path), {}).get(DEVICE_IFACE)
            if props is None:
                continue
            devices.append({
                "address": str(props.get("Address", "")), "name": str(props.get("Name", "")),
                "rssi": int(props["RSSI"]) if "RSSI" in props else None,
                "manufacturer_ids": [f"0x{int(k):04X}" for k in
                                     props.get("ManufacturerData", {}).keys()],
                "service_uuids": [str(u) for u in props.get("UUIDs", [])],
                "service_data": [str(k) for k in props.get("ServiceData", {}).keys()]})
        self._capture.event("scan_devices", devices=devices)
        ids: dict[str, int] = {}
        for d in devices:
            for m in d["manufacturer_ids"] or ["none"]:
                ids[m] = ids.get(m, 0) + 1
        named = sorted((d for d in devices if d["name"]),
                       key=lambda d: -(d["rssi"] if d["rssi"] is not None else -999))[:8]
        log.info("Scan heard %d Bluetooth devices in total; manufacturer IDs: %s; "
                 "strongest named: %s", len(devices),
                 ", ".join(f"{k}×{v}" for k, v in sorted(ids.items(), key=lambda kv: -kv[1]))
                 or "none",
                 ", ".join(f"{d['name']} ({d['rssi']} dBm)" for d in named) or "none")

    def _adopt(self, device_path: str, addr: str, info: dict) -> None:
        """Pair with a bike the user agreed to. Two ways in:

        - Start pairing was pressed and the bike advertises "ready to pair";
        - the user picked it in Nearby Bosch eBikes and pressed Pair selected
          bike (works whatever the bike advertises).
        After this the bike is known and reconnects by itself.
        """
        if device_path in self._adopting:
            log.info("Pairing with %s already in progress", addr)
            return
        if self._window is not None:
            log.info("Not pairing with %s while checking MCSP; try again afterwards", addr)
            return
        self._adopting.add(device_path)
        self._connect_called[device_path] = time.monotonic()
        if self._scanning:              # one radio: let the connection have it
            if self._scan_stop_timer is not None:
                GLib.source_remove(self._scan_stop_timer)
            self._stop_scan()
        # The kernel only stores a bond while the adapter is bondable
        # (Pairable); outside the window switch it on just for this Pair().
        if not self._window_open():
            self._set_pairable(True)

        def done() -> None:
            self._adopting.discard(device_path)
            if not self._window_open():
                self._set_pairable(self._pairable_initial)
        log.info("Pairing with Bosch eBike %s (%s, signal %s dBm)", addr,
                 ble_policy.pairing_text(info.get("pairable")), info.get("rssi"))
        self._capture.event("pair_attempt", address=addr, path=device_path)

        def paired() -> None:
            done()
            self._known_bikes.add(addr)
            self._explorer_state.setdefault(addr, {})["adopted"] = (
                datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self._save_state()
            self._capture.event("paired", address=addr)
            log.info("Paired with %s", addr)
            supervisor.dismiss(f"pair_{addr.replace(':', '').lower()}")
            if self._selected == addr:
                self._selected = None
            entry = self._nearby.get(addr)
            if entry:
                self._nearby.heard(addr, entry["path"], entry["rssi"], entry["pairable"], True)
            self._publish_nearby()
            self._refresh_setup()
            # Services may have resolved while pairing was still running.
            try:
                props = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, device_path),
                                       PROPS_IFACE)
                if bool(props.Get(DEVICE_IFACE, "ServicesResolved")):
                    self._on_services_resolved(device_path, addr)
            except dbus.DBusException as exc:
                log.debug("ServicesResolved check for %s: %s", addr, exc)

        def failed(exc) -> None:
            done()
            self._forget_link(device_path)
            self._consented.discard(addr)
            # Don't keep a link to a bike that isn't ours: Pair() opened it.
            dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, device_path),
                           DEVICE_IFACE).Disconnect(
                reply_handler=lambda: None,
                error_handler=lambda e: log.debug("Disconnect %s: %s", addr, e))
            log.warning("Pairing with %s failed: %s", addr, exc.get_dbus_name())
            self._capture.event("pair_failed", address=addr, error=str(exc))
            supervisor.notify(f"pair_{addr.replace(':', '').lower()}",
                              "Bosch eBike: pairing failed",
                              supervisor.PAIRING_FAILED.format(
                                  address=addr, reason=exc.get_dbus_name(),
                                  bridge=self._bridge_ref()))

        dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, device_path), DEVICE_IFACE).Pair(
            reply_handler=paired, error_handler=failed, timeout=60)

    def _pairing_window_elapsed(self) -> bool:
        if not self._window_open():
            log.info("Pairing window closed — switching to private advertising")
            self._set_pairable(self._pairable_initial)
            self._publish_pairing_state()
            self._readvertise()
            self._refresh_setup()
        return False  # one-shot timer

    def _publish_pairing_state(self) -> None:
        if self._mqtt is not None:
            self._mqtt.publish_pairing_active(self._window_open())

    def _bike_paths(self) -> set[str]:
        """BlueZ object paths of bikes we have seen connected."""
        return {b["device_path"] for b in self._bikes.values() if b.get("device_path")}

    def _all_known_bikes_connected(self) -> bool:
        """True if every bike listed in config is currently connected.

        A bike counts as connected once its char_path is set. If no bikes are
        configured (auto-discover mode) this is always False, so the bridge
        keeps advertising to pick up any bike.
        """
        configured = [b for b in self._cfg.get("bikes", [])]
        if not configured:
            return False
        for b in configured:
            addr = b["address"].upper()
            state = self._bikes.get(addr)
            if not state or not state.get("char_path"):
                return False
        return True

    def _unregister_advertisement(self) -> None:
        """Tear down the current advert object, ignoring if already gone."""
        try:
            mgr = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, self._adapter_path),
                LE_ADV_MGR_IFACE,
            )
            try:
                mgr.UnregisterAdvertisement(ADV_PATH)
            except dbus.DBusException:
                pass
            try:
                if self._adv is not None:
                    self._adv.remove_from_connection()
            except Exception:
                pass
            self._adv = None
        except dbus.DBusException as exc:
            log.warning("Unregister advertisement failed: %s", exc)

    def _readvertise(self) -> None:
        """Keep the named LE advertisement in the right state.

        BlueZ stops (releases) a connectable advertisement as soon as a peer
        connects. With only one advert object the bridge then falls back to the
        adapter's generic discoverable mode, which exposes the raw MAC but NOT
        our bridge name or the LDI Service Solicitation UUID — so a SECOND eBike
        only sees a nameless MAC.

        Policy:
          • If every configured bike is connected, stop advertising entirely —
            there is no free slot, so broadcasting just wastes airtime and
            invites unknown devices.
          • Otherwise, (re-)register the named, solicitation-carrying advert so
            the next bike can still discover the bridge by name.
        """
        if self._window is not None:
            log.debug("Checking MCSP like the Flow app: not advertising")
            return
        if self._all_known_bikes_connected():
            log.info("All configured bikes connected — stopping advertising")
            self._unregister_advertisement()
            return

        try:
            # Drop any stale advert object before re-registering.
            self._unregister_advertisement()
            self._adv = self._register_advertisement(self._adapter_path)
        except dbus.DBusException as exc:
            log.warning("Re-advertise failed: %s", exc)

    def _device_path(self, addr: str) -> dbus.ObjectPath:
        """BlueZ object path for a peer, e.g. /org/bluez/hci0/dev_A4_0D_BC_..."""
        return dbus.ObjectPath(
            f"{self._adapter_path}/dev_{addr.upper().replace(':', '_')}"
        )

    def _forget_bikes_idle(self) -> bool:
        """GLib idle wrapper so an MQTT-thread button press runs on the loop."""
        self.forget_bikes()
        return False  # run once

    def forget_bikes(self) -> None:
        """ADD-ON: remove the bonds of every known bike, then open pairing.

        Backs the "Forget paired bikes" button: the manual fix when a bike
        won't reconnect. Only bikes are touched, never Home Assistant's other
        Bluetooth devices.
        """
        adapter = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, self._adapter_path), ADAPTER_IFACE)
        for addr in sorted(self._known_bikes):
            path = self._bikes.get(addr, {}).get("device_path") or self._device_path(addr)
            self._mark_offline(addr)
            try:
                adapter.RemoveDevice(path)
                log.warning("Forgot bike %s (bond removed)", addr)
            except dbus.DBusException as exc:
                log.info("No bond to remove for %s: %s", addr, exc.get_dbus_name())
        # Configured bikes stay known (the user listed them); learned ones go.
        self._known_bikes = {b["address"].upper() for b in self._cfg.get("bikes", [])}
        for state in self._explorer_state.values():
            if isinstance(state, dict):
                state.pop("adopted", None)
        self._save_state()
        self._consented.clear()
        self._adopting.clear()
        self._selected = None
        self._connect_fails.clear()
        log.warning("Bikes forgotten — add the bike again in the Flow app")
        self.start_pairing()

    def _find_ldi_char(self, device_path: str) -> str | None:
        """Return the D-Bus path of the LDI Live Data characteristic, or None."""
        om = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/"), OM_IFACE)
        for path, ifaces in om.GetManagedObjects().items():
            if GATT_CHR_IFACE not in ifaces:
                continue
            if not str(path).startswith(device_path):
                continue
            uuid = str(ifaces[GATT_CHR_IFACE].get("UUID", "")).lower()
            if uuid == LDI_CHAR_UUID:
                return str(path)
        return None

    # ── Notification handling ─────────────────────────────────────────────────

    def _subscribe(self, char_path: str, bike_addr: str) -> None:
        """Enable GATT notifications on the LDI characteristic."""
        try:
            char_obj = self._bus.get_object(BLUEZ_SERVICE, char_path)

            # Connect PropertiesChanged signal *before* StartNotify so we
            # don't miss the first notification.
            # ADD-ON: keep the match so it can be removed on disconnect;
            # upstream added a new one per connection, so after N rides every
            # notification was handled N times.
            self._remove_notify_match(bike_addr)
            self._bikes[bike_addr]["notify_match"] = self._bus.add_signal_receiver(
                handler_function=lambda iface, changed, inv, path=char_path:
                    self._on_char_changed(path, bike_addr, iface, changed),
                signal_name="PropertiesChanged",
                dbus_interface=PROPS_IFACE,
                bus_name=BLUEZ_SERVICE,
                path=char_path,
                path_keyword=None,
            )

            dbus.Interface(char_obj, GATT_CHR_IFACE).StartNotify(
                reply_handler=lambda: log.info(
                    "Notifications enabled for %s", bike_addr
                ),
                error_handler=lambda e: log.error(
                    "StartNotify failed for %s: %s", bike_addr, e
                ),
            )

            # Initial read for a full snapshot (notifications only carry changed
            # fields; without a read we'd wait for the first change per field).
            try:
                raw = bytes(
                    dbus.Interface(char_obj, GATT_CHR_IFACE).ReadValue({})
                )
                self._handle_payload(bike_addr, raw, source="initial-read")
            except dbus.DBusException as exc:
                log.warning(
                    "Initial read failed for %s (will rely on notifications): %s",
                    bike_addr, exc,
                )

        except dbus.DBusException as exc:
            log.error("Failed to subscribe for %s: %s", bike_addr, exc)

    def _remove_notify_match(self, bike_addr: str) -> None:
        """ADD-ON: drop a bike's notification signal handler, if any."""
        match = self._bikes.get(bike_addr, {}).pop("notify_match", None)
        if match is not None:
            match.remove()

    def _mark_offline(self, bike_addr: str) -> None:
        """Forget a bike's live connection and tell Home Assistant."""
        self._remove_notify_match(bike_addr)
        bike = self._bikes.get(bike_addr)
        if bike:
            bike["state"] = {}
            bike["char_path"] = None
            self._end_session(bike_addr, "disconnected")
        self._capture.event("bike_offline", bike=bike_addr)
        if bike:
            bike["ldi"] = False
            self._refresh_bike(bike_addr)
            self._refresh_setup()
        self._capture.write_report()
        self._mqtt.publish_availability(bike_addr, False)

    def _on_char_changed(
        self, char_path: str, bike_addr: str, iface: str, changed: dict
    ) -> None:
        if iface != GATT_CHR_IFACE or "Value" not in changed:
            return
        raw = bytes(changed["Value"])
        self._handle_payload(bike_addr, raw, source="notify")

    def _handle_payload(self, bike_addr: str, raw: bytes, source: str) -> None:
        log.debug("[%s] %s: %s", bike_addr, source, raw.hex())
        unknown: dict = {}
        try:
            data = decode_live_data(raw, unknown)
        except Exception as exc:
            log.error("Decode error for %s: %s", bike_addr, exc)
            self._capture.event("ldi", bike=bike_addr, source=source, data=raw,
                                error=str(exc))
            return
        self._capture.event("ldi", bike=bike_addr, source=source, data=raw,
                            decoded=data, unknown=unknown)
        ldi = self._capture.bike(bike_addr).setdefault("ldi", {})
        ldi.update(data, last_seen=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if unknown:
            ldi.setdefault("unknown_fields", {}).update(
                {str(k): v for k, v in unknown.items()})
        # ADD-ON: newer bike firmware may add fields; surface each one once.
        for field_no, value in unknown.items():
            if field_no not in self._unknown_fields_seen:
                self._unknown_fields_seen.add(field_no)
                log.info("Undocumented LDI field %d = %s (worth reporting upstream)",
                         field_no, value)

        if not data:
            return

        # Auto-register unknown bikes (addr not in config)
        if bike_addr not in self._bikes:
            name = f"eBike {bike_addr[-5:].replace(':', '')}"
            self._bikes[bike_addr] = {"name": name, "state": {}, "char_path": None}

        bike = self._bikes[bike_addr]
        bike["state"].update(data)   # merge sparse notification into running state

        # ── Charge-time estimation ─────────────────────────────────────────
        est = bike.setdefault("charge_est", ChargeEstimator())
        # Reset the estimate whenever the charging flag flips (plug/unplug),
        # so a fresh session re-seeds the reference point.
        if "charger_connected" in data:
            prev = bike.get("_was_charging")
            if prev is not None and data["charger_connected"] != prev:
                est.reset()
            bike["_was_charging"] = data["charger_connected"]
        charging = bool(bike["state"].get("charger_connected"))
        soc = bike["state"].get("battery_soc")
        soc = float(soc) if soc is not None else None
        est.update(soc, charging)
        # Always (re)publish the two ETA fields so they clear to null when not
        # charging. JSON null → HA template '' default → entity shows unknown.
        bike["state"]["charge_eta_80_min"] = est.eta_to(soc, charging, 80.0)
        bike["state"]["charge_eta_100_min"] = est.eta_to(soc, charging, 100.0)
        # ADD-ON: derived values (marked as estimates in HA) and freshness.
        # Battery size: the bike's own setting, else the add-on's.
        bike["state"]["energy_remaining_wh"] = estimated_energy_wh(
            soc, battery_wh_for(self._cfg, bike_addr))
        now_m = time.monotonic()
        if now_m - bike.get("_last_seen_mono", -1e9) >= 60:
            bike["_last_seen_mono"] = now_m
            bike["state"]["last_seen"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")

        self._mqtt.publish_discovery(bike_addr, bike["name"])
        self._mqtt.publish_state(bike_addr, bike["state"])
        log.debug("[%s] state update: %s", bike["name"], data)

    # ── Device lifecycle ──────────────────────────────────────────────────────

    def _on_services_resolved(self, device_path: str, addr: str) -> None:
        """Called once BlueZ has fully resolved the eBike's GATT services."""
        bike_addr = addr.upper()
        if device_path in self._abandoned and self._opened_by_us(device_path):
            log.info("[%s] The MCSP check had already ended; disconnecting", bike_addr)
            dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, device_path),
                           DEVICE_IFACE).Disconnect(reply_handler=lambda: None,
                                                    error_handler=lambda e: None)
            return
        if device_path in self._adopting:
            # Pair() opened this link; it becomes ours only once pairing
            # succeeds (paired() runs this again then).
            log.info("Services of %s resolved; waiting for pairing to finish", bike_addr)
            return
        char_path = self._find_ldi_char(device_path)
        chars = inventory(gatt_objects(self._bus, device_path))
        has_mcsp = bool(find_char(chars, mcsp.RX_UUID) and find_char(chars, mcsp.TX_UUID))
        if not char_path and not has_mcsp:
            # On a shared adapter most devices are Home Assistant's own
            # sensors, not bikes. Only complain about known bikes.
            if bike_addr in self._known_bikes:
                log.error(
                    "Neither LDI nor MCSP found on %s. "
                    "Is this a Bosch smart-system eBike with firmware ≥ v19?", bike_addr)
            return

        log.info("eBike %s connected – LDI %s, MCSP %s", bike_addr,
                 "yes" if char_path else "no", "yes" if has_mcsp else "no")
        self._known_bikes.add(bike_addr)

        # Successful resolve → connection is healthy, clear any failure count.
        self._connect_fails[bike_addr] = 0

        if bike_addr not in self._bikes:
            name = f"eBike {bike_addr[-5:].replace(':', '')}"
            self._bikes[bike_addr] = {"name": name, "state": {}, "char_path": char_path}
        else:
            self._bikes[bike_addr]["char_path"] = char_path
        self._bikes[bike_addr]["device_path"] = device_path
        # "char_path" marks the bike as connected; an MCSP-only link has no
        # LDI characteristic, so use the device path as the marker then.
        self._bikes[bike_addr]["char_path"] = char_path or device_path
        # Did we open this link (central connect), or did the bike (LDI)?
        self._bikes[bike_addr]["central"] = self._opened_by_us(device_path)
        if char_path and not self._bikes[bike_addr]["central"]:
            # The bike connects to us for LDI: never open central links to it
            # automatically (only "Connect like the Flow app" does, with LDI paused).
            state = self._explorer_state.setdefault(bike_addr, {})
            if not state.get("ldi_accessory"):
                state["ldi_accessory"] = True
                self._save_state()
        if self._window is not None and self._window["addr"] == bike_addr:
            self._window["phase"] = ("connected (opened by the add-on)"
                                     if self._bikes[bike_addr]["central"]
                                     else "the bike connected first (LDI)")
        self._bikes[bike_addr]["ldi"] = bool(char_path)
        self._bikes[bike_addr]["last_seen"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        self._bosch_paths.add(device_path)
        self._capture.event("bike_connected", bike=bike_addr, path=device_path,
                            ldi=bool(char_path), mcsp=has_mcsp,
                            opened_by="add-on" if self._bikes[bike_addr]["central"] else "bike")

        self._mqtt.publish_availability(bike_addr, True)
        self._announce_new_bike(bike_addr)
        if char_path:
            self._subscribe(char_path, bike_addr)
        self._start_explorer(bike_addr, device_path)
        self._refresh_bike(bike_addr)
        self._refresh_setup()

        # BlueZ released our advert when this bike connected. Re-register it so
        # a second eBike can still discover the bridge by name (not just MAC).
        self._readvertise()

    # ── Explorer (GATT inventory + MCSP) ──────────────────────────────────────

    def _start_explorer(self, addr: str, device_path: str) -> None:
        bike = self._bikes[addr]
        bike["mcsp_status"] = None
        if bike.get("explorer"):
            self._end_session(addr, "restarted")
        bike["explorer"] = BikeExplorer(
            self._bus, addr, device_path, self._table, self._capture, _BikeHooks(self, addr),
            use_mcsp=self._use_mcsp, gatt_listen=self._gatt_listen)
        try:
            bike["explorer"].start()
        except dbus.DBusException as exc:
            log.error("[%s] Explorer failed to start: %s", addr, exc)
            self._capture.event("explorer_error", bike=addr, error=str(exc))

    def _end_session(self, addr: str, reason: str) -> None:
        """Stop a bike's explorer and keep its final MCSP status in the report."""
        bike = self._bikes.get(addr) or {}
        explorer = bike.get("explorer")
        if not explorer:
            return
        explorer.stop(reason)
        if explorer.mcsp:
            self._capture.bike(addr)["sessions"].append(dict(explorer.mcsp.status))
        bike["explorer"] = None

    def _on_mcsp_status(self, addr: str, mcsp_status: dict) -> None:
        self._bikes[addr]["mcsp_status"] = mcsp_status
        self._mqtt.publish_mcsp_status(addr, self._bikes[addr]["name"], mcsp_status)
        self._refresh_bike(addr)

    def _should_sweep(self, addr: str, firmware: str | None) -> bool:
        """Sweep once per bike per drive-unit firmware version."""
        if not self._auto_sweep:
            return False
        done = self._explorer_state.get(addr, {}).get("swept_firmware")
        return done != (firmware or "unknown")

    def _sweep_done(self, addr: str, firmware: str | None) -> None:
        self._explorer_state.setdefault(addr, {}).update(
            swept_firmware=firmware or "unknown",
            swept_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._save_state()

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self._explorer_state, indent=1),
                                  encoding="utf-8")
        except OSError as exc:
            log.warning("Could not save explorer state: %s", exc)

    def _announce_new_bike(self, addr: str) -> None:
        """First time a bike connects that isn't in the options: tell the user
        its address, with a ready-to-paste 'bikes' entry."""
        configured = {b["address"].upper() for b in self._cfg.get("bikes", [])}
        state = self._explorer_state.setdefault(addr, {})
        if addr in configured or state.get("announced"):
            return
        if supervisor.notify(f"bike_{addr.replace(':', '').lower()}",
                             "Bosch eBike: bike connected",
                             supervisor.BIKE_FOUND.format(
                                 name=self._bikes[addr]["name"], address=addr)):
            state["announced"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._save_state()

    def _is_moving(self, addr: str) -> bool:
        """From the LDI: is the bike being ridden right now?"""
        ldi = self._bikes.get(addr, {}).get("state", {})
        return bool(ldi.get("in_motion")) or (ldi.get("speed_kmh") or 0) > 0

    def _release_bike(self, addr: str) -> None:
        """Explorer finished: let go of a link we opened, so the Flow app can
        connect, and don't reconnect for mcsp_reconnect_hours."""
        if self._window is not None and self._window["addr"] == addr:
            self._end_window(self._window.get("outcome") or "explored")
            return
        bike = self._bikes.get(addr) or {}
        path = bike.get("device_path")
        if not bike.get("central") or not path:
            log.info("[%s] Exploration done; the link was opened by the bike (LDI), "
                     "so it stays up", addr)
            self._capture.event("release_skipped", bike=addr, reason="bike-initiated link")
            return
        self._hold_off[path] = time.monotonic() + self._reconnect_hours * 3600
        bike["released_until"] = time.time() + self._reconnect_hours * 3600
        log.info("[%s] Exploration done; disconnecting so the Flow app can connect "
                 "(next connect in %s h)", addr, self._reconnect_hours)
        self._capture.event("released", bike=addr, reconnect_hours=self._reconnect_hours)
        dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, path), DEVICE_IFACE).Disconnect(
            reply_handler=lambda: None,
            error_handler=lambda e: self._capture.event("release_failed", bike=addr,
                                                        error=str(e)))
        self._refresh_bike(addr)

    def _on_mcsp_value(self, addr: str, dp_id: int, value) -> None:
        """Publish an MCSP datapoint that has a Home Assistant entity."""
        bike = self._bikes[addr]
        bike["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if dp_id in datapoints.IDENTITY:
            self._on_identity(addr, dp_id, value)
        if dp_id in (0x180D, 0x180C) and isinstance(value, list):
            # Long names preferred; short names only until long ones arrive.
            if dp_id == 0x180D or not bike.get("assist_names"):
                bike["assist_names"] = value
        if self._raw_entities:
            dp = self._table.get(dp_id)
            self._mqtt.publish_datapoint(addr, bike["name"], dp_id,
                                         dp.label if dp else f"unknown.0x{dp_id:04X}",
                                         dp.unit if dp else None, value)
        entity = self._entity_by_id.get(dp_id)
        if entity is None:
            return
        state = bike.setdefault("mcsp_state", {})
        state[entity.key] = datapoints.entity_state(entity, value, bike.get("assist_names"))
        state["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._mqtt.publish_mcsp_discovery(addr, bike["name"])
        self._mqtt.publish_mcsp_state(addr, state)

    def _sweep_idle(self) -> bool:
        """MQTT button: read every readable datapoint on every connected bike."""
        sessions = [b["explorer"].mcsp for b in self._bikes.values()
                    if b.get("explorer") and b["explorer"].mcsp]
        if not sessions:
            log.warning("Read sweep: no bike with an MCSP session is connected")
        for session in sessions:
            session.sweep(forced=True)
        return False

    def _on_bosch_advert(self, device_path: str, payload: bytes | None, props: dict,
                         live: bool = True) -> None:
        """Log Bosch advertisements, and connect to the bike if enabled.

        `payload` is the Bosch manufacturer data, or None when the bike was
        recognised by its discovery service only (pairing mode unknown then).
        """
        self._bosch_paths.add(device_path)
        if payload is None:
            payload = self._bosch_adverts.get(device_path)
        self._bosch_adverts[device_path] = payload
        addr = self._device_address(device_path) or device_path
        info = (ble_policy.parse_bosch_advert(payload) if payload is not None
                else {"raw": None, "pairable": None, "seen_by": "discovery service 0xFE02"})
        info.update(name=str(props.get("Name", "")),
                    rssi=int(props["RSSI"]) if "RSSI" in props else None, path=device_path)
        if "AdvertisingFlags" in props:
            info["advertising_flags"] = bytes(props["AdvertisingFlags"]).hex()
        # Log again when the data or the advertising flags change (flags
        # 0x05 vs 0x06 = limited vs general discoverable: maybe pairing mode).
        hex_payload = (payload.hex() if payload is not None else "none") + "/" + str(
            info.get("advertising_flags"))
        if self._adverts_seen.get(device_path) != hex_payload:
            self._adverts_seen[device_path] = hex_payload
            log.info("Bosch advertisement from %s: %s", addr, info)
            self._capture.advert(addr, info)
            self._capture.event("advert", address=addr, live=live, **info)
        if not live:
            return
        if self._scanning:
            self._scan_heard[addr] = {"rssi": info.get("rssi"), "pairable": info.get("pairable")}
        # The Nearby dropdown and Setup Status show every Bosch bike heard.
        if self._nearby.heard(addr, device_path, info.get("rssi"), info.get("pairable"),
                              addr in self._known_bikes):
            self._publish_nearby()
            self._refresh_setup()
        if self._window is not None and addr == self._window["addr"]:
            if self._window["phase"] == WAITING and not self._is_connected(device_path):
                self._window_connect(device_path)
            return
        if addr in self._armed:
            del self._armed[addr]
            log.info("Heard %s again: pairing now", addr)
            self._adopt(device_path, addr, info)
            return
        if not self._mcsp_connect:
            return
        if addr in self._known_bikes:
            self._maybe_connect(device_path)
        elif self._window_open() and info.get("pairable") is True:
            self._adopt(device_path, addr, info)

    # ── Nearby dropdown, Setup Status, bike Status ───────────────────────────

    def _publish_nearby(self) -> None:
        options = self._nearby.options()
        selected = next((o for o in options if Nearby.address_of(o) == self._selected), None)
        if selected is None:
            self._selected, selected = None, options[0]
        self._mqtt.set_select("nearby", options, selected)

    def _on_nearby_selected(self, option: str) -> bool:
        self._selected = Nearby.address_of(option)
        log.info("Nearby bike selected: %s", self._selected or "none")
        self._publish_nearby()
        return False

    def _pair_selected(self) -> bool:
        """'Pair selected bike': the user's explicit pick is the consent."""
        entry = self._nearby.get(self._selected) if self._selected else None
        if entry is None:
            log.warning("Pair selected bike: pick a bike under 'Nearby Bosch eBikes' first")
            self._capture.event("pair_selected_none")
            return False
        addr = self._selected
        if addr in self._known_bikes:
            log.info("Pair selected bike: %s is already added", addr)
            return False
        self._consented.add(addr)
        quiet = time.time() - entry["seen"]
        if quiet <= PAIR_RECENT_SEC:
            self._adopt(entry["path"], addr, {"rssi": entry["rssi"], "pairable": entry["pairable"]})
            return False
        # Gone quiet: wait for its next advertisement (pairing needs it listening).
        self._armed[addr] = time.monotonic() + PAIR_WAIT_SEC
        log.info("Pair selected bike: %s last heard %d s ago. Waiting up to %d s for it to "
                 "advertise (switch it on, or hold its power button until the light turns "
                 "blue); pairing as soon as it's heard", addr, quiet, PAIR_WAIT_SEC)
        self._capture.event("pair_armed", address=addr, quiet_sec=round(quiet))
        if self._adapter_path and not self._adopting:
            self._start_scan(PAIR_WAIT_SEC)
        GLib.timeout_add_seconds(PAIR_WAIT_SEC + 1, self._pair_wait_elapsed, addr)
        self._refresh_setup()
        return False

    def _pair_wait_elapsed(self, addr: str) -> bool:
        deadline = self._armed.get(addr)
        if deadline is not None and time.monotonic() >= deadline:
            del self._armed[addr]
            self._consented.discard(addr)
            log.warning("Pair selected bike: %s wasn't heard within %d s; nothing paired",
                        addr, PAIR_WAIT_SEC)
            self._capture.event("pair_wait_expired", address=addr)
            supervisor.notify(f"pair_{addr.replace(':', '').lower()}",
                              "Bosch eBike: pairing didn't start",
                              supervisor.PAIRING_FAILED.format(
                                  address=addr, bridge=self._bridge_ref(),
                                  reason=f"the bike wasn't heard within {PAIR_WAIT_SEC // 60} min"))
            self._refresh_setup()
        return False

    def _bridge_ref(self) -> str:
        """A link to the bridge device's page in Home Assistant, once found."""
        if self._bridge_link is None:
            device_id = supervisor.render_template(supervisor.BRIDGE_DEVICE_TEMPLATE)
            if device_id:
                self._bridge_link = (f"[the Bosch eBike Bridge device]"
                                     f"(/config/devices/device/{device_id})")
        return self._bridge_link or status.BRIDGE

    def _refresh_setup(self) -> None:
        """Setup Status sensor plus the evolving 'setup' notification."""
        connected = sorted(a for a, b in self._bikes.items() if b.get("char_path"))
        bridge = status.BRIDGE if self._known_bikes else self._bridge_ref()
        state, attributes, note = status.setup_status(
            sorted(self._known_bikes), connected, self._nearby.strangers(),
            self._window_open(), self._last_scan_heard,
            waiting=next(iter(self._armed), None), bridge=bridge)
        self._mqtt.publish_setup_status(state, attributes)
        key = None if note is None else (bridge, tuple(
            (a, b["pairable"]) for a, b in sorted(self._nearby.strangers())))
        if key == self._setup_note:
            return
        if note is None:
            supervisor.dismiss("setup")
            self._setup_note = None
        elif supervisor.notify("setup", "Bosch eBike: add your bike", note):
            self._setup_note = key

    def _refresh_bike(self, addr: str) -> None:
        bike = self._bikes.get(addr)
        if not bike:
            return
        w = self._window if self._window and self._window["addr"] == addr else None
        state, attributes = status.bike_status(
            bool(bike.get("ldi")), bike.get("mcsp_status"), bike.get("released_until"),
            bike.get("last_seen"), time.time(), window_phase=w and w["phase"],
            last_check=self._explorer_state.get(addr, {}).get("mcsp_window"))
        self._mqtt.publish_bike_status(addr, bike["name"], state, attributes)

    # ── Identity and firmware ────────────────────────────────────────────────

    def _on_identity(self, addr: str, dp_id: int, value) -> None:
        """A component told us what it is; update the device and firmware."""
        component, field = datapoints.IDENTITY[dp_id]
        bike = self._bikes[addr]
        identity = bike.setdefault("identity", {}).setdefault(component, {})
        if identity.get(field) == value:
            return
        identity[field] = value
        self._capture.bike(addr).setdefault("identity", {})[component] = dict(identity)
        drive = bike["identity"].get("DriveUnit", {})
        info = {k: v for k, v in (("model", drive.get("productName")),
                                  ("sw_version", drive.get("softwareVersion")),
                                  ("hw_version", drive.get("hardwareVersion")),
                                  ("serial_number", drive.get("serialNumber"))) if v}
        self._mqtt.set_device_info(addr, bike["name"], info)
        self._refresh_bike(addr)
        if field in ("productName", "softwareVersion"):
            self._evaluate_firmware(addr)

    def _firmware_tick(self) -> bool:
        """Daily: fetch Bosch's published versions in the background."""
        if not self._firmware_check:
            return True

        def work():
            try:
                versions = firmware.parse_versions(firmware.fetch(self._firmware_page))
                GLib.idle_add(self._firmware_loaded, versions, None)
            except Exception as exc:          # network, HTTP, parsing: never fatal
                GLib.idle_add(self._firmware_loaded, None, str(exc))
        threading.Thread(target=work, daemon=True).start()
        return True

    def _firmware_loaded(self, versions: dict | None, error: str | None) -> bool:
        if not versions:
            log.warning("Firmware check failed (%s); will retry tomorrow",
                        error or "no versions on the page")
            self._capture.event("firmware_check_failed", error=error)
            return False
        self._explorer_state["_firmware"] = {
            "versions": versions, "page": self._firmware_page,
            "checked": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._save_state()
        log.info("Firmware check: Bosch publishes %d component versions", len(versions))
        self._capture.event("firmware_versions", versions=versions)
        for addr in self._bikes:
            self._evaluate_firmware(addr)
        return False

    def _evaluate_firmware(self, addr: str) -> None:
        """Publish an update entity per component, and notify once per release."""
        bike = self._bikes[addr]
        if not self._firmware_check:
            if addr not in self._fw_removed:      # entities from an earlier run go
                self._fw_removed.add(addr)
                for key, _title, _component in firmware.COMPONENTS:
                    self._mqtt.remove_firmware(addr, key)
            return
        versions = self._published_versions()
        notified = self._explorer_state.setdefault(addr, {}).setdefault("fw_notified", {})
        for key, title, component in firmware.COMPONENTS:
            identity = bike.get("identity", {}).get(component, {})
            installed = identity.get("softwareVersion")
            if not installed:
                continue
            published = firmware.match(identity.get("productName"), component, versions)
            self._mqtt.publish_firmware(addr, bike["name"], key, title, firmware.update_payload(
                title, installed, published, self._firmware_page), available=bool(published))
            note_id = f"firmware_{addr.replace(':', '').lower()}_{key}"
            if published and not firmware.is_newer(published[1], installed) \
                    and key in notified:
                notified.pop(key)                 # installed: the notice is done
                self._save_state()
                supervisor.dismiss(note_id)
            if published and firmware.is_newer(published[1], installed) \
                    and notified.get(key) != published[1]:
                notified[key] = published[1]
                self._save_state()
                log.info("[%s] %s firmware %s published (installed %s)", addr, title,
                         published[1], installed)
                supervisor.notify(
                    note_id,
                    f"Bosch eBike: {title} firmware {published[1]} available",
                    f"Bosch lists **{published[1]}** for *{published[0]}*; your bike "
                    f"reports **{installed}**. Update with the Bosch Flow app. This is "
                    f"Bosch's published version for that product line, not a check "
                    f"of your bike's eligibility. [Release notes]"
                    f"({firmware.release_notes_url(self._firmware_page)})")

    def _published_versions(self) -> dict:
        """Bosch's published versions, if checked recently from the configured page."""
        cache = self._explorer_state.get("_firmware") or {}
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(cache["checked"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return {}
        if cache.get("page") != self._firmware_page or age > FIRMWARE_STALE_SEC:
            return {}
        return cache.get("versions") or {}

    def _opened_by_us(self, device_path: str) -> bool:
        called = self._connect_called.get(device_path)
        connected = self._connected_at.get(device_path)
        return called is not None and connected is not None and connected >= called

    def _is_connected(self, device_path: str) -> bool:
        try:
            return bool(dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, device_path),
                                       PROPS_IFACE).Get(DEVICE_IFACE, "Connected"))
        except dbus.DBusException:
            return False

    # ── "Connect like the Flow app": MCSP over a link the add-on opens ───────

    def _forget_link(self, device_path: str) -> None:
        """A link ended (or never came up): its role record goes with it."""
        self._connect_called.pop(device_path, None)
        self._connected_at.pop(device_path, None)
        self._abandoned.discard(device_path)

    def start_window(self, addr: str | None = None, auto: bool = False) -> bool:
        """Pause LDI and connect to the bike the way the Flow app does.

        On the first real bike MCSP was silent on the link the bike opened for
        LDI. The Flow app (and BikeBridge) connect *to* the bike instead. So:
        stop advertising (the bike can't reconnect for LDI), drop the LDI link,
        connect on the bike's next advertisement, run MCSP, then disconnect and
        advertise again so LDI comes back. Bounded by WINDOW_SEC; advertising
        is always restored; no pairing meanwhile (the LDI bond's keys stay).
        """
        if self._window is not None:
            log.info("Connect like the Flow app: already running")
            return False
        known = sorted(self._known_bikes)
        target = addr or next((a for a in known if self._bikes.get(a, {}).get("char_path")),
                              known[0] if known else None)
        if target is None:
            log.warning("Connect like the Flow app: no bike added yet")
            return False
        if self._is_moving(target):
            log.warning("Connect like the Flow app: %s is moving; try when it stands still",
                        target)
            return False
        path = self._bikes.get(target, {}).get("device_path") or self._device_path(target)
        window = {"addr": target, "path": str(path), "phase": WAITING,
                  "started": time.monotonic(), "outcome": None, "auto": auto,
                  "connect_issued": False}
        self._window = window
        # Timers first: whatever fails below, the window ends and advertising returns.
        GLib.timeout_add_seconds(WINDOW_SEC, self._window_deadline, window)
        GLib.timeout_add_seconds(2, self._window_watch, window)
        try:
            log.info("[%s] Connect like the Flow app: pausing LDI (advertising off) and "
                     "connecting to the bike on its next advertisement (up to %d s)",
                     target, WINDOW_SEC)
            self._capture.event("mcsp_window_start", bike=target, auto=auto)
            bike = self._bikes.get(target, {})
            bike["mcsp_status"] = None             # judge only this window's session
            self._unregister_advertisement()
            if bike.get("char_path"):
                def drop_failed(e) -> None:
                    if self._window is window:
                        self._end_window(f"couldn't drop the LDI link: {e.get_dbus_name()}")
                dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, path),
                               DEVICE_IFACE).Disconnect(reply_handler=lambda: None,
                                                        error_handler=drop_failed)
            if self._adapter_path and not self._adopting:
                self._start_scan(WINDOW_SEC)
            self._refresh_bike(target)
        except Exception as exc:                   # D-Bus gone, disk full ...
            log.error("[%s] Connect like the Flow app couldn't start: %s", target, exc)
            self._end_window(f"couldn't start: {exc}")
        return False

    def _window_connect(self, device_path: str) -> None:
        w = self._window
        w["phase"] = "connecting (as the Flow app does)"
        w["connect_issued"] = True
        if self._scanning:                      # one radio: let the connection have it
            if self._scan_stop_timer is not None:
                GLib.source_remove(self._scan_stop_timer)
            self._stop_scan()
        self._connect_called[device_path] = time.monotonic()
        log.info("[%s] Heard the bike: connecting to it as central", w["addr"])
        self._capture.event("window_connect", bike=w["addr"], path=device_path)
        self._refresh_bike(w["addr"])

        def failed(exc) -> None:
            self._forget_link(device_path)
            if self._window is w:
                self._end_window(f"connect failed: {exc.get_dbus_name()}")

        dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, device_path), DEVICE_IFACE).Connect(
            reply_handler=lambda: self._capture.event("window_connected", bike=w["addr"]),
            error_handler=failed, timeout=40)

    def _window_watch(self, window: dict) -> bool:
        """Every 2 s: did the bike answer on *our* link? Done once it goes idle."""
        if self._window is not window:
            return False
        bike = self._bikes.get(window["addr"], {})
        st = bike.get("mcsp_status") or {}
        ours = bool(bike.get("central") and bike.get("char_path"))
        if ours and window["outcome"] is None and (st.get("notifications")
                                                   or st.get("bike_requests")
                                                   or st.get("values")):
            window["outcome"] = "replies"
            log.info("[%s] The bike answers MCSP on this connection", window["addr"])
            self._capture.event("window_replies", bike=window["addr"])
            self._refresh_bike(window["addr"])
        if window["outcome"] == "replies" and str(st.get("state", "")).startswith("idle"):
            self._end_window("replies")
            return False
        return True

    def _window_deadline(self, window: dict) -> bool:
        if self._window is window:
            self._end_window(window["outcome"] or {
                WAITING: "no advertisement heard from the bike",
            }.get(window["phase"], f"no MCSP answer before the time limit ({window['phase']})"))
        return False

    def _on_mcsp_silent(self, addr: str) -> None:
        w = self._window
        if w is not None and w["addr"] == addr:
            bike = self._bikes.get(addr, {})
            self._end_window("connected, but the bike sent nothing on MCSP" if bike.get("central")
                             else "the bike reconnected for LDI before the add-on could connect")
            return
        log.warning("[%s] The bike isn't answering MCSP on the link it opened for LDI. "
                    "Press 'Connect like the Flow app' (bike on, standing still) to try "
                    "the way the app connects", addr)
        self._refresh_bike(addr)

    def _end_window(self, outcome: str) -> None:
        w = self._window
        if w is None:
            return
        self._window = None
        addr = w["addr"]
        try:
            log.info("[%s] Connect like the Flow app finished: %s", addr, outcome)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            state = self._explorer_state.setdefault(addr, {})
            state["mcsp_window"] = {"outcome": outcome, "at": now}
            if outcome == "replies":
                state["mcsp_central_ok"] = now
            self._save_state()
            bike = self._bikes.get(addr) or {}
            # Drop our link: connected, still connecting, or not resolved yet.
            path = w["path"]
            ours = bike.get("central") and bike.get("char_path")
            if ours or w["connect_issued"]:
                path = bike.get("device_path") if ours else path
                self._abandoned.add(str(path))     # if a pending connect lands later
                dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, path),
                               DEVICE_IFACE).Disconnect(
                    reply_handler=lambda: None,
                    error_handler=lambda e: log.debug("Window disconnect: %s", e))
            if self._scanning:
                if self._scan_stop_timer is not None:
                    GLib.source_remove(self._scan_stop_timer)
                self._stop_scan()
            self._capture.event("mcsp_window_end", bike=addr, outcome=outcome,
                                phase=w["phase"],
                                seconds=round(time.monotonic() - w["started"]))
        except Exception as exc:
            log.error("[%s] Ending the MCSP check: %s", addr, exc)
        finally:
            self._readvertise()                  # LDI can reconnect, whatever happened
        if not w["auto"] or outcome != "replies":
            supervisor.notify(f"mcsp_check_{addr.replace(':', '').lower()}",
                              "Bosch eBike: MCSP check finished",
                              supervisor.MCSP_CHECK.format(
                                  outcome=outcome, address=addr,
                                  next=("repeats every "
                                        f"{self._reconnect_hours:g} h while the bike is in "
                                        "range" if outcome == "replies" and self._reconnect_hours
                                        else "won't repeat by itself")))
        self._refresh_bike(addr)

    def _window_tick(self) -> bool:
        """Repeat the check every mcsp_reconnect_hours, but only for a bike that
        has answered over a link we opened (else LDI would drop for nothing)."""
        try:
            if self._window is not None or not self._reconnect_hours:
                return True
            for addr, state in self._explorer_state.items():
                if not isinstance(state, dict) or not state.get("mcsp_central_ok"):
                    continue
                try:
                    last = datetime.fromisoformat(state["mcsp_window"]["at"])
                except (KeyError, TypeError, ValueError):
                    continue
                hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                if (hours >= self._reconnect_hours
                        and self._bikes.get(addr, {}).get("char_path")
                        and not self._is_moving(addr)):
                    self.start_window(addr=addr, auto=True)
                    break
        except Exception as exc:
            log.error("Repeat MCSP check: %s", exc)
        return True

    def _maybe_connect(self, device_path: str) -> None:
        """Connect to one of *your* advertising bikes as a central (the Flow
        app's role), to reach MCSP.

        Only bikes listed in the options or already bonded with Bosch
        services; never someone else's bike. One attempt in flight across the
        adapter, at most one every CONNECT_GLOBAL_GAP_SEC, once a minute per
        device, and not again for mcsp_reconnect_hours after a release.
        """
        now = time.monotonic()
        if (now < self._hold_off.get(device_path, 0)
                or now - self._connect_tried.get(device_path, -1e9) < CONNECT_RETRY_SEC
                or self._connect_in_flight
                or now - self._last_connect < CONNECT_GLOBAL_GAP_SEC):
            return
        try:
            props = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, device_path),
                                   PROPS_IFACE).GetAll(DEVICE_IFACE)
        except dbus.DBusException:
            return
        addr = str(props.get("Address", "")).upper()
        if bool(props.get("Connected", False)):
            return
        if self._window is not None or self._explorer_state.get(addr, {}).get("ldi_accessory"):
            return
        if addr not in self._known_bikes:
            if device_path not in self._skipped_foreign:
                self._skipped_foreign.add(device_path)
                log.info("Bosch device %s is advertising but isn't one of your bikes; "
                         "not connecting (add its address under 'bikes' to allow it)", addr)
                self._capture.event("connect_skipped", address=addr,
                                    reason="not a configured or bonded bike")
            return
        self._connect_tried[device_path] = now
        self._last_connect = now
        self._connect_in_flight = True
        self._connect_called[device_path] = now
        log.info("Connecting to %s as central (MCSP)", addr)
        self._capture.event("connect_attempt", address=addr,
                            paired=bool(props.get("Paired", False)))

        def done(event: str, **extra) -> None:
            self._connect_in_flight = False
            self._capture.event(event, address=addr, **extra)

        def failed(e) -> None:
            log.info("Connect to %s failed: %s", addr, e.get_dbus_name())
            done("connect_failed", error=str(e))

        dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, device_path), DEVICE_IFACE).Connect(
            reply_handler=lambda: done("connect_ok"), error_handler=failed)

    def _on_disconnected(self, addr: str, device_path: str | None = None) -> None:
        bike_addr = addr.upper()
        # ADD-ON: never touch Home Assistant's own BLE devices. Upstream would
        # count their disconnects and eventually remove their bonds.
        if bike_addr not in self._known_bikes:
            return
        log.info("eBike disconnected: %s", bike_addr)
        w = self._window
        if w is not None and w["addr"] == bike_addr and w["phase"] != WAITING:
            self._mark_offline(bike_addr)
            self._end_window(w.get("outcome") or "the bike dropped the connection")
            return

        # Was this a healthy session (services resolved) or a pre-resolve drop?
        # A drop before services ever resolved is the stale-bond signature.
        was_resolved = bool(self._bikes.get(bike_addr, {}).get("char_path"))
        self._mark_offline(bike_addr)

        if was_resolved:
            # Clean disconnect of a working connection — not a failure.
            self._connect_fails[bike_addr] = 0
        else:
            self._connect_fails[bike_addr] = self._connect_fails.get(bike_addr, 0) + 1
            n = self._connect_fails[bike_addr]
            log.warning(
                "Pre-resolve disconnect for %s (%d/%d) — possible stale bond",
                bike_addr, n, STALE_BOND_THRESHOLD,
            )
            self._capture.event("pre_resolve_disconnect", bike=bike_addr, count=n)
            if n >= STALE_BOND_THRESHOLD:
                # Never remove a bond automatically (a busy bike or a refused
                # central connection looks the same). Tell the user instead.
                log.warning("[%s] Looks like a stale bond. If the bike won't reconnect, "
                            "press 'Forget paired bikes' and pair it again.", bike_addr)
                self._capture.event("stale_bond_suspected", bike=bike_addr, count=n)
                self._connect_fails[bike_addr] = 0

        # Ensure the named advert is live again after a disconnect.
        self._readvertise()

    # ── D-Bus signal monitoring ───────────────────────────────────────────────

    def _monitor(self) -> None:
        """
        Watch for:
          • InterfacesAdded   – device appears (first connect)
          • PropertiesChanged – Connected / ServicesResolved changes on Device1
        """

        def on_interfaces_added(path: str, interfaces: dict) -> None:
            if DEVICE_IFACE not in interfaces:
                return
            props = interfaces[DEVICE_IFACE]
            addr = str(props.get("Address", ""))
            # A device heard for the first time arrives here, with its
            # advertisement: the only time BlueZ sends unchanged data.
            if self._scanning:
                self._scan_paths.add(str(path))
            is_bike, payload = ble_policy.bosch_bike_advert(props)
            if is_bike:
                self._on_bosch_advert(str(path), payload, props)
            # ServicesResolved may already be True if this is a fast re-bond
            if bool(props.get("ServicesResolved", False)):
                self._on_services_resolved(str(path), addr)

        def on_properties_changed(
            iface: str, changed: dict, invalidated: list, sender_path: str = ""
        ) -> None:
            if iface == ADAPTER_IFACE and sender_path == self._adapter_path:
                self._on_adapter_changed(changed)
                return
            if iface != DEVICE_IFACE:
                return

            if self._scanning and ("RSSI" in changed or "ManufacturerData" in changed):
                self._scan_paths.add(sender_path)
            # Bosch advertisements (from any device, bike or not yet known):
            # new data, or the same data again (only RSSI changes then).
            is_bike, payload = ble_policy.bosch_bike_advert(changed)
            if is_bike or ("RSSI" in changed and sender_path in self._bosch_adverts):
                self._on_bosch_advert(sender_path, payload, changed)
            # Every property change of a Bosch-related device goes to the
            # capture (Connected, Paired, Bonded, Address, ServicesResolved ...),
            # including the first connection of a bike we haven't seen yet.
            uuids = [str(u).lower() for u in changed.get("UUIDs", [])]
            if ble_policy.advertises_ldi(uuids) or mcsp.SERVICE_UUID in uuids:
                self._bosch_paths.add(sender_path)
            if sender_path in self._bosch_paths:
                self._capture.event("device_changed", path=sender_path,
                                    changed=explorer_plain(changed))

            # While HA scans, Device1 RSSI/ManufacturerData change many times
            # a second. Drop those cheaply before any D-Bus round trip.
            if not ble_policy.is_relevant_change(changed):
                return
            if ble_policy.address_from_path(sender_path) is None:
                return
            addr = self._device_address(sender_path)

            if "Connected" in changed and bool(changed["Connected"]):
                self._connected_at[sender_path] = time.monotonic()
            if "ServicesResolved" in changed and bool(changed["ServicesResolved"]):
                self._on_services_resolved(sender_path, addr)

            if "Connected" in changed and not bool(changed["Connected"]):
                self._forget_link(sender_path)
                self._on_disconnected(addr, sender_path)

        def on_interfaces_removed(path: str, interfaces: list) -> None:
            # ADD-ON: a device object vanishing (e.g. bluetoothd trouble)
            # sends no Connected=False, so mark the bike offline here.
            if DEVICE_IFACE not in interfaces:
                return
            for addr, bike in self._bikes.items():
                if bike.get("device_path") == str(path) and bike.get("char_path"):
                    log.info("eBike %s removed by BlueZ", addr)
                    self._mark_offline(addr)

        # ADD-ON: bus_name limits these to BlueZ's signals.
        self._bus.add_signal_receiver(
            on_interfaces_added,
            signal_name="InterfacesAdded",
            dbus_interface=OM_IFACE,
            bus_name=BLUEZ_SERVICE,
        )
        self._bus.add_signal_receiver(
            on_interfaces_removed,
            signal_name="InterfacesRemoved",
            dbus_interface=OM_IFACE,
            bus_name=BLUEZ_SERVICE,
        )
        self._bus.add_signal_receiver(
            on_properties_changed,
            signal_name="PropertiesChanged",
            dbus_interface=PROPS_IFACE,
            bus_name=BLUEZ_SERVICE,
            path_keyword="sender_path",
        )

        def on_device_disconnected(*args, sender_path: str = "") -> None:
            # BlueZ (5.82+) says why a device disconnected. Older versions
            # never send this signal; then Connected=False is all we get.
            if sender_path in self._bosch_paths:
                self._capture.event("disconnect_reason", path=sender_path,
                                    reason=[str(a) for a in args])
                log.info("Disconnect reason for %s: %s", sender_path, [str(a) for a in args])

        self._bus.add_signal_receiver(
            on_device_disconnected, signal_name="Disconnected",
            dbus_interface=DEVICE_IFACE, bus_name=BLUEZ_SERVICE, path_keyword="sender_path")

        # Handle any bikes that are already connected at startup
        om = dbus.Interface(self._bus.get_object(BLUEZ_SERVICE, "/"), OM_IFACE)
        objects = om.GetManagedObjects()
        adapter = explorer_plain(objects.get(self._adapter_path, {}).get(ADAPTER_IFACE, {}))
        self._capture.report["adapter"] = adapter
        self._capture.event("adapter", path=self._adapter_path, properties=adapter)
        log.info("Adapter %s: %s", self._adapter_path, adapter)
        for path, ifaces in objects.items():
            if DEVICE_IFACE not in ifaces:
                continue
            props = ifaces[DEVICE_IFACE]
            addr  = str(props.get("Address", "")).upper()
            uuids = [str(u).lower() for u in props.get("UUIDs", [])]
            # Remember bikes bonded before this start (BlueZ keeps their GATT
            # service UUIDs), so stale-bond recovery still works.
            if ble_policy.advertises_ldi(uuids) or mcsp.SERVICE_UUID in uuids:
                self._bosch_paths.add(str(path))
                if bool(props.get("Paired", False)):
                    if addr not in self._known_bikes:
                        log.info("Known bonded eBike: %s", addr)
                    self._known_bikes.add(addr)
            is_bike, payload = ble_policy.bosch_bike_advert(props)
            if is_bike:
                # Cached by BlueZ: log it, but only connect on a live advert.
                self._on_bosch_advert(str(path), payload, props, live=False)
            if bool(props.get("ServicesResolved", False)):
                self._on_services_resolved(str(path), addr)

        # ADD-ON: if bluetoothd restarts, our agent and advertisement are gone.
        self._bus.watch_name_owner(BLUEZ_SERVICE, self._on_bluez_owner_changed)

    # ── ADD-ON: recovery on a shared adapter ──────────────────────────────────

    def _on_adapter_changed(self, changed: dict) -> None:
        """Re-advertise when the adapter comes back after a power cycle.

        Home Assistant's Bluetooth integration can reset the shared adapter
        when it thinks it is stuck. That silently drops our advertisement.
        """
        if "Powered" in changed and bool(changed["Powered"]):
            log.warning("Bluetooth adapter powered back on — re-advertising")
            if self._window_open():
                self._set_pairable(True)
            self._readvertise()

    def _on_bluez_owner_changed(self, owner: str) -> None:
        """bluetoothd (re)started: register the agent and advert again."""
        previous = self._bluez_owner
        self._bluez_owner = owner
        if previous is None:
            return  # first callback just reports the current owner
        # Any connection BlueZ had is gone, whether it died or restarted.
        for addr, bike in self._bikes.items():
            if bike.get("char_path"):
                self._mark_offline(addr)
        if not owner:
            log.warning("bluetoothd went away — waiting for it to return")
            return
        log.warning("bluetoothd (re)started — re-registering agent and advert")
        try:
            self._register_agent()
            if self._window_open():
                self._set_pairable(True)
            self._readvertise()
        except dbus.DBusException as exc:
            log.error("Re-registration after bluetoothd restart failed: %s", exc)

    def _advertising_watchdog(self) -> bool:
        """Periodically re-register the advert while a bike is missing."""
        if not self._all_known_bikes_connected():
            log.debug("Advertising watchdog: re-registering advertisement")
            self._readvertise()
        return True  # keep the timer running

    # ── Entry point ───────────────────────────────────────────────────────────

    def _write_run_header(self) -> None:
        """Everything needed to make sense of this run's capture later."""
        options = {k: v for k, v in self._cfg.items() if k != "mqtt"}
        options["mqtt"] = {k: v for k, v in self._cfg.get("mqtt", {}).items()
                           if k not in ("password", "username")}
        table = Path(datapoints.TABLE_PATH).read_bytes()
        header = {
            "addon_version": self._cfg.get("version"),
            "options": options,
            "host": platform.uname()._asdict(),
            "python": sys.version.split()[0],
            "datapoint_table_sha256": hashlib.sha256(table).hexdigest()[:16],
            "datapoints": len(self._table),
            "readable_datapoints": sum(dp.readable for dp in self._table.values()),
            "adapter_path": self._adapter_path,
            "explorer_state": self._explorer_state,
            "capture_dir": self._capture.run_dir,
            "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._capture.report["run"] = header
        self._capture.event("run_start", **header)
        log.info("Run: add-on %s, capture in %s", header["addon_version"],
                 header["capture_dir"])

    def run(self) -> None:
        adapter_path = self._get_adapter_path()
        self._adapter_path = adapter_path
        self._setup_adapter(adapter_path)
        self._register_agent()
        self._write_run_header()

        # Pairing is opened with the "Start pairing" button. Opening it on
        # every start would let any nearby device pair for 5 minutes after
        # each restart, so that is opt-in.
        if self._open_pairing_on_start:
            self.start_pairing()
        else:
            # BlueZ switches the adapter to pairable once a default agent is
            # registered; outside the pairing window put it back as found.
            self._set_pairable(self._pairable_initial)
            self._publish_pairing_state()
            self._readvertise()

        # Publish discovery for every configured bike up front, so their HA
        # entities (incl. the corrected 'connected' sensor) register even while
        # the bike is offline. Mark them offline until they actually connect.
        for addr, bike in self._bikes.items():
            self._mqtt.publish_discovery(addr, bike["name"])
            self._mqtt.publish_availability(addr, False)

        # Pairing controls (button + status) for Home Assistant. The button
        # press arrives on the MQTT thread, so marshal start_pairing() onto the
        # GLib main loop (all D-Bus/adv work must happen on that thread).
        self._mqtt.publish_pairing_discovery(
            lambda: GLib.idle_add(self._start_pairing_idle)
        )
        self._mqtt.publish_forget_discovery(
            lambda: GLib.idle_add(self._forget_bikes_idle)
        )
        self._mqtt.publish_sweep_discovery(lambda: GLib.idle_add(self._sweep_idle))
        # Finding and adding a bike from Home Assistant.
        self._mqtt.add_select("nearby", "Nearby Bosch eBikes", "mdi:bicycle-electric",
                              self._nearby.options(),
                              lambda option: GLib.idle_add(self._on_nearby_selected, option))
        self._mqtt.add_button("pair_selected", "Pair selected bike", "mdi:link-variant",
                              lambda: GLib.idle_add(self._pair_selected))
        self._mqtt.add_button("mcsp_window", "Connect like the Flow app", "mdi:cellphone-link",
                              lambda: GLib.idle_add(self.start_window))
        GLib.timeout_add_seconds(WINDOW_TICK_SEC, self._window_tick)
        if self._capture.run_dir:
            webui.start(str(Path(self._capture.run_dir).parent))
        # Firmware: first check a minute after start, then daily.
        GLib.timeout_add_seconds(FIRMWARE_FIRST_CHECK_SEC,
                                 lambda: self._firmware_tick() and False)
        GLib.timeout_add_seconds(24 * 3600, self._firmware_tick)
        self._publish_pairing_state()
        # Report every minute (plus at disconnects and shutdown): enough detail
        # without rewriting a large file on the SD card every few seconds.
        GLib.timeout_add_seconds(60, self._capture.write_report)

        log.info(supervisor.check_core_api())
        self._monitor()
        # After _monitor(): it adds bikes bonded before this start.
        self._refresh_setup()
        for addr in self._bikes:
            self._refresh_bike(addr)
        GLib.timeout_add_seconds(ADVERTISING_WATCHDOG_SEC, self._advertising_watchdog)
        GLib.timeout_add_seconds(SCAN_EVERY_SEC, self._scan_tick)
        GLib.timeout_add_seconds(2, lambda: self._scan_tick() and False)   # first scan now

        def _shutdown(signum, frame) -> None:
            log.info("Caught signal %d – shutting down", signum)
            self._set_pairable(self._pairable_initial)  # ADD-ON
            for addr in self._bikes:
                self._mqtt.publish_availability(addr, False)
                self._end_session(addr, "add-on stopped")
            self._capture.event("run_stop", signal=signum)
            self._capture.close()
            self._mqtt.stop()
            self._loop.quit()

        signal.signal(signal.SIGINT,  _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        log.info(
            "Bridge running – advertising as '%s', waiting for eBike connections",
            self._cfg.get("bridge_name", "HA eBike Bridge"),
        )
        self._loop.run()


# ── CLI entry point ───────────────────────────────────────────────────────────

MQTT_RETRY_SEC = float(os.environ.get("BOSCH_MQTT_RETRY_SEC", 30))     # env: tests


def wait_for_config(options_path: str) -> dict:
    """Build the config, waiting (with a notification) for an MQTT broker.

    The broker's details come from the add-on options if set, otherwise from
    the Supervisor, where Mosquitto registers them. Mosquitto drops and
    re-registers them on every restart, so "not registered" is often brief:
    keep checking instead of exiting, and tell the user in Home Assistant.
    """
    notified, tries = False, 0
    while True:
        options = load_options(options_path)
        env, reason = dict(os.environ), "set mqtt_host in the options"
        if not options.get("mqtt_host"):
            service, reason = supervisor.mqtt_service()
            if service:
                env.update(MQTT_HOST=str(service["host"]),
                           MQTT_PORT=str(service.get("port") or 1883),
                           MQTT_USERNAME=str(service.get("username") or ""),
                           MQTT_PASSWORD=str(service.get("password") or ""))
        try:
            config = build_config(options, env)
        except MqttMissing:
            if tries % 10 == 0:
                log.warning("No MQTT broker registered yet (%s). Waiting; see the "
                            "notification in Home Assistant.", reason)
            if not notified:
                notified = supervisor.notify(
                    "mqtt", "Bosch eBike: waiting for MQTT broker",
                    supervisor.MQTT_MISSING.format(reason=reason))
            tries += 1
            time.sleep(MQTT_RETRY_SEC)
            continue
        if notified:
            supervisor.dismiss("mqtt")
        log.info("MQTT broker: %s:%s", config["mqtt"]["broker"], config["mqtt"]["port"])
        return config


def main() -> None:
    # Configuration comes from the add-on options (/data/options.json) and the
    # MQTT broker registered with the Supervisor.
    options_path = sys.argv[1] if len(sys.argv) > 1 else OPTIONS_PATH
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        config = wait_for_config(options_path)
    except ConfigError as exc:
        log.error("%s", exc)
        supervisor.notify("config", "Bosch eBike: check the configuration", str(exc))
        sys.exit(1)
    logging.getLogger().setLevel(getattr(logging, config["log_level"].upper(), logging.INFO))

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    try:
        EbikeBridge(config).run()
    except (dbus.DBusException, RuntimeError) as exc:
        log.error(
            "Cannot talk to Bluetooth (BlueZ over D-Bus): %s. The add-on needs host "
            "D-Bus access (host_dbus) and a Bluetooth adapter on the host.", exc,
        )
        supervisor.notify("bluetooth", "Bosch eBike: Bluetooth not reachable",
                          supervisor.BLUETOOTH_MISSING.format(reason=exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
