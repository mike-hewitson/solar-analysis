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
PostgreSQL: solar_meter (Ubuntu mini)
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

The live database is PostgreSQL on the Ubuntu mini. The MacBook's
`solar_meter.db` SQLite file is retained as a migration-era fallback; it is not
the active database used by the mini's scheduled jobs.

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
| `tuya_power_extractor.py` | Downloads meter readings from Tuya and stores them in the configured database |
| `solar_analysis.py` | Builds cleaned readings, daily summaries and hourly profiles |
| `solar_report.py` | Produces charts, CSV reports and planning summaries |
| `plot_non_geyser_power.py` | Plots total and/or non-geyser power for a selected day from existing database readings |
| `solar_meter.db` | MacBook SQLite database retained as a fallback copy |
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
to the database configured in `config.ini`. It requests `power_a` and `power_b`
together and stores the combined readings.

The extractor is designed to be **idempotent**:

- Re-fetching an overlapping time range is safe.
- Existing timestamps are updated rather than duplicated.
- New timestamps are inserted.
- Extraction runs are recorded in the database.
- No-argument runs save a successful catch-up endpoint in `database_info`.

The database therefore contains one row per unique meter timestamp.

## Running the extractor

Change to the Solar directory:

```bash
cd ~/Documents/Solar
```

### Catch up from the saved endpoint (default)

```bash
python3 tuya_power_extractor.py
```

With no arguments, the script resumes from the saved endpoint. On its first
run, it uses the day after the most recent successful legacy daily extraction.
Each run re-fetches a 10-minute overlap to pick up late Tuya readings and stops
5 minutes before the current time. The endpoint advances only after readings,
run history, and checkpoint are committed successfully. A missed manual run
therefore makes the next run cover the whole gap. This is the normal command
for manual catch-up runs, such as when testing from the MacBook. On the Ubuntu
mini, systemd runs this no-argument catch-up automatically every two hours.

### Extract a specific date

```bash
python3 tuya_power_extractor.py --date 2026-10-09
```

The extractor requests the complete South African calendar day, from midnight
on the selected date up to (but not including) midnight on the following day.
Use this option for historical backfills; it does not move the catch-up cursor.

### Failure notification

The extractor can send a failure alert to the existing ntfy topic. Set the
`SOLAR_NTFY_TOPIC` environment variable before running it; the topic is not
stored in the script or project files. Successful runs do not send a message.
If the variable is unset, the extraction still runs normally and reports that
failure alerts are not configured if a run fails.

On the mini, the systemd wrapper reads `NTFY_TOPIC` from
`~/.config/solar-db-backup/ntfy.env` and passes it to the extractor as
`SOLAR_NTFY_TOPIC`. The topic value remains in that private environment file.

## Mini deployment and schedule

The routine extractor runs on the Ubuntu Mac mini (`pihole`) from
`~/solar-tuya-extractor`, using the mini's local PostgreSQL database. Its
systemd oneshot service runs the extractor without arguments, and its timer
runs every two hours at even hours in `Africa/Johannesburg` time. The timer is
persistent, so a run missed while the mini is off is started after it returns.

The first catch-up on the mini completed successfully on 2026-10-13: 54 rows
were inserted, 57 overlapping rows were updated, and the Tuya API reported no
failures.

Useful commands to check or start the mini service:

```bash
systemctl list-timers --all --no-pager | grep solar-tuya
sudo systemctl status solar-tuya-extractor.service --no-pager -l
sudo journalctl -u solar-tuya-extractor.service -n 40 --no-pager
sudo systemctl start solar-tuya-extractor.service
```

Avoid starting a manual extractor run on the MacBook while a mini run is in
progress. Both installations use the same PostgreSQL checkpoint and database.

The mini runs the following daily jobs from `~/solar-health-check`, all using
the local PostgreSQL database. Times are in `Africa/Johannesburg`:

| Time | Job | Result |
|---|---|---|
| 22:00 | `solar-db-backup.timer` | Creates a PostgreSQL custom-format dump in `~/solar-backups/` |
| 05:00 | `solar-health-check.timer` | Emails the database integrity and coverage report |
| 05:05 | `solar-analysis.timer` | Rebuilds analysis tables and emails the complete output |
| 05:10 | `solar-report.timer` | Emails report output with the two generated PNG charts attached |

The health check, analysis, and report emails go through the configured Gmail
`msmtp` account to `mike@hewitson.co.za` and `hewitson.is.me@gmail.com`. The
report runner also sends an ntfy alert if report generation fails, any chart is
missing, or email delivery fails. The ntfy topic is read from the mini's
private `~/.config/solar-db-backup/ntfy.env`; the topic value is not stored in
this README. Report output and CSV/PNG files are kept under
`~/solar-health-check/solar report/`.

Useful commands to inspect the daily jobs:

```bash
systemctl list-timers --all --no-pager | grep -E 'solar-(db-backup|health-check|analysis|report)'
sudo systemctl status solar-health-check.service solar-analysis.service solar-report.service --no-pager -l
sudo journalctl -u solar-health-check.service -u solar-analysis.service -u solar-report.service -n 60 --no-pager
```

The systemd timers are persistent, so a scheduled run missed while the mini is
off is started after it returns. The Tuya extractor remains on its separate
every-two-hours schedule.

All five scheduled jobs (extractor, backup, health check, analysis and report)
send a successful-run ping to their individual Healthchecks.io checks. The
Tuya extractor sends a `/start` ping before running, its existing success ping
after a successful run, and `/fail` if systemd reports an unsuccessful run.
This allows Healthchecks.io to measure run time and receive immediate failure
signals. Other jobs send only the successful-run ping. The check-in URLs are
stored privately on the mini in
`~/.config/solar-heartbeats/solar.env` (mode `600`) and must not be shared or
committed to this project. Healthchecks.io alerts are routed through the
configured ntfy integration. The five checks were manually verified as up.

To enable Tuya extractor run-time measurement, install its Healthchecks
drop-in and reload systemd:

```bash
sudo install -D -m 644 \
  ~/solar-tuya-extractor/systemd/solar-tuya-extractor.service.d/healthchecks.conf \
  /etc/systemd/system/solar-tuya-extractor.service.d/healthchecks.conf
sudo systemctl daemon-reload
```

Useful commands to check or start the analysis email service:

```bash
systemctl list-timers --all --no-pager | grep solar-analysis
sudo systemctl status solar-analysis.service --no-pager -l
sudo journalctl -u solar-analysis.service -n 40 --no-pager
sudo systemctl start solar-analysis.service
```

### Optionally write a CSV copy

```bash
python3 tuya_power_extractor.py --output-csv yesterday.csv
```

A relative CSV path is written in the Solar project directory. CSV output is
optional; the database selected in `config.ini` remains the primary data store.

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
database storage time. This makes it easier to spot a change in API behaviour.

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

The report generator currently produces four files.

## Daily chart

```text
solar daily load profile.png
```

Shows daily electricity consumption split into:

- geyser
- non-geyser

Complete days are identified separately.

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

# 13. Program 4 — Single-day power chart

## `plot_non_geyser_power.py`

This script creates a graph for one calendar day using readings already stored
in the configured database (SQLite or PostgreSQL). The `--chart both` option plots three lines together: geyser,
non-geyser and total household power. It does **not** contact Tuya, extract data,
or modify the database.

If `--date` is omitted, it charts yesterday using South African local time.

Run from the Solar directory:

```bash
cd ~/Documents/Solar
```

To run this chart from the mini against its local PostgreSQL database, copy the
script into the same directory as the analysis and report scripts:

```bash
# Run on the MacBook
cd ~/Documents/Solar
scp plot_non_geyser_power.py piholeadmin@192.168.1.37:~/solar-health-check/

# Then run on the mini
cd ~/solar-health-check
./.venv/bin/python plot_non_geyser_power.py
```

The default database and output paths are relative to the script directory.
On the mini, the PNG is saved under `~/solar-health-check/solar report/`.

### Chart non-geyser power (default)

```bash
./.venv/bin/python plot_non_geyser_power.py
```

### Chart geyser power

```bash
./.venv/bin/python plot_non_geyser_power.py --chart geyser
```

### Chart total household power

```bash
./.venv/bin/python plot_non_geyser_power.py --chart total
```

### Chart geyser, non-geyser and total power

```bash
./.venv/bin/python plot_non_geyser_power.py --chart both
```

### Chart a specific date

Supply the date in `YYYY-MM-DD` format:

```bash
./.venv/bin/python plot_non_geyser_power.py --date 2026-10-11
```

Combine the date and chart selection as needed:

```bash
./.venv/bin/python plot_non_geyser_power.py --date 2026-10-11 --chart both
./.venv/bin/python plot_non_geyser_power.py --date 2026-10-11 --chart geyser
```

### Display the graph after saving

Add `--show` to open the graph window:

```bash
./.venv/bin/python plot_non_geyser_power.py --chart both --show
```

### Output

For `--chart both`, the graph includes geyser power, non-geyser power and total
household power, with a legend and daily energy totals in the footer.

The PNG graph is saved in a `solar report/` folder beside the script. On the
MacBook this is:

```text
~/Documents/Solar/solar report/
```

On the mini it is `~/solar-health-check/solar report/`.

The filename includes the chart type and date, for example:

```text
non_geyser_power_2026-10-11.png
geyser_power_2026-10-11.png
total_power_2026-10-11.png
geyser_non_geyser_and_total_power_2026-10-11.png
```

The script also prints the number of readings, the energy totals relevant to
the selected chart, and the peak total and non-geyser power with their times.
Energy integration ignores gaps longer than five minutes.

Non-geyser power is calculated as total household power (`Power_A`) minus
cleaned geyser power (`Power_B`). Negative geyser readings are treated as zero,
and geyser readings below the 20 W noise threshold are treated as zero.
Negative resulting non-geyser power is also clamped to zero.

The script requires Matplotlib. If needed, install it with:

```bash
python3 -m pip install matplotlib
```

---

# 15. Normal operating procedure

The mini collects Tuya readings automatically every two hours. The rest of the
workflow can be run from the MacBook, whose database configuration connects to
PostgreSQL on the mini:

### Step 1 — Check the mini extractor

Confirm the timer and most recent run:

```bash
ssh piholeadmin@192.168.1.37
systemctl list-timers --all --no-pager | grep solar-tuya
sudo systemctl status solar-tuya-extractor.service --no-pager -l
```

To start an extra catch-up run manually on the mini, use:

```bash
sudo systemctl start solar-tuya-extractor.service
```

For a historical backfill, run the extractor from the MacBook with a date:

```bash
cd ~/Documents/Solar
python3 tuya_power_extractor.py --date 2026-10-09
```

### Step 2 — Review the daily analysis email

The mini rebuilds the analysis tables and emails the complete analysis output
daily at 05:05 SAST. To rebuild the tables manually from the MacBook when
needed:

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

# 15. Database

The live database is PostgreSQL:

```text
Database: solar_meter
Host: Ubuntu mini (local jobs connect to 127.0.0.1)
Role: solar_app
```

The MacBook's `solar_meter.db` is the SQLite copy retained after migration. The
mini is the active collection and reporting host.

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

# 16. Backups

The mini creates a daily PostgreSQL custom-format dump at 22:00 SAST in:

```text
~/solar-backups/solar_meter_<UTC timestamp>.dump
```

The job is `solar-db-backup.service`, scheduled by `solar-db-backup.timer`.
Failure sends an ntfy message using the private topic configuration at
`~/.config/solar-db-backup/ntfy.env`. AWS/S3 off-host backup is not yet
configured. Keep the MacBook SQLite fallback and normal Mac backups until
off-host PostgreSQL backups are established and verified.

---

# 17. Current data interpretation

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

# 18. Current limitations

At present, the hourly profile is based only on complete calendar days.

This is intentional.

Partial days can seriously distort hourly averages.

For example, if data starts at 15:00, the missing morning hours must not be
interpreted as zero consumption.

As more days accumulate, the hourly profile will automatically become more
representative.

---

# 19. Solar-planning use

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

# 20. Current solar-system context

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

# 21. Python environment

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

# 22. Troubleshooting

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

# 23. Useful checks

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

# 24. Recommended long-term workflow

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

If an analysis rule changes — for example, the geyser noise threshold changesfrom 20 W to 25 W — simply change `config.ini` and rerun:

```bash
python3 solar_analysis.py
```

The derived analysis will be rebuilt using the new rule while the raw readings
remain unchanged.

---

# 25. Future extensions

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

## Annual PVGIS simulation: daily load inputs

The PVGIS simulation uses two different household-load inputs:

1. **Daily energy targets:** the `daily_load_projection` table supplies projected
   total, geyser and non-geyser energy for every calendar day of the simulated year.
   The projection is mapped to the PVGIS year by month and day.
2. **Hourly load shape:** the latest complete measured day in `meter_readings` is
   used to construct the hourly household-load profile. The non-geyser hourly profile
   is scaled separately for each simulated day to match that day's projected
   non-geyser energy. The geyser target comes from that day's projected geyser energy.

Consequently, the startup figures labelled “Measured load day”, “Measured daily
load”, “Measured geyser” and “Measured non-geyser” describe the latest complete
measured day. They are diagnostic baseline figures; they are **not** copied as the
same daily energy total for every day of the annual simulation.

The script prints `Projection days available` to show how many daily projection
records were loaded. It expects a full calendar year's worth of dates.

The hourly profile in this script is based on the latest complete measured day,
not an average of all complete days. As more measurements accumulate, this remains
a limitation unless the load-profile logic is deliberately changed.

---

## Grid-outage resilience simulation

Script:

```text
solar_outage_simulation_updated.py
```

The outage simulation reuses the PVGIS model in `solar_pvgis_simulation.py` and
uses `daily_load_projection` for daily demand. It evaluates every possible outage
start date in the selected PVGIS year for one-, two- and three-day outages.

Run from the Solar project directory:

```bash
cd ~/Documents/Solar
python3 solar_outage_simulation_updated.py
```

By default, **each outage starts with a fully charged battery**. This is the primary
resilience case: the grid fails unexpectedly while the battery is full. The model
uses a 15 kWh usable battery, 20% reserve and 8 kW inverter by default.

A secondary mode starts each outage with the state of charge reached by the
preceding normal grid-connected day:

```bash
python3 solar_outage_simulation_updated.py --start-soc-mode normal
```

This represents an outage occurring after poor-solar days have already depleted
the battery. It is a separate stress test and should not be confused with the
default full-battery case.

During an outage, the dispatch priority is:

```text
PV → non-geyser household load → battery
                                  ↓
                      surplus PV after battery is full
                                  ↓
                                geyser
```

The geyser is discretionary during an outage and is heated only from PV surplus
after the battery is full. The model does not deliberately discharge the battery
to heat the geyser. Grid import is zero during the simulated outage; any household
load that cannot be supplied by PV and battery down to the reserve is recorded as
unserved.

The script writes all tested outage windows to:

```text
solar report/outage simulation/
```

The output CSV filename identifies the PVGIS year and starting-SOC mode, for example:

```text
pvgis 2023 outage windows - full battery.csv
pvgis 2023 outage windows - normal SOC.csv
```

Results include outage dates and duration, starting/minimum/ending battery SOC,
PV generation, non-geyser household load, battery charge and discharge, unserved
household energy, geyser energy required and heated, curtailed PV, and whether the
battery reserve was reached.

Important limitations:

- “House load” in the outage model excludes the geyser, because geyser heating is
  treated as discretionary during an outage.
- The model does not explicitly separate oven use from the measured non-geyser load.
- The geyser is modelled as an energy load, not as a physical hot-water tank with
  thermostat and temperature dynamics.
- This is a resilience study using historical PVGIS weather and projected demand,
  not an engineering certification or a guarantee of performance during a real outage.

---

## Quick reference

```bash
# Go to project
cd ~/Documents/Solar

# View the mini's extractor schedule
ssh piholeadmin@192.168.1.37
systemctl list-timers --all --no-pager | grep solar-tuya

# Start an extra catch-up run on the mini
sudo systemctl start solar-tuya-extractor.service
exit

# Backfill a specific day from the MacBook
cd ~/Documents/Solar
python3 tuya_power_extractor.py --date YYYY-MM-DD

# Rebuild analysis
python3 solar_analysis.py

# Generate charts and CSV reports
python3 solar_report.py
```

That's the complete normal workflow.
