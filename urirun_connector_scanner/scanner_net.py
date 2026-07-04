# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
"""Scanner-service networking helpers for the host dashboard.

LAN host detection, public base-URL construction, QR-code PNG generation, self-signed TLS
certs, and phone-scanner URL probing. Extracted from host_dashboard.py as a self-contained
leaf module (zero host_dashboard dependencies); re-exported there for backward compatibility.
"""
from __future__ import annotations

import os
import socket
import ssl
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_QR_BOX_SIZE = 12


def _lan_host() -> str:
    configured = os.environ.get("URIRUN_DASHBOARD_PUBLIC_HOST")
    if configured:
        return configured
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            host = sock.getsockname()[0]
            if host and not host.startswith("127."):
                return host
    except OSError:
        pass
    try:
        host = socket.gethostbyname(socket.gethostname())
        if host and not host.startswith("127."):
            return host
    except OSError:
        pass
    return "127.0.0.1"

def _url_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host

def _public_base_url(scheme: str, host: str, port: int) -> str:
    explicit = os.environ.get("URIRUN_DASHBOARD_PUBLIC_URL")
    if explicit:
        return explicit.rstrip("/")
    bind_host = (host or "127.0.0.1").strip("[]")
    if bind_host in {"", "0.0.0.0", "::"}:
        public_host = _lan_host()
    else:
        public_host = bind_host
    return f"{scheme}://{_url_host(public_host)}:{port}"

def _scanner_autonomy_params() -> dict[str, str]:
    return {
        "autostart": os.environ.get("URIRUN_PHONE_SCANNER_AUTOSTART", "1"),
        "auto": os.environ.get("URIRUN_PHONE_SCANNER_AUTO", "1"),
        "best": os.environ.get("URIRUN_PHONE_SCANNER_BEST", "1"),
        "count": os.environ.get("URIRUN_PHONE_SCANNER_BEST_COUNT", "6"),
        "minScore": os.environ.get("URIRUN_PHONE_SCANNER_MIN_SCORE", "45"),
        "interval": os.environ.get("URIRUN_PHONE_SCANNER_INTERVAL", "3"),
    }

def _scanner_page_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in _scanner_autonomy_params().items():
        query.setdefault(key, value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/scanner", urlencode(query), parts.fragment))

def _write_qr_png(url: str, path: Path) -> None:
    import qrcode

    path.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=_QR_BOX_SIZE,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    image.save(path)

def _ensure_tls_cert(cert: str, key: str) -> tuple[str, str]:
    cert_path = Path(cert).expanduser()
    key_path = Path(key).expanduser()
    if cert_path.is_file() and key_path.is_file():
        return str(cert_path), str(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key_path),
            "-out", str(cert_path),
            "-days", "365",
            "-subj", "/CN=urirun-dashboard.local",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return str(cert_path), str(key_path)

def _probe_scanner_url(url: str, timeout: float = 1.5) -> bool:
    import urllib.request

    try:
        context = ssl._create_unverified_context() if url.startswith("https://") else None
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            return 200 <= int(response.status) < 500
    except Exception:  # noqa: BLE001
        return False

def _phone_scanner_url(port: int, *, scheme: str | None = None) -> str:
    scanner_scheme = (scheme or os.environ.get("URIRUN_PHONE_SCANNER_SCHEME", "https")).strip() or "https"
    return _scanner_page_url(f"{scanner_scheme}://{_url_host(_lan_host())}:{int(port)}/scanner")

def _phone_scanner_external_status(port: int, *, timeout: float = 0.35) -> dict:
    primary_scheme = os.environ.get("URIRUN_PHONE_SCANNER_SCHEME", "https").strip().lower() or "https"
    schemes = [primary_scheme]
    if os.environ.get("URIRUN_PHONE_SCANNER_PROBE_BOTH", "1").lower() in {"1", "true", "yes", "on"}:
        fallback = "http" if primary_scheme == "https" else "https"
        schemes.append(fallback)

    seen: set[str] = set()
    primary_url = _phone_scanner_url(port, scheme=primary_scheme)
    for scheme in schemes:
        if scheme in seen:
            continue
        seen.add(scheme)
        url = _phone_scanner_url(port, scheme=scheme)
        if _probe_scanner_url(url, timeout=timeout):
            return {"status": "external-running", "reachable": True, "url": url}
    return {"status": "stopped", "reachable": False, "url": primary_url}
