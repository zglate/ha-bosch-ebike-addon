# Bosch eBike

> **Very early version – don't use it yet.** This is an exploration build
> for testing on one bike; it hasn't been run against a real bike yet and may
> change completely between versions.

Data and diagnostics from a Bosch smart system eBike, over the Home Assistant
host's own Bluetooth adapter. No ESP32 or second Pi needed.

It finds the bike by itself: switch the bike on, pick it in a dropdown,
press a button. Two ways it reads data:

- **Live Data Interface (LDI)**: Bosch's official interface. The bike
  connects to this add-on like it would to a Garmin and streams battery %,
  speed, cadence, rider power, odometer and status flags while it's on.
- **MCSP**: the channel the Bosch Flow app itself uses. Version 0.4 is an
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

## Install

1. **Settings → Apps → App store** (called *Add-on store* on older
   versions) → ⋮ (top right) → **Repositories**, add
   `https://github.com/zglate/ha-bosch-ebike-addon`, then close the dialog.
2. Find **Bosch eBike** in the store (it's marked *experimental*), select
   **Install**, then **Start**.
3. A **notification** in Home Assistant's sidebar says what to do next. If
   anything is missing (MQTT broker, Bluetooth), the notification says so
   and how to fix it; the app keeps checking and carries on by itself.

## The Bosch eBike page

Everything happens on the app's own page: **Settings → Apps → Bosch eBike →
Open web UI**. Switch on **Show in sidebar** on that same app page once, and
it's one click from the Home Assistant sidebar. Notifications link straight
to it.

- **No bike yet:** the page walks you through adding one. Plug the bike into
  its charger (or switch it on) next to Home Assistant; it appears in the list
  within a minute; press **This is my bike**. If that fails, put it in pairing
  mode: switch it on, hold the System Controller's on/off button for more than
  3 seconds until the top bar flashes blue, and press it again.
- **Your bikes:** one card each with charge, health, cycles, distance and
  when it was last read. Click a card for everything: battery, charging,
  riding hours, temperatures, software versions (with *update available*),
  **Read now**, **Remove**, and the bike's **name** and **battery size**.
- **Add another bike:** the button under the cards.
- **Debug captures:** at the bottom, collapsed: download a run to send along
  with the app's log.

### How it reads the bike

Two ways, both read-only:

- **Live data (LDI):** Bosch's official feed (charge, distance, speed,
  cadence, your pedalling power, charger, lights). For it, also add the app as
  an accessory in the Flow app: the page's *Live riding data* box has the
  steps and a **Start pairing** button.
- **Full readings (MCSP):** battery health, cycles, temperatures, motor hours,
  software versions, charging mode. The bike only answers these on a
  connection the app opens itself, the way the Flow app does, and it takes one
  such connection at a time. So, when your bike **advertises** (at plug-in, or
  when switched on), the app connects for a few seconds and asks whether the
  charger is connected:
  - **charging:** it reads everything (about a minute), then lets go;
  - **not charging** (you're about to ride): it lets go at once, so the Flow
    app gets the bike. If the Flow app connects in those few seconds, it may
    need one retry.

  At most one such peek every 10 minutes per bike. Turn it off with the
  `mcsp_connect` option; **Read now** on the page works either way (bike on,
  standing still, Flow app closed).

## Your first test session

1. Start the add-on with default options and add the bike as above.
2. Leave the bike **switched on and standing still** next to the host for
   about **10 minutes** (or press **Read now** on the page). The first time, the add-on reads the Flow app's
   start-up set, then sweeps every readable datapoint (~700), gently paced.
   Watch the bike's **Status** entity, for example `Exploring (LDI + MCSP):
   sweeping datapoints, 40 %, 12 answered`. If it says *MCSP not answering on
   this link*, press **Connect like the Flow app** (above). *MCSP Status* has
   the detail.
3. Also try: plugging the charger in for a few minutes, and pressing the
   assist +/- buttons (no need to ride).
4. Switch the bike off. Then send back the run folder (see below).

The sweep pauses by itself if the bike starts moving, and runs once per bike
per drive-unit firmware version (it repeats automatically after a firmware
update; the **Run MCSP read sweep** button forces one).

### What to send back

Open the app's page (**Settings → Apps → Bosch eBike**) and press **Open web
UI**: it lists the runs, newest first, each as a zip download. (They are in
`/share/bosch_ebike/run-<date>-<time>/`.) Each contains:

- `report.json`: the summary. Add-on version and options (no passwords),
  adapter, what the bike advertised, its Bluetooth services, every
  datapoint's value or error code, the session's milestones and timings.
- `capture-001.jsonl` …: every byte sent and received, with timestamps.

Send the zip along with the add-on **Log** and anything
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
- connects to your bike by itself, except for the two automatic reads
  (charger plugged in, battery full) and the button; a read lasts at most 5
  minutes, then the bike is released;
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

**MCSP** (appear once read, kept between reads): Battery State of Health,
Charge Cycles (total / on bike / off bike), Battery Temperature, Battery FET
Temperature, Drive Unit Temperature, Battery Capacity (Ah), Battery Total
Energy, System Usable Energy, Remaining Energy for Rider, Lifetime Delivered
Energy (Wh) and Charge (Ah), Power-on Time, Power-on Time with Motor Support,
Charging Active, **Charging Mode** and **Charge Limit** (the battery's own
charging setting; the Flow app doesn't offer a way to change it yet), software
versions of drive unit / battery / controller / head unit, plus several
"(raw)" sensors whose meaning isn't confirmed yet. Motor Power, Motor Torque,
Rider Torque, Motor Cadence and Assist Mode only change while riding, when the
add-on leaves the bike to the Flow app, so they mostly show the last read.

**Status:** a plain-language summary per bike (connected over LDI and/or
MCSP, exploring with progress, reading like the Flow app, not connected); its
attributes include the last automatic read (`mcsp_check`: reason, outcome,
time).

**Firmware:** one **update entity** per component (drive unit, battery,
controller, display) with the version your bike reports and the latest
version Bosch publishes, plus release notes. It shows *update available*
when Bosch lists a newer one, and one notification per new version. It's
information only: install updates with the Flow app. If the app can't match
a component to Bosch's list (or the check hasn't succeeded in a week), its
update entity shows *unavailable* rather than claiming you're up to date.
With `firmware_check` off the update entities are removed. Bosch's list is per product line, not a check of
your particular bike's eligibility.

**Diagnostics:** *MCSP Status* (state, with every counter, milestone and
start-up stage as attributes) and *MCSP Datapoints with Values*. Every
datapoint that returns a value also gets its own entity, **hidden by
default**: Settings → Devices → the bike → *entities not shown* to enable any
of them.

A value the bike hasn't sent shows as *unknown*, never as zero or off.

**Bridge device:** *Setup Status*, *Nearby Bosch eBikes*, **Pair selected
bike**, **Start pairing**, **Connect like the Flow app**, **Forget paired
bikes**, **Run MCSP read sweep**, *Pairing Active*.

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
| `mcsp_connect` | true | Automatic MCSP reads when the charger is plugged in and when the battery is full |
| `mcsp_sweep` | true | Sweep every readable datapoint, once per firmware |
| `mcsp_reconnect_hours` | 6 | Not used since 0.6 (kept so existing configurations stay valid) |
| `gatt_listen` | true | Record every other notifying Bluetooth characteristic |
| `raw_entities` | true | One hidden diagnostic entity per decoded datapoint |
| `capture` | true | Write the debug capture to `/share/bosch_ebike` |
| `open_pairing_on_start` | false | Open the pairing window every time the add-on starts |
| `firmware_check` | true | Compare the bike's firmware with Bosch's published versions daily |
| `firmware_page` | Bosch Canada's software-update page | Page to read published versions from (use your country's) |
| `mqtt_host` etc. | empty | Only if you don't use the Mosquitto add-on |

## How it shares Bluetooth with Home Assistant

- It only reacts to devices that expose Bosch services or advertise as Bosch.
- It never removes other devices' pairings, and never its bikes' by itself.
- Pairing only happens during the window, for a bike you picked with **Pair
  selected bike**, or with a bike it already knows; the adapter's *Pairable*
  setting is on only for the window or that one pairing attempt.
- It never makes the adapter discoverable over classic Bluetooth.
- One connection attempt at a time, at most one every 30 s.
- It re-advertises by itself after an adapter reset or `bluetoothd` restart.

## Troubleshooting

Setup problems (no MQTT broker, no Bluetooth, no bike added yet, pairing
failed) appear as **notifications in Home Assistant** with what to do, and
*Setup Status* on the bridge device says where setup stands.

**Nearby Bosch eBikes stays empty.** Check the add-on log: after every scan
it says which Bosch bikes it heard, or "no Bosch eBike heard". Make sure the
bike is switched on (display or controller lit) and close to the host.
Bosch bikes may only advertise for a while after being switched on; switch
it off and on again.

**"Waiting for MQTT broker".** Mosquitto registers its connection details
with the Supervisor each time it starts. If the app says it's waiting:
restart the **Mosquitto broker** app and check its log for *"Successfully
send service information to the Supervisor"*. This app picks it up within
30 seconds by itself, no restart needed.

**MCSP Status says "MCSP not exposed on this connection".** The link the
bike opened for the LDI doesn't carry MCSP. The automatic reads and the
**Connect like the Flow app** button connect the way the app does instead.

**"Bosch device ... isn't one of your bikes".** Add it with **Pair selected
bike** if it is yours.

**Pairing fails, or the Flow app never finds the bridge.**
1. Retry **Pair selected bike** with the bike on, or press **Start pairing**
   and retry within 5 minutes.
2. Remove any old "HA eBike Bridge" entry under Components in the Flow app.
3. To check whether Home Assistant's Bluetooth integration is in the way:
   Settings → Devices & services → Bluetooth → the adapter → ⋮ → Disable,
   restart this add-on, pair again, then re-enable it.

**"Looks like a stale bond".** The bike connected and dropped repeatedly. If
it won't reconnect, press **Forget paired bikes** and pair again.

**The Flow app can't connect.** Only during a read (at most 5 minutes after
plugging in the charger, at 100 %, or after the button). Set `mcsp_connect` to
false to stop the automatic reads.

## Credits

Starting code from [possm/ha-bosch-ebike-pibridge](https://github.com/possm/ha-bosch-ebike-pibridge)
(Apache-2.0). Protocol details from the maintainer's own research,
[BikeBridge](https://codeberg.org/bg443/BikeBridge) and
[Xunil99/ha-bosch-ebike](https://github.com/Xunil99/ha-bosch-ebike); see
`SOURCES.md` in the repository.
