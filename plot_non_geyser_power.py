#!/usr/bin/env python3
"""
Plot household power for one calendar day from the configured Solar database.

Usage:
    python3 plot_non_geyser_power.py
    python3 plot_non_geyser_power.py --date 2026-10-11
    python3 plot_non_geyser_power.py --date 2026-10-11 --chart geyser
    python3 plot_non_geyser_power.py --date 2026-10-11 --show

The default date is yesterday in South African local time.
No Tuya API calls or data extraction are performed.
"""

import argparse
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from solar_db import connect_database, database_label, sql_placeholder


TZ = ZoneInfo("Africa/Johannesburg")
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB = SCRIPT_DIR / "solar_meter.db"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "solar report"
GEYSER_NOISE_THRESHOLD_W = 20.0
MAX_INTEGRATION_GAP_SECONDS = 300.0


def parse_args():
    parser = argparse.ArgumentParser(
        description="Graph household, geyser and/or non-geyser power for a day using the configured Solar database."
    )
    parser.add_argument(
        "--date",
        dest="day",
        help="Calendar date in YYYY-MM-DD format (default: yesterday, South African time).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite path when SQLite is configured (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Folder for the PNG graph (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--chart",
        choices=("non-geyser", "geyser", "total", "both"),
        default="non-geyser",
        help=(
            "Which power series to chart: 'non-geyser' (default), "
            "'total', or 'both' (geyser, non-geyser and total)."
        ),
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Display the graph after saving it.",
    )
    return parser.parse_args()


def resolve_day(day_text):
    if not day_text:
        return datetime.now(TZ).date() - timedelta(days=1)
    try:
        return date.fromisoformat(day_text)
    except ValueError:
        raise SystemExit("ERROR: --date must be in YYYY-MM-DD format, e.g. 2026-10-11.")


def load_readings(db_path, selected_day):
    try:
        connection = connect_database(db_path, readonly=True)
    except Exception as exc:
        raise SystemExit(f"ERROR: Could not open database read-only: {exc}")

    try:
        query = f"""
            SELECT timestamp_sast, power_a_w, power_b_w
            FROM meter_readings
            WHERE reading_date = {sql_placeholder()}
            ORDER BY timestamp_sast
        """
        rows = connection.execute(query, (selected_day.isoformat(),)).fetchall()
        return rows
    finally:
        connection.close()


def parse_timestamp(value):
    if value is None:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # Accommodate timestamps stored without an explicit timezone.
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def integrate_kwh(times, watts):
    """Trapezoidal integration, ignoring intervals over five minutes."""
    total_wh = 0.0
    for i in range(1, len(times)):
        seconds = (times[i] - times[i - 1]).total_seconds()
        if 0 < seconds <= MAX_INTEGRATION_GAP_SECONDS:
            total_wh += ((watts[i - 1] + watts[i]) / 2.0) * seconds / 3600.0
    return total_wh / 1000.0


def main():
    args = parse_args()
    selected_day = resolve_day(args.day)
    rows = load_readings(args.db.expanduser(), selected_day)

    data = []
    for timestamp, total_w, geyser_w in rows:
        dt = parse_timestamp(timestamp)
        try:
            total_w = float(total_w)
            geyser_w = float(geyser_w)
        except (TypeError, ValueError):
            continue
        if dt is None:
            continue
        # Project's existing cleaning rule: negative readings are zero, and
        # geyser readings below 20 W are treated as CT/electrical noise.
        cleaned_geyser_w = max(0.0, geyser_w)
        if cleaned_geyser_w < GEYSER_NOISE_THRESHOLD_W:
            cleaned_geyser_w = 0.0
        non_geyser_w = max(0.0, total_w - cleaned_geyser_w)
        data.append((dt, total_w, cleaned_geyser_w, non_geyser_w))

    if not data:
        raise SystemExit(
            f"No usable readings found for {selected_day.isoformat()} in {args.db}.\n"
            "Check that this date is present in the database; this script does not extract data."
        )

    data.sort(key=lambda item: item[0])
    times = [item[0] for item in data]
    total_w = [item[1] for item in data]
    geyser_w = [item[2] for item in data]
    non_geyser_w = [item[3] for item in data]

    peak_non_geyser_index = max(range(len(non_geyser_w)), key=non_geyser_w.__getitem__)
    peak_non_geyser_kw = non_geyser_w[peak_non_geyser_index] / 1000.0
    peak_total_index = max(range(len(total_w)), key=total_w.__getitem__)
    peak_total_kw = total_w[peak_total_index] / 1000.0
    non_geyser_kwh = integrate_kwh(times, non_geyser_w)
    total_kwh = integrate_kwh(times, total_w)
    geyser_kwh = integrate_kwh(times, geyser_w)

    args.output_dir.expanduser().mkdir(parents=True, exist_ok=True)
    chart_suffix = {
        "non-geyser": "non_geyser_power",
        "geyser": "geyser_power",
        "total": "total_power",
        "both": "geyser_non_geyser_and_total_power",
    }[args.chart]
    output_path = (
        args.output_dir.expanduser()
        / f"{chart_suffix}_{selected_day.isoformat()}.png"
    )

    fig, ax = plt.subplots(figsize=(13, 6.5))
    if args.chart in ("non-geyser", "both"):
        ax.plot(
            times, [value / 1000.0 for value in non_geyser_w],
            linewidth=1.0, label="Non-geyser power"
        )
    if args.chart in ("geyser", "both"):
        ax.plot(
            times, [value / 1000.0 for value in geyser_w],
            linewidth=1.0, label="Geyser power"
        )
    if args.chart in ("total", "both"):
        ax.plot(
            times, [value / 1000.0 for value in total_w],
            linewidth=1.0, label="Total household power"
        )
    chart_titles = {
        "non-geyser": "Non-geyser household power",
        "geyser": "Geyser power",
        "total": "Total household power",
        "both": "Geyser, non-geyser and total household power",
    }
    ax.set_title(
        f"{chart_titles[args.chart]} — {selected_day.strftime('%A, %d %B %Y')}"
    )
    ax.set_xlabel("Time (South African time)")
    ax.set_ylabel("Power (kW)")
    ax.set_xlim(
        datetime.combine(selected_day, time.min, TZ),
        datetime.combine(selected_day + timedelta(days=1), time.min, TZ),
    )
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 2), tz=TZ))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")

    summary_parts = [f"Readings: {len(data):,}"]
    if args.chart in ("non-geyser", "both"):
        summary_parts.append(
            f"Non-geyser peak: {peak_non_geyser_kw:.2f} kW "
            f"at {times[peak_non_geyser_index].strftime('%H:%M')}"
        )
    if args.chart in ("total", "both"):
        summary_parts.append(
            f"Total peak: {peak_total_kw:.2f} kW "
            f"at {times[peak_total_index].strftime('%H:%M')}"
        )
    if args.chart in ("geyser", "both"):
        summary_parts.append(f"Geyser energy: {geyser_kwh:.2f} kWh")
    if args.chart in ("non-geyser", "both"):
        summary_parts.append(f"Non-geyser energy*: {non_geyser_kwh:.2f} kWh")
    if args.chart in ("total", "both"):
        summary_parts.append(f"Total energy: {total_kwh:.2f} kWh")
    summary = "   |   ".join(summary_parts)
    fig.text(0.08, 0.015, summary + "\n*Energy integration ignores gaps longer than 5 minutes.",
             ha="left", va="bottom", fontsize=9)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.savefig(output_path, dpi=160, bbox_inches="tight")

    print(f"Date:                  {selected_day.isoformat()}")
    print(f"Database:              {database_label(args.db)}")
    print(f"Readings plotted:      {len(data):,}")
    print(f"Total household:       {total_kwh:.2f} kWh")
    print(f"Geyser (cleaned):      {geyser_kwh:.2f} kWh")
    print(f"Non-geyser:            {non_geyser_kwh:.2f} kWh")
    print(
        f"Peak non-geyser power: {peak_non_geyser_kw:.2f} kW "
        f"at {times[peak_non_geyser_index].strftime('%H:%M')}"
    )
    print(
        f"Peak total power:      {peak_total_kw:.2f} kW "
        f"at {times[peak_total_index].strftime('%H:%M')}"
    )
    print(f"Chart selection:       {args.chart}")
    print(f"Graph saved to:        {output_path}")

    if args.show:
        plt.show()
    else:
        plt.close(fig)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"ERROR: Database problem: {exc}")
