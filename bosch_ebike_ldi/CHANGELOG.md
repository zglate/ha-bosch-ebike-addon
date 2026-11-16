# Changelog

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
