#!/usr/bin/env python3
"""Estimate geyser energy used for routine top-ups over recent complete days.

The largest configurable number of heating sessions each day are treated as
shower-related recovery. Remaining sessions are treated as routine top-ups.
This estimates electrical energy, not direct tank heat loss.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta
from pathlib import Path

from solar_analysis import load_config
from solar_db import connect_database, sql_placeholder

SCRIPT_DIR = Path(__file__).resolve().parent
REPORT_DIR = SCRIPT_DIR / "solar report"
MAX_INTERVAL_SECONDS = 300.0
SESSION_GAP_SECONDS = 300.0


def cleaned_power(value, threshold_w):
    if value is None:
        return None
    power = float(value)
    if abs(power) < threshold_w:
        return 0.0
    return max(power, 0.0)


def collect_sessions(rows, threshold_w):
    """Integrate valid geyser intervals and group nearby active intervals."""
    sessions = []
    for previous, current in zip(rows, rows[1:]):
        t0 = datetime.fromisoformat(str(previous["timestamp_sast"]))
        t1 = datetime.fromisoformat(str(current["timestamp_sast"]))
        elapsed = (t1 - t0).total_seconds()
        if elapsed <= 0 or elapsed > MAX_INTERVAL_SECONDS:
            continue

        p0 = cleaned_power(previous["power_b_w"], threshold_w)
        p1 = cleaned_power(current["power_b_w"], threshold_w)
        if p0 is None or p1 is None or max(p0, p1) <= 0:
            continue

        energy = (p0 + p1) * 0.5 * elapsed / 3_600_000
        day = t0.date()
        if (
            sessions
            and sessions[-1]["day"] == day
            and (t0 - sessions[-1]["last_active"]).total_seconds()
            <= SESSION_GAP_SECONDS
        ):
            session = sessions[-1]
            session["end"] = t1
            session["last_active"] = t1
            session["energy_kwh"] += energy
        else:
            sessions.append({
                "day": day,
                "start": t0,
                "end": t1,
                "last_active": t1,
                "energy_kwh": energy,
            })
    return sessions


def main():
    parser = argparse.ArgumentParser(
        description="Estimate the geyser energy used for routine top-ups."
    )
    parser.add_argument(
        "--days", type=int, default=10,
        help="number of complete calendar days to analyse (default: 10)",
    )
    parser.add_argument(
        "--showers-per-day", type=int, default=2,
        help="largest daily heating sessions treated as shower-related (default: 2)",
    )
    parser.add_argument(
        "--end-date", type=date.fromisoformat,
        help="exclusive end date in YYYY-MM-DD format (default: today)",
    )
    args = parser.parse_args()
    if args.days < 1 or args.showers_per_day < 0:
        parser.error("--days must be positive and --showers-per-day cannot be negative")

    end_date = args.end_date or date.today()
    start_date = end_date - timedelta(days=args.days)
    start_text = f"{start_date.isoformat()} 00:00:00"
    end_text = f"{end_date.isoformat()} 00:00:00"

    db_path, noise_threshold_w = load_config()
    placeholder = sql_placeholder()
    query = f"""
        SELECT timestamp_sast, power_b_w
        FROM meter_readings
        WHERE timestamp_sast >= {placeholder}
          AND timestamp_sast < {placeholder}
        ORDER BY timestamp_sast
    """
    with connect_database(db_path, readonly=True, named_rows=True) as con:
        rows = con.execute(query, (start_text, end_text)).fetchall()

    sessions = collect_sessions(rows, noise_threshold_w)
    days = [start_date + timedelta(days=i) for i in range(args.days)]
    by_day = {day: [] for day in days}
    for session in sessions:
        if session["day"] in by_day:
            by_day[session["day"]].append(session)

    total_energy = 0.0
    total_topup = 0.0
    for day in days:
        daily = sorted(by_day[day], key=lambda item: item["energy_kwh"], reverse=True)
        for rank, session in enumerate(daily, start=1):
            session["rank"] = rank
            session["category"] = (
                "shower-related" if rank <= args.showers_per_day else "top-up"
            )
        total_energy += sum(item["energy_kwh"] for item in daily)
        total_topup += sum(
            item["energy_kwh"] for item in daily if item["category"] == "top-up"
        )

    if not rows:
        raise SystemExit(f"No meter readings found from {start_text} to {end_text}.")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = REPORT_DIR / (
        f"geyser_topup_sessions_{start_date.isoformat()}_to_"
        f"{(end_date - timedelta(days=1)).isoformat()}.csv"
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "date", "session_rank", "classification", "start", "end",
            "duration_minutes", "geyser_energy_kwh",
        ])
        for day in days:
            for session in sorted(by_day[day], key=lambda item: item["start"]):
                writer.writerow([
                    day.isoformat(),
                    session["rank"],
                    session["category"],
                    session["start"].strftime("%H:%M:%S"),
                    session["end"].strftime("%H:%M:%S"),
                    round((session["end"] - session["start"]).total_seconds() / 60, 1),
                    round(session["energy_kwh"], 4),
                ])

    share = total_topup / total_energy * 100 if total_energy else 0.0
    print(f"Geyser top-up estimate: {start_date} to {end_date - timedelta(days=1)}")
    print(f"Complete days: {args.days}; shower-related sessions excluded per day: "
          f"{args.showers_per_day}")
    print(f"{'Date':<12} {'Sessions':>8} {'Geyser kWh':>12} "
          f"{'Top-up kWh':>12} {'Top-up %':>10}")
    for day in days:
        daily = by_day[day]
        energy = sum(item["energy_kwh"] for item in daily)
        topup = sum(
            item["energy_kwh"] for item in daily if item["category"] == "top-up"
        )
        percent = topup / energy * 100 if energy else 0.0
        print(f"{day} {len(daily):>8} {energy:>12.2f} {topup:>12.2f} {percent:>9.1f}%")
    print(f"Overall geyser energy: {total_energy:.2f} kWh")
    print(f"Estimated top-up energy: {total_topup:.2f} kWh ({share:.1f}%)")
    print(f"Session details: {csv_path}")


if __name__ == "__main__":
    main()
