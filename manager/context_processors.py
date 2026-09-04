from django.conf import settings

from .models import AppSetting
from .security import role_for


def application_context(request):
    site_name = AppSetting.objects.filter(key="site_name").values_list("value", flat=True).first() or "Printer Manager"
    return {
        "pm_role": role_for(request.user),
        "pm_https": settings.HTTPS_ENABLED,
        "pm_version": "1.0.0",
        "pm_site_name": site_name,
    }
