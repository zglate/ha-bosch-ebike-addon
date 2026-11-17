# Changelog

## 0.7.1

- The page keeps showing the last full reading (health, cycles, temperatures,
  software versions ...) after the app restarts, instead of blank values
  until the next one. Readings from before 0.7 count as the last good one.

## 0.7.0 – the Bosch eBike page

- **One page for everything** (Open web UI, or the sidebar with *Show in
  sidebar* on): an overview card per bike (charge, health, cycles, distance,
  last full reading), each bike's details (battery, charging, riding hours,
  temperatures, software versions with *update available*), **Read now**,
  **Remove**, and the bike's **name** and **battery size**, saved by the app
  (no YAML). Adding a bike is a short guided list on the same page. Debug
  captures moved to the bottom. Notifications link to the page.
- **Full readings when the bike advertises ("peek").** A real plug-in this
  morning showed: switched off, the bike wakes at plug-in, advertises once,
  connects for live data, and goes back to sleep if that link is dropped, so
  0.6's charger-triggered reads got nothing and cost the live data. Now, when
  your bike advertises, the app connects for a few seconds and asks for the
  charger state: charging, it reads everything; not charging, it lets go at
  once for the Flow app. At most once per 10 minutes. The 0.6 charger and
  full-battery triggers are gone.
- Battery charge and odometer also come from the full reading (for bikes
  without the live feed).
- Quieter log: with a bike added, "nothing heard" scan lines are debug-level
  and the device summary is hourly. While your bike isn't connected the app
  listens almost continuously, since it only advertises briefly at plug-in.
- The bridge's "Connect like the Flow app" button is now **Read bike now**.

## 0.6.0 – MCSP reads that leave the Flow app alone

The first real MCSP session read 125 datapoints (battery health 96 %, 2.4
cycles, motor hours, temperatures, firmware ...). It also showed that, while
the add-on was connected the phone way, the Flow app couldn't connect (one
test; likely one such connection at a time). So:

- **Automatic reads at the right moments only:** about 30 s after the charger
  is plugged in, and when the battery reaches 100 % (seen through the LDI).
  The add-on **never connects to the bike by itself otherwise** (it grabbed
  the bike at every power-on in 0.5). The 6-hourly repeat is gone too.
- **Fixed: one value could stall a whole read.** Datapoints containing raw
  bytes crashed when published, before the request was marked done, so the
  start-up phase never finished. Bytes now publish as hex, and a publishing
  error can no longer interrupt MCSP.
- New or now properly-unit'd sensors: Battery FET and Drive Unit temperature
  (°C), Remaining Energy for Rider (Wh), Lifetime Delivered Energy (Wh) and
  Charge (Ah), **Charging Mode** and **Charge Limit** (the battery's charging
  setting: currently charge-time optimised, no limit; the Flow app has no
  control for it yet), Active Issue (raw).
- Notifications only for button presses; automatic reads show in the bike's
  Status attributes (`mcsp_check`: reason, outcome, time).
- A plug-in read is cancelled if the charger is unplugged during the 30 s
  delay (the bike is about to be ridden).
- A failing debug-capture write (e.g. full disk) now stops the capture instead
  of interrupting the Bluetooth handling.

## 0.5.0 – connect like the Flow app

What the first real bike showed: the LDI works (battery, odometer, speed,
cadence, rider power, light, lock, charger), but MCSP sent nothing at all on
the link the bike opened for the LDI. The Flow app connects the other way
round, so:

- **New button: Connect like the Flow app.** Pauses the LDI (advertising off,
  link dropped), connects to the bike on its next advertisement as the phone
  does, runs MCSP, then disconnects and advertises again so the LDI comes
  back. At most 5 minutes; advertising is always restored; no pairing
  accepted meanwhile. A notification gives the result. It repeats every
  `mcsp_reconnect_hours` only once a bike has answered this way.
- **MCSP stops when the bike is silent**: after 20 unanswered requests with
  nothing at all received, instead of timing out ~700 reads. Not recorded as
  a completed sweep. Status says *MCSP not answering on this link*.
- Bike Status shows how many datapoints answered while exploring.
- **Who opened a link is measured** (the add-on's connect call came before
  BlueZ reported it connected), not assumed: the first real link was opened
  by the bike but logged as the add-on's. Bikes that connect to us for the
  LDI are never connected to automatically.
- The transport handshake goes out in one write, as the Flow app does.
- **Download debug captures from the app's Open web UI page**, no Samba or SSH.
- *Last Seen* updates at most once a minute (it flooded the logbook).

## 0.4.3

- **Fixed: a bike standing still was heard once, then never again.** BlueZ
  only reports a device again when its signal moves by 8 dBm or more, unless
  the scan has a signal-strength filter. Scans now use one that lets
  everything through, so every advertisement counts (and Pair selected bike
  pairs on the next one).
- The log shows when a bike's advertising flags change (limited vs general
  discoverable; may tell pairing mode apart).

## 0.4.2

- **Fixed: notifications never appeared, silently.** A refused notification
  was counted as shown. Now each one is logged as shown, or with Home
  Assistant's error, and the log says at start whether Home Assistant's API
  is reachable.
- The setup notification links **straight to the Bosch eBike Bridge device**.
- **Pair selected bike waits for the bike.** A Bosch bike may advertise only
  briefly; if it hasn't been heard in the last few seconds the app waits up
  to 3 minutes (scanning) and pairs the moment it's heard, instead of trying
  to pair a bike that isn't listening. Setup Status says it's waiting.
- No scanning while pairing (the Pi's single radio jammed: scans got no
  reply).
- Pairing-mode instructions are Bosch's own (System Controller manual): bike
  on, hold the on/off button for more than 3 s, let go when the top bar of
  the charge indicator flashes blue.

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
