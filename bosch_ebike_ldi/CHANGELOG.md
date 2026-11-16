# Changelog

## 0.4.1

- **Fixed: the bike wasn't found.** A real Range VLT CX advertises only its
  name and Bosch's discovery service (0xFE02), without the manufacturer data
  the app was looking for. Bikes are now recognised by either. Without
  manufacturer data, pairing mode shows as *unknown*: pick the bike and press
  **Pair selected bike** (Start pairing only auto-pairs a bike that says it's
  ready).
- **Fixed: a bike repeating the same advertisement was missed.** BlueZ sends
  advertisement data once (or from its cache) and afterwards only signal
  updates; those now count as hearing the bike.
- After each scan the log lists how many Bluetooth devices were heard in
  total, their manufacturer IDs and the strongest named ones, and the capture
  has the full list, to tell "nothing heard" from "not recognised".

## 0.4.0 – the whole setup journey

- **Add your bike from Home Assistant, without the Flow app.** Bosch bikes the
  app hears appear in a **Nearby Bosch eBikes** dropdown (with pairing
  mode; signal strength in Setup Status and the setup notification). Pick
  yours and press **Pair selected bike**. Whether a bike accepts this outside
  pairing mode is not yet tested on a real bike; if it doesn't, put it in
  pairing mode and press **Start pairing** (or pick it and press Pair selected
  bike while it's in pairing mode). Paired bikes are remembered and reconnect by themselves;
  the `bikes` option is now only for names and battery sizes.
- **One setup notification** that evolves: how to add a bike and which Bosch
  bikes are heard nearby, dismissed once a bike is added. Replaces the
  per-bike "found nearby" notices.
- **Setup Status** sensor on the bridge device: where setup stands.
- **Status** sensor per bike: connected over LDI and/or MCSP, exploring
  (with sweep progress), released for the Flow app until when.
- **Bike device info** from the bike itself: model, serial number, drive unit
  software and hardware version.
- **Firmware update entities** per component (drive unit, battery,
  controller, display): the version your bike reports against the latest one
  Bosch publishes, with release notes, checked daily. One notification per
  new version, dismissed once installed. A component Bosch's list doesn't
  name shows *unavailable*, never "up to date". Information only, never
  installs. Options `firmware_check` and `firmware_page`.
- Scanning for bikes no longer depends on `mcsp_connect`.
- Scans are visible in the log: each scan's start, errors, and which Bosch
  bikes were heard (or "no Bosch eBike heard").
- Every option now has a name and description in the Configuration tab.
- The "bike connected" and "pairing failed" notifications match the new flow.

## 0.3.2

- **Home Assistant finds the bike itself.** The app now scans for Bosch bikes
  (a short scan every minute while no bike is connected, continuously during
  the pairing window). Press **Start pairing** with the bike in pairing mode
  and the app pairs with it and explores it. No Flow-app step needed for MCSP.
- A Bosch bike heard nearby but not in pairing mode gets a "found nearby"
  notification instead of being ignored silently.
- Outside the pairing window the adapter's *Pairable* setting is put back as
  it was (BlueZ switches it on when a pairing agent registers).

## 0.3.1

- **Setup problems now show up in Home Assistant** as notifications, with what
  to do, instead of only in the log:
  - no MQTT broker registered yet: the app keeps checking every 30 s and
    carries on by itself once Mosquitto registers (Mosquitto drops and
    re-registers its details on every restart), then clears the notice;
  - Bluetooth not reachable;
  - a new bike connected: its address with a ready-to-paste `bikes` entry.
- The app now asks the Supervisor for the MQTT broker itself (no more
  "Service not enabled" error line from the start script).
- Fixed the `bashio::addon.version is deprecated` warning.

## 0.3.0 – exploration release

- **New: MCSP exploration** (the Flow app's Bluetooth channel), read-only:
  - transport handshake, answering the bike's or starting its own;
  - reads and subscribes the Flow app's own start-up set (from the app's
    observed source addresses), then sweeps every readable datapoint once
    per drive-unit firmware, paused while the bike moves;
  - retries when the bike answers NOT_READY, and falls back to the other
    source address if nothing answers;
  - at most 4 requests in flight, 25 ms apart, with back-off on OVERLOADED;
  - every outgoing frame is checked against a read-only allowlist; 185
    command datapoints are excluded from the table;
  - only "unsupported" replies to the bike's requests to the phone; its
    start-up stages are recorded.
- **New: connecting to your bike** as a central to reach MCSP: only bikes in
  `bikes` or already paired; one attempt at a time; released a few minutes
  after exploring; no reconnect for `mcsp_reconnect_hours`.
- **New: Bluetooth inventory**: device properties, every service,
  characteristic and descriptor read, every other notifying characteristic
  recorded.
- **New: debug capture** per run in `/share/bosch_ebike/run-*/`: a JSONL
  event log and `report.json` with run details, milestones, every
  datapoint's value or error, the bike's requests and disconnect reasons.
- **New: Home Assistant entities** for MCSP (motor power, torque, assist
  mode, battery health/temperature/cycles/capacity, hours, software
  versions), MCSP status diagnostics, a hidden entity per decoded datapoint,
  and a **Run MCSP read sweep** button.
- **Changed:** the pairing window no longer opens on start
  (`open_pairing_on_start`); use the button.
- **Changed:** a suspected stale bond is only reported; bonds are removed
  only with the **Forget paired bikes** button.
- **Changed:** the add-on is renamed "Bosch eBike" (same slug, entities kept).

## 0.2.0

- Fixed *System Locked* (inverted), *Charger Connected* (was "Charging"),
  *Diagnostic Tool Connected* (no longer a "problem"), *Light* class; live
  entities go unavailable while the bike is off; unknown instead of off for
  missing values; *Estimated Remaining Energy*, *Last Seen*, *Forget paired
  bikes*, add-on version on the bridge device.

## 0.1.0

- possm's Raspberry Pi bridge packaged as a Home Assistant add-on sharing the
  host's Bluetooth adapter.
