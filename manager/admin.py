from django.contrib import admin

from .models import AppSetting, AuditEvent, Device, DiscoveryRun, PrinterEndpoint, PrintJob, ScanJob, ScannerEndpoint, Task


for model in (Device, PrinterEndpoint, ScannerEndpoint, DiscoveryRun, PrintJob, ScanJob, Task, AuditEvent, AppSetting):
    admin.site.register(model)
