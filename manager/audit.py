from .models import AuditEvent


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",", 1)[0].strip() if forwarded else request.META.get("REMOTE_ADDR")) or None


def record(action, *, request=None, actor=None, target=None, detail=None):
    if target:
        target_type = target.__class__.__name__
        target_id = str(target.pk)
    else:
        target_type = target_id = ""
    return AuditEvent.objects.create(
        actor=actor or (request.user if request and request.user.is_authenticated else None),
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail or {},
        ip_address=client_ip(request) if request else None,
    )
