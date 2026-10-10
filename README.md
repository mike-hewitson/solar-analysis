# Solar Meter Data Project

This folder contains the tools used to collect, store, clean, analyse and report
electricity-consumption data from the Tuya smart meter.

The project is intended to become the measured-load foundation for the new-house
solar, inverter and battery design.

---

## 1. Project overview

The data flow is:

```text
Tuya Smart Meter
       │
       ▼
tuya_power_extractor.py
       │
       ▼
solar_meter.db
       │
       ├── raw meter readings
       │
       ▼
solar_analysis.py
       │
       ▼
derived analysis tables
       │
       ▼
solar_report.py
       │
       ├── charts
       └── CSV reports
```

The SQLite database is the **single source of truth**.

Raw meter readings are retained. Cleaning and analysis are performed in derived
database objects so that the original measurements are not destroyed.

---

# 2. Files in the Solar folder

The normal working folder is:

```text
~/Documents/Solar/
```

The important files are:

| File | Purpose |
|---|---|
| `config.ini` | Tuya credentials, database location and analysis settings |
| `tuya_power_extractor.py` | Downloads meter readings from Tuya and stores them in SQLite |
| `solar_analysis.py` | Builds cleaned readings, daily summaries and hourly profiles |
| `solar_report.py` | Produces charts, CSV reports and planning summaries |
| `solar_meter.db` | SQLite database containing the meter data and derived analysis |
| `solar report/` | Output directory created by `solar_report.py` |

There may also be older extractor scripts in the folder. These are historical
versions and should not normally be used unless specifically required.

---

# 3. Configuration

`config.ini` contains the Tuya credentials and project settings.

Typical structure:

```ini
[tuya]
device_id = YOUR_DEVICE_ID
access_id = YOUR_ACCESS_ID
access_key = YOUR_ACCESS_KEY

[database]
path = solar_meter.db

[analysis]
geyser_noise_threshold_w = 20
```

## Important

`config.ini` contains credentials.

Do not publish it, email it unnecessarily, or put it into a public Git repository.

The database path can be relative:

```ini
path = solar_meter.db
```

This means the database is expected in the same project directory as the scripts.

---

# 4. Tuya meter channels

The current meter interpretation is:

### Power_A

`Power_A_W` is the total electricity consumption of the house.

There is currently no solar generation, so this represents electricity being
imported from the grid.

### Power_B

`Power_B_W` is the electricity measured by the CT associated with the geyser.

The geyser is the only significant load on that CT.

Power_B is therefore treated as:

```text
Geyser consumption
```

Small Power_B readings are regarded as CT/electrical noise.

The raw Power_B measurement is retained unchanged in the database.

The cleaning rule is applied only in the analysis layer.

---

# 5. Geyser noise threshold

The current setting is:

```ini
geyser_noise_threshold_w = 20
```

This means:

```text
Power_B < 20 W  →  0 W geyser load
Power_B ≥ 20 W  →  retained as geyser load
```

Negative cleaned values are also treated as zero.

This threshold is deliberately configurable.

As more real-world data is collected, the Power_B distribution can be examined
and the threshold changed if necessary.

---

# 6. Program 1 — Tuya data extractor

## `tuya_power_extractor.py`

This program retrieves meter readings from the Tuya Cloud API and writes them
directly into the SQLite database. It requests `power_a` and `power_b` together
and stores the combined readings in `solar_meter.db`.

The extractor is designed to be **idempotent**:

- Running the same date more than once is safe.
- Existing timestamps are updated rather than duplicated.
- New timestamps are inserted.
- Extraction runs are recorded in the database.

The database therefore contains one row per unique meter timestamp.

## Running the extractor

Change to the Solar directory:

```bash
cd ~/Documents/Solar
```

### Extract yesterday's complete day (default)

```bash
python3 tuya_power_extractor.py
```

If `--date` is omitted, the script selects yesterday using the
`Africa/Johannesburg` timezone. This is the recommended normal daily command.

### Extract a specific date

```bash
python3 tuya_power_extractor.py --date 2026-10-09
```

The extractor requests the complete South African calendar day, from midnight
on the selected date up to (but not including) midnight on the following day.

### Optionally write a CSV copy

```bash
python3 tuya_power_extractor.py --output-csv yesterday.csv
```

A relative CSV path is written in the Solar project directory. CSV output is
optional; SQLite remains the primary data store.

## Current performance defaults

The following settings are the tested defaults in `tuya_power_extractor.py`:

| Setting | Default |
|---|---:|
| Extraction window | 15 minutes |
| Ordinary pagination delay | 0.25 seconds |
| Initial rate-limit backoff | 1.5 seconds |
| Maximum rate-limit backoff | 30 seconds |
| Channels requested together | `power_a`, `power_b` |

The 15-minute windows and 0.25-second pagination delay were tested on more than
one extraction run. The faster run returned the same reading counts as the
original slower run, with no API failures, rate-limit retries or rejected rows.
These values are a practical baseline; further performance tuning is
intentionally paused unless a problem appears.

The script retains a 1.5-second initial backoff for actual rate-limit responses,
which doubles as needed up to 30 seconds. This protection is separate from the
short ordinary pagination delay.

For a one-off run, either tuning option can be overridden:

```bash
# Use 15-minute windows with the default 0.25-second pagination delay
python3 tuya_power_extractor.py --date 2026-10-09

# Example: override the window size or pagination delay
python3 tuya_power_extractor.py --date 2026-10-09 --window-minutes 10
python3 tuya_power_extractor.py --date 2026-10-09 --pagination-delay 0.5
```

The extractor prints a timing summary at the end, including API requests,
request time, pagination and between-window sleeps, rate-limit retries, and
SQLite storage time. This makes it easier to spot a change in API behaviour.

## Configuration

Configuration is held separately in `config.ini`:

```ini
[tuya]
device_id = YOUR_DEVICE_ID
access_id = YOUR_ACCESS_ID
access_key = YOUR_ACCESS_KEY

[database]
path = solar_meter.db

[analysis]
geyser_noise_threshold_w = 20
```

Do not put real Tuya credentials into files that are shared or uploaded.

---

# 7. Program 2 — Analysis layer

## `solar_analysis.py`

This program takes the raw readings in `solar_meter.db` and creates the
derived analysis layer.

It does **not** modify or delete the raw meter readings.

Run:

```bash
cd ~/Documents/Solar
python3 solar_analysis.py
```

It rebuilds the derived analysis from the raw readings.

This means it is safe to run again after:

- downloading additional readings
- changing the geyser noise threshold
- correcting analysis logic
- rebuilding the database from a backup

---

# 8. What the analysis layer calculates

## Clean meter readings

The analysis creates:

```text
v_meter_readings_clean
```

This provides:

- timestamp
- total house power
- cleaned geyser power
- non-geyser power
- interval energy

The important relationship is:

```text
Non-geyser power
    =
Total house power - cleaned geyser power
```

---

## Interval energy

Energy is calculated from the measured power and the elapsed time between
successive readings.

Only intervals of up to five minutes are used.

If a large gap occurs, the program does not invent energy for the missing period.

This is important because the Tuya API sometimes has small gaps between readings.

---

# 9. Daily summary

The analysis creates:

```text
daily_load_summary
```

It contains:

- number of readings
- first timestamp
- last timestamp
- coverage span
- complete-day flag
- total energy
- geyser energy
- non-geyser energy
- average total power
- average geyser power
- average non-geyser power
- peak total power
- peak non-geyser power

A day is considered complete when there is suitable coverage near both the
beginning and end of the calendar day.

Partial days are retained but are not used for the average hourly profile.

---

# 10. Hourly load profile

The analysis creates:

```text
hourly_load_profile
```

This uses **complete days only**.

For each hour it calculates:

- average total power
- average geyser power
- average non-geyser power
- average total energy
- average geyser energy
- average non-geyser energy

Hourly energy is calculated by:

1. summing the individual meter intervals within each hour for each complete day
2. then averaging those hourly totals across complete days

This is important.

It prevents a common error where averaging the energy of individual
10-second meter readings produces the energy of only one sampling interval
instead of the energy consumed during the entire hour.

---

# 11. Program 3 — Report generator

## `solar_report.py`

This program reads the derived analysis tables and produces graphical and CSV
reports.

Run:

```bash
cd ~/Documents/Solar
python3 solar_report.py
```

It creates:

```text
solar report/
```

inside the project directory.

---

# 12. Report files

The report generator currently produces five files.

## Daily chart

```text
solar daily load profile.png
```

Shows daily electricity consumption split into:

- geyser
- non-geyser

Complete days are identified separately.

---

## Hourly power chart

```text
solar hourly load profile.png
```

Shows the average hourly:

- total load
- geyser load
- non-geyser load

Complete days only.

---

## Hourly energy chart

```text
solar hourly energy profile.png
```

Shows the actual average kWh consumed during each hour.

This is particularly useful for solar modelling.

---

## Daily CSV

```text
solar daily load summary.csv
```

Contains the daily analysis data in spreadsheet-compatible format.

---

## Hourly CSV

```text
solar hourly load profile.csv
```

Contains the complete-day hourly profile.

---

# 13. Normal operating procedure

Once the system is established, the normal workflow is:

### Step 1 — Download meter data

For the normal daily run, extract yesterday's complete day:

```bash
cd ~/Documents/Solar
python3 tuya_power_extractor.py
```

To extract a specific date instead:

```bash
python3 tuya_power_extractor.py --date YYYY-MM-DD
```

For example:

```bash
python3 tuya_power_extractor.py --date 2026-10-09
```

### Step 2 — Rebuild the analysis

```bash
python3 solar_analysis.py
```

### Step 3 — Generate the report

```bash
python3 solar_report.py
```

The complete workflow is therefore:

```text
Tuya
 ↓
tuya_power_extractor.py
 ↓
solar_meter.db
 ↓
solar_analysis.py
 ↓
derived analysis
 ↓
solar_report.py
 ↓
charts + CSV reports
```

---

# 14. Database

The database is:

```text
solar_meter.db
```

It is a standard SQLite database and requires no database server.

The main raw table is:

```text
meter_readings
```

Important fields include:

```text
timestamp_sast
reading_date
power_a_w
power_b_w
source_file
imported_at
```

There is a unique constraint on the timestamp, preventing duplicate readings.

The database also contains:

```text
extraction_runs
```

which records extraction attempts and their results.

Derived objects include:

```text
v_meter_readings_clean
daily_load_summary
hourly_load_profile
```

The derived objects can safely be rebuilt from the raw data.

---

# 15. Backups

The SQLite database should be backed up periodically.

Recommended:

```text
solar_meter.db
```

should be included in normal Mac/Time Machine backups.

Avoid placing an actively-written SQLite database inside a synchronised
Dropbox/iCloud/OneDrive folder if possible.

A simple manual backup can be made with:

```bash
cp ~/Documents/Solar/solar_meter.db \
   ~/Documents/Solar/solar_meter_backup.db
```

For a proper backup while the database is in use, SQLite's backup mechanism
is preferable to simply copying the file.

---

# 16. Current data interpretation

The current project assumes:

```text
Power_A = total house load
Power_B = geyser load
```

Therefore:

```text
Non-geyser load = Power_A - cleaned Power_B
```

This interpretation should be revisited if the electrical installation changes.

For example, if additional circuits are later connected to the Power_B CT,
Power_B will no longer represent the geyser alone.

---

# 17. Current limitations

At present, the hourly profile is based only on complete calendar days.

This is intentional.

Partial days can seriously distort hourly averages.

For example, if data starts at 15:00, the missing morning hours must not be
interpreted as zero consumption.

As more days accumulate, the hourly profile will automatically become more
representative.

---

# 18. Solar-planning use

The eventual purpose of this data is to provide a measured load profile for
the house's solar design.

Important metrics include:

- daily energy consumption
- daytime energy consumption
- morning demand
- evening demand
- overnight demand
- geyser energy
- non-geyser energy
- peak instantaneous load
- sustained load
- solar-window consumption
- load that can potentially be shifted
- battery discharge requirements

The current report already calculates several useful windows:

```text
06:00–09:00
09:00–15:00
15:00–18:00
09:00–17:00
```

These will eventually be compared with the expected PV production profile from
the proposed east/west roof installation.

---

# 19. Current solar-system context

The measured load data is intended to support the new-house solar design.

Current design assumptions include:

- east/west roof
- approximately 22° roof pitch
- approximately 60 m² west-facing roof
- approximately 100 m² east-facing roof
- no significant shading
- no grid export
- entire house inverter-backed during normal operation
- grid supplements solar/battery when required
- electric oven is inverter-backed
- gas hob
- no electric space heating
- no swimming pool
- EV-ready
- geyser is a controllable load
- proposed battery modelling includes a 15 kWh battery option

The measured load profile should eventually replace the generic consumption
assumptions used in earlier solar simulations.

---

# 20. Python environment

The current project has been running using:

```bash
python3
```

The Mac's current Python installation is also producing a urllib3 warning about
LibreSSL.

This warning does not currently prevent the extractor from working.

Matplotlib is required by:

```text
solar_report.py
```

If it is not installed:

```bash
python3 -m pip install --user matplotlib
```

Then verify:

```bash
python3 -c "import matplotlib; print(matplotlib.__version__)"
```

A future improvement would be to move the project to a Homebrew Python
installation and/or a dedicated Python virtual environment.

That is not required for the current system to operate.

---

# 21. Troubleshooting

## `ModuleNotFoundError: No module named 'matplotlib'`

Install matplotlib:

```bash
python3 -m pip install --user matplotlib
```

Then rerun:

```bash
python3 solar_report.py
```

---

## Tuya authentication error

Check:

```text
config.ini
```

particularly:

```ini
device_id =
access_id =
access_key =
```

Do not put these credentials directly on the command line unless temporarily
debugging.

---

## Database not found

Check that the current directory is:

```bash
cd ~/Documents/Solar
```

and that:

```text
solar_meter.db
```

exists there.

The configured path is read from:

```ini
[database]
path = solar_meter.db
```

---

## No hourly data

Run:

```bash
python3 solar_analysis.py
```

The hourly profile only uses complete days.

If there are no complete days yet, the hourly profile will contain no useful
data.

---

# 22. Useful checks

Check that the database exists:

```bash
ls -lh ~/Documents/Solar/solar_meter.db
```

Check the project files:

```bash
ls -lh ~/Documents/Solar/
```

Run the extractor help:

```bash
python3 tuya_power_extractor.py --help
```

Run analysis:

```bash
python3 solar_analysis.py
```

Generate reports:

```bash
python3 solar_report.py
```

---

# 23. Recommended long-term workflow

As data accumulates, the preferred workflow is:

```text
1. Extract new Tuya data
        ↓
2. Rebuild analysis
        ↓
3. Generate report
        ↓
4. Review charts
        ↓
5. Periodically use the measured profile
   in the solar/PV/battery model
```

There is no need to manually edit the SQLite database.

The raw readings should be treated as historical measurements.

If an analysis rule changes — for example, the geyser noise threshold changes
from 20 W to 25 W — simply change `config.ini` and rerun:

```bash
python3 solar_analysis.py
```

The derived analysis will be rebuilt using the new rule while the raw readings
remain unchanged.

---

# 24. Future extensions

The project is deliberately structured so that further analysis can be added
without changing the raw data collection.

Potential future additions include:

- multi-day load-profile charts
- weekday/weekend comparisons
- monthly consumption summaries
- load-duration curves
- peak-demand analysis
- geyser runtime analysis
- daytime versus night-time consumption
- solar self-consumption modelling
- PV production simulation
- battery state-of-charge simulation
- inverter loading analysis
- geyser load-shedding/load-management simulation
- comparison of measured demand against proposed inverter sizes

The ultimate objective is to use the measured household profile to replace
generic assumptions in the solar-system design.

---

## Quick reference

```bash
# Go to project
cd ~/Documents/Solar

# Download yesterday's complete day (default)
python3 tuya_power_extractor.py

# Or download a specific day
python3 tuya_power_extractor.py --date YYYY-MM-DD

# Rebuild analysis
python3 solar_analysis.py

# Generate charts and CSV reports
python3 solar_report.py
```

That's the complete normal workflow.
