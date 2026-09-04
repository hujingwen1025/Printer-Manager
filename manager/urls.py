from django.urls import path

from . import views


urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("devices/", views.device_list, name="device_list"),
    path("devices/add/", views.device_create, name="device_create"),
    path("devices/<uuid:pk>/", views.device_detail, name="device_detail"),
    path("devices/<uuid:pk>/edit/", views.device_edit, name="device_edit"),
    path("devices/<uuid:pk>/delete/", views.device_delete, name="device_delete"),
    path("discovery/", views.discovery, name="discovery"),
    path("discovery/<uuid:pk>/", views.discovery_detail, name="discovery_detail"),
    path("printers/<int:printer_id>/print/", views.print_submit, name="print_submit"),
    path("printers/<int:pk>/command/<str:command>/", views.printer_command, name="printer_command"),
    path("printers/<int:pk>/defaults/", views.printer_defaults, name="printer_defaults"),
    path("scanners/<int:scanner_id>/scan/", views.scan_submit, name="scan_submit"),
    path("scanners/<int:pk>/revalidate/", views.scanner_revalidate, name="scanner_revalidate"),
    path("jobs/", views.jobs, name="jobs"),
    path("jobs/print/<uuid:pk>/<str:command>/", views.print_job_command, name="print_job_command"),
    path("jobs/scan/<uuid:pk>/cancel/", views.scan_job_cancel, name="scan_job_cancel"),
    path("jobs/scan/<uuid:pk>/download/", views.scan_download, name="scan_download"),
    path("jobs/scan/<uuid:pk>/preview/", views.scan_preview, name="scan_preview"),
    path("jobs/scan/<uuid:pk>/rename/", views.scan_rename, name="scan_rename"),
    path("jobs/scan/<uuid:pk>/delete/", views.scan_delete, name="scan_delete"),
    path("audit/", views.audit_log, name="audit"),
    path("users/", views.user_list, name="user_list"),
    path("users/add/", views.user_create, name="user_create"),
    path("users/<int:pk>/edit/", views.user_edit, name="user_edit"),
    path("users/<int:pk>/password/", views.user_password, name="user_password"),
    path("users/<int:pk>/unlock/", views.user_unlock, name="user_unlock"),
    path("account/password/", views.password_change, name="password_change"),
    path("settings/", views.system_settings, name="system_settings"),
]
