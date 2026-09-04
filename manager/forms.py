import ipaddress

from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, SetPasswordForm, UserCreationForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError

from .models import AppSetting, Device, PrinterEndpoint, ScannerEndpoint
from .security import ROLES, ROLE_VIEWER
from .services.discovery import validate_endpoint_uri, validate_private_cidr


class LoginForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}))
    password = forms.CharField(strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))


class DiscoveryForm(forms.Form):
    kind = forms.ChoiceField(choices=(("mdns", "AirPrint / AirScan"), ("lan", "LAN scan")))
    cidr = forms.CharField(required=False, help_text="Required for LAN scans, for example 192.168.1.0/24")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") == "lan":
            if not cleaned.get("cidr"):
                self.add_error("cidr", "Enter the private network to scan")
            else:
                try:
                    validate_private_cidr(cleaned["cidr"])
                except ValueError as exc:
                    self.add_error("cidr", str(exc))
        else:
            cleaned["cidr"] = ""
        return cleaned


class DeviceForm(forms.ModelForm):
    printer_uri = forms.CharField(required=False, label="Printer IPP/IPPS URI")
    queue_name = forms.SlugField(required=False, max_length=80)
    scanner_uri = forms.URLField(required=False, label="Scanner eSCL/WSD URI")
    scanner_protocol = forms.ChoiceField(required=False, choices=ScannerEndpoint.Protocol.choices)

    class Meta:
        model = Device
        fields = ["name", "address", "location", "notes", "enabled"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            printer = PrinterEndpoint.objects.filter(device=self.instance).first()
            scanner = ScannerEndpoint.objects.filter(device=self.instance).first()
            if printer:
                self.initial.setdefault("printer_uri", printer.uri)
                self.initial.setdefault("queue_name", printer.queue_name)
            if scanner:
                self.initial.setdefault("scanner_uri", scanner.uri)
                self.initial.setdefault("scanner_protocol", scanner.protocol)

    def clean(self):
        cleaned = super().clean()
        printer_uri, scanner_uri = cleaned.get("printer_uri", "").strip(), cleaned.get("scanner_uri", "").strip()
        if not printer_uri and not scanner_uri:
            raise ValidationError("Add at least one printer or scanner endpoint")
        if printer_uri:
            if not cleaned.get("queue_name"):
                self.add_error("queue_name", "A CUPS queue name is required")
            try:
                validate_endpoint_uri(printer_uri, "printer")
            except (ValueError, OSError) as exc:
                self.add_error("printer_uri", str(exc))
            printer_matches = PrinterEndpoint.objects.filter(uri=printer_uri)
            queue_matches = PrinterEndpoint.objects.filter(queue_name=cleaned.get("queue_name", ""))
            if self.instance.pk:
                printer_matches = printer_matches.exclude(device=self.instance)
                queue_matches = queue_matches.exclude(device=self.instance)
            if printer_matches.exists():
                self.add_error("printer_uri", "This printer endpoint is already configured")
            if queue_matches.exists():
                self.add_error("queue_name", "This queue name is already in use")
        if scanner_uri:
            try:
                validate_endpoint_uri(scanner_uri, "scanner")
            except (ValueError, OSError) as exc:
                self.add_error("scanner_uri", str(exc))
            scanner_matches = ScannerEndpoint.objects.filter(uri=scanner_uri)
            if self.instance.pk:
                scanner_matches = scanner_matches.exclude(device=self.instance)
            if scanner_matches.exists():
                self.add_error("scanner_uri", "This scanner endpoint is already configured")
        return cleaned


class PrintJobForm(forms.Form):
    document = forms.FileField()
    title = forms.CharField(max_length=255, required=False)
    copies = forms.IntegerField(min_value=1, max_value=999, initial=1)
    page_ranges = forms.CharField(required=False, help_text="Example: 1-3,5")
    media = forms.CharField(required=False)
    sides = forms.ChoiceField(required=False, choices=(("", "Printer default"), ("one-sided", "One-sided"), ("two-sided-long-edge", "Two-sided, long edge"), ("two-sided-short-edge", "Two-sided, short edge")))
    color = forms.ChoiceField(required=False, choices=(("", "Printer default"), ("auto", "Automatic"), ("color", "Color"), ("monochrome", "Monochrome")))
    quality = forms.ChoiceField(required=False, choices=(("", "Printer default"), ("3", "Draft"), ("4", "Normal"), ("5", "High")))
    orientation = forms.ChoiceField(required=False, choices=(("", "Automatic"), ("3", "Portrait"), ("4", "Landscape")))
    fit_to_page = forms.BooleanField(required=False, initial=True)

    def __init__(self, *args, printer=None, **kwargs):
        super().__init__(*args, **kwargs)
        capabilities = printer.capabilities if printer else {}
        media = capabilities.get("media-supported", [])
        if media:
            self.fields["media"] = forms.ChoiceField(required=False, choices=[("", "Printer default")] + [(str(v), str(v)) for v in media])
        supported_sides = capabilities.get("sides-supported", [])
        if supported_sides:
            labels = dict(self.fields["sides"].choices)
            self.fields["sides"].choices = [("", "Printer default")] + [(v, labels.get(v, v)) for v in supported_sides]
        supported_colors = capabilities.get("print-color-mode-supported", [])
        if supported_colors:
            labels = dict(self.fields["color"].choices)
            self.fields["color"].choices = [("", "Printer default")] + [(v, labels.get(v, str(v).title())) for v in supported_colors]

    def clean_document(self):
        upload = self.cleaned_data["document"]
        limit = AppSetting.get_int("max_upload_mb", 100, minimum=1, maximum=1024) * 1024 * 1024
        if upload.size > limit:
            raise ValidationError(f"File exceeds the {limit // 1024 // 1024} MB limit")
        return upload

    def options(self):
        data = self.cleaned_data
        return {
            "copies": data["copies"], "page-ranges": data.get("page_ranges", ""), "media": data.get("media", ""),
            "sides": data.get("sides", ""), "print-color-mode": data.get("color", ""),
            "print-quality": data.get("quality", ""), "orientation-requested": data.get("orientation", ""),
            "fit-to-page": "true" if data.get("fit_to_page") else "false",
        }


class ScanJobForm(forms.Form):
    title = forms.CharField(max_length=255, required=False)
    source = forms.CharField(max_length=100, required=False, initial="Flatbed")
    mode = forms.ChoiceField(choices=(("Color", "Color"), ("Gray", "Grayscale"), ("Lineart", "Line art")))
    resolution = forms.ChoiceField(choices=(("75", "75 dpi"), ("150", "150 dpi"), ("300", "300 dpi"), ("600", "600 dpi")), initial="300")
    page_width = forms.DecimalField(required=False, min_value=1, max_value=500, decimal_places=1, label="Width (mm)")
    page_height = forms.DecimalField(required=False, min_value=1, max_value=500, decimal_places=1, label="Height (mm)")
    output_format = forms.ChoiceField(choices=(("pdf", "PDF"), ("png", "PNG / ZIP"), ("jpeg", "JPEG / ZIP")))

    def __init__(self, *args, scanner=None, **kwargs):
        super().__init__(*args, **kwargs)
        capabilities = scanner.capabilities if scanner else {}
        if capabilities.get("sources"):
            self.fields["source"] = forms.ChoiceField(choices=[(v, v) for v in capabilities["sources"]])
        if capabilities.get("modes"):
            self.fields["mode"].choices = [(v, v) for v in capabilities["modes"]]
        resolutions = [str(v) for v in capabilities.get("resolutions", []) if str(v).strip().isdigit()]
        if resolutions:
            self.fields["resolution"].choices = [(v, f"{v} dpi") for v in resolutions]

    def options(self):
        return {key: str(self.cleaned_data[key]) for key in ("source", "mode", "resolution", "page_width", "page_height") if self.cleaned_data.get(key) not in (None, "")}


class QueueDefaultsForm(forms.Form):
    media = forms.CharField(required=False)
    sides = forms.ChoiceField(required=False, choices=(("", "Device default"), ("one-sided", "One-sided"), ("two-sided-long-edge", "Two-sided, long edge"), ("two-sided-short-edge", "Two-sided, short edge")))
    print_color_mode = forms.ChoiceField(required=False, choices=(("", "Device default"), ("auto", "Automatic"), ("color", "Color"), ("monochrome", "Monochrome")))
    print_quality = forms.ChoiceField(required=False, choices=(("", "Device default"), ("3", "Draft"), ("4", "Normal"), ("5", "High")))
    orientation_requested = forms.ChoiceField(required=False, choices=(("", "Device default"), ("3", "Portrait"), ("4", "Landscape")))

    def cups_options(self):
        return {key.replace("_", "-"): value for key, value in self.cleaned_data.items() if value}


class ManagedUserForm(UserCreationForm):
    role = forms.ChoiceField(choices=[(r, r.title()) for r in ROLES], initial=ROLE_VIEWER)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email", "role", "is_active")


class ManagedUserEditForm(forms.ModelForm):
    role = forms.ChoiceField(choices=[(r, r.title()) for r in ROLES])

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email", "role", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            group = self.instance.groups.filter(name__in=ROLES).first()
            self.fields["role"].initial = group.name if group else ROLE_VIEWER


class SettingsForm(forms.Form):
    site_name = forms.CharField(max_length=80, initial="Printer Manager", help_text="Name shown in the browser and navigation.")
    time_zone = forms.CharField(max_length=64, initial="UTC", help_text="IANA timezone, for example Asia/Shanghai or Europe/London.")
    session_timeout_minutes = forms.IntegerField(min_value=5, max_value=43200, initial=480, help_text="Idle login lifetime. Applies to active sessions on their next request.")
    discovery_seconds = forms.IntegerField(min_value=3, max_value=60, initial=12, help_text="How long an explicitly started AirPrint/AirScan discovery listens.")
    scan_timeout_minutes = forms.IntegerField(min_value=1, max_value=120, initial=15, help_text="Maximum runtime for one scan job.")
    office_conversion_timeout_seconds = forms.IntegerField(min_value=30, max_value=600, initial=120, help_text="Maximum runtime for LibreOffice document conversion.")
    task_retry_limit = forms.IntegerField(min_value=1, max_value=10, initial=3, help_text="Maximum attempts assigned to newly submitted background tasks.")
    print_retention_hours = forms.IntegerField(min_value=1, max_value=720, initial=24, help_text="How long uploaded print files remain available for retry.")
    scan_retention_days = forms.IntegerField(min_value=1, max_value=365, initial=7, help_text="How long completed scan files remain available.")
    max_upload_mb = forms.IntegerField(min_value=1, max_value=1024, initial=100, help_text="Maximum accepted document size.")

    def clean_time_zone(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        value = self.cleaned_data["time_zone"].strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError("Enter a valid IANA timezone such as Asia/Shanghai")
        return value
