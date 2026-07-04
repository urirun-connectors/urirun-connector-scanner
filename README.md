# urirun-connector-scanner

**Phone scanner, document archive & sync** — connector ekosystemu
[ifURI / urirun](https://github.com/if-uri/urirun).
Schematy URI: `scanner://`, `document://`, `dashboard://` (phone-scanner / QR)

Capture documents from a phone over the LAN, run the OCR + metadata pipeline,
deduplicate and archive them, and sync the archive to a node — all addressed by
URI. This package holds the scanner pipeline, the document archive/sync logic, and
the artifact-admin helpers.

## Opis

The scanner pipeline ingests a phone capture, runs OCR + LLM/regex metadata
extraction, fingerprints the result for dedup, registers it as an artifact, and can
sync the document archive to another node.

Routes / URIs produced and served:

| URI | Co robi |
| --- | --- |
| `scanner://host/capture/<digest>` | a captured frame (artifact URI) |
| `scanner://host/session/<digest>` | a scan session |
| `document://host/...` | an archived document |
| `document://host/archive/command/sync-to-node` | sync the document archive to a node |
| `dashboard://host/phone-scanner/command/start` | start the phone-scanner service |
| `dashboard://host/service/phone-scanner/command/restart` | restart it |
| `dashboard://host/qr/...` | LAN QR codes (open the scanner on a phone) |
| `fs://{target}/file/command/write-b64` · `query/read-b64` | move small PDFs to/from a node |

Modules: `scanner_bridge.py` (pipeline + artifact registration), `scanner_service.py`
/ `scanner_net.py` (phone-scanner service + LAN URLs), `document_sync.py` +
`document_metadata.py` (archive + metadata), `artifacts_admin.py` (artifact admin).

## Wiring

This package is a **library**, not a standalone `urirun.bindings` scheme connector
(so it has no `urirun.bindings` entry point — that is by design, not a gap). It is
consumed by the **`urirun-service-scanner`** service, which registers under
`[project.entry-points."urirun.services"]` and whose `service_manifest()` declares
the `scanner://` / `dashboard://` / `service://` routes. The phone scanner runs on
`:8196`; run the service (`urirun-service-scanner serve`) to expose the routes, which
the host dashboard also drives.

The library deliberately does **not** depend on the hub `urirun` package. That keeps
the dependency edge clean so the hub can later replace its bundled
`urirun_scanner/*` fallback with thin shims to this package without creating a
release-cycle (`urirun -> scanner -> urirun`).

## Wymagania

- **python:** Python 3.10+
- **optional:** PaddleOCR / Tesseract for OCR; an LLM (`llm://`) for metadata
  extraction (regex fallback otherwise); `urirun-artifacts` for the archive schema.

## Instalacja (dev)

```bash
pip install -e .
PYTHONPATH=. pytest -q
```

## Powiązane

- [urirun](https://github.com/if-uri/urirun) — rdzeń ekosystemu
- [urirun-connector-ocr](https://github.com/if-uri/urirun-connector-ocr) · [urirun-connector-smart-crop](https://github.com/if-uri/urirun-connector-smart-crop) — pipeline OCR/crop
- [urirun-connector-docid](https://github.com/if-uri/urirun-connector-docid) — dedup / document identity
