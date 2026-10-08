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

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, PathPatch
from matplotlib.path import Path as MplPath

SCRIPT_DIR = Path(__file__).resolve().parent


def load_config():
    config_path = SCRIPT_DIR / "config.ini"
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    config = configparser.ConfigParser()
    config.read(config_path)
    return config


def load_db_path(config):
    db_path = Path(config.get("database", "path"))
    if not db_path.is_absolute():
        db_path = SCRIPT_DIR / db_path
    return db_path


def load_solar_defaults(config):
    section = "solar"
    required = [
        "pv_total_kwp",
        "pv_east_kwp",
        "pv_west_kwp",
        "inverter_kw",
        "battery_usable_kwh",
        "battery_reserve_percent",
        "initial_battery_soc_kwh",
    ]
    missing = [key for key in required if not config.has_option(section, key)]
    if missing:
        raise configparser.Error(
            f"Missing required [{section}] setting(s): {', '.join(missing)}"
        )

    return {
        "pv": config.getfloat(section, "pv_total_kwp"),
        "east": config.getfloat(section, "pv_east_kwp"),
        "west": config.getfloat(section, "pv_west_kwp"),
        "inverter": config.getfloat(section, "inverter_kw"),
        "battery": config.getfloat(section, "battery_usable_kwh"),
        "reserve": config.getfloat(section, "battery_reserve_percent"),
        "initial_soc": config.getfloat(section, "initial_battery_soc_kwh"),
    }


def args(config):
    defaults = load_solar_defaults(config)
    p = argparse.ArgumentParser(
        description="Simulate measured household load against east/west PV and battery."
    )
    p.add_argument("--pv", type=float, default=defaults["pv"])
    p.add_argument("--east", type=float, default=defaults["east"])
    p.add_argument("--west", type=float, default=defaults["west"])
    p.add_argument("--inverter", type=float, default=defaults["inverter"])
    p.add_argument("--battery", type=float, default=defaults["battery"])
    p.add_argument("--reserve", type=float, default=defaults["reserve"])
    p.add_argument(
        "--initial-soc", type=float, default=defaults["initial_soc"],
        help="Starting battery SOC in kWh. Default: value from [solar] initial_battery_soc_kwh."
    )
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
        "battery_to_household_kwh": 0.0, "battery_to_geyser_kwh": 0.0,
        "grid_to_household_kwh": 0.0, "grid_to_geyser_kwh": 0.0,
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
        remaining_non = non_kw - direct_non
        remaining_geyser = geyser_kw - direct_geyser
        remaining_load = remaining_non + remaining_geyser

        space = max(0.0, battery_kwh - soc)
        charge_kw = min(remaining_pv, space / (dt_h * charge_eff)) if dt_h > 0 else 0.0
        soc += charge_kw * dt_h * charge_eff
        remaining_pv -= charge_kw

        available = max(0.0, soc - reserve_kwh)
        discharge_kw = min(remaining_load, available * discharge_eff / dt_h) if dt_h > 0 else 0.0
        soc -= discharge_kw * dt_h / discharge_eff

        # Allocate battery discharge to the two load types so the aggregate
        # Sankey can show household and geyser as separate destinations.
        battery_to_non_kw = min(remaining_non, discharge_kw)
        battery_to_geyser_kw = discharge_kw - battery_to_non_kw
        remaining_non -= battery_to_non_kw
        remaining_geyser -= battery_to_geyser_kw
        remaining_load = remaining_non + remaining_geyser

        # Grid supplies whatever remains, with the same household-first
        # allocation used above.
        grid_kw = max(0.0, remaining_load)
        grid_to_non_kw = min(remaining_non, grid_kw)
        grid_to_geyser_kw = grid_kw - grid_to_non_kw

        metrics["direct_pv_kwh"] += (direct_non + direct_geyser) * dt_h
        metrics["pv_to_geyser_kwh"] += direct_geyser * dt_h
        metrics["battery_charge_kwh"] += charge_kw * dt_h
        metrics["battery_discharge_kwh"] += discharge_kw * dt_h
        metrics["battery_to_household_kwh"] += battery_to_non_kw * dt_h
        metrics["battery_to_geyser_kwh"] += battery_to_geyser_kw * dt_h
        metrics["grid_import_kwh"] += grid_kw * dt_h
        metrics["grid_to_household_kwh"] += grid_to_non_kw * dt_h
        metrics["grid_to_geyser_kwh"] += grid_to_geyser_kw * dt_h
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
            "battery_to_household_kw": battery_to_non_kw,
            "battery_to_geyser_kw": battery_to_geyser_kw,
            "grid_kw": grid_kw,
            "grid_to_household_kw": grid_to_non_kw,
            "grid_to_geyser_kw": grid_to_geyser_kw,
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



def make_sankey_chart(out_dir, summary, inverter_kw, battery_kwh, reserve_pct,
                      geyser_mode, geyser_start, geyser_end, geyser_max_kw):
    """Create a compact, landscape Sankey-style energy-flow diagram.

    The visual is deliberately modelled on a mobile energy-analysis Sankey:
    dark rounded panel, source nodes on the left, destination nodes on the
    right, smooth curved ribbons, and ribbon width proportional to kWh.
    A ribbon keeps exactly the same thickness from its source attachment to
    its destination attachment.
    """
    if not summary:
        return None

    charge_eff = 0.95
    discharge_eff = 0.95
    total = lambda key: sum(m[key] for _, _, m in summary)

    pv_kwh = total("pv_kwh")
    household_kwh = total("measured_non_geyser_kwh")
    geyser_kwh = total("scheduled_geyser_kwh")
    load_kwh = household_kwh + geyser_kwh
    pv_to_household = total("direct_pv_kwh") - total("pv_to_geyser_kwh")
    pv_to_geyser = total("pv_to_geyser_kwh")
    battery_charge = total("battery_charge_kwh")
    battery_to_household = total("battery_to_household_kwh")
    battery_to_geyser = total("battery_to_geyser_kwh")
    grid_to_household = total("grid_to_household_kwh")
    grid_to_geyser = total("grid_to_geyser_kwh")
    grid_import = total("grid_import_kwh")
    curtailed = total("pv_curtailed_kwh")
    battery_discharge = total("battery_discharge_kwh")

    battery_losses = (
        battery_charge * (1.0 - charge_eff)
        + battery_discharge * (1.0 / discharge_eff - 1.0)
    )
    start_soc = summary[0][2]["starting_soc_kwh"]
    end_soc = summary[-1][2]["ending_soc_kwh"]
    net_soc_change = end_soc - start_soc

    # Exact energy-accounting checks.
    if abs((pv_to_household + pv_to_geyser + battery_charge + curtailed) - pv_kwh) > 1e-6:
        raise RuntimeError("Sankey PV reconciliation failed.")
    if abs((pv_to_household + battery_to_household + grid_to_household) - household_kwh) > 1e-6:
        raise RuntimeError("Sankey household-load reconciliation failed.")
    if abs((pv_to_geyser + battery_to_geyser + grid_to_geyser) - geyser_kwh) > 1e-6:
        raise RuntimeError("Sankey geyser-load reconciliation failed.")
    if abs((grid_to_household + grid_to_geyser) - grid_import) > 1e-6:
        raise RuntimeError("Sankey grid reconciliation failed.")
    if abs((battery_to_household + battery_to_geyser) - battery_discharge) > 1e-6:
        raise RuntimeError("Sankey battery-discharge reconciliation failed.")
    if abs(battery_charge - battery_discharge - battery_losses - net_soc_change) > 1e-6:
        raise RuntimeError("Sankey battery reconciliation failed.")

    # ------------------------------------------------------------------
    # Landscape/mobile-inspired layout.
    # ------------------------------------------------------------------
    fig = plt.figure(figsize=(12.5, 7.2), facecolor="#151820")
    ax = fig.add_axes([0.018, 0.025, 0.964, 0.95])
    ax.set_xlim(0, 160)
    ax.set_ylim(0, 90)
    ax.axis("off")

    panel = FancyBboxPatch(
        (1, 1), 158, 88,
        boxstyle="round,pad=0.0,rounding_size=8",
        facecolor="#343943", edgecolor="#454a56", linewidth=1.0,
        zorder=0,
    )
    ax.add_patch(panel)

    text_primary = "#F0F2F5"
    text_muted = "#AEB4C0"
    node_edge = "#5C6370"

    # Header: intentionally resembles the compact energy-analysis card.
    ax.text(6, 84.0, "Energy Analysis (kWh)", color=text_primary,
            fontsize=14, fontweight="bold", ha="left", va="center")

    def pill(x, y, w, label):
        p = FancyBboxPatch(
            (x, y), w, 9,
            boxstyle="round,pad=0.0,rounding_size=4.5",
            facecolor="#272B34", edgecolor="#454A55", linewidth=0.8,
            zorder=1,
        )
        ax.add_patch(p)
        ax.text(x + w/2, y + 4.5, label, color="#D6DAE1",
                fontsize=8.5, ha="center", va="center")

    pill(6, 72.0, 40, "SIMULATION")
    pill(112, 72.0, 40, f"{len(summary)} DAY" + ("S" if len(summary) != 1 else ""))

    # Stable source/destination colours, intentionally softer than the
    # screenshot so the labels remain readable.
    pv_color = "#A9D95A"
    battery_color = "#58B9B0"
    grid_color = "#6676D8"
    household_color = "#7B55A8"
    geyser_color = "#3D8D72"
    curtail_color = "#7D8797"
    loss_color = "#9299A5"

    # Energy scale: node heights and ribbons use the same conversion.
    largest = max(pv_kwh, grid_import, battery_discharge, battery_charge,
                  household_kwh, geyser_kwh, curtailed, 1.0)
    # Main flow area is deliberately generous vertically so the ribbons can
    # cross smoothly without making the diagram excessively tall.
    flow_scale = 31.0 / largest
    ribbon_scale = flow_scale
    node_width = 27.0
    left_x = 6.0
    right_x = 127.0

    def h(value):
        return max(7.0, value * flow_scale)

    def node(x, cy, title, value, color, height_value=None):
        nh = h(value if height_value is None else height_value)
        patch = FancyBboxPatch(
            (x, cy - nh/2), node_width, nh,
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
        return {
            "x0": x, "x1": x + node_width,
            "y0": cy - nh/2, "y1": cy + nh/2,
            "h": nh,
        }

    # Source boxes represent energy available to the house. Battery is shown
    # as "BATTERY OUT" because its outgoing energy is what feeds the loads.
    # A separate BATTERY IN box on the right makes the charge flow unambiguous.
    pv_box = node(left_x, 56, "PV", pv_kwh, pv_color)
    battery_out_box = node(left_x, 38, "Battery out", battery_discharge,
                           battery_color)
    grid_box = node(left_x, 20, "Grid", grid_import, grid_color)

    household_box = node(right_x, 55, "Household", household_kwh, household_color)
    geyser_box = node(right_x, 37, "Geyser", geyser_kwh, geyser_color)
    battery_in_box = node(right_x, 20, "Battery in", battery_charge, battery_color)
    curtailed_box = node(right_x, 8, "Curtailed", curtailed, curtail_color) if curtailed > 1e-9 else None

    def flow_slots(box, values, total_value, top=True):
        """Return attachment slots whose widths are proportional to the flow's
        share of this particular node.

        This deliberately allows a ribbon to taper between its two ends.
        For example, a 2 kWh flow occupies 2/10 of a 10 kWh source node and
        2/20 of a 20 kWh destination node. This matches the visual convention
        requested for the energy-analysis style: each node's incoming/outgoing
        flows add up to the full height of that node, while the underlying kWh
        values remain unchanged.
        """
        total_value = max(float(total_value), 1e-12)
        node_h = box["h"]
        # Keep the ribbons slightly inside the rounded node corners.  The
        # inset is applied equally to the whole stack, so relative flow
        # proportions are unchanged while the ribbons no longer run into
        # the curved top/bottom corners of the box.
        edge_inset = min(1.4, node_h * 0.12)
        flow_h = max(node_h - 2.0 * edge_inset, 0.1)
        cursor = (box["y1"] - edge_inset) if top else (box["y0"] + edge_inset)
        out = []
        for value in values:
            value = max(0.0, float(value))
            fh = flow_h * (value / total_value)
            if fh <= 1e-10:
                out.append(None)
                continue
            cy = cursor - fh/2 if top else cursor + fh/2
            out.append((cy, fh))
            cursor = cursor - fh if top else cursor + fh
        return out

    def ribbon(box_a, slot_a, box_b, slot_b, color, alpha=0.58):
        """Draw a cubic ribbon that visually joins into both node boxes.

        The ribbon is allowed to extend slightly inside each node.  This
        removes the visible seam between a box and its ribbons, matching the
        integrated Sankey treatment in the reference image.
        """
        if slot_a is None or slot_b is None:
            return
        ya, fh_a = slot_a
        yb, fh_b = slot_b
        join = 0.9
        x0 = box_a["x1"] - join
        x1 = box_b["x0"] + join
        dx = x1 - x0
        c1 = x0 + dx * 0.40
        c2 = x0 + dx * 0.60
        verts = [
            (x0, ya + fh_a/2),
            (c1, ya + fh_a/2), (c2, yb + fh_b/2), (x1, yb + fh_b/2),
            (x1, yb - fh_b/2),
            (c2, yb - fh_b/2), (c1, ya - fh_a/2), (x0, ya - fh_a/2),
            (x0, ya + fh_a/2),
        ]
        codes = [
            MplPath.MOVETO,
            MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
            MplPath.LINETO,
            MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
            MplPath.CLOSEPOLY,
        ]
        ax.add_patch(PathPatch(
            MplPath(verts, codes), facecolor=color, edgecolor="none",
            alpha=alpha, zorder=2,
        ))

    # Build exact source/destination attachment points. Each node's outgoing
    # and incoming ribbons fill the corresponding node exactly (except the
    # deliberately terminal battery-loss annotation).
    pv_out = flow_slots(pv_box, [pv_to_household, pv_to_geyser,
                                 battery_charge, curtailed], pv_kwh, top=True)
    batt_out = flow_slots(battery_out_box, [battery_to_household,
                                            battery_to_geyser], battery_discharge, top=True)
    grid_out = flow_slots(grid_box, [grid_to_household, grid_to_geyser], grid_import, top=True)

    house_in = flow_slots(household_box, [pv_to_household,
                                          battery_to_household,
                                          grid_to_household], household_kwh, top=True)
    geyser_in = flow_slots(geyser_box, [pv_to_geyser,
                                        battery_to_geyser,
                                        grid_to_geyser], geyser_kwh, top=True)
    batt_in = flow_slots(battery_in_box, [battery_charge], battery_charge, top=True)
    curt_in = flow_slots(curtailed_box, [curtailed], curtailed, top=True) if curtailed_box else [None]

    # Draw broad ribbons first, then narrower ribbons on top so crossings have
    # the same visual language as the reference image.
    ribbon(pv_box, pv_out[0], household_box, house_in[0], pv_color)
    ribbon(pv_box, pv_out[1], geyser_box, geyser_in[0], pv_color, 0.50)
    ribbon(pv_box, pv_out[2], battery_in_box, batt_in[0], battery_color, 0.62)
    if curtailed_box:
        ribbon(pv_box, pv_out[3], curtailed_box, curt_in[0], curtail_color, 0.48)

    ribbon(battery_out_box, batt_out[0], household_box, house_in[1], battery_color, 0.60)
    ribbon(battery_out_box, batt_out[1], geyser_box, geyser_in[1], battery_color, 0.52)

    ribbon(grid_box, grid_out[0], household_box, house_in[2], grid_color, 0.55)
    ribbon(grid_box, grid_out[1], geyser_box, geyser_in[2], grid_color, 0.50)

    # Battery losses are not a load and therefore are kept as a small labelled
    # terminal flow rather than pretending they feed one of the house loads.
    if battery_losses > 1e-9:
        ax.text(80, 7.8, f"Battery losses  {battery_losses:.1f} kWh",
                color=text_muted, fontsize=7.5, ha="center", va="center")

    # Footer with the simulation configuration and battery state change.
    mode = "solar-shift geyser" if geyser_mode == "solar-shift" else "measured geyser"
    footer = f"{mode}  •  {battery_kwh:.0f} kWh battery  •  {inverter_kw:.0f} kW inverter"
    ax.text(80, 3.6, footer, color=text_muted, fontsize=7.2,
            ha="center", va="center")
    if abs(net_soc_change) > 1e-9:
        sign = "+" if net_soc_change > 0 else "−"
        ax.text(80, 6.0, f"Net battery SOC change  {sign}{abs(net_soc_change):.1f} kWh",
                color=text_muted, fontsize=7.2, ha="center", va="center")

    ax.text(80, 77.0, "Solar Step 6 — Aggregate Energy Flow",
            color=text_primary, fontsize=10.5, fontweight="bold",
            ha="center", va="center")
    ax.text(80, 74.0, "Ribbon width ∝ energy (kWh)",
            color=text_muted, fontsize=7.2, ha="center", va="center")

    path = out_dir / "solar simulation aggregate sankey.png"
    fig.savefig(path, dpi=180, facecolor=fig.get_facecolor())
    plt.close(fig)

    csv_path = out_dir / "solar simulation aggregate energy flows.csv"
    with csv_path.open("w", newline="") as f:
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
            ["Battery losses", battery_losses],
            ["Grid to household", grid_to_household],
            ["Grid to geyser", grid_to_geyser],
            ["Grid import", grid_import],
            ["Starting SOC", start_soc],
            ["Ending SOC", end_soc],
            ["Net SOC change", net_soc_change],
        ])

    return path, csv_path

def main():
    config = load_config()
    a = args(config)
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

    db_path = load_db_path(config)
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

    sankey_path, flow_csv_path = make_sankey_chart(
        out_dir, summary, a.inverter, a.battery, a.reserve,
        a.geyser_mode, a.geyser_start, a.geyser_end, a.geyser_max_kw
    )
    print(f"Sankey energy-flow chart: {sankey_path}")
    print(f"Sankey flow data CSV:      {flow_csv_path}")
    print()

    print("Important:")
    print("  The PV curve is a transparent clear-sky test curve, not a site-specific")
    print("  weather/PVGIS result. The daily-yield value is a scenario assumption.")
    print("  Use --pv-yields to apply a different yield to successive days.")
    print("  Use --repeat-days N to repeat the most recent complete measured load profile N days.")
    print(f"\nDetailed CSVs: {out_dir}\n")


if __name__ == "__main__":
    main()
