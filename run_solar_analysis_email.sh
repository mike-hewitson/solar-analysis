#!/bin/sh
# Run the Solar Step 3 analysis and email its complete output.

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

body_file=$(mktemp)
message_file=$(mktemp)
trap 'rm -f "$body_file" "$message_file"' EXIT HUP INT TERM

if ./.venv/bin/python solar_analysis.py >"$body_file" 2>&1; then
    analysis_status=0
    result=SUCCESS
else
    analysis_status=$?
    result="FAILED (exit ${analysis_status})"
fi

{
    printf 'From: hewitson.is.me@gmail.com\n'
    printf 'To: mike@hewitson.co.za\n'
    printf 'Cc: hewitson.is.me@gmail.com\n'
    printf 'Subject: Solar Step 3 analysis %s - %s\n' "$result" "$(date '+%Y-%m-%d')"
    printf 'MIME-Version: 1.0\n'
    printf 'Content-Type: text/plain; charset=UTF-8\n'
    printf '\n'
    cat "$body_file"
} >"$message_file"

if /usr/bin/msmtp -a gmail -t <"$message_file"; then
    mail_status=0
else
    mail_status=$?
fi

if [ "$mail_status" -ne 0 ]; then
    exit "$mail_status"
fi

exit "$analysis_status"
