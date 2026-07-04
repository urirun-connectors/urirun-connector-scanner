from __future__ import annotations

import os

from urirun_connector_scanner.scanner_net import (
    _public_base_url,
    _scanner_autonomy_params,
    _scanner_page_url,
    _url_host,
    _phone_scanner_url,
    _phone_scanner_external_status,
)


# ─── _url_host ───────────────────────────────────────────────────────────────

def test_url_host_plain_ipv4():
    assert _url_host("192.168.1.10") == "192.168.1.10"


def test_url_host_wraps_ipv6():
    assert _url_host("::1") == "[::1]"
    assert _url_host("2001:db8::1") == "[2001:db8::1]"


def test_url_host_already_bracketed_ipv6():
    assert _url_host("[::1]") == "[::1]"


def test_url_host_hostname():
    assert _url_host("mydevice.local") == "mydevice.local"


# ─── _public_base_url ────────────────────────────────────────────────────────

def test_public_base_url_uses_explicit_env(monkeypatch):
    monkeypatch.setenv("URIRUN_DASHBOARD_PUBLIC_URL", "https://example.com")
    assert _public_base_url("http", "0.0.0.0", 8194) == "https://example.com"


def test_public_base_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("URIRUN_DASHBOARD_PUBLIC_URL", "https://example.com/")
    assert _public_base_url("http", "0.0.0.0", 8194) == "https://example.com"


def test_public_base_url_bind_all_uses_lan_host(monkeypatch):
    monkeypatch.delenv("URIRUN_DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.setenv("URIRUN_DASHBOARD_PUBLIC_HOST", "10.0.0.5")
    url = _public_base_url("http", "0.0.0.0", 8194)
    assert url == "http://10.0.0.5:8194"


def test_public_base_url_explicit_bind_host(monkeypatch):
    monkeypatch.delenv("URIRUN_DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.delenv("URIRUN_DASHBOARD_PUBLIC_HOST", raising=False)
    url = _public_base_url("https", "192.168.1.50", 8765)
    assert url == "https://192.168.1.50:8765"


def test_public_base_url_ipv6_bind_host(monkeypatch):
    monkeypatch.delenv("URIRUN_DASHBOARD_PUBLIC_URL", raising=False)
    monkeypatch.delenv("URIRUN_DASHBOARD_PUBLIC_HOST", raising=False)
    url = _public_base_url("http", "::1", 9000)
    assert url == "http://[::1]:9000"


# ─── _scanner_autonomy_params ────────────────────────────────────────────────

def test_scanner_autonomy_params_defaults(monkeypatch):
    for key in ("URIRUN_PHONE_SCANNER_AUTOSTART", "URIRUN_PHONE_SCANNER_AUTO",
                "URIRUN_PHONE_SCANNER_BEST", "URIRUN_PHONE_SCANNER_BEST_COUNT",
                "URIRUN_PHONE_SCANNER_MIN_SCORE", "URIRUN_PHONE_SCANNER_INTERVAL"):
        monkeypatch.delenv(key, raising=False)
    params = _scanner_autonomy_params()
    assert params["autostart"] == "1"
    assert params["auto"] == "1"
    assert params["best"] == "1"
    assert params["count"] == "6"
    assert params["minScore"] == "45"
    assert params["interval"] == "3"


def test_scanner_autonomy_params_from_env(monkeypatch):
    monkeypatch.setenv("URIRUN_PHONE_SCANNER_BEST_COUNT", "3")
    monkeypatch.setenv("URIRUN_PHONE_SCANNER_INTERVAL", "5")
    params = _scanner_autonomy_params()
    assert params["count"] == "3"
    assert params["interval"] == "5"


# ─── _scanner_page_url ───────────────────────────────────────────────────────

def test_scanner_page_url_adds_default_path(monkeypatch):
    for key in ("URIRUN_PHONE_SCANNER_AUTOSTART", "URIRUN_PHONE_SCANNER_AUTO",
                "URIRUN_PHONE_SCANNER_BEST", "URIRUN_PHONE_SCANNER_BEST_COUNT",
                "URIRUN_PHONE_SCANNER_MIN_SCORE", "URIRUN_PHONE_SCANNER_INTERVAL"):
        monkeypatch.delenv(key, raising=False)
    url = _scanner_page_url("https://192.168.1.5:8196")
    assert url.startswith("https://192.168.1.5:8196/scanner")
    assert "autostart=1" in url
    assert "interval=3" in url


def test_scanner_page_url_preserves_existing_query_params(monkeypatch):
    for key in ("URIRUN_PHONE_SCANNER_AUTOSTART", "URIRUN_PHONE_SCANNER_AUTO",
                "URIRUN_PHONE_SCANNER_BEST", "URIRUN_PHONE_SCANNER_BEST_COUNT",
                "URIRUN_PHONE_SCANNER_MIN_SCORE", "URIRUN_PHONE_SCANNER_INTERVAL"):
        monkeypatch.delenv(key, raising=False)
    url = _scanner_page_url("https://host:8196/scanner?autostart=0")
    # setdefault means existing value wins
    assert "autostart=0" in url
    assert "autostart=1" not in url


# ─── _phone_scanner_url ──────────────────────────────────────────────────────

def test_phone_scanner_url_uses_https_by_default(monkeypatch):
    monkeypatch.delenv("URIRUN_PHONE_SCANNER_SCHEME", raising=False)
    monkeypatch.setenv("URIRUN_DASHBOARD_PUBLIC_HOST", "10.0.0.9")
    url = _phone_scanner_url(8196)
    assert url.startswith("https://")
    assert "8196" in url


def test_phone_scanner_url_respects_scheme_override(monkeypatch):
    monkeypatch.setenv("URIRUN_DASHBOARD_PUBLIC_HOST", "10.0.0.9")
    url = _phone_scanner_url(8196, scheme="http")
    assert url.startswith("http://")


# ─── _phone_scanner_external_status ─────────────────────────────────────────

def test_external_status_unreachable_returns_ok_false(monkeypatch):
    monkeypatch.setenv("URIRUN_DASHBOARD_PUBLIC_HOST", "127.0.0.1")
    monkeypatch.delenv("URIRUN_PHONE_SCANNER_SCHEME", raising=False)
    result = _phone_scanner_external_status(19999, timeout=0.05)
    assert "reachable" in result
    assert result["reachable"] is False
