#!/usr/bin/env python3
"""
A small fake BlueZ ("org.bluez") with a fake Bosch bike, for integration tests.

It implements the D-Bus surface the bridge uses: ObjectManager, one adapter
(Adapter1 + LEAdvertisingManager1), AgentManager1, devices with Connect/Pair,
the LDI characteristic, a firmware-revision characteristic, an extra
notifying characteristic, and - when enabled per bike - the MCSP service.

The fake bike's MCSP side is written independently of the add-on's mcsp.py
(byte layouts from the implementation handoff), so the two can't share a bug.

A test-only interface on /test drives it: connect/advertise a bike, send
notifications, ask the agent to pair, and read back everything the bridge did.

Run: DBUS_SYSTEM_BUS_ADDRESS=... python3 fake_bluez.py
"""
import json

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

ADAPTER = "/org/bluez/hci0"
LDI_SERVICE = "0000eb20-eaa2-11e9-81b4-2a2ae2dbcce4"
LDI_CHAR = "0000eb21-eaa2-11e9-81b4-2a2ae2dbcce4"
MCSP_SERVICE = "00000010-eaa2-11e9-81b4-2a2ae2dbcce4"
MCSP_RX = "00000011-eaa2-11e9-81b4-2a2ae2dbcce4"
MCSP_TX = "00000012-eaa2-11e9-81b4-2a2ae2dbcce4"
FIRMWARE_CHAR = "00002a26-0000-1000-8000-00805f9b34fb"
EXTRA_NOTIFY_CHAR = "0000eb40-eaa2-11e9-81b4-2a2ae2dbcce4"
PROPS = "org.freedesktop.DBus.Properties"
OM = "org.freedesktop.DBus.ObjectManager"
GATT_CHAR = "org.bluez.GattCharacteristic1"


def plain(value):
    """Convert a D-Bus value to plain JSON-friendly Python."""
    if isinstance(value, dbus.Array):
        return [plain(v) for v in value]
    if isinstance(value, dbus.Boolean):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    return str(value)


class BluezObject(dbus.service.Object):
    """A BlueZ object: a set of interfaces with properties."""

    def __init__(self, bus, path, interfaces):
        super().__init__(bus, path)
        self.path = path
        self.interfaces = interfaces  # {interface: {property: value}}

    def update(self, iface, **changes):
        """Change properties and signal only the ones that really changed."""
        changed = {k: v for k, v in changes.items()
                   if self.interfaces[iface].get(k) != v}
        self.interfaces[iface].update(changed)
        if changed:
            self.PropertiesChanged(iface, changed, dbus.Array([], signature="s"))

    @dbus.service.method(PROPS, in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.interfaces[iface][prop]

    @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.interfaces.get(iface, {})

    @dbus.service.method(PROPS, in_signature="ssv")
    def Set(self, iface, prop, value):
        STATE["property_sets"].append([str(prop), bool(value)])
        self.update(iface, **{prop: value})

    @dbus.service.signal(PROPS, signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass


class Adapter(BluezObject):
    def __init__(self, bus):
        super().__init__(bus, ADAPTER, {
            "org.bluez.Adapter1": {"Address": "00:11:22:33:44:55", "Powered": False,
                                   "Discoverable": False, "Pairable": False},
            "org.bluez.LEAdvertisingManager1": {},
        })

    @dbus.service.method("org.bluez.LEAdvertisingManager1",
                         in_signature="oa{sv}", sender_keyword="sender",
                         async_callbacks=("reply", "error"))
    def RegisterAdvertisement(self, path, options, reply, error, sender=None):
        # Like real BlueZ, read the advertisement's properties back, and do it
        # asynchronously: bluetoothd never blocks on its clients (a blocking
        # call here deadlocks against the bridge's own synchronous calls).
        def got(props):
            STATE["advertisements"].append({str(k): plain(v) for k, v in props.items()})
            reply()

        adv = dbus.Interface(BUS.get_object(sender, path), PROPS)
        adv.GetAll("org.bluez.LEAdvertisement1", reply_handler=got, error_handler=error)

    @dbus.service.method("org.bluez.LEAdvertisingManager1", in_signature="o")
    def UnregisterAdvertisement(self, path):
        STATE["unregistrations"] += 1

    @dbus.service.method("org.bluez.Adapter1", in_signature="o")
    def RemoveDevice(self, path):
        STATE["removed_devices"].append(str(path))
        ROOT.remove(str(path))


class AgentManager(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, "/org/bluez")
        self.agent = None  # (bus name, object path)

    @dbus.service.method("org.bluez.AgentManager1", in_signature="os",
                         sender_keyword="sender")
    def RegisterAgent(self, path, capability, sender=None):
        self.agent = (sender, str(path))
        STATE["agent_capability"] = str(capability)
        STATE["agent_registrations"] += 1

    @dbus.service.method("org.bluez.AgentManager1", in_signature="o")
    def RequestDefaultAgent(self, path):
        STATE["default_agent"] = str(path)


class Characteristic(BluezObject):
    def __init__(self, bus, path, uuid, flags, read_value=b"", mtu=None):
        props = {"UUID": uuid, "Flags": dbus.Array(flags, signature="s"),
                 "Value": dbus.Array([], signature="y")}
        if mtu:
            props["MTU"] = dbus.UInt16(mtu)
        super().__init__(bus, path, {GATT_CHAR: props})
        self.read_value = read_value
        self.on_write = None
        self.on_notify_start = None

    @dbus.service.method(GATT_CHAR)
    def StartNotify(self):
        STATE["notify_started"].append(self.path)
        if self.on_notify_start:
            GLib.idle_add(self.on_notify_start)

    @dbus.service.method(GATT_CHAR, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        return dbus.ByteArray(self.read_value)

    @dbus.service.method(GATT_CHAR, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        STATE["gatt_writes"].append({"path": self.path, "type": str(options.get("type", ""))})
        if self.on_write:
            self.on_write(bytes(value))


class FakeMcspBike:
    """The bike's side of MCSP: answers reads/subscribes from a value table."""

    def __init__(self, rx, config):
        self.rx = rx
        self.values = {int(k, 16): bytes.fromhex(v) for k, v in config.get("values", {}).items()}
        self.overload_once = {int(k, 16) for k in config.get("overload_once", [])}
        self.only_src = int(config["only_src"], 16) if config.get("only_src") else None
        self.starts_handshake = config.get("starts_handshake", True)
        self.not_ready_left = config.get("not_ready_requests", 0)

    # Frames: [channel<<5 | end<<4 | len>>8][len & 0xff] payload
    def send(self, channel, payload):
        frame = bytes([(channel << 5) | 0x10 | (len(payload) >> 8), len(payload) & 0xFF])
        GLib.idle_add(self._emit, frame + payload)

    def _emit(self, data):
        self.rx.update(GATT_CHAR, Value=dbus.Array(data, signature="y"))
        self.rx.interfaces[GATT_CHAR]["Value"] = dbus.Array([], signature="y")
        return False

    def on_notify_start(self):
        if self.starts_handshake:
            self.send(0, b"\x01\x03")                     # VERSION 3
            self.send(0, b"\x04\x00\xb4")                 # MAX_SEGMENTATION 180
        # The bike asks the "phone" a question (READ 0x40AA, seq 3) ...
        self.send(1, b"\x21\x00\xc0\xaa\x03")
        # ... writes its startup stage (WRITE 0x40A9 = 5, seq 1) ...
        self.send(1, b"\x21\x00\xc0\xa9\x21" + b"\x08\x05")
        # ... and sends something to the eBike gateway, which isn't ours to answer.
        self.send(1, b"\x21\x00\xc1\x80\x04")
        return False

    def on_write(self, data):
        channel, length = data[0] >> 5, ((data[0] & 0x0F) << 8) | data[1]
        payload = data[2:2 + length]
        log = MCSP_LOG
        if channel == 0:
            log["commands"].append(payload.hex())
            return
        src, dst = int.from_bytes(payload[:2], "big"), int.from_bytes(payload[2:4], "big")
        op, seq = payload[4] >> 4, payload[4] & 0x0F
        log["messages"].append({"src": src, "dst": dst, "op": op, "seq": seq,
                                "body": payload[5:].hex()})
        if op % 2 == 1:
            return                                        # a reply from the app
        dp = dst & 0x7FFF
        reply_to = (src | 0x8000).to_bytes(2, "big")
        if self.only_src is not None and src != self.only_src:
            self.send(1, dp.to_bytes(2, "big") + src.to_bytes(2, "big")
                      + bytes([((op + 1) << 4) | seq, 2]))            # NO_ROUTE_FOUND
            return
        if self.not_ready_left > 0:
            self.not_ready_left -= 1
            self.send(1, dp.to_bytes(2, "big") + src.to_bytes(2, "big")
                      + bytes([((op + 1) << 4) | seq, 3]))            # NOT_READY
            return
        if dp in self.overload_once:
            self.overload_once.discard(dp)
            self.send(1, dp.to_bytes(2, "big") + src.to_bytes(2, "big")
                      + bytes([((op + 1) << 4) | seq, 1]))            # OVERLOADED
            return
        if op == 0:                                                   # READ
            if dp in self.values:
                self.send(1, dp.to_bytes(2, "big") + reply_to + bytes([0x10 | seq])
                          + self.values[dp])
            else:
                self.send(1, dp.to_bytes(2, "big") + src.to_bytes(2, "big")
                          + bytes([0x10 | seq, 2]))
        elif op in (6, 8):                                            # (UN)SUBSCRIBE
            self.send(1, dp.to_bytes(2, "big") + reply_to + bytes([((op + 1) << 4) | seq]))
            if op == 6 and dp in self.values:
                self.send(1, (dp | 0x8000).to_bytes(2, "big") + self.values[dp])

    def notify(self, dp, body):
        self.send(1, (dp | 0x8000).to_bytes(2, "big") + body)


class Root(dbus.service.Object):
    """ObjectManager at '/' that knows every object."""

    def __init__(self, bus):
        super().__init__(bus, "/")
        self.objects = {}

    def add(self, obj):
        self.objects[obj.path] = obj
        self.InterfacesAdded(obj.path, obj.interfaces)

    def remove(self, path):
        """Remove an object and everything below it, like BlueZ does."""
        for child in [p for p in self.objects if p == path or p.startswith(path + "/")]:
            obj = self.objects.pop(child)
            obj.remove_from_connection()
            self.InterfacesRemoved(child, dbus.Array(obj.interfaces, signature="s"))

    @dbus.service.method(OM, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        return {path: obj.interfaces for path, obj in self.objects.items()}

    @dbus.service.signal(OM, signature="oa{sa{sv}}")
    def InterfacesAdded(self, path, interfaces):
        pass

    @dbus.service.signal(OM, signature="oas")
    def InterfacesRemoved(self, path, interfaces):
        pass


def device_path(address):
    return f"{ADAPTER}/dev_{address.upper().replace(':', '_')}"


class Device(BluezObject):
    """A device the bridge may connect to as a central (Connect/Pair)."""

    def __init__(self, bus, address, is_bike, paired):
        uuids = dbus.Array([LDI_SERVICE] if is_bike else [], signature="s")
        super().__init__(bus, device_path(address), {"org.bluez.Device1": {
            "Address": address.upper(), "AddressType": "public", "Name": "smart system eBike",
            "Connected": False, "ServicesResolved": False, "Paired": paired,
            "Bonded": paired, "UUIDs": uuids}})
        self.is_bike = is_bike
        self.snapshot = b""

    @dbus.service.method("org.bluez.Device1")
    def Connect(self):
        STATE["connect_calls"].append(self.path)
        GLib.idle_add(lambda: CONTROL.connect(self, resolve=True) or False)

    @dbus.service.method("org.bluez.Device1")
    def Pair(self):
        STATE["pair_calls"].append(self.path)

    @dbus.service.method("org.bluez.Device1")
    def Disconnect(self):
        STATE["disconnect_calls"].append(self.path)
        GLib.idle_add(lambda: self.update("org.bluez.Device1", Connected=False,
                                          ServicesResolved=False) or False)


class Control(dbus.service.Object):
    """Test-only remote control."""

    def __init__(self, bus):
        super().__init__(bus, "/test")
        self.mcsp_config = {}        # address -> config
        self.bikes = {}              # address -> FakeMcspBike
        self.extra = {}              # address -> extra notifying characteristic

    def _device(self, address, is_bike, paired):
        path = device_path(address)
        if path not in ROOT.objects:
            ROOT.add(Device(BUS, address, is_bike, paired))
        return ROOT.objects[path]

    def _gatt(self, device):
        """Create the bike's GATT objects (once)."""
        base = device.path
        if f"{base}/service0010/char0011" in ROOT.objects:
            return
        ROOT.add(BluezObject(BUS, f"{base}/service0010",
                             {"org.bluez.GattService1": {"UUID": LDI_SERVICE}}))
        ROOT.add(Characteristic(BUS, f"{base}/service0010/char0011", LDI_CHAR,
                                ["read", "notify"], read_value=device.snapshot))
        ROOT.add(BluezObject(BUS, f"{base}/service0030",
                             {"org.bluez.GattService1": {
                                 "UUID": "0000180a-0000-1000-8000-00805f9b34fb"}}))
        ROOT.add(Characteristic(BUS, f"{base}/service0030/char0031", FIRMWARE_CHAR,
                                ["read"], read_value=b"21.12.0"))
        extra = Characteristic(BUS, f"{base}/service0030/char0032", EXTRA_NOTIFY_CHAR,
                               ["notify"])
        ROOT.add(extra)
        self.extra[device.interfaces["org.bluez.Device1"]["Address"]] = extra
        config = self.mcsp_config.get(device.interfaces["org.bluez.Device1"]["Address"])
        if config is not None:
            ROOT.add(BluezObject(BUS, f"{base}/service0020",
                                 {"org.bluez.GattService1": {"UUID": MCSP_SERVICE}}))
            rx = Characteristic(BUS, f"{base}/service0020/char0021", MCSP_RX, ["notify"],
                                mtu=247)
            tx = Characteristic(BUS, f"{base}/service0020/char0022", MCSP_TX,
                                ["write-without-response"], mtu=247)
            bike = FakeMcspBike(rx, config)
            rx.on_notify_start, tx.on_write = bike.on_notify_start, bike.on_write
            ROOT.add(rx)
            ROOT.add(tx)
            self.bikes[device.interfaces["org.bluez.Device1"]["Address"]] = bike

    def connect(self, device, resolve):
        if device.is_bike:
            self._gatt(device)
        device.update("org.bluez.Device1", Connected=True)
        if resolve:
            device.update("org.bluez.Device1", ServicesResolved=True)

    @dbus.service.method("test.FakeBluez", in_signature="s")
    def AddBondedBike(self, address):
        self._device(address, is_bike=True, paired=True)

    @dbus.service.method("test.FakeBluez", in_signature="ss")
    def EnableMcsp(self, address, config_json):
        self.mcsp_config[address.upper()] = json.loads(config_json)

    @dbus.service.method("test.FakeBluez", in_signature="say")
    def Advertise(self, address, payload):
        """A bike that is on and advertising, not yet connected."""
        device = self._device(address, is_bike=True, paired=True)
        device.update("org.bluez.Device1", RSSI=dbus.Int16(-60),
                      ManufacturerData=dbus.Dictionary(
                          {dbus.UInt16(0x02A6): dbus.Array(bytes(payload), signature="y")},
                          signature="qv"))

    @dbus.service.method("test.FakeBluez", in_signature="sbbay")
    def Connect(self, address, is_bike, resolve, snapshot):
        device = self._device(address, is_bike, paired=True)
        device.snapshot = bytes(snapshot)
        char = ROOT.objects.get(f"{device.path}/service0010/char0011")
        if char is not None:
            char.read_value = device.snapshot
        self.connect(device, resolve)

    @dbus.service.method("test.FakeBluez", in_signature="s")
    def Disconnect(self, address):
        device = ROOT.objects[device_path(address)]
        device.update("org.bluez.Device1", Connected=False, ServicesResolved=False)

    @dbus.service.method("test.FakeBluez", in_signature="say")
    def Notify(self, address, payload):
        char = ROOT.objects[f"{device_path(address)}/service0010/char0011"]
        char.update(GATT_CHAR, Value=dbus.Array(payload, signature="y"))

    @dbus.service.method("test.FakeBluez", in_signature="sqay")
    def McspNotify(self, address, dp, body):
        self.bikes[address.upper()].notify(int(dp), bytes(body))

    @dbus.service.method("test.FakeBluez", in_signature="say")
    def ExtraNotify(self, address, payload):
        self.extra[address.upper()].update(GATT_CHAR, Value=dbus.Array(payload, signature="y"))

    @dbus.service.method("test.FakeBluez", in_signature="s")
    def SetAddress(self, path_address):
        """BlueZ learned the bike's identity: Address changes, the path doesn't."""
        device = ROOT.objects[device_path(path_address)]
        device.update("org.bluez.Device1", Address=IDENTITY_ADDRESS)

    @dbus.service.method("test.FakeBluez", in_signature="s", out_signature="b",
                         async_callbacks=("reply", "error"))
    def AskPairing(self, address, reply, error):
        """Ask the default agent to authorise an LE Just-Works pairing.

        Like bluetoothd, the agent is called asynchronously, so the bridge
        can still query this fake (e.g. the device Address) while deciding.
        """
        sender, path = AGENTS.agent
        agent = dbus.Interface(BUS.get_object(sender, path), "org.bluez.Agent1")

        def rejected(exc):
            STATE["agent_errors"].append(exc.get_dbus_name())
            reply(False)

        agent.RequestAuthorization(dbus.ObjectPath(device_path(address)),
                                   reply_handler=lambda: reply(True),
                                   error_handler=rejected)

    @dbus.service.method("test.FakeBluez")
    def PowerCycle(self):
        ADAPTER_OBJ.update("org.bluez.Adapter1", Powered=False)
        ADAPTER_OBJ.update("org.bluez.Adapter1", Powered=True)

    @dbus.service.method("test.FakeBluez")
    def RestartDaemon(self):
        """Simulate bluetoothd restarting: the bus name is lost and regained."""
        def restart():
            BUS.release_name("org.bluez")
            BUS.request_name("org.bluez")
            return False
        GLib.timeout_add(50, restart)

    @dbus.service.method("test.FakeBluez", out_signature="s")
    def State(self):
        return json.dumps(STATE)

    @dbus.service.method("test.FakeBluez", out_signature="s")
    def McspLog(self):
        return json.dumps(MCSP_LOG)


IDENTITY_ADDRESS = "C0:FF:EE:00:00:01"

STATE = {"advertisements": [], "unregistrations": 0, "removed_devices": [],
         "notify_started": [], "property_sets": [], "agent_errors": [],
         "default_agent": None, "agent_capability": None,
         "agent_registrations": 0, "gatt_writes": [], "connect_calls": [],
         "pair_calls": [], "disconnect_calls": []}
MCSP_LOG = {"commands": [], "messages": []}

if __name__ == "__main__":
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    BUS = dbus.SystemBus()
    NAME = dbus.service.BusName("org.bluez", BUS)
    ROOT = Root(BUS)
    ADAPTER_OBJ = Adapter(BUS)
    ROOT.objects[ADAPTER] = ADAPTER_OBJ
    AGENTS = AgentManager(BUS)
    CONTROL = Control(BUS)
    print("fake bluez ready", flush=True)
    GLib.MainLoop().run()
