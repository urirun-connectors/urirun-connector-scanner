from __future__ import annotations

import hashlib
import os
import time
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .scanner_net import (
    _ensure_tls_cert,
    _lan_host,
    _phone_scanner_external_status,
    _phone_scanner_url,
    _probe_scanner_url,
    _public_base_url,
    _scanner_page_url,
    _url_host,
    _write_qr_png,
)
if TYPE_CHECKING:
    pass

_SERVICE_LOCK: threading.Lock = threading.Lock()
_SERVICE_SERVERS: dict[str, ThreadingHTTPServer] = {}
_SERVICE_THREADS: dict[str, threading.Thread] = {}


def phone_scanner_service_id(bind_host: str, port: int) -> str:
    return f"https://{bind_host}:{port}"


def startup_phone_qr(
    project: str,
    db: "str | None",
    *,
    scheme: str,
    host: str,
    port: int,
    qr_url: "str | None" = None,
    content_prefix: str = "Phone scanner QR ready",
    host_db_fn: "Callable[[], Any]",
    preview_url_fn: "Callable[[str, str], str]",
    chat_message_fn: "Callable[..., dict]",
    add_chat_message_fn: "Callable[[str | None, dict], dict | None]",
) -> dict:
    base_url = _public_base_url(scheme, host, port)
    scanner_url = _scanner_page_url((qr_url or os.environ.get("URIRUN_DASHBOARD_QR_URL") or f"{base_url}/scanner").strip())
    digest = hashlib.sha256(scanner_url.encode("utf-8")).hexdigest()
    root = Path(os.environ.get("URIRUN_DASHBOARD_QR_DIR", "~/.urirun/host-dashboard/qr")).expanduser()
    path = root / f"phone-scanner-{digest[:12]}.png"
    bind_host = (host or "").strip("[]")
    reachable_from_phone = bind_host not in {"127.0.0.1", "localhost", "::1"}
    secure_camera_context = scanner_url.startswith("https://") or scanner_url.startswith("http://127.0.0.1") or scanner_url.startswith("http://localhost")
    meta = {
        "url": scanner_url,
        "dashboardUrl": f"{base_url}/",
        "scannerUrl": scanner_url,
        "bindHost": host,
        "port": port,
        "scheme": scheme,
        "reachableFromPhone": reachable_from_phone,
        "secureCameraContext": secure_camera_context,
    }
    uri = f"dashboard://host/qr/{digest[:16]}"
    attachment = None
    try:
        _write_qr_png(scanner_url, path)
        artifact = host_db_fn().register_artifact(db, "dashboard-qr", uri, str(path), meta)
        attachment = {
            "kind": "qr-code",
            "path": str(path),
            "uri": uri,
            "previewUrl": preview_url_fn(str(path), project),
            "meta": meta,
        }
    except Exception as exc:  # noqa: BLE001 - QR is helpful, not required for serving.
        artifact = {"kind": "dashboard-qr", "uri": uri, "path": None, "meta": {**meta, "error": str(exc)}}

    content = f"{content_prefix}: {scanner_url}"
    if not reachable_from_phone:
        content += " (dashboard is bound to loopback; use --host 0.0.0.0 for phone access)"
    elif not secure_camera_context:
        content += " (phone camera usually needs HTTPS)"
    message = chat_message_fn(
        "system",
        content,
        detail={"uri": uri, "url": scanner_url, "selectedTargets": ["service:phone-scanner"], "artifact": artifact, "metadata": meta},
        attachments=[attachment] if attachment else [],
    )
    add_chat_message_fn(db, message)
    return {"ok": True, "uri": uri, "url": scanner_url, "artifact": artifact, "message": message}


def phone_node_qr(
    project: str,
    db: "str | None",
    payload: dict,
    *,
    host_db_fn: "Callable[[], Any]",
    preview_url_fn: "Callable[[str, str], str]",
    chat_message_fn: "Callable[..., dict]",
    add_chat_message_fn: "Callable[[str | None, dict], dict | None]",
    ensure_android_node_fn: "Callable[[dict], dict] | None" = None,
) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    try:
        port = int(payload.get("port") or os.environ.get("URIRUN_ANDROID_NODE_PORT") or 8195)
    except (TypeError, ValueError):
        port = 8195
    host = _lan_host()
    setup_url = str(payload.get("url") or f"http://{host}:{port}/").strip()
    digest = hashlib.sha256(setup_url.encode("utf-8")).hexdigest()
    root = Path(os.environ.get("URIRUN_DASHBOARD_QR_DIR", "~/.urirun/host-dashboard/qr")).expanduser()
    path = root / f"smartphone-node-{digest[:12]}.png"
    reachable_from_phone = not host.startswith("127.")
    service_reachable = _probe_scanner_url(setup_url, timeout=1.5)
    service_start = None
    auto_start = str(payload.get("autoStart", "1")).strip().lower() not in {"0", "false", "no", "off"}
    if not service_reachable and auto_start and ensure_android_node_fn is not None:
        try:
            service_start = ensure_android_node_fn({**payload, "port": port, "url": setup_url})
        except Exception as exc:  # noqa: BLE001 - QR should still explain the failure
            service_start = {"ok": False, "error": str(exc)}
        if isinstance(service_start, dict) and service_start.get("ok"):
            for _ in range(8):
                service_reachable = _probe_scanner_url(setup_url, timeout=2.0)
                if service_reachable:
                    break
                time.sleep(0.5)
    meta = {
        "url": setup_url,
        "port": port,
        "host": host,
        "kind": "smartphone-node",
        "reachableFromPhone": reachable_from_phone,
        "serviceReachable": service_reachable,
    }
    if service_start is not None:
        meta["serviceStart"] = service_start
    uri = f"dashboard://host/qr/smartphone-node/{digest[:16]}"
    preview_url = None
    try:
        _write_qr_png(setup_url, path)
        artifact = host_db_fn().register_artifact(db, "dashboard-qr", uri, str(path), meta)
        preview_url = preview_url_fn(str(path), project)
        attachment = {"kind": "qr-code", "path": str(path), "uri": uri, "previewUrl": preview_url, "meta": meta}
    except Exception as exc:  # noqa: BLE001 - QR is helpful, not required
        artifact = {"kind": "dashboard-qr", "uri": uri, "path": None, "meta": {**meta, "error": str(exc)}}
        attachment = None
    content = f"Smartphone node QR ready: {setup_url}"
    if not service_reachable:
        content += " (android-node service is not reachable; start it with: urirun-android-node serve)"
    elif service_start and not service_start.get("alreadyRunning"):
        content += " (android-node service started)"
    message = chat_message_fn(
        "system", content,
        detail={"uri": uri, "url": setup_url, "selectedTargets": ["service:android-node"], "artifact": artifact, "metadata": meta},
        attachments=[attachment] if attachment else [],
    )
    add_chat_message_fn(db, message)
    return {
            "ok": True, "uri": uri, "url": setup_url, "previewUrl": preview_url,
            "port": port, "reachableFromPhone": reachable_from_phone, "serviceStart": service_start,
            "serviceReachable": service_reachable, "artifact": artifact,
        }


def ensure_phone_scanner_service(
    project: str,
    db: "str | None",
    config: "str | None" = None,
    node_urls: "list[str] | None" = None,
    token: "str | None" = None,
    identity: "str | None" = None,
    *,
    host: "str | None" = None,
    port: "int | None" = None,
    tls_cert: "str | None" = None,
    tls_key: "str | None" = None,
    serve_fn: "Callable[..., ThreadingHTTPServer]",
    startup_phone_qr_fn: "Callable[..., dict]",
    host_db_fn: "Callable[[], Any]",
) -> dict:
    bind_host = host or os.environ.get("URIRUN_PHONE_SCANNER_HOST", "0.0.0.0")
    scanner_port = int(port or os.environ.get("URIRUN_PHONE_SCANNER_PORT", "8196"))
    cert = tls_cert or os.environ.get("URIRUN_PHONE_SCANNER_TLS_CERT", "~/.urirun/certs/urirun-dashboard.crt")
    key = tls_key or os.environ.get("URIRUN_PHONE_SCANNER_TLS_KEY", "~/.urirun/certs/urirun-dashboard.key")
    cert, key = _ensure_tls_cert(cert, key)
    scanner_url = _scanner_page_url(f"https://{_url_host(_lan_host())}:{scanner_port}/scanner")
    service_id = f"https://{bind_host}:{scanner_port}"

    with _SERVICE_LOCK:
        server = _SERVICE_SERVERS.get(service_id)
        thread = _SERVICE_THREADS.get(service_id)
        if server is not None and thread is not None and thread.is_alive():
            status = "already-running"
        elif _probe_scanner_url(scanner_url):
            status = "external-running"
        else:
            server = serve_fn(
                project=project,
                db=db,
                config=config,
                host=bind_host,
                port=scanner_port,
                node_urls=node_urls,
                token=token,
                identity=identity,
                tls_cert=cert,
                tls_key=key,
                startup_qr=False,
            )
            thread = threading.Thread(target=server.serve_forever, name=f"urirun-phone-scanner-{scanner_port}", daemon=True)
            thread.start()
            _SERVICE_SERVERS[service_id] = server
            _SERVICE_THREADS[service_id] = thread
            status = "started"

    qr = startup_phone_qr_fn(
        project,
        db,
        scheme="https",
        host=bind_host,
        port=scanner_port,
        qr_url=scanner_url,
        content_prefix="Phone scanner service ready",
    )
    meta = {
        "status": status,
        "service": "phone-scanner",
        "url": scanner_url,
        "bindHost": bind_host,
        "hostIp": _lan_host(),
        "port": scanner_port,
        "tlsCert": cert,
    }
    try:
        host_db_fn().add_log(db, "service", "phone-scanner", meta)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, **meta, "qr": qr, "message": qr.get("message")}


def _pop_scanner_service(service_id: str) -> tuple:
    """Pop the registered server and thread for service_id under the service lock."""
    with _SERVICE_LOCK:
        server = _SERVICE_SERVERS.pop(service_id, None)
        thread = _SERVICE_THREADS.pop(service_id, None)
    return server, thread


def _handle_inprocess_restart(
    project: str,
    db: "str | None",
    config: "str | None",
    node_urls: "list[str] | None",
    token: "str | None",
    identity: "str | None",
    bind_host: str,
    scanner_port: int,
    server: "ThreadingHTTPServer | None",
    thread: "threading.Thread | None",
    ensure_fn: "Callable[..., dict]",
) -> "dict | None":
    """Schedule an in-process restart when the service is currently alive; return None otherwise."""
    if server is None or thread is None or not thread.is_alive():
        return None

    def _restart() -> None:
        try:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        ensure_fn(
            project,
            db,
            config,
            node_urls=node_urls,
            token=token,
            identity=identity,
            host=bind_host,
            port=scanner_port,
        )

    threading.Thread(target=_restart, name=f"urirun-phone-scanner-restart-{scanner_port}", daemon=True).start()
    return {
        "ok": True,
        "scheduled": True,
        "manager": "in-process",
        "service": "phone-scanner",
        "port": scanner_port,
        "url": _phone_scanner_url(scanner_port),
    }


def _handle_port_free_restart(
    scanner_port: int,
    force_port_kill: bool,
    meta: dict,
    free_port_fn: "Callable[..., dict]",
    ensure_fn: "Callable[..., dict]",
    project: str,
    db: "str | None",
    config: "str | None" = None,
    node_urls: "list[str] | None" = None,
    token: "str | None" = None,
    identity: "str | None" = None,
    bind_host: str = "0.0.0.0",
) -> "dict | None":
    """Free the port if occupied; return a result dict if handled, None if no holders."""
    replaced = free_port_fn(scanner_port, force=force_port_kill)
    if not replaced.get("holders"):
        return None
    if not replaced.get("ok") or replaced.get("remaining"):
        return {
            "ok": False,
            **meta,
            "replace": replaced,
            "reason": "port is owned by a process that was not safely replaceable; use forcePortKill only in a controlled environment",
        }
    started = ensure_fn(
        project,
        db,
        config,
        node_urls=node_urls,
        token=token,
        identity=identity,
        host=bind_host,
        port=scanner_port,
    )
    return {"ok": True, "manager": "port-replace", "restart": True, "replace": replaced, **started}


def _handle_external_status_restart(
    scanner_port: int,
    meta: dict,
    external_status_fn: "Callable[..., dict] | None",
    ensure_fn: "Callable[..., dict]",
    project: str,
    db: "str | None",
    config: "str | None",
    node_urls: "list[str] | None",
    token: "str | None",
    identity: "str | None",
    bind_host: str,
) -> dict:
    """Check external reachability; start the service if stopped, or report unmanaged."""
    _ext_status = external_status_fn if external_status_fn is not None else _phone_scanner_external_status
    status = _ext_status(scanner_port)
    if not status.get("reachable"):
        started = ensure_fn(
            project,
            db,
            config,
            node_urls=node_urls,
            token=token,
            identity=identity,
            host=bind_host,
            port=scanner_port,
        )
        return {"ok": True, "manager": "start-if-stopped", "restart": False, **started}

    return {
        "ok": False,
        **meta,
        "status": status,
        "reason": "scanner is reachable but is not managed by this dashboard process; configure a supervisor restart command",
    }


def _resolve_scanner_bind_params(payload: dict) -> tuple:
    """Resolve bind host and port from payload or environment."""
    bind_host = str(payload.get("host") or os.environ.get("URIRUN_PHONE_SCANNER_HOST", "0.0.0.0"))
    scanner_port = int(payload.get("port") or os.environ.get("URIRUN_PHONE_SCANNER_PORT", "8196"))
    return bind_host, scanner_port


def restart_phone_scanner_service(
    project: str,
    db: "str | None",
    config: "str | None" = None,
    node_urls: "list[str] | None" = None,
    token: "str | None" = None,
    identity: "str | None" = None,
    payload: "dict | None" = None,
    *,
    ensure_fn: "Callable[..., dict]",
    free_port_fn: "Callable[..., dict]",
    external_status_fn: "Callable[..., dict] | None" = None,
    service_restart_argv_fn: "Callable[..., tuple]",
    schedule_restart_fn: "Callable[..., dict]",
) -> dict:
    payload = payload or {}
    force_port_kill = str(payload.get("forcePortKill") or payload.get("force") or "").strip().lower() in {"1", "true", "yes", "on"}
    argv, meta = service_restart_argv_fn(
        payload,
        service="phone-scanner",
        env_prefix="URIRUN_PHONE_SCANNER",
        default_unit="urirun-service-scanner.service",
    )
    meta.setdefault("exampleUri", "dashboard://host/service/phone-scanner/command/restart")
    if argv:
        return schedule_restart_fn(argv, payload, meta)

    bind_host, scanner_port = _resolve_scanner_bind_params(payload)
    service_id = phone_scanner_service_id(bind_host, scanner_port)
    server, thread = _pop_scanner_service(service_id)

    result = _handle_inprocess_restart(
        project, db, config, node_urls, token, identity,
        bind_host, scanner_port, server, thread, ensure_fn,
    )
    if result is not None:
        return result

    result = _handle_port_free_restart(
        scanner_port, force_port_kill, meta, free_port_fn, ensure_fn,
        project, db, config, node_urls, token, identity, bind_host,
    )
    if result is not None:
        return result

    return _handle_external_status_restart(
        scanner_port, meta, external_status_fn, ensure_fn,
        project, db, config, node_urls, token, identity, bind_host,
    )
