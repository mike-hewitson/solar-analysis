#!/usr/bin/env python3
"""
Solar Step 6 — PVGIS grid-outage resilience simulation

Uses:
  - Historical PVGIS hourly production for 7.0 kWp east/west PV
  - The daily_load_projection table stored in solar_meter.db
  - The measured complete-day hourly load shape retained from the current
    solar_pvgis_simulation.py model
  - A 15 kWh usable battery by default
  - 20% battery reserve
  - 95% battery charge/discharge efficiency

For every possible outage start day in the selected PVGIS year, this script
simulates 1-, 2- and 3-day grid outages.

During an outage:
  1. PV supplies the non-geyser household load directly.
  2. Remaining PV charges the battery.
  3. Only after the battery reaches 100% is excess PV allowed to heat the
     geyser.
  4. The geyser never deliberately draws battery energy.
  5. If PV + battery cannot supply the household load down to the reserve,
     that household load is counted as unserved. Grid import is always zero.

The normal grid-connected annual simulation is also run first so that
the projected annual load and normal operating results are available.

By default, every outage starts with a fully charged battery. This is the
primary outage-resilience test: it measures how the proposed system performs
if the grid fails unexpectedly while the battery is in a healthy, fully
charged state.

A secondary stress-test mode (--start-soc-mode normal) is also available. In
that mode each outage inherits the SOC reached by the preceding normal
grid-connected day, showing what happens if the grid fails after several
poor-solar days have already depleted the battery.

The geyser is still an energy model, not a physical thermal tank model.
"""

import argparse
import csv
import importlib.util
from datetime import datetime
from pathlib import Path


DEFAULT_SCRIPT = "solar_pvgis_simulation.py"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, default=2023)
    p.add_argument("--db", default="solar_meter.db")
    p.add_argument("--script", default=DEFAULT_SCRIPT,
                   help="PVGIS simulation script containing the shared model")
    p.add_argument("--pv-cache", default=None)
    p.add_argument("--output-dir", default="solar report/outage simulation")
    p.add_argument("--battery-kwh", type=float, default=15.0)
    p.add_argument("--reserve", type=float, default=0.20)
    p.add_argument("--inverter-kw", type=float, default=8.0)
    p.add_argument("--geyser-max-kw", type=float, default=3.0)
    p.add_argument(
        "--start-soc-mode",
        choices=("full", "normal"),
        default="full",
        help=(
            "Outage starting SOC: full starts every outage at 100%% battery; "
            "normal uses the SOC reached by the preceding normal day. "
            "Default: full"
        ),
    )
    p.add_argument("--no-download", action="store_true")
    return p.parse_args()


def load_model(path):
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"PVGIS simulation script not found: {path}")

    spec = importlib.util.spec_from_file_location(
        "solar_pvgis_shared_model", path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_pv(mod, year, cache, no_download):
    east_data, _, _ = mod.fetch_pvgis(
        mod.EAST_KWP, -90, year, cache, no_download
    )
    west_data, _, _ = mod.fetch_pvgis(
        mod.WEST_KWP, 90, year, cache, no_download
    )

    east = mod.pvgis_hourly(east_data)
    west = mod.pvgis_hourly(west_data)

    pv = {}
    for ts, value in east.items():
        pv[(ts.date(), ts.hour)] = value + west.get(ts, 0.0)

    return pv


def build_scaled_load(mod, load_profile, projection, day):
    """
    Retain the measured hourly shape while scaling the two load components
    independently to the projected energy for this calendar day.
    """
    projected = projection[(day.month, day.day)]

    non_geyser_scale = (
        projected["non_geyser_kwh"] /
        load_profile["non_geyser_kwh"]
    )

    non_geyser = {
        h: load_profile["hourly_non_geyser"].get(h, 0.0) * non_geyser_scale
        for h in range(24)
    }

    return non_geyser, projected["geyser_kwh"]


def simulate_normal_year(mod, pv, load_profile, projection, args, year):
    """
    Run the existing normal grid-connected model so that each outage start
    can inherit the SOC that the battery would realistically have reached.
    """
    ns = type("Args", (), {})()
    ns.battery_kwh = args.battery_kwh
    ns.reserve = args.reserve
    ns.inverter_kw = args.inverter_kw
    ns.geyser_start = 9
    ns.geyser_end = 16
    ns.geyser_max_kw = args.geyser_max_kw

    daily, annual, monthly, min_soc, max_soc = mod.simulate_year(
        pv, load_profile, projection, ns, year
    )
    return daily, annual


def simulate_outage(mod, pv, load_profile, projection, args,
                    start_index, duration_days, normal_daily):
    """
    Simulate one outage beginning at start_index.

    Returns detailed daily and aggregate outage results.
    """
    reserve_kwh = args.battery_kwh * args.reserve

    if args.start_soc_mode == "full":
        # Primary resilience test: an unexpected grid outage starts with the
        # battery fully charged. This answers how long the proposed system
        # can sustain the house when the outage occurs with a normal,
        # healthy battery state.
        start_soc = args.battery_kwh
    elif start_index == 0:
        start_soc = args.battery_kwh
    else:
        # Secondary stress test: the outage starts with the SOC produced by
        # the preceding normal grid-connected simulation.
        start_soc = normal_daily[start_index - 1]["end_soc_kwh"]

    soc = start_soc
    min_soc = soc

    total_pv = 0.0
    total_house_load = 0.0
    total_geyser_required = 0.0
    total_geyser_heated = 0.0
    total_battery_charge_input = 0.0
    total_battery_discharge = 0.0
    total_unserved_house = 0.0
    total_curtailed = 0.0

    reserve_reached = False
    reserve_hour = None
    daily_results = []

    for offset in range(duration_days):
        idx = start_index + offset
        if idx >= len(normal_daily):
            break

        day = datetime.fromisoformat(normal_daily[idx]["date"]).date()
        non_geyser, geyser_required = build_scaled_load(
            mod, load_profile, projection, day
        )

        pv_day = {h: pv.get((day, h), 0.0) for h in range(24)}

        day_pv = sum(pv_day.values())
        day_house = sum(non_geyser.values())
        day_geyser_required = geyser_required

        day_geyser_heated = 0.0
        day_charge = 0.0
        day_discharge = 0.0
        day_unserved = 0.0
        day_curtailed = 0.0

        # The geyser is an optional thermal-storage load during an outage.
        # We don't need to model its normal hourly demand because any heating
        # is only allowed when there is genuine solar surplus after the battery
        # is full. The daily energy target is simply a maximum to be met.
        geyser_remaining = geyser_required

        for h in range(24):
            p = pv_day[h]
            house = non_geyser.get(h, 0.0)

            # 1. PV directly supplies the normal household load.
            pv_to_house = min(p, house)
            remaining_house = house - pv_to_house
            surplus = max(0.0, p - pv_to_house)

            # 2. If the house needs more energy, discharge the battery.
            if remaining_house > 0:
                available = max(0.0, soc - reserve_kwh)
                battery_draw = min(
                    remaining_house / mod.BATTERY_EFF,
                    available
                )
                soc -= battery_draw
                delivered = battery_draw * mod.BATTERY_EFF
                day_discharge += delivered
                total_battery_discharge += delivered
                remaining_house -= delivered

                if remaining_house > 1e-9:
                    day_unserved += remaining_house
                    total_unserved_house += remaining_house

            # 3. Once the household is supplied, charge the battery from
            # remaining PV.
            if surplus > 0:
                room = max(0.0, args.battery_kwh - soc)
                charge_input = min(
                    surplus,
                    room / mod.BATTERY_EFF
                )
                soc += charge_input * mod.BATTERY_EFF
                surplus -= charge_input
                day_charge += charge_input
                total_battery_charge_input += charge_input

            # 4. Only PV surplus remaining after the battery is FULL can heat
            # the geyser. The geyser is capped at 3 kW per hour.
            if surplus > 0 and soc >= args.battery_kwh - 1e-9:
                geyser_heat = min(
                    surplus,
                    args.geyser_max_kw,
                    geyser_remaining
                )
                geyser_remaining -= geyser_heat
                day_geyser_heated += geyser_heat
                surplus -= geyser_heat

            # 5. Anything left is curtailed. No grid import is allowed.
            day_curtailed += surplus

            min_soc = min(min_soc, soc)

            if soc <= reserve_kwh + 1e-9 and not reserve_reached:
                reserve_reached = True
                reserve_hour = (
                    f"{day} {h:02d}:00"
                )

        total_pv += day_pv
        total_house_load += day_house
        total_geyser_required += day_geyser_required
        total_geyser_heated += day_geyser_heated
        total_curtailed += day_curtailed

        daily_results.append({
            "date": str(day),
            "pv_kwh": day_pv,
            "house_load_kwh": day_house,
            "geyser_required_kwh": day_geyser_required,
            "geyser_heated_kwh": day_geyser_heated,
            "battery_charge_kwh": day_charge,
            "battery_discharge_kwh": day_discharge,
            "unserved_house_kwh": day_unserved,
            "curtailed_kwh": day_curtailed,
            "end_soc_kwh": soc,
        })

    return {
        "start_date": daily_results[0]["date"],
        "end_date": daily_results[-1]["date"],
        "duration_days": len(daily_results),
        "start_soc_kwh": start_soc,
        "end_soc_kwh": soc,
        "min_soc_kwh": min_soc,
        "pv_kwh": total_pv,
        "house_load_kwh": total_house_load,
        "geyser_required_kwh": total_geyser_required,
        "geyser_heated_kwh": total_geyser_heated,
        "battery_charge_kwh": total_battery_charge_input,
        "battery_discharge_kwh": total_battery_discharge,
        "unserved_house_kwh": total_unserved_house,
        "curtailed_kwh": total_curtailed,
        "reserve_reached": reserve_reached,
        "reserve_reached_at": reserve_hour or "",
        "daily": daily_results,
    }


def main():
    args = parse_args()

    root = Path.cwd()
    db = Path(args.db).expanduser()
    script = Path(args.script).expanduser()
    out = Path(args.output_dir).expanduser()
    cache = (
        Path(args.pv_cache).expanduser()
        if args.pv_cache
        else root / "solar report/pvgis/pvgis cache"
    )

    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    mod = load_model(script)

    pv = build_pv(mod, args.year, cache, args.no_download)
    load_profile = mod.load_complete_measured_day(db)
    projection = mod.load_daily_projection(db)

    print("=" * 72)
    print("Solar Step 6 — grid-outage resilience simulation")
    print("=" * 72)
    print(f"PVGIS year:                 {args.year}")
    print("PV:                         7.00 kWp (3.5 east + 3.5 west)")
    print(f"Inverter reference:         {args.inverter_kw:.2f} kW")
    print(f"Battery usable:             {args.battery_kwh:.2f} kWh")
    print(f"Battery reserve:            {args.reserve * 100:.1f}%")
    print("Load model:                 daily_load_projection")
    print(f"Measured hourly shape:      {load_profile['date']}")
    print("Outage geyser strategy:     excess PV only after battery is full")
    if args.start_soc_mode == "full":
        print("Outage starting SOC:         100% (full battery)")
    else:
        print("Outage starting SOC:         preceding normal-day SOC")
    print()

    normal_daily, normal_annual = simulate_normal_year(
        mod, pv, load_profile, projection, args, args.year
    )

    all_results = {1: [], 2: [], 3: []}

    for duration in (1, 2, 3):
        for start_index in range(len(normal_daily) - duration + 1):
            result = simulate_outage(
                mod, pv, load_profile, projection, args,
                start_index, duration, normal_daily
            )
            all_results[duration].append(result)

    # Worst windows are ranked primarily by unserved household energy, then
    # by minimum SOC. This answers "can the house survive?" before looking at
    # battery reserve behaviour.
    for duration in (1, 2, 3):
        all_results[duration].sort(
            key=lambda x: (
                x["unserved_house_kwh"],
                -x["min_soc_kwh"],
                -x["geyser_heated_kwh"],
            ),
            reverse=True,
        )

    # Write every outage start-day result so we can inspect the complete
    # weather sequence later rather than only the worst case.
    mode_label = "full battery" if args.start_soc_mode == "full" else "normal SOC"
    csv_path = out / f"pvgis {args.year} outage windows - {mode_label}.csv"
    fields = [
        "duration_days", "start_date", "end_date",
        "start_soc_kwh", "end_soc_kwh", "min_soc_kwh",
        "pv_kwh", "house_load_kwh", "geyser_required_kwh",
        "geyser_heated_kwh", "battery_charge_kwh",
        "battery_discharge_kwh", "unserved_house_kwh",
        "curtailed_kwh", "reserve_reached", "reserve_reached_at",
    ]

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for duration in (1, 2, 3):
            for result in all_results[duration]:
                writer.writerow({
                    key: result[key] for key in fields
                })

    print("Normal annual simulation:")
    print(f"  Annual load:              {normal_annual['load']:.1f} kWh")
    print(f"  Annual grid import:       {normal_annual['grid']:.1f} kWh")
    print()

    for duration in (1, 2, 3):
        print("=" * 72)
        print(f"WORST {duration}-DAY OUTAGE WINDOWS")
        print("=" * 72)
        print(
            "Rank  Start       End         Start SOC  Min SOC  End SOC  "
            "PV     House  Unserved  Geyser heated"
        )
        print(
            "----  ----------  ----------  ---------  -------  -------  "
            "-----  -----  --------  -------------"
        )

        for rank, result in enumerate(all_results[duration][:10], start=1):
            print(
                f"{rank:>4}  "
                f"{result['start_date']}  "
                f"{result['end_date']}  "
                f"{result['start_soc_kwh']:>9.2f}  "
                f"{result['min_soc_kwh']:>7.2f}  "
                f"{result['end_soc_kwh']:>7.2f}  "
                f"{result['pv_kwh']:>5.1f}  "
                f"{result['house_load_kwh']:>5.1f}  "
                f"{result['unserved_house_kwh']:>8.2f}  "
                f"{result['geyser_heated_kwh']:>13.2f}"
            )

        worst = all_results[duration][0]
        print()
        print(f"Worst {duration}-day window: "
              f"{worst['start_date']} to {worst['end_date']}")
        print(f"  Starting SOC:             {worst['start_soc_kwh']:.2f} kWh")
        print(f"  Minimum SOC:              {worst['min_soc_kwh']:.2f} kWh")
        print(f"  Ending SOC:               {worst['end_soc_kwh']:.2f} kWh")
        print(f"  PV generation:            {worst['pv_kwh']:.2f} kWh")
        print(f"  House load served target: {worst['house_load_kwh']:.2f} kWh")
        print(f"  Unserved house load:      {worst['unserved_house_kwh']:.2f} kWh")
        print(f"  Geyser required:          {worst['geyser_required_kwh']:.2f} kWh")
        print(f"  Geyser actually heated:   {worst['geyser_heated_kwh']:.2f} kWh")
        print(f"  Battery discharge:        {worst['battery_discharge_kwh']:.2f} kWh")
        print(f"  PV curtailed:             {worst['curtailed_kwh']:.2f} kWh")
        print(f"  Reserve reached:          {'YES' if worst['reserve_reached'] else 'NO'}")
        if worst["reserve_reached"]:
            print(f"  Reserve first reached:    {worst['reserve_reached_at']}")
        print()

    print(f"Complete outage-window CSV: {csv_path}")
    print()
    if args.start_soc_mode == "full":
        print(
            "This run starts every outage with a full battery. "
            "Use --start-soc-mode normal for the separate depleted-battery "
            "stress test."
        )
    else:
        print(
            "This is the depleted-battery stress test. "
            "The primary resilience run uses the default full-battery mode."
        )


if __name__ == "__main__":
    main()
