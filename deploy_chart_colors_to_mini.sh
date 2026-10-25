#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MINI_HOST="piholeadmin@192.168.1.37"
MINI_DIR="~/solar-health-check/"

cd "$SCRIPT_DIR"
scp \
  solar_chart_colors.py \
  solar_report.py \
  plot_non_geyser_power.py \
  "$MINI_HOST:$MINI_DIR"

printf 'Copied the chart palette and updated chart scripts to %s:%s\n' \
  "$MINI_HOST" "$MINI_DIR"
