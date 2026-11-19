#!/usr/bin/env python3
"""Run the monthly usage projection and email its complete output."""

from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
import subprocess
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
RECIPIENT = "mike@hewitson.co.za"
CC = "hewitson.is.me@gmail.com"


def main() -> int:
    run = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "solar_usage_projection.py")],
        cwd=SCRIPT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    success = run.returncode == 0
    if not success and run.stdout:
        print("Projection command failed; captured output follows:", file=sys.stderr)
        sys.stderr.write(run.stdout)
        if not run.stdout.endswith("\n"):
            sys.stderr.write("\n")
    now = datetime.now().astimezone()
    message = EmailMessage()
    message["From"] = "hewitson.is.me@gmail.com"
    message["To"] = RECIPIENT
    message["Cc"] = CC
    message["Subject"] = (
        f"Solar monthly usage projection — {now:%Y-%m} "
        f"({'success' if success else f'FAILED (exit {run.returncode})'})"
    )
    body = [
        f"Projection run: {now:%Y-%m-%d %H:%M:%S %Z}",
        f"Result: {'success' if success else f'FAILED (exit code {run.returncode})'}",
        "",
        "Complete projection output:",
        "=" * 72,
        run.stdout or "(No output)",
    ]
    text = "\n".join(body)
    message.set_content(text)
    message.add_alternative(
        '<!doctype html><html><body><pre style="font-family:monospace;'
        f'white-space:pre-wrap">{escape(text)}</pre></body></html>',
        subtype="html",
    )

    sent = subprocess.run(
        ["/usr/bin/msmtp", "-a", "gmail", "-t"],
        input=message.as_bytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if sent.returncode:
        sys.stderr.write(sent.stderr.decode(errors="replace"))
        return sent.returncode
    print(f"Projection output emailed to {RECIPIENT} (cc {CC}).")
    return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
