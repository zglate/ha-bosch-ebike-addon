# Design

How the add-on talks to the bike, and why. User-facing behaviour is in
[bosch_ebike_ldi/DOCS.md](bosch_ebike_ldi/DOCS.md); the history of each
decision is in [the changelog](bosch_ebike_ldi/CHANGELOG.md).

## 1. Two channels

| | Live data (LDI) | Full readings (MCSP) |
|---|---|---|
| What | Bosch's official feed: charge, odometer, speed, cadence, rider power, flags | The Flow app's channel: about 700 readable datapoints |
| Who connects | The bike, to the add-on (the add-on is a peripheral, like a Garmin) | The add-on, to the bike (central, like the phone) |
| Set up by | Adding the add-on as an accessory in the Flow app | Nothing extra (uses the bond from the accessory step) |
| When | Whenever the bike is on, and for the whole charge | Short checks when the bike advertises; full readings while charging or on request |

Observed on a real bike (Performance Line CX, firmware 21.x):

- The bike answers MCSP only on a connection the add-on opens. On the link
  the bike opens for live data, MCSP stays silent; the add-on remembers this
  per bike and doesn't ask again on that link.
- The bike takes one phone-style connection at a time; the phone wins.
- Plugged in while off, the bike wakes, advertises once, connects live data
  and streams it for the whole charge; on the charger it wakes again about
  every 2 hours. Dropping its link while it charges switched off makes it go
  back to sleep, so the add-on never forces that.

## 2. Checks and full readings

**Check** (on every advertisement of a known bike with no link, at most once
per 10 minutes per bike): connect as central, read "charger connected"
(`0x0089`; an empty value means false), decide within 30 s:

- charging: read the curated set (identity, battery, motor, range, ride
  computer, messages), about 12 s on the real bike, then disconnect;
- not charging: disconnect at once, so the Flow app gets the bike when the
  rider is about to ride.

**Full reading** ("Read full details", or "Read bike now" in HA): stop
advertising, drop the live-data link, connect as central, read, then restore.
At most 5 minutes.

A session that gets no answers to 20 requests in a row goes silent: it stops
asking, cancels its timers and reports why.

## 3. Read-only

- The datapoint table marks what may be read: 694 readable datapoints; the
  185 calls (RPC) and 88 other datapoints are in it only as not readable, and
  nothing is ever requested from them, except the one call below.
- Every outgoing MCSP frame is checked before it is written
  (`mcsp.check_outgoing`): on the transport channel only the version,
  flow-control and segmentation commands; on the message bus only READ,
  SUBSCRIBE and UNSUBSCRIBE requests without a body, "unsupported"
  responses without a body, and one call (RPC): `bleCentralGetBatteryService`
  (`0x2220`) with exactly a remote id as its argument. Anything else raises
  instead of being sent.
- That call is a getter: the System Controller is a Bluetooth central for
  wireless remotes (the Mini Remote, on a coin cell), and the Flow app asks it
  for each remote's battery level this way. The remotes come from
  `bleCentralDatabase` (`0x2210`); changes arrive on `0x2221`.
- So requests to the add-on from the bike get "unsupported".
- At most 4 requests in flight, 25 ms apart; back off on OVERLOADED; retry
  NOT_READY a few times; try the other source address if one stays silent.

## 4. State

Per bike, in `/data`: the last values, what each check found, the first
battery health (for the trend), and a charging and riding history
(`stats.py`): charges from start to full, and rides as the odometer and
charge difference between unplugging and the next plug-in. On first start
the history is rebuilt once from the debug captures already on disk.

## 5. The page

`webui.py` serves one page through Home Assistant ingress (only the
Supervisor's proxy and localhost may connect). The page polls a JSON
snapshot every 2 s and posts a few actions (read, stop, add, start pairing,
remove, settings). It is bike-first: charge and status, then health,
messages, the bike's ride computer, charging and riding stats, and the
details.

## 6. Accessory identity

While advertising for live data, the add-on serves a Bluetooth Device
Information service. The Flow app shows Model Number (`2A24`) as *Part
number*, Serial Number (`2A25`) as *Serial number* and Software Revision
(`2A28`) as *Software version*; the name comes from the host adapter's
alias. Product code stays empty for accessories (System ID, IEEE and PnP ID
were tried).

## 7. Firmware

Each component reports its product name and versions. Bosch's public
software-update page lists `Name: version` lines; matching tries the exact
name, then `<name> smart system`, then for batteries `Batteries smart
system`. No match means "unknown", never a guess. Information only.

## 8. Verification

- Unit tests for every pure module.
- Integration tests: the real `bridge.py` against a fake BlueZ and a fake
  bike written independently of `mcsp.py`, on amd64 and arm64.
- Mutation checks on the safety rules, and an independent review before
  each release.
- Real-bike behaviour is checked by hand and recorded in the changelog.
