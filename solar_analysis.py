#!/usr/bin/env python3
"""
Check the database and build cleaned load summaries.

Run from the Solar directory:

    python3 solar_analysis.py
    python3 solar_analysis.py --check-only

The script:
  - reports database size, timestamp integrity, recent coverage, and extraction history
  - leaves meter_readings untouched
  - cleans small Power_B readings as geyser noise
  - calculates non-geyser load
  - calculates interval energy from timestamp differences
  - creates/rebuilds derived tables for daily and hourly analysis
  - only treats complete calendar days as eligible for hourly averages
"""

import argparse
import configparser
from pathlib import Path

from solar_db import (
    connect_database as open_database,
    database_backend,
    database_label,
)

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.ini"


def load_config():
    config = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        config.read(CONFIG_FILE)

    db_value = config.get(
        "database",
        "path",
        fallback="solar_meter.db",
    ).strip() or "solar_meter.db"

    db_path = Path(db_value)
    if not db_path.is_absolute():
        db_path = SCRIPT_DIR / db_path

    # Power_B values below this threshold are treated as CT noise.
    noise_threshold = config.getfloat(
        "analysis",
        "geyser_noise_threshold_w",
        fallback=20.0,
    )

    return db_path, noise_threshold


def connect(db_path):
    return open_database(db_path)


def format_gap(seconds):
    total_seconds = int(round(float(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def print_database_check(con):
    print("========================================")
    print("Solar database integrity check")
    print("========================================")
    print(f"Database: {database_label()}")

    if database_backend() in {"postgres", "postgresql"}:
        database_size_bytes = con.execute(
            "SELECT pg_database_size(current_database())"
        ).fetchone()[0]
    else:
        page_count = con.execute("PRAGMA page_count").fetchone()[0]
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        database_size_bytes = page_count * page_size

    print(f"Database storage: {database_size_bytes / 1_000_000:.2f} MB")

    row = con.execute("""
        SELECT
            COUNT(*),
            MIN(timestamp_sast),
            MAX(timestamp_sast),
            COUNT(DISTINCT timestamp_sast)
        FROM meter_readings
    """).fetchone()

    print(f"\nTotal rows:          {row[0]:,}")
    print(f"Earliest timestamp:  {row[1]}")
    print(f"Latest timestamp:    {row[2]}")
    print(f"Distinct timestamps: {row[3]:,}")

    duplicates = con.execute("""
        SELECT COUNT(*)
        FROM (
            SELECT timestamp_sast
            FROM meter_readings
            GROUP BY timestamp_sast
            HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    print(f"Duplicate timestamps: {duplicates}")

    print("\nDaily coverage (most recent 14 days):")
    print("-" * 78)
    daily = con.execute("""
        SELECT
            reading_date,
            COUNT(*) AS total,
            SUM(CASE WHEN power_a_w IS NOT NULL THEN 1 ELSE 0 END) AS power_a,
            SUM(CASE WHEN power_b_w IS NOT NULL THEN 1 ELSE 0 END) AS power_b,
            MIN(timestamp_sast) AS first_reading,
            MAX(timestamp_sast) AS last_reading
        FROM meter_readings
        GROUP BY reading_date
        ORDER BY reading_date DESC
        LIMIT 14
    """).fetchall()
    daily.reverse()

    print(
        f"{'Date':<12} {'Rows':>7} {'A':>7} {'B':>7} "
        f"{'First':>24} {'Last':>24}"
    )
    for row in daily:
        print(
            f"{row[0]:<12} {row[1]:>7,} {row[2]:>7,} {row[3]:>7,} "
            f"{row[4]:>24} {row[5]:>24}"
        )

    print("\nLargest timestamp gaps:")
    print("-" * 78)
    if database_backend() in {"postgres", "postgresql"}:
        gap_seconds = (
            "EXTRACT(EPOCH FROM "
            "(timestamp_sast::timestamp - previous::timestamp))"
        )
        rounded_gap = f"ROUND(({gap_seconds})::numeric, 3)"
    else:
        gap_seconds = (
            "(julianday(timestamp_sast) - julianday(previous)) * 86400"
        )
        rounded_gap = f"ROUND(({gap_seconds}), 3)"

    gaps = con.execute(f"""
        WITH ordered AS (
            SELECT
                timestamp_sast,
                LAG(timestamp_sast) OVER (ORDER BY timestamp_sast) AS previous
            FROM meter_readings
        )
        SELECT previous, timestamp_sast, {rounded_gap} AS gap_seconds
        FROM ordered
        WHERE previous IS NOT NULL
        ORDER BY gap_seconds DESC
        LIMIT 15
    """).fetchall()
    for previous, current, seconds in gaps:
        print(f"{format_gap(seconds):>10}  {previous} -> {current}")

    print("\nExtraction runs:")
    print("-" * 78)
    runs = con.execute("""
        SELECT
            source_file,
            started_at,
            completed_at,
            rows_read,
            rows_inserted,
            rows_updated,
            rows_rejected,
            status
        FROM extraction_runs
        ORDER BY id DESC
        LIMIT 10
    """).fetchall()
    for row in runs:
        print(
            f"{row[0]} | {row[7]} | read={row[3]} "
            f"inserted={row[4]} updated={row[5]} rejected={row[6]}"
        )

    print("\n========================================")
    print("Check complete")
    print("========================================")


def create_schema(con, noise_threshold):
    threshold = float(noise_threshold)
    is_postgres = database_backend() in {"postgres", "postgresql"}
    timestamp_expr = "timestamp_sast::timestamp" if is_postgres else "timestamp_sast"
    greatest = "GREATEST" if is_postgres else "MAX"
    power_average = (
        "((total_power_w + next_power_a_w)::double precision / 2.0)"
        if is_postgres
        else "((total_power_w + next_power_a_w) / 2.0)"
    )
    geyser_start_power = "geyser_power_w::double precision" if is_postgres else "geyser_power_w"
    next_geyser_power = "next_power_b_w::double precision" if is_postgres else "next_power_b_w"
    energy_zero = "0.0::double precision" if is_postgres else "0.0"
    gap_seconds = (
        "EXTRACT(EPOCH FROM (next_timestamp - timestamp_value))"
        if is_postgres
        else "(julianday(next_timestamp) - julianday(timestamp_value)) * 86400.0"
    )
    if not is_postgres:
        con.execute("DROP VIEW IF EXISTS v_meter_readings_clean")
    create_view = "CREATE OR REPLACE VIEW" if is_postgres else "CREATE VIEW"
    meter_table = "public.meter_readings" if is_postgres else "meter_readings"

    con.execute(f"""
    {create_view} v_meter_readings_clean AS
    WITH ordered AS (
        SELECT
            id,
            timestamp_sast,
            {timestamp_expr} AS timestamp_value,
            reading_date,
            power_a_w,
            power_b_w,
            LEAD({timestamp_expr}) OVER (ORDER BY timestamp_sast) AS next_timestamp,
            LEAD(power_a_w) OVER (ORDER BY timestamp_sast) AS next_power_a_w,
            LEAD(power_b_w) OVER (ORDER BY timestamp_sast) AS next_power_b_w
        FROM {meter_table}
    ),
    cleaned AS (
        SELECT
            id,
            timestamp_sast,
            timestamp_value,
            reading_date,
            power_a_w AS total_power_w,
            CASE
                WHEN power_b_w IS NULL THEN NULL
                WHEN ABS(power_b_w) < {threshold}
                    THEN 0.0
                ELSE {greatest}(power_b_w, 0.0)
            END AS geyser_power_w,
            next_timestamp,
            next_power_a_w,
            next_power_b_w
        FROM ordered
    )
    SELECT
        id,
        timestamp_sast,
        reading_date,
        total_power_w,
        geyser_power_w,
        CASE
            WHEN total_power_w IS NULL THEN NULL
            WHEN geyser_power_w IS NULL THEN total_power_w
            ELSE {greatest}(total_power_w - geyser_power_w, 0.0)
        END AS non_geyser_power_w,

        CASE
            WHEN next_timestamp IS NULL
              OR next_timestamp <= timestamp_value
              OR {gap_seconds} > 300.0
              OR total_power_w IS NULL OR next_power_a_w IS NULL
            THEN NULL
            ELSE {power_average}
                 * ({gap_seconds})
                 / 3600000.0
        END AS total_energy_kwh,

        CASE
            WHEN next_timestamp IS NULL
              OR next_timestamp <= timestamp_value
              OR {gap_seconds} > 300.0
              OR geyser_power_w IS NULL OR next_power_b_w IS NULL
            THEN NULL
            ELSE (
                (
                    {geyser_start_power} +
                    CASE
                        WHEN ABS(next_power_b_w) < {threshold}
                            THEN {energy_zero}
                        ELSE {greatest}({next_geyser_power}, 0.0)
                    END
                ) / 2.0
            )
            * ({gap_seconds})
            / 3600000.0
        END AS geyser_energy_kwh
    FROM cleaned;
    """)

    con.execute("""
    CREATE TABLE IF NOT EXISTS daily_load_summary (
        reading_date TEXT PRIMARY KEY,
        reading_count INTEGER NOT NULL,
        first_timestamp TEXT NOT NULL,
        last_timestamp TEXT NOT NULL,
        span_hours REAL NOT NULL,
        is_complete_day INTEGER NOT NULL,
        total_energy_kwh REAL,
        geyser_energy_kwh REAL,
        non_geyser_energy_kwh REAL,
        avg_total_power_w REAL,
        avg_geyser_power_w REAL,
        avg_non_geyser_power_w REAL,
        peak_total_power_w REAL,
        peak_non_geyser_power_w REAL
    )
    """)

    con.execute("""
    CREATE TABLE IF NOT EXISTS hourly_load_profile (
        hour INTEGER PRIMARY KEY,
        complete_days INTEGER NOT NULL,
        avg_total_power_w REAL,
        avg_geyser_power_w REAL,
        avg_non_geyser_power_w REAL,
        avg_total_energy_kwh REAL,
        avg_geyser_energy_kwh REAL,
        avg_non_geyser_energy_kwh REAL,
        non_geyser_fraction REAL
    )
    """)


def populate_daily(con):
    span_hours = (
        "EXTRACT(EPOCH FROM (MAX(timestamp_sast::timestamp) - "
        "MIN(timestamp_sast::timestamp))) / 3600.0"
        if database_backend() in {"postgres", "postgresql"}
        else "(julianday(MAX(timestamp_sast)) - julianday(MIN(timestamp_sast))) * 24.0"
    )
    con.execute("DELETE FROM daily_load_summary")
    con.execute(f"""
    INSERT INTO daily_load_summary (
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
    )
    SELECT
        reading_date,
        COUNT(*),
        MIN(timestamp_sast),
        MAX(timestamp_sast),
        {span_hours},

        CASE
            WHEN MIN(timestamp_sast) <= reading_date || ' 00:05:00'
             AND MAX(timestamp_sast) >= reading_date || ' 23:55:00'
            THEN 1
            ELSE 0
        END,

        SUM(total_energy_kwh),
        SUM(geyser_energy_kwh),
        SUM(total_energy_kwh) - SUM(geyser_energy_kwh),
        AVG(total_power_w),
        AVG(geyser_power_w),
        AVG(non_geyser_power_w),
        MAX(total_power_w),
        MAX(non_geyser_power_w)

    FROM v_meter_readings_clean
    GROUP BY reading_date
    ORDER BY reading_date
    """)


def populate_hourly(con):
    # Calculate hourly power averages and hourly energy correctly.
    #
    # Power is averaged over the samples in each hour.
    # Energy is first SUMMED for each date/hour, then averaged across
    # complete days.  This is important: averaging the individual
    # interval-energy values gives the energy of only one sampling interval,
    # not the energy consumed during the hour.
    #
    # Intervals are assigned to the hour containing their start timestamp.
    # With the meter's roughly 10-second sampling interval, only a very small
    # amount of energy can fall across an hour boundary; the daily totals
    # remain fully interval-integrated.
    con.execute("DELETE FROM hourly_load_profile")

    con.execute("""
    INSERT INTO hourly_load_profile (
        hour,
        complete_days,
        avg_total_power_w,
        avg_geyser_power_w,
        avg_non_geyser_power_w,
        avg_total_energy_kwh,
        avg_geyser_energy_kwh,
        avg_non_geyser_energy_kwh,
        non_geyser_fraction
    )
    WITH hourly_daily AS (
        SELECT
            reading_date,
            CAST(substr(timestamp_sast, 12, 2) AS INTEGER) AS hour,
            AVG(total_power_w) AS total_power_w,
            AVG(geyser_power_w) AS geyser_power_w,
            AVG(non_geyser_power_w) AS non_geyser_power_w,
            SUM(COALESCE(total_energy_kwh, 0.0)) AS total_energy_kwh,
            SUM(COALESCE(geyser_energy_kwh, 0.0)) AS geyser_energy_kwh,
            SUM(COALESCE(total_energy_kwh, 0.0))
              - SUM(COALESCE(geyser_energy_kwh, 0.0))
              AS non_geyser_energy_kwh
        FROM v_meter_readings_clean
        WHERE reading_date IN (
            SELECT reading_date
            FROM daily_load_summary
            WHERE is_complete_day = 1
        )
        GROUP BY reading_date, hour
    )
    SELECT
        hour,
        COUNT(*) AS complete_days,
        AVG(total_power_w),
        AVG(geyser_power_w),
        AVG(non_geyser_power_w),
        AVG(total_energy_kwh),
        AVG(geyser_energy_kwh),
        AVG(non_geyser_energy_kwh),
        AVG(non_geyser_energy_kwh) /
            NULLIF(
                (
                    SELECT SUM(avg_non_geyser_energy_kwh)
                    FROM (
                        SELECT AVG(non_geyser_energy_kwh) AS avg_non_geyser_energy_kwh
                        FROM hourly_daily
                        GROUP BY hour
                    )
                ),
                0.0
            ) AS non_geyser_fraction
    FROM hourly_daily
    GROUP BY hour
    ORDER BY hour
    """)


def print_summary(con, threshold):
    print("\n========================================")
    print("Analysis layer")
    print("========================================")
    print(f"Database:                 {database_label()}")
    print(f"Geyser noise threshold:   {threshold:.1f} W")

    print("\nDaily summary:")
    print("-" * 105)
    print(
        f"{'Date':<12} {'Rows':>7} {'Complete':>9} "
        f"{'Total kWh':>11} {'Geyser kWh':>11} {'Non-geyser':>12} "
        f"{'Peak W':>9}"
    )

    for row in con.execute("""
        SELECT reading_date, reading_count, is_complete_day,
               total_energy_kwh, geyser_energy_kwh,
               non_geyser_energy_kwh, peak_total_power_w
        FROM (
            SELECT reading_date, reading_count, is_complete_day,
                   total_energy_kwh, geyser_energy_kwh,
                   non_geyser_energy_kwh, peak_total_power_w
            FROM daily_load_summary
            ORDER BY reading_date DESC
            LIMIT 10
        ) AS recent_days
        ORDER BY reading_date
    """):
        print(
            f"{row[0]:<12} {row[1]:>7,} {row[2]:>9} "
            f"{row[3]:>11.3f} {row[4]:>11.3f} {row[5]:>12.3f} "
            f"{row[6]:>9.1f}"
        )

    print("\nAverage hourly profile — complete days only:")
    print("-" * 95)
    print(
        f"{'Hour':>5} {'Days':>6} {'Total W':>12} "
        f"{'Geyser W':>12} {'Non-geyser W':>15} "
        f"{'Total kWh':>12} {'Non-geyser kWh':>16} {'NG frac':>10}"
    )

    for row in con.execute("""
        SELECT hour, complete_days, avg_total_power_w,
               avg_geyser_power_w, avg_non_geyser_power_w,
               avg_total_energy_kwh, avg_non_geyser_energy_kwh,
               non_geyser_fraction
        FROM hourly_load_profile
        ORDER BY hour
    """):
        print(
            f"{row[0]:>5} {row[1]:>6} {row[2]:>12.1f} "
            f"{row[3]:>12.1f} {row[4]:>15.1f} "
            f"{row[5]:>12.4f} {row[6]:>16.4f}"
        )

    print("\n========================================")


def main():
    parser = argparse.ArgumentParser(
        description="Check the Solar database and build its analysis summaries."
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run the database integrity check without rebuilding analysis tables.",
    )
    args = parser.parse_args()

    db_path, threshold = load_config()
    con = connect(db_path)

    try:
        print_database_check(con)
        if args.check_only:
            return

        create_schema(con, threshold)
        populate_daily(con)
        populate_hourly(con)
        con.commit()
        print_summary(con, threshold)
    finally:
        con.close()


if __name__ == "__main__":
    main()
