from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .document_sync import load_document_index as _load_document_index, file_sha256 as _file_sha256


def _query_value(query: dict, name: str, default: "str | None" = None) -> "str | None":
    values = query.get(name)
    return values[0] if values else default


PAGE_ACTION_LOCK = threading.Lock()
PAGE_ACTION_QUEUES: dict[str, list[dict]] = {}
_PAGE_ACTION_QUEUE_MAX = 50
_POLL_ITEMS_MAX = 20
_OCR_PREVIEW_CHARS = 180

SCANNER_BEST_LOCK = threading.Lock()
SCANNER_BEST_SESSIONS: dict[str, dict] = {}
SCANNER_LIVE_STREAMS: dict[str, dict] = {}


def scanner_live_store_locked(
    series_id: str,
    series: dict,
    *,
    status: str = "running",
    error: str | None = None,
    document: dict | None = None,
    artifact: dict | None = None,
) -> None:
    """Write a live-stream snapshot for ``series_id``. Must be called under SCANNER_BEST_LOCK."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    candidates = [item for item in (series.get("candidates") or []) if isinstance(item, dict)]
    best = series.get("best") if isinstance(series.get("best"), dict) else None
    SCANNER_LIVE_STREAMS[series_id] = {
        "seriesId": series_id,
        "createdAt": series.get("createdAt") or ts,
        "updatedAt": ts,
        "status": status,
        "count": len(candidates),
        "best": best,
        "candidates": candidates[-8:],
        "error": error,
        "document": document or series.get("document"),
        "artifact": artifact or series.get("artifact"),
    }
    if len(SCANNER_LIVE_STREAMS) > 20:
        keep = sorted(SCANNER_LIVE_STREAMS.items(), key=lambda item: str(item[1].get("updatedAt") or ""), reverse=True)[:20]
        SCANNER_LIVE_STREAMS.clear()
        SCANNER_LIVE_STREAMS.update(dict(keep))


def scanner_best_update(series_id: str, candidate: dict) -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with SCANNER_BEST_LOCK:
        series = SCANNER_BEST_SESSIONS.setdefault(series_id, {"createdAt": ts, "candidates": []})
        series["updatedAt"] = ts
        series["candidates"].append(candidate)
        series["candidates"] = series["candidates"][-24:]
        best = max(series["candidates"], key=lambda item: float((item.get("quality") or {}).get("score") or 0.0))
        series["best"] = best
        scanner_live_store_locked(series_id, series, status="running")
        return {
            "seriesId": series_id,
            "count": len(series["candidates"]),
            "best": public_scanner_candidate(best),
        }


def scanner_best_take(series_id: str, *, clear: bool = True) -> dict | None:
    with SCANNER_BEST_LOCK:
        series = SCANNER_BEST_SESSIONS.get(series_id)
        if not series:
            return None
        if clear:
            SCANNER_BEST_SESSIONS.pop(series_id, None)
        return dict(series)


def scanner_staging_dir() -> Path:
    return Path(os.environ.get("URIRUN_SCANNER_DIR", "~/.urirun/host-dashboard/scans")).expanduser().resolve()


_scanner_staging_dir = scanner_staging_dir
_time = time.time


@dataclass(frozen=True)
class ScannerBridgeDeps:
    preview_url: Callable[[str, str], str | None]
    register_artifact: Callable[[str | None, str, str, str, dict], Any]
    chat_message: Callable[..., dict]
    add_chat_message: Callable[[str | None, dict], Any]
    add_log: Callable[[str | None, str, str, dict], Any] | None = None


def crop_overlay_attachment(
    deps: ScannerBridgeDeps,
    *,
    uri: str,
    project: str,
    overlay_path: str,
    crop: dict,
    meta: dict,
    original_path: Path,
) -> dict:
    return {
        "kind": "crop-overlay",
        "path": overlay_path,
        "uri": f"{uri}/crop-overlay",
        "previewUrl": deps.preview_url(overlay_path, project),
        "meta": {
            "crop": crop,
            "quality": meta.get("quality"),
            "sourceCaptureUri": uri,
            "sourceImage": str(original_path),
        },
    }


def register_document_artifact(
    deps: ScannerBridgeDeps,
    db: str | None,
    project: str,
    *,
    uri: str,
    display_path: Path,
    original_path: Path,
    meta: dict,
    ocr: dict,
    document: dict,
) -> tuple[Any, dict]:
    """Register the canonical document-pdf artifact; return (artifact_row, chat_attachment)."""
    document_id = str(
        document.get("duplicateOf") or document.get("docId") or meta.get("sha256") or ""
    )
    document_uri = str(document.get("uri") or f"document://host/{quote(document_id, safe='')}")
    document_meta = {
        "document": document,
        "ocr": {key: value for key, value in ocr.items() if key != "text"},
        "sourceCaptureUri": uri,
        "sourceImage": str(original_path),
        "displayImage": str(display_path),
    }
    artifact = deps.register_artifact(db, "document-pdf", document_uri, str(document["path"]), document_meta)
    attachment = {
        "kind": "document-pdf",
        "path": str(document["path"]),
        "uri": document_uri,
        "previewUrl": deps.preview_url(str(document["path"]), project),
        "meta": document_meta,
    }
    return artifact, attachment


def scanner_result_content(content_prefix: str, crop: dict, document: dict, ocr: dict) -> str:
    """Human chat line summarizing one scan: crop / document-PDF / OCR outcome."""
    content = content_prefix
    if crop.get("ok"):
        content += " (cropped to receipt)"
    if document.get("ok") and document.get("path"):
        content += " -> document PDF"
        if document.get("duplicate"):
            content += " (duplicate)"
    elif document.get("error"):
        content += " (document archive failed)"
    else:
        content += " (no document PDF)"
    if ocr.get("ok") and ocr.get("text"):
        content += f": {str(ocr.get('text'))[:_OCR_PREVIEW_CHARS]}"
    elif ocr.get("error"):
        content += f" (OCR: {ocr.get('error')})"
    return content


def public_scanner_candidate(candidate: dict) -> dict:
    ocr = candidate.get("ocr") if isinstance(candidate.get("ocr"), dict) else {}
    return {
        "seriesId": candidate.get("seriesId"),
        "frameIndex": candidate.get("frameIndex"),
        "uri": candidate.get("uri"),
        "path": candidate.get("displayPath"),
        "originalPath": candidate.get("originalPath"),
        "overlayPath": candidate.get("overlayPath"),
        "overlay": candidate.get("overlay"),
        "sha256": candidate.get("sha256"),
        "quality": candidate.get("quality"),
        "detectedDocument": candidate.get("detectedDocument"),
        "crop": candidate.get("crop"),
        "ocr": {key: value for key, value in ocr.items() if key != "text"},
    }


def scanner_public_candidate_for_live(
    candidate: dict | None,
    project: str,
    *,
    preview_url: Callable[[str, str], str | None],
) -> dict | None:
    if not isinstance(candidate, dict):
        return None
    public = public_scanner_candidate(candidate)
    path = public.get("path")
    if path:
        public["previewUrl"] = preview_url(str(path), project)
    original = public.get("originalPath")
    if original:
        public["originalPreviewUrl"] = preview_url(str(original), project)
    overlay = public.get("overlayPath")
    if overlay:
        public["overlayPreviewUrl"] = preview_url(str(overlay), project)
    return public


def scanner_live_state_from_streams(
    streams: list[dict],
    project: str,
    *,
    limit: int = 8,
    preview_url: Callable[[str, str], str | None],
    utc_now: Callable[[], str],
) -> dict:
    selected = sorted(
        [dict(item) for item in streams],
        key=lambda item: str(item.get("updatedAt") or ""),
        reverse=True,
    )[: max(1, min(_POLL_ITEMS_MAX, int(limit or 8)))]
    public_streams = []
    for stream in selected:
        candidates = [
            item
            for item in (
                scanner_public_candidate_for_live(candidate, project, preview_url=preview_url)
                for candidate in stream.get("candidates", [])
            )
            if item
        ]
        best = scanner_public_candidate_for_live(stream.get("best"), project, preview_url=preview_url)
        document = stream.get("document") if isinstance(stream.get("document"), dict) else {}
        if document.get("path"):
            document = {**document, "previewUrl": preview_url(str(document["path"]), project)}
        public_streams.append({
            **{key: value for key, value in stream.items() if key not in {"best", "candidates", "document"}},
            "best": best,
            "candidates": candidates,
            "document": document,
        })
    return {"ok": True, "updatedAt": utc_now(), "streams": public_streams}


def register_scanner_result(
    deps: ScannerBridgeDeps,
    project: str,
    db: str | None,
    *,
    uri: str,
    display_path: Path,
    original_path: Path,
    meta: dict,
    crop: dict,
    ocr: dict,
    document: dict,
    content_prefix: str,
) -> dict:
    # The staged crop/image can be gone when docid recognized a duplicate and cleaned
    # staging. In that case do not register a second artifact row that points at the
    # existing document PDF; the document-pdf artifact below is the canonical record.
    display_exists = Path(str(display_path)).expanduser().is_file()
    attachments = []
    document_artifact = None
    overlay_path = str(meta.get("overlayPath") or "")
    if overlay_path and Path(overlay_path).expanduser().is_file():
        attachments.append(crop_overlay_attachment(
            deps,
            uri=uri,
            project=project,
            overlay_path=overlay_path,
            crop=crop,
            meta=meta,
            original_path=original_path,
        ))
    if document.get("ok") and document.get("path"):
        document_artifact, document_attachment = register_document_artifact(
            deps,
            db,
            project,
            uri=uri,
            display_path=display_path,
            original_path=original_path,
            meta=meta,
            ocr=ocr,
            document=document,
        )
        attachments.append(document_attachment)
    if document_artifact is None and display_exists:
        scan_artifact = deps.register_artifact(db, "camera-scan", uri, str(display_path), meta)
    else:
        scan_artifact = {
            "kind": "camera-scan",
            "uri": uri,
            "path": None,
            "meta": meta,
            "skipped": True,
            "reason": (
                "document-pdf artifact is canonical"
                if document_artifact
                else "staged display image is not available"
            ),
        }
    primary_artifact = document_artifact or scan_artifact
    content = scanner_result_content(content_prefix, crop, document, ocr)
    message = deps.chat_message(
        "system",
        content,
        detail={
            "artifact": primary_artifact,
            "scanArtifact": scan_artifact,
            "documentArtifact": document_artifact,
            "primaryArtifact": primary_artifact,
            "uri": uri,
            "selectedTargets": ["service:phone-scanner"],
            "ocr": ocr,
            "document": document,
        },
        attachments=attachments,
    )
    deps.add_chat_message(db, message)
    return {
        "artifact": primary_artifact,
        "scanArtifact": scan_artifact,
        "documentArtifact": document_artifact,
        "primaryArtifact": primary_artifact,
        "message": message,
    }


def _add_log(deps: ScannerBridgeDeps, db: str | None, stream: str, event: str, detail: dict) -> None:
    if deps.add_log is None:
        return
    try:
        deps.add_log(db, stream, event, detail)
    except Exception:  # noqa: BLE001
        pass


def scanner_session(deps: ScannerBridgeDeps, db: str | None, payload: dict) -> dict:
    event = str(payload.get("event") or "open")
    fingerprint = json.dumps({
        "event": event,
        "userAgent": payload.get("userAgent", ""),
        "href": payload.get("href", ""),
        "at": payload.get("at", ""),
    }, sort_keys=True)
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    uri = f"scanner://host/session/{digest[:16]}"
    detail = {
        "uri": uri,
        "event": event,
        "selectedTargets": ["service:phone-scanner"],
        "href": payload.get("href"),
        "width": payload.get("width"),
        "height": payload.get("height"),
        "userAgent": payload.get("userAgent", ""),
        "at": payload.get("at"),
        "tracks": payload.get("tracks") or [],
    }
    label = (
        "Phone scanner opened"
        if event == "open"
        else "Phone scanner camera started"
        if event == "camera-started"
        else f"Phone scanner {event}"
    )
    message = deps.chat_message("system", label, detail=detail)
    _add_log(deps, db, "scanner-session", event, detail)
    deps.add_chat_message(db, message)
    return {"ok": True, "uri": uri, "message": message}


def uri_event(deps: ScannerBridgeDeps, db: str | None, query: dict[str, list[str]]) -> dict:
    event = _query_value(query, "e", "event") or "event"
    detail = {
        "site": _query_value(query, "s", ""),
        "event": event,
        "path": _query_value(query, "p", ""),
        "url": _query_value(query, "u", ""),
        "referrer": _query_value(query, "r", ""),
        "label": _query_value(query, "l", ""),
        "value": _query_value(query, "v", ""),
        "raw": {
            key: values[0] if len(values) == 1 else values
            for key, values in query.items()
        },
    }
    _add_log(deps, db, "uri-js", event, detail)
    return {"ok": True, "event": event}


def page_action_enqueue(
    deps: ScannerBridgeDeps,
    db: str | None,
    *,
    target: str,
    uri: str,
    payload: dict | None = None,
    mode: str = "execute",
    source: str = "host",
    uri_mode: Callable[[Any], str],
    utc_now: Callable[[], str],
) -> dict:
    target = (target or "scanner").strip() or "scanner"
    action_id = hashlib.sha256(
        f"{time.time_ns()}:{target}:{uri}".encode("utf-8")
    ).hexdigest()[:16]
    item = {
        "id": action_id,
        "target": target,
        "uri": uri,
        "payload": payload or {},
        "mode": uri_mode(mode),
        "source": source,
        "createdAt": utc_now(),
    }
    with PAGE_ACTION_LOCK:
        queue = PAGE_ACTION_QUEUES.setdefault(target, [])
        queue.append(item)
        PAGE_ACTION_QUEUES[target] = queue[-_PAGE_ACTION_QUEUE_MAX:]
    _add_log(deps, db, "page-action", "queued", item)
    return {"ok": True, "queued": True, "target": target, "action": item}


def page_action_poll(target: str = "scanner", limit: int = 4) -> dict:
    target = (target or "scanner").strip() or "scanner"
    limit = max(1, min(_POLL_ITEMS_MAX, int(limit or 4)))
    with PAGE_ACTION_LOCK:
        queue = PAGE_ACTION_QUEUES.get(target, [])
        actions = queue[:limit]
        PAGE_ACTION_QUEUES[target] = queue[limit:]
    return {"ok": True, "target": target, "actions": actions, "count": len(actions)}


def page_action_result(
    deps: ScannerBridgeDeps,
    db: str | None,
    payload: dict,
    *,
    utc_now: Callable[[], str],
) -> dict:
    detail = {
        "id": payload.get("id"),
        "target": payload.get("target") or "scanner",
        "uri": payload.get("uri"),
        "ok": payload.get("ok"),
        "error": payload.get("error"),
        "result": payload.get("result"),
        "at": payload.get("at") or utc_now(),
    }
    _add_log(deps, db, "page-action", "result", detail)
    return {"ok": True, "result": detail}


def scanner_status_from_log(item: dict) -> tuple[dict, str, dict] | None:
    """Return (status_dict, action_uri, detail) for a scanner camera-status log entry."""
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    if item.get("event") != "result" or (detail.get("target") or "scanner") != "scanner":
        return None
    uri = str(detail.get("uri") or "")
    if not (
        uri.endswith("/camera/query/status")
        or uri.endswith("/camera/command/start")
        or uri.endswith("/ui/button/start-camera/command/click")
    ):
        return None
    result = detail.get("result") if isinstance(detail.get("result"), dict) else {}
    status = result.get("status") if isinstance(result.get("status"), dict) else result
    if not isinstance(status, dict):
        return None
    return status, uri, detail


def latest_scanner_page_status(logs: list[dict] | tuple[dict, ...]) -> dict:
    """Build the public scanner page status from recent page-action logs."""
    for item in logs:
        found = scanner_status_from_log(item)
        if found is None:
            continue
        status, uri, detail = found
        public_status = {key: value for key, value in status.items() if key != "localActions"}
        public_status.update({
            "actionUri": uri,
            "ok": detail.get("ok"),
            "error": detail.get("error") or public_status.get("error"),
            "at": detail.get("at") or item.get("created_at"),
        })
        return public_status
    return {}


def scanner_artifact_doc_meta(artifact: dict) -> dict:
    """Return merged detected/document metadata used by scanner artifact views."""
    meta = artifact.get("meta") if isinstance(artifact.get("meta"), dict) else {}
    document = meta.get("document") if isinstance(meta.get("document"), dict) else {}
    document_meta = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    detected = meta.get("detectedDocument") if isinstance(meta.get("detectedDocument"), dict) else {}
    return {**detected, **document_meta}


def is_scanner_artifact(kind: str, uri: str, meta: dict) -> bool:
    """True when an artifact came from the phone-scanner pipeline."""
    return (
        kind in {"camera-scan", "document-pdf", "dashboard-qr"}
        and (
            uri.startswith(("scanner://", "document://host/", "dashboard://host/qr/"))
            or str(meta.get("sourceCaptureUri") or "").startswith("scanner://")
        )
    )


def scanner_artifact_item(
    artifact: dict,
    kind: str,
    uri: str,
    path: str,
    display_path: str,
    doc: dict,
    project: str,
    *,
    preview_url: Callable[[str, str], str | None],
) -> dict:
    """Build the public artifact item used by scanner status widgets."""
    return {
        "id": artifact.get("id"),
        "kind": kind,
        "uri": uri,
        "path": path,
        "createdAt": artifact.get("created_at"),
        "previewUrl": preview_url(display_path, project) if display_path else "",
        "filePreviewUrl": preview_url(path, project) if path else "",
        "label": Path(path).name if path else uri,
        **{key: value for key, value in doc.items() if value},
    }


def scanner_service_live_views(
    scanner: dict,
    service: dict,
    recent_artifacts: list[dict],
    camera_status: dict,
    *,
    utc_now: Callable[[], str],
) -> dict:
    """Build host-dashboard live/status views for the phone scanner service."""
    views: list[dict] = []
    streams = scanner.get("streams") or []
    if streams:
        status_order = {"accepted": 4, "running": 3, "rejected": 2, "failed": 1}
        status = max(
            (str(item.get("status") or "running") for item in streams),
            key=lambda item: status_order.get(item, 0),
            default="running",
        )
        views.append({
            "id": "service:phone-scanner/live",
            "target": "service:phone-scanner",
            "serviceId": "service:phone-scanner",
            "title": "phone scanner stream",
            "kind": "stream",
            "view": "scanner-stream",
            "status": status,
            "updatedAt": scanner.get("updatedAt"),
            "refreshMs": 1000,
            "data": {"streams": streams},
            "supportedViews": [
                "scanner-stream",
                "scanner-status",
                "table",
                "image-list",
                "video",
                "iframe",
                "form",
                "graph",
                "json",
            ],
        })
    if service or recent_artifacts or camera_status:
        views.append({
            "id": "service:phone-scanner/status",
            "target": "service:phone-scanner",
            "serviceId": "service:phone-scanner",
            "title": "phone scanner status",
            "kind": "status",
            "view": "scanner-status",
            "status": "running" if service.get("reachable") else "stopped",
            "updatedAt": camera_status.get("at") or scanner.get("updatedAt"),
            "refreshMs": 1000,
            "data": {
                "service": service,
                "cameraStatus": camera_status,
                "recentArtifacts": recent_artifacts,
                "streamCount": len(streams),
            },
            "supportedViews": ["scanner-status", "scanner-stream", "image-list", "iframe", "json"],
        })
    return {"ok": True, "updatedAt": utc_now(), "views": views}


def scanner_flow_result(
    service: dict,
    autonomous_scan: bool,
    camera_action_uri: str,
    camera_payload: dict,
    torch_click_uri: str,
    torch_enabled: bool | None,
    queued_camera: dict | None,
    queued_torch: dict | None,
    prompt: str,
    selected_nodes: list[str],
    selected_targets: list[str],
) -> dict:
    """Build the response dict for the phone-scanner chat path."""
    return {
        "ok": bool(service.get("ok")),
        "prompt": prompt,
        "execute": True,
        "selectedNodes": selected_nodes,
        "selectedTargets": selected_targets,
        "generator": {"provider": "host-dashboard", "intent": "phone-scanner-service"},
        "flow": {
            "task": {"id": "phone-scanner-service", "title": "Start phone scanner service"},
            "steps": [
                {"id": "start-phone-scanner", "uri": "dashboard://host/phone-scanner/command/start", "payload": {}},
                *([{
                    "id": "queue-camera-autonomous" if autonomous_scan else "queue-camera-start",
                    "uri": camera_action_uri,
                    "payload": camera_payload,
                }] if queued_camera else []),
                *([{
                    "id": "queue-camera-light",
                    "uri": torch_click_uri,
                    "payload": {"target": "scanner", "enabled": bool(torch_enabled)},
                }] if queued_torch else []),
            ],
        },
        "timeline": [
            {
                "id": "start-phone-scanner",
                "uri": "dashboard://host/phone-scanner/command/start",
                "target": "host",
                "ok": bool(service.get("ok")),
                "status": service.get("status"),
            },
            *([{
                "id": "queue-camera-autonomous" if autonomous_scan else "queue-camera-start",
                "uri": camera_action_uri,
                "target": "scanner-page",
                "ok": bool(queued_camera.get("ok")),
                "status": "queued",
                "autonomous": bool(autonomous_scan),
            }] if queued_camera else []),
            *([{
                "id": "queue-camera-light",
                "uri": torch_click_uri,
                "target": "scanner-page",
                "ok": bool(queued_torch.get("ok")),
                "status": "queued",
            }] if queued_torch else []),
        ],
        "results": {
            "phone-scanner-service": service,
            **({"camera-start": queued_camera} if queued_camera else {}),
            **({"camera-torch": queued_torch} if queued_torch else {}),
        },
        "attachments": ((service.get("message") or {}).get("attachments") or []),
    }


def nl_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.translate(str.maketrans({"ł": "l", "ß": "ss"}))


def is_phone_scanner_prompt(prompt: str) -> bool:
    text = nl_text(prompt)
    scanner_terms = (
        "skaner", "scanner", "skan", "scan", "kamera", "camera", "telefon", "phone", "mobile", "mobil",
        "webrtc", "qr", "qrcode", "paragon", "rachunek", "smartfon", "latark", "swiatl", "torch", "flash",
    )
    service_terms = ("aplikac", "uslug", "service", "stron", "narzedz", "interfejs")
    start_terms = (
        "uruchom", "wystart", "stworz", "utworz", "start", "create", "open", "wlacz", "odpal", "daj",
        "pokaz", "link", "adres", "ip", "qr", "wylacz", "zgas", "disable", "off",
    )
    wants_scanner = any(word in text for word in scanner_terms)
    wants_service = any(word in text for word in service_terms)
    wants_start = any(word in text for word in start_terms)
    autonomous_context = any(word in text for word in ("auto", "autonom", "samoczyn", "petl", "ciagl", "co 1"))
    mobile_context = any(word in text for word in (
        "telefon", "phone", "mobile", "mobil", "smartfon", "webrtc", "kamera", "camera",
        "qr", "skaner", "scanner", "latark", "swiatl", "torch", "flash",
    ))
    return (wants_start and (wants_scanner or (wants_service and mobile_context))) or (
        autonomous_context and wants_scanner
    )


def is_autonomous_scanner_prompt(prompt: str) -> bool:
    text = nl_text(prompt)
    autonomous_terms = ("auto", "autonom", "samoczyn", "petl", "ciagl", "co 1")
    document_terms = ("paragon", "rachunek", "faktur", "receipt", "invoice")
    scanner_terms = ("skan", "scan", "skaner", "scanner", "kamera", "camera", "telefon", "smartfon", "phone", "mobile")
    return any(word in text for word in autonomous_terms) and (
        any(word in text for word in document_terms) or any(word in text for word in scanner_terms)
    )


def is_camera_start_prompt(prompt: str) -> bool:
    text = nl_text(prompt)
    camera_terms = ("kamer", "camera", "webcam", "aparat", "obiektyw")
    start_terms = ("wlacz", "uruchom", "start", "odpal", "otworz", "aktywow", "enable")
    return any(word in text for word in camera_terms) and any(word in text for word in start_terms)


def torch_enabled_from_prompt(prompt: str) -> bool | None:
    text = nl_text(prompt)
    torch_terms = ("latark", "swiatl", "oswietl", "lampa", "led", "torch", "flash")
    if not any(word in text for word in torch_terms):
        return None
    off_terms = ("wylacz", "zgas", "off", "disable", "stop")
    on_terms = ("wlacz", "uruchom", "start", "odpal", "zaswiec", "on", "enable")
    if any(word in text for word in off_terms):
        return False
    if any(word in text for word in on_terms):
        return True
    return True


# --------------------------------------------------------------------------- #
# Image / crop quality scoring — pure functions, no host_dashboard deps
# --------------------------------------------------------------------------- #

from ._scanner_quality import (  # noqa: E402
    bounded, frame_visual_metrics, crop_dimensions, crop_geometry_score,
    crop_quality_score, doctype_quality_score, metadata_quality_score,
    ocr_quality_score, visual_quality_score, document_frame_quality,
    orientation_summary,
)


# --------------------------------------------------------------------------- #
# Capture pipeline — pure helpers and state-touching best-finish utilities
# --------------------------------------------------------------------------- #

def decode_capture_image(raw_image: str) -> tuple[str, bytes, str, str]:
    """Parse a ``data:image/*;base64`` payload into (mime, raw_bytes, sha256, file_ext)."""
    match = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", raw_image, re.S)
    if not match:
        raise ValueError("image must be a data:image/*;base64 payload")
    mime, encoded = match.group(1), match.group(2)
    raw = base64.b64decode(encoded.encode("ascii"), validate=False)
    digest = hashlib.sha256(raw).hexdigest()
    ext = ".jpg" if mime in {"image/jpeg", "image/jpg"} else ".png" if mime == "image/png" else ".bin"
    return mime, raw, digest, ext


_decode_capture_image = decode_capture_image


def capture_quality_ok(payload: dict, quality: dict, min_score: float) -> bool:
    return bool(payload.get("force")) or (
        float(quality.get("score") or 0.0) >= min_score and bool(quality.get("documentLike"))
    )


def capture_display_path(crop: dict, path: Path) -> Path:
    return Path(crop["path"]) if crop.get("ok") and crop.get("path") else path


def cleanup_duplicate_scan_files(paths: list) -> list[str]:
    try:
        staging = scanner_staging_dir()
    except Exception:  # noqa: BLE001
        return []
    removed: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        try:
            target = Path(str(raw)).expanduser().resolve()
        except Exception:  # noqa: BLE001
            continue
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        if staging not in target.parents:
            continue
        try:
            target.unlink(missing_ok=True)
            removed.append(str(target))
        except Exception:  # noqa: BLE001
            pass
    return removed


def resolve_best_candidate(series: dict) -> dict | None:
    best = series.get("best")
    if isinstance(best, dict):
        return best
    candidates = [item for item in series.get("candidates", []) if isinstance(item, dict)]
    if not candidates:
        return None
    return max(candidates, key=lambda item: float((item.get("quality") or {}).get("score") or 0.0))


def best_quality_rejected(payload: dict, quality: dict) -> tuple[bool, float]:
    min_score = float(payload.get("minScore") if payload.get("minScore") is not None else 45.0)
    rejected = not payload.get("force") and (
        float(quality.get("score") or 0.0) < min_score or not quality.get("documentLike")
    )
    return rejected, min_score


def best_candidate_paths(best: dict) -> tuple[Path, Path]:
    return (
        Path(str(best.get("originalPath") or "")).expanduser().resolve(),
        Path(str(best.get("displayPath") or "")).expanduser().resolve(),
    )


def best_crop_and_ocr(best: dict) -> tuple[dict, dict]:
    crop = best.get("crop") if isinstance(best.get("crop"), dict) else {}
    ocr = best.get("ocr") if isinstance(best.get("ocr"), dict) else {}
    return crop, ocr


def best_series_not_found(series_id: str) -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with SCANNER_BEST_LOCK:
        SCANNER_LIVE_STREAMS[series_id] = {
            "seriesId": series_id,
            "createdAt": ts,
            "updatedAt": ts,
            "status": "failed",
            "count": 0,
            "best": None,
            "candidates": [],
            "error": "scanner best series not found",
            "document": {},
            "artifact": None,
        }
    return {"ok": False, "error": "scanner best series not found", "seriesId": series_id}


def store_best_finish(series: dict, series_id: str, best: dict, document: dict, registered: dict) -> None:
    with SCANNER_BEST_LOCK:
        series["best"] = best
        series["document"] = document
        series["artifact"] = registered["documentArtifact"] or registered["artifact"]
        scanner_live_store_locked(
            series_id,
            series,
            status="accepted" if document.get("ok") else "failed",
            error=None if document.get("ok") else str(document.get("error") or "document archive failed"),
            document=document,
            artifact=registered["documentArtifact"] or registered["artifact"],
        )


def best_finish_store_failure(
    series_id: str,
    series: dict,
    *,
    status: str,
    error: str,
    best: dict | None = None,
    project: str = "",
    extra: dict | None = None,
    preview_url: Callable[[str, str], str | None] | None = None,
) -> dict:
    with SCANNER_BEST_LOCK:
        if best is not None:
            series["best"] = best
        scanner_live_store_locked(series_id, series, status=status, error=error)
    result: dict = {"ok": False, "error": error, "seriesId": series_id}
    if best is not None:
        result["best"] = scanner_public_candidate_for_live(best, project, preview_url=preview_url) if preview_url else public_scanner_candidate(best)
    if extra:
        result.update(extra)
    return result


def capture_reject_result(
    *,
    uri: str,
    min_score: float,
    quality: dict,
    ocr: dict,
    crop: dict,
    overlay: dict,
    detected_document: dict,
    paths: list,
) -> dict:
    removed_scan_files = cleanup_duplicate_scan_files(paths)
    reject_reason = str(quality.get("cropReason") or "")
    if not reject_reason and not quality.get("documentLike"):
        reject_reason = "not document-like"
    if not reject_reason:
        reject_reason = "low-quality scan"
    return {
        "ok": True,
        "rejected": True,
        "uri": uri,
        "reason": reject_reason,
        "minScore": min_score,
        "quality": quality,
        "ocr": ocr,
        "crop": crop,
        "overlay": overlay,
        "detectedDocument": detected_document,
        "removedScanFiles": removed_scan_files,
    }


def capture_candidate_result(
    project: str,
    payload: dict,
    *,
    uri: str,
    mime: str,
    digest: str,
    raw_len: int,
    path: Path,
    display_path: Path,
    overlay_path: str,
    overlay: dict,
    crop: dict,
    ocr: dict,
    detected_document: dict,
    quality: dict,
    preview_url: Callable[[str, str], str | None] | None = None,
) -> dict:
    candidate = {
        "seriesId": str(payload.get("seriesId") or ""),
        "frameIndex": payload.get("frameIndex"),
        "uri": uri,
        "mime": mime,
        "sha256": digest,
        "bytes": raw_len,
        "originalPath": str(path),
        "displayPath": str(display_path),
        "overlayPath": overlay_path,
        "overlay": overlay,
        "crop": crop,
        "ocr": ocr,
        "detectedDocument": detected_document,
        "quality": quality,
        "capturedAt": payload.get("capturedAt"),
        "userAgent": payload.get("userAgent", ""),
        "width": payload.get("width"),
        "height": payload.get("height"),
    }
    series = None
    if candidate["seriesId"]:
        series = scanner_best_update(candidate["seriesId"], candidate)
    return {
        "ok": True,
        "uri": uri,
        "candidate": scanner_public_candidate_for_live(candidate, project, preview_url=preview_url) if preview_url else public_scanner_candidate(candidate),
        "series": series,
        "ocr": ocr,
        "crop": crop,
        "overlay": overlay,
        "quality": quality,
        "detectedDocument": detected_document,
    }


def scanner_live_state(
    project: str,
    limit: int = 8,
    *,
    preview_url: Callable[[str, str], str | None] | None = None,
) -> dict:
    with SCANNER_BEST_LOCK:
        streams = [dict(item) for item in SCANNER_LIVE_STREAMS.values()]
    utc_now = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # noqa: E731
    return scanner_live_state_from_streams(
        streams,
        project,
        limit=limit,
        preview_url=preview_url or (lambda p, pr: None),
        utc_now=utc_now,
    )


def draw_crop_box(draw: Any, canvas: Any, color: tuple, box: Any, scale: float,
                  original_width: int, original_height: int) -> None:
    if box and len(box) == 4:
        left, top, right, bottom = (float(v) for v in box)
        scaled_box = (
            int(max(0, min(original_width, left)) * scale),
            int(max(0, min(original_height, top)) * scale),
            int(max(0, min(original_width, right)) * scale),
            int(max(0, min(original_height, bottom)) * scale),
        )
        for offset in range(4):
            draw.rectangle(
                (scaled_box[0] - offset, scaled_box[1] - offset,
                 scaled_box[2] + offset, scaled_box[3] + offset),
                outline=color,
            )
    else:
        draw.rectangle((3, 3, canvas.size[0] - 4, canvas.size[1] - 4), outline=color, width=4)


def draw_overlay_label(draw: Any, canvas: Any, crop: dict, quality: dict | None, ok: bool) -> None:
    from PIL import ImageFont
    score = (quality or {}).get("score")
    label_parts = [
        "crop:ok" if ok else "crop:rejected",
        str(crop.get("method") or crop.get("reason") or ""),
        f"score={score}" if score is not None else "",
    ]
    label = " | ".join(part for part in label_parts if part)[:180]
    font = ImageFont.load_default()
    try:
        text_box = draw.textbbox((0, 0), label, font=font)
        text_width, text_height = text_box[2] - text_box[0], text_box[3] - text_box[1]
    except Exception:  # noqa: BLE001
        text_width = min(canvas.size[0] - 16, max(80, len(label) * 6))
        text_height = 12
    draw.rectangle((6, 6, min(canvas.size[0] - 6, text_width + 18), text_height + 18), fill=(0, 0, 0))
    draw.text((12, 10), label, fill=(255, 255, 255), font=font)


def scanner_crop_overlay(original_path: str | Path, crop: dict, quality: dict | None = None) -> dict:
    """Write a diagnostic image with the detected crop box drawn over the raw frame."""
    try:
        from PIL import Image, ImageDraw, ImageOps

        source = Path(original_path).expanduser().resolve()
        if not source.is_file():
            return {"ok": False, "reason": "source image missing"}
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        original_width, original_height = image.size
        max_side = int(os.environ.get("URIRUN_SCANNER_OVERLAY_MAX_SIDE", "1100") or "1100")
        scale = min(1.0, max(240, max_side) / max(original_width, original_height))
        canvas = image.resize((max(1, int(original_width * scale)), max(1, int(original_height * scale)))) if scale < 1.0 else image.copy()
        draw = ImageDraw.Draw(canvas)
        ok = bool(crop.get("ok"))
        partial = bool(crop.get("partialEdge"))
        color = (48, 214, 126) if ok else (239, 68, 68) if partial else (245, 158, 11)
        box = crop.get("box") if isinstance(crop.get("box"), (list, tuple)) else None
        draw_crop_box(draw, canvas, color, box, scale, original_width, original_height)
        draw_overlay_label(draw, canvas, crop, quality, ok)
        target = source.with_name(f"{source.stem}-crop-overlay.jpg")
        canvas.save(target, format="JPEG", quality=88, optimize=True)
        return {
            "ok": True,
            "path": str(target),
            "width": canvas.size[0],
            "height": canvas.size[1],
            "scale": round(scale, 6),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}


def auto_crop_receipt(path: Path) -> dict:
    try:
        from urirun_connector_smart_crop import detect_document_crop
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"smart-crop connector unavailable: {exc}", "originalPath": str(path)}
    return detect_document_crop(path, output_path=path.with_name(f"{path.stem}-receipt-crop.jpg"))


def capture_ocr_and_detect(path: Path, display_path: Path, payload: dict, archive: bool,
                            *, local_image_ocr: Any, extract_document_metadata: Any,
                            truthy_env: Any) -> tuple[dict, dict]:
    """OCR the frame and extract document metadata for a capture."""
    ocr_source = path if truthy_env("URIRUN_SCANNER_OCR_FULLFRAME", "1") else display_path
    ocr = local_image_ocr(str(ocr_source), backend=None if archive else "tesseract")
    detected_document = extract_document_metadata(
        str(ocr.get("text") or ""),
        captured_at=payload.get("capturedAt"),
        image_path=str(path) if archive else None,
        use_llm=archive,
    )
    return ocr, detected_document


def refresh_best_ocr(fallback_ocr: dict, original_path: Path, display_path: Path,
                     *, local_image_ocr: Any, truthy_env: Any) -> dict:
    """Re-OCR the kept frame with the accurate full backend, falling back to the cheap read."""
    ocr_source = original_path if truthy_env("URIRUN_SCANNER_OCR_FULLFRAME", "1") else display_path
    refreshed = local_image_ocr(str(ocr_source))
    if refreshed.get("ok") and str(refreshed.get("text") or "").strip():
        return refreshed
    return fallback_ocr


def ensure_best_overlay(best: dict, crop: dict, quality: dict, original_path: Path) -> tuple[dict, str]:
    """Reuse the candidate's crop overlay if its file still exists, else render a fresh one."""
    overlay = best.get("overlay") if isinstance(best.get("overlay"), dict) else {}
    overlay_path = str(best.get("overlayPath") or overlay.get("path") or "")
    if not overlay_path or not Path(overlay_path).expanduser().is_file():
        overlay = scanner_crop_overlay(original_path, crop, quality)
        overlay_path = str(overlay.get("path") or "") if overlay.get("ok") else ""
        best["overlay"] = overlay
        best["overlayPath"] = overlay_path
    return overlay, overlay_path


def scanner_capture(
    project: str,
    db: "str | None",
    payload: dict,
    *,
    deps: ScannerBridgeDeps,
    archive_fn: "Callable[..., dict]",
    local_image_ocr_fn: "Callable[..., dict]",
    extract_document_metadata_fn: "Callable[..., dict]",
    truthy_env_fn: "Callable[[str, str], Any]",
    auto_crop_receipt_fn: "Callable[..., dict] | None" = None,
) -> dict:
    prune_scanner_staging(_scanner_staging_dir)
    mode = str(payload.get("mode") or "").lower()
    archive = not (payload.get("archive") is False or mode in {"candidate", "best-candidate", "analyze", "analysis"})
    mime, raw, digest, ext = _decode_capture_image(str(payload.get("image") or ""))
    root = _scanner_staging_dir()
    root.mkdir(parents=True, exist_ok=True)
    import time as _time  # noqa: PLC0415
    name = f"{_time.strftime('%Y%m%dT%H%M%SZ', _time.gmtime())}-phone-scan-{digest[:12]}{ext}"
    path = root / name
    path.write_bytes(raw)
    _crop_fn = auto_crop_receipt_fn if auto_crop_receipt_fn is not None else auto_crop_receipt
    crop = _crop_fn(path)
    display_path = capture_display_path(crop, path)
    ocr, detected_document = capture_ocr_and_detect(
        path, display_path, payload, archive,
        local_image_ocr=local_image_ocr_fn,
        extract_document_metadata=extract_document_metadata_fn,
        truthy_env=truthy_env_fn,
    )
    quality = document_frame_quality(crop, ocr, detected_document, display_path)
    overlay = scanner_crop_overlay(path, crop, quality)
    overlay_path = str(overlay.get("path") or "") if overlay.get("ok") else ""
    uri = f"scanner://host/capture/{digest[:16]}"
    document: dict = {"ok": False, "reason": "analysis-only", "metadata": detected_document}
    min_score = float(os.environ.get("URIRUN_PHONE_SCANNER_MIN_SCORE", "45"))
    if archive and not capture_quality_ok(payload, quality, min_score):
        return capture_reject_result(
            uri=uri, min_score=min_score, quality=quality, ocr=ocr, crop=crop, overlay=overlay,
            detected_document=detected_document, paths=[path, display_path, overlay_path],
        )
    if archive:
        try:
            document = archive_fn(
                display_path=display_path,
                original_path=path,
                ocr=ocr,
                crop=crop,
                source_sha256=digest,
                captured_at=payload.get("capturedAt"),
                metadata=detected_document,
            )
        except Exception as exc:  # noqa: BLE001
            document = {"ok": False, "error": str(exc), "metadata": detected_document}
    if not archive:
        return capture_candidate_result(
            project, payload, uri=uri, mime=mime, digest=digest, raw_len=len(raw), path=path,
            display_path=display_path, overlay_path=overlay_path, overlay=overlay, crop=crop,
            ocr=ocr, detected_document=detected_document, quality=quality,
            preview_url=deps.preview_url,
        )
    meta = {
        "source": payload.get("source") or "phone",
        "width": payload.get("width"),
        "height": payload.get("height"),
        "mime": mime,
        "sha256": digest,
        "bytes": len(raw),
        "originalPath": str(path),
        "displayPath": str(display_path),
        "overlayPath": overlay_path,
        "overlay": overlay,
        "crop": crop,
        "capturedAt": payload.get("capturedAt"),
        "userAgent": payload.get("userAgent", ""),
        "ocr": ocr,
        "detectedDocument": detected_document,
        "quality": quality,
        "document": document,
    }
    registered = register_scanner_result(
        deps, project, db,
        uri=uri, display_path=display_path, original_path=path,
        meta=meta, crop=crop, ocr=ocr, document=document, content_prefix="Phone scan saved",
    )
    return {
        "ok": True,
        "uri": uri,
        "artifact": registered["artifact"],
        "scanArtifact": registered["scanArtifact"],
        "documentArtifact": registered["documentArtifact"],
        "primaryArtifact": registered["primaryArtifact"],
        "ocr": ocr,
        "detectedDocument": detected_document,
        "orientation": orientation_summary(crop),
        "quality": quality,
        "overlay": overlay,
        "document": document,
        "message": registered["message"],
    }


def scanner_best_finish(
    project: str,
    db: "str | None",
    payload: dict,
    *,
    deps: ScannerBridgeDeps,
    archive_fn: "Callable[..., dict]",
    local_image_ocr_fn: "Callable[..., dict]",
    truthy_env_fn: "Callable[[str, str], Any]",
) -> dict:
    prune_scanner_staging(_scanner_staging_dir)
    series_id = str(payload.get("seriesId") or "").strip()
    if not series_id:
        raise ValueError("seriesId is required")
    series = scanner_best_take(series_id, clear=payload.get("clear", True) is not False)
    if not series:
        return best_series_not_found(series_id)
    best = resolve_best_candidate(series)
    if not isinstance(best, dict):
        return best_finish_store_failure(series_id, series, status="failed",
                                        error="scanner best series has no candidates")
    quality = best.get("quality") if isinstance(best.get("quality"), dict) else {}
    quality_rejected, min_score = best_quality_rejected(payload, quality)
    if quality_rejected:
        return best_finish_store_failure(
            series_id, series, status="rejected",
            error="no reliable receipt or invoice candidate found",
            best=best, project=project, extra={"minScore": min_score},
            preview_url=deps.preview_url,
        )
    original_path, display_path = best_candidate_paths(best)
    if not original_path.is_file() or not display_path.is_file():
        return best_finish_store_failure(series_id, series, status="failed",
                                         error="best candidate file is missing",
                                         best=best, project=project, preview_url=deps.preview_url)
    crop, ocr = best_crop_and_ocr(best)
    ocr = refresh_best_ocr(ocr, original_path, display_path,
                           local_image_ocr=local_image_ocr_fn, truthy_env=truthy_env_fn)
    digest = str(best.get("sha256") or _file_sha256(original_path))
    detected_document = best.get("detectedDocument") or {}
    try:
        document = archive_fn(
            display_path=display_path,
            original_path=original_path,
            ocr=ocr,
            crop=crop,
            source_sha256=digest,
            captured_at=str(best.get("capturedAt") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        document = {"ok": False, "error": str(exc), "metadata": detected_document}
    uri = str(best.get("uri") or f"scanner://host/capture/{digest[:16]}")
    overlay, overlay_path = ensure_best_overlay(best, crop, quality, original_path)
    meta = {
        "source": "phone-best",
        "seriesId": series_id,
        "frameIndex": best.get("frameIndex"),
        "candidateCount": len(series.get("candidates", [])),
        "width": best.get("width"),
        "height": best.get("height"),
        "mime": best.get("mime"),
        "sha256": digest,
        "bytes": best.get("bytes"),
        "originalPath": str(original_path),
        "displayPath": str(display_path),
        "overlayPath": overlay_path,
        "overlay": overlay,
        "crop": crop,
        "capturedAt": best.get("capturedAt"),
        "userAgent": best.get("userAgent", ""),
        "ocr": ocr,
        "detectedDocument": detected_document,
        "quality": quality,
        "document": document,
    }
    registered = register_scanner_result(
        deps, project, db,
        uri=uri, display_path=display_path, original_path=original_path,
        meta=meta, crop=crop, ocr=ocr, document=document, content_prefix="Best phone scan saved",
    )
    store_best_finish(series, series_id, best, document, registered)
    return {
        "ok": True,
        "seriesId": series_id,
        "best": scanner_public_candidate_for_live(best, project, preview_url=deps.preview_url),
        "uri": uri,
        "artifact": registered["artifact"],
        "scanArtifact": registered["scanArtifact"],
        "documentArtifact": registered["documentArtifact"],
        "primaryArtifact": registered["primaryArtifact"],
        "ocr": ocr,
        "detectedDocument": detected_document,
        "orientation": orientation_summary(crop),
        "quality": quality,
        "overlay": overlay,
        "document": document,
        "message": registered["message"],
    }


_LAST_STAGING_PRUNE: float = 0.0


def staging_keep_paths() -> set[str]:
    """Resolved paths that pruning must never remove: files of an archived document and of any
    active (not-yet-finished) best-frame series. Best-effort; a read failure just keeps less."""
    keep: set[str] = set()
    try:
        for doc in _load_document_index().get("documents", []):
            if isinstance(doc, dict):
                for key in ("originalPath", "cropPath"):
                    if doc.get(key):
                        keep.add(str(Path(str(doc[key])).expanduser().resolve()))
    except Exception:  # noqa: BLE001
        pass
    try:
        with SCANNER_BEST_LOCK:
            sessions = list(SCANNER_BEST_SESSIONS.values())
        for session in sessions:
            for cand in (session.get("candidates") or []):
                if isinstance(cand, dict):
                    for key in ("originalPath", "displayPath", "overlayPath"):
                        if cand.get(key):
                            keep.add(str(Path(str(cand[key])).expanduser().resolve()))
    except Exception:  # noqa: BLE001
        pass
    return keep


def prune_scanner_staging(staging_dir_fn: Any, *, min_interval: float = 60.0) -> int:
    """Drop stale candidate frames from the staging dir, keeping a safety window."""
    global _LAST_STAGING_PRUNE
    now = time.time()
    if now - _LAST_STAGING_PRUNE < min_interval:
        return 0
    _LAST_STAGING_PRUNE = now
    keep_recent = float(os.environ.get("URIRUN_SCANNER_KEEP_RECENT", "90"))
    if keep_recent <= 0:
        return 0
    try:
        staging = staging_dir_fn()
    except Exception:  # noqa: BLE001
        return 0
    if not staging.is_dir():
        return 0
    keep = staging_keep_paths()
    cutoff = now - keep_recent
    removed = 0
    try:
        entries = list(staging.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if str(entry.resolve()) in keep:
                continue
            if entry.stat().st_mtime > cutoff:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed
