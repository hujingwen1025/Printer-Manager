import ipaddress
import re
import socket
import subprocess
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


class ScannerDriverError(ValueError):
    pass


def _private_address(value):
    try:
        address = ipaddress.ip_address(socket.gethostbyname(value))
    except (OSError, ValueError) as exc:
        raise ScannerDriverError("The scanner address could not be resolved") from exc
    if not (address.is_private or address.is_link_local):
        raise ScannerDriverError("Scanner endpoints must use a private or link-local address")
    return str(address)


@dataclass(frozen=True)
class ScannerDriver:
    id: str
    label: str
    sane_backend: str
    airscan_protocol: str = ""

    def normalize_endpoint(self, value, address=""):
        value = (value or "").strip()
        if self.id == "hpaio":
            return self._normalize_hpaio(value, address)
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ScannerDriverError("Enter a valid HTTP or HTTPS scanner endpoint")
        _private_address(parsed.hostname)
        return value

    def _normalize_hpaio(self, value, address):
        if value.startswith("hpaio:"):
            parsed = urlparse(value)
            query = parse_qs(parsed.query)
            host = (query.get("ip") or query.get("hostname") or [""])[0]
            if not host:
                raise ScannerDriverError("The HPAIO identifier must contain an IP address or hostname")
            _private_address(host)
            return value
        host = value or address
        host = _private_address(host)
        return make_hpaio_uri(host)

    def config_entry(self, scanner):
        if not self.airscan_protocol:
            return None
        safe_name = scanner.device.name.replace('"', "'").replace("\n", " ")
        return f'"{safe_name}" = {scanner.uri}, {self.airscan_protocol}'

    def resolve(self, scanner, found_scanners):
        if self.id == "hpaio":
            return scanner.uri
        name = scanner.device.name.lower()
        for found in found_scanners:
            if not found["sane_name"].startswith("airscan:"):
                continue
            if name in found["description"].lower() or name in found["sane_name"].lower():
                return found["sane_name"]
        raise ScannerDriverError("Scanner is not reachable through the configured AirScan endpoint")


def make_hpaio_uri(address, timeout=8):
    try:
        result = subprocess.run(
            ["hp-makeuri", "-s", address], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise ScannerDriverError("The HP HPLIP scanner driver is not installed in this image") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScannerDriverError("HPLIP timed out while checking this address") from exc
    combined = "\n".join((result.stdout or "", result.stderr or ""))
    match = re.search(r"(?m)^\s*(hpaio:\S+)", combined)
    if result.returncode or not match:
        message = (result.stderr or result.stdout or "HPLIP did not identify a supported scanner").strip()
        raise ScannerDriverError(message[-500:])
    return match.group(1).strip()


DRIVERS = {
    "airscan-escl": ScannerDriver("airscan-escl", "AirScan / eSCL", "airscan", "escl"),
    "airscan-wsd": ScannerDriver("airscan-wsd", "AirScan / WSD", "airscan", "wsd"),
    "hpaio": ScannerDriver("hpaio", "HP HPLIP / HPAIO", "hpaio"),
}

LEGACY_IDS = {"escl": "airscan-escl", "wsd": "airscan-wsd"}


def get_scanner_driver(driver_id):
    driver_id = LEGACY_IDS.get(driver_id, driver_id)
    try:
        return DRIVERS[driver_id]
    except KeyError as exc:
        raise ScannerDriverError(f"Scanner driver '{driver_id}' is not installed") from exc


def scanner_driver_choices():
    return [(driver.id, driver.label) for driver in DRIVERS.values()]
