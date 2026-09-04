import tempfile
import zipfile
import socket
import ipaddress
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image

from manager.models import Device, PrintJob, PrinterEndpoint, ScanJob, ScannerEndpoint
from manager.services.discovery import _discover_wsd_scanners, _wsd_metadata, validate_private_cidr, validate_endpoint_uri
from manager.services.documents import inspect_document, normalize_for_print
from manager.services.sane_service import regenerate_config
from manager.services.scanner_drivers import ScannerDriverError, get_scanner_driver, make_hpaio_uri
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

    def test_wsd_printer_only_response_is_not_reported_as_scanner(self):
        payload = b'<ProbeMatches><ProbeMatch><Types>PrintDeviceType</Types><EndpointReference><Address>urn:uuid:printer</Address></EndpointReference><XAddrs>http://192.168.1.40:5357/DeviceService</XAddrs></ProbeMatch></ProbeMatches>'

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
        self.assertEqual(results, [])

    def test_wsd_resolves_only_hosted_scanner_service(self):
        payload = b'<ProbeMatches><ProbeMatch><Types>ScanDeviceType PrintDeviceType</Types><EndpointReference><Address>urn:uuid:mfp</Address></EndpointReference><XAddrs>http://192.168.1.40:5357/DeviceService</XAddrs></ProbeMatch></ProbeMatches>'
        metadata = b'''<Metadata><ThisModel><Manufacturer>Example</Manufacturer><ModelName>MFP</ModelName></ThisModel><Relationship><Hosted><Types>ScannerServiceType</Types><EndpointReference><Address>http://192.168.1.40:5358/WSDScanner</Address></EndpointReference></Hosted><Hosted><Types>PrinterServiceType</Types><EndpointReference><Address>http://192.168.1.40:5358/WSDPrinter</Address></EndpointReference></Hosted></Relationship></Metadata>'''

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

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return metadata

        with patch("manager.services.discovery.socket.socket", return_value=FakeSocket()), \
             patch("manager.services.discovery.socket.gethostbyname", side_effect=lambda host: host), \
             patch("manager.services.discovery.urllib.request.urlopen", return_value=FakeResponse()):
            results = _discover_wsd_scanners(ipaddress.ip_network("192.168.1.0/24"), timeout=0.01)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["uri"], "http://192.168.1.40:5358/WSDScanner")
        self.assertEqual(results[0]["protocol"], "airscan-wsd")

    def test_wsd_metadata_rejects_malformed_oversized_and_outside_endpoints(self):
        class FakeResponse:
            status = 200
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return self.payload

        network = ipaddress.ip_network("192.168.1.0/24")
        cases = (
            b"<not-closed>",
            b"x" * 262145,
            b"<Metadata><Hosted><Types>ScannerServiceType</Types><Address>http://10.0.0.8/WSDScanner</Address></Hosted></Metadata>",
        )
        for payload in cases:
            with self.subTest(size=len(payload)), patch(
                "manager.services.discovery.urllib.request.urlopen", return_value=FakeResponse(payload)
            ):
                self.assertEqual(_wsd_metadata("http://192.168.1.40/device", "urn:uuid:mfp", network, 0.01), [])

    def test_hpaio_uri_is_generated_without_a_shell(self):
        completed = subprocess.CompletedProcess(["hp-makeuri"], 0, "hpaio:/net/HP_MFP?ip=192.168.1.40\n", "")
        with patch("manager.services.scanner_drivers.subprocess.run", return_value=completed) as run:
            self.assertEqual(make_hpaio_uri("192.168.1.40"), "hpaio:/net/HP_MFP?ip=192.168.1.40")
        self.assertEqual(run.call_args.args[0], ["hp-makeuri", "-s", "192.168.1.40"])

    def test_hpaio_rejects_public_endpoint(self):
        with patch("manager.services.scanner_drivers.socket.gethostbyname", return_value="8.8.8.8"):
            with self.assertRaises(ScannerDriverError):
                get_scanner_driver("hpaio").normalize_endpoint("hpaio:/net/HP?ip=8.8.8.8")

    def test_hpaio_reports_missing_driver_tool(self):
        with patch("manager.services.scanner_drivers.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesMessage(ScannerDriverError, "not installed"):
                make_hpaio_uri("192.168.1.40")

    def test_sane_configuration_enables_only_vetted_backends(self):
        device = Device.objects.create(name="Configured scanner", address="192.168.1.40")
        ScannerEndpoint.objects.create(device=device, uri="http://192.168.1.40/eSCL", protocol="airscan-escl")
        with tempfile.TemporaryDirectory() as directory, override_settings(DATA_DIR=Path(directory)):
            regenerate_config()
            self.assertIn("airscan\nhpaio\n", (Path(directory) / "sane" / "dll.conf").read_text())
            airscan = (Path(directory) / "sane" / "airscan.conf").read_text()
            self.assertIn("http://192.168.1.40/eSCL, escl", airscan)


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
