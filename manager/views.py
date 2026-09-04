import mimetypes
import secrets
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from .audit import record
from .forms import (DeviceForm, DiscoveryForm, ManagedUserEditForm, ManagedUserForm, PrintJobForm,
                    QueueDefaultsForm, ScanJobForm, SettingsForm)
from .models import AppSetting, AuditEvent, Device, DiscoveryRun, PrinterEndpoint, PrintJob, ScanJob, ScannerEndpoint, Task, print_expiration, scan_expiration
from .security import ROLE_ADMIN, ROLE_OPERATOR, admin_required, has_role, operator_required
from .services.documents import inspect_document


@login_required
def dashboard(request):
    devices = Device.objects.select_related("printer", "scanner")[:12]
    print_jobs = visible_print_jobs(request.user)[:8]
    scan_jobs = visible_scan_jobs(request.user)[:8]
    return render(request, "manager/dashboard.html", {"devices": devices, "print_jobs": print_jobs, "scan_jobs": scan_jobs})


def visible_print_jobs(user):
    qs = PrintJob.objects.select_related("owner", "printer__device")
    return qs if has_role(user, ROLE_ADMIN) else qs.filter(owner=user) if has_role(user, ROLE_OPERATOR) else qs.defer("source_path", "normalized_path")


def visible_scan_jobs(user):
    qs = ScanJob.objects.select_related("owner", "scanner__device")
    return qs if has_role(user, ROLE_ADMIN) else qs.filter(owner=user) if has_role(user, ROLE_OPERATOR) else qs.defer("result_path")


@login_required
def device_list(request):
    return render(request, "manager/device_list.html", {"devices": Device.objects.select_related("printer", "scanner")})


@login_required
def device_detail(request, pk):
    device = get_object_or_404(Device.objects.select_related("printer", "scanner"), pk=pk)
    return render(request, "manager/device_detail.html", {"device": device, "defaults_form": QueueDefaultsForm(initial=getattr(getattr(device, "printer", None), "default_options", {}))})


@admin_required
def device_create(request):
    initial = {key: request.GET.get(key, "") for key in ("name", "address", "printer_uri", "scanner_uri", "scanner_protocol")}
    if initial.get("printer_uri"):
        initial["queue_name"] = slugify(initial.get("name") or "printer")[:80]
    form = DeviceForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            device = form.save()
            _save_endpoints(device, form.cleaned_data)
        record("device.created", request=request, target=device)
        messages.success(request, "Device saved. Validation is running in the background.")
        return redirect("device_detail", pk=device.pk)
    return render(request, "manager/form_page.html", {"form": form, "title": "Add device", "submit_label": "Add device"})


@admin_required
def device_edit(request, pk):
    device = get_object_or_404(Device, pk=pk)
    initial = {key: request.GET.get(key) for key in ("printer_uri", "scanner_uri", "scanner_protocol") if request.GET.get(key)}
    form = DeviceForm(request.POST or None, instance=device, initial=initial)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            device = form.save()
            _save_endpoints(device, form.cleaned_data)
        record("device.updated", request=request, target=device)
        messages.success(request, "Device updated and queued for revalidation.")
        return redirect("device_detail", pk=device.pk)
    return render(request, "manager/form_page.html", {"form": form, "title": "Edit device", "submit_label": "Save changes"})


def _save_endpoints(device, data):
    if data.get("printer_uri"):
        previous = PrinterEndpoint.objects.filter(device=device).first()
        old_queue = previous.queue_name if previous else ""
        printer, _ = PrinterEndpoint.objects.update_or_create(device=device, defaults={"uri": data["printer_uri"], "queue_name": data["queue_name"]})
        if old_queue and old_queue != printer.queue_name:
            Task.enqueue("delete_printer_queue", queue_name=old_queue)
        Task.enqueue("configure_printer", printer_id=printer.pk)
    elif hasattr(device, "printer"):
        device.printer.delete()
    if data.get("scanner_uri"):
        scanner, _ = ScannerEndpoint.objects.update_or_create(device=device, defaults={"uri": data["scanner_uri"], "protocol": data.get("scanner_protocol") or "escl"})
        Task.enqueue("configure_scanner", scanner_id=scanner.pk)
    elif hasattr(device, "scanner"):
        device.scanner.delete()


@admin_required
@require_POST
def device_delete(request, pk):
    device = get_object_or_404(Device, pk=pk)
    if PrintJob.objects.filter(printer__device=device).exists() or ScanJob.objects.filter(scanner__device=device).exists():
        device.enabled = False
        device.save(update_fields=["enabled", "updated_at"])
        messages.info(request, "Device has job history, so it was disabled instead of deleted.")
    else:
        if hasattr(device, "printer"):
            Task.enqueue("delete_printer_queue", queue_name=device.printer.queue_name)
        record("device.deleted", request=request, target=device, detail={"name": device.name})
        device.delete()
        Task.enqueue("regenerate_scanners")
        messages.success(request, "Device deleted.")
    return redirect("device_list")


@admin_required
def discovery(request):
    form = DiscoveryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        run = DiscoveryRun.objects.create(kind=form.cleaned_data["kind"], cidr=form.cleaned_data["cidr"], requested_by=request.user)
        Task.enqueue("discovery", run_id=str(run.id))
        record("discovery.started", request=request, target=run, detail={"kind": run.kind, "cidr": run.cidr})
        return redirect("discovery_detail", pk=run.pk)
    return render(request, "manager/discovery.html", {"form": form, "runs": DiscoveryRun.objects.select_related("requested_by")[:15]})


@admin_required
def discovery_detail(request, pk):
    run = get_object_or_404(DiscoveryRun, pk=pk)
    known = {device.address: str(device.pk) for device in Device.objects.all()}
    results = [{**result, "existing_device_id": known.get(result.get("address"))} for result in run.results]
    return render(request, "manager/discovery_detail.html", {"run": run, "results": results})


@operator_required
def print_submit(request, printer_id):
    printer = get_object_or_404(PrinterEndpoint.objects.select_related("device"), pk=printer_id, device__enabled=True)
    form = PrintJobForm(request.POST or None, request.FILES or None, printer=printer)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["document"]
        job_id = secrets.token_hex(16)
        directory = Path(settings.MEDIA_ROOT) / "uploads" / job_id
        directory.mkdir(parents=True, mode=0o700)
        path = directory / "source"
        with path.open("wb") as destination:
            for chunk in upload.chunks():
                destination.write(chunk)
        path.chmod(0o600)
        try:
            mime = inspect_document(path)
        except ValueError as exc:
            path.unlink(missing_ok=True)
            form.add_error("document", str(exc))
        else:
            job = PrintJob.objects.create(owner=request.user, printer=printer, title=form.cleaned_data["title"] or Path(upload.name).stem,
                                          original_name=Path(upload.name).name[:255], source_path=str(path), mime_type=mime,
                                          size_bytes=upload.size, options=form.options(), artifact_expires_at=print_expiration())
            Task.enqueue("print", print_job_id=str(job.id))
            record("print.submitted", request=request, target=job, detail={"printer": printer.device.name})
            messages.success(request, "Print job queued.")
            return redirect("jobs")
    return render(request, "manager/print_submit.html", {"form": form, "printer": printer})


@operator_required
def scan_submit(request, scanner_id):
    scanner = get_object_or_404(ScannerEndpoint.objects.select_related("device"), pk=scanner_id, device__enabled=True)
    form = ScanJobForm(request.POST or None, scanner=scanner)
    if request.method == "POST" and form.is_valid():
        job = ScanJob.objects.create(owner=request.user, scanner=scanner, title=form.cleaned_data["title"] or f"Scan {timezone.localtime():%Y-%m-%d %H:%M}",
                                     options=form.options(), output_format=form.cleaned_data["output_format"], artifact_expires_at=scan_expiration())
        Task.enqueue("scan", scan_job_id=str(job.id))
        record("scan.submitted", request=request, target=job, detail={"scanner": scanner.device.name})
        messages.success(request, "Scan job queued. Load the scanner now if needed.")
        return redirect("jobs")
    return render(request, "manager/scan_submit.html", {"form": form, "scanner": scanner})


@login_required
def jobs(request):
    return render(request, "manager/jobs.html", {"print_jobs": visible_print_jobs(request.user), "scan_jobs": visible_scan_jobs(request.user)})


def _can_manage_job(user, job):
    return has_role(user, ROLE_ADMIN) or (has_role(user, ROLE_OPERATOR) and job.owner_id == user.id)


@login_required
@require_POST
def print_job_command(request, pk, command):
    job = get_object_or_404(PrintJob, pk=pk)
    if not _can_manage_job(request.user, job):
        raise PermissionDenied
    if command not in {"cancel", "hold", "release", "retry"}:
        raise Http404
    Task.enqueue("print_job_command", print_job_id=str(job.id), command=command)
    record(f"print.{command}", request=request, target=job)
    messages.success(request, f"Print job {command} requested.")
    return redirect("jobs")


@login_required
@require_POST
def scan_job_cancel(request, pk):
    job = get_object_or_404(ScanJob, pk=pk)
    if not _can_manage_job(request.user, job):
        raise PermissionDenied
    job.cancel_requested = True
    if job.state == ScanJob.State.PENDING:
        job.state = ScanJob.State.CANCELLED
        job.completed_at = timezone.now()
    job.save(update_fields=["cancel_requested", "state", "completed_at", "updated_at"])
    record("scan.cancel", request=request, target=job)
    messages.success(request, "Scan cancellation requested.")
    return redirect("jobs")


@login_required
def scan_download(request, pk):
    job = get_object_or_404(ScanJob, pk=pk)
    if not _can_manage_job(request.user, job) or not job.result_path:
        raise PermissionDenied
    path = Path(job.result_path)
    if not path.is_file():
        raise Http404
    record("scan.downloaded", request=request, target=job)
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)


@login_required
@require_POST
def scan_delete(request, pk):
    job = get_object_or_404(ScanJob, pk=pk)
    if not _can_manage_job(request.user, job):
        raise PermissionDenied
    if job.result_path:
        Path(job.result_path).unlink(missing_ok=True)
        job.result_path = ""
        job.save(update_fields=["result_path", "updated_at"])
    record("scan.file_deleted", request=request, target=job)
    messages.success(request, "Scan file deleted; history was retained.")
    return redirect("jobs")


@login_required
@require_POST
def scan_rename(request, pk):
    job = get_object_or_404(ScanJob, pk=pk)
    if not _can_manage_job(request.user, job):
        raise PermissionDenied
    title = request.POST.get("title", "").strip()
    if not title or len(title) > 255:
        messages.error(request, "Enter a scan name up to 255 characters.")
    else:
        job.title = title
        job.save(update_fields=["title", "updated_at"])
        record("scan.renamed", request=request, target=job)
        messages.success(request, "Scan renamed.")
    return redirect("jobs")


@login_required
def scan_preview(request, pk):
    job = get_object_or_404(ScanJob, pk=pk)
    if not _can_manage_job(request.user, job) or not job.result_path:
        raise PermissionDenied
    path = Path(job.result_path)
    if not path.is_file() or path.suffix.lower() == ".zip":
        raise Http404
    record("scan.previewed", request=request, target=job)
    return FileResponse(path.open("rb"), as_attachment=False, filename=path.name)


@admin_required
@require_POST
def printer_command(request, pk, command):
    printer = get_object_or_404(PrinterEndpoint, pk=pk)
    if command not in {"enable", "disable", "accept", "reject", "test", "refresh"}:
        raise Http404
    kind = "refresh_printer" if command == "refresh" else "printer_command"
    payload = {"printer_id": printer.pk}
    if kind == "printer_command":
        payload["command"] = command
    Task.enqueue(kind, **payload)
    record(f"printer.{command}", request=request, target=printer)
    messages.success(request, f"Printer {command} requested.")
    return redirect("device_detail", pk=printer.device_id)


@admin_required
@require_POST
def printer_defaults(request, pk):
    printer = get_object_or_404(PrinterEndpoint, pk=pk)
    form = QueueDefaultsForm(request.POST)
    if form.is_valid():
        Task.enqueue("printer_command", printer_id=printer.pk, command="defaults", options=form.cups_options())
        record("printer.defaults", request=request, target=printer, detail=form.cups_options())
        messages.success(request, "Printer defaults queued for update.")
    else:
        messages.error(request, "Printer defaults were invalid.")
    return redirect("device_detail", pk=printer.device_id)


@admin_required
def audit_log(request):
    return render(request, "manager/audit.html", {"events": AuditEvent.objects.select_related("actor")[:500]})


@admin_required
def user_list(request):
    return render(request, "manager/users.html", {"users": User.objects.prefetch_related("groups").order_by("username")})


@admin_required
def user_create(request):
    form = ManagedUserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        user.groups.add(Group.objects.get(name=form.cleaned_data["role"]))
        record("user.created", request=request, target=user, detail={"role": form.cleaned_data["role"]})
        messages.success(request, "User created.")
        return redirect("user_list")
    return render(request, "manager/form_page.html", {"form": form, "title": "Add user", "submit_label": "Create user"})


@admin_required
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    was_admin = user.groups.filter(name=ROLE_ADMIN).exists()
    form = ManagedUserEditForm(request.POST or None, instance=user)
    if request.method == "POST" and form.is_valid():
        removing_last_admin = was_admin and (form.cleaned_data["role"] != ROLE_ADMIN or not form.cleaned_data["is_active"])
        other_admin_exists = User.objects.filter(is_active=True, groups__name=ROLE_ADMIN).exclude(pk=user.pk).exists()
        if removing_last_admin and not other_admin_exists:
            form.add_error("role", "Create another active administrator before demoting or disabling the last administrator")
        else:
            user = form.save()
            user.groups.clear()
            user.groups.add(Group.objects.get(name=form.cleaned_data["role"]))
            record("user.updated", request=request, target=user, detail={"role": form.cleaned_data["role"]})
            messages.success(request, "User updated.")
            return redirect("user_list")
    return render(request, "manager/form_page.html", {"form": form, "title": f"Edit {user.username}", "submit_label": "Save changes"})


@admin_required
def user_password(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = SetPasswordForm(user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        record("user.password_reset", request=request, target=user)
        messages.success(request, "Password reset.")
        return redirect("user_list")
    return render(request, "manager/form_page.html", {"form": form, "title": f"Reset password for {user.username}", "submit_label": "Reset password"})


@admin_required
@require_POST
def user_unlock(request, pk):
    from axes.utils import reset

    user = get_object_or_404(User, pk=pk)
    reset(username=user.username)
    record("user.unlocked", request=request, target=user)
    messages.success(request, "Login lockout cleared.")
    return redirect("user_list")


@login_required
def password_change(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        record("auth.password_changed", request=request)
        messages.success(request, "Password changed.")
        return redirect("dashboard")
    return render(request, "manager/form_page.html", {"form": form, "title": "Change password", "submit_label": "Change password"})


@admin_required
def system_settings(request):
    defaults = {
        "site_name": "Printer Manager", "time_zone": "UTC", "session_timeout_minutes": 480,
        "discovery_seconds": 12, "scan_timeout_minutes": 15,
        "office_conversion_timeout_seconds": 120, "task_retry_limit": 3,
        "print_retention_hours": 24, "scan_retention_days": 7, "max_upload_mb": 100,
    }
    stored = dict(AppSetting.objects.filter(key__in=defaults).values_list("key", "value"))
    initial = {key: stored.get(key, value) for key, value in defaults.items()}
    form = SettingsForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        for key, value in form.cleaned_data.items():
            AppSetting.objects.update_or_create(key=key, defaults={"value": str(value)})
        record("settings.updated", request=request, detail=form.cleaned_data)
        messages.success(request, "Settings saved and applied. Existing jobs keep the limits assigned when they were submitted.")
        return redirect("system_settings")
    return render(request, "manager/form_page.html", {"form": form, "title": "System settings", "submit_label": "Save settings"})
