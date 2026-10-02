#!/usr/bin/env python3

"""
Solar Step 5 — Solar Planning Analysis

Uses the SQLite derived analysis to produce measured-load metrics useful for
PV, inverter and battery modelling.

Complete days only are used for averaged planning metrics.

Usage:
    python3 solar_planning_analysis.py
"""

from pathlib import Path
import configparser
import csv
import sqlite3


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


def load_data(con):
    daily = con.execute("""
        SELECT
            reading_date,
            total_energy_kwh,
            geyser_energy_kwh,
            non_geyser_energy_kwh,
            peak_total_power_w,
            peak_non_geyser_power_w
        FROM daily_load_summary
        WHERE is_complete_day = 1
        ORDER BY reading_date
    """).fetchall()

    hourly = con.execute("""
        SELECT
            hour,
            complete_days,
            avg_total_power_w,
            avg_geyser_power_w,
            avg_non_geyser_power_w,
            avg_total_energy_kwh,
            avg_geyser_energy_kwh,
            avg_non_geyser_energy_kwh
        FROM hourly_load_profile
        ORDER BY hour
    """).fetchall()

    return daily, hourly


def window(hourly, start, end):
    rows = [r for r in hourly if start <= r[0] < end]

    return {
        "total": sum(r[5] or 0 for r in rows),
        "geyser": sum(r[6] or 0 for r in rows),
        "non_geyser": sum(r[7] or 0 for r in rows),
    }


def write_csv(out_dir, windows, daily):
    path = out_dir / "solar planning metrics.csv"

    headers = [
        "metric",
        "total_kwh_per_day",
        "geyser_kwh_per_day",
        "non_geyser_kwh_per_day",
        "description",
    ]

    rows = []

    descriptions = {
        "overnight_18_06": "18:00–06:00",
        "morning_06_09": "06:00–09:00",
        "solar_09_15": "09:00–15:00",
        "afternoon_15_18": "15:00–18:00",
        "solar_09_17": "09:00–17:00",
        "daytime_08_18": "08:00–18:00",
    }

    for name, values in windows.items():
        rows.append([
            name,
            values["total"],
            values["geyser"],
            values["non_geyser"],
            descriptions.get(name, ""),
        ])

    if daily:
        rows.append([
            "complete_day_total",
            sum(r[1] or 0 for r in daily) / len(daily),
            sum(r[2] or 0 for r in daily) / len(daily),
            sum(r[3] or 0 for r in daily) / len(daily),
            "24-hour average",
        ])

    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    return path


def main():
    db_path = load_db_path()

    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    out_dir = SCRIPT_DIR / "solar report"
    out_dir.mkdir(exist_ok=True)

    with sqlite3.connect(db_path) as con:
        daily, hourly = load_data(con)

    if not daily:
        raise RuntimeError(
            "No complete days are available yet. "
            "Run solar_analysis.py after a full calendar day has been collected."
        )

    windows = {
        "overnight_18_06": {
            "total": 0, "geyser": 0, "non_geyser": 0
        },
        "morning_06_09": window(hourly, 6, 9),
        "solar_09_15": window(hourly, 9, 15),
        "afternoon_15_18": window(hourly, 15, 18),
        "solar_09_17": window(hourly, 9, 17),
        "daytime_08_18": window(hourly, 8, 18),
    }

    # The overnight period crosses midnight.
    overnight_a = window(hourly, 18, 24)
    overnight_b = window(hourly, 0, 6)

    windows["overnight_18_06"] = {
        key: overnight_a[key] + overnight_b[key]
        for key in ("total", "geyser", "non_geyser")
    }

    # Calculate averages across complete days.
    n = len(daily)

    for values in windows.values():
        for key in values:
            values[key] /= n

    total = sum(r[1] or 0 for r in daily) / n
    geyser = sum(r[2] or 0 for r in daily) / n
    non_geyser = sum(r[3] or 0 for r in daily) / n
    peak = max(r[4] or 0 for r in daily)

    solar_window = windows["solar_09_17"]
    daytime = windows["daytime_08_18"]
    overnight = windows["overnight_18_06"]

    print("\n========================================")
    print("Solar Step 5 planning analysis")
    print("========================================")
    print(f"Complete days analysed:       {n}")
    print()

    print("Measured average daily energy:")
    print(f"  Total household:            {total:8.3f} kWh")
    print(f"  Geyser:                     {geyser:8.3f} kWh")
    print(f"  Non-geyser:                 {non_geyser:8.3f} kWh")
    print(f"  Geyser share:               {100 * geyser / total:8.1f}%")
    print(f"  Peak measured load:         {peak / 1000:8.3f} kW")
    print()

    print("Energy by planning window:")
    print("--------------------------------------------------------------")
    print("Window             Total kWh   Geyser kWh   Non-geyser kWh")
    print("--------------------------------------------------------------")

    labels = [
        ("Overnight 18–06", "overnight_18_06"),
        ("Morning 06–09", "morning_06_09"),
        ("Solar 09–15", "solar_09_15"),
        ("Afternoon 15–18", "afternoon_15_18"),
        ("Solar 09–17", "solar_09_17"),
        ("Daytime 08–18", "daytime_08_18"),
    ]

    for label, key in labels:
        v = windows[key]
        print(
            f"{label:18s}"
            f"{v['total']:11.3f}"
            f"{v['geyser']:14.3f}"
            f"{v['non_geyser']:16.3f}"
        )

    print()
    print("Solar-planning indicators:")
    print(f"  Energy in 09:00–17:00:     {solar_window['total']:8.3f} kWh/day")
    print(f"    of which geyser:         {solar_window['geyser']:8.3f} kWh/day")
    print(f"    non-geyser:              {solar_window['non_geyser']:8.3f} kWh/day")
    print()
    print(f"  Energy in 08:00–18:00:     {daytime['total']:8.3f} kWh/day")
    print(f"  Energy outside 18:00–06:00:{overnight['total']:7.3f} kWh/day")

    csv_path = write_csv(out_dir, windows, daily)
    print()
    print(f"CSV report: {csv_path}")
    print()

    print("Note:")
    print("These are measured-load indicators, not a PV or battery size recommendation.")
    print("As additional complete days accumulate, the values will become more representative.")


if __name__ == "__main__":
    main()
