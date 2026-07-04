from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from urirun_connector_scanner.document_sync import (
    boolish,
    document_archive_pdfs,
    document_sync_verification,
)


# ─── boolish ─────────────────────────────────────────────────────────────────

def test_boolish_true_values():
    for v in (True, "1", "true", "yes", "on", "YES", "True"):
        assert boolish(v) is True, v


def test_boolish_false_values():
    for v in (False, "0", "false", "no", "off", "", "FALSE"):
        assert boolish(v) is False, v


def test_boolish_none_uses_default():
    assert boolish(None, default=True) is True
    assert boolish(None, default=False) is False


# ─── document_archive_pdfs ───────────────────────────────────────────────────

def test_document_archive_pdfs_finds_nested_pdfs():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        subdir = root / "2024-01"
        subdir.mkdir()
        (subdir / "receipt.pdf").write_bytes(b"%PDF")
        (subdir / "notes.txt").write_bytes(b"text")
        result = document_archive_pdfs(root)
        assert len(result) == 1
        assert result[0].name == "receipt.pdf"


def test_document_archive_pdfs_excludes_no_invoice():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        no_inv = root / "no_invoice"
        no_inv.mkdir()
        (no_inv / "excluded.pdf").write_bytes(b"%PDF")
        good = root / "2024-01"
        good.mkdir()
        (good / "included.pdf").write_bytes(b"%PDF")
        result = document_archive_pdfs(root)
        assert len(result) == 1
        assert result[0].name == "included.pdf"


def test_document_archive_pdfs_missing_dir():
    assert document_archive_pdfs(Path("/nonexistent/dir")) == []


def test_document_archive_pdfs_returns_sorted():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for name in ("zz", "aa", "mm"):
            (root / name).mkdir()
            (root / name / "file.pdf").write_bytes(b"%PDF")
        result = document_archive_pdfs(root)
        assert [r.parent.name for r in result] == sorted(["zz", "aa", "mm"])


# ─── document_sync_verification ──────────────────────────────────────────────

def _make_files(root: Path, relative_paths: list[str]) -> list[Path]:
    paths = []
    for rel in relative_paths:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF")
        paths.append(p)
    return paths


def test_sync_verification_all_uploaded_and_verified():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        files = _make_files(root, ["2024-01/a.pdf", "2024-01/b.pdf"])
        results = [
            {"relativePath": "2024-01/a.pdf", "writeOk": True, "verified": True},
            {"relativePath": "2024-01/b.pdf", "writeOk": True, "verified": True},
        ]
        v = document_sync_verification(files, results, source_root=root, read_back=True)
        assert v["ok"] is True
        assert v["expectedFiles"] == 2
        assert v["verifiedFiles"] == 2
        assert v["mode"] == "read-back-sha256"


def test_sync_verification_partial_upload_fails():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        files = _make_files(root, ["2024-01/a.pdf", "2024-01/b.pdf"])
        results = [{"relativePath": "2024-01/a.pdf", "writeOk": True, "verified": True}]
        v = document_sync_verification(files, results, source_root=root, read_back=False)
        assert v["ok"] is False
        assert v["failedFiles"] == 1
        assert "2024-01/b.pdf" in v["missing"]


def test_sync_verification_write_ack_mode():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        files = _make_files(root, ["2024-01/x.pdf"])
        results = [{"relativePath": "2024-01/x.pdf", "writeOk": True, "verified": False}]
        v = document_sync_verification(files, results, source_root=root, read_back=False)
        assert v["mode"] == "write-ack-sha256"
