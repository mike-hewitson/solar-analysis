PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meter_readings (
    id INTEGER PRIMARY KEY,
    timestamp_sast TEXT NOT NULL,
    reading_date TEXT NOT NULL,
    power_a_w REAL,
    power_b_w REAL,
    source_file TEXT,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(timestamp_sast)
);

CREATE INDEX IF NOT EXISTS idx_meter_readings_timestamp
    ON meter_readings(timestamp_sast);

CREATE INDEX IF NOT EXISTS idx_meter_readings_date
    ON meter_readings(reading_date);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    rows_read INTEGER,
    rows_inserted INTEGER,
    rows_updated INTEGER,
    rows_rejected INTEGER,
    status TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS database_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR REPLACE INTO database_info(key, value)
VALUES
    ('database_version', '1.0'),
    ('timezone', 'Africa/Johannesburg'),
    ('meter_total_channel', 'Power_A_W'),
    ('meter_geyser_channel', 'Power_B_W'),
    ('geyser_noise_policy', 'Power_B is retained raw; cleaning/thresholding belongs in the derived analysis layer'),
    ('source_format', 'Tuya CSV: datetime_sast,Power_A_W,Power_B_W');
