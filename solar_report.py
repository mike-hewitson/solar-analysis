#!/usr/bin/env python3

"""
Solar Step 4 — Load Profile Report

Reads the derived data in solar_meter.db and produces:
  1. Daily energy chart for all available dates
  2. Average hourly load profile for complete days only
  3. CSV exports of the daily and hourly summaries
  4. A text summary with key planning metrics

Usage:
    python3 solar_report.py

The database path is read from config.ini, using the same [database]
section as the extractor and analysis scripts.
"""

from pathlib import Path
import configparser
import csv
import sqlite3
import statistics
import sys
from datetime import datetime

import matplotlib.pyplot as plt


SCRIPT_DIR = Path(__file__).resolve().parent


def load_db_path():
    config_path = SCRIPT_DIR / "config.ini"

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    config = configparser.ConfigParser()
    config.read(config_path)

    if not config.has_option("database", "path"):
        raise ValueError("config.ini does not contain [database] path")

    db_path = Path(config.get("database", "path"))

    if not db_path.is_absolute():
        db_path = SCRIPT_DIR / db_path

    return db_path


def fetch_data(con):
    daily = con.execute("""
        SELECT
            reading_date,
            reading_count,
            first_timestamp,
            last_timestamp,
            span_hours,
            is_complete_day,
            total_energy_kwh,
            geyser_energy_kwh,
            non_geyser_energy_kwh,
            avg_total_power_w,
            avg_geyser_power_w,
            avg_non_geyser_power_w,
            peak_total_power_w,
            peak_non_geyser_power_w
        FROM daily_load_summary
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


def find_peak_averages(con, windows_minutes=(1, 5, 15), top_n=3):
    """
    Find the highest rolling-average loads for the requested time windows.

    Power readings are linearly interpolated between meter samples onto a
    one-second grid. This makes the rolling averages comparable even though
    the Tuya meter timestamps are not perfectly regular.

    For each window, the report returns the three highest distinct windows,
    separated by at least one window length, together with the average total,
    geyser and house loads.
    """
    rows = con.execute("""
        SELECT
            timestamp_sast,
            total_power_w,
            geyser_power_w,
            non_geyser_power_w
        FROM v_meter_readings_clean
        WHERE total_power_w IS NOT NULL
        ORDER BY timestamp_sast
    """).fetchall()

    if len(rows) < 2:
        return {}

    # Convert readings to epoch seconds and interpolate each channel onto a
    # one-second grid. Limit interpolation across gaps greater than 5 minutes.
    points = []
    for row in rows:
        dt = datetime.fromisoformat(row[0])
        points.append((
            dt.timestamp(),
            row[1] or 0.0,
            row[2] or 0.0,
            row[3] or 0.0
        ))

    results = {}

    for window_minutes in windows_minutes:
        window_seconds = int(window_minutes * 60)
        grid = []

        for i in range(len(points) - 1):
            t0, total0, geyser0, house0 = points[i]
            t1, total1, geyser1, house1 = points[i + 1]
            gap = t1 - t0

            if gap <= 0 or gap > 300:
                continue

            # Add integer-second samples, including the endpoint of each
            # interval where appropriate.
            start_sec = int(t0)
            end_sec = int(t1)

            for ts in range(start_sec, end_sec):
                fraction = (ts - t0) / gap
                grid.append((
                    ts,
                    total0 + fraction * (total1 - total0),
                    geyser0 + fraction * (geyser1 - geyser0),
                    house0 + fraction * (house1 - house0)
                ))

        if len(grid) < window_seconds:
            results[window_minutes] = []
            continue

        # Sliding window over the interpolated one-second samples.
        candidates = []
        total_sum = geyser_sum = house_sum = 0.0

        for i, sample in enumerate(grid):
            total_sum += sample[1]
            geyser_sum += sample[2]
            house_sum += sample[3]

            if i >= window_seconds:
                old = grid[i - window_seconds]
                total_sum -= old[1]
                geyser_sum -= old[2]
                house_sum -= old[3]

            if i >= window_seconds - 1:
                end_ts = sample[0]
                start_ts = end_ts - window_seconds + 1
                candidates.append({
                    "start": datetime.fromtimestamp(start_ts),
                    "end": datetime.fromtimestamp(end_ts),
                    "total_w": total_sum / window_seconds,
                    "geyser_w": geyser_sum / window_seconds,
                    "house_w": house_sum / window_seconds,
                })

        # Highest average windows, keeping distinct events rather than
        # reporting many overlapping windows from the same load event.
        candidates.sort(key=lambda x: x["total_w"], reverse=True)
        selected = []

        for candidate in candidates:
            if any(
                abs(
                    (candidate["start"] - chosen["start"]).total_seconds()
                ) < window_seconds
                for chosen in selected
            ):
                continue

            selected.append(candidate)

            if len(selected) >= top_n:
                break

        results[window_minutes] = selected

    return results

def export_csv(out_dir, daily, hourly):
    daily_path = out_dir / "solar daily load summary.csv"
    hourly_path = out_dir / "solar hourly load profile.csv"

    daily_headers = [
        "reading_date", "reading_count", "first_timestamp",
        "last_timestamp", "span_hours", "is_complete_day",
        "total_energy_kwh", "geyser_energy_kwh",
        "non_geyser_energy_kwh", "avg_total_power_w",
        "avg_geyser_power_w", "avg_non_geyser_power_w",
        "peak_total_power_w", "peak_non_geyser_power_w"
    ]

    hourly_headers = [
        "hour", "complete_days", "avg_total_power_w",
        "avg_geyser_power_w", "avg_non_geyser_power_w",
        "avg_total_energy_kwh", "avg_geyser_energy_kwh",
        "avg_non_geyser_energy_kwh"
    ]

    with daily_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(daily_headers)
        writer.writerows(daily)

    with hourly_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(hourly_headers)
        writer.writerows(hourly)

    return daily_path, hourly_path


def make_daily_chart(out_dir, daily):
    dates = [r[0] for r in daily]
    total = [r[6] or 0 for r in daily]
    geyser = [r[7] or 0 for r in daily]
    non_geyser = [r[8] or 0 for r in daily]

    x = list(range(len(dates)))

    fig, ax = plt.subplots(figsize=(11, 6))

    ax.bar(x, geyser, label="Geyser")
    ax.bar(x, non_geyser, bottom=geyser, label="Non-geyser")

    for i, row in enumerate(daily):
        if row[5]:
            ax.text(
                i,
                total[i] + max(total) * 0.015,
                f"{total[i]:.1f}",
                ha="center",
                va="bottom",
                fontsize=9
            )

    ax.set_title("Daily Electricity Consumption")
    ax.set_ylabel("Energy (kWh)")
    ax.set_xticks(x)
    ax.set_xticklabels(dates, rotation=30, ha="right")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)

    fig.tight_layout()
    path = out_dir / "solar daily load profile.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)

    return path


def make_hourly_chart(out_dir, hourly):
    hours = [r[0] for r in hourly]
    total = [r[2] or 0 for r in hourly]
    geyser = [r[3] or 0 for r in hourly]
    non_geyser = [r[4] or 0 for r in hourly]

    labels = [f"{h:02d}:00" for h in hours]

    fig, ax = plt.subplots(figsize=(12, 6))

    ax.plot(hours, total, marker="o", label="Total")
    ax.plot(hours, geyser, marker="o", label="Geyser")
    ax.plot(hours, non_geyser, marker="o", label="Non-geyser")

    ax.set_title("Average Hourly Load — Complete Days Only")
    ax.set_xlabel("Time of day")
    ax.set_ylabel("Average power (W)")
    ax.set_xticks(hours)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.grid(True, alpha=0.25)
    ax.legend()

    fig.tight_layout()
    path = out_dir / "solar hourly load profile.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)

    return path


def make_hourly_energy_chart(out_dir, hourly):
    hours = [r[0] for r in hourly]
    total = [r[5] or 0 for r in hourly]
    geyser = [r[6] or 0 for r in hourly]
    non_geyser = [r[7] or 0 for r in hourly]

    x = list(range(len(hours)))

    fig, ax = plt.subplots(figsize=(12, 6))

    ax.bar(x, geyser, label="Geyser")
    ax.bar(x, non_geyser, bottom=geyser, label="Non-geyser")

    ax.set_title("Average Hourly Energy — Complete Days Only")
    ax.set_xlabel("Time of day")
    ax.set_ylabel("Energy (kWh)")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{h:02d}:00" for h in hours],
                       rotation=45, ha="right")
    ax.grid(axis="y", alpha=0.25)
    ax.legend()

    fig.tight_layout()
    path = out_dir / "solar hourly energy profile.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)

    return path


def print_report(daily, hourly, peak_averages):
    complete = [r for r in daily if r[5]]

    print("\n========================================")
    print("Solar Step 4 load profile report")
    print("========================================")

    print(f"Calendar days available: {len(daily)}")
    print(f"Complete days:           {len(complete)}")

    if complete:
        total = sum(r[6] or 0 for r in complete)
        geyser = sum(r[7] or 0 for r in complete)
        other = sum(r[8] or 0 for r in complete)

        print("\nComplete-day energy:")
        print(f"  Total:                 {total / len(complete):8.3f} kWh/day")
        print(f"  Geyser:                {geyser / len(complete):8.3f} kWh/day")
        print(f"  Non-geyser:            {other / len(complete):8.3f} kWh/day")
        print(f"  Geyser share:          {100 * geyser / total:8.1f}%")

        peak = max(r[12] or 0 for r in complete)
        print(f"  Peak total load:       {peak / 1000:8.3f} kW")

    if peak_averages:
        print("\nTop 3 rolling-average loads:")

        for window_minutes in (1, 5, 15):
            events = peak_averages.get(window_minutes, [])

            print(f"\n  {window_minutes}-minute average:")
            print(
                f"  {'Rank':>4}  {'Start':<19}  {'End':<19}  "
                f"{'Total kW':>8}  {'Geyser kW':>9}  {'House kW':>8}"
            )
            print(
                f"  {'----':>4}  {'-' * 19}  {'-' * 19}  "
                f"{'-' * 8}  {'-' * 9}  {'-' * 8}"
            )

            for n, event in enumerate(events, start=1):
                print(
                    f"  {n:>4}  "
                    f"{event['start'].strftime('%Y-%m-%d %H:%M:%S'):<19}  "
                    f"{event['end'].strftime('%Y-%m-%d %H:%M:%S'):<19}  "
                    f"{event['total_w'] / 1000:8.3f}  "
                    f"{event['geyser_w'] / 1000:9.3f}  "
                    f"{event['house_w'] / 1000:8.3f}"
                )


    if hourly:
        # Solar-planning windows.
        windows = [
            ("06:00–09:00", 6, 9),
            ("09:00–15:00", 9, 15),
            ("15:00–18:00", 15, 18),
            ("09:00–17:00", 9, 17),
        ]

        print("\nAverage hourly energy by planning window:")
        for label, start, end in windows:
            rows = [r for r in hourly if start <= r[0] < end]
            total = sum(r[5] or 0 for r in rows)
            geyser = sum(r[6] or 0 for r in rows)
            other = sum(r[7] or 0 for r in rows)
            print(
                f"  {label:10s}  total {total:6.3f} kWh"
                f"   geyser {geyser:6.3f}"
                f"   non-geyser {other:6.3f}"
            )


def main():
    db_path = load_db_path()

    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    out_dir = SCRIPT_DIR / "solar report"
    out_dir.mkdir(exist_ok=True)

    with sqlite3.connect(db_path) as con:
        daily, hourly = fetch_data(con)
        peak_averages = find_peak_averages(con)

    if not daily:
        raise RuntimeError("No daily analysis data found. Run solar_analysis.py first.")

    daily_csv, hourly_csv = export_csv(out_dir, daily, hourly)
    daily_png = make_daily_chart(out_dir, daily)
    hourly_png = make_hourly_chart(out_dir, hourly)
    hourly_energy_png = make_hourly_energy_chart(out_dir, hourly)

    print_report(daily, hourly, peak_averages)

    print("\nFiles created:")
    print(f"  {daily_png}")
    print(f"  {hourly_png}")
    print(f"  {hourly_energy_png}")
    print(f"  {daily_csv}")
    print(f"  {hourly_csv}")
    print()


if __name__ == "__main__":
    main()
