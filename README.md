# Solar / Smart Meter Analysis Project

## Overview

This project extracts electricity data from the Tuya smart meter, stores the
measurements in SQLite, analyses the household load profile, and models the
proposed solar PV + battery system.

Current system modelling reference:

- Location: Sandbaai / Hermanus, Western Cape
- PV: approximately 7 kWp
- PV orientation: 3.5 kWp east + 3.5 kWp west
- Roof pitch: 22°
- Inverter reference: 8 kW
- Battery reference: 15 kWh usable
- Battery reserve: 20%
- Grid export: disabled / zero export
- Geyser: approximately 150–200 L electric
- Geyser strategy: preferentially use surplus daytime solar
- Oven: inverter-backed and available during grid outages
- EV: future-ready, but no current EV

The current objective is high solar self-consumption, useful battery storage,
and whole-house backup rather than multi-day off-grid autonomy.

---

# 1. Tuya Smart Meter

Meter:

- Brand/platform: Tuya / SmartLife
- Model: `DY_SMART_METER_01_T1U`
- Tuya category: `zndb`
- Tuya region: Central Europe
- Local timezone: Africa/Johannesburg / UTC+02:00

Important channels:

- `Power_A` = total household/grid power
- `Power_B` = geyser circuit power

There is currently no solar installation, so all measured power is imported
from the grid.

Power_B is known to be the geyser circuit. Small Power_B readings are treated
as CT/electrical noise and are ignored in the cleaned analysis below the
configured threshold.

---

# 2. Tuya extraction

The extraction script retrieves Tuya report-log data using the Tuya Cloud API.

Typical command:

```bash
cd ~/Documents/Solar
python3 tuya_power_extractor.py --date 2026-09-29
```

The script uses short API windows, pagination and rate-limit handling.

Configuration is held separately from the script:

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

# 3. SQLite database

Database:

```text
~/Documents/Solar/solar_meter.db
```

Main table:

```sql
meter_readings
```

Fields include:

- `timestamp_sast`
- `reading_date`
- `power_a_w`
- `power_b_w`
- `source_file`
- `imported_at`

There is a unique constraint on the timestamp, so repeated extraction runs
should not create duplicate measurements.

An `extraction_runs` table records extraction history.

A `database_info` table records configuration and interpretation metadata.

---

# 4. Current measured data

As of the latest database check:

- Earliest measurement: 2026-09-28
- Latest measurement: 2026-09-30
- More than 16,000 readings
- No duplicate timestamps
- Sampling intervals are generally tens of seconds

The most recent complete day currently available is:

```text
2026-09-29
```

Current measured daily energy:

```text
Household:       ~13.20 kWh
Geyser:           ~8.13 kWh
Non-geyser:       ~5.07 kWh
Measured peak:    ~6.20 kW
```

The measured geyser consumption is currently a very large proportion of daily
energy. This should NOT yet be treated as a representative annual average,
because the database contains only a short period of observations.

As more days accumulate, the solar model should use the actual measured load
rather than repeating 29 September.

---

# 5. Load cleaning and analysis

Script:

```text
solar_analysis.py
```

The analysis creates cleaned/derived data including:

- total household power
- cleaned geyser power
- non-geyser power
- integrated total energy
- integrated geyser energy
- daily summaries
- hourly load profiles

Geyser cleaning:

- negative Power_B values are discarded
- Power_B below the configured 20 W threshold is treated as zero
- raw Power_B remains available in the database

Energy integration uses trapezoidal integration over valid intervals.
Intervals longer than 300 seconds are excluded from energy integration.

---

# 6. Current report

Script:

```text
solar_report.py
```

Report directory:

```text
~/Documents/Solar/solar report/
```

Current outputs include:

```text
solar daily load profile.png
solar hourly load profile.png
solar hourly energy profile.png
solar daily load summary.csv
solar hourly load profile.csv
```

These reports are based on the measured Tuya data.

---

# 8. Annual load projection

Script:

```text
solar_usage_projection.py
```

This script creates a full-year daily household-load projection from the
available monthly electricity totals and the average daily load measured from
all complete days in the Tuya database.

The process is:

1. Read the measured monthly electricity totals from `monthly-usage.csv`.
2. Use a linear regression to estimate missing months.
3. Calculate the average daily household, geyser and non-geyser consumption
   from all complete days in `daily_load_summary`.
4. For every day of the year, scale those daily averages up or down according
   to the applicable month's total.
5. Apply the same monthly scaling factor to the total, geyser and non-geyser
   components.
6. Verify that the projected daily values for each month add up to that
   month's target total.
7. Store the resulting daily projection in SQLite.

The projection is therefore seasonal: October, for example, uses the October
monthly total as its target, while the measured daily average supplies the
household load shape and geyser/non-geyser split.

The database table created by the script is:

```sql
daily_load_projection
```

Fields:

```text
projection_date
month
day
scaling_factor
total_energy_kwh
geyser_energy_kwh
non_geyser_energy_kwh
```

The projection table is separate from the measured-data tables. Running the
projection script does not modify the original Tuya measurements or the
`daily_load_summary` table.

The script prints every seventh projected day for checking and also prints a
monthly target-versus-projected verification.

---

# 8. Solar / battery simulation

Main simulator:

```text
solar_battery_simulation.py
```

The simulator can model:

- PV generation
- household consumption
- geyser solar shifting
- battery charging
- battery discharge
- battery state of charge
- grid import
- curtailed PV
- continuous SOC across multiple days

Current reference case:

```text
PV:                 7.0 kWp
East:               3.5 kWp
West:               3.5 kWp
Inverter:           8.0 kW
Battery:            15.0 kWh usable
Battery reserve:    20%
Battery efficiency: approximately 95%
```

The geyser solar-shift model currently uses:

```text
Heating window:     09:00–16:00
Maximum power:      3.0 kW
```

Important limitation:

The geyser is currently modelled as an energy-shiftable load. It is NOT yet
a physical hot-water tank / thermostat model.

---

# 9. Multi-day stress testing

The simulator supports continuous battery SOC across repeated days.

Example:

```bash
python3 solar_battery_simulation.py   --repeat-days 7   --pv-yields 4.5,1.5,1.0,1.0,1.0,1.0,4.5
```

This was used to demonstrate how a 15 kWh battery behaves during several
consecutive poor-solar days.

The stress test showed that the battery can eventually reach its 20% reserve
during a prolonged poor-PV sequence, after which grid energy is required.

This is a scenario test, not a weather forecast.

---

# 10. PVGIS Hermanus simulation

Script:

```text
solar_pvgis_simulation.py
```

This replaced the earlier synthetic PV curve with historical PVGIS solar data.

PVGIS configuration:

```text
Location:       approximately -34.42, 19.24
PV:             3.5 kWp east + 3.5 kWp west
Tilt:           22°
PVGIS losses:   14%
Year tested:    2023
```

PVGIS data is cached locally under:

```text
solar report/pvgis/pvgis cache/
```

The model uses historical hourly solar/weather data rather than an artificial
clear-sky curve.

The simulation assumes zero export: surplus PV is curtailed once household
load is satisfied and the battery is full.

---

# 11. 2023 PVGIS result — current reference

Using the current measured 29 September load profile repeated throughout
2023:

```text
PV generation:          ~9,368 kWh
Household load:         ~4,817 kWh
Direct PV to load:      ~3,751 kWh
Battery discharge:        ~925 kWh
Grid import:              ~141 kWh
PV curtailed:           ~4,593 kWh
```

The important observation is that annual PV energy is substantially greater
than annual household demand.

The principal problem is therefore seasonal timing rather than annual solar
energy availability.

---

# 12. Winter result

The 2023 simulation identified June as the most difficult period.

Worst 14-day period:

```text
14 June – 27 June 2023
PV generation:     ~130 kWh
Grid import:        ~57 kWh
Battery reached:     3 kWh / 20% reserve
```

The most difficult individual simulated day produced only about:

```text
2.3 kWh PV
```

against a modelled household demand of approximately:

```text
13.2 kWh
```

This demonstrates why a prolonged sequence of poor solar days matters more
than a single poor day.

---

# 13. Battery sensitivity test

Script:

```text
solar battery sensitivity.py
solar_outage_simulation_updated.py
```

This compares different battery sizes while keeping the PVGIS weather,
household load and all other assumptions identical.

Current 2023 results:

| Battery | Annual grid | June grid | Worst 14-day grid |
|---:|---:|---:|---:|
| 10 kWh | 180.7 kWh | 87.7 kWh | 57.1 kWh |
| 15 kWh | 140.7 kWh | 87.4 kWh | 57.1 kWh |
| 20 kWh | 118.6 kWh | 87.4 kWh | 57.1 kWh |
| 25 kWh | 105.3 kWh | 87.4 kWh | 57.1 kWh |

The result indicates diminishing returns from increasing battery capacity.

The 15 kWh battery is therefore currently being retained as the reference
case rather than automatically increasing battery size.

This is NOT yet a final sizing decision.

---

---

# 14. Grid-outage resilience simulation

Script:

```text
solar_outage_simulation.py
```

The outage simulation uses the historical 2023 PVGIS hourly production profile
together with the current seasonal daily load projection. It tests every
possible outage starting date in the PVGIS year for:

- 1-day outages
- 2-day outages
- 3-day outages

The primary outage-resilience test starts **every outage with a fully charged
15 kWh usable battery**. This is intended to answer the practical question:

> What happens if the grid fails unexpectedly while the battery is fully
> charged?

Current outage configuration:

```text
PV:                     7.0 kWp
PV orientation:         3.5 kWp east + 3.5 kWp west
Inverter reference:     8.0 kW
Battery:                15.0 kWh usable
Battery reserve:        20% / 3.0 kWh
Starting SOC:            15.0 kWh
PV data:                 PVGIS historical 2023
Load model:              daily_load_projection
```

During an outage the dispatch priority is:

```text
PV → house load → battery
                     ↓
              surplus PV only
                     ↓
                  geyser
```

The geyser is deliberately treated as a discretionary load during an outage.
It may only use surplus PV **after the battery is completely full**. The
battery is therefore never deliberately discharged to heat the geyser.

The simulation records:

- starting, minimum and ending battery SOC
- PV generation
- non-geyser house load
- unserved house load
- geyser energy required
- geyser energy actually heated
- battery discharge
- curtailed PV
- whether the battery reserve was reached

## Current full-battery outage results

Using the current load projection and 2023 PVGIS weather:

| Outage | Worst period | Minimum SOC | Unserved house load |
|---|---|---:|---:|
| 1 day | 27 Jun 2023 | 7.74 kWh | 0.00 kWh |
| 2 days | 13–14 Jun 2023 | 5.08 kWh | 0.00 kWh |
| 3 days | 25–27 Jun 2023 | 3.00 kWh | 1.26 kWh |

The worst 1-day case generated only 2.29 kWh of PV against a 9.15 kWh
non-geyser house load, yet the full battery still supplied the complete house
load and ended at 7.74 kWh.

The worst 2-day case generated 9.03 kWh against 18.30 kWh of non-geyser house
load. The battery ended at 5.08 kWh and there was no unserved house load.

The worst 3-day case was 25–27 June:

```text
Starting SOC:          15.00 kWh
PV generation:         15.18 kWh
House load:             27.45 kWh
Battery discharge:      14.97 kWh
Minimum / ending SOC:    3.00 kWh
Unserved house load:      1.26 kWh
Geyser heated:            0.00 kWh
```

This indicates that, in the current model, a full 15 kWh battery provides
excellent resilience for one- and two-day grid outages and very nearly
covers even the worst three-day winter sequence.

A separate stress-test mode is available:

```bash
python3 solar_outage_simulation.py --start-soc-mode normal
```

This starts each outage using the SOC reached by the preceding normal
grid-connected simulation. It represents the different scenario where the
grid fails after several poor-solar days have already depleted the battery.
It should not be confused with the primary full-battery outage-resilience
test.

The complete outage-window results are written to:

```text
solar report/outage simulation/
```

Important interpretation:

The outage simulation's "house load" excludes the geyser, because the geyser
is intentionally treated as discretionary during an outage. Oven behaviour is
also not yet explicitly represented in the measured load model.

The outage simulation is a resilience study, not an engineering certification.
It uses historical PVGIS weather and the current projected load model.


# 14. Current modelling conclusions

The simulations currently indicate:

1. A 7 kWp east/west PV array produces substantially more annual energy than
   the current household consumption.

2. The important constraint is winter solar availability, particularly
   sequences of poor days.

3. Increasing battery capacity above 15 kWh reduces annual grid import, but
   has relatively little effect on the worst June sequence in the current
   model.

4. There is substantial annual PV curtailment because there is already much
   more summer PV energy than the house and battery can absorb.

5. The geyser is currently a major component of measured household demand.

6. Intelligent geyser control is therefore potentially important, especially
   during prolonged poor-solar periods.

7. The current load model is the biggest remaining limitation.

---

# 15. Next modelling priority

The next major improvement should NOT be another battery-size test.

Instead:

```text
Actual measured Tuya load
        +
Historical PVGIS solar profile
        +
7 kWp east/west PV
        +
15 kWh battery
```

The current simulation can use the new `daily_load_projection` table as a
seasonal load model. The projection is based on the average measured daily
load, scaled to the applicable monthly electricity total.

As the Tuya database accumulates more data, replace this projection baseline
with actual measured daily/hourly load profiles.

The improved model should eventually distinguish:

- actual daily load
- weekday/weekend behaviour
- actual geyser usage
- morning demand
- evening demand
- cooking days
- washing/tumble-dryer days
- daily peak power
- seasonal household behaviour

That will make the final battery and inverter sizing substantially more
defensible.

---

# 16. Important modelling caveats

The current results should not be treated as an engineering certification
or a supplier quotation.

Specific limitations:

- PVGIS is historical modelled solar data, not an on-site solar measurement.
- The current household profile is based on a very short measurement period.
- The current annual projection is based on a short measured period and
  scales the measured daily average to the projected monthly totals.
- The geyser is not yet modelled as a thermal tank.
- Oven behaviour is not yet represented realistically in the load profile.
- The current model uses simplified battery efficiency and dispatch logic.
- Actual inverter charge/discharge limits have not yet been incorporated.
- Actual inverter overload/surge characteristics have not yet been modelled.
- Zero export is assumed.
- PV clipping and detailed MPPT behaviour are simplified.
- Actual future EV consumption is not included.

---

# 17. Practical working sequence

The recommended sequence from here is:

```text
1. Continue collecting Tuya measurements
        ↓
2. Update the SQLite database daily
        ↓
3. Build a larger measured load dataset
        ↓
4. Generate measured daily/hourly load profiles
        ↓
5. Combine measured load with PVGIS hourly weather
        ↓
6. Model the 7 kWp east/west PV system
        ↓
7. Compare 10 / 15 / 20 kWh batteries
        ↓
8. Examine winter sequences
        ↓
9. Test 1 / 2 / 3-day grid-outage resilience
        ↓
10. Examine inverter peak-load requirements
        ↓
11. Model geyser control more realistically
        ↓
12. Model oven behaviour during outages
        ↓
13. Compare supplier proposals against the resulting requirements
```

---

# 18. Files currently associated with the project

Core scripts:

```text
tuya_power_extractor.py
solar_analysis.py
solar_report.py
solar_usage_projection.py
solar_battery_simulation.py
solar_pvgis_simulation.py
solar battery sensitivity.py
```

Database:

```text
solar_meter.db
```

Reports:

```text
solar report/
```

PVGIS cache:

```text
solar report/pvgis/pvgis cache/
```

---
