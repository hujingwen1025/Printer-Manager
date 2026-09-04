import logging
import os
import shutil
import socket
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .audit import record
from .models import AppSetting, DiscoveryRun, PrintJob, ScanJob, Task
from .services import cups_service
from .services.discovery import discover_lan, discover_mdns
from .services.documents import normalize_for_print
from .services.sane_service import fetch_capabilities, regenerate_config, run_scan


logger = logging.getLogger(__name__)
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def claim_task():
    now = timezone.now()
    with transaction.atomic():
        Task.objects.filter(state=Task.State.RUNNING, lease_expires_at__lt=now).update(
            state=Task.State.PENDING, lease_owner="", lease_expires_at=None
        )
        task = Task.objects.select_for_update().filter(state=Task.State.PENDING, run_after__lte=now).first()
        if not task:
            return None
        task.state = Task.State.RUNNING
        task.lease_owner = WORKER_ID
        task.lease_expires_at = now + timedelta(minutes=20)
        task.attempts += 1
        task.save(update_fields=["state", "lease_owner", "lease_expires_at", "attempts", "updated_at"])
        return task


def process_one():
    task = claim_task()
    if not task:
        return False
    try:
        HANDLERS[task.kind](**task.payload)
    except Exception as exc:
        logger.exception("Task %s failed", task.id)
        task.error = str(exc)[:500]
        if task.attempts < task.max_attempts:
            task.state = Task.State.PENDING
            task.run_after = timezone.now() + timedelta(seconds=10 * task.attempts)
        else:
            task.state = Task.State.FAILED
        task.lease_owner = ""
        task.lease_expires_at = None
        task.save(update_fields=["state", "error", "run_after", "lease_owner", "lease_expires_at", "updated_at"])
    else:
        task.state = Task.State.COMPLETE
        task.error = ""
        task.lease_owner = ""
        task.lease_expires_at = None
        task.save(update_fields=["state", "error", "lease_owner", "lease_expires_at", "updated_at"])
    return True


def handle_discovery(run_id):
    run = DiscoveryRun.objects.get(pk=run_id)
    run.state = DiscoveryRun.State.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["state", "started_at"])
    try:
        duration = AppSetting.get_int("discovery_seconds", 12, minimum=3, maximum=60)
        run.results = discover_mdns(duration) if run.kind == DiscoveryRun.Kind.MDNS else discover_lan(run.cidr)
        run.state = DiscoveryRun.State.COMPLETE
    except Exception as exc:
        run.state = DiscoveryRun.State.FAILED
        run.error = str(exc)[:500]
        raise
    finally:
        run.finished_at = timezone.now()
        run.save(update_fields=["results", "state", "error", "finished_at"])


def handle_configure_printer(printer_id):
    from .models import PrinterEndpoint
    printer = PrinterEndpoint.objects.select_related("device").get(pk=printer_id)
    try:
        cups_service.ensure_queue(printer)
    except Exception as exc:
        printer.device.status = "offline"
        printer.device.status_message = str(exc)[:255]
        printer.device.save(update_fields=["status", "status_message", "updated_at"])
        raise


def handle_configure_scanner(scanner_id):
    from .models import ScannerEndpoint
    scanner = ScannerEndpoint.objects.select_related("device").get(pk=scanner_id)
    regenerate_config()
    try:
        fetch_capabilities(scanner)
        scanner.device.status = "online"
        scanner.device.last_seen_at = timezone.now()
        scanner.device.status_message = ""
    except Exception as exc:
        scanner.device.status = "offline"
        scanner.device.status_message = str(exc)[:255]
        raise
    finally:
        scanner.device.save(update_fields=["status", "last_seen_at", "status_message", "updated_at"])


def handle_print(print_job_id):
    job = PrintJob.objects.select_related("printer", "printer__device", "owner").get(pk=print_job_id)
    if job.cancel_requested:
        job.state = PrintJob.State.CANCELLED
        job.completed_at = timezone.now()
        job.save(update_fields=["state", "completed_at", "updated_at"])
        return
    job.state = PrintJob.State.CONVERTING
    job.error = ""
    job.save(update_fields=["state", "error", "updated_at"])
    try:
        output_dir = Path(settings.MEDIA_ROOT) / "prints" / str(job.id)
        normalized = normalize_for_print(job.source_path, job.mime_type, output_dir)
        job.normalized_path = str(normalized)
        job.cups_job_id = cups_service.submit_file(job.printer, normalized, job.title, job.options)
        job.state = PrintJob.State.SUBMITTED
        job.save(update_fields=["normalized_path", "cups_job_id", "state", "updated_at"])
    except Exception as exc:
        job.state = PrintJob.State.FAILED
        job.error = str(exc)[:500]
        job.completed_at = timezone.now()
        job.save(update_fields=["state", "error", "completed_at", "updated_at"])
        raise


def handle_scan(scan_job_id):
    job = ScanJob.objects.select_related("scanner", "scanner__device", "owner").get(pk=scan_job_id)
    if job.cancel_requested:
        job.state = ScanJob.State.CANCELLED
        job.completed_at = timezone.now()
        job.save(update_fields=["state", "completed_at", "updated_at"])
        return
    job.state = ScanJob.State.SCANNING
    job.error = ""
    job.save(update_fields=["state", "error", "updated_at"])
    started = time.monotonic()
    record("scan.started", target=job, detail={"scanner": job.scanner.device.name})
    try:
        result, pages, diagnostics = run_scan(job)
        job.result_path = str(result)
        job.page_count = pages
        job.state = ScanJob.State.COMPLETE
        job.completed_at = timezone.now()
        job.save(update_fields=["result_path", "page_count", "state", "completed_at", "updated_at"])
        record("scan.completed", target=job, detail=diagnostics)
    except InterruptedError as exc:
        job.state = ScanJob.State.CANCELLED
        job.error = str(exc)[:500]
        job.completed_at = timezone.now()
        job.save(update_fields=["state", "error", "completed_at", "updated_at"])
        record("scan.cancelled", target=job, detail={
            "exception_type": type(exc).__name__, "diagnostics": getattr(exc, "diagnostics", {}),
            "duration_seconds": round(time.monotonic() - started, 2),
        })
    except Exception as exc:
        job.state = ScanJob.State.FAILED
        job.error = str(exc)[:500]
        job.completed_at = timezone.now()
        job.save(update_fields=["state", "error", "completed_at", "updated_at"])
        record("scan.failed", target=job, detail={
            "exception_type": type(exc).__name__, "message": str(exc)[:500],
            "diagnostics": getattr(exc, "diagnostics", {}),
            "duration_seconds": round(time.monotonic() - started, 2),
        })
        raise


def handle_refresh_printer(printer_id):
    from .models import PrinterEndpoint
    printer = PrinterEndpoint.objects.select_related("device").get(pk=printer_id)
    try:
        cups_service.refresh_printer(printer)
    except Exception as exc:
        printer.device.status = "offline"
        printer.device.status_message = str(exc)[:255]
        printer.device.save(update_fields=["status", "status_message", "updated_at"])
        raise


def handle_printer_command(printer_id, command, options=None):
    from .models import PrinterEndpoint
    printer = PrinterEndpoint.objects.select_related("device").get(pk=printer_id)
    if command == "test":
        cups_service.test_page(printer)
    elif command == "defaults":
        cups_service.set_defaults(printer, options or {})
    else:
        cups_service.set_queue_state(printer, command)


def handle_print_job_command(print_job_id, command):
    job = PrintJob.objects.get(pk=print_job_id)
    if command == "retry":
        if not Path(job.source_path).exists():
            raise RuntimeError("The retained source file has expired")
        job.state = PrintJob.State.PENDING
        job.error = ""
        job.cups_job_id = None
        job.cancel_requested = False
        job.save(update_fields=["state", "error", "cups_job_id", "cancel_requested", "updated_at"])
        Task.enqueue("print", print_job_id=str(job.id))
    elif job.cups_job_id:
        cups_service.job_command(job.cups_job_id, command)
        if command == "cancel":
            job.state = PrintJob.State.CANCELLED
            job.completed_at = timezone.now()
        elif command == "hold":
            job.state = PrintJob.State.HELD
        elif command == "release":
            job.state = PrintJob.State.SUBMITTED
        job.save(update_fields=["state", "completed_at", "updated_at"])
    elif command == "cancel":
        job.cancel_requested = True
        job.state = PrintJob.State.CANCELLED
        job.completed_at = timezone.now()
        job.save(update_fields=["cancel_requested", "state", "completed_at", "updated_at"])


def handle_delete_printer_queue(queue_name):
    try:
        cups_service.remove_queue_by_name(queue_name)
    except Exception as exc:
        if "not found" not in str(exc).lower():
            raise


def handle_regenerate_scanners():
    regenerate_config()


def sync_print_jobs():
    mapping = {3: PrintJob.State.PENDING, 4: PrintJob.State.HELD, 5: PrintJob.State.PRINTING, 7: PrintJob.State.CANCELLED, 8: PrintJob.State.CANCELLED, 9: PrintJob.State.COMPLETE}
    jobs = PrintJob.objects.exclude(cups_job_id=None).filter(state__in=[PrintJob.State.SUBMITTED, PrintJob.State.HELD, PrintJob.State.PRINTING])
    for job in jobs:
        try:
            cups_state, _ = cups_service.get_job_state(job.cups_job_id)
            next_state = mapping.get(cups_state)
            if next_state and next_state != job.state:
                job.state = next_state
                if next_state in {PrintJob.State.COMPLETE, PrintJob.State.CANCELLED}:
                    job.completed_at = timezone.now()
                job.save(update_fields=["state", "completed_at", "updated_at"])
        except Exception:
            logger.warning("Could not refresh CUPS job %s", job.cups_job_id, exc_info=True)


def cleanup_expired():
    now = timezone.now()
    for job in PrintJob.objects.filter(artifact_expires_at__lte=now):
        for path in (job.source_path, job.normalized_path):
            if path:
                Path(path).unlink(missing_ok=True)
        directory = Path(settings.MEDIA_ROOT) / "prints" / str(job.id)
        shutil.rmtree(directory, ignore_errors=True)
        if job.source_path or job.normalized_path:
            job.source_path = ""
            job.normalized_path = ""
            job.save(update_fields=["source_path", "normalized_path", "updated_at"])
    for job in ScanJob.objects.filter(artifact_expires_at__lte=now).exclude(result_path=""):
        result = Path(job.result_path)
        result.unlink(missing_ok=True)
        expected = Path(settings.MEDIA_ROOT) / "scans" / str(job.id)
        shutil.rmtree(expected, ignore_errors=True)
        job.result_path = ""
        job.save(update_fields=["result_path", "updated_at"])


HANDLERS = {
    "discovery": handle_discovery,
    "configure_printer": handle_configure_printer,
    "configure_scanner": handle_configure_scanner,
    "print": handle_print,
    "scan": handle_scan,
    "refresh_printer": handle_refresh_printer,
    "printer_command": handle_printer_command,
    "print_job_command": handle_print_job_command,
    "delete_printer_queue": handle_delete_printer_queue,
    "regenerate_scanners": handle_regenerate_scanners,
}
