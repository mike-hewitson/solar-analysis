#!/usr/bin/env python3
"""Run solar_report.py and email its output with the generated charts."""
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
REPORT_DIR = SCRIPT_DIR / "solar report"
IMAGES = [
    REPORT_DIR / "solar daily load profile.png",
    REPORT_DIR / "solar hourly load profile.png",
    REPORT_DIR / "solar hourly energy profile.png",
]
RECIPIENTS = ["mike@hewitson.co.za", "hewitson.is.me@gmail.com"]
SENDER = "hewitson.is.me@gmail.com"
NTFY_ENV = Path.home() / ".config" / "solar-db-backup" / "ntfy.env"


def ntfy_topic() -> str | None:
    """Read NTFY_TOPIC without evaluating the shell config file."""
    try:
        lines = NTFY_ENV.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if separator and key.strip() == "NTFY_TOPIC":
            return value.strip().strip("\"'") or None
    return None


def notify_failure(reason: str, details: str) -> bool:
    topic = ntfy_topic()
    if not topic:
        print(f"ntfy notification skipped: NTFY_TOPIC not found in {NTFY_ENV}", file=sys.stderr)
        return False
    url = topic if topic.startswith(("https://", "http://")) else f"https://ntfy.sh/{topic}"
    response = subprocess.run(
        [
            "/usr/bin/curl", "--fail", "--silent", "--show-error",
            "-H", "Title: Solar report failed",
            "-H", "Priority: high",
            "-H", "Tags: warning",
            "--data-binary", f"{reason}\n\n{details[-3000:]}",
            url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if response.returncode:
        print(f"ntfy notification failed: {response.stderr.strip()}", file=sys.stderr)
        return False
    print("ntfy failure notification sent.")
    return True


def main() -> int:
    if sys.argv[1:] == ["--test-ntfy"]:
        return 0 if notify_failure("Solar report ntfy test.", "Failure notification channel is reachable.") else 1

    run = subprocess.run(
        [str(SCRIPT_DIR / ".venv" / "bin" / "python"), str(SCRIPT_DIR / "solar_report.py")],
        cwd=SCRIPT_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    success = run.returncode == 0
    now = datetime.now().astimezone()

    message = EmailMessage()
    message["From"] = SENDER
    message["To"] = RECIPIENTS[0]
    message["Cc"] = RECIPIENTS[1]
    message["Subject"] = f"Solar load profile report — {now:%Y-%m-%d} ({'success' if success else 'FAILED'})"
    body = [
        f"Solar report run: {now:%Y-%m-%d %H:%M:%S %Z}",
        f"Result: {'success' if success else f'FAILED (exit code {run.returncode})'}",
        "",
        "Report output:",
        "=" * 72,
        run.stdout or "(No output)",
    ]
    missing = [str(path) for path in IMAGES if not path.is_file()]
    if missing:
        body.extend(["", "Expected chart image(s) not found:", *missing])
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

    for path in IMAGES:
        if path.is_file():
            message.add_attachment(
                path.read_bytes(), maintype="image", subtype="png", filename=path.name
            )

    sent = subprocess.run(
        ["/usr/bin/msmtp", "-a", "gmail", "-t"],
        input=message.as_bytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if sent.returncode:
        sys.stderr.write(sent.stderr.decode(errors="replace"))
        notify_failure(
            "Solar report email delivery failed.",
            sent.stderr.decode(errors="replace") or "msmtp returned a failure status.",
        )
        return sent.returncode
    print(f"Email sent to {', '.join(RECIPIENTS)}; attached {len(IMAGES) - len(missing)} chart image(s).")
    if not success:
        notify_failure(f"Solar report failed with exit code {run.returncode}.", run.stdout or "No report output.")
        return run.returncode or 1
    if missing:
        notify_failure("Solar report did not create all expected chart images.", "\n".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
