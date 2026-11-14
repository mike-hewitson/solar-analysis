#!/usr/bin/env python3

"""
Solar Step 4 — Load Profile Report

Reads the derived data in solar_meter.db and produces:
  1. Daily energy chart for the current Johannesburg calendar month
  2. Monthly energy chart and summary from October 2026 onward
  3. Average hourly load profile for complete days only
  4. CSV exports of the daily, monthly and hourly summaries
  5. A text summary with key planning metrics

Usage:
    python3 solar_report.py

The database path is read from config.ini, using the same [database]
section as the extractor and analysis scripts.
"""

from pathlib import Path
import configparser
import csv
import calendar
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from solar_db import connect_database
from solar_chart_colors import GEYSER, NON_GEYSER

import matplotlib.pyplot as plt


SCRIPT_DIR = Path(__file__).resolve().parent
MONTHLY_REPORT_START = date(2026, 10, 1)


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


def find_peak_total_load(con):
    """Return the peak total-load reading and its component loads on a complete day."""
    return con.execute("""
        SELECT
            timestamp_sast,
            total_power_w,
            geyser_power_w,
            non_geyser_power_w
        FROM v_meter_readings_clean
        WHERE reading_date IN (
            SELECT reading_date
            FROM daily_load_summary
            WHERE is_complete_day = 1
        )
          AND total_power_w IS NOT NULL
        ORDER BY total_power_w DESC, timestamp_sast
        LIMIT 1
    """).fetchone()


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
                candidates.append((
                    total_sum / window_seconds,
                    start_ts,
                    end_ts,
                    geyser_sum / window_seconds,
                    house_sum / window_seconds,
                ))

        # Highest average windows, keeping distinct events rather than
        # reporting many overlapping windows from the same load event.
        candidates.sort(key=lambda x: x[0], reverse=True)
        selected = []

        for candidate in candidates:
            if any(
                abs(candidate[1] - chosen[1]) < window_seconds
                for chosen in selected
            ):
                continue

            selected.append(candidate)

            if len(selected) >= top_n:
                break

        results[window_minutes] = [
            {
                "start": datetime.fromtimestamp(candidate[1]),
                "end": datetime.fromtimestamp(candidate[2]),
                "total_w": candidate[0],
                "geyser_w": candidate[3],
                "house_w": candidate[4],
            }
            for candidate in selected
        ]

    return results


def _reading_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def summarize_monthly(daily):
    """Aggregate daily energy into calendar months from October 2026 onward."""
    monthly = defaultdict(lambda: {
        "days_with_readings": 0,
        "complete_days": 0,
        "total_energy_kwh": 0.0,
        "geyser_energy_kwh": 0.0,
        "non_geyser_energy_kwh": 0.0,
    })

    for row in daily:
        reading_date = _reading_date(row[0])
        if reading_date < MONTHLY_REPORT_START:
            continue

        month = reading_date.strftime("%Y-%m")
        summary = monthly[month]
        summary["days_with_readings"] += 1
        summary["complete_days"] += bool(row[5])
        summary["total_energy_kwh"] += row[6] or 0.0
        summary["geyser_energy_kwh"] += row[7] or 0.0
        summary["non_geyser_energy_kwh"] += row[8] or 0.0

    results = []
    for month, summary in sorted(monthly.items()):
        year, month_number = map(int, month.split("-"))
        days_in_month = calendar.monthrange(year, month_number)[1]
        is_complete_month = (
            summary["days_with_readings"] == days_in_month
            and summary["complete_days"] == days_in_month
        )
        results.append((
            month,
            days_in_month,
            summary["days_with_readings"],
            summary["complete_days"],
            is_complete_month,
            summary["total_energy_kwh"],
            summary["geyser_energy_kwh"],
            summary["non_geyser_energy_kwh"],
        ))

    return results


def export_csv(out_dir, daily, hourly, monthly):
    daily_path = out_dir / "solar daily load summary.csv"
    hourly_path = out_dir / "solar hourly load profile.csv"
    monthly_path = out_dir / "solar monthly load summary.csv"

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

    monthly_headers = [
        "month", "days_in_month", "days_with_readings", "complete_days",
        "is_complete_month", "total_energy_kwh", "geyser_energy_kwh",
        "non_geyser_energy_kwh",
    ]

    with daily_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(daily_headers)
        writer.writerows(daily)

    with hourly_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(hourly_headers)
        writer.writerows(hourly)

    with monthly_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(monthly_headers)
        writer.writerows(monthly)

    return daily_path, hourly_path, monthly_path


def make_daily_chart(out_dir, daily, as_of_date=None):
    """Create the daily energy chart for the current Johannesburg month."""
    as_of_date = as_of_date or datetime.now(
        ZoneInfo("Africa/Johannesburg")
    ).date()
    chart_daily = []
    for row in daily:
        reading_date = _reading_date(row[0])
        if (reading_date.year, reading_date.month) == (
            as_of_date.year, as_of_date.month
        ):
            chart_daily.append(row)

    dates = [_reading_date(row[0]).isoformat() for row in chart_daily]
    total = [r[6] or 0 for r in chart_daily]
    geyser = [r[7] or 0 for r in chart_daily]
    non_geyser = [r[8] or 0 for r in chart_daily]

    x = list(range(len(dates)))

    fig, ax = plt.subplots(figsize=(11, 6))

    ax.bar(x, non_geyser, color=NON_GEYSER, label="Non-geyser")
    ax.bar(x, geyser, bottom=non_geyser, color=GEYSER, label="Geyser")

    for i, row in enumerate(chart_daily):
        if row[5]:
            ax.text(
                i,
                total[i] + max(total) * 0.015,
                f"{total[i]:.1f}",
                ha="center",
                va="bottom",
                fontsize=9
            )

    month_title = as_of_date.strftime("%B %Y")
    ax.set_title(f"Daily Electricity Consumption — {month_title}")
    if not chart_daily:
        ax.text(
            0.5, 0.5, f"No readings for {month_title}",
            transform=ax.transAxes, ha="center", va="center"
        )
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


def make_hourly_energy_chart(out_dir, hourly):
    hours = [r[0] for r in hourly]
    total = [r[5] or 0 for r in hourly]
    geyser = [r[6] or 0 for r in hourly]
    non_geyser = [r[7] or 0 for r in hourly]

    x = list(range(len(hours)))

    fig, ax = plt.subplots(figsize=(12, 6))

    ax.bar(x, non_geyser, color=NON_GEYSER, label="Non-geyser")
    ax.bar(x, geyser, bottom=non_geyser, color=GEYSER, label="Geyser")

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


def make_monthly_chart(out_dir, monthly):
    months = [datetime.strptime(row[0], "%Y-%m").strftime("%b %Y") for row in monthly]
    total = [row[5] for row in monthly]
    geyser = [row[6] for row in monthly]
    non_geyser = [row[7] for row in monthly]
    x = list(range(len(months)))

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.bar(x, non_geyser, color=NON_GEYSER, label="Non-geyser")
    ax.bar(x, geyser, bottom=non_geyser, color=GEYSER, label="Geyser")

    if total:
        label_offset = max(total) * 0.015
        for i, row in enumerate(monthly):
            if row[4]:
                ax.text(
                    i,
                    total[i] + label_offset,
                    f"{total[i]:.1f}",
                    ha="center",
                    va="bottom",
                    fontsize=9,
                )

    ax.set_title("Monthly Electricity Consumption (from October 2026)")
    ax.set_ylabel("Energy (kWh)")
    ax.set_xticks(x)
    ax.set_xticklabels(months, rotation=30, ha="right")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    if not monthly:
        ax.text(0.5, 0.5, "No monthly data available from October 2026", ha="center", va="center", transform=ax.transAxes)

    fig.tight_layout()
    path = out_dir / "solar monthly load profile.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)

    return path


def print_report(daily, hourly, monthly, peak_averages, peak_total_load):
    complete = [r for r in daily if r[5]]

    print("\n========================================")
    print("Solar Step 4 load profile report")
    print("========================================")

    print(f"Calendar days available: {len(daily)}")
    print(f"Complete days:           {len(complete)}")

    print("\nMonthly electricity consumption (from October 2026):")
    if monthly:
        print(
            f"  {'Month':<10}  {'Total kWh':>10}  {'Geyser kWh':>11}  "
            f"{'Non-geyser kWh':>15}  {'Complete days':>14}"
        )
        for row in monthly:
            month_label = datetime.strptime(row[0], "%Y-%m").strftime("%b %Y")
            month_status = "complete" if row[4] else "to date / partial"
            print(
                f"  {month_label:<10}  {row[5]:10.1f}  {row[6]:11.1f}  "
                f"{row[7]:15.1f}  {row[3]:>4}/{row[1]:<4} ({month_status})"
            )
    else:
        print("  No monthly data available from October 2026.")

    if complete:
        total = sum(r[6] or 0 for r in complete)
        geyser = sum(r[7] or 0 for r in complete)
        other = sum(r[8] or 0 for r in complete)

        print("\nComplete-day energy:")
        print(f"  Total:                 {total / len(complete):8.3f} kWh/day")
        print(f"  Geyser:                {geyser / len(complete):8.3f} kWh/day")
        print(f"  Non-geyser:            {other / len(complete):8.3f} kWh/day")
        print(f"  Geyser share:          {100 * geyser / total:8.1f}%")

        if peak_total_load:
            timestamp = datetime.fromisoformat(str(peak_total_load[0]))
            total_w = peak_total_load[1] or 0.0
            geyser_w = peak_total_load[2] or 0.0
            non_geyser_w = peak_total_load[3] or 0.0
            print(
                f"  Peak total load:       {total_w / 1000:8.3f} kW"
                f" at {timestamp:%Y-%m-%d %H:%M:%S}"
            )
            print(
                f"    Geyser:              {geyser_w / 1000:8.3f} kW"
                f"  Non-geyser: {non_geyser_w / 1000:8.3f} kW"
            )

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
    out_dir = SCRIPT_DIR / "solar report"
    out_dir.mkdir(exist_ok=True)

    with connect_database() as con:
        daily, hourly = fetch_data(con)
        peak_total_load = find_peak_total_load(con)
        peak_averages = find_peak_averages(con)

    if not daily:
        raise RuntimeError("No daily analysis data found. Run solar_analysis.py first.")

    monthly = summarize_monthly(daily)
    daily_csv, hourly_csv, monthly_csv = export_csv(out_dir, daily, hourly, monthly)
    daily_png = make_daily_chart(out_dir, daily)
    hourly_energy_png = make_hourly_energy_chart(out_dir, hourly)
    monthly_png = make_monthly_chart(out_dir, monthly)

    print_report(daily, hourly, monthly, peak_averages, peak_total_load)

    print("\nFiles created:")
    print(f"  {daily_png}")
    print(f"  {hourly_energy_png}")
    print(f"  {monthly_png}")
    print(f"  {daily_csv}")
    print(f"  {hourly_csv}")
    print(f"  {monthly_csv}")
    print()


if __name__ == "__main__":
    main()
