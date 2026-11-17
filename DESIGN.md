# Design – Bosch eBike add-on 0.4

> Superseded in parts by 0.5/0.6 (see `bosch_ebike_ldi/CHANGELOG.md`): MCSP
> runs over a link the add-on opens ("Connect like the Flow app"), only on
> charger plug-in, at full battery and on the button; the add-on never
> connects by itself otherwise, and there is no hold-off or 6-hourly repeat.

One release covering the whole journey, from install to daily use. Each
step says what the user sees and what the add-on does.

## 1. Journey

| Step | User sees | Add-on does |
|---|---|---|
| Install | App in the store (experimental) | — |
| Start, no broker | Notification "waiting for MQTT broker", with what to do | Asks the Supervisor every 30 s; continues by itself |
| Start, no Bluetooth | Notification "Bluetooth not reachable" | Stops |
| Start, no bike yet | **Setup** notification: how to add the bike. Bridge **Setup Status** = "No bike yet" | Scans for 15 s every minute |
| Bike switched on nearby | It appears in **Nearby Bosch eBikes** (a dropdown: address, "ready to pair"; signal strength in Setup Status) and in the Setup notification | Records every Bosch advertisement |
| Add the bike, either way | (a) Bike in pairing mode, press **Start pairing**; or (b) pick it in Nearby Bosch eBikes and press **Pair selected bike** | Pairs (MCSP, with the Pi as central). During the window it also advertises as an LDI accessory for the Flow app's *Add new device* |
| Paired | Setup notification replaced by "Bike added" (address, optional naming); bike device appears with model, serial and firmware | Bike remembered in `/data`; explores it |
| Exploring | Bike **Status** = "Exploring (MCSP): reading start-up set" or "…: sweeping datapoints, 40 %" | Start-up set, then sweep once per firmware (paused while moving) |
| Done | Bike Status = "Released for the Flow app; next check after 19:40" | Disconnects a link it opened; holds off `mcsp_reconnect_hours` |
| Riding / daily | LDI entities live (if Flow-paired); MCSP values refreshed at each check | Reconnects to your bike only; never holds it |
| New firmware published | **Update** entity per component shows "update available", plus one notification | Checks Bosch's public list daily. Never installs anything |
| Problem | Setup Status / bike Status say what's wrong; log lines explain | Capture + report per run in `/share/bosch_ebike/` |

## 2. Entities

**Bridge device:** Setup Status (sensor with attributes), Nearby Bosch
eBikes (select), Pair selected bike, Start pairing, Forget paired bikes, Run
MCSP read sweep (buttons), Pairing active, Bridge (binary sensors).

**Each bike:**
- Device info: model = drive unit product name, `sw_version` = drive unit
  software, serial number.
- Status sensor (text plus attributes).
- LDI entities (as 0.2) and MCSP entities (as 0.3).
- Update entities: drive unit, battery, controller, head unit. Each has the
  installed version (read from the bike), the latest version (Bosch's public
  list), a title, a summary and the release-notes URL. They have no install
  command.
- Hidden per-datapoint entities.

## 3. Notifications

Each has one fixed ID and is replaced in place, never stacked:

- `mqtt`: waiting for broker.
- `bluetooth`: Bluetooth unreachable.
- `setup`: how to add a bike, plus what's heard nearby. Dismissed once a bike
  is known.
- `bike_<addr>`: bike added.
- `pair_<addr>`: pairing failed.
- `firmware_<addr>_<component>`: new published version. Once per version.

## 4. Safety (unchanged rules)

- Read-only MCSP; commands are excluded from the table.
- Pairing only with consent: the pairing window plus a bike in pairing mode,
  or an explicit pick from Nearby.
- Never another person's bike. Never auto-remove bonds.
- Release after exploring, and hold off before reconnecting.
- Firmware: metadata only, never install.

## 5. Modules

| Module | Role |
|---|---|
| `status.py` (pure) | Setup status and bike status text and attributes from state |
| `nearby.py` (pure) | Registry of heard Bosch bikes; dropdown options ↔ address |
| `firmware.py` | Parse Bosch's public version list, match components, compare versions (pure); daily fetch |
| `bridge.py` | Orchestration: BlueZ, scanning, pairing, LDI, wiring |
| `explorer.py` / `mcsp.py` / `datapoints.py` | As in 0.3 |
| `ha_mqtt.py` | All entity discovery and state |
| `supervisor.py` | Broker lookup, notifications |

## 6. Firmware matching

The bike reports each component's `productName` (drive unit `0x182E`,
battery `0x009B`, controller `0x206F`, head unit `0x0D09`, plus partNumber,
productCode, serialNumber, hardwareVersion, softwareVersion).

Bosch's list has `Name: version` lines. Matching, in order:

1. Exact name.
2. `<name> smart system`.
3. For batteries, `Batteries smart system`.

If nothing matches, the latest version stays unknown; it is never guessed.
Versions are compared numerically, field by field.

## 7. Verification

- Unit tests for every pure module.
- End-to-end tests against the fake BlueZ, fake bike and fake Supervisor,
  covering the whole journey (no bike → nearby → pair from dropdown → status
  → exploration → release → update entity), plus the 0.3 safety tests.
- x86 and ARM64 runs; mutation checks on the new rules.
- An independent review before release.
