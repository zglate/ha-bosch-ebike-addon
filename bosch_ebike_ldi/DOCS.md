# Bosch eBike

> **Very early version – don't use it yet.** This is an exploration build
> for testing on one bike; it hasn't been run against a real bike yet and may
> change completely between versions.

Data and diagnostics from a Bosch smart system eBike, over the Home Assistant
host's own Bluetooth adapter. No ESP32 or second Pi needed.

Two ways in:

- **Live Data Interface (LDI)**: Bosch's official interface. The bike
  connects to this add-on like it would to a Garmin and streams battery %,
  speed, cadence, rider power, odometer and status flags while it's on.
- **MCSP**: the channel the Bosch Flow app itself uses. Version 0.3 is an
  **exploration release**: it reads, read-only and gently, everything it
  safely can (motor power, assist mode, battery health/temperature/cycles,
  motor hours, software versions ...) and writes a detailed debug capture,
  so we learn exactly what your bike exposes on its firmware.

## Requirements

- Bosch smart system eBike, drive unit software **v19 or newer**
  (Flow app → bike → ⚙ → system info).
- Bike parked within Bluetooth range of the Home Assistant host (~10 m,
  fewer through walls).
- The **Mosquitto broker** add-on and the **MQTT** integration.
- A Bluetooth adapter on the host (the Raspberry Pi's built-in one is fine).

## Install (local add-on)

1. Copy the `bosch_ebike_ldi` folder into the `local_apps` folder on Home
   Assistant (called `addons` on older versions), using the **Samba share**
   or **Terminal & SSH** app. You should end up with
   `local_apps/bosch_ebike_ldi/config.yaml`.
2. **Settings → Apps → Install app**, then ⋮ (top right) → **Check for
   updates** (refresh the page if needed).
3. Open **Bosch eBike** under **Local apps** and select **Install**. The
   first install builds the image on the Pi and takes a few minutes.
4. Select **Start**, then open the **Log** tab. You should see
   `Run: add-on 0.3.0, capture in /share/bosch_ebike/run-...`.

If it doesn't appear under Local apps, check **Settings → System → Logs**
with **Supervisor** selected for errors about `config.yaml`.

## Let Home Assistant find the bike (recommended)

The app scans for Bosch bikes itself. To pair with yours:

1. Switch the bike on next to the Home Assistant host and put it in
   **pairing mode**. On Bosch controllers this is typically a long press of
   the power button until the light or display shows the Bluetooth/blue
   pairing indication; check your controller's manual.
2. Press **Start pairing** on the *Bosch eBike Bridge* device (Settings →
   Devices & services → Devices).
3. Within the 5-minute window the app pairs with the bike that says it's
   ready to pair, then explores it over MCSP. A notification shows its
   address with a ready-to-paste `bikes` entry.

It only pairs while the window is open **and** the bike signals pairing
mode, so it can't take a neighbour's bike. A Bosch bike it hears that isn't
in pairing mode just gets a "found nearby" notification.

This gives the MCSP data. The official LDI stream additionally needs the
Flow app step below (Bosch only streams LDI to accessories added there).

## Pair the bike in the Flow app (for the LDI)

The add-on accepts a new bike only during a **5-minute pairing window**,
opened with the **Start pairing** button on the *Bosch eBike Bridge* device.

1. Switch the bike on next to the Home Assistant host.
2. Press **Start pairing** in Home Assistant.
3. In the **Bosch eBike Flow** app: your bike → ⚙ → **Components** →
   **Add new device**.
4. The bike looks for accessories; pick **HA eBike Bridge** (it may show as
   a bare address). It can take 3–4 tries.
5. The log shows `eBike AA:BB:... connected – LDI yes, MCSP ...` and a new
   device with sensors appears in Home Assistant.

From then on the bike reconnects by itself whenever it's on and in range.

**Then:** copy the bike's address from that log line into the `bikes`
option (with a name and `battery_wh`). The add-on only ever opens its own
connection to bikes listed there or already paired, never anyone else's.

## Your first test session

1. Start the add-on with default options and pair the bike as above.
2. Leave the bike **switched on and standing still** next to the host for
   about **10 minutes**. The first time, the add-on reads the Flow app's
   start-up set, then sweeps every readable datapoint (~700), gently paced.
   Watch the *MCSP Status* entity on the bike's device: it goes through
   `listening` → `handshake sent` → `reading start-up set` →
   `sweeping ...` → `idle (sweep complete)`.
3. Also try: plugging the charger in for a few minutes, and pressing the
   assist +/- buttons (no need to ride).
4. Switch the bike off. Then send back the run folder (see below).

The sweep pauses by itself if the bike starts moving, and runs once per bike
per drive-unit firmware version (it repeats automatically after a firmware
update; the **Run MCSP read sweep** button forces one).

### What to send back

Everything is in **`/share/bosch_ebike/run-<date>-<time>/`** (the `share`
folder of the Samba share):

- `report.json`: the summary. Add-on version and options (no passwords),
  adapter, what the bike advertised, its Bluetooth services, every
  datapoint's value or error code, the session's milestones and timings.
- `capture-001.jsonl` …: every byte sent and received, with timestamps.

Zip the run folder and send it, along with the add-on **Log** and anything
you noticed (for example: did the Flow app still connect afterwards?).

## What it reads, and what it never does

**Reads:** the LDI stream; every readable Bluetooth characteristic and
descriptor; Bosch advertisements; and over MCSP, read and subscribe requests
for datapoints, which are the same ones the Flow app requests when it starts,
then a sweep of the rest of the readable table.

**Never:**
- writes a setting, sends a command, touches eBike Lock, ownership, keys or
  firmware. Every outgoing MCSP frame is checked against a read-only allowlist
  before it is sent, and command datapoints (185 of them) are excluded from
  the table entirely;
- answers the bike's requests with anything but "unsupported";
- connects to a Bosch bike that isn't yours (it logs it and moves on);
- holds on to your bike: a connection it opened is released a few minutes
  after exploring finishes, and it doesn't reconnect for
  `mcsp_reconnect_hours` (default 6), so the Flow app can always connect;
- removes a pairing by itself (only the **Forget paired bikes** button does);
- sends more than 4 requests at a time, at least 25 ms apart; it backs off if
  the bike says it's overloaded.

If the bike answers "not ready", the start-up set is retried up to 3 times;
if nothing answers from one source address, the other is tried.

## What you get in Home Assistant

**LDI, kept while the bike is off** (last known value): Battery SoC,
Odometer, Estimated Remaining Energy (SoC × `battery_wh`, an estimate),
Charger Connected, Last Seen, Connected.

**LDI, unavailable while the bike is off:** Rider Power (*your* pedalling
power), Speed, Cadence, Ambient Brightness, Light, In Motion, System Locked
(the LDI flag, not eBike Lock), Light Reserve, Diagnostic Tool Connected
(a dealer tool, not a fault), Charge Time to 80 % / 100 %.

**MCSP** (appear once read): Motor Power, Motor Torque, Rider Torque, Motor
Cadence, Assist Mode (by name), Battery Temperature, Battery State of Health,
Charge Cycles (total / on bike / off bike), Battery Capacity (Ah), Battery
Total Energy, System Usable Energy, Power-on Time, Power-on Time with Motor
Support, Charging Active, software versions of drive unit / battery /
controller / head unit, plus several "(raw)" sensors whose units aren't
confirmed yet.

**Diagnostics:** *MCSP Status* (state, with every counter, milestone and
start-up stage as attributes) and *MCSP Datapoints with Values*. Every
datapoint that returns a value also gets its own entity, **hidden by
default**: Settings → Devices → the bike → *entities not shown* to enable any
of them.

A value the bike hasn't sent shows as *unknown*, never as zero or off.

Bridge buttons: **Start pairing**, **Forget paired bikes**, **Run MCSP read
sweep**.

## Useful Home Assistant helpers

- **km per day/week/month:** Helpers → *Utility Meter* on *Odometer*.
- **Your energy per ride (Wh):** Helpers → *Integral (Riemann sum)* on *Rider
  Power* (method *left*, time unit hours), then a Utility Meter on it.
- **Charged notification:** automation on *Battery SoC* above 80 while
  *Charger Connected* is on.

Ride history, motor hours and service records from Bosch's cloud:
[Xunil99/ha-bosch-ebike](https://github.com/Xunil99/ha-bosch-ebike) alongside
this add-on (battery size 800 Wh for a PowerTube 800; Canada not yet
confirmed).

## Options

| Option | Default | Meaning |
|---|---|---|
| `bridge_name` | HA eBike Bridge | Name shown to the Flow app |
| `adapter` | hci0 | Bluetooth adapter (`auto` = first found) |
| `private_advertising` | true | Only visible to Flow apps during the pairing window |
| `log_level` | info | `debug` adds every raw packet and every refused datapoint |
| `battery_wh` | 800 | Nominal battery energy, for the energy estimate |
| `bikes` | [] | Your bikes: `address`, optional `name` and `battery_wh` |
| `mcsp` | true | Run the MCSP exploration when the bike exposes it |
| `mcsp_connect` | true | Connect to your bike (listed or paired) when it advertises, to reach MCSP |
| `mcsp_sweep` | true | Sweep every readable datapoint, once per firmware |
| `mcsp_reconnect_hours` | 6 | After releasing the bike, wait this long before connecting again |
| `gatt_listen` | true | Record every other notifying Bluetooth characteristic |
| `raw_entities` | true | One hidden diagnostic entity per decoded datapoint |
| `capture` | true | Write the debug capture to `/share/bosch_ebike` |
| `open_pairing_on_start` | false | Open the pairing window every time the add-on starts |
| `mqtt_host` etc. | empty | Only if you don't use the Mosquitto add-on |

## How it shares Bluetooth with Home Assistant

- It only reacts to devices that expose Bosch services or advertise as Bosch.
- It never removes other devices' pairings, and never its bikes' by itself.
- Pairing is only accepted during the window (or from a bike it already
  knows); the adapter's *Pairable* setting is on only for that window.
- It never makes the adapter discoverable over classic Bluetooth.
- One connection attempt at a time, at most one every 30 s.
- It re-advertises by itself after an adapter reset or `bluetoothd` restart.

## Troubleshooting

Setup problems (no MQTT broker, no Bluetooth, a new bike connected) appear as
**notifications in Home Assistant** with what to do.

**"Waiting for MQTT broker".** Mosquitto registers its connection details
with the Supervisor each time it starts. If the app says it's waiting:
restart the **Mosquitto broker** app and check its log for *"Successfully
send service information to the Supervisor"*. This app picks it up within
30 seconds by itself, no restart needed.

**MCSP Status says "MCSP not exposed on this connection".** The link the
bike opened for the LDI doesn't carry MCSP. With `mcsp_connect` on, the
add-on connects to the bike's own advertisement instead, *if the bike's
address is listed under `bikes`* (or it's already paired).

**"Bosch device ... isn't one of your bikes".** Add its address under
`bikes` if it is yours.

**The bike never finds the bridge, or pairing fails.**
1. Press **Start pairing** and retry within 5 minutes, with the bike on.
2. Remove any old "HA eBike Bridge" entry under Components in the Flow app.
3. To check whether Home Assistant's Bluetooth integration is in the way:
   Settings → Devices & services → Bluetooth → the adapter → ⋮ → Disable,
   restart this add-on, pair again, then re-enable it.

**"Looks like a stale bond".** The bike connected and dropped repeatedly. If
it won't reconnect, press **Forget paired bikes** and pair again.

**The Flow app can't connect while the add-on runs.** Note it in your report.
Set `mcsp_connect` to false to keep the add-on to the LDI only.

## Credits

Starting code from [possm/ha-bosch-ebike-pibridge](https://github.com/possm/ha-bosch-ebike-pibridge)
(Apache-2.0). Protocol details from the maintainer's own research,
[BikeBridge](https://codeberg.org/bg443/BikeBridge) and
[Xunil99/ha-bosch-ebike](https://github.com/Xunil99/ha-bosch-ebike); see
`SOURCES.md` in the repository.
