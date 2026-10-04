#!/usr/bin/env python3

"""
Solar energy projection — Step 1

1. Read measured monthly usage from monthly-usage.csv.
2. Use linear regression to estimate missing months.
3. Print the complete 12-month usage profile.
4. Read complete days from solar_meter.db.
5. Calculate average daily energy usage:
       - Total
       - Geyser
       - Non-geyser household
6. Print the results.

The database path is read from config.ini, using the same [database]
section as the other solar scripts.
"""

from pathlib import Path
import calendar
import configparser
import csv
import sqlite3
import statistics


SCRIPT_DIR = Path(__file__).resolve().parent

MONTH_NAMES = [
    "January", "February", "March", "April",
    "May", "June", "July", "August",
    "September", "October", "November", "December"
]


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


def load_monthly_csv():
    """
    Read monthly usage from monthly-usage.csv.

    File format:
        February;326
        March;284
        April;343
        ...

    The file has no header row and uses semicolons as separators.
    """
    csv_path = SCRIPT_DIR / "monthly-usage.csv"

    if not csv_path.exists():
        raise FileNotFoundError(
            f"Monthly usage file not found: {csv_path}"
        )

    monthly = {}

    with csv_path.open("r", newline="") as f:
        reader = csv.reader(f, delimiter=";")

        for row in reader:
            if not row or not row[0].strip():
                continue

            month_value = row[0].strip()
            usage_value = row[1].strip()

            try:
                month = int(month_value)
            except ValueError:
                month = (
                    MONTH_NAMES.index(month_value.capitalize()) + 1
                )

            if not 1 <= month <= 12:
                raise ValueError(f"Invalid month: {month}")

            monthly[month] = float(usage_value)

    return monthly

def linear_regression_estimate(monthly):
    """Estimate missing months using linear regression."""

    measured = [
        (month, usage)
        for month, usage in monthly.items()
        if usage is not None
    ]

    if len(measured) < 2:
        raise ValueError(
            "At least two measured months are required "
            "for linear regression."
        )

    x_values = [x for x, y in measured]
    y_values = [y for x, y in measured]

    x_mean = statistics.mean(x_values)
    y_mean = statistics.mean(y_values)

    numerator = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in measured
    )

    denominator = sum(
        (x - x_mean) ** 2
        for x in x_values
    )

    slope = numerator / denominator
    intercept = y_mean - slope * x_mean

    complete = dict(monthly)

    for month in range(1, 13):
        if month not in complete or complete[month] is None:
            complete[month] = intercept + slope * month

    return complete, slope, intercept


def print_monthly_usage(monthly, measured_months):
    print()
    print("=" * 60)
    print("MONTHLY ELECTRICITY USAGE")
    print("=" * 60)

    annual_total = 0.0

    for month in range(1, 13):
        usage = monthly[month]
        source = (
            "measured"
            if month in measured_months
            else "estimated"
        )

        print(
            f"{MONTH_NAMES[month - 1]:<12} "
            f"{usage:8.1f} kWh   {source}"
        )

        annual_total += usage

    print("-" * 60)
    print(
        f"{'Annual total':<12} "
        f"{annual_total:8.1f} kWh"
    )


def calculate_complete_day_average(db_path):
    """
    Calculate average daily energy usage using all complete days
    in daily_load_summary.
    """

    with sqlite3.connect(db_path) as con:
        rows = con.execute("""
            SELECT
                reading_date,
                total_energy_kwh,
                geyser_energy_kwh,
                non_geyser_energy_kwh
            FROM daily_load_summary
            WHERE is_complete_day = 1
            ORDER BY reading_date
        """).fetchall()

    if not rows:
        raise RuntimeError(
            "No complete days found in daily_load_summary."
        )

    total_values = [row[1] for row in rows if row[1] is not None]
    geyser_values = [row[2] for row in rows if row[2] is not None]
    non_geyser_values = [
        row[3] for row in rows if row[3] is not None
    ]

    return {
        "number_of_complete_days": len(rows),
        "total_kwh": statistics.mean(total_values),
        "geyser_kwh": statistics.mean(geyser_values),
        "non_geyser_kwh": statistics.mean(non_geyser_values),
        "dates": [row[0] for row in rows],
    }


def print_complete_day_average(result):
    print()
    print("=" * 60)
    print("AVERAGE DAILY USAGE — COMPLETE DAYS ONLY")
    print("=" * 60)

    print(
        f"Complete days analysed: "
        f"{result['number_of_complete_days']}"
    )

    print()
    print(
        f"Total household use:   "
        f"{result['total_kwh']:.3f} kWh/day"
    )

    print(
        f"Geyser:                "
        f"{result['geyser_kwh']:.3f} kWh/day"
    )

    print(
        f"Non-geyser household:  "
        f"{result['non_geyser_kwh']:.3f} kWh/day"
    )

    geyser_share = (
        result["geyser_kwh"] /
        result["total_kwh"] *
        100
    )

    print(
        f"Geyser share:          "
        f"{geyser_share:.1f}%"
    )

    print()
    print(
        f"First complete day:    "
        f"{result['dates'][0]}"
    )

    print(
        f"Last complete day:     "
        f"{result['dates'][-1]}"
    )


def build_daily_projection(monthly, average_daily):
    """
    Build a daily projection for every day of 2026.

    Each month's scaling factor is chosen so the projected daily
    totals for that month add up exactly to that month's usage total.
    """
    projection = []

    baseline_total = average_daily["total_kwh"]
    baseline_geyser = average_daily["geyser_kwh"]
    baseline_non_geyser = average_daily["non_geyser_kwh"]

    for month in range(1, 13):
        days_in_month = calendar.monthrange(2026, month)[1]

        baseline_month_total = baseline_total * days_in_month
        scaling_factor = monthly[month] / baseline_month_total

        daily_total = baseline_total * scaling_factor
        daily_geyser = baseline_geyser * scaling_factor
        daily_non_geyser = baseline_non_geyser * scaling_factor

        for day in range(1, days_in_month + 1):
            projection.append({
                "date": f"2026-{month:02d}-{day:02d}",
                "month": month,
                "day": day,
                "scaling_factor": scaling_factor,
                "total_kwh": daily_total,
                "geyser_kwh": daily_geyser,
                "non_geyser_kwh": daily_non_geyser,
            })

    return projection


def print_daily_projection(projection):
    """Print every 7th projected day for checking."""
    print()
    print("=" * 88)
    print("DAILY ENERGY PROJECTION — EVERY 7TH DAY")
    print("=" * 88)
    print(
        f"{'Date':<12} "
        f"{'Scale':>8} "
        f"{'Total kWh':>12} "
        f"{'Geyser kWh':>12} "
        f"{'Non-geyser kWh':>16}"
    )
    print("-" * 88)

    for index, row in enumerate(projection, start=1):
        if index % 7 == 0:
            print(
                f"{row['date']:<12} "
                f"{row['scaling_factor']:>8.4f} "
                f"{row['total_kwh']:>12.3f} "
                f"{row['geyser_kwh']:>12.3f} "
                f"{row['non_geyser_kwh']:>16.3f}"
            )

    print("-" * 88)
    print(f"Projected days: {len(projection)}")


def verify_monthly_totals(projection, monthly):
    """Verify each month's projected total against its target."""
    print()
    print("=" * 72)
    print("MONTHLY PROJECTION CHECK")
    print("=" * 72)

    for month in range(1, 13):
        projected = sum(
            row["total_kwh"]
            for row in projection
            if row["month"] == month
        )
        target = monthly[month]
        difference = projected - target

        print(
            f"{MONTH_NAMES[month - 1]:<12} "
            f"Target: {target:8.2f} kWh   "
            f"Projected: {projected:8.2f} kWh   "
            f"Difference: {difference:+.6f}"
        )


def save_daily_projection_to_database(db_path, projection):
    """
    Save the daily projection to a dedicated SQLite table.

    Measured Tuya data and the existing daily_load_summary table are
    left untouched.
    """
    with sqlite3.connect(db_path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS daily_load_projection (
                projection_date TEXT PRIMARY KEY,
                month INTEGER NOT NULL,
                day INTEGER NOT NULL,
                scaling_factor REAL NOT NULL,
                total_energy_kwh REAL NOT NULL,
                geyser_energy_kwh REAL NOT NULL,
                non_geyser_energy_kwh REAL NOT NULL
            )
        """)

        con.executemany("""
            INSERT OR REPLACE INTO daily_load_projection (
                projection_date,
                month,
                day,
                scaling_factor,
                total_energy_kwh,
                geyser_energy_kwh,
                non_geyser_energy_kwh
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            (
                row["date"],
                row["month"],
                row["day"],
                row["scaling_factor"],
                row["total_kwh"],
                row["geyser_kwh"],
                row["non_geyser_kwh"],
            )
            for row in projection
        ])

        con.commit()

    print()
    print("=" * 60)
    print("DAILY PROJECTION SAVED")
    print("=" * 60)
    print(f"Database: {db_path}")
    print("Table:    daily_load_projection")
    print(f"Rows:     {len(projection)}")


def main():
    # --------------------------------------------------------------
    # Monthly usage
    # --------------------------------------------------------------

    monthly = load_monthly_csv()
    measured_months = set(monthly.keys())

    complete_monthly, slope, intercept = (
        linear_regression_estimate(monthly)
    )

    print_monthly_usage(
        complete_monthly,
        measured_months
    )

    print()
    print(
        f"Linear regression: "
        f"usage = {intercept:.2f} + "
        f"{slope:.2f} × month"
    )

    # --------------------------------------------------------------
    # Complete-day database average
    # --------------------------------------------------------------

    db_path = load_db_path()

    if not db_path.exists():
        raise FileNotFoundError(
            f"Database not found: {db_path}"
        )

    result = calculate_complete_day_average(db_path)

    print_complete_day_average(result)

    projection = build_daily_projection(
        complete_monthly,
        result
    )
    print_daily_projection(projection)
    verify_monthly_totals(projection, complete_monthly)
    save_daily_projection_to_database(db_path, projection)


if __name__ == "__main__":
    main()
