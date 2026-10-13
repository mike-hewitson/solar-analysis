#!/usr/bin/env python3
from solar_db import connect_database, database_backend, database_label

con = connect_database()

print("========================================")
print("Solar database integrity check")
print("========================================")
print(f"Database: {database_label()}")

# Overall range/count
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

# Duplicate timestamps
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

# Daily coverage
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

for r in daily:
    print(
        f"{r[0]:<12} {r[1]:>7,} {r[2]:>7,} {r[3]:>7,} "
        f"{r[4]:>24} {r[5]:>24}"
    )

# Gaps between consecutive readings
print("\nLargest timestamp gaps:")
print("-" * 78)

if database_backend() in {"postgres", "postgresql"}:
    gap_seconds = "EXTRACT(EPOCH FROM (timestamp_sast::timestamp - previous::timestamp))"
    rounded_gap = f"ROUND(({gap_seconds})::numeric, 3)"
else:
    gap_seconds = "(julianday(timestamp_sast) - julianday(previous)) * 86400"
    rounded_gap = f"ROUND(({gap_seconds}), 3)"
gaps = con.execute(f"""
    WITH ordered AS (
        SELECT
            timestamp_sast,
            LAG(timestamp_sast) OVER (ORDER BY timestamp_sast) AS previous
        FROM meter_readings
    )
    SELECT
        previous,
        timestamp_sast,
        {rounded_gap} AS gap_seconds
    FROM ordered
    WHERE previous IS NOT NULL
    ORDER BY gap_seconds DESC
    LIMIT 15
""").fetchall()

for previous, current, seconds in gaps:
    print(f"{seconds:>10.3f}s  {previous} -> {current}")

# Extraction history
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

for r in runs:
    print(
        f"{r[0]} | {r[7]} | read={r[3]} "
        f"inserted={r[4]} updated={r[5]} rejected={r[6]}"
    )

print("\n========================================")
print("Check complete")
print("========================================")

con.close()
