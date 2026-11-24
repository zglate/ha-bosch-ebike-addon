# Bosch eBike

> **Early version.** Tested on one bike so far. It talks to the bike partly
> over an undocumented channel, so things may change between versions.

Data and diagnostics from a Bosch smart system eBike, over the Home Assistant
host's own Bluetooth adapter. No ESP32 or second Pi needed. Read-only: it
never changes anything on the bike.

## Requirements

- Bosch smart system eBike, drive unit software **v19 or newer**
  (Flow app → bike → ⚙ → system info).
- Bike parked within Bluetooth range of the Home Assistant host (~10 m,
  fewer through walls), for example where you charge it.
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
- **Your bike:** charge, status and when it was last read at the top, with
  **Read full details**. Below:
  - battery health, charge cycles, distance and motor hours;
  - **messages from the bike** (Bosch fault codes, with what to do);
  - **last ride, from the bike**: the bike's own ride computer, also for rides
    without the phone (distance, moving time, speeds, your power, energy,
    jumps and other tricks; no map, which needs the phone's GPS);
  - **charging & riding**: time to full while charging, the last charge, how
    long a full charge takes, the last ride between charges, your energy per
    km and what a full battery is good for;
  - battery, charging, range per assist mode, motor and software (with
    *update available*);
  - **live riding data**: how to set up Bosch's live feed (below);
  - **bike settings**: name, battery size, remove.
- **Several bikes:** an overview with one card each (click a card for its
  page); **Add another bike** under them.
- **Debug captures:** at the bottom, collapsed: download a run to send along
  with the app's log.

## How it reads the bike

Two ways, both read-only:

- **Live data (LDI):** Bosch's official feed (charge, distance, speed,
  cadence, your pedalling power, charger, lights), sent by the bike while it's
  on, and for the whole charge when it's charging. For it, add this app as an
  accessory in the Flow app: the page's *Live riding data* box has the steps
  and a **Start pairing** button. In the Flow app it shows as an accessory
  with the part number, serial number and software version from the options
  below.
- **Full readings (MCSP):** battery health, cycles, temperatures, motor hours,
  software versions, charging mode, range, the ride computer, messages. The
  bike only answers these on a connection the app opens itself, the way the
  Flow app does, and it takes one such connection at a time. So, when your
  bike **advertises** (at plug-in, or when switched on), the app connects for
  a few seconds and asks whether the charger is connected:
  - **charging:** it reads everything (under a minute), then lets go;
  - **not charging** (you're about to ride): it lets go at once, so the Flow
    app gets the bike. If the Flow app connects in those few seconds, it may
    need one retry.

  At most one such check every 10 minutes per bike. Turn it off with the
  `mcsp_connect` option; **Read full details** on the page works either way
  (bike on, standing still, Flow app closed; at most 5 minutes).

The first full reading of a bike's firmware also reads every other readable
datapoint once, gently paced, and pauses if the bike moves (`mcsp_sweep`).

### Notifications

- **Remote battery low:** once when a wireless remote's coin cell (Mini
  Remote) reads 20 % or less at a full reading; the page shows the same
  alert. It clears by itself once a new cell reads higher.
- **Fully charged:** once per charge (option `notify_full`). For a push to
  your phone instead, make an automation on the bike's **Battery SoC**
  sensor going to 100 while **Charger Connected** is on.
- **New firmware published:** once per version (option `firmware_check`).
- Setup problems: no MQTT broker, no Bluetooth, pairing failed.

## What it never does

- Writes a setting, sends a command, touches eBike Lock, ownership, keys or
  firmware. Every outgoing MCSP frame is checked against a read-only allowlist
  before it is sent: reads and subscriptions, plus exactly one call, the
  getter the Flow app uses for a wireless remote's battery (with nothing but
  the remote's number). Every other call is refused.
- Answers the bike's requests with anything but "unsupported".
- Connects to a Bosch bike that isn't yours (it logs it and moves on).
- Holds your bike: a check lets go within seconds unless the bike is
  charging, and a full reading lasts at most 5 minutes.
- Removes a pairing by itself (only **Remove** or **Forget paired bikes**
  does).
- Sends more than 4 requests at a time, at least 25 ms apart; it backs off if
  the bike says it's overloaded.
- Installs firmware: the firmware check is information only.

## What you get in Home Assistant

**LDI, kept while the bike is off** (last known value): Battery SoC,
Odometer, Estimated Remaining Energy (SoC × `battery_wh`, an estimate),
Charger Connected, Last Seen, Connected.

**LDI, unavailable while the bike is off:** Rider Power (*your* pedalling
power), Speed, Cadence, Ambient Brightness, Light, In Motion, System Locked
(the LDI flag, not eBike Lock), Light Reserve, Diagnostic Tool Connected
(a dealer tool, not a fault), Charge Time to 80 % / 100 %.

**Full readings** (appear once read, kept between reads): Battery State of
Health, Charge Cycles, Battery and drive unit temperatures, Battery Capacity
(Ah), System Usable Energy, Remaining Energy, Lifetime Delivered Energy (Wh)
and Charge (Ah), Power-on Time, Power-on Time with Motor Support, Charging
Mode and Charge Limit (the battery's own charging setting), Range per assist
mode, Bike Messages, Remote Battery (the wireless remote's coin cell; the
lowest if there are several), the last ride (moving time, average speed,
energy, calories, jumps) and software versions. Values that only change while riding
(motor power, torque, assist mode) mostly show the last read.

**Status:** a plain-language summary per bike; its attributes include the
last automatic check (`mcsp_check`: reason, outcome, time).

**Firmware:** one **update entity** per component (drive unit, battery,
controller, display) with the version your bike reports and the latest
version Bosch publishes, plus release notes. Install updates with the Flow
app. If the app can't match a component to Bosch's list (or the check hasn't
succeeded in a week), the entity shows *unavailable* rather than claiming
you're up to date. Bosch's list is per product line, not a check of your
particular bike's eligibility.

**Diagnostics:** *MCSP Status* and *MCSP Datapoints with Values*. With
`raw_entities` on, every datapoint that returns a value also gets its own
entity, hidden by default.

A value the bike hasn't sent shows as *unknown*, never as zero or off.

**Bridge device:** *Setup Status*, *Nearby Bosch eBikes*, **Pair selected
bike**, **Start pairing**, **Read bike now**, **Forget paired bikes**, **Run
MCSP read sweep**, *Pairing active*, *Bridge*.

## Useful Home Assistant helpers

- **km per day/week/month:** Helpers → *Utility Meter* on *Odometer*.
- **Your energy per ride (Wh):** Helpers → *Integral (Riemann sum)* on *Rider
  Power* (method *left*, time unit hours), then a Utility Meter on it.

Ride history with maps and service records from Bosch's cloud:
[Xunil99/ha-bosch-ebike](https://github.com/Xunil99/ha-bosch-ebike) alongside
this add-on.

## Options

| Option | Default | Meaning |
|---|---|---|
| `bridge_name` | Home Assistant Reader | Name this app advertises; you pick it when adding the accessory in the Flow app |
| `accessory_model` | BHA2026 | Shown as **Part number** for this accessory in the Flow app |
| `accessory_serial` | (empty: HA- + end of the host's Bluetooth address) | Shown as **Serial number** |
| `accessory_manufacturer`, `accessory_hardware` | Home Assistant, HA-4200-PI | Served to the bike; the Flow app doesn't show them |
| `adapter` | hci0 | Bluetooth adapter (`auto` = first found) |
| `private_advertising` | true | Only visible to Flow apps during the pairing window |
| `log_level` | info | `debug` adds every raw packet and every refused datapoint |
| `battery_wh` | 800 | Nominal battery energy, for estimates (also per bike on the page) |
| `bikes` | [] | Your bikes: `address`, optional `name` and `battery_wh` |
| `mcsp` | true | Full readings over the Flow app's channel |
| `mcsp_connect` | true | Check the bike when it advertises; read everything when it's charging |
| `mcsp_sweep` | true | Read every readable datapoint once per firmware |
| `mcsp_reconnect_hours` | 6 | Not used since 0.6 (kept so existing configurations stay valid) |
| `gatt_listen` | true | Record every other notifying Bluetooth characteristic |
| `raw_entities` | false | One hidden diagnostic entity per decoded datapoint |
| `capture` | true | Write the debug capture to `/share/bosch_ebike` |
| `open_pairing_on_start` | false | Open the pairing window every time the add-on starts |
| `notify_full` | true | Notification when the battery is full |
| `firmware_check` | true | Compare the bike's firmware with Bosch's published versions daily |
| `firmware_page` | Bosch Canada's software-update page | Page to read published versions from (use your country's) |
| `mqtt_host` etc. | empty | Only if you don't use the Mosquitto add-on |

In the Flow app the accessory's **Software version** is always the add-on's
version, and its name is the host's own Bluetooth name (for example
"homeassistant"); the **Product code** stays empty for accessories.

## Debug captures

The page's *Debug captures* section lists the runs, newest first, each as a
zip download (they are in `/share/bosch_ebike/run-<date>-<time>/`). Each
contains:

- `report.json`: the summary. Add-on version and options (no passwords),
  adapter, what the bike advertised, its Bluetooth services, every
  datapoint's value or error code, the session's milestones and timings.
- `capture-001.jsonl` …: every byte sent and received, with timestamps.

They contain your bike's Bluetooth address and serial numbers; share them
only with people you trust.

## How it shares Bluetooth with Home Assistant

- It only reacts to devices that expose Bosch services or advertise as Bosch.
- It never removes other devices' pairings, and never its bikes' by itself.
- Pairing only happens during the window, for a bike you picked, or with a
  bike it already knows; the adapter's *Pairable* setting is on only for the
  window or that one pairing attempt.
- It never makes the adapter discoverable over classic Bluetooth.
- One connection attempt at a time, at most one every 30 s.
- It re-advertises by itself after an adapter reset or `bluetoothd` restart.

## Troubleshooting

Setup problems (no MQTT broker, no Bluetooth, no bike added yet, pairing
failed) appear as **notifications in Home Assistant** with what to do.

**The bike doesn't show up.** Check the add-on log: after every scan it says
which Bosch bikes it heard, or "no Bosch eBike heard". Plug the bike into its
charger or switch it on (display or controller lit), close to the host. Bosch
bikes only advertise for a while after waking up; switch it off and on again.

**"Waiting for MQTT broker".** Mosquitto registers its connection details
with the Supervisor each time it starts. Restart the **Mosquitto broker** app
and check its log for *"Successfully send service information to the
Supervisor"*. This app picks it up within 30 seconds by itself.

**No full readings.** The bike answers them only on a connection the app
opens, not on the live-data link the bike opens; the page's status line says
when it last answered. Press **Read full details** with the bike on, standing
still, and the Flow app closed.

**"Bosch device ... isn't one of your bikes".** Add it on the page if it is
yours.

**Pairing fails, or the Flow app never finds the accessory.**
1. Press **Start pairing** on the page and retry within 5 minutes.
2. Remove any old entry for this app ("HA eBike Bridge", "Home Assistant
   Reader" or "Accessory") under Components in the Flow app.
3. To check whether Home Assistant's Bluetooth integration is in the way:
   Settings → Devices & services → Bluetooth → the adapter → ⋮ → Disable,
   restart this add-on, pair again, then re-enable it.

**"Looks like a stale bond".** The bike connected and dropped repeatedly. If
it won't reconnect, press **Forget paired bikes** and pair again.

**The Flow app can't connect.** Only possible during a check (a few seconds
when the bike wakes up) or a full reading (at most 5 minutes); retry once.
Set `mcsp_connect` to false to stop the automatic checks.

## Credits

Starting code from [possm/ha-bosch-ebike-pibridge](https://github.com/possm/ha-bosch-ebike-pibridge)
(Apache-2.0). Protocol details from the maintainer's own research,
[BikeBridge](https://codeberg.org/bg443/BikeBridge) and
[Xunil99/ha-bosch-ebike](https://github.com/Xunil99/ha-bosch-ebike); see
`SOURCES.md` in the repository.
