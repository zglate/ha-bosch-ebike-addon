"""
Bike explorer: learn everything a connected bike exposes, read-only.

Started once BlueZ has resolved a bike's GATT services:
  1. Device dump: every Device1 property BlueZ knows (address type, bond
     state, UUIDs, manufacturer data ...).
  2. GATT inventory: every service, characteristic and descriptor; read each
     readable one; listen on every notifying characteristic.
  3. If the bike exposes MCSP (the Flow app's channel), a McspSession:
     handshake, then read and subscribe the datapoints the Flow app itself
     uses, then sweep every other readable datapoint, gently paced.

Nothing is ever written to the bike except MCSP read/subscribe requests,
"unsupported" replies and transport setup (see mcsp.check_outgoing), plus the
standard notification switch-on (StartNotify) for listening.

Everything goes to the debug capture. Decoded values reach the bridge through
callbacks so it can publish them to Home Assistant.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque

import dbus
from gi.repository import GLib

import mcsp
from datapoints import (CURATED_READS, CURATED_SUBSCRIBES, DRIVE_UNIT_SOFTWARE,
                        KEY_EXCHANGE_PENDING, READ_ON_CONNECT, READ_SOURCE, STARTUP_STAGE,
                        SUBSCRIBE_ON_CONNECT, SUBSCRIBE_SOURCE, Datapoint, decode_value)

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"
SERVICE_IFACE = "org.bluez.GattService1"
CHAR_IFACE = "org.bluez.GattCharacteristic1"
DESC_IFACE = "org.bluez.GattDescriptor1"
LDI_CHAR_UUID = "0000eb21-eaa2-11e9-81b4-2a2ae2dbcce4"     # handled by the bridge

# Friendly names for standard characteristics worth reading.
STANDARD_NAMES = {
    "00002a00": "Device Name", "00002a19": "Battery Level", "00002a24": "Model Number",
    "00002a25": "Serial Number", "00002a26": "Firmware Revision",
    "00002a27": "Hardware Revision", "00002a28": "Software Revision",
    "00002a29": "Manufacturer Name",
}


def gatt_objects(bus, device_path: str) -> dict:
    """BlueZ GATT objects below one device, as plain Python values."""
    om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IFACE)
    prefix = str(device_path) + "/"
    return {str(path): plain(ifaces) for path, ifaces in om.GetManagedObjects().items()
            if str(path).startswith(prefix)}


def inventory(objects: dict) -> list[dict]:
    """Characteristics of a device, with their service and descriptors.

    `objects` maps object path -> {interface: {property: value}} (plain values).
    """
    services = {p: i[SERVICE_IFACE].get("UUID") for p, i in objects.items()
                if SERVICE_IFACE in i}
    descriptors: dict = {}
    for p, i in sorted(objects.items()):
        if DESC_IFACE in i:
            descriptors.setdefault(p.rsplit("/", 1)[0], []).append({
                "path": p, "uuid": str(i[DESC_IFACE].get("UUID", "")).lower(),
                "flags": list(i[DESC_IFACE].get("Flags", []))})
    chars = []
    for p, i in sorted(objects.items()):
        if CHAR_IFACE not in i:
            continue
        props = i[CHAR_IFACE]
        uuid = str(props.get("UUID", "")).lower()
        chars.append({
            "path": p,
            "uuid": uuid,
            "name": STANDARD_NAMES.get(uuid[:8], ""),
            "service": services.get(p.rsplit("/", 1)[0]),
            "flags": list(props.get("Flags", [])),
            "mtu": props.get("MTU"),
            "descriptors": descriptors.get(p, []),
        })
    return chars


def find_char(chars: list[dict], uuid: str) -> dict | None:
    return next((c for c in chars if c["uuid"] == uuid), None)


def plain(value):
    """dbus-python types -> plain Python, recursively (bytes stay bytes)."""
    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, dbus.Array) and value.signature == "y":
        return bytes(value)
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k) if not isinstance(k, int) else int(k): plain(v)
                for k, v in value.items()}
    return str(value)


def _text(raw: bytes) -> str:
    """Printable text form of a value, or '' if it isn't text."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return text if text and text.isprintable() else ""




class Hooks:
    """What an explorer needs from the bridge. Defaults do nothing."""

    def on_value(self, dp_id: int, value) -> None: ...
    def on_status(self, status: dict) -> None: ...
    def should_sweep(self, firmware: str | None) -> bool: return True
    def on_sweep_done(self, firmware: str | None) -> None: ...
    def is_moving(self) -> bool: return False
    def on_release(self) -> None: ...
    def on_silent(self) -> None: ...


class BikeExplorer:
    """Device dump, GATT inventory and optional MCSP session for one bike."""

    def __init__(self, bus, address: str, device_path: str, table: dict, capture,
                 hooks: Hooks, use_mcsp: bool = True, gatt_listen: bool = True,
                 mcsp_off_reason: str = "MCSP disabled in options") -> None:
        self._bus, self.address, self._path = bus, address, device_path
        self._table, self._capture, self._hooks = table, capture, hooks
        self._use_mcsp, self._gatt_listen = use_mcsp, gatt_listen
        self._mcsp_off_reason = mcsp_off_reason
        self._matches: list = []
        self.mcsp: McspSession | None = None

    def start(self) -> None:
        self._dump_device()
        chars = inventory(gatt_objects(self._bus, self._path))
        report = self._capture.bike(self.address)
        report["gatt"] = {c["uuid"]: {k: v for k, v in c.items() if k != "path"} for c in chars}
        self._capture.event("gatt_inventory", bike=self.address, characteristics=chars)
        log.info("[%s] GATT: %d characteristics in services %s", self.address, len(chars),
                 ", ".join(sorted({c["service"] or "?" for c in chars})))
        for c in chars:
            log.info("[%s]   %s %s %s", self.address, c["uuid"], c["name"], c["flags"])
            if "read" in c["flags"]:
                self._read(c["path"], c["uuid"], CHAR_IFACE, c["name"])
            for d in c["descriptors"]:
                if "read" in d["flags"]:
                    self._read(d["path"], f"{c['uuid']}/{d['uuid']}", DESC_IFACE, "")
            if self._gatt_listen and c["uuid"] not in (mcsp.RX_UUID, LDI_CHAR_UUID) and (
                    "notify" in c["flags"] or "indicate" in c["flags"]):
                self._listen(c)

        rx, tx = find_char(chars, mcsp.RX_UUID), find_char(chars, mcsp.TX_UUID)
        if not (rx and tx):
            self._status_only("MCSP not exposed on this connection")
        elif not self._use_mcsp:
            self._status_only(self._mcsp_off_reason)
        else:
            self.mcsp = McspSession(self._bus, self.address, rx, tx, self._table,
                                    self._capture, self._hooks)
            self.mcsp.start()

    def stop(self, reason: str = "stopped") -> None:
        for match in self._matches:
            match.remove()
        self._matches.clear()
        if self.mcsp:
            self.mcsp.stop(reason)

    def _dump_device(self) -> None:
        try:
            props = plain(dbus.Interface(self._bus.get_object(BLUEZ, self._path),
                                         PROPS_IFACE).GetAll(DEVICE_IFACE))
        except dbus.DBusException as exc:
            props = {"error": str(exc)}
        self._capture.bike(self.address)["device"] = props
        self._capture.event("device", bike=self.address, properties=props)
        log.info("[%s] Device: %s", self.address, {k: v for k, v in props.items()
                                                   if k not in ("ManufacturerData", "ServiceData")})

    def _status_only(self, state: str) -> None:
        log.info("[%s] %s", self.address, state)
        self._capture.bike(self.address)["mcsp"] = {"state": state}
        try:
            self._hooks.on_status({"state": state})
        except Exception as exc:
            log.error("[%s] on_status failed: %s", self.address, exc)

    def _read(self, path: str, key: str, iface: str, name: str) -> None:
        gatt = self._capture.bike(self.address).setdefault("gatt_values", {})

        def ok(value):
            raw = bytes(value)
            gatt[key] = {"hex": raw.hex(), "text": _text(raw), "name": name}
            self._capture.event("gatt_read", bike=self.address, uuid=key, value=raw,
                                text=_text(raw))
            log.info("[%s] read %s %s = %s", self.address, key, name, _text(raw) or raw.hex())

        def failed(exc):
            gatt[key] = {"error": exc.get_dbus_name(), "name": name}
            self._capture.event("gatt_read_error", bike=self.address, uuid=key, error=str(exc))
            log.info("[%s] read %s failed: %s", self.address, key, exc.get_dbus_name())

        dbus.Interface(self._bus.get_object(BLUEZ, path), iface).ReadValue(
            {}, reply_handler=ok, error_handler=failed)

    def _listen(self, c: dict) -> None:
        """Switch on notifications of a characteristic and record every value."""
        def changed(iface, props, invalidated):
            if iface == CHAR_IFACE and "Value" in props:
                raw = bytes(props["Value"])
                self._capture.event("gatt_notify", bike=self.address, uuid=c["uuid"],
                                    value=raw)

        self._matches.append(self._bus.add_signal_receiver(
            changed, signal_name="PropertiesChanged", dbus_interface=PROPS_IFACE,
            bus_name=BLUEZ, path=c["path"]))
        dbus.Interface(self._bus.get_object(BLUEZ, c["path"]), CHAR_IFACE).StartNotify(
            reply_handler=lambda: log.info("[%s] listening on %s", self.address, c["uuid"]),
            error_handler=lambda e: self._capture.event(
                "gatt_notify_error", bike=self.address, uuid=c["uuid"], error=str(e)))


def _already_notifying(exc) -> bool:
    """BlueZ answers StartNotify on an already-notifying characteristic (e.g.
    after a reconnect) with InProgress or NotPermitted 'Already notifying'."""
    name = exc.get_dbus_name()
    return name == "org.bluez.Error.InProgress" or (
        name == "org.bluez.Error.NotPermitted" and "lready" in str(exc))


class McspSession:
    """A read-only MCSP session over one bike connection.

    Order: switch on receive -> transport handshake (answering the bike's, or
    our own if it stays silent) -> read + subscribe the start-up set ->
    (if the bike answered NOT_READY, retry that set; if nothing succeeded from
    one source address, retry from the other) -> sweep every other readable
    datapoint once per firmware version, paused while the bike is moving ->
    release the bike a few minutes after it goes quiet.

    Every frame passes mcsp.check_outgoing, and nothing is ever sent to a
    datapoint the table doesn't mark readable (commands are excluded).
    """

    HANDSHAKE_WAIT_MS = 1500      # wait for the bike to start the handshake
    SETTLE_MS = 500               # pause between handshake and first requests
    REQUEST_TIMEOUT_MS = int(os.environ.get("BOSCH_MCSP_TIMEOUT_MS", "4000"))   # env: tests
    # Give up when this many requests in a row time out and the bike has never
    # sent a single byte on this connection: it isn't talking MCSP here.
    SILENT_AFTER_TIMEOUTS = 20
    MAX_IN_FLIGHT = 4
    # Minimum spacing between requests. Overridable only so tests run fast.
    SEND_GAP_MS = int(os.environ.get("BOSCH_MCSP_SEND_GAP_MS", "25"))
    OVERLOAD_BACKOFF_MS = 2000
    NOT_READY_RETRY_MS = 10_000
    NOT_READY_RETRIES = 3
    MOVING_CHECK_MS = 5000        # how often a paused sweep checks again
    REFRESH_MS = int(os.environ.get("BOSCH_MCSP_REFRESH_MS", str(10 * 60 * 1000)))  # env: tests
    RELEASE_AFTER_MS = int(os.environ.get("BOSCH_MCSP_RELEASE_MS", str(5 * 60 * 1000)))
    STATUS_EVERY_MS = 1000
    MIN_GOOD_MTU = 188            # spec minimum ATT MTU 185 + 3

    def __init__(self, bus, address: str, rx: dict, tx: dict, table: dict, capture,
                 hooks: Hooks) -> None:
        self._bus, self.address = bus, address
        self._rx_path, self._tx_path = rx["path"], tx["path"]
        self._table, self._capture, self._hooks = table, capture, hooks
        self._deframer, self._seq = mcsp.Deframer(), mcsp.Sequence()
        self._queue: deque = deque()
        self._queued: set = set()         # (op, datapoint, body) waiting or in flight
        self._pending: dict = {}          # (datapoint, seq) -> request dict
        self._outstanding: dict = {}      # phase -> requests not yet finished
        self._timers: set = set()
        self._pump_timer = self._release_timer = None
        self._match = None
        self._handshake_sent = self._requests_started = self._swept = False
        self.silent = False
        self._silent_timeouts = 0
        self._status_scheduled = False
        self._not_ready_rounds = 0
        self._max_in_flight = self.MAX_IN_FLIGHT
        self._read_source = READ_SOURCE
        self.answered: set[int] = set()   # datapoints that returned a value
        self.firmware: str | None = None
        mtu = tx.get("mtu") or rx.get("mtu")
        self.status = {
            "state": "starting", "mtu": mtu, "peer_version": None, "peer_max_packet": None,
            "packet_size": min(mtu - 3, mcsp.DEFAULT_PACKET) if mtu else mcsp.DEFAULT_PACKET,
            "handshake": None, "requests_sent": 0, "values": 0, "errors": {},
            "timeouts": 0, "notifications": 0, "bike_requests": 0, "overloaded": 0,
            "skipped_not_readable": 0, "tx_blocked": 0, "dropped_messages": 0,
            "ok_by_source": {}, "startup_stages": [], "key_exchange_pending": None,
            "sweep_total": 0, "sweep_done": 0, "milestones": {},
        }
        self._milestone("session_created")
        if mtu and mtu < self.MIN_GOOD_MTU:
            log.warning("[%s] MTU %s is below %d; large MCSP messages may fail",
                        self.address, mtu, self.MIN_GOOD_MTU)
            self._capture.event("mtu_warning", bike=self.address, mtu=mtu)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._match = self._bus.add_signal_receiver(
            self._on_properties, signal_name="PropertiesChanged",
            dbus_interface=PROPS_IFACE, bus_name=BLUEZ, path=self._rx_path)

        def failed(exc):
            if _already_notifying(exc):
                self._notifying()
            else:
                self._set_state(f"StartNotify failed: {exc.get_dbus_name()}")
        dbus.Interface(self._bus.get_object(BLUEZ, self._rx_path), CHAR_IFACE).StartNotify(
            reply_handler=self._notifying, error_handler=failed)

    def stop(self, reason: str = "stopped") -> None:
        partial = self._deframer.partial_bytes()
        if partial:
            self._capture.event("rx_partial_discarded", bike=self.address, bytes=partial)
        for timer in list(self._timers):
            GLib.source_remove(timer)
        self._timers.clear()
        self._pump_timer = self._release_timer = None
        if self._match is not None:
            self._match.remove()
            self._match = None
        self._deframer.reset()
        self._milestone(reason)
        self._set_state(reason)

    def _after(self, ms: int, fn, *args) -> int:
        def run():
            self._timers.discard(timer)
            fn(*args)
            return False
        timer = GLib.timeout_add(ms, run)
        self._timers.add(timer)
        return timer

    def _cancel(self, timer) -> None:
        if timer in self._timers:
            GLib.source_remove(timer)
            self._timers.discard(timer)

    def _milestone(self, name: str) -> None:
        stamp = time.time()
        self.status["milestones"].setdefault(name, round(stamp, 3))
        self._capture.event("milestone", bike=self.address, name=name)

    def _notifying(self) -> None:
        self._milestone("notify_on")
        self._set_state("listening")
        self._after(self.HANDSHAKE_WAIT_MS, self._proactive_handshake)

    def _proactive_handshake(self) -> None:
        if not self._handshake_sent:
            self.status["handshake"] = "proactive (bike sent none)"
            self._send_handshake()

    def _send_handshake(self) -> None:
        self._handshake_sent = True
        frames = mcsp.handshake(self.status["packet_size"])
        for f in frames:
            mcsp.check_outgoing(mcsp.COMMAND_CHANNEL, f[2:])      # read-only, always
        data = b"".join(frames)
        if len(data) <= self.status["packet_size"]:
            self._write(data)
        else:
            for f in frames:
                self._write(f)
        self._milestone("handshake_sent")
        self._set_state("handshake sent")
        self._after(self.SETTLE_MS, self._start_requests)

    def _start_requests(self) -> None:
        if self._requests_started:
            return
        self._requests_started = True
        self._set_state("reading start-up set")
        self._enqueue_startup_set("connect")
        self._after(self.REFRESH_MS, self._refresh)

    def _enqueue_startup_set(self, phase: str) -> None:
        for dp in READ_ON_CONNECT:
            self._enqueue(mcsp.READ, dp, self._read_source, phase)
        for dp in SUBSCRIBE_ON_CONNECT:
            self._enqueue(mcsp.SUBSCRIBE, dp, SUBSCRIBE_SOURCE, phase)

    def _refresh(self) -> None:
        for dp in CURATED_READS:
            self._enqueue(mcsp.READ, dp, self._read_source, "refresh")
        self._after(self.REFRESH_MS, self._refresh)

    def sweep(self, forced: bool = False) -> int:
        """Read every readable datapoint that hasn't returned a value yet."""
        if self.silent:
            log.warning("[%s] Not sweeping: the bike isn't answering MCSP on this connection",
                        self.address)
            return 0
        ids = [i for i, dp in sorted(self._table.items())
               if dp.readable and i not in self.answered
               and (mcsp.READ, i, b"") not in self._queued]
        self.status["sweep_total"] += len(ids)
        self._milestone("sweep_started")
        for dp in ids:
            self._enqueue(mcsp.READ, dp, self._read_source, "sweep")
        self._set_state(f"sweeping {len(ids)} datapoints" + (" (button)" if forced else ""))
        return len(ids)

    def _phase_done(self, phase: str) -> None:
        if phase == "sweep":
            self._milestone("sweep_done")
            self._call_hook("on_sweep_done", self.firmware)
            self._set_state("idle (sweep complete)")
            self._schedule_release()
            return
        if phase not in ("connect", "not_ready_retry", "source_retry"):
            return
        errors = self.status["errors"]
        ok = self.status["ok_by_source"]
        # 1. The bike may still be starting up: retry the start-up set.
        if (errors.get("NOT_READY", 0) and not ok
                and self._not_ready_rounds < self.NOT_READY_RETRIES):
            self._not_ready_rounds += 1
            log.warning("[%s] Bike answered NOT_READY; retrying start-up set (%d/%d)",
                        self.address, self._not_ready_rounds, self.NOT_READY_RETRIES)
            self._set_state(f"bike not ready, retry {self._not_ready_rounds} in 10 s")
            self._after(self.NOT_READY_RETRY_MS, self._enqueue_startup_set, "not_ready_retry")
            return
        # 2. Nothing worked from one source address: try the other once.
        if phase != "source_retry":
            retried = False
            if not any(k.startswith("READ") for k in ok):
                log.warning("[%s] No READ succeeded from 0x%04X; retrying from 0x%04X",
                            self.address, self._read_source, SUBSCRIBE_SOURCE)
                for dp in CURATED_READS:
                    self._enqueue(mcsp.READ, dp, SUBSCRIBE_SOURCE, "source_retry")
                retried = True
            if not any(k.startswith("SUBSCRIBE") for k in ok):
                log.warning("[%s] No SUBSCRIBE succeeded from 0x%04X; retrying from 0x%04X",
                            self.address, SUBSCRIBE_SOURCE, READ_SOURCE)
                for dp in CURATED_SUBSCRIBES:
                    self._enqueue(mcsp.SUBSCRIBE, dp, READ_SOURCE, "source_retry")
                retried = True
            if retried:
                return
        # 3. Sweep the rest, once per firmware, only if reads work at all.
        if f"READ from 0x{SUBSCRIBE_SOURCE:04X}" in ok and not any(
                k == f"READ from 0x{READ_SOURCE:04X}" for k in ok):
            self._read_source = SUBSCRIBE_SOURCE          # use the address that worked
        if not any(k.startswith("READ") for k in ok):
            self._set_state("idle (no read succeeded, sweep skipped)")
            self._schedule_release()
        elif self._swept or not self._call_hook("should_sweep", self.firmware,
                                                 default=True):
            self._set_state("idle (sweep already done for this firmware)")
            self._schedule_release()
        else:
            self._swept = True
            self.sweep()

    def _schedule_release(self) -> None:
        """Let go of the bike a while after exploring, so the Flow app can connect."""
        if self._release_timer is None:
            self._release_timer = self._after(self.RELEASE_AFTER_MS, self._release)

    def _release(self) -> None:
        self._release_timer = None
        if self._pending or self._queue:
            self._schedule_release()
            return
        self._milestone("released")
        self._call_hook("on_release")

    # ── Sending ──────────────────────────────────────────────────────────────

    def _enqueue(self, op: int, dp: int, src: int, phase: str, tries: int = 0,
                 front: bool = False, body: bytes = b"") -> None:
        if self.silent:
            return
        entry = self._table.get(dp)
        allowed_call = op == mcsp.RPC and dp in mcsp.SAFE_CALLS
        if not allowed_call and (entry is None or not entry.readable):
            self.status["skipped_not_readable"] += 1
            self._capture.event("request_skipped", bike=self.address, id=f"0x{dp:04X}",
                                op=mcsp.OP_NAMES[op], reason="not marked readable")
            return
        if (op, dp, body) in self._queued and tries == 0:
            return
        self._queued.add((op, dp, body))
        item = {"op": op, "dp": dp, "src": src, "phase": phase, "tries": tries, "body": body}
        (self._queue.appendleft if front else self._queue.append)(item)
        self._outstanding[phase] = self._outstanding.get(phase, 0) + 1
        if self._release_timer is not None:          # new work: postpone the release
            self._cancel(self._release_timer)
            self._release_timer = None
        self._pump()

    def _pump(self) -> None:
        """Send the next queued request, keeping requests spaced and bounded."""
        if self._pump_timer is not None:
            return
        if not self._queue or len(self._pending) >= self._max_in_flight:
            return
        if self._queue[0]["phase"] == "sweep" and self._hooks.is_moving():
            if self.status["state"] != "sweep paused (bike moving)":
                self._set_state("sweep paused (bike moving)")
            self._pump_timer = self._after(self.MOVING_CHECK_MS, self._unpause)
            return
        item = self._queue.popleft()
        seq = self._seq.next()
        key = (item["dp"], seq)
        item["sent"] = time.monotonic()
        item["timer"] = self._after(self.REQUEST_TIMEOUT_MS, self._timed_out, key)
        self._pending[key] = item
        self.status["requests_sent"] += 1
        self._send_frames(mcsp.MESSAGEBUS_CHANNEL,
                          mcsp.request(item["op"], item["dp"], seq, item.get("body", b""),
                                       src=item["src"]))
        self._pump_timer = self._after(self.SEND_GAP_MS, self._unpause)

    def _unpause(self) -> None:
        self._pump_timer = None
        if self.status["state"] == "sweep paused (bike moving)" and not self._hooks.is_moving():
            self._set_state("sweeping (resumed)")
        self._pump()

    def _send_frames(self, channel: int, payload: bytes) -> None:
        try:
            mcsp.check_outgoing(channel, payload)          # read-only, always
        except mcsp.UnsafeFrame as exc:
            self.status["tx_blocked"] += 1
            self._capture.event("tx_blocked", bike=self.address, channel=channel,
                                payload=payload, reason=str(exc))
            log.error("[%s] Blocked an unsafe frame: %s", self.address, exc)
            return
        for f in mcsp.frame(channel, payload, self.status["packet_size"]):
            self._write(f)

    def _write(self, data: bytes) -> None:
        self._capture.event("tx", bike=self.address, data=data)
        dbus.Interface(self._bus.get_object(BLUEZ, self._tx_path), CHAR_IFACE).WriteValue(
            dbus.Array(data, signature="y"), {"type": "command"},
            reply_handler=lambda: None,
            error_handler=lambda e: self._capture.event(
                "tx_error", bike=self.address, data=data, error=str(e)))

    def _finish(self, item: dict) -> None:
        self._queued.discard((item["op"], item["dp"], item.get("body", b"")))
        phase = item["phase"]
        if phase == "sweep":
            self.status["sweep_done"] += 1
        self._outstanding[phase] -= 1
        if self._outstanding[phase] == 0:
            self._phase_done(phase)

    def _timed_out(self, key) -> None:
        item = self._pending.pop(key, None)
        if item is None:
            return
        self.status["timeouts"] += 1
        self._record(item["dp"], self._source(item), status=None)
        if "first_rx" not in self.status["milestones"]:
            self._silent_timeouts += 1
            if self._silent_timeouts >= self.SILENT_AFTER_TIMEOUTS:
                self._go_silent()
                return
        self._finish(item)
        self._pump()

    def _go_silent(self) -> None:
        """Nothing at all has come back: stop asking. Not a completed sweep."""
        self.silent = True
        for timer in list(self._timers):         # refresh, pump, request timeouts ...
            GLib.source_remove(timer)
        self._timers.clear()
        self._release_timer = None
        self._pending.clear()
        self._queue.clear()
        self._queued.clear()
        self._outstanding.clear()
        self._cancel(self._pump_timer)
        self._pump_timer = None
        log.warning("[%s] MCSP: %d requests in a row got no answer and the bike has sent "
                    "nothing on this connection; stopping", self.address, self._silent_timeouts)
        self._milestone("silent")
        self._set_state("bike not answering MCSP on this connection")
        self._call_hook("on_silent")

    @staticmethod
    def _source(item: dict) -> str:
        return f"{mcsp.OP_NAMES[item['op']]} from 0x{item['src']:04X}"

    # ── Receiving ────────────────────────────────────────────────────────────

    def _on_properties(self, iface, changed, invalidated) -> None:
        if iface != CHAR_IFACE or "Value" not in changed:
            return
        data = bytes(changed["Value"])
        if "first_rx" not in self.status["milestones"]:
            self._milestone("first_rx")
        self._capture.event("rx", bike=self.address, data=data)
        before = self._deframer.dropped_messages
        for channel, payload in self._deframer.feed(data):
            if channel == mcsp.COMMAND_CHANNEL:
                self._on_command(payload)
            elif channel == mcsp.MESSAGEBUS_CHANNEL:
                self._on_message(payload)
            else:
                self._capture.event("rx_channel", bike=self.address, channel=channel,
                                    payload=payload)
        if self._deframer.dropped_messages != before:
            self.status["dropped_messages"] = self._deframer.dropped_messages
            self._capture.event("rx_dropped", bike=self.address,
                                messages=self._deframer.dropped_messages,
                                bytes=self._deframer.dropped_bytes)

    def _on_command(self, payload: bytes) -> None:
        command, body = mcsp.parse_commands(payload)
        self._capture.event("rx_command", bike=self.address, command=command, body=body)
        log.info("[%s] MCSP transport command %d %s", self.address, command, body.hex())
        if command == mcsp.CMD_VERSION and body:
            self.status["peer_version"] = body[0]
            self._milestone("peer_handshake")
            if not self._handshake_sent:
                self.status["handshake"] = "answered the bike's"
                self._send_handshake()
        elif command == mcsp.CMD_MAX_SEGMENTATION_PACKET and len(body) >= 2:
            peer = int.from_bytes(body[:2], "big")
            self.status["peer_max_packet"] = peer
            self.status["packet_size"] = min(self.status["packet_size"], peer)
        self._schedule_status()

    def _on_message(self, payload: bytes) -> None:
        try:
            msg = mcsp.parse_message(payload)
        except ValueError as exc:
            self._capture.event("rx_malformed", bike=self.address, payload=payload,
                                error=str(exc))
            return
        if msg.kind == "notify":
            self.status["notifications"] += 1
            self._record(msg.src, "notify", 0, msg.body)
        elif msg.kind == "response":
            self._on_response(msg)
        else:
            self._on_bike_request(msg)

    def _on_response(self, msg: mcsp.Message) -> None:
        item = self._pending.pop((msg.src, msg.seq), None)
        if item is None:
            self._record(msg.src, f"unmatched {mcsp.OP_NAMES.get(msg.op, msg.op)}",
                         msg.status, msg.body)
            return
        self._cancel(item["timer"])
        latency = round((time.monotonic() - item["sent"]) * 1000)
        if msg.status == 1 and item["tries"] < 2:            # OVERLOADED: back off
            self.status["overloaded"] += 1
            self._max_in_flight = 1
            log.warning("[%s] Bike overloaded; slowing down", self.address)
            self._capture.event("overloaded", bike=self.address, id=f"0x{msg.src:04X}")
            self._outstanding[item["phase"]] -= 1
            self._after(self.OVERLOAD_BACKOFF_MS, self._enqueue, item["op"], item["dp"],
                        item["src"], item["phase"], item["tries"] + 1, True,
                        item.get("body", b""))
            return
        if item["op"] == mcsp.RPC:
            self._on_call_result(item, msg, latency)
            self._finish(item)
            self._pump()
            return
        if msg.status == 0 and item["dp"] == mcsp.REMOTE_DATABASE:
            # The wireless remotes the System Controller knows: ask each for
            # its battery, in the same phase (before this one finishes).
            try:
                ids = mcsp.remote_ids(msg.body)
            except ValueError:
                ids = []
            for remote in ids:
                self._enqueue(mcsp.RPC, mcsp.GET_REMOTE_BATTERY, item["src"], item["phase"],
                              body=mcsp.remote_id_body(remote))
        if msg.status == 0:
            key = self._source(item)
            self.status["ok_by_source"][key] = self.status["ok_by_source"].get(key, 0) + 1
            if "first_success" not in self.status["milestones"]:
                self._milestone("first_success")
        elif msg.status in (3, 6):
            self._milestone("first_not_ready" if msg.status == 3 else "first_denied")
        self._record(msg.src, self._source(item), msg.status, msg.body, latency)
        self._finish(item)
        self._pump()

    def _on_call_result(self, item: dict, msg: mcsp.Message, latency: int) -> None:
        """A wireless remote's battery level (the only call we make)."""
        body = item.get("body", b"")
        remote = 0 if not body else mcsp.remote_ids(b"\x0a" + bytes([len(body)]) + body)[0]
        status_name = mcsp.STATUS_NAMES.get(msg.status, f"status_{msg.status}")
        level = error = None
        if msg.status == 0:
            try:
                level = mcsp.remote_battery_level(msg.body)
            except ValueError as exc:
                error = str(exc)
        else:
            self.status["errors"][status_name] = self.status["errors"].get(status_name, 0) + 1
        self._capture.event("datapoint", bike=self.address, id=f"0x{item['dp']:04X}",
                            label="RemoteControl.bleCentralGetBatteryService",
                            status=status_name, source=self._source(item), remote=remote,
                            raw=msg.body.hex(), latency_ms=latency, value=level,
                            decode_error=error)
        log.info("[%s] Wireless remote %d battery: %s (reply %s)", self.address, remote,
                 f"{level} %" if level is not None else
                 (status_name if msg.status else "no value"), msg.body.hex() or "empty")
        if level is not None:
            self._call_hook("on_value", item["dp"], {"remote": remote, "level": level})

    def _on_bike_request(self, msg: mcsp.Message) -> None:
        """The bike asking the 'phone' for something. We implement nothing,
        but the requests tell us what the bike expects from an app."""
        self.status["bike_requests"] += 1
        op = mcsp.OP_NAMES.get(msg.op, str(msg.op))
        key = f"0x{msg.dst:04X} {op}"
        requests = self._capture.bike(self.address)["bike_requests"]
        requests[key] = requests.get(key, 0) + 1
        self._capture.event("bike_request", bike=self.address, src=f"0x{msg.src:04X}",
                            dst=f"0x{msg.dst:04X}", op=op, seq=msg.seq, body=msg.body)
        log.info("[%s] Bike asked the app: %s %s", self.address, key, msg.body.hex())
        if msg.dst == STARTUP_STAGE and msg.op == mcsp.WRITE:
            try:
                stage = decode_value(self._table.get(STARTUP_STAGE), msg.body)
            except Exception:                      # malformed: record it raw
                stage = msg.body.hex()
            if isinstance(stage, dict):           # a one-field message: {1: stage}
                stage = stage.get(1, stage)
            self.status["startup_stages"].append({"t": round(time.time(), 3),
                                                  "stage": stage})
            self._milestone(f"startup_stage_{stage}")
        # Only answer requests addressed to the phone side (0x40xx).
        if (msg.dst >> 8) == 0x40:
            self._send_frames(mcsp.MESSAGEBUS_CHANNEL, mcsp.unsupported_reply(msg))

    # ── Results ──────────────────────────────────────────────────────────────

    def _record(self, dp_id: int, source: str, status: int | None, body: bytes = b"",
                latency_ms: int | None = None) -> None:
        dp: Datapoint | None = self._table.get(dp_id)
        label = dp.label if dp else f"unknown.0x{dp_id:04X}"
        value = error = None
        if status == 0 and body:
            try:
                value = decode_value(dp, body)
            except Exception as exc:          # keep raw bytes; never crash the session
                error = str(exc)
        elif status == 0 and dp is not None and dp.kind == "bool" and source.startswith("READ"):
            value = False                     # protobuf's false is an empty message
        status_name = ("timeout" if status is None
                       else mcsp.STATUS_NAMES.get(status, f"status_{status}"))
        if status != 0:
            self.status["errors"][status_name] = self.status["errors"].get(status_name, 0) + 1
        result = {"status": status_name, "source": source, "raw": body.hex(),
                  "latency_ms": latency_ms}
        if status == 0 and (body or value is not None):
            result.update(value=value, decode_error=error)
        self._capture.datapoint(self.address, dp_id, label, **result)
        self._capture.event("datapoint", bike=self.address, id=f"0x{dp_id:04X}",
                            label=label, **result)
        if status == 0 and error is None and value is not None:
            if dp_id == DRIVE_UNIT_SOFTWARE:
                self.firmware = str(value)
            if dp_id == KEY_EXCHANGE_PENDING:
                self.status["key_exchange_pending"] = value
            if dp_id not in self.answered:
                self.answered.add(dp_id)
                self.status["values"] = len(self.answered)
                log.info("[%s] %s = %s", self.address, label, value)
            self._call_hook("on_value", dp_id, value)
        elif status not in (0, None):
            log.debug("[%s] %s: %s (%s)", self.address, label, status_name, source)
        self._schedule_status()

    def _set_state(self, state: str) -> None:
        self.status["state"] = state
        log.info("[%s] MCSP: %s", self.address, state)
        self._capture.bike(self.address)["mcsp"] = self.status
        self._call_hook("on_status", dict(self.status))

    def _call_hook(self, name: str, *args, default=None):
        """Publishing to Home Assistant must never break the session: an
        exception here used to skip finishing the request, so the start-up
        phase never completed (first real bike)."""
        try:
            return getattr(self._hooks, name)(*args)
        except Exception as exc:
            log.error("[%s] %s failed: %s", self.address, name, exc)
            self._capture.event("hook_error", bike=self.address, hook=name, error=str(exc))
            return default

    def _schedule_status(self) -> None:
        """Publish status at most once a second (a sweep makes hundreds of changes)."""
        if self._status_scheduled:
            return
        self._status_scheduled = True

        def publish():
            self._status_scheduled = False
            self._capture.bike(self.address)["mcsp"] = self.status
            self._call_hook("on_status", dict(self.status))
        self._after(self.STATUS_EVERY_MS, publish)
