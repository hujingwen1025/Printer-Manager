import tempfile
import zipfile
import socket
import ipaddress
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from manager.models import Device, PrintJob, PrinterEndpoint, ScanJob, ScannerEndpoint
from manager.services.discovery import _discover_wsd_scanners, validate_private_cidr, validate_endpoint_uri
from manager.services.documents import inspect_document, normalize_for_print
from manager.task_processor import cleanup_expired


class DiscoveryValidationTests(TestCase):
    def test_private_slash_24_or_smaller_is_allowed(self):
        self.assertEqual(str(validate_private_cidr("192.168.12.0/24")), "192.168.12.0/24")
        self.assertEqual(str(validate_private_cidr("10.0.0.8/30")), "10.0.0.8/30")

    def test_public_ipv6_and_large_networks_are_rejected(self):
        for value in ("8.8.8.0/24", "fd00::/120", "10.0.0.0/8"):
            with self.assertRaises(ValueError):
                validate_private_cidr(value)

    @patch("manager.services.discovery.socket.gethostbyname", return_value="192.168.1.10")
    def test_endpoint_protocol_is_restricted(self, _resolve):
        validate_endpoint_uri("ipp://printer.local/ipp/print", "printer")
        with self.assertRaises(ValueError):
            validate_endpoint_uri("socket://printer.local:9100", "printer")

    def test_wsd_results_are_filtered_to_requested_network(self):
        payload = b'<ProbeMatches><ProbeMatch><XAddrs>http://192.168.1.40:5357/DeviceService http://10.0.0.4:5357/DeviceService</XAddrs></ProbeMatch></ProbeMatches>'

        class FakeSocket:
            calls = 0
            def settimeout(self, value): pass
            def sendto(self, value, target): pass
            def recvfrom(self, size):
                self.calls += 1
                if self.calls == 1:
                    return payload, ("192.168.1.40", 3702)
                raise socket.timeout
            def close(self): pass

        def resolve(host):
            return host

        with patch("manager.services.discovery.socket.socket", return_value=FakeSocket()), patch("manager.services.discovery.socket.gethostbyname", side_effect=resolve):
            results = _discover_wsd_scanners(ipaddress.ip_network("192.168.1.0/24"), timeout=0.01)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["address"], "192.168.1.40")


class DocumentTests(TestCase):
    def test_png_is_inspected_and_normalized_to_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "misleading.docx"
            Image.new("RGB", (10, 10), "white").save(source, "PNG")
            self.assertEqual(inspect_document(source), "image/png")
            output = normalize_for_print(source, "image/png", Path(directory) / "out")
            self.assertTrue(output.read_bytes().startswith(b"%PDF"))

    def test_unknown_file_is_rejected(self):
        with tempfile.NamedTemporaryFile() as source:
            source.write(b"not a supported document")
            source.flush()
            with self.assertRaises(ValueError):
                inspect_document(source.name)

    def test_macro_bearing_office_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unsafe.docm"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("word/document.xml", "<document/>")
                archive.writestr("word/vbaProject.bin", b"macro")
            with self.assertRaisesMessage(ValueError, "Macro-bearing"):
                inspect_document(source)


class RetentionTests(TestCase):
    def setUp(self):
        group = Group.objects.create(name="operator")
        self.user = User.objects.create_user("owner", password="a-long-enough-password")
        self.user.groups.add(group)
        self.device = Device.objects.create(name="MFP", address="192.168.1.4")
        self.printer = PrinterEndpoint.objects.create(device=self.device, uri="ipp://192.168.1.4/ipp/print", queue_name="mfp")
        self.scanner = ScannerEndpoint.objects.create(device=self.device, uri="http://192.168.1.4/eSCL")

    def test_cleanup_removes_expired_files_but_keeps_history(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=Path(directory)):
            print_source = Path(directory) / "source.pdf"
            print_source.write_bytes(b"print")
            scan_dir = Path(directory) / "scans" / "result"
            scan_dir.mkdir(parents=True)
            scan_result = scan_dir / "scan.pdf"
            scan_result.write_bytes(b"scan")
            expired = timezone.now() - timedelta(minutes=1)
            print_job = PrintJob.objects.create(owner=self.user, printer=self.printer, title="Print", original_name="print.pdf",
                                                source_path=str(print_source), mime_type="application/pdf", artifact_expires_at=expired)
            scan_job = ScanJob.objects.create(owner=self.user, scanner=self.scanner, title="Scan", result_path=str(scan_result),
                                              state="complete", artifact_expires_at=expired)
            cleanup_expired()
            print_job.refresh_from_db()
            scan_job.refresh_from_db()
            self.assertFalse(print_source.exists())
            self.assertEqual(print_job.source_path, "")
            self.assertEqual(scan_job.result_path, "")
            self.assertFalse(scan_result.exists())
            self.assertTrue(PrintJob.objects.filter(pk=print_job.pk).exists())
            self.assertTrue(ScanJob.objects.filter(pk=scan_job.pk).exists())
