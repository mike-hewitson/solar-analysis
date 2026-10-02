#!/usr/bin/env python3

"""
Solar Step 6 — measured-load PV / battery simulation

Version 2 fixes the load-handling problem in the first solar-shift version.
The simulation now works directly from the irregular high-resolution meter
samples instead of resampling the whole day to one-minute values. This keeps
measured daily energy intact and avoids inventing load between the first
sample and midnight.

Solar-shift mode preserves the measured daily geyser energy and reallocates it
into the requested daytime window, preferentialentially selecting intervals
with the greatest estimated PV surplus after the non-geyser household load.

The PV curve remains a transparent synthetic east/west clear-sky test curve;
it is not a site-specific Hermanus/PVGIS production model.
"""

from pathlib import Path
import argparse
import configparser
import csv
import math
import sqlite3
from datetime import datetime

SCRIPT_DIR = Path(__file__).resolve().parent


def load_db_path():
    config_path = SCRIPT_DIR / "config.ini"
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    config = configparser.ConfigParser()
    config.read(config_path)
    db_path = Path(config.get("database", "path"))
    if not db_path.is_absolute():
        db_path = SCRIPT_DIR / db_path
    return db_path


def args():
    p = argparse.ArgumentParser(description="Simulate measured household load against east/west PV and battery.")
    p.add_argument("--pv", type=float, default=7.0)
    p.add_argument("--east", type=float, default=3.5)
    p.add_argument("--west", type=float, default=3.5)
    p.add_argument("--inverter", type=float, default=8.0)
    p.add_argument("--battery", type=float, default=15.0)
    p.add_argument("--reserve", type=float, default=20.0)
    p.add_argument("--initial-soc", type=float, default=None,
                    help="Starting battery SOC in kWh. Default: full battery.")
    p.add_argument("--pv-yield", type=float, default=4.5,
                    help="Clear-sky daily PV yield in kWh/kWp/day.")
    p.add_argument("--pv-yields", type=str, default=None,
                    help="Optional comma-separated daily PV yields, one per simulated day. "
                         "The final value is repeated if fewer values are supplied.")
    p.add_argument("--repeat-days", type=int, default=1,
                    help="Repeat the selected measured load profile for this many consecutive simulated days. "
                         "Useful for multi-day weather/SOC scenarios when only one complete measured day is available.")
    p.add_argument("--geyser-mode", choices=["measured", "solar-shift"], default="solar-shift")
    p.add_argument("--geyser-start", type=float, default=9.0)
    p.add_argument("--geyser-end", type=float, default=16.0)
    p.add_argument("--geyser-max-kw", type=float, default=3.0)
    return p.parse_args()


def pv_shape(hour_decimal, orientation):
    if orientation == "east":
        peak, width, start, end = 11.0, 3.1, 5.0, 17.5
    else:
        peak, width, start, end = 13.5, 3.2, 6.0, 19.0
    if hour_decimal < start or hour_decimal > end:
        return 0.0
    x = (hour_decimal - peak) / width
    return math.exp(-0.5 * x * x)


def normalise_shape(orientation):
    total = 0.0
    for minute in range(1440):
        total += pv_shape(minute / 60.0, orientation) / 60.0
    return total


EAST_NORM = normalise_shape("east")
WEST_NORM = normalise_shape("west")


def pv_power_kw(hour_decimal, east_kwp, west_kwp, daily_yield_kwh_per_kwp):
    east = pv_shape(hour_decimal, "east") / EAST_NORM * daily_yield_kwh_per_kwp * east_kwp
    west = pv_shape(hour_decimal, "west") / WEST_NORM * daily_yield_kwh_per_kwp * west_kwp
    return east + west


def fetch_complete_days(con):
    return [r[0] for r in con.execute("""
        SELECT reading_date FROM daily_load_summary
        WHERE is_complete_day = 1 ORDER BY reading_date
    """).fetchall()]


def fetch_day(con, date):
    return con.execute("""
        SELECT timestamp_sast, total_power_w, geyser_power_w, non_geyser_power_w
        FROM v_meter_readings_clean
        WHERE reading_date = ? ORDER BY timestamp_sast
    """, (date,)).fetchall()


def prepare_intervals(rows):
    """Return exact intervals between raw meter samples."""
    parsed = []
    for r in rows:
        parsed.append((
            datetime.fromisoformat(r[0]),
            float(r[1] or 0),
            float(r[2] or 0),
            float(r[3] or 0),
        ))

    intervals = []
    for i in range(len(parsed) - 1):
        t0, a0, g0, n0 = parsed[i]
        t1, a1, g1, n1 = parsed[i + 1]
        dt = (t1 - t0).total_seconds()
        if dt <= 0 or dt > 300:
            continue
        mid = t0 + (t1 - t0) / 2
        intervals.append({
            "t0": t0,
            "t1": t1,
            "mid": mid,
            "dt_h": dt / 3600.0,
            "total0_w": max(a0, 0.0),
            "total1_w": max(a1, 0.0),
            "geyser0_w": max(g0, 0.0),
            "geyser1_w": max(g1, 0.0),
            "non0_w": max(n0, 0.0),
            "non1_w": max(n1, 0.0),
        })
    return intervals


def trap_energy_kwh(w0, w1, dt_h):
    return ((w0 + w1) / 2.0) / 1000.0 * dt_h


def build_solar_shift_geyser_schedule(intervals, east_kwp, west_kwp,
                                       daily_yield, start_hour, end_hour,
                                       max_geyser_kw):
    """Shift the measured daily geyser energy into the best solar intervals."""
    target_kwh = sum(trap_energy_kwh(x["geyser0_w"], x["geyser1_w"], x["dt_h"]) for x in intervals)

    candidates = []
    for i, x in enumerate(intervals):
        h = x["mid"].hour + x["mid"].minute / 60.0 + x["mid"].second / 3600.0
        if start_hour <= h < end_hour:
            non_kw = ((x["non0_w"] + x["non1_w"]) / 2.0) / 1000.0
            pv_kw = pv_power_kw(h, east_kwp, west_kwp, daily_yield)
            surplus = max(0.0, pv_kw - non_kw)
            candidates.append((surplus, pv_kw, i))

    # Highest PV surplus first. We first allocate what can be supplied directly
    # by PV surplus, then use remaining capacity in the same solar window.
    candidates.sort(key=lambda z: (z[0], z[1]), reverse=True)
    schedule_kw = [0.0] * len(intervals)
    remaining = target_kwh

    for surplus, pv_kw, i in candidates:
        if remaining <= 1e-12:
            break
        dt_h = intervals[i]["dt_h"]
        power_kw = min(max_geyser_kw, surplus)
        energy = min(remaining, power_kw * dt_h)
        if energy > 0:
            schedule_kw[i] = energy / dt_h
            remaining -= energy

    # Guaranteed preservation of the measured daily energy provided the window
    # has sufficient physical capacity. Fill any remaining energy at up to the
    # geyser's maximum rating; this may draw from battery/grid if PV is low.
    for surplus, pv_kw, i in candidates:
        if remaining <= 1e-12:
            break
        dt_h = intervals[i]["dt_h"]
        spare_kw = max(0.0, max_geyser_kw - schedule_kw[i])
        energy = min(remaining, spare_kw * dt_h)
        if energy > 0:
            schedule_kw[i] += energy / dt_h
            remaining -= energy

    capacity_kwh = sum(max_geyser_kw * x["dt_h"] for _, _, i in candidates for x in [intervals[i]])
    if remaining > 1e-7:
        raise RuntimeError(
            f"Solar-shift geyser window cannot accommodate measured energy: "
            f"target={target_kwh:.4f} kWh, capacity={capacity_kwh:.4f} kWh, "
            f"unallocated={remaining:.4f} kWh"
        )

    scheduled_kwh = sum(schedule_kw[i] * intervals[i]["dt_h"] for i in range(len(intervals)))
    if abs(scheduled_kwh - target_kwh) > 1e-6:
        raise RuntimeError(
            f"Geyser energy reconciliation failed: measured={target_kwh:.6f} kWh, "
            f"scheduled={scheduled_kwh:.6f} kWh"
        )
    return schedule_kw, target_kwh


def simulate_day(intervals, east_kwp, west_kwp, inverter_kw,
                 battery_kwh, reserve_pct, daily_yield,
                 geyser_mode, geyser_start, geyser_end, geyser_max_kw,
                 initial_soc_kwh):
    reserve_kwh = battery_kwh * reserve_pct / 100.0
    soc = min(max(initial_soc_kwh, reserve_kwh), battery_kwh)
    starting_soc_kwh = soc

    measured_geyser_kwh = sum(trap_energy_kwh(x["geyser0_w"], x["geyser1_w"], x["dt_h"]) for x in intervals)
    measured_total_kwh = sum(trap_energy_kwh(x["total0_w"], x["total1_w"], x["dt_h"]) for x in intervals)
    measured_non_kwh = sum(trap_energy_kwh(x["non0_w"], x["non1_w"], x["dt_h"]) for x in intervals)

    if geyser_mode == "solar-shift":
        geyser_schedule, schedule_target = build_solar_shift_geyser_schedule(
            intervals, east_kwp, west_kwp, daily_yield,
            geyser_start, geyser_end, geyser_max_kw
        )
    else:
        geyser_schedule = [
            ((x["geyser0_w"] + x["geyser1_w"]) / 2.0) / 1000.0
            for x in intervals
        ]
        schedule_target = measured_geyser_kwh

    scheduled_geyser_kwh = sum(geyser_schedule[i] * x["dt_h"] for i, x in enumerate(intervals))
    expected_shifted_load_kwh = measured_non_kwh + scheduled_geyser_kwh

    metrics = {
        "pv_kwh": 0.0, "load_kwh": 0.0,
        "measured_load_kwh": measured_total_kwh,
        "measured_non_geyser_kwh": measured_non_kwh,
        "measured_geyser_kwh": measured_geyser_kwh,
        "scheduled_geyser_kwh": scheduled_geyser_kwh,
        "simulated_geyser_kwh": 0.0,
        "direct_pv_kwh": 0.0, "pv_to_geyser_kwh": 0.0,
        "battery_charge_kwh": 0.0, "battery_discharge_kwh": 0.0,
        "grid_import_kwh": 0.0, "pv_curtailed_kwh": 0.0,
        "max_load_kw": 0.0, "max_grid_kw": 0.0,
        "measured_peak_kw": max(max(x["total0_w"] for x in intervals), max(x["total1_w"] for x in intervals)) / 1000.0 if intervals else 0.0,
        "min_soc_kwh": soc, "max_soc_kwh": soc,
        "starting_soc_kwh": starting_soc_kwh,
        "ending_soc_kwh": soc,
    }

    charge_eff = 0.95
    discharge_eff = 0.95
    out = []

    for i, x in enumerate(intervals):
        dt_h = x["dt_h"]
        h = x["mid"].hour + x["mid"].minute / 60.0 + x["mid"].second / 3600.0
        non_kw = ((x["non0_w"] + x["non1_w"]) / 2.0) / 1000.0
        geyser_kw = geyser_schedule[i]
        load_kw = non_kw + geyser_kw
        pv_raw = pv_power_kw(h, east_kwp, west_kwp, daily_yield)
        pv_kw = min(pv_raw, inverter_kw)

        metrics["pv_kwh"] += pv_kw * dt_h
        metrics["load_kwh"] += load_kw * dt_h
        metrics["simulated_geyser_kwh"] += geyser_kw * dt_h
        metrics["max_load_kw"] = max(metrics["max_load_kw"], load_kw)

        direct_non = min(non_kw, pv_kw)
        remaining_pv = pv_kw - direct_non
        direct_geyser = min(geyser_kw, remaining_pv)
        remaining_pv -= direct_geyser
        remaining_load = (non_kw - direct_non) + (geyser_kw - direct_geyser)

        space = max(0.0, battery_kwh - soc)
        charge_kw = min(remaining_pv, space / (dt_h * charge_eff)) if dt_h > 0 else 0.0
        soc += charge_kw * dt_h * charge_eff
        remaining_pv -= charge_kw

        available = max(0.0, soc - reserve_kwh)
        discharge_kw = min(remaining_load, available * discharge_eff / dt_h) if dt_h > 0 else 0.0
        soc -= discharge_kw * dt_h / discharge_eff
        remaining_load -= discharge_kw
        grid_kw = max(0.0, remaining_load)

        metrics["direct_pv_kwh"] += (direct_non + direct_geyser) * dt_h
        metrics["pv_to_geyser_kwh"] += direct_geyser * dt_h
        metrics["battery_charge_kwh"] += charge_kw * dt_h
        metrics["battery_discharge_kwh"] += discharge_kw * dt_h
        metrics["grid_import_kwh"] += grid_kw * dt_h
        metrics["pv_curtailed_kwh"] += remaining_pv * dt_h
        metrics["max_grid_kw"] = max(metrics["max_grid_kw"], grid_kw)
        metrics["min_soc_kwh"] = min(metrics["min_soc_kwh"], soc)
        metrics["max_soc_kwh"] = max(metrics["max_soc_kwh"], soc)

        out.append({
            "timestamp": x["mid"].isoformat(sep=" "),
            "interval_seconds": x["dt_h"] * 3600,
            "non_geyser_kw": non_kw,
            "measured_geyser_kw": ((x["geyser0_w"] + x["geyser1_w"]) / 2.0) / 1000.0,
            "simulated_geyser_kw": geyser_kw,
            "load_kw": load_kw,
            "pv_kw": pv_kw,
            "direct_pv_kw": direct_non + direct_geyser,
            "pv_to_geyser_kw": direct_geyser,
            "battery_charge_kw": charge_kw,
            "battery_discharge_kw": discharge_kw,
            "grid_kw": grid_kw,
            "battery_soc_kwh": soc,
            "pv_curtailed_kw": remaining_pv,
        })

    metrics["ending_soc_kwh"] = soc

    # These are deliberately hard checks. The shifted load must preserve the
    # measured daily energy, and the simulated household load must equal
    # measured non-geyser energy plus the preserved geyser energy.
    if abs(metrics["scheduled_geyser_kwh"] - measured_geyser_kwh) > 1e-6:
        raise RuntimeError("Internal error: scheduled geyser energy does not equal measured geyser energy.")
    if abs(metrics["load_kwh"] - expected_shifted_load_kwh) > 1e-6:
        raise RuntimeError("Internal error: simulated load does not reconcile to measured non-geyser + geyser energy.")

    return metrics, out


def write_day_csv(path, rows):
    fields = list(rows[0].keys()) if rows else []
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    a = args()
    if abs((a.east + a.west) - a.pv) > 0.001:
        raise SystemExit(f"PV mismatch: east ({a.east}) + west ({a.west}) must equal total PV ({a.pv})")
    if a.reserve < 0 or a.reserve >= 100:
        raise SystemExit("Reserve must be between 0 and <100 percent.")
    if a.repeat_days < 1:
        raise SystemExit("--repeat-days must be at least 1.")
    if a.geyser_end <= a.geyser_start:
        raise SystemExit("Geyser end hour must be later than start hour.")
    if a.pv_yield <= 0:
        raise SystemExit("PV daily yield must be greater than zero.")
    if a.initial_soc is not None and (a.initial_soc < 0 or a.initial_soc > a.battery):
        raise SystemExit("Initial SOC must be between 0 and the usable battery capacity.")

    if a.pv_yields:
        try:
            pv_yields = [float(v.strip()) for v in a.pv_yields.split(",") if v.strip()]
        except ValueError:
            raise SystemExit("--pv-yields must be a comma-separated list of numbers.")
        if not pv_yields or any(v <= 0 for v in pv_yields):
            raise SystemExit("--pv-yields must contain only positive numbers.")
    else:
        pv_yields = [a.pv_yield]

    db_path = load_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    out_dir = SCRIPT_DIR / "solar report"
    out_dir.mkdir(exist_ok=True)

    with sqlite3.connect(db_path) as con:
        dates = fetch_complete_days(con)
        if not dates:
            raise RuntimeError("No complete days available for simulation.")

        if a.repeat_days > 1:
            # Use the most recent complete measured day as the representative
            # household-load profile and repeat it for consecutive simulated days.
            source_date = dates[-1]
            measured_profiles = [(source_date, prepare_intervals(fetch_day(con, source_date)))]
        else:
            measured_profiles = [(dates[-1], prepare_intervals(fetch_day(con, dates[-1])))]

        starting_soc = a.battery if a.initial_soc is None else a.initial_soc
        summary = []

        for day_index in range(a.repeat_days):
            source_date, intervals = measured_profiles[0]
            daily_yield = pv_yields[min(day_index, len(pv_yields) - 1)]

            metrics, detail = simulate_day(
                intervals, a.east, a.west, a.inverter, a.battery,
                a.reserve, daily_yield, a.geyser_mode,
                a.geyser_start, a.geyser_end, a.geyser_max_kw,
                starting_soc
            )

            sim_label = source_date if a.repeat_days == 1 else f"Day {day_index + 1}"
            summary.append((sim_label, daily_yield, metrics))
            write_day_csv(
                out_dir / f"solar simulation {sim_label}.csv",
                detail
            )

            starting_soc = metrics["ending_soc_kwh"]

    n = len(summary)

    def avg(key):
        return sum(m[key] for _, _, m in summary) / n

    total = lambda key: sum(m[key] for _, _, m in summary)

    print("\n========================================")
    print("Solar Step 6 measured-load simulation — continuous battery SOC")
    print("========================================")
    print(f"Simulated days:               {n}")
    print()
    print("System case:")
    print(f"  PV total:                  {a.pv:6.2f} kWp")
    print(f"    East:                    {a.east:6.2f} kWp")
    print(f"    West:                    {a.west:6.2f} kWp")
    print(f"  Inverter:                  {a.inverter:6.2f} kW")
    print(f"  Battery usable:            {a.battery:6.2f} kWh")
    print(f"  Battery reserve:           {a.reserve:6.1f}%")
    print(f"  Initial battery SOC:       {(a.battery if a.initial_soc is None else a.initial_soc):6.3f} kWh")
    print(f"  Geyser mode:               {a.geyser_mode}")
    if a.geyser_mode == "solar-shift":
        print(f"    Heating window:          {a.geyser_start:5.1f}–{a.geyser_end:5.1f}")
        print(f"    Maximum geyser power:    {a.geyser_max_kw:5.2f} kW")
    print(f"  Measured load profile:      most recent complete day, repeated {a.repeat_days} time(s)")
    if a.pv_yields:
        print(f"  PV yield sequence:         {', '.join(f'{v:.2f}' for v in pv_yields)} kWh/kWp/day")
    print()
    print("Continuous SOC / daily results:")

    header = (
        "  Date        PV yield  Start SOC  Load kWh  Geyser  PV→load  "
        "Bat charge  Bat discharge  Grid  End SOC"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for date, daily_yield, m in summary:
        print(
            f"  {date}    {daily_yield:7.2f}    {m['starting_soc_kwh']:8.3f}"
            f"   {m['load_kwh']:7.3f}  {m['scheduled_geyser_kwh']:6.3f}"
            f"   {m['direct_pv_kwh']:7.3f}     {m['battery_charge_kwh']:7.3f}"
            f"       {m['battery_discharge_kwh']:7.3f}  {m['grid_import_kwh']:5.3f}"
            f"  {m['ending_soc_kwh']:7.3f}"
        )

    print()
    print("Aggregate / average results:")
    print(f"  Average household load:    {avg('load_kwh'):8.3f} kWh/day")
    print(f"  Average measured geyser:   {avg('measured_geyser_kwh'):8.3f} kWh/day")
    print(f"  Average PV generation:     {avg('pv_kwh'):8.3f} kWh/day")
    print(f"  Average direct PV to load: {avg('direct_pv_kwh'):8.3f} kWh/day")
    print(f"  Average PV to geyser:      {avg('pv_to_geyser_kwh'):8.3f} kWh/day")
    print(f"  Average battery charge:    {avg('battery_charge_kwh'):8.3f} kWh/day")
    print(f"  Average battery discharge: {avg('battery_discharge_kwh'):8.3f} kWh/day")
    print(f"  Average grid import:       {avg('grid_import_kwh'):8.3f} kWh/day")
    print(f"  Average PV curtailed:      {avg('pv_curtailed_kwh'):8.3f} kWh/day")
    print()
    print("Battery / power indicators:")
    print(f"  Initial SOC:               {summary[0][2]['starting_soc_kwh']:8.3f} kWh")
    print(f"  Final SOC:                 {summary[-1][2]['ending_soc_kwh']:8.3f} kWh")
    print(f"  Minimum SOC:               {min(m['min_soc_kwh'] for _, _, m in summary):8.3f} kWh")
    print(f"  Maximum SOC:               {max(m['max_soc_kwh'] for _, _, m in summary):8.3f} kWh")
    print(f"  Measured peak load:        {max(m['measured_peak_kw'] for _, _, m in summary):8.3f} kW")
    print(f"  Simulated peak load:       {max(m['max_load_kw'] for _, _, m in summary):8.3f} kW")
    print(f"  Peak grid import:          {max(m['max_grid_kw'] for _, _, m in summary):8.3f} kW")
    print()
    print("Energy totals:")
    print(f"  Total PV generation:       {total('pv_kwh'):8.3f} kWh")
    print(f"  Total household load:      {total('load_kwh'):8.3f} kWh")
    print(f"  Total grid import:         {total('grid_import_kwh'):8.3f} kWh")
    print()
    print("Checks:")
    print("  Geyser energy preserved:    YES")
    print("  Household energy reconciled: YES")
    print("  Battery SOC carried day-to-day: YES")
    if a.repeat_days > 1:
        print("  Repeated measured load profile: YES")
    print()
    print("Important:")
    print("  The PV curve is a transparent clear-sky test curve, not a site-specific")
    print("  weather/PVGIS result. The daily-yield value is a scenario assumption.")
    print("  Use --pv-yields to apply a different yield to successive days.")
    print("  Use --repeat-days N to repeat the most recent complete measured load profile N days.")
    print(f"\nDetailed CSVs: {out_dir}\n")


if __name__ == "__main__":
    main()
