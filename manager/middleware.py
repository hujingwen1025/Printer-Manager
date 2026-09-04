from .audit import record


class RuntimeSettingsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.utils import timezone

        from .models import AppSetting

        timezone.activate(AppSetting.get_value("time_zone", "UTC"))
        if getattr(request, "user", None) and request.user.is_authenticated:
            minutes = AppSetting.get_int("session_timeout_minutes", 480, minimum=5, maximum=43200)
            request.session.set_expiry(minutes * 60)
        return self.get_response(request)


class LoginAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        before = request.session.get("_pm_authenticated_user")
        response = self.get_response(request)
        after = request.user.pk if getattr(request, "user", None) and request.user.is_authenticated else None
        if after and before != after:
            record("auth.login", request=request)
            request.session["_pm_authenticated_user"] = after
        elif before and not after:
            request.session.pop("_pm_authenticated_user", None)
        return response
