import os
import subprocess
from pathlib import Path

from django.conf import settings


def _connection():
    import cups

    cups.setServer(settings.PM_CUPS_SERVER)
    return cups.Connection()


def sanitize_attributes(attrs):
    safe = {}
    wanted = {
        "printer-info", "printer-location", "printer-make-and-model", "printer-state",
        "printer-state-message", "printer-is-accepting-jobs", "queued-job-count",
        "media-supported", "media-default", "sides-supported", "sides-default",
        "print-color-mode-supported", "print-color-mode-default", "print-quality-supported",
        "orientation-requested-supported", "printer-supply", "printer-supply-description",
    }
    for key in wanted:
        value = attrs.get(key)
        if isinstance(value, (str, int, bool, float)):
            safe[key] = value
        elif isinstance(value, (list, tuple)):
            safe[key] = [x for x in value if isinstance(x, (str, int, bool, float))]
    return safe


def ensure_queue(printer):
    conn = _connection()
    conn.addPrinter(printer.queue_name, device=printer.uri, ppdname="everywhere", info=printer.device.name, location=printer.device.location)
    conn.enablePrinter(printer.queue_name)
    conn.acceptJobs(printer.queue_name)
    return refresh_printer(printer)


def remove_queue(printer):
    _connection().deletePrinter(printer.queue_name)


def refresh_printer(printer):
    conn = _connection()
    attrs = conn.getPrinterAttributes(printer.queue_name)
    clean = sanitize_attributes(attrs)
    printer.capabilities = clean
    printer.accepting_jobs = bool(clean.get("printer-is-accepting-jobs", True))
    printer.queue_enabled = int(clean.get("printer-state", 5)) != 5
    printer.queued_jobs = int(clean.get("queued-job-count", 0))
    printer.supplies = clean.get("printer-supply", [])
    printer.save(update_fields=["capabilities", "accepting_jobs", "queue_enabled", "queued_jobs", "supplies"])
    printer.device.status = "online"
    printer.device.status_message = str(clean.get("printer-state-message", ""))[:255]
    from django.utils import timezone
    printer.device.last_seen_at = timezone.now()
    printer.device.save(update_fields=["status", "status_message", "last_seen_at", "updated_at"])
    return clean


def set_queue_state(printer, command):
    conn = _connection()
    actions = {
        "enable": lambda: conn.enablePrinter(printer.queue_name),
        "disable": lambda: conn.disablePrinter(printer.queue_name),
        "accept": lambda: conn.acceptJobs(printer.queue_name),
        "reject": lambda: conn.rejectJobs(printer.queue_name),
    }
    if command not in actions:
        raise ValueError("Unsupported queue command")
    actions[command]()
    refresh_printer(printer)


def set_defaults(printer, options):
    environment = {**os.environ, "CUPS_SERVER": settings.PM_CUPS_SERVER}
    for key, value in options.items():
        if value:
            result = subprocess.run(["lpadmin", "-p", printer.queue_name, "-o", f"{key}-default={value}"], capture_output=True, text=True, timeout=30, env=environment)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "Unable to update printer defaults")
    printer.default_options = {k: v for k, v in options.items() if v}
    printer.save(update_fields=["default_options"])


def submit_file(printer, path, title, options):
    return _connection().printFile(printer.queue_name, str(path), title, {str(k): str(v) for k, v in options.items() if v not in (None, "")})


def test_page(printer):
    candidates = [Path("/usr/share/cups/data/testprint"), Path("/usr/share/cups/data/default-testpage.pdf")]
    page = next((p for p in candidates if p.exists()), None)
    if not page:
        raise RuntimeError("The CUPS test page is not installed")
    return submit_file(printer, page, "Printer Manager test page", {})


def job_command(job_id, command):
    conn = _connection()
    if command == "cancel":
        conn.cancelJob(int(job_id), purge_job=False)
    elif command == "hold":
        conn.setJobHoldUntil(int(job_id), "indefinite")
    elif command == "release":
        conn.setJobHoldUntil(int(job_id), "no-hold")
    else:
        raise ValueError("Unsupported job command")


def get_job_state(job_id):
    attrs = _connection().getJobAttributes(int(job_id))
    return int(attrs.get("job-state", 0)), str(attrs.get("job-state-reasons", ""))


def remove_queue_by_name(queue_name):
    _connection().deletePrinter(queue_name)
