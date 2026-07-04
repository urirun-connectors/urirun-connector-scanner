from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .document_sync import document_index_path, scanned_id_log_path


def artifact_delete_roots(project: str) -> list[Path]:
    roots = [
        Path(os.environ.get("URIRUN_ARTIFACT_DIR", "~/.urirun/artifacts")).expanduser(),
        Path(os.environ.get("URIRUN_DOCUMENT_DIR", "~/.urirun/documents")).expanduser(),
        Path("~/.urirun/host-dashboard").expanduser(),
    ]
    out: list[Path] = []
    for root in roots:
        try:
            out.append(root.resolve())
        except OSError:
            continue
    return out


def artifact_file_delete_allowed(path: str, project: str) -> bool:
    if not path:
        return False
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        return False
    roots = artifact_delete_roots(project)
    return any(resolved == root or root in resolved.parents for root in roots)


def payload_bool(payload: dict, name: str, default: bool) -> bool:
    if name not in payload:
        return default
    value = payload.get(name)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def global_document_metadata_paths() -> set[Path]:
    paths: set[Path] = set()
    for candidate in (document_index_path(), scanned_id_log_path()):
        try:
            paths.add(candidate.expanduser().resolve())
        except OSError:
            continue
    return paths


def safe_artifact_sidecar_path(path: str | None, project: str) -> str | None:
    if not path:
        return None
    try:
        target = Path(str(path)).expanduser().resolve()
    except OSError:
        return None
    if target.suffix.lower() != ".json":
        return None
    if target in global_document_metadata_paths():
        return None
    if not artifact_file_delete_allowed(str(target), project):
        return None
    return str(target)


def artifact_delete_candidate_paths(item: dict, project: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    artifact_path = str(item.get("path") or "")
    if artifact_path:
        out.append((artifact_path, "artifact"))
        try:
            sibling = Path(artifact_path).expanduser().resolve().with_suffix(".json")
            if sibling.is_file():
                sidecar = safe_artifact_sidecar_path(str(sibling), project)
                if sidecar:
                    out.append((sidecar, "sidecar"))
        except OSError:
            pass
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    document = meta.get("document") if isinstance(meta.get("document"), dict) else {}
    for candidate in (meta.get("jsonPath"), document.get("jsonPath")):
        sidecar = safe_artifact_sidecar_path(str(candidate or ""), project)
        if sidecar:
            out.append((sidecar, "sidecar"))
    return out


def delete_one_artifact_file(artifact_path: str, role: str, project: str) -> dict:
    info = {"path": artifact_path, "role": role, "deleted": False, "skipped": False, "error": ""}
    if not artifact_file_delete_allowed(artifact_path, project):
        info["skipped"] = True
        info["error"] = "path is outside allowed artifact roots"
        return info
    try:
        target = Path(artifact_path).expanduser().resolve()
        if target.is_file():
            target.unlink()
            info["deleted"] = True
        else:
            info["skipped"] = True
            info["error"] = "file missing"
    except OSError as exc:
        info["error"] = str(exc)
    return info


def delete_artifact_files(artifacts: list, project: str) -> list[dict]:
    files: list[dict] = []
    seen_paths: set[str] = set()
    for item in artifacts:
        for artifact_path, role in artifact_delete_candidate_paths(item, project):
            if not artifact_path or artifact_path in seen_paths:
                continue
            seen_paths.add(artifact_path)
            files.append(delete_one_artifact_file(artifact_path, role, project))
    return files


def artifact_visual_path(artifact: dict) -> str:
    path = str(artifact.get("path") or "")
    meta = artifact.get("meta") if isinstance(artifact.get("meta"), dict) else {}
    if path.lower().endswith(".pdf"):
        return str(meta.get("displayImage") or meta.get("displayPath") or meta.get("previewImage") or meta.get("image") or "")
    return path


def artifact_file_exists(path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).expanduser().resolve().is_file()
    except Exception:  # noqa: BLE001
        return False


def artifact_dedupe_key(item: dict) -> tuple[str, str]:
    path = str(item.get("path") or item.get("visualPath") or "")
    if path:
        try:
            return ("path", str(Path(path).expanduser().resolve()))
        except Exception:  # noqa: BLE001
            return ("path", str(Path(path).expanduser()))
    uri = str(item.get("uri") or item.get("id") or "")
    return ("uri", uri)


def artifact_dedupe_rank(item: dict) -> tuple[int, int, str]:
    kind_rank = {
        "document-pdf": 0,
        "camera-scan": 1,
        "receipt-crop": 2,
        "dashboard-qr": 3,
    }
    missing_rank = 0 if item.get("fileExists") or item.get("previewExists") else 10
    return (missing_rank, kind_rank.get(str(item.get("kind") or ""), 5), str(item.get("created_at") or ""))


def merge_artifact_group(group: list[dict]) -> dict:
    if len(group) == 1:
        return group[0]
    keep = sorted(group, key=artifact_dedupe_rank)[0].copy()
    keep_id = str(keep.get("id") or "")
    keep["duplicateCount"] = len(group)
    keep["duplicateIds"] = [str(item.get("id")) for item in group if item.get("id") and str(item.get("id")) != keep_id]
    keep["duplicateArtifactIds"] = [str(item.get("id")) for item in group if item.get("id")]
    keep["duplicateUris"] = [
        str(item.get("uri"))
        for item in group
        if item.get("uri") and str(item.get("uri")) != str(keep.get("uri") or "")
    ]
    return keep


def preview_url(path: str, project: str) -> str | None:
    try:
        source = Path(path).expanduser().resolve()
        in_temp = source.parent == Path(tempfile.gettempdir()) and source.name.startswith("urirun-")
        roots = [
            Path(project).expanduser().resolve(),
            Path("~/.urirun").expanduser().resolve(),
            Path(os.environ.get("URIRUN_ARTIFACT_DIR", "~/.urirun/artifacts")).expanduser().resolve(),
        ]
        if source.is_file() and (in_temp or any(source == root or source.is_relative_to(root) for root in roots)):
            return f"/api/file?path={quote(str(source))}"
    except Exception:  # noqa: BLE001
        return None
    return None


def public_artifact(artifact: dict, project: str) -> dict:
    path = str(artifact.get("path") or "")
    visual_path = artifact_visual_path(artifact)
    file_preview = preview_url(path, project) if path else None
    visual_preview = preview_url(visual_path, project) if visual_path else None
    return {
        **artifact,
        "fileExists": artifact_file_exists(path),
        "previewExists": artifact_file_exists(visual_path),
        "visualPath": visual_path,
        "filePreviewUrl": file_preview or "",
        "previewUrl": visual_preview or "",
    }


def public_artifacts(artifacts: list[dict], project: str) -> list[dict]:
    return [public_artifact(artifact, project) for artifact in artifacts]


def attachment_visual_path(meta: dict) -> str:
    return str(meta.get("displayImage") or meta.get("displayPath") or meta.get("previewImage") or meta.get("image") or "")


def apply_attachment_file_fields(item: dict, path: str, file_preview: str | None) -> None:
    if path:
        item["fileExists"] = bool(file_preview)
        item["filePreviewUrl"] = file_preview or ""


def apply_attachment_visual_fields(item: dict, visual_path: str, visual_preview: str | None) -> None:
    if visual_path:
        item["previewExists"] = bool(visual_preview)
        item["visualPath"] = visual_path
        item["visualPreviewUrl"] = visual_preview or ""


def public_chat_attachment(attachment: dict, project: str) -> dict:
    """Normalize old chat attachments so the UI never embeds stale /api/file links."""
    item = dict(attachment or {})
    # Widget / plan attachments carry a label in "path", not a real filesystem path —
    # skip file-presence logic so the renderer displays them by kind, not file state.
    if item.get("kind") in {"twin-monitor", "twin-plan"}:
        return item
    path = str(item.get("path") or "")
    file_preview = preview_url(path, project) if path else None
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    visual_path = attachment_visual_path(meta)
    visual_preview = preview_url(visual_path, project) if visual_path else None
    apply_attachment_file_fields(item, path, file_preview)
    apply_attachment_visual_fields(item, visual_path, visual_preview)
    prev = str(item.get("previewUrl") or "")
    if prev.startswith("/api/file?path="):
        item["previewUrl"] = file_preview or ""
    elif not prev and file_preview:
        item["previewUrl"] = file_preview
    return item


def public_chat_attachments(attachments: Any, project: str) -> list[dict]:
    if not isinstance(attachments, list):
        return []
    return [public_chat_attachment(item, project) for item in attachments if isinstance(item, dict)]


def dedupe_public_artifacts(public: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for item in public:
        key = artifact_dedupe_key(item)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    return [merge_artifact_group(groups[key]) for key in order]


def visible_public_artifacts(
    artifacts: list[dict],
    project: str,
    *,
    include_missing: bool = False,
    include_duplicates: bool = False,
) -> list[dict]:
    public = public_artifacts(artifacts, project)
    if not include_missing:
        public = [item for item in public if item.get("fileExists") or item.get("previewExists")]
    if include_duplicates:
        return public
    return dedupe_public_artifacts(public)


_ATTACHMENT_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_ATTACHMENT_TAGGED_KINDS = {"screenshot", "photo", "image", "scan", "crop", "artifact"}
_ATTACHMENT_SKIP_KEYS = {"bytes_b64", "base64", "data", "routing", "contract", "examples", "inputSchema"}
_ATTACHMENT_HINT_WORDS = ("photo", "image", "screenshot", "artifact", "scan")


def _add_attachment(
    path: str, *, kind: str = "file", meta: dict | None = None, uri: str = "",
    attachments: list, seen: set, limit: int, project: str,
) -> None:
    if not path or path in seen or len(attachments) >= limit:
        return
    seen.add(path)
    _ext_img = Path(path).suffix.lower() in _ATTACHMENT_IMAGE_EXTS
    item: dict = {
        "kind": kind if (kind and kind not in {"file", ""}) else ("image" if _ext_img else "file"),
        "path": path, "uri": uri, "meta": meta or {},
    }
    prev = preview_url(path, project)
    if prev:
        item["previewUrl"] = prev
    attachments.append(item)


def _walk_attachments(
    node: Any, hint: str,
    attachments: list, seen: set, limit: int, project: str,
) -> None:
    if len(attachments) >= limit:
        return
    if isinstance(node, dict):
        if node.get("artifactPath"):
            _add_attachment(str(node["artifactPath"]), kind="artifact", meta=node,
                            uri=str(node.get("uri") or ""),
                            attachments=attachments, seen=seen, limit=limit, project=project)
        node_kind = str(node.get("kind") or "")
        _node_tags = node.get("_tags") or []
        if node.get("path") and (
            any(word in hint.lower() for word in _ATTACHMENT_HINT_WORDS)
            or node_kind in _ATTACHMENT_TAGGED_KINDS
            or any(t in _ATTACHMENT_TAGGED_KINDS for t in (_node_tags if isinstance(_node_tags, list) else []))
        ):
            _add_attachment(str(node["path"]), kind=node_kind or hint or "file", meta=node,
                            uri=str(node.get("uri") or ""),
                            attachments=attachments, seen=seen, limit=limit, project=project)
        if node.get("cropPath"):
            _add_attachment(str(node["cropPath"]), kind="crop", meta=node,
                            attachments=attachments, seen=seen, limit=limit, project=project)
        for key in ("photo", "screenshot", "image", "object", "inspection"):
            child = node.get(key)
            if isinstance(child, dict):
                _walk_attachments(child, key, attachments, seen, limit, project)
            elif isinstance(child, str) and ("/" in child or "\\" in child):
                _add_attachment(child, kind=key,
                                attachments=attachments, seen=seen, limit=limit, project=project)
        for key, child in node.items():
            if key in _ATTACHMENT_SKIP_KEYS:
                continue
            _walk_attachments(child, str(key), attachments, seen, limit, project)
    elif isinstance(node, list):
        for item in node:
            _walk_attachments(item, hint, attachments, seen, limit, project)


def collect_attachments(value: Any, project: str, *, limit: int = 24) -> list[dict]:
    """Find screenshot/photo/OCR artifacts in a URI result tree for chat rendering."""
    attachments: list[dict] = []
    seen: set[str] = set()
    _walk_attachments(value, "", attachments, seen, limit, project)
    return attachments


def iter_orphan_candidates(roots: list, seen: set, global_metadata: set):
    """Yield resolved ``*.json`` sidecar paths under roots, skipping the index and known metadata."""
    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if not resolved_root.is_dir():
            continue
        for candidate in resolved_root.rglob("*.json"):
            try:
                target = candidate.resolve()
            except OSError:
                continue
            if target in seen or target in global_metadata or target.name == "index.json":
                continue
            seen.add(target)
            yield target




def cleanup_one_sidecar(target: Path, project: str, *, delete_files: bool, sibling_suffixes: tuple) -> dict | None:
    """Return a delete-info record for an orphan sidecar, or None when it still has a real sibling."""
    if not artifact_file_delete_allowed(str(target), project):
        return {"path": str(target), "role": "orphan-sidecar", "deleted": False, "skipped": True, "error": "path is outside allowed artifact roots"}
    siblings = [target.with_suffix(suffix) for suffix in sibling_suffixes]
    if any(path.is_file() for path in siblings):
        return None
    info = {"path": str(target), "role": "orphan-sidecar", "deleted": False, "skipped": False, "error": ""}
    if delete_files:
        try:
            target.unlink()
            info["deleted"] = True
        except OSError as exc:
            info["error"] = str(exc)
    else:
        info["skipped"] = True
        info["error"] = "dry run"
    return info




def artifacts_delete(host_db: "Any", project: str, db: "str | None", payload: dict) -> dict:
    ids = payload.get("ids") or payload.get("artifactIds") or []
    if isinstance(ids, str):
        ids = [ids]
    clean_ids = [str(item).strip() for item in ids if str(item).strip()]
    if not clean_ids:
        return {"ok": False, "error": "ids are required", "deleted": 0, "filesDeleted": 0}
    artifacts = host_db.artifacts_by_ids(db, clean_ids)
    files = delete_artifact_files(artifacts, project) if payload_bool(payload, "deleteFiles", True) else []
    deleted = host_db.delete_artifacts(db, clean_ids)
    result = {
        "ok": True,
        "requested": len(clean_ids),
        "matched": len(artifacts),
        "deleted": deleted,
        "filesDeleted": len([item for item in files if item.get("deleted")]),
        "files": files,
    }
    try:
        host_db.add_log(db, "artifacts", "delete", result)
    except Exception:  # noqa: BLE001
        pass
    return result


def artifacts_dedupe_rows(host_db: "Any", project: str, db: "str | None", payload: dict) -> dict:
    """Remove duplicate artifact DB rows that point at the same physical output."""
    limit = int(payload.get("limit") or 10_000)
    limit = max(1, min(limit, 50_000))
    delete_rows = payload_bool(payload, "deleteRows", True)
    pub = public_artifacts(host_db.list_artifacts(db, limit=limit), project)
    groups: "dict[tuple[str, str], list[dict]]" = {}
    for item in pub:
        key = artifact_dedupe_key(item)
        if not key[1]:
            continue
        groups.setdefault(key, []).append(item)

    duplicate_groups: "list[dict]" = []
    delete_ids: "list[str]" = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        keep = sorted(group, key=artifact_dedupe_rank)[0]
        keep_id = str(keep.get("id") or "")
        duplicate_ids = [
            str(item.get("id"))
            for item in group
            if item.get("id") and str(item.get("id")) != keep_id
        ]
        if not duplicate_ids:
            continue
        delete_ids.extend(duplicate_ids)
        duplicate_groups.append({
            "key": {"type": key[0], "value": key[1]},
            "keepId": keep_id,
            "keepKind": keep.get("kind"),
            "deleteIds": duplicate_ids,
            "count": len(group),
        })

    deleted = host_db.delete_artifacts(db, delete_ids) if delete_rows and delete_ids else 0
    result = {
        "ok": True,
        "scanned": len(pub),
        "groups": duplicate_groups,
        "duplicateRows": len(delete_ids),
        "deleted": deleted,
        "dryRun": not delete_rows,
    }
    try:
        host_db.add_log(db, "artifacts", "dedupe", result)
    except Exception:  # noqa: BLE001
        pass
    return result


def artifacts_cleanup_orphan_sidecars(host_db: "Any", project: str, db: "str | None", payload: dict) -> dict:
    import os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    delete_files = payload_bool(payload, "deleteFiles", True)
    include_artifact_dir = payload_bool(payload, "includeArtifactDir", False)
    roots = [Path(os.environ.get("URIRUN_DOCUMENT_DIR", "~/.urirun/documents")).expanduser()]
    if include_artifact_dir:
        roots.append(Path(os.environ.get("URIRUN_ARTIFACT_DIR", "~/.urirun/artifacts")).expanduser())
    global_metadata = global_document_metadata_paths()
    sibling_suffixes = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bin")
    files: "list[dict]" = []
    seen: "set[Path]" = set()
    for target in iter_orphan_candidates(roots, seen, global_metadata):
        info = cleanup_one_sidecar(target, project, delete_files=delete_files, sibling_suffixes=sibling_suffixes)
        if info is not None:
            files.append(info)
    result = {
        "ok": True,
        "filesDeleted": len([item for item in files if item.get("deleted")]),
        "files": files,
    }
    try:
        host_db.add_log(db, "artifacts", "cleanup-orphans", result)
    except Exception:  # noqa: BLE001
        pass
    return result
