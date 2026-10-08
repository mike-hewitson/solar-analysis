#!/usr/bin/env python3
"""
Solar PVGIS Hermanus simulation

Downloads one historical year of hourly PVGIS production for:
  - 3.5 kWp east-facing, 22 degree tilt
  - 3.5 kWp west-facing, 22 degree tilt

Then combines that PV profile with the daily household-load projection
stored in solar_meter.db. The projection is mapped by month/day onto the
selected PVGIS year.

The intraday non-geyser load shape comes from the database's
hourly_load_profile table, which is calculated as the average of all
complete measured days by solar_analysis.py. That average shape is normalised
to 100% and applied to each projected day's non-geyser energy.

The projected daily geyser energy is then shifted into the 09:00-16:00 solar
window, prioritising hours with PV surplus after non-geyser household demand. Remaining geyser energy is placed within
the same window at up to the configured maximum geyser power.

This is an energy model, not a thermal tank/thermostat model.

Requires: Python 3.9+ and requests.
No pandas or other third-party package is required.
"""

import argparse
import calendar
import configparser
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

TILT = 22
LOSS_PCT = 14
PVTECH = "crystSi"
BATTERY_EFF = 0.95

GEYSER_START = 9
GEYSER_END = 16       # exclusive
GEYSER_MAX_KW = 3.0


def load_config():
    config = configparser.ConfigParser()
    config_path = Path(__file__).resolve().parent / "config.ini"
    if not config_path.exists():
        raise FileNotFoundError(
            f"config.ini not found: {config_path}"
        )
    config.read(config_path)
    return config


def load_solar_defaults(config):
    required = (
        "pv_east_kwp",
        "pv_west_kwp",
        "inverter_kw",
        "battery_usable_kwh",
        "battery_reserve_percent",
    )
    missing = [key for key in required if not config.has_option("solar", key)]
    if missing:
        raise KeyError(
            "Missing [solar] setting(s) in config.ini: " + ", ".join(missing)
        )

    return {
        "east_kwp": config.getfloat("solar", "pv_east_kwp"),
        "west_kwp": config.getfloat("solar", "pv_west_kwp"),
        "inverter_kw": config.getfloat("solar", "inverter_kw"),
        "battery_kwh": config.getfloat("solar", "battery_usable_kwh"),
        "reserve_frac": config.getfloat("solar", "battery_reserve_percent") / 100.0,
    }


def load_db_path(config):
    db_path = config.get("database", "path", fallback="solar_meter.db")
    path = Path(db_path).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path

# Household load is supplied by the daily_load_projection table created
# by solar_usage_projection.py. The PVGIS simulation maps the projection's
# month/day onto the selected historical PVGIS year.
PROJECTION_TABLE = "daily_load_projection"


def parse_args(solar_defaults):
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=2023,
                   help="Historical PVGIS year to simulate (default 2023)")
    p.add_argument("--db", default=None,
                   help="SQLite database path")
    p.add_argument("--pv-cache", default=None,
                   help="Optional directory for cached PVGIS JSON files")
    p.add_argument("--output-dir", default="solar report/pvgis",
                   help="Output directory")
    p.add_argument("--battery-kwh", type=float, default=solar_defaults["battery_kwh"])
    p.add_argument("--reserve", type=float, default=solar_defaults["reserve_frac"],
                   help="Battery reserve fraction, e.g. 0.20")
    p.add_argument("--inverter-kw", type=float, default=solar_defaults["inverter_kw"])
    p.add_argument("--geyser-start", type=int, default=GEYSER_START)
    p.add_argument("--geyser-end", type=int, default=GEYSER_END)
    p.add_argument("--geyser-max-kw", type=float, default=GEYSER_MAX_KW)
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


def load_hourly_shape(db_path):
    """Load the average complete-day non-geyser shape from the database.

    solar_analysis.py creates hourly_load_profile from all complete measured
    days. The non_geyser_fraction column is the normalised average hourly
    non-geyser energy and therefore defines the intraday shape only; the
    daily_load_projection table remains authoritative for daily energy.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT hour,
                   complete_days,
                   avg_non_geyser_energy_kwh,
                   non_geyser_fraction
            FROM hourly_load_profile
            ORDER BY hour
        """).fetchall()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "hourly_load_profile is missing or outdated. "
            "Run solar_analysis.py first to rebuild the database analysis tables."
        ) from exc
    finally:
        conn.close()

    if len(rows) != 24:
        raise RuntimeError(
            f"Expected 24 hourly load-shape rows, found {len(rows)}."
        )

    complete_days = min(int(r["complete_days"]) for r in rows)
    if complete_days <= 0:
        raise RuntimeError("No complete measured days are available for the intraday load shape.")

    fractions = {int(r["hour"]): max(0.0, float(r["non_geyser_fraction"] or 0.0))
                 for r in rows}
    fraction_sum = sum(fractions.values())

    if fraction_sum <= 0:
        raise RuntimeError("The database contains no usable non-geyser hourly load shape.")

    # Re-normalise defensively so rounding in the stored values cannot change
    # the projected daily energy.
    fractions = {h: value / fraction_sum for h, value in fractions.items()}

    return {
        "complete_days": complete_days,
        "non_geyser_fraction": fractions,
        "average_non_geyser_kwh": sum(
            max(0.0, float(r["avg_non_geyser_energy_kwh"] or 0.0))
            for r in rows
        ),
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


def load_daily_projection(db_path):
    """
    Load the full daily household projection generated by
    solar_usage_projection.py.

    The simulation year may differ from the projection year, so the
    projection is indexed by month/day.
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT projection_date, month, day, scaling_factor,
                   total_energy_kwh, geyser_energy_kwh, non_geyser_energy_kwh
            FROM {PROJECTION_TABLE}
            ORDER BY projection_date
        """).fetchall()

    if not rows:
        raise RuntimeError(
            f"No rows found in {PROJECTION_TABLE}. "
            "Run solar_usage_projection.py first."
        )

    projection = {}
    for row in rows:
        projection[(int(row["month"]), int(row["day"]))] = {
            "date": row["projection_date"],
            "scaling_factor": float(row["scaling_factor"]),
            "total_kwh": float(row["total_energy_kwh"]),
            "geyser_kwh": float(row["geyser_energy_kwh"]),
            "non_geyser_kwh": float(row["non_geyser_energy_kwh"]),
        }

    # The projection should contain every day of its source year.
    missing = [
        (month, day)
        for month in range(1, 13)
        for day in range(1, calendar.monthrange(2026, month)[1] + 1)
        if (month, day) not in projection
    ]
    if missing:
        raise RuntimeError(
            f"{PROJECTION_TABLE} is missing {len(missing)} calendar day(s), "
            f"for example {missing[:5]}. Run solar_usage_projection.py again."
        )

    return projection


def simulate_year(pv_by_local_hour, hourly_shape, daily_projection, args, year):
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

        # The daily projection is the authoritative source of daily energy.
        # The database average profile is used only as a normalised intraday shape for
        # distributing projected non-geyser energy across the hours.
        projected = daily_projection[(day.month, day.day)]

        non_geyser = {
            h: fraction * projected["non_geyser_kwh"]
            for h, fraction in hourly_shape["non_geyser_fraction"].items()
        }
        geyser = projected["geyser_kwh"]

        geyser_sched = schedule_geyser(
            non_geyser, pv, geyser,
            args.geyser_start, args.geyser_end, args.geyser_max_kw
        )

        total_load = {h: non_geyser.get(h, 0.0) + geyser_sched[h] for h in range(24)}

        day_pv = sum(pv.values())
        day_load = sum(total_load.values())
        day_geyser = sum(geyser_sched.values())

        # The simulated daily household energy must exactly equal the
        # database projection for this calendar day.
        if abs(day_load - projected["total_kwh"]) > 1e-6:
            raise RuntimeError(
                f"Daily load reconciliation failed for {day}: "
                f"simulated={day_load:.6f} kWh, "
                f"projected={projected['total_kwh']:.6f} kWh"
            )

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
    config = load_config()
    solar_defaults = load_solar_defaults(config)
    args = parse_args(solar_defaults)
    db = Path(args.db).expanduser() if args.db else load_db_path(config)
    out = Path(args.output_dir).expanduser()
    cache = Path(args.pv_cache).expanduser() if args.pv_cache else out / "pvgis cache"
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    print("=" * 52)
    print("Hermanus PVGIS historical-year solar simulation")
    print("=" * 52)
    print(f"PVGIS year:                 {args.year}")
    print(f"Location:                   {LAT:.4f}, {LON:.4f}")
    print(f"PV:                         {solar_defaults['east_kwp'] + solar_defaults['west_kwp']:.2f} kWp "
          f"({solar_defaults['east_kwp']:.2f} east + {solar_defaults['west_kwp']:.2f} west)")
    print(f"Roof tilt:                  {TILT}°")
    print(f"PVGIS system losses:        {LOSS_PCT}%")
    print(f"Inverter:                   {args.inverter_kw:.2f} kW")
    print(f"Battery usable:              {args.battery_kwh:.2f} kWh")
    print(f"Battery reserve:             {args.reserve * 100:.1f}%")
    print(f"Geyser window:              {args.geyser_start:02d}:00-{args.geyser_end:02d}:00")
    print("Monthly household-demand model:")

    east_data, east_path, east_cached = fetch_pvgis(
        solar_defaults["east_kwp"], -90, args.year, cache, args.no_download
    )
    west_data, west_path, west_cached = fetch_pvgis(
        solar_defaults["west_kwp"], 90, args.year, cache, args.no_download
    )

    east = pvgis_hourly(east_data)
    west = pvgis_hourly(west_data)

    pv_by_local_hour = {}
    for ts, p in east.items():
        pv_by_local_hour[(ts.date(), ts.hour)] = p + west.get(ts, 0.0)

    # Daily energy comes from the database projection. The intraday
    # non-geyser shape comes from the average of all complete measured days
    # stored in hourly_load_profile by solar_analysis.py.
    hourly_shape = load_hourly_shape(db)
    daily_projection = load_daily_projection(db)

    print(f"Daily load source:          {PROJECTION_TABLE}")
    print(f"Projection days available:  {len(daily_projection)}")
    print("Intraday load shape:        average of complete measured days")
    print(f"Complete days in shape:     {hourly_shape['complete_days']}")
    print(f"Average non-geyser load:    {hourly_shape['average_non_geyser_kwh']:.3f} kWh/day")
    print("  (shape only; daily energy comes from the projection)")
    print()

    daily, annual, monthly, min_soc, max_soc = simulate_year(
        pv_by_local_hour, hourly_shape, daily_projection, args, args.year
    )

    daily_fields = list(daily[0].keys())
    write_csv(out / f"pvgis {args.year} daily simulation.csv", daily, daily_fields)

    monthly_rows = []
    for m in range(1, 13):
        d = monthly[m]
        days = len([x for x in daily if datetime.fromisoformat(x["date"]).month == m])
        row = {"month": m, "target_load_kwh": d["load"], "days": days}
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
    print(f"Peak simulated load:         {max(x["peak_load_kw"] for x in daily):.3f} kW")
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
    print("  * Daily household energy comes from daily_load_projection. That")
    print("    projection is based on the current measured daily baseline and")
    print("    monthly usage curve. As more meter data accumulates, it should")
    print("    eventually be replaced with measured daily/hourly load profiles.")
    print("  * The projection year is mapped by month/day onto the selected PVGIS")
    print("    year; this does not imply that the projected load occurred on the")
    print("    historical PVGIS dates.")
    print("  * The geyser is modelled as an energy-shiftable load, not as a")
    print("    physical hot-water tank with thermostat cycling.")
    print("  * Zero export is assumed; excess PV is curtailed once the battery")
    print("    is full and household load is satisfied.")

if __name__ == "__main__":
    main()
