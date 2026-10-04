#!/usr/bin/env python3
"""
Solar PVGIS Hermanus simulation

Downloads one historical year of hourly PVGIS production for:
  - 3.5 kWp east-facing, 22 degree tilt
  - 3.5 kWp west-facing, 22 degree tilt

Then combines that PV profile with the most recent complete measured
household-load day in solar_meter.db, repeated for every day of the
selected PVGIS year.

The measured geyser energy is preserved each day and shifted into the
09:00-16:00 solar window, prioritising hours with PV surplus after
non-geyser household demand. Remaining geyser energy is placed within
the same window at up to the configured maximum geyser power.

This is an energy model, not a thermal tank/thermostat model.

Requires: Python 3.9+ and requests.
No pandas or other third-party package is required.
"""

import argparse
import calendar
import csv
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, PathPatch
from matplotlib.path import Path as MplPath


PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_3/seriescalc"
LAT = -34.42
LON = 19.24
TZ = ZoneInfo("Africa/Johannesburg")

EAST_KWP = 3.5
WEST_KWP = 3.5
TILT = 22
LOSS_PCT = 14
PVTECH = "crystSi"

INVERTER_KW = 8.0
BATTERY_KWH = 15.0
RESERVE_FRAC = 0.20
BATTERY_EFF = 0.95

GEYSER_START = 9
GEYSER_END = 16       # exclusive
GEYSER_MAX_KW = 3.0

# Actual historical household consumption supplied for February-October 2026.
# January, November and December are filled from the annual planning target
# below; these are explicitly provisional until measured data is available.
HISTORICAL_MONTHLY_KWH = {
    1: None,
    2: 326.0,
    3: 284.0,
    4: 343.0,
    5: 444.0,
    6: 526.0,
    7: 513.0,
    8: 484.0,
    9: 448.0,
    10: 460.0,
    11: None,
    12: None,
}
ANNUAL_PLANNING_KWH = 4930.0


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=2023,
                   help="Historical PVGIS year to simulate (default 2023)")
    p.add_argument("--db", default="solar_meter.db",
                   help="SQLite database path")
    p.add_argument("--pv-cache", default=None,
                   help="Optional directory for cached PVGIS JSON files")
    p.add_argument("--output-dir", default="solar report/pvgis",
                   help="Output directory")
    p.add_argument("--battery-kwh", type=float, default=BATTERY_KWH)
    p.add_argument("--reserve", type=float, default=RESERVE_FRAC,
                   help="Battery reserve fraction, e.g. 0.20")
    p.add_argument("--inverter-kw", type=float, default=INVERTER_KW)
    p.add_argument("--geyser-start", type=int, default=GEYSER_START)
    p.add_argument("--geyser-end", type=int, default=GEYSER_END)
    p.add_argument("--geyser-max-kw", type=float, default=GEYSER_MAX_KW)
    p.add_argument("--annual-target-kwh", type=float, default=ANNUAL_PLANNING_KWH,
                   help="Annual target used only to fill missing Jan/Nov/Dec values (default 4930 kWh)")
    p.add_argument("--missing-month-kwh", type=float, default=None,
                   help="Use this fixed kWh/month for any missing Jan/Nov/Dec values instead of deriving them")
    p.add_argument("--no-download", action="store_true",
                   help="Use cached PVGIS files only")
    return p.parse_args()


def pvgis_params(peakpower, aspect, year):
    return {
        "lat": LAT,
        "lon": LON,
        "startyear": year,
        "endyear": year,
        "pvcalculation": 1,
        "peakpower": peakpower,
        "angle": TILT,
        "aspect": aspect,              # PVGIS: -90 east, +90 west
        "loss": LOSS_PCT,
        "pvtechchoice": PVTECH,
        "mountingplace": "free",
        "usehorizon": 1,
        "outputformat": "json",
        "browser": 0,
    }


def fetch_pvgis(peakpower, aspect, year, cache_dir, no_download=False):
    name = f"pvgis_{year}_{'east' if aspect < 0 else 'west'}_{peakpower:.1f}kwp.json"
    path = cache_dir / name

    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f), path, True

    if no_download:
        raise FileNotFoundError(
            f"PVGIS cache file not found: {path}\n"
            f"Run once without --no-download to download it."
        )

    params = pvgis_params(peakpower, aspect, year)
    print(f"Downloading PVGIS {year} {'east' if aspect < 0 else 'west'} data...")
    r = requests.get(PVGIS_URL, params=params, timeout=90)
    if not r.ok:
        try:
            msg = r.json().get("message", r.text)
        except Exception:
            msg = r.text
        raise RuntimeError(f"PVGIS request failed ({r.status_code}): {msg}")
    data = r.json()
    cache_dir.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f)
    return data, path, False


def pvgis_hourly(data):
    rows = data["outputs"]["hourly"]
    out = {}
    for row in rows:
        ts = datetime.strptime(row["time"], "%Y%m%d:%H%M").replace(
            tzinfo=ZoneInfo("UTC")
        ).astimezone(TZ)
        # P is average PV power for the hour, in W.
        out[ts] = max(0.0, float(row.get("P", 0.0))) / 1000.0
    return out


def load_complete_measured_day(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT timestamp_sast, reading_date, power_a_w, power_b_w
        FROM meter_readings
        ORDER BY timestamp_sast
    """).fetchall()
    conn.close()

    by_date = defaultdict(list)
    for r in rows:
        by_date[r["reading_date"]].append(r)

    candidates = []
    for d, rs in by_date.items():
        if len(rs) < 1000:
            continue
        first = datetime.fromisoformat(rs[0]["timestamp_sast"])
        last = datetime.fromisoformat(rs[-1]["timestamp_sast"])
        if first.time() <= datetime.strptime("00:05", "%H:%M").time() and \
           last.time() >= datetime.strptime("23:55", "%H:%M").time():
            candidates.append((d, rs))

    if not candidates:
        raise RuntimeError("No complete measured day was found in meter_readings.")

    date_str, rs = sorted(candidates, key=lambda x: x[0])[-1]
    threshold = 20.0

    # Build hourly energy by splitting each valid trapezoidal interval at hour boundaries.
    hourly_total = defaultdict(float)
    hourly_geyser = defaultdict(float)

    parsed = []
    for r in rs:
        ts = datetime.fromisoformat(r["timestamp_sast"])
        a = float(r["power_a_w"] or 0.0)
        b = float(r["power_b_w"] or 0.0)
        b = max(0.0, b)
        if b < threshold:
            b = 0.0
        a = max(0.0, a)
        parsed.append((ts, a, b))

    for i in range(len(parsed) - 1):
        t0, a0, b0 = parsed[i]
        t1, a1, b1 = parsed[i + 1]
        dt = (t1 - t0).total_seconds()
        if dt <= 0 or dt > 300:
            continue

        # Linear/trapezoidal integration, split over hour boundaries.
        cur = t0
        while cur < t1:
            hour_start = cur.replace(minute=0, second=0, microsecond=0)
            hour_end = hour_start + timedelta(hours=1)
            seg_end = min(t1, hour_end)
            f0 = (cur - t0).total_seconds() / dt
            f1 = (seg_end - t0).total_seconds() / dt

            aa0 = a0 + (a1 - a0) * f0
            aa1 = a0 + (a1 - a0) * f1
            bb0 = b0 + (b1 - b0) * f0
            bb1 = b0 + (b1 - b0) * f1

            seg_hours = (seg_end - cur).total_seconds() / 3600.0
            total_kwh = ((aa0 + aa1) / 2.0) / 1000.0 * seg_hours
            geyser_kwh = ((bb0 + bb1) / 2.0) / 1000.0 * seg_hours

            h = hour_start.hour
            hourly_total[h] += total_kwh
            hourly_geyser[h] += geyser_kwh
            cur = seg_end

    hourly_non_geyser = {
        h: max(0.0, hourly_total[h] - hourly_geyser[h])
        for h in range(24)
    }

    total = sum(hourly_total.values())
    geyser = sum(hourly_geyser.values())

    return {
        "date": date_str,
        "hourly_total": dict(hourly_total),
        "hourly_geyser": dict(hourly_geyser),
        "hourly_non_geyser": hourly_non_geyser,
        "total_kwh": total,
        "geyser_kwh": geyser,
        "non_geyser_kwh": sum(hourly_non_geyser.values()),
    }


def schedule_geyser(load, pv, geyser_energy, start_hour, end_hour, max_kw):
    scheduled = {h: 0.0 for h in range(24)}
    remaining = geyser_energy

    candidates = list(range(start_hour, end_hour))
    # First use hours where PV exceeds non-geyser load.
    candidates.sort(
        key=lambda h: max(0.0, pv.get(h, 0.0) - load.get(h, 0.0)),
        reverse=True,
    )

    for h in candidates:
        if remaining <= 1e-9:
            break
        surplus = max(0.0, pv.get(h, 0.0) - load.get(h, 0.0))
        amount = min(remaining, max_kw, surplus)
        scheduled[h] += amount
        remaining -= amount

    # Preserve measured daily geyser energy. Any remaining energy is scheduled
    # at the same maximum power within the solar window, favouring higher PV.
    for h in candidates:
        if remaining <= 1e-9:
            break
        room = max_kw - scheduled[h]
        if room <= 0:
            continue
        amount = min(remaining, room)
        scheduled[h] += amount
        remaining -= amount

    if remaining > 1e-6:
        raise RuntimeError(
            f"Geyser energy {geyser_energy:.3f} kWh cannot fit into "
            f"{start_hour:02d}:00-{end_hour:02d}:00 at {max_kw:.2f} kW."
        )
    return scheduled


def build_monthly_targets(args):
    targets = dict(HISTORICAL_MONTHLY_KWH)
    missing = [m for m, v in targets.items() if v is None]

    if not missing:
        return targets, []

    if args.missing_month_kwh is not None:
        fill = args.missing_month_kwh
        method = f"fixed {fill:.1f} kWh/month"
    else:
        known_total = sum(v for v in targets.values() if v is not None)
        remaining = args.annual_target_kwh - known_total
        if remaining <= 0:
            raise ValueError(
                "Annual target is not greater than the known February-October total."
            )
        fill = remaining / len(missing)
        method = (
            f"remaining annual target ({args.annual_target_kwh:.1f} kWh) "
            f"split equally across missing months"
        )

    for m in missing:
        targets[m] = fill
    return targets, missing


def simulate_year(pv_by_local_hour, load_profile, args, year, monthly_targets):
    start_soc = args.battery_kwh
    reserve_kwh = args.battery_kwh * args.reserve

    daily = []
    annual = defaultdict(float)
    monthly = defaultdict(lambda: defaultdict(float))
    min_soc = start_soc
    max_soc = start_soc

    # 2023 has 365 days. Use the PVGIS local timestamps to enumerate dates.
    # PVGIS hourly timestamps can cross the UTC/local-time year boundary.
    # Restrict the simulation explicitly to the requested local calendar year
    # so that the model always contains exactly 365/366 local dates.
    year_start = datetime(year, 1, 1).date()
    year_end = datetime(year, 12, 31).date()
    dates = sorted({
        d for d, h in pv_by_local_hour
        if year_start <= d <= year_end
    })
    for day in dates:
        pv = {h: pv_by_local_hour.get((day, h), 0.0) for h in range(24)}

        # Scale the measured daily shape so that the month's household demand
        # matches the historical monthly consumption target. The geyser and
        # non-geyser components are scaled by the same factor, preserving the
        # measured load composition until more measured days are available.
        days_in_month = calendar.monthrange(day.year, day.month)[1]
        target_daily = monthly_targets[day.month] / days_in_month
        scale = target_daily / load_profile["total_kwh"]
        non_geyser = {h: v * scale for h, v in load_profile["hourly_non_geyser"].items()}
        geyser = load_profile["geyser_kwh"] * scale

        geyser_sched = schedule_geyser(
            non_geyser, pv, geyser,
            args.geyser_start, args.geyser_end, args.geyser_max_kw
        )

        total_load = {h: non_geyser.get(h, 0.0) + geyser_sched[h] for h in range(24)}

        day_pv = sum(pv.values())
        day_load = sum(total_load.values())
        day_geyser = sum(geyser_sched.values())

        pv_direct = 0.0
        pv_to_geyser = 0.0
        charge_input = 0.0
        discharge_output = 0.0
        battery_to_household = 0.0
        battery_to_geyser = 0.0
        grid_to_household = 0.0
        grid_to_geyser = 0.0
        grid = 0.0
        curtailed = 0.0
        peak_load = 0.0
        peak_grid = 0.0

        for h in range(24):
            p = pv[h]
            load = total_load[h]
            peak_load = max(peak_load, load)

            # Split each hour into non-geyser and geyser flows so the
            # aggregate Sankey can show the actual destination of PV,
            # battery and grid energy without double-counting the geyser.
            non_h = non_geyser.get(h, 0.0)
            geyser_h = geyser_sched[h]

            direct_non = min(non_h, p)
            remaining_pv = max(0.0, p - direct_non)
            direct_geyser = min(geyser_h, remaining_pv)
            remaining_pv = max(0.0, remaining_pv - direct_geyser)

            remaining_non = max(0.0, non_h - direct_non)
            remaining_geyser = max(0.0, geyser_h - direct_geyser)
            remaining_load = remaining_non + remaining_geyser

            pv_direct += direct_non + direct_geyser
            pv_to_geyser += direct_geyser

            battery_to_non_h = 0.0
            battery_to_geyser_h = 0.0
            grid_to_non_h = 0.0
            grid_to_geyser_h = 0.0

            if remaining_pv > 0:
                room = max(0.0, args.battery_kwh - start_soc)
                charge_input_h = min(remaining_pv, room / BATTERY_EFF)
                start_soc += charge_input_h * BATTERY_EFF
                charge_input += charge_input_h
                curtailed_h = max(0.0, remaining_pv - charge_input_h)
                curtailed += curtailed_h
            elif remaining_load > 0:
                available = max(0.0, start_soc - reserve_kwh)
                battery_draw = min(remaining_load / BATTERY_EFF, available)
                start_soc -= battery_draw
                delivered = battery_draw * BATTERY_EFF
                discharge_output += delivered

                # Household/non-geyser load gets battery energy first,
                # matching the allocation convention used in the existing
                # Sankey implementation.
                battery_to_non_h = min(remaining_non, delivered)
                battery_to_geyser_h = delivered - battery_to_non_h
                remaining_non -= battery_to_non_h
                remaining_geyser -= battery_to_geyser_h

                remaining = max(0.0, remaining_non + remaining_geyser)
                if remaining > 0:
                    grid_to_non_h = min(remaining_non, remaining)
                    grid_to_geyser_h = remaining - grid_to_non_h
                    grid += remaining
                    peak_grid = max(peak_grid, remaining)

            battery_to_household += battery_to_non_h
            battery_to_geyser += battery_to_geyser_h
            grid_to_household += grid_to_non_h
            grid_to_geyser += grid_to_geyser_h

            min_soc = min(min_soc, start_soc)
            max_soc = max(max_soc, start_soc)

        m = day.month
        annual["pv"] += day_pv
        annual["load"] += day_load
        annual["geyser"] += day_geyser
        annual["pv_direct"] += pv_direct
        annual["pv_to_geyser"] += pv_to_geyser
        annual["charge"] += charge_input
        annual["discharge"] += discharge_output
        annual["battery_to_household"] += battery_to_household
        annual["battery_to_geyser"] += battery_to_geyser
        annual["grid_to_household"] += grid_to_household
        annual["grid_to_geyser"] += grid_to_geyser
        annual["grid"] += grid
        annual["curtailed"] += curtailed

        monthly[m]["pv"] += day_pv
        monthly[m]["load"] += day_load
        monthly[m]["geyser"] += day_geyser
        monthly[m]["pv_direct"] += pv_direct
        monthly[m]["pv_to_geyser"] += pv_to_geyser
        monthly[m]["charge"] += charge_input
        monthly[m]["discharge"] += discharge_output
        monthly[m]["battery_to_household"] += battery_to_household
        monthly[m]["battery_to_geyser"] += battery_to_geyser
        monthly[m]["grid_to_household"] += grid_to_household
        monthly[m]["grid_to_geyser"] += grid_to_geyser
        monthly[m]["grid"] += grid
        monthly[m]["curtailed"] += curtailed

        daily.append({
            "date": str(day),
            "pv_kwh": day_pv,
            "load_kwh": day_load,
            "geyser_kwh": day_geyser,
            "pv_direct_kwh": pv_direct,
            "pv_to_geyser_kwh": pv_to_geyser,
            "battery_charge_kwh": charge_input,
            "battery_discharge_kwh": discharge_output,
            "battery_to_household_kwh": battery_to_household,
            "battery_to_geyser_kwh": battery_to_geyser,
            "grid_to_household_kwh": grid_to_household,
            "grid_to_geyser_kwh": grid_to_geyser,
            "grid_kwh": grid,
            "curtailed_kwh": curtailed,
            "end_soc_kwh": start_soc,
            "peak_load_kw": peak_load,
            "peak_grid_kw": peak_grid,
        })

    annual["year"] = year
    return daily, annual, monthly, min_soc, max_soc



def make_sankey_chart(out_dir, annual, start_soc, end_soc, inverter_kw, battery_kwh, reserve):
    """Create the aggregate annual Sankey using the simulation's actual flows.

    The layout follows the Sankey-style chart used in the Step 6 simulation:
    source boxes on the left, destinations on the right, smooth ribbons whose
    widths are proportional to annual kWh, and explicit battery losses/SOC
    change below the main flow.
    """
    pv_kwh = annual["pv"]
    household_kwh = annual["load"] - annual["geyser"]
    geyser_kwh = annual["geyser"]
    pv_to_household = annual["pv_direct"] - annual["pv_to_geyser"]
    pv_to_geyser = annual["pv_to_geyser"]
    battery_charge = annual["charge"]
    battery_discharge = annual["discharge"]
    battery_to_household = annual["battery_to_household"]
    battery_to_geyser = annual["battery_to_geyser"]
    grid_to_household = annual["grid_to_household"]
    grid_to_geyser = annual["grid_to_geyser"]
    grid_import = annual["grid"]
    curtailed = annual["curtailed"]

    net_soc_change = end_soc - start_soc
    battery_losses = battery_charge - battery_discharge - net_soc_change

    # Hard reconciliation checks. These make the chart fail loudly rather
    # than silently displaying an energy-flow error.
    checks = {
        "PV": pv_to_household + pv_to_geyser + battery_charge + curtailed - pv_kwh,
        "household": pv_to_household + battery_to_household + grid_to_household - household_kwh,
        "geyser": pv_to_geyser + battery_to_geyser + grid_to_geyser - geyser_kwh,
        "grid": grid_to_household + grid_to_geyser - grid_import,
        "battery": battery_to_household + battery_to_geyser - battery_discharge,
    }
    for name, error in checks.items():
        if abs(error) > 1e-6:
            raise RuntimeError(f"Sankey {name} reconciliation failed: {error:.9f} kWh")

    # Keep the same dark, compact visual language as the existing Step 6 chart.
    fig, ax = plt.subplots(figsize=(14, 7.5))
    fig.patch.set_facecolor("#11161C")
    ax.set_facecolor("#11161C")
    ax.set_xlim(0, 160)
    ax.set_ylim(0, 86)
    ax.axis("off")

    pv_color = "#F5B642"
    battery_color = "#5BC0EB"
    grid_color = "#B7BBC2"
    household_color = "#6CC070"
    geyser_color = "#E07A5F"
    curtail_color = "#777D87"
    node_edge = "#313943"
    text_primary = "#F1F3F5"
    text_muted = "#A8AFB8"

    largest = max(pv_kwh, grid_import, battery_discharge, battery_charge,
                  household_kwh, geyser_kwh, curtailed, 1.0)
    flow_scale = 31.0 / largest
    node_width = 27.0
    left_x = 6.0
    right_x = 127.0

    def h(value):
        return max(7.0, value * flow_scale)

    def node(x, cy, title, value, color):
        nh = h(value)
        patch = FancyBboxPatch(
            (x, cy - nh / 2), node_width, nh,
            boxstyle="round,pad=0.0,rounding_size=2.2",
            facecolor=color, edgecolor=node_edge, linewidth=0.6,
            alpha=0.98, zorder=3,
        )
        ax.add_patch(patch)
        ax.text(x + 2.3, cy + 2.5, title.upper(), color="#E9EDF2",
                fontsize=7.3, fontweight="bold", ha="left", va="center", zorder=5)
        ax.text(x + 2.3, cy - 1.8, f"{value:.1f}", color="#FFFFFF",
                fontsize=15, fontweight="bold", ha="left", va="center", zorder=5)
        ax.text(x + 2.3, cy - 5.2, "kWh", color="#E0E4EA",
                fontsize=7.5, ha="left", va="center", zorder=5)
        return {"x0": x, "x1": x + node_width,
                "y0": cy - nh / 2, "y1": cy + nh / 2, "h": nh}

    pv_box = node(left_x, 57, "PV", pv_kwh, pv_color)
    battery_out_box = node(left_x, 39, "Battery out", battery_discharge, battery_color)
    grid_box = node(left_x, 21, "Grid", grid_import, grid_color)

    household_box = node(right_x, 58, "Household", household_kwh, household_color)
    geyser_box = node(right_x, 40, "Geyser", geyser_kwh, geyser_color)
    battery_in_box = node(right_x, 22, "Battery in", battery_charge, battery_color)
    curtailed_box = node(right_x, 8, "Curtailed", curtailed, curtail_color) if curtailed > 1e-9 else None

    def flow_slots(box, values, total_value, top=True):
        total_value = max(float(total_value), 1e-12)
        edge_inset = min(1.4, box["h"] * 0.12)
        flow_h = max(box["h"] - 2.0 * edge_inset, 0.1)
        cursor = box["y1"] - edge_inset if top else box["y0"] + edge_inset
        out = []
        for value in values:
            fh = flow_h * max(0.0, float(value)) / total_value
            if fh <= 1e-10:
                out.append(None)
                continue
            cy = cursor - fh / 2 if top else cursor + fh / 2
            out.append((cy, fh))
            cursor = cursor - fh if top else cursor + fh
        return out

    def ribbon(box_a, slot_a, box_b, slot_b, color, alpha=0.58):
        if slot_a is None or slot_b is None:
            return
        ya, ha = slot_a
        yb, hb = slot_b
        x0 = box_a["x1"] - 0.4
        x1 = box_b["x0"] + 0.4
        c1 = x0 + (x1 - x0) * 0.42
        c2 = x0 + (x1 - x0) * 0.58

        verts = [
            (x0, ya + ha / 2),
            (c1, ya + ha / 2),
            (c2, yb + hb / 2),
            (x1, yb + hb / 2),
            (x1, yb - hb / 2),
            (c2, yb - hb / 2),
            (c1, ya - ha / 2),
            (x0, ya - ha / 2),
            (x0, ya + ha / 2),
        ]
        codes = [
            MplPath.MOVETO, MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
            MplPath.LINETO, MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
            MplPath.CLOSEPOLY,
        ]
        ax.add_patch(PathPatch(
            MplPath(verts, codes),
            facecolor=color, edgecolor="none", alpha=alpha, zorder=1,
        ))

    pv_out = flow_slots(
        pv_box, [pv_to_household, pv_to_geyser, battery_charge, curtailed],
        pv_kwh
    )
    battery_out = flow_slots(
        battery_out_box, [battery_to_household, battery_to_geyser],
        battery_discharge
    )
    grid_out = flow_slots(
        grid_box, [grid_to_household, grid_to_geyser], grid_import
    )

    house_in = flow_slots(
        household_box, [pv_to_household, battery_to_household, grid_to_household],
        household_kwh, top=False
    )
    geyser_in = flow_slots(
        geyser_box, [pv_to_geyser, battery_to_geyser, grid_to_geyser],
        geyser_kwh, top=False
    )
    batt_in = flow_slots(battery_in_box, [battery_charge], battery_charge, top=False)
    curt_in = flow_slots(curtailed_box, [curtailed], curtailed, top=False) if curtailed_box else [None]

    ribbon(pv_box, pv_out[0], household_box, house_in[0], pv_color)
    ribbon(pv_box, pv_out[1], geyser_box, geyser_in[0], pv_color, 0.50)
    ribbon(pv_box, pv_out[2], battery_in_box, batt_in[0], battery_color, 0.62)
    if curtailed_box:
        ribbon(pv_box, pv_out[3], curtailed_box, curt_in[0], curtail_color, 0.48)

    ribbon(battery_out_box, battery_out[0], household_box, house_in[1], battery_color, 0.60)
    ribbon(battery_out_box, battery_out[1], geyser_box, geyser_in[1], battery_color, 0.52)
    ribbon(grid_box, grid_out[0], household_box, house_in[2], grid_color, 0.55)
    ribbon(grid_box, grid_out[1], geyser_box, geyser_in[2], grid_color, 0.50)

    ax.text(80, 80.5, "PVGIS — Annual Aggregate Energy Flow",
            color=text_primary, fontsize=12, fontweight="bold",
            ha="center", va="center")
    ax.text(80, 77.8, "Ribbon width ∝ energy (kWh)",
            color=text_muted, fontsize=7.5, ha="center", va="center")

    if battery_losses > 1e-9:
        ax.text(80, 5.0, f"Battery losses  {battery_losses:.1f} kWh",
                color=text_muted, fontsize=7.5, ha="center", va="center")
    if abs(net_soc_change) > 1e-9:
        sign = "+" if net_soc_change > 0 else "−"
        ax.text(80, 2.8, f"Net battery SOC change  {sign}{abs(net_soc_change):.1f} kWh",
                color=text_muted, fontsize=7.2, ha="center", va="center")

    footer = f"{battery_kwh:.0f} kWh usable battery  •  {inverter_kw:.0f} kW inverter  •  {reserve * 100:.0f}% reserve"
    ax.text(80, 0.8, footer, color=text_muted, fontsize=7.2,
            ha="center", va="center")

    path = out_dir / f"pvgis {annual['year']} annual sankey.png"
    fig.savefig(path, dpi=180, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)

    csv_path = out_dir / f"pvgis {annual['year']} sankey energy flows.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["flow", "kwh"])
        w.writerows([
            ["PV generation", pv_kwh],
            ["Household load", household_kwh],
            ["Geyser load", geyser_kwh],
            ["PV to household", pv_to_household],
            ["PV to geyser", pv_to_geyser],
            ["PV to battery", battery_charge],
            ["PV curtailed", curtailed],
            ["Battery to household", battery_to_household],
            ["Battery to geyser", battery_to_geyser],
            ["Battery discharge", battery_discharge],
            ["Battery losses", battery_losses],
            ["Grid to household", grid_to_household],
            ["Grid to geyser", grid_to_geyser],
            ["Grid import", grid_import],
            ["Starting SOC", start_soc],
            ["Ending SOC", end_soc],
            ["Net SOC change", net_soc_change],
        ])
    return path, csv_path


def write_csv(path, rows, fieldnames):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main():
    args = parse_args()
    db = Path(args.db).expanduser()
    out = Path(args.output_dir).expanduser()
    cache = Path(args.pv_cache).expanduser() if args.pv_cache else out / "pvgis cache"
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    print("=" * 52)
    print("Hermanus PVGIS historical-year solar simulation")
    print("=" * 52)
    print(f"PVGIS year:                 {args.year}")
    print(f"Location:                   {LAT:.4f}, {LON:.4f}")
    print(f"PV:                         7.00 kWp (3.5 east + 3.5 west)")
    print(f"Roof tilt:                  {TILT}°")
    print(f"PVGIS system losses:        {LOSS_PCT}%")
    print(f"Inverter:                   {args.inverter_kw:.2f} kW")
    print(f"Battery usable:              {args.battery_kwh:.2f} kWh")
    print(f"Battery reserve:             {args.reserve * 100:.1f}%")
    print(f"Geyser window:              {args.geyser_start:02d}:00-{args.geyser_end:02d}:00")
    print("Monthly household-demand model:")

    east_data, east_path, east_cached = fetch_pvgis(
        EAST_KWP, -90, args.year, cache, args.no_download
    )
    west_data, west_path, west_cached = fetch_pvgis(
        WEST_KWP, 90, args.year, cache, args.no_download
    )

    east = pvgis_hourly(east_data)
    west = pvgis_hourly(west_data)

    pv_by_local_hour = {}
    for ts, p in east.items():
        pv_by_local_hour[(ts.date(), ts.hour)] = p + west.get(ts, 0.0)

    load_profile = load_complete_measured_day(db)
    monthly_targets, provisional_months = build_monthly_targets(args)
    print(f"Measured load day:          {load_profile['date']}")
    print(f"Measured daily load:        {load_profile['total_kwh']:.3f} kWh")
    print(f"Measured geyser:             {load_profile['geyser_kwh']:.3f} kWh")
    print(f"Measured non-geyser:         {load_profile['non_geyser_kwh']:.3f} kWh")
    print(f"Annual planning target:      {args.annual_target_kwh:.1f} kWh")
    print("Month  Target kWh  Basis")
    print("-----  ----------  ---------------------------------------------")
    for m in range(1, 13):
        basis = "historical" if m not in provisional_months else "provisional"
        print(f"{m:>5}  {monthly_targets[m]:>10.1f}  {basis}")
    if provisional_months:
        if args.missing_month_kwh is None:
            print(f"Provisional months {provisional_months}: remaining annual target split equally.")
        else:
            print(f"Provisional months {provisional_months}: fixed {args.missing_month_kwh:.1f} kWh/month.")
    print()

    daily, annual, monthly, min_soc, max_soc = simulate_year(
        pv_by_local_hour, load_profile, args, args.year, monthly_targets
    )

    daily_fields = list(daily[0].keys())
    write_csv(out / f"pvgis {args.year} daily simulation.csv", daily, daily_fields)

    monthly_rows = []
    for m in range(1, 13):
        d = monthly[m]
        days = len([x for x in daily if datetime.fromisoformat(x["date"]).month == m])
        row = {"month": m, "target_load_kwh": monthly_targets[m], "days": days}
        row.update({k + "_kwh": d[k] for k in
                    ["pv", "load", "geyser", "pv_direct", "pv_to_geyser",
                     "charge", "discharge", "grid", "curtailed"]})
        monthly_rows.append(row)
    write_csv(out / f"pvgis {args.year} monthly simulation.csv",
              monthly_rows, list(monthly_rows[0].keys()))

    worst = sorted(daily, key=lambda x: x["grid_kwh"], reverse=True)[:10]
    worst_soc = sorted(daily, key=lambda x: x["end_soc_kwh"])[:10]
    write_csv(out / f"pvgis {args.year} worst grid days.csv", worst, daily_fields)
    write_csv(out / f"pvgis {args.year} lowest SOC days.csv", worst_soc, daily_fields)

    sankey_path, sankey_csv = make_sankey_chart(
        out, annual, args.battery_kwh, daily[-1]["end_soc_kwh"],
        args.inverter_kw, args.battery_kwh, args.reserve
    )
    print(f"Sankey chart:               {sankey_path}")
    print(f"Sankey flow data:            {sankey_csv}")
    print()

    print("Monthly results:")
    print("Month  PV kWh  Load kWh  Grid kWh  End-of-month SOC")
    print("-----  ------  --------  --------  ----------------")
    for row in monthly_rows:
        month = row["month"]
        end_day = [x for x in daily if datetime.fromisoformat(x["date"]).month == month][-1]
        print(f"{month:>5}  {row['pv_kwh']:>6.1f}  {row['load_kwh']:>8.1f}  "
              f"{row['grid_kwh']:>8.1f}  {end_day['end_soc_kwh']:>16.2f}")

    print()
    print("Annual results:")
    print(f"PV generation:              {annual['pv']:.1f} kWh")
    print(f"Household load:             {annual['load']:.1f} kWh")
    print(f"Geyser energy:              {annual['geyser']:.1f} kWh")
    print(f"Direct PV to load:          {annual['pv_direct']:.1f} kWh")
    print(f"Direct PV to geyser:        {annual['pv_to_geyser']:.1f} kWh")
    print(f"Battery charge input:       {annual['charge']:.1f} kWh")
    print(f"Battery discharge output:   {annual['discharge']:.1f} kWh")
    print(f"Grid import:                {annual['grid']:.1f} kWh")
    print(f"PV curtailed:               {annual['curtailed']:.1f} kWh")
    print(f"Minimum battery SOC:        {min_soc:.2f} kWh")
    print(f"Maximum battery SOC:        {max_soc:.2f} kWh")
    print(f"Peak measured load remains: 6.203 kW reference from current dataset")
    print()

    # Identify the most stressful consecutive 14-day periods by grid import.
    # This is more useful for winter battery sizing than a monthly total alone.
    window = 14
    if len(daily) >= window:
        windows = []
        for i in range(len(daily) - window + 1):
            chunk = daily[i:i + window]
            windows.append((
                sum(x["grid_kwh"] for x in chunk),
                sum(x["pv_kwh"] for x in chunk),
                min(x["end_soc_kwh"] for x in chunk),
                chunk[0]["date"],
                chunk[-1]["date"],
            ))
        windows.sort(reverse=True)
        print(f"Worst {window}-day periods by grid import:")
        print("Start       End         Grid kWh  PV kWh  Min end SOC")
        print("----------  ----------  --------  ------  -----------")
        for w in windows[:10]:
            print(f"{w[3]}  {w[4]}  {w[0]:>8.1f}  {w[1]:>6.1f}  {w[2]:>11.2f}")
        print()

    print("Lowest-SOC / highest-grid winter days:")
    winter_days = [x for x in daily if 5 <= datetime.fromisoformat(x["date"]).month <= 8]
    winter_days.sort(key=lambda x: (-x["grid_kwh"], x["end_soc_kwh"]))
    print("Date        PV kWh  Grid kWh  End SOC  Geyser kWh")
    print("----------  ------  --------  -------  ----------")
    for x in winter_days[:15]:
        print(f"{x['date']}  {x['pv_kwh']:>6.1f}  {x['grid_kwh']:>8.1f}  "
              f"{x['end_soc_kwh']:>7.2f}  {x['geyser_kwh']:>10.2f}")
    print()
    print("PVGIS source:")
    print("  Joint Research Centre / European Commission, PVGIS 5.3")
    print(f"  East cache: {east_path}")
    print(f"  West cache: {west_path}")
    print()
    print("Important limitations:")
    print("  * PVGIS provides the historical solar/weather profile; it is not a")
    print("    site measurement at the house.")
    print("  * The household load shape is the most recent complete measured day")
    print("    scaled to the supplied monthly consumption curve. As more meter")
    print("    data accumulates, this should be replaced with measured daily")
    print("    profiles and measured seasonal behaviour.")
    print("  * February-October monthly targets are historical values supplied by")
    print("    the user. January/November/December are provisional until measured")
    print("    values are available.")
    print("  * The geyser is modelled as an energy-shiftable load, not as a")
    print("    physical hot-water tank with thermostat cycling.")
    print("  * Zero export is assumed; excess PV is curtailed once the battery")
    print("    is full and household load is satisfied.")

if __name__ == "__main__":
    main()
