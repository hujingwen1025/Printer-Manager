from datetime import date, datetime

from django.core.exceptions import ObjectDoesNotExist

from .models import AuditEvent


SENSITIVE_KEY_PARTS = ("password", "secret", "token", "credential", "authorization", "cookie", "csrf")


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",", 1)[0].strip() if forwarded else request.META.get("REMOTE_ADDR")) or None


def _safe(value, key=""):
    if any(part in key.lower() for part in SENSITIVE_KEY_PARTS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _target_snapshot(target):
    if not target:
        return {}
    snapshot = {"model": target.__class__.__name__, "id": str(target.pk)}
    for field in ("name", "username", "address", "queue_name", "uri", "protocol", "sane_name",
                  "state", "status", "enabled", "accepting_jobs", "queue_enabled", "cups_job_id",
                  "output_format", "page_count", "size_bytes", "error", "created_at", "completed_at"):
        if hasattr(target, field):
            snapshot[field] = _safe(getattr(target, field), field)
    def related(name):
        try:
            return getattr(target, name, None)
        except ObjectDoesNotExist:
            return None

    owner = related("owner")
    if owner:
        snapshot["owner"] = owner.username
    device = related("device")
    if device:
        snapshot["device"] = {"id": str(device.pk), "name": device.name, "address": device.address}
    printer = related("printer")
    if printer:
        snapshot["printer"] = {"id": str(printer.pk), "device": printer.device.name, "queue": printer.queue_name}
    scanner = related("scanner")
    if scanner:
        snapshot["scanner"] = {"id": str(scanner.pk), "device": scanner.device.name,
                               "protocol": scanner.protocol, "uri": scanner.uri, "sane_name": scanner.sane_name}
    for field in ("options", "capabilities", "default_options"):
        value = getattr(target, field, None)
        if value:
            snapshot[field] = _safe(value, field)
    return snapshot


def record(action, *, request=None, actor=None, target=None, detail=None):
    if target:
        target_type = target.__class__.__name__
        target_id = str(target.pk)
    else:
        target_type = target_id = ""
    expanded_detail = {"schema_version": 2, "event": _safe(detail or {})}
    target_detail = _target_snapshot(target)
    if target_detail:
        expanded_detail["target"] = target_detail
    if request:
        expanded_detail["request"] = {
            "method": request.method,
            "path": request.path,
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
        }
    return AuditEvent.objects.create(
        actor=actor or (request.user if request and request.user.is_authenticated else None),
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=expanded_detail,
        ip_address=client_ip(request) if request else None,
    )
