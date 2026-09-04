import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

from django.conf import settings
from PIL import Image

from manager.models import AppSetting, ScannerEndpoint
from manager.services.scanner_drivers import ScannerDriverError, get_scanner_driver


class ScanFailure(RuntimeError):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class ScanCancelled(InterruptedError):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def _clean_output(value, limit=4000):
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value or "").strip()
    return value[-limit:]


def sane_environment():
    config_dir = Path(settings.DATA_DIR) / "sane"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dll = config_dir / "dll.conf"
    dll.write_text("airscan\nhpaio\n", encoding="utf-8")
    os.chmod(dll, 0o600)
    return {**os.environ, "SANE_CONFIG_DIR": str(config_dir)}


def regenerate_config():
    config_dir = Path(settings.DATA_DIR) / "sane"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    dll = config_dir / "dll.conf"
    dll.write_text("airscan\nhpaio\n", encoding="utf-8")
    os.chmod(dll, 0o600)
    lines = ["[options]", "discovery = disable", "", "[devices]"]
    for scanner in ScannerEndpoint.objects.select_related("device").order_by("device__name"):
        entry = get_scanner_driver(scanner.protocol).config_entry(scanner)
        if entry:
            lines.append(entry)
    path = config_dir / "airscan.conf"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def list_scanners():
    regenerate_config()
    result = subprocess.run(["scanimage", "-L"], capture_output=True, text=True, timeout=30, env=sane_environment())
    if result.returncode and "No scanners were identified" not in result.stderr:
        raise RuntimeError(result.stderr.strip() or "Unable to list scanners")
    found = []
    for line in result.stdout.splitlines():
        match = re.match(r"device `([^']+)' is a (.+)", line)
        if match:
            found.append({"sane_name": match.group(1), "description": match.group(2)})
    return found


def resolve_scanner(scanner):
    driver = get_scanner_driver(scanner.protocol)
    found_scanners = [] if driver.id == "hpaio" else list_scanners()
    try:
        sane_name = driver.resolve(scanner, found_scanners)
    except ScannerDriverError as exc:
        raise ScanFailure(str(exc), {
            "stage": "resolve", "configured_device": scanner.device.name,
            "configured_uri": scanner.uri, "configured_driver": driver.id,
            "available_scanners": found_scanners,
        }) from exc
    if scanner.sane_name != sane_name:
        scanner.sane_name = sane_name
        scanner.save(update_fields=["sane_name"])
    return sane_name


def fetch_capabilities(scanner):
    sane_name = resolve_scanner(scanner)
    command = ["scanimage", "-d", sane_name, "--all-options"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, env=sane_environment())
    except subprocess.TimeoutExpired as exc:
        raise ScanFailure("Scanner capability validation timed out", {
            "stage": "capabilities", "command": command, "sane_device": sane_name,
            "configured_driver": scanner.protocol, "timeout_seconds": 30,
        }) from exc
    if result.returncode:
        diagnostics = {
            "stage": "capabilities", "command": command, "sane_device": sane_name,
            "configured_driver": scanner.protocol, "return_code": result.returncode,
            "stdout": _clean_output(result.stdout), "stderr": _clean_output(result.stderr),
        }
        raise ScanFailure(_friendly_error(result.stderr), diagnostics)
    text = result.stdout
    capabilities = {
        "raw": text[-20000:],
        "sources": _choices(text, "source"),
        "modes": _choices(text, "mode"),
        "resolutions": _choices(text, "resolution"),
        "duplex": "duplex" in text.lower(),
    }
    scanner.capabilities = capabilities
    scanner.save(update_fields=["capabilities"])
    return capabilities


def _choices(text, option):
    match = re.search(rf"--{re.escape(option)}\s+([^\n]+)", text, re.IGNORECASE)
    if not match:
        return []
    return [x.strip(' |[]"') for x in re.split(r"\||,", match.group(1)) if x.strip(' |[]"')]


def run_scan(job):
    started = time.monotonic()
    scanner_name = resolve_scanner(job.scanner)
    output_dir = Path(settings.MEDIA_ROOT) / "scans" / str(job.id)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pattern = output_dir / "page-%03d.png"
    opts = job.options
    command = ["scanimage", "-d", scanner_name, "--format=png", f"--batch={pattern}"]
    mappings = {"source": "--source", "mode": "--mode", "resolution": "--resolution", "page_width": "-x", "page_height": "-y"}
    for key, flag in mappings.items():
        value = opts.get(key)
        if value:
            command.extend([flag, str(value)])
    if not str(opts.get("source", "")).lower().startswith(("adf", "automatic")):
        command.append("--batch-count=1")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=sane_environment())
    timeout_seconds = AppSetting.get_int("scan_timeout_minutes", 15, minimum=1, maximum=120) * 60
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None:
        job.refresh_from_db(fields=["cancel_requested"])
        if job.cancel_requested:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise ScanCancelled("Scan cancelled", {"stage": "scanning", "command": command,
                                                    "duration_seconds": round(time.monotonic() - started, 2)})
        if time.monotonic() >= deadline:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise ScanFailure("The scan exceeded the configured time limit", {
                "stage": "scanning", "command": command, "timeout_seconds": timeout_seconds,
                "duration_seconds": round(time.monotonic() - started, 2),
            })
        time.sleep(0.5)
    stdout, stderr = process.communicate()
    pages = sorted(output_dir.glob("page-*.png"))
    diagnostics = {
        "stage": "scanimage", "command": command, "sane_device": scanner_name,
        "configured_driver": job.scanner.protocol, "configured_endpoint": job.scanner.uri,
        "return_code": process.returncode, "stdout": _clean_output(stdout),
        "stderr": _clean_output(stderr), "pages_received": len(pages),
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    if job.cancel_requested:
        raise ScanCancelled("Scan cancelled", diagnostics)
    if process.returncode and not pages:
        raise ScanFailure(_friendly_error(stderr), diagnostics)
    if not pages:
        raise ScanFailure("The scanner returned no pages", diagnostics)
    requested = job.output_format
    if requested == "pdf":
        target = output_dir / "scan.pdf"
        images = [Image.open(page).convert("RGB") for page in pages]
        try:
            images[0].save(target, "PDF", save_all=True, append_images=images[1:], resolution=150)
        finally:
            for image in images:
                image.close()
    elif len(pages) == 1:
        target = output_dir / f"scan.{requested}"
        with Image.open(pages[0]) as image:
            image.convert("RGB" if requested == "jpeg" else image.mode).save(target, "JPEG" if requested == "jpeg" else "PNG")
    else:
        target = output_dir / "scan-images.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for page in pages:
                archive.write(page, page.name)
    for page in pages:
        if page != target:
            page.unlink(missing_ok=True)
    os.chmod(target, 0o600)
    diagnostics.update({"result_format": requested, "result_name": target.name})
    return target, len(pages), diagnostics


def _friendly_error(stderr):
    value = stderr.lower()
    mappings = (("device busy", "Scanner is busy"), ("no documents", "The document feeder is empty"),
                ("out of documents", "The document feeder is empty"), ("cover", "The scanner cover is open"),
                ("access denied", "Scanner access was denied"), ("invalid argument", "The scanner rejected one or more selected options"),
                ("plugin", "The selected scanner requires a vendor plug-in that is not installed"),
                ("device i/o", "Scanner connection or driver initialization failed"),
                ("i/o error", "Scanner is unreachable"), ("connection refused", "Scanner connection was refused"),
                ("timed out", "Scanner connection timed out"))
    for needle, message in mappings:
        if needle in value:
            return message
    return "The scan failed; an administrator can expand the scan.failed audit event for diagnostic details"
