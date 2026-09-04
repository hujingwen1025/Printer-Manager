import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class Device(models.Model):
    class Status(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        ONLINE = "online", "Online"
        OFFLINE = "offline", "Offline"
        WARNING = "warning", "Warning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=255)
    location = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    enabled = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNKNOWN)
    status_message = models.CharField(max_length=255, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PrinterEndpoint(models.Model):
    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="printer")
    uri = models.CharField(max_length=500, unique=True)
    queue_name = models.SlugField(max_length=80, unique=True)
    capabilities = models.JSONField(default=dict, blank=True)
    default_options = models.JSONField(default=dict, blank=True)
    accepting_jobs = models.BooleanField(default=True)
    queue_enabled = models.BooleanField(default=True)
    queued_jobs = models.PositiveIntegerField(default=0)
    supplies = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"{self.device.name} printer"


class ScannerEndpoint(models.Model):
    class Protocol(models.TextChoices):
        AIRSCAN_ESCL = "airscan-escl", "AirScan / eSCL"
        AIRSCAN_WSD = "airscan-wsd", "AirScan / WSD"
        HPAIO = "hpaio", "HP HPLIP / HPAIO"

    class ValidationState(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="scanner")
    uri = models.CharField(max_length=500, unique=True)
    protocol = models.CharField(max_length=24, choices=Protocol.choices, default=Protocol.AIRSCAN_ESCL)
    sane_name = models.CharField(max_length=180, blank=True)
    capabilities = models.JSONField(default=dict, blank=True)
    validation_state = models.CharField(max_length=12, choices=ValidationState.choices, default=ValidationState.UNKNOWN)
    validation_message = models.CharField(max_length=500, blank=True)
    validated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.device.name} scanner"


class DiscoveryRun(models.Model):
    class Kind(models.TextChoices):
        MDNS = "mdns", "AirPrint / AirScan"
        LAN = "lan", "LAN scan"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    cidr = models.CharField(max_length=64, blank=True)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="discovery_runs")
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    results = models.JSONField(default=list, blank=True)
    error = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class PrintJob(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        CONVERTING = "converting", "Converting"
        SUBMITTED = "submitted", "Submitted"
        HELD = "held", "Held"
        PRINTING = "printing", "Printing"
        COMPLETE = "complete", "Complete"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="print_jobs")
    printer = models.ForeignKey(PrinterEndpoint, on_delete=models.PROTECT, related_name="jobs")
    title = models.CharField(max_length=255)
    original_name = models.CharField(max_length=255)
    source_path = models.CharField(max_length=500)
    normalized_path = models.CharField(max_length=500, blank=True)
    mime_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField(default=0)
    options = models.JSONField(default=dict, blank=True)
    cups_job_id = models.PositiveIntegerField(null=True, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    error = models.CharField(max_length=500, blank=True)
    cancel_requested = models.BooleanField(default=False)
    artifact_expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class ScanJob(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        SCANNING = "scanning", "Scanning"
        PROCESSING = "processing", "Processing"
        COMPLETE = "complete", "Complete"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="scan_jobs")
    scanner = models.ForeignKey(ScannerEndpoint, on_delete=models.PROTECT, related_name="jobs")
    title = models.CharField(max_length=255)
    options = models.JSONField(default=dict, blank=True)
    result_path = models.CharField(max_length=500, blank=True)
    output_format = models.CharField(max_length=12, default="pdf")
    page_count = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    error = models.CharField(max_length=500, blank=True)
    cancel_requested = models.BooleanField(default=False)
    artifact_expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Task(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=50)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    run_after = models.DateTimeField(default=timezone.now)
    lease_owner = models.CharField(max_length=100, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["state", "run_after"])]

    @classmethod
    def enqueue(cls, kind, **payload):
        retry_limit = AppSetting.get_int("task_retry_limit", 3, minimum=1, maximum=10)
        return cls.objects.create(kind=kind, payload=payload, max_attempts=retry_limit)


class AuditEvent(models.Model):
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events")
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=100, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class AppSetting(models.Model):
    key = models.CharField(max_length=100, primary_key=True)
    value = models.CharField(max_length=500)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_value(cls, key, default):
        value = cls.objects.filter(key=key).values_list("value", flat=True).first()
        return default if value in (None, "") else value

    @classmethod
    def get_int(cls, key, default, *, minimum=None, maximum=None):
        try:
            value = int(cls.get_value(key, default))
        except (TypeError, ValueError):
            value = int(default)
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value


def print_expiration():
    return timezone.now() + timedelta(hours=AppSetting.get_int("print_retention_hours", 24, minimum=1, maximum=720))


def scan_expiration():
    return timezone.now() + timedelta(days=AppSetting.get_int("scan_retention_days", 7, minimum=1, maximum=365))
