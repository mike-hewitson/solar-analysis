#!/usr/bin/env python3
"""
Battery-size sensitivity test using the existing PVGIS Hermanus simulation.

Runs the same 2023 PVGIS east/west solar profile and measured household
load against several usable battery sizes. Only battery capacity changes.

Default battery sizes:
  10, 15, 20, 25 kWh usable

Requires the existing solar_pvgis_simulation.py in the same directory and
its cached PVGIS files / solar_meter.db.
"""

import argparse
import csv
import subprocess
import sys
from pathlib import Path


def args():
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=2023)
    p.add_argument("--batteries", default="10,15,20,25",
                   help="Comma-separated usable battery sizes in kWh")
    p.add_argument("--script", default="solar_pvgis_simulation.py")
    p.add_argument("--output-dir", default="solar report/battery sensitivity")
    return p.parse_args()


def run_case(base_script, year, battery, root):
    # Import the existing simulation as a module so the sensitivity test
    # uses exactly the same PVGIS and load model.
    import importlib.util

    spec = importlib.util.spec_from_file_location("solar_pvgis_simulation", base_script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    a = argparse.Namespace(
        year=year,
        db=str(root / "solar_meter.db"),
        pv_cache=str(root / "solar report/pvgis/pvgis cache"),
        output_dir=str(root / "solar report/battery sensitivity"),
        battery_kwh=battery,
        reserve=0.20,
        inverter_kw=8.0,
        geyser_start=9,
        geyser_end=16,
        geyser_max_kw=3.0,
        annual_target_kwh=mod.ANNUAL_PLANNING_KWH,
        missing_month_kwh=None,
        no_download=False,
    )

    east_data, _, _ = mod.fetch_pvgis(mod.EAST_KWP, -90, year,
                                       Path(a.pv_cache), False)
    west_data, _, _ = mod.fetch_pvgis(mod.WEST_KWP, 90, year,
                                       Path(a.pv_cache), False)

    east = mod.pvgis_hourly(east_data)
    west = mod.pvgis_hourly(west_data)

    pv = {}
    for ts, p in east.items():
        pv[(ts.date(), ts.hour)] = p + west.get(ts, 0.0)

    load = mod.load_complete_measured_day(Path(a.db))
    monthly_targets, _ = mod.build_monthly_targets(a)
    daily, annual, monthly, min_soc, max_soc = mod.simulate_year(
        pv, load, a, year, monthly_targets
    )

    june = monthly[6]
    worst_window = None
    if len(daily) >= 14:
        windows = []
        for i in range(len(daily) - 13):
            chunk = daily[i:i+14]
            windows.append({
                "grid": sum(x["grid_kwh"] for x in chunk),
                "pv": sum(x["pv_kwh"] for x in chunk),
                "start": chunk[0]["date"],
                "end": chunk[-1]["date"],
                "min_soc": min(x["end_soc_kwh"] for x in chunk),
            })
        worst_window = max(windows, key=lambda x: x["grid"])

    return {
        "battery_kwh": battery,
        "annual_pv_kwh": annual["pv"],
        "annual_load_kwh": annual["load"],
        "annual_grid_kwh": annual["grid"],
        "annual_curtailed_kwh": annual["curtailed"],
        "annual_battery_charge_kwh": annual["charge"],
        "annual_battery_discharge_kwh": annual["discharge"],
        "annual_pv_direct_kwh": annual["pv_direct"],
        "annual_pv_to_geyser_kwh": annual["pv_to_geyser"],
        "june_pv_kwh": june["pv"],
        "june_grid_kwh": june["grid"],
        "june_curtailed_kwh": june["curtailed"],
        "min_soc_kwh": min_soc,
        "max_soc_kwh": max_soc,
        "worst_14d_grid_kwh": worst_window["grid"] if worst_window else None,
        "worst_14d_start": worst_window["start"] if worst_window else "",
        "worst_14d_end": worst_window["end"] if worst_window else "",
        "worst_14d_min_soc_kwh": worst_window["min_soc"] if worst_window else None,
    }


def main():
    a = args()
    root = Path(a.script).expanduser().resolve().parent
    batteries = [float(x.strip()) for x in a.batteries.split(",") if x.strip()]
    out = Path(a.output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    print("=" * 66)
    print("Battery-size sensitivity — Hermanus PVGIS 2023")
    print("=" * 66)
    print(f"PV:             7.00 kWp east/west")
    print(f"Inverter:       8.00 kW")
    print(f"Reserve:        20%")
    print(f"Battery cases:  {', '.join(f'{x:g}' for x in batteries)} kWh")
    print()

    results = []
    for b in batteries:
        print(f"Running {b:g} kWh battery...")
        r = run_case(a.script, a.year, b, root)
        results.append(r)

    fields = list(results[0].keys())
    csv_path = out / "battery sensitivity 2023.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    print()
    print("Summary")
    print("-" * 66)
    print("Battery  Annual grid  June grid  Worst 14d  Min SOC  Curtailed")
    print("kWh      kWh           kWh        grid kWh    kWh      kWh")
    print("-------  ------------  ---------  ----------  -------  ---------")
    for r in results:
        print(f"{r['battery_kwh']:>7.0f}  "
              f"{r['annual_grid_kwh']:>12.1f}  "
              f"{r['june_grid_kwh']:>9.1f}  "
              f"{r['worst_14d_grid_kwh']:>10.1f}  "
              f"{r['min_soc_kwh']:>7.2f}  "
              f"{r['annual_curtailed_kwh']:>9.1f}")

    print()
    base = results[0]
    print("Incremental effect versus the smallest battery")
    print("-" * 66)
    print("Battery  Grid saved/year  Grid saved/extra kWh  June grid")
    print("-------  ---------------  --------------------  ---------")
    for r in results:
        extra = r["battery_kwh"] - base["battery_kwh"]
        saved = base["annual_grid_kwh"] - r["annual_grid_kwh"]
        ratio = saved / extra if extra > 0 else 0.0
        print(f"{r['battery_kwh']:>7.0f}  {saved:>15.1f}  {ratio:>20.2f}  {r['june_grid_kwh']:>9.1f}")

    print()
    print(f"CSV: {csv_path}")


if __name__ == "__main__":
    main()
