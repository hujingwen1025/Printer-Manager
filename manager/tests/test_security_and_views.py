import base64
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import RequestFactory
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from manager.audit import record
from manager.models import AppSetting, AuditEvent, Device, DiscoveryRun, PrinterEndpoint, PrintJob, ScanJob, ScannerEndpoint, Task
from manager.services.sane_service import ScanFailure
from manager.task_processor import handle_print, handle_scan
from manager.security import ROLES


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class BaseCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.groups = {name: Group.objects.create(name=name) for name in ROLES}
        cls.admin = User.objects.create_user("admin", password="correct-horse-battery-staple")
        cls.admin.groups.add(cls.groups["admin"])
        cls.operator = User.objects.create_user("operator", password="correct-horse-battery-staple")
        cls.operator.groups.add(cls.groups["operator"])
        cls.other = User.objects.create_user("other", password="correct-horse-battery-staple")
        cls.other.groups.add(cls.groups["operator"])
        cls.viewer = User.objects.create_user("viewer", password="correct-horse-battery-staple")
        cls.viewer.groups.add(cls.groups["viewer"])
        cls.device = Device.objects.create(name="Office MFP", address="192.168.1.20")
        cls.printer = PrinterEndpoint.objects.create(device=cls.device, uri="ipp://192.168.1.20/ipp/print", queue_name="office-mfp")
        cls.scanner = ScannerEndpoint.objects.create(device=cls.device, uri="http://192.168.1.20/eSCL", protocol="escl")


class AuthenticationTests(BaseCase):
    def test_health_is_public_but_application_redirects(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_viewer_cannot_reach_admin_or_operator_actions(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("discovery")).status_code, 403)
        self.assertEqual(self.client.get(reverse("print_submit", args=[self.printer.pk])).status_code, 403)

    def test_operator_cannot_manage_another_users_job(self):
        job = PrintJob.objects.create(owner=self.other, printer=self.printer, title="Private", original_name="private.pdf",
                                      source_path="/tmp/not-real", mime_type="application/pdf", artifact_expires_at="2030-01-01T00:00:00Z")
        self.client.force_login(self.operator)
        response = self.client.post(reverse("print_job_command", args=[job.pk, "cancel"]))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Task.objects.filter(kind="print_job_command").exists())

    def test_viewer_job_page_redacts_titles(self):
        PrintJob.objects.create(owner=self.other, printer=self.printer, title="Confidential layoffs", original_name="secret.pdf",
                                source_path="/tmp/not-real", mime_type="application/pdf", artifact_expires_at="2030-01-01T00:00:00Z")
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("jobs"))
        self.assertContains(response, "Private document")
        self.assertNotContains(response, "Confidential layoffs")

    def test_admin_pages_render_with_configured_multifunction_device(self):
        self.client.force_login(self.admin)
        for url in (reverse("dashboard"), reverse("device_list"), reverse("device_detail", args=[self.device.pk]),
                    reverse("discovery"), reverse("jobs"), reverse("user_list"), reverse("audit"), reverse("system_settings")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_all_private_get_pages_require_login(self):
        urls = (reverse("dashboard"), reverse("device_list"), reverse("device_detail", args=[self.device.pk]),
                reverse("discovery"), reverse("jobs"), reverse("user_list"), reverse("audit"), reverse("system_settings"),
                reverse("print_submit", args=[self.printer.pk]), reverse("scan_submit", args=[self.scanner.pk]))
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response.url)


class DiscoveryTests(BaseCase):
    def test_discovery_is_explicit_and_enqueued(self):
        self.assertEqual(DiscoveryRun.objects.count(), 0)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("discovery"), {"kind": "mdns", "cidr": ""})
        self.assertEqual(response.status_code, 302)
        run = DiscoveryRun.objects.get()
        self.assertEqual(run.state, "pending")
        self.assertTrue(Task.objects.filter(kind="discovery", payload__run_id=str(run.pk)).exists())

    def test_lan_scan_rejects_public_and_large_ranges(self):
        self.client.force_login(self.admin)
        for cidr in ("8.8.8.0/24", "192.168.0.0/16", "not-a-network"):
            response = self.client.post(reverse("discovery"), {"kind": "lan", "cidr": cidr})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(DiscoveryRun.objects.count(), 0)


class JobTests(BaseCase):
    def test_operator_can_queue_supported_document(self):
        self.client.force_login(self.operator)
        upload = SimpleUploadedFile("photo.png", PNG, content_type="image/png")
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=Path(directory)):
            with patch("manager.views.inspect_document", return_value="image/png"):
                response = self.client.post(reverse("print_submit", args=[self.printer.pk]), {
                    "document": upload, "title": "Photo", "copies": 1, "mode": "Color", "resolution": "300", "output_format": "pdf",
                })
            self.assertEqual(response.status_code, 302)
            job = PrintJob.objects.get()
            self.assertEqual(job.owner, self.operator)
            self.assertTrue(Path(job.source_path).is_file())
            self.assertTrue(Task.objects.filter(kind="print", payload__print_job_id=str(job.pk)).exists())

    def test_scan_download_is_owner_or_admin_only(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "scan.pdf"
            result.write_bytes(b"scan")
            job = ScanJob.objects.create(owner=self.operator, scanner=self.scanner, title="Scan", result_path=str(result),
                                         state="complete", artifact_expires_at="2030-01-01T00:00:00Z")
            self.client.force_login(self.other)
            self.assertEqual(self.client.get(reverse("scan_download", args=[job.pk])).status_code, 403)
            self.client.force_login(self.admin)
            response = self.client.get(reverse("scan_download", args=[job.pk]))
            self.assertEqual(response.status_code, 200)

    def test_scan_owner_can_rename_and_preview_result(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "scan.pdf"
            result.write_bytes(b"%PDF-1.4\n")
            job = ScanJob.objects.create(owner=self.operator, scanner=self.scanner, title="Old name", result_path=str(result),
                                         state="complete", artifact_expires_at="2030-01-01T00:00:00Z")
            self.client.force_login(self.operator)
            response = self.client.post(reverse("scan_rename", args=[job.pk]), {"title": "New name"})
            self.assertEqual(response.status_code, 302)
            job.refresh_from_db()
            self.assertEqual(job.title, "New name")
            response = self.client.get(reverse("scan_preview", args=[job.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertIn("inline", response.headers["Content-Disposition"])

    def test_worker_normalizes_and_submits_print_job(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=Path(directory)):
            source = Path(directory) / "source.png"
            source.write_bytes(PNG)
            job = PrintJob.objects.create(owner=self.operator, printer=self.printer, title="Worker test", original_name="source.png",
                                          source_path=str(source), mime_type="image/png", artifact_expires_at="2030-01-01T00:00:00Z")
            with patch("manager.task_processor.cups_service.submit_file", return_value=42):
                handle_print(str(job.pk))
            job.refresh_from_db()
            self.assertEqual(job.state, "submitted")
            self.assertEqual(job.cups_job_id, 42)
            self.assertTrue(Path(job.normalized_path).is_file())


class UserManagementTests(BaseCase):
    def test_admin_can_create_role_scoped_user(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user_create"), {
            "username": "newviewer", "role": "viewer", "is_active": "on",
            "password1": "another-correct-horse-battery", "password2": "another-correct-horse-battery",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get(username="newviewer").groups.filter(name="viewer").exists())

    def test_last_active_admin_cannot_be_demoted(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user_edit", args=[self.admin.pk]), {
            "username": "admin", "role": "viewer", "is_active": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.groups.filter(name="admin").exists())
        self.assertContains(response, "last administrator")


class RuntimeSettingsTests(BaseCase):
    def test_admin_can_store_operational_settings_in_database(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("system_settings"), {
            "site_name": "Office Print Hub", "time_zone": "Asia/Shanghai",
            "session_timeout_minutes": 60, "discovery_seconds": 20,
            "scan_timeout_minutes": 10, "office_conversion_timeout_seconds": 90,
            "task_retry_limit": 5, "print_retention_hours": 48,
            "scan_retention_days": 14, "max_upload_mb": 250,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AppSetting.get_value("site_name", ""), "Office Print Hub")
        self.assertEqual(AppSetting.get_int("task_retry_limit", 3), 5)
        self.assertTrue(Task.objects.filter(kind="unused").count() == 0)
        self.assertEqual(Task.enqueue("unused").max_attempts, 5)

    def test_invalid_timezone_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("system_settings"), {
            "site_name": "Printer Manager", "time_zone": "Not/A_Timezone",
            "session_timeout_minutes": 60, "discovery_seconds": 12,
            "scan_timeout_minutes": 15, "office_conversion_timeout_seconds": 120,
            "task_retry_limit": 3, "print_retention_hours": 24,
            "scan_retention_days": 7, "max_upload_mb": 100,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "valid IANA timezone")


class BootstrapTests(TestCase):
    def test_initial_admin_can_be_created_from_dockhand_environment(self):
        environment = {
            "PM_ADMIN_USERNAME": "dockhand-admin",
            "PM_ADMIN_PASSWORD": "a-secure-dockhand-password",
            "PM_ADMIN_PASSWORD_FILE": "",
        }
        with patch.dict(os.environ, environment, clear=False):
            call_command("bootstrap", verbosity=0)
        user = User.objects.get(username="dockhand-admin")
        self.assertTrue(user.check_password("a-secure-dockhand-password"))
        self.assertTrue(user.groups.filter(name="admin").exists())


class AuditDetailTests(BaseCase):
    def test_audit_event_captures_request_target_and_redacts_secrets(self):
        request = RequestFactory().post("/scanners/1/scan/", HTTP_USER_AGENT="Audit test browser")
        request.user = self.operator
        event = record("scan.test", request=request, target=self.scanner,
                       detail={"options": {"resolution": "300"}, "api_token": "must-not-appear"})
        self.assertEqual(event.detail["request"]["method"], "POST")
        self.assertEqual(event.detail["target"]["device"]["name"], "Office MFP")
        self.assertEqual(event.detail["event"]["api_token"], "[redacted]")

    def test_audit_page_has_expandable_pretty_details(self):
        record("scan.test", actor=self.operator, target=self.scanner, detail={"stage": "diagnostic"})
        self.client.force_login(self.admin)
        response = self.client.get(reverse("audit"))
        self.assertContains(response, "<details", html=False)
        self.assertContains(response, "diagnostic")

    def test_scan_failure_records_sane_diagnostics(self):
        job = ScanJob.objects.create(owner=self.operator, scanner=self.scanner, title="Broken scan",
                                     options={"resolution": "300"}, output_format="pdf",
                                     artifact_expires_at="2030-01-01T00:00:00Z")
        diagnostics = {"stage": "scanimage", "return_code": 1, "stderr": "Invalid argument", "command": ["scanimage"]}
        with patch("manager.task_processor.run_scan", side_effect=ScanFailure("The scanner rejected one or more selected options", diagnostics)):
            with self.assertRaises(ScanFailure):
                handle_scan(str(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.state, "failed")
        event = AuditEvent.objects.get(action="scan.failed")
        self.assertEqual(event.detail["event"]["diagnostics"]["return_code"], 1)
        self.assertEqual(event.detail["target"]["options"]["resolution"], "300")
