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
from collections import defaultdict
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
DEFAULT_WINDOW_MINUTES = 15
DEFAULT_PAGINATION_DELAY = 0.25
TIMEOUT = 30
BASE_DELAY = 1.5
MAX_DELAY = 30.0

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.ini"

# Runtime instrumentation. Timings are printed at the end of each run.
METRICS = {
    "api_requests": 0,
    "api_seconds": 0.0,
    "api_failures": 0,
    "rate_limit_retries": 0,
    "pages": 0,
    "windows": 0,
    "window_seconds": 0.0,
    "parse_seconds": 0.0,
    "sleep_between_windows_seconds": 0.0,
    "sleep_pagination_seconds": 0.0,
    "sleep_rate_limit_seconds": 0.0,
}



def tracked_sleep(seconds, category):
    """Sleep and record elapsed time by reason."""
    if seconds <= 0:
        return
    started = time.perf_counter()
    time.sleep(seconds)
    elapsed = time.perf_counter() - started
    key = f"sleep_{category}_seconds"
    METRICS[key] = METRICS.get(key, 0.0) + elapsed


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

    started = time.perf_counter()
    METRICS["api_requests"] += 1
    try:
        response = requests.get(API + path, params=params, headers=headers, timeout=TIMEOUT)
        try:
            payload = response.json()
        except ValueError:
            METRICS["api_failures"] += 1
            raise RuntimeError(
                f"Tuya returned non-JSON HTTP {response.status_code}: {response.text[:500]}"
            )
        if response.status_code >= 400:
            METRICS["api_failures"] += 1
        return payload
    except requests.RequestException:
        METRICS["api_failures"] += 1
        raise
    finally:
        METRICS["api_seconds"] += time.perf_counter() - started


def fetch_page(api, device_id, codes, start_ms, end_ms, last_row_key=None):
    params = {
        "codes": ",".join(codes),
        "start_time": start_ms,
        "end_time": end_ms,
        "size": PAGE_SIZE,
    }
    if last_row_key:
        params["last_row_key"] = last_row_key
    return signed_get(api, PATH.format(device_id), params)


def fetch_window(api, device_id, codes, start_ms, end_ms, pagination_delay):
    """Fetch all requested data-point codes in one time window."""
    rows_by_code = {code: [] for code in codes}
    last_row_key = None
    delay = BASE_DELAY

    while True:
        while True:
            response = fetch_page(api, device_id, codes, start_ms, end_ms, last_row_key)
            if response.get("success"):
                break

            if response.get("code") in (40000309, 429):
                METRICS["rate_limit_retries"] += 1
                print(f"    Rate limited; waiting {delay:.1f}s...")
                tracked_sleep(delay, "rate_limit")
                delay = min(MAX_DELAY, delay * 2)
                continue

            raise RuntimeError(f"Tuya API error for {','.join(codes)}: {response}")

        METRICS["pages"] += 1
        result = response.get("result") or {}
        parse_started = time.perf_counter()
        for item in result.get("logs") or []:
            try:
                code = item.get("code")
                if code not in rows_by_code:
                    continue
                # Tuya documentation uses event_time; retain eventTime compatibility
                # with responses returned by some versions of the API.
                event_time = item.get("event_time", item.get("eventTime"))
                rows_by_code[code].append({
                    "event_time_ms": int(event_time),
                    "watts": convert_power(code, item["value"]),
                })
            except (KeyError, TypeError, ValueError):
                continue

        METRICS["parse_seconds"] += time.perf_counter() - parse_started

        if not result.get("hasMore", result.get("has_more", False)):
            break

        new_key = result.get("lastRowKey", result.get("last_row_key"))
        if not new_key:
            raise RuntimeError(
                f"Tuya returned hasMore=true for {','.join(codes)}, but no last row key."
            )
        if new_key == last_row_key:
            raise RuntimeError(f"Tuya pagination key did not change for {','.join(codes)}.")

        last_row_key = new_key
        tracked_sleep(pagination_delay, "pagination")

    return rows_by_code


def fetch_all(api, device_id, codes, start_ms, end_ms, window_minutes, pagination_delay):
    readings = {code: {} for code in codes}
    cursor = start_ms
    chunk = 0
    window_ms = window_minutes * 60 * 1000
    total = (end_ms - start_ms + window_ms - 1) // window_ms
    started = time.perf_counter()

    while cursor < end_ms:
        chunk += 1
        chunk_end = min(cursor + window_ms, end_ms)
        start_dt = datetime.fromtimestamp(cursor / 1000, TZ)
        end_dt = datetime.fromtimestamp(chunk_end / 1000, TZ)

        print(f"  Combined channels: chunk {chunk}/{total} "
              f"{start_dt:%Y-%m-%d %H:%M:%S}–{end_dt:%H:%M:%S}")

        window_started = time.perf_counter()
        rows_by_code = fetch_window(
            api, device_id, codes, cursor, chunk_end, pagination_delay
        )
        METRICS["windows"] += 1
        METRICS["window_seconds"] += time.perf_counter() - window_started

        for code in codes:
            rows = rows_by_code[code]
            for row in rows:
                readings[code][row["event_time_ms"]] = row["watts"]
            print(f"    {code}: retrieved {len(rows)} readings")

        cursor = chunk_end
        if cursor < end_ms:
            tracked_sleep(BASE_DELAY, "between_windows")

    elapsed = time.perf_counter() - started
    print(f"  Combined-channel extraction elapsed: {elapsed:.2f}s")
    return {code: sorted(values.items()) for code, values in readings.items()}


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
        "--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES,
        help=(
            "Window size in minutes (default: 15). Larger windows may require "
            "more pagination; override with --window-minutes if needed."
        ),
    )
    parser.add_argument(
        "--pagination-delay", type=float, default=DEFAULT_PAGINATION_DELAY,
        help=(
            "Seconds to wait between ordinary pagination pages (default: 0.25). "
            "Rate-limit backoff remains separate and starts at 1.5 seconds."
        ),
    )
    parser.add_argument(
        "--output-csv",
        help="Optional CSV output path for this extraction.",
    )
    args = parser.parse_args()
    if args.window_minutes < 1:
        parser.error("--window-minutes must be at least 1")
    if args.pagination_delay < 0:
        parser.error("--pagination-delay must be zero or greater")

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
    print(f"Window:     {args.window_minutes} minute(s)")
    print(f"Pagination: {args.pagination_delay:.2f}s ordinary delay")
    print(f"Rate limit: {BASE_DELAY:.2f}s initial backoff, max {MAX_DELAY:.1f}s")
    print("========================================")

    connection = connect_database(config["db_path"])

    try:
        print("\nConnecting to Tuya Cloud (Central Europe)...")
        api = TuyaOpenAPI(API, config["access_id"], config["access_key"])
        api.connect()

        start_ms, end_ms = to_ms(start), to_ms(end)

        extraction_started = time.perf_counter()
        print(f"\nRequesting power_a and power_b together for {args.date}...")
        readings = fetch_all(
            api, config["device_id"], ["power_a", "power_b"], start_ms, end_ms,
            args.window_minutes, args.pagination_delay,
        )
        power_a = readings["power_a"]
        power_b = readings["power_b"]

        combine_started = time.perf_counter()
        rows = combine(power_a, power_b)
        combine_seconds = time.perf_counter() - combine_started

        print("\nStoring readings in SQLite...")
        db_started = time.perf_counter()
        inserted, updated = store_readings(
            connection, rows, source_name
        )
        db_seconds = time.perf_counter() - db_started

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
        print("\nTiming summary")
        print(f"  API requests:       {METRICS['api_requests']}")
        print(f"  API request time:   {METRICS['api_seconds']:.2f}s")
        print(f"  API failures:       {METRICS['api_failures']}")
        print(f"  Rate-limit retries: {METRICS['rate_limit_retries']}")
        print(f"  API pages:          {METRICS['pages']}")
        print(f"  Time windows:       {METRICS['windows']}")
        print(f"  Window elapsed:     {METRICS['window_seconds']:.2f}s")
        print(f"  Parsing time:       {METRICS['parse_seconds']:.2f}s")
        print(f"  Sleep: between windows  {METRICS['sleep_between_windows_seconds']:.2f}s")
        print(f"  Sleep: pagination       {METRICS['sleep_pagination_seconds']:.2f}s")
        print(f"  Sleep: rate limits      {METRICS['sleep_rate_limit_seconds']:.2f}s")
        sleep_total = sum(
            METRICS[key] for key in (
                "sleep_between_windows_seconds",
                "sleep_pagination_seconds",
                "sleep_rate_limit_seconds",
            )
        )
        print(f"  Sleep total:        {sleep_total:.2f}s")
        print(f"  Combine time:       {combine_seconds:.2f}s")
        print(f"  Database time:      {db_seconds:.2f}s")
        print(f"  Total work time:    {time.perf_counter() - extraction_started:.2f}s")
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
