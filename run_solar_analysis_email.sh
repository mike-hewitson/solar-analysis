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

if ! ./.venv/bin/python - "$body_file" "$result" "$message_file" <<'PYMAIL'
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
import sys

body_path, result, message_path = sys.argv[1:]
report = Path(body_path).read_text(encoding="utf-8", errors="replace")
message = EmailMessage()
message["From"] = "hewitson.is.me@gmail.com"
message["To"] = "mike@hewitson.co.za"
message["Cc"] = "hewitson.is.me@gmail.com"
now = datetime.now().astimezone()
message["Subject"] = f"Solar Step 3 analysis {result} - {now:%Y-%m-%d}"
plain_text = report or "(No analysis output)\n"
message.set_content(plain_text)
html_body = (
    '<!doctype html><html><body style="margin:0;padding:16px;">'
    '<pre style="margin:0;font-family:Menlo,Monaco,Consolas,'
    '\'Liberation Mono\',\'Courier New\',monospace;'
    'font-size:13px;line-height:1.35;white-space:pre;">'
    f"{escape(plain_text)}</pre></body></html>"
)
message.add_alternative(html_body, subtype="html")
Path(message_path).write_bytes(message.as_bytes())
PYMAIL
then
    echo "Could not construct the Solar analysis email." >&2
    exit 1
fi

if /usr/bin/msmtp -a gmail -t <"$message_file"; then
    mail_status=0
else
    mail_status=$?
fi

if [ "$mail_status" -ne 0 ]; then
    exit "$mail_status"
fi

exit "$analysis_status"
