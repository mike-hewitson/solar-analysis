#!/usr/bin/env python3
"""
Import a Tuya CSV into the solar_meter SQLite database.

Usage:
    python import_tuya_csv.py "path/to/tuya_power_since_installation.csv"

The import is idempotent: re-importing the same readings will update the
existing row rather than create duplicates.
"""

import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_NAME = "solar_meter.db"
EXPECTED_COLUMNS = {"datetime_sast", "Power_A_W", "Power_B_W"}


def parse_float(value):
    if value is None or value.strip() == "":
        return None
    return float(value)


def main():
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: python import_tuya_csv.py <tuya_csv_file>"
        )

    csv_path = Path(sys.argv[1]).expanduser().resolve()
    if not csv_path.exists():
        raise SystemExit(f"CSV file not found: {csv_path}")

    db_path = Path(__file__).resolve().with_name(DB_NAME)
    conn = sqlite3.connect(db_path)

    started = datetime.now().isoformat(timespec="seconds")
    rows_read = rows_inserted = rows_updated = rows_rejected = 0

    try:
        with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)

            if not reader.fieldnames or not EXPECTED_COLUMNS.issubset(reader.fieldnames):
                raise SystemExit(
                    f"Unexpected CSV columns. Expected at least {sorted(EXPECTED_COLUMNS)}, "
                    f"found {reader.fieldnames}"
                )

            for row in reader:
                rows_read += 1
                try:
                    ts = datetime.strptime(
                        row["datetime_sast"].strip(),
                        "%Y-%m-%d %H:%M:%S.%f"
                    )
                    timestamp_sast = ts.isoformat(sep=" ", timespec="milliseconds")
                    reading_date = ts.date().isoformat()
                    power_a = parse_float(row.get("Power_A_W"))
                    power_b = parse_float(row.get("Power_B_W"))

                    existing = conn.execute(
                        "SELECT id FROM meter_readings WHERE timestamp_sast = ?",
                        (timestamp_sast,)
                    ).fetchone()

                    conn.execute(
                        """
                        INSERT INTO meter_readings
                            (timestamp_sast, reading_date, power_a_w, power_b_w, source_file)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(timestamp_sast) DO UPDATE SET
                            reading_date=excluded.reading_date,
                            power_a_w=excluded.power_a_w,
                            power_b_w=excluded.power_b_w,
                            source_file=excluded.source_file
                        """,
                        (timestamp_sast, reading_date, power_a, power_b, csv_path.name)
                    )

                    if existing:
                        rows_updated += 1
                    else:
                        rows_inserted += 1

                except Exception:
                    rows_rejected += 1

        completed = datetime.now().isoformat(timespec="seconds")

        conn.execute(
            """
            INSERT INTO extraction_runs
                (source_file, started_at, completed_at, rows_read,
                 rows_inserted, rows_updated, rows_rejected, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                csv_path.name, started, completed, rows_read,
                rows_inserted, rows_updated, rows_rejected,
                "success" if rows_rejected == 0 else "completed_with_rejections",
                "Initial historical CSV import"
            )
        )
        conn.commit()

        print(f"Database: {db_path}")
        print(f"Source:   {csv_path}")
        print(f"Rows read:     {rows_read:,}")
        print(f"Rows inserted: {rows_inserted:,}")
        print(f"Rows updated:  {rows_updated:,}")
        print(f"Rows rejected: {rows_rejected:,}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
