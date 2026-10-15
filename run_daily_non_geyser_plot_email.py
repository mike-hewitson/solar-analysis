#!/usr/bin/env python3
"""Generate yesterday's non-geyser chart and email it as an attachment."""
from datetime import datetime, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
import subprocess
import sys
import time

from run_solar_report_email import notify_failure

SCRIPT_DIR = Path(__file__).resolve().parent
REPORT_DIR = SCRIPT_DIR / "solar report"
TZ = ZoneInfo("Africa/Johannesburg")
RECIPIENTS = ["mike@hewitson.co.za", "hewitson.is.me@gmail.com"]
SENDER = "hewitson.is.me@gmail.com"


def healthcheck_url() -> str | None:
    env_path = Path.home() / ".config" / "solar-heartbeats" / "solar.env"
    try:
        lines = env_path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key.strip() == "HC_PLOT":
            return value.strip().strip("\"'") or None
    return None


def main() -> int:
    run_day = datetime.now(TZ).date() - timedelta(days=1)
    image = REPORT_DIR / f"non_geyser_power_{run_day.isoformat()}.png"
    started_at = time.time()
    run = subprocess.run(
        [
            str(SCRIPT_DIR / ".venv" / "bin" / "python"),
            str(SCRIPT_DIR / "plot_non_geyser_power.py"),
            "--date", run_day.isoformat(),
        ],
        cwd=SCRIPT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    image_created = image.is_file() and image.stat().st_mtime >= started_at
    success = run.returncode == 0 and image_created
    now = datetime.now(TZ)

    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = RECIPIENTS[0]
    message["Cc"] = RECIPIENTS[1]
    result = "success" if success else "FAILED"
    message["Subject"] = f"Solar non-geyser power plot — {run_day.isoformat()} ({result})"
    body = [
        f"Plot run: {now:%Y-%m-%d %H:%M:%S %Z}",
        f"Reading date: {run_day.isoformat()}",
        f"Result: {result}" if success else f"Result: FAILED (exit code {run.returncode})",
        "",
        "Plot output:",
        "=" * 72,
        run.stdout or "(No output)",
    ]
    if not image_created:
        body.extend(["", f"Expected graph was not created: {image}"])
    body_text = "\n".join(body)
    message.set_content(body_text)
    html_body = (
        '<!doctype html><html><body style="margin:0;padding:16px;">'
        '<pre style="margin:0;font-family:Menlo,Monaco,Consolas,'
        '\'Liberation Mono\',\'Courier New\',monospace;'
        'font-size:13px;line-height:1.35;white-space:pre;">'
        f"{escape(body_text)}</pre></body></html>"
    )
    message.add_alternative(html_body, subtype="html")

    if image_created:
        message.add_attachment(
            image.read_bytes(), maintype="image", subtype="png", filename=image.name
        )

    sent = subprocess.run(
        ["/usr/bin/msmtp", "-a", "gmail", "-t"],
        input=message.as_bytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if sent.returncode:
        detail = sent.stderr.decode(errors="replace") or "msmtp returned a failure status."
        sys.stderr.write(detail)
        notify_failure("Solar non-geyser plot email delivery failed.", detail)
        return sent.returncode

    print(f"Email sent to {', '.join(RECIPIENTS)}; graph attached: {image_created}.")
    if not success:
        notify_failure(
            f"Solar non-geyser plot failed for {run_day.isoformat()} (exit code {run.returncode}).",
            run.stdout or f"Expected graph was not created: {image}",
        )
        return run.returncode or 1

    ping_url = healthcheck_url()
    if ping_url:
        ping = subprocess.run(
            ["/usr/bin/curl", "--fail", "--silent", "--show-error", "--max-time", "10", ping_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if ping.returncode:
            detail = ping.stderr.strip() or "Healthchecks.io ping returned a failure status."
            notify_failure("Solar non-geyser plot Healthchecks.io ping failed.", detail)
            return ping.returncode
        print("Healthchecks.io success ping sent.")
    else:
        print("Healthchecks.io ping not configured (HC_PLOT is absent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
