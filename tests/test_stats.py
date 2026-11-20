"""Tests for the charging and riding history (stats.py)."""
import stats

H = 3600


def test_a_charge_and_the_ride_before_it():
    h = {}
    stats.update(h, 0, soc=100, charger=False, km=64.4)             # unplugged, full
    stats.update(h, 10 * H, soc=60, charger=False, km=80.0)         # riding
    assert stats.update(h, 20 * H, soc=10, charger=True, km=92.4)   # plugged in: ride closed
    stats.update(h, 22 * H, soc=55)
    stats.update(h, 24.42 * H, soc=100)                              # full after 4 h 25 min
    stats.update(h, 30 * H, charger=False)                           # unplugged
    s = stats.summary(h, usable_wh=769.4)
    assert s["last_charge"]["start_soc"] == 10 and s["last_charge"]["end_soc"] == 100
    assert s["last_charge"]["minutes"] == 265                        # 4 h 25 min
    assert s["full_charge_minutes"] == round(100 / (90 / 4.42) * 60)
    ride = s["last_ride"]
    assert ride["km"] == 28.0 and ride["used_pct"] == 90
    assert ride["wh"] == 692 and ride["wh_per_km"] == 24.7
    assert s["wh_per_km"] == 24.7 and s["range_full_km"] == 31
    assert s["charging"] is None and s["charges"] == 1


def test_time_to_full_while_charging():
    h = {}
    stats.update(h, 0, soc=10, charger=True, km=1)
    stats.update(h, 4.5 * H, soc=100)
    stats.update(h, 5 * H, charger=False)
    stats.update(h, 10 * H, soc=40, charger=True)
    stats.update(h, 11 * H, soc=60)
    s = stats.summary(h)
    assert s["charging"]["soc"] == 60 and s["charging"]["minutes_to_full"] == 120  # 20 %/h


def test_a_missed_unplug_is_noticed_by_the_lower_charge():
    h = {}
    stats.update(h, 0, soc=30, charger=True, km=10)
    stats.update(h, 3 * H, soc=100, km=10)
    stats.update(h, 30 * H, soc=40, charger=True, km=35)  # rode 25 km, never saw unplugging
    s = stats.summary(h, usable_wh=800)
    assert s["charges"] == 1 and s["last_ride"]["km"] == 25 and s["last_ride"]["used_pct"] == 60
    assert s["charging"]["start_soc"] == 40


def test_top_ups_and_no_history_stay_quiet():
    h = {}
    assert stats.summary(h)["last_charge"] is None
    stats.update(h, 0, soc=98, charger=True, km=5)
    stats.update(h, H, soc=100)
    stats.update(h, 2 * H, charger=False)
    s = stats.summary(h)
    assert s["charges"] == 0 and s["full_charge_minutes"] is None   # 2 % isn't a charge


def test_a_plug_in_uses_the_odometer_that_arrives_a_moment_after_it():
    """A full reading: 'charger connected' first, then charge and odometer."""
    h = {}
    stats.update(h, 0, soc=100, charger=False, km=64.36)          # unplugged, full
    stats.update(h, 30 * H, charger=True)                          # plugged in (alone)
    stats.update(h, 30 * H + 1, soc=10)
    stats.update(h, 30 * H + 2, km=92.42)
    s = stats.summary(h, usable_wh=769.4)
    assert s["last_ride"]["km"] == 28.06 and s["last_ride"]["used_pct"] == 90


def test_a_finished_charge_counts_while_still_plugged_in():
    h = {}
    stats.update(h, 0, soc=10, charger=True, km=92)
    stats.update(h, 4.42 * H, soc=100)
    s = stats.summary(h)
    assert s["last_charge"]["minutes"] == 265 and s["charges"] == 1
    assert s["charging"]["full"] is True
