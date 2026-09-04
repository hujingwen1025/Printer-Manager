import asyncio
import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse


SERVICE_TYPES = ("_ipp._tcp.local.", "_ipps._tcp.local.", "_uscan._tcp.local.", "_uscans._tcp.local.")


def validate_private_cidr(value):
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise ValueError("Enter a valid network CIDR, such as 192.168.1.0/24") from exc
    private_ranges = tuple(ipaddress.ip_network(item) for item in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    if network.version != 4 or not any(network.subnet_of(private) for private in private_ranges):
        raise ValueError("Only private IPv4 networks may be scanned")
    if network.prefixlen < 24:
        raise ValueError("LAN scans are limited to /24 or smaller networks")
    return network


class _Listener:
    def __init__(self):
        self.results = []

    def add_service(self, zeroconf, service_type, name):
        info = zeroconf.get_service_info(service_type, name, timeout=2000)
        if not info:
            return
        addresses = info.parsed_addresses()
        def decode(value):
            if value is None:
                return ""
            return value.decode(errors="replace") if isinstance(value, bytes) else str(value)
        props = {decode(k): decode(v) for k, v in info.properties.items()}
        secure = service_type.startswith(("_ipps", "_uscans"))
        is_scanner = "uscan" in service_type
        host = addresses[0] if addresses else info.server.rstrip(".")
        if is_scanner:
            path = props.get("rs", props.get("path", "eSCL")).lstrip("/")
            uri = f"{'https' if secure else 'http'}://{host}:{info.port}/{path}"
            endpoint_type = "scanner"
            protocol = "escl"
        else:
            path = props.get("rp", "ipp/print").lstrip("/")
            uri = f"{'ipps' if secure else 'ipp'}://{host}:{info.port}/{path}"
            endpoint_type = "printer"
            protocol = "ipps" if secure else "ipp"
        self.results.append({
            "name": props.get("ty") or name.split(".", 1)[0], "address": host,
            "uri": uri, "endpoint_type": endpoint_type, "protocol": protocol,
            "service": service_type.rstrip("."), "properties": props,
        })

    update_service = add_service

    def remove_service(self, zeroconf, service_type, name):
        return None


def discover_mdns(seconds=12):
    from zeroconf import ServiceBrowser, Zeroconf

    listener = _Listener()
    zc = Zeroconf()
    browsers = [ServiceBrowser(zc, service_type, listener) for service_type in SERVICE_TYPES]
    try:
        import time
        time.sleep(seconds)
    finally:
        for browser in browsers:
            browser.cancel()
        zc.close()
    unique = {}
    for result in listener.results:
        unique[(result["endpoint_type"], result["uri"])] = result
    return list(unique.values())


def _tcp_open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_escl(host, secure=False):
    scheme, port = ("https", 443) if secure else ("http", 80)
    uri = f"{scheme}://{host}:{port}/eSCL"
    context = ssl._create_unverified_context() if secure else None
    try:
        req = urllib.request.Request(uri + "/ScannerCapabilities", headers={"User-Agent": "PrinterManager/1.0"})
        with urllib.request.urlopen(req, timeout=1.5, context=context) as response:
            body = response.read(2048).lower()
            if response.status == 200 and b"scanner" in body:
                return uri
    except (OSError, urllib.error.URLError, TimeoutError):
        pass
    return None


def _probe_host(address):
    host = str(address)
    results = []
    if _tcp_open(host, 631):
        results.append({"name": f"IPP printer at {host}", "address": host, "uri": f"ipp://{host}:631/ipp/print", "endpoint_type": "printer", "protocol": "ipp"})
    for secure in (False, True):
        uri = _probe_escl(host, secure)
        if uri:
            results.append({"name": f"AirScan scanner at {host}", "address": host, "uri": uri, "endpoint_type": "scanner", "protocol": "escl"})
    return results


def discover_lan(cidr):
    network = validate_private_cidr(cidr)
    results = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        for found in pool.map(_probe_host, network.hosts()):
            results.extend(found)
    results.extend(_discover_wsd_scanners(network))
    return results


def _discover_wsd_scanners(network, timeout=2.0):
    """Issue one bounded WS-Discovery probe and keep only endpoints inside the requested CIDR."""
    message_id = uuid.uuid4()
    probe = f'''<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dpws="http://schemas.xmlsoap.org/ws/2006/02/devprof">
<e:Header><w:MessageID>uuid:{message_id}</w:MessageID><w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dpws:Device</d:Types></d:Probe></e:Body></e:Envelope>'''.encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    found = {}
    try:
        sock.sendto(probe, ("239.255.255.250", 3702))
        while True:
            try:
                payload, sender = sock.recvfrom(65535)
            except socket.timeout:
                break
            text = payload.decode(errors="ignore")
            matches = re.findall(r"<(?:\w+:)?XAddrs>(.*?)</(?:\w+:)?XAddrs>", text, re.IGNORECASE | re.DOTALL)
            for match in matches:
                for uri in match.split():
                    parsed = urlparse(uri)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                        continue
                    try:
                        address = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))
                    except (OSError, ValueError):
                        continue
                    if address in network:
                        found[uri] = {"name": f"WSD scanner at {address}", "address": str(address), "uri": uri,
                                      "endpoint_type": "scanner", "protocol": "wsd"}
    except OSError:
        return []
    finally:
        sock.close()
    return list(found.values())


def validate_endpoint_uri(uri, endpoint_type):
    parsed = urlparse(uri)
    allowed = {"ipp", "ipps"} if endpoint_type == "printer" else {"http", "https"}
    if parsed.scheme not in allowed or not parsed.hostname:
        raise ValueError(f"Enter a valid {'IPP/IPPS' if endpoint_type == 'printer' else 'HTTP/HTTPS'} endpoint")
    host = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))
    if not (host.is_private or host.is_link_local):
        raise ValueError("Device endpoints must resolve to a private or link-local address")
    return parsed
