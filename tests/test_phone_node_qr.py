from __future__ import annotations

from urirun_connector_scanner import scanner_service as service


class _DB:
    def register_artifact(self, db, kind, uri, path, meta):
        return {"kind": kind, "uri": uri, "path": path, "meta": meta}


def _message(role, content, detail=None, attachments=None):
    return {
        "role": role,
        "content": content,
        "detail": detail or {},
        "attachments": attachments or [],
    }


def _add_message(db, message):
    return {"ok": True, "message": message}


def _preview(path, project):
    return path


def _write_qr(url, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")


def test_phone_node_qr_starts_android_node_when_service_is_unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("URIRUN_DASHBOARD_QR_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_lan_host", lambda: "127.0.0.1")
    monkeypatch.setattr(service, "_write_qr_png", _write_qr)
    probes = []

    def probe(url, timeout=1.5):
        probes.append((url, timeout))
        return len(probes) >= 2

    starts = []

    def ensure(payload):
        starts.append(payload)
        return {"ok": True, "alreadyRunning": False, "url": payload["url"]}

    monkeypatch.setattr(service, "_probe_scanner_url", probe)
    res = service.phone_node_qr(
        ".",
        None,
        {},
        host_db_fn=_DB,
        preview_url_fn=_preview,
        chat_message_fn=_message,
        add_chat_message_fn=_add_message,
        ensure_android_node_fn=ensure,
    )

    assert len(starts) == 1
    assert starts[0]["port"] == 8195
    assert res["serviceReachable"] is True
    assert res["serviceStart"]["ok"] is True
    assert res["artifact"]["meta"]["serviceStart"]["ok"] is True


def test_phone_node_qr_respects_autostart_false(monkeypatch, tmp_path):
    monkeypatch.setenv("URIRUN_DASHBOARD_QR_DIR", str(tmp_path))
    monkeypatch.setattr(service, "_lan_host", lambda: "127.0.0.1")
    monkeypatch.setattr(service, "_write_qr_png", _write_qr)
    monkeypatch.setattr(service, "_probe_scanner_url", lambda url, timeout=1.5: False)

    def ensure(payload):
        raise AssertionError("ensure should not be called when autoStart is false")

    res = service.phone_node_qr(
        ".",
        None,
        {"autoStart": False},
        host_db_fn=_DB,
        preview_url_fn=_preview,
        chat_message_fn=_message,
        add_chat_message_fn=_add_message,
        ensure_android_node_fn=ensure,
    )

    assert res["serviceReachable"] is False
    assert res["serviceStart"] is None
    assert "serviceStart" not in res["artifact"]["meta"]
