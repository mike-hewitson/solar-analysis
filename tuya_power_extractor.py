#!/usr/bin/env python3
"""Daily Tuya smart-meter extractor -> SQLite.

Usage:
    python3 tuya_power_extractor.py --date 2026-09-29

Reads Tuya credentials and database path from config.ini in this directory.
"""

import argparse
import configparser
import csv
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

try:
    from tuya_connector import TuyaOpenAPI
except ImportError:
    print("Install with: python3 -m pip install tuya-connector-python requests")
    sys.exit(1)

API = "https://openapi.tuyaeu.com"
PATH = "/v2.1/cloud/thing/{}/report-logs"
TZ = ZoneInfo("Africa/Johannesburg")
PAGE_SIZE = 20
WINDOW_MS = 5 * 60 * 1000
TIMEOUT = 30
BASE_DELAY = 1.5
MAX_DELAY = 30.0

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.ini"


def load_config():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Configuration file not found: {CONFIG_FILE}")

    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)

    for section in ("tuya", "database"):
        if not config.has_section(section):
            raise ValueError(f"config.ini is missing the [{section}] section.")

    values = {}
    for key in ("device_id", "access_id", "access_key"):
        values[key] = config.get("tuya", key, fallback="").strip()
        if not values[key]:
            raise ValueError(f"config.ini is missing tuya setting: {key}")

    db_value = config.get("database", "path", fallback="solar_meter.db").strip()
    db_path = Path(db_value or "solar_meter.db")
    if not db_path.is_absolute():
        db_path = SCRIPT_DIR / db_path
    values["db_path"] = db_path
    return values


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be YYYY-MM-DD.")


def day_range(day):
    start = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
    return start, start + timedelta(days=1)


def to_ms(value):
    return int(value.timestamp() * 1000)


def convert_power(code, raw):
    value = float(raw)
    if code in ("power_a", "power_b"):
        # Both Tuya channels use the sign convention already established
        # for this meter; returned values are physical consumption in watts.
        return -value / 10.0
    raise ValueError(f"Unsupported code: {code}")


def signed_get(api, path, params):
    refresh = getattr(api, "_TuyaOpenAPI__refresh_access_token_if_need", None)
    if refresh:
        refresh(path)

    calculate_sign = getattr(api, "_calculate_sign", None)
    if calculate_sign is None:
        raise RuntimeError("Installed tuya-connector-python lacks required signing support.")

    sign, request_time = calculate_sign("GET", path, params, None)
    headers = {
        "client_id": api.access_id,
        "sign": sign,
        "sign_method": "HMAC-SHA256",
        "access_token": api.token_info.access_token,
        "t": str(request_time),
    }

    response = requests.get(API + path, params=params, headers=headers, timeout=TIMEOUT)
    try:
        return response.json()
    except ValueError:
        raise RuntimeError(
            f"Tuya returned HTTP {response.status_code}: {response.text[:500]}"
        )


def fetch_page(api, device_id, code, start_ms, end_ms, last_row_key=None):
    params = {
        "codes": code,
        "start_time": start_ms,
        "end_time": end_ms,
        "size": PAGE_SIZE,
    }
    if last_row_key:
        params["last_row_key"] = last_row_key
    return signed_get(api, PATH.format(device_id), params)


def fetch_window(api, device_id, code, start_ms, end_ms):
    rows = []
    last_row_key = None
    delay = BASE_DELAY

    while True:
        while True:
            response = fetch_page(api, device_id, code, start_ms, end_ms, last_row_key)
            if response.get("success"):
                break

            if response.get("code") in (40000309, 429):
                print(f"    Rate limited; waiting {delay:.1f}s...")
                time.sleep(delay)
                delay = min(MAX_DELAY, delay * 2)
                continue

            raise RuntimeError(f"Tuya API error for {code}: {response}")

        result = response.get("result") or {}
        for item in result.get("logs") or []:
            try:
                rows.append({
                    "event_time_ms": int(item["eventTime"]),
                    "watts": convert_power(code, item["value"]),
                })
            except (KeyError, TypeError, ValueError):
                continue

        if not result.get("hasMore"):
            break

        new_key = result.get("lastRowKey")
        if not new_key:
            raise RuntimeError(f"Tuya returned hasMore=true for {code}, but no lastRowKey.")
        if new_key == last_row_key:
            raise RuntimeError(f"Tuya pagination key did not change for {code}.")

        last_row_key = new_key
        time.sleep(delay)

    return rows


def fetch_all(api, device_id, code, start_ms, end_ms):
    readings = {}
    cursor = start_ms
    chunk = 0
    total = (end_ms - start_ms + WINDOW_MS - 1) // WINDOW_MS

    while cursor < end_ms:
        chunk += 1
        chunk_end = min(cursor + WINDOW_MS, end_ms)
        start_dt = datetime.fromtimestamp(cursor / 1000, TZ)
        end_dt = datetime.fromtimestamp(chunk_end / 1000, TZ)

        print(f"  {code}: chunk {chunk}/{total} "
              f"{start_dt:%Y-%m-%d %H:%M:%S}–{end_dt:%H:%M:%S}")

        rows = fetch_window(api, device_id, code, cursor, chunk_end)
        for row in rows:
            readings[row["event_time_ms"]] = row["watts"]

        print(f"    Retrieved {len(rows)} readings")
        cursor = chunk_end
        if cursor < end_ms:
            time.sleep(BASE_DELAY)

    return sorted(readings.items())


def nearest(times, target, tolerance_ms=2500):
    if not times:
        return None

    lo, hi = 0, len(times)
    while lo < hi:
        mid = (lo + hi) // 2
        if times[mid] < target:
            lo = mid + 1
        else:
            hi = mid

    candidates = []
    if lo < len(times):
        candidates.append(times[lo])
    if lo > 0:
        candidates.append(times[lo - 1])

    best = min(candidates, key=lambda value: abs(value - target))
    return best if abs(best - target) <= tolerance_ms else None


def combine(power_a, power_b):
    a, b = dict(power_a), dict(power_b)
    a_times, b_times = sorted(a), sorted(b)
    rows = []
    matched_b = set()

    for timestamp in a_times:
        b_timestamp = nearest(b_times, timestamp)
        if b_timestamp is not None:
            matched_b.add(b_timestamp)

        local_dt = datetime.fromtimestamp(timestamp / 1000, TZ)
        rows.append({
            "timestamp": local_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "power_a_w": a[timestamp],
            "power_b_w": None if b_timestamp is None else b[b_timestamp],
        })

    for timestamp in b_times:
        if timestamp in matched_b or nearest(a_times, timestamp) is not None:
            continue
        local_dt = datetime.fromtimestamp(timestamp / 1000, TZ)
        rows.append({
            "timestamp": local_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "power_a_w": None,
            "power_b_w": b[timestamp],
        })

    rows.sort(key=lambda row: row["timestamp"])
    return rows


def connect_database(db_path):
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def store_readings(connection, rows, source_name):
    inserted = updated = 0

    for row in rows:
        timestamp = row["timestamp"]
        existing = connection.execute(
            "SELECT 1 FROM meter_readings WHERE timestamp_sast = ?",
            (timestamp,),
        ).fetchone()

        connection.execute(
            """
            INSERT INTO meter_readings
                (timestamp_sast, reading_date, power_a_w, power_b_w, source_file)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(timestamp_sast) DO UPDATE SET
                power_a_w = excluded.power_a_w,
                power_b_w = excluded.power_b_w,
                source_file = excluded.source_file
            """,
            (
                timestamp,
                timestamp[:10],
                row["power_a_w"],
                row["power_b_w"],
                source_name,
            ),
        )

        if existing is None:
            inserted += 1
        else:
            updated += 1

    return inserted, updated


def record_run(connection, source_name, started_at, completed_at,
               rows_read, rows_inserted, rows_updated, rows_rejected,
               status, notes):
    connection.execute(
        """
        INSERT INTO extraction_runs
            (source_file, started_at, completed_at, rows_read, rows_inserted,
             rows_updated, rows_rejected, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_name, started_at, completed_at, rows_read, rows_inserted,
         rows_updated, rows_rejected, status, notes),
    )


def write_csv(rows, filename):
    with open(filename, "w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(["datetime_sast", "Power_A_W", "Power_B_W"])
        for row in rows:
            writer.writerow([
                row["timestamp"],
                "" if row["power_a_w"] is None else f'{row["power_a_w"]:.1f}',
                "" if row["power_b_w"] is None else f'{row["power_b_w"]:.1f}',
            ])


def main():
    parser = argparse.ArgumentParser(
        description="Extract one complete SAST day from Tuya into solar_meter.db."
    )
    default_date = datetime.now(TZ).date() - timedelta(days=1)
    parser.add_argument(
        "--date", type=parse_date, default=default_date,
        help=(
            "Complete SAST day, e.g. 2026-09-29 "
            f"(default: yesterday, {default_date:%Y-%m-%d})"
        ),
    )
    parser.add_argument(
        "--output-csv",
        help="Optional CSV output path for this extraction.",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    start, end = day_range(args.date)
    started_at = datetime.now(TZ).isoformat()
    source_name = f"Tuya API {args.date}"

    print("========================================")
    print("Tuya daily solar-meter extraction")
    print("========================================")
    print(f"Date:       {args.date}")
    print(f"Start:      {start}")
    print(f"End:        {end}")
    print(f"Config:     {CONFIG_FILE}")
    print(f"Database:   {config['db_path']}")
    print("========================================")

    connection = connect_database(config["db_path"])

    try:
        print("\nConnecting to Tuya Cloud (Central Europe)...")
        api = TuyaOpenAPI(API, config["access_id"], config["access_key"])
        api.connect()

        start_ms, end_ms = to_ms(start), to_ms(end)

        print(f"\nRequesting power_a for {args.date}...")
        power_a = fetch_all(
            api, config["device_id"], "power_a", start_ms, end_ms
        )

        print("\nPausing before power_b...")
        time.sleep(3)

        print(f"\nRequesting power_b for {args.date}...")
        power_b = fetch_all(
            api, config["device_id"], "power_b", start_ms, end_ms
        )

        rows = combine(power_a, power_b)

        print("\nStoring readings in SQLite...")
        inserted, updated = store_readings(
            connection, rows, source_name
        )

        completed_at = datetime.now(TZ).isoformat()
        record_run(
            connection, source_name, started_at, completed_at,
            len(rows), inserted, updated, 0, "success",
            "Direct Tuya API extraction to SQLite.",
        )
        connection.commit()

        if args.output_csv:
            output_csv = Path(args.output_csv)
            if not output_csv.is_absolute():
                output_csv = SCRIPT_DIR / output_csv
            write_csv(rows, output_csv)

        print("\n========================================")
        print("Extraction complete")
        print("========================================")
        print(f"Power_A readings: {len(power_a)}")
        print(f"Power_B readings: {len(power_b)}")
        print(f"Combined rows:    {len(rows)}")
        print(f"Rows inserted:    {inserted}")
        print(f"Rows updated:     {updated}")
        print("Rows rejected:    0")
        print(f"Database:         {config['db_path']}")
        if args.output_csv:
            print(f"CSV:              {output_csv}")
        print("========================================")

    except Exception as exc:
        connection.rollback()
        completed_at = datetime.now(TZ).isoformat()
        try:
            record_run(
                connection, source_name, started_at, completed_at,
                0, 0, 0, 0, "failed", str(exc),
            )
            connection.commit()
        except Exception:
            connection.rollback()
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
