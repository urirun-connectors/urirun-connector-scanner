# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
"""Document OCR + LLM metadata extraction for the host dashboard scanner pipeline.

Extracted from host_dashboard.py: the OCR cascade (tesseract / urirun-connector-ocr /
vision-LLM fallback), receipt/invoice field parsing (date / amount / type / contractor), and
the LLM structured-extraction path. A self-contained leaf module; host_dashboard re-exports
these names so existing call sites keep working.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

try:
    from docid.dedup import normalize_text as _dedup_normalize_text
except Exception:  # noqa: BLE001 - docid is optional; metadata still works without it.
    _dedup_normalize_text = None

from .document_sync import truthy_env as _truthy_env

def _local_image_ocr_tesseract(path: str) -> dict:
    if not shutil_which("tesseract"):
        return {"ok": False, "backend": "none", "error": "tesseract is not installed on host"}
    import subprocess

    proc = subprocess.run(["tesseract", path, "stdout", "-l", "eng+pol"],
                          capture_output=True, text=True, timeout=90, check=False)
    if proc.returncode != 0:
        proc = subprocess.run(["tesseract", path, "stdout"],
                              capture_output=True, text=True, timeout=90, check=False)
    if proc.returncode != 0:
        return {"ok": False, "backend": "tesseract", "error": (proc.stderr or "").strip()}
    text = proc.stdout.strip()
    return {"ok": True, "backend": "tesseract", "text": text, "chars": len(text)}

def _ocr_text_ok(result: dict | None) -> bool:
    """True when an OCR result envelope actually carries usable (non-blank) text."""
    return bool(result and result.get("ok") and str(result.get("text") or "").strip())

def _ocr_connector_envelope(path: str, backend: str) -> tuple[dict | None, dict | None]:
    """Run the urirun-connector-ocr read. Returns ``(envelope, None)`` on a successful call,
    or ``(None, finished)`` where ``finished`` is a ready tesseract-fallback result when the
    connector is unavailable or raised."""
    try:
        from urirun_connector_ocr.core import image_text  # type: ignore
    except Exception as exc:  # noqa: BLE001
        result = _local_image_ocr_tesseract(path)
        result.setdefault("connectorError", f"urirun-connector-ocr unavailable: {exc}")
        return None, result
    try:
        envelope = image_text(image=path, backend=backend, lang="eng+pol", max_chars=20000)
    except Exception as exc:  # noqa: BLE001
        result = _local_image_ocr_tesseract(path)
        result.setdefault("connectorError", str(exc))
        return None, result
    return envelope, None

def _local_image_ocr(path: str, backend: str | None = None) -> dict:
    """OCR a scanned image for the phone-scanner pipeline.

    Prefers the urirun-connector-ocr ``auto`` cascade, whose first backend is PaddleOCR
    (PP-OCRv5/v6 det+rec with document orientation + dewarping). PaddleOCR reads Polish
    receipts on the *full frame* far more reliably than plain tesseract and does not lose
    the header/footer to an aggressive crop. Falls back to direct tesseract, then — when both
    paddle and tesseract come back empty — to a vision-LLM read (`_local_image_ocr_llm`), so a
    scan never yields empty text. Set ``URIRUN_SCANNER_OCR_BACKEND=tesseract`` to force the old
    path; ``URIRUN_SCANNER_OCR_LLM_FALLBACK=0`` to disable the LLM last resort.

    ``backend`` overrides the env default for one call. The live "best frame" loop scores
    transient candidates with the cheap ``tesseract`` backend and only pays for the full
    paddle read on the document it actually keeps (manual Scan, or the chosen best frame),
    so a 30s/frame OCR never piles up behind the ~3s capture interval.
    """
    backend = str(backend if backend is not None else os.environ.get("URIRUN_SCANNER_OCR_BACKEND", "auto")).strip().lower()
    if backend in {"", "tesseract"}:
        return _local_image_ocr_tesseract(path)
    envelope, finished = _ocr_connector_envelope(path, backend)
    if finished is not None:  # connector unavailable / errored — tesseract fallback already built
        return finished
    if _ocr_text_ok(envelope):
        return {
            "ok": True,
            "backend": envelope.get("backend", backend),
            "text": str(envelope.get("text") or ""),
            "chars": envelope.get("chars") or len(str(envelope.get("text") or "")),
            "boxCount": envelope.get("box_count"),
            "docPreprocess": envelope.get("docPreprocess"),
        }
    # Connector found nothing usable; fall back to tesseract so a scan never silently fails.
    fallback = _local_image_ocr_tesseract(path)
    if _ocr_text_ok(fallback):
        return fallback
    # Last resort: read the image with a vision LLM. Covers the case where paddle is broken
    # AND tesseract is missing/blank — the scan still yields text instead of empty metadata.
    llm = _local_image_ocr_llm(path)
    if _ocr_text_ok(llm):
        return llm
    if not fallback.get("ok"):
        fallback.setdefault("connectorError", str(envelope.get("error") or "connector OCR returned no text"))
    return fallback

def _local_image_ocr_llm(path: str) -> dict | None:
    """OCR an image with a vision LLM — the final fallback when paddle and tesseract fail.

    Returns ``None`` when disabled (``URIRUN_SCANNER_OCR_LLM_FALLBACK=0``) or no vision
    model/key is configured, so it is always a safe last resort. Uses the same model
    resolution as the metadata extractor (``URIRUN_SCANNER_LLM_VISION_MODEL`` /
    ``URIRUN_SCANNER_LLM_MODEL`` / ``LLM_MODEL``).
    """
    if not _truthy_env("URIRUN_SCANNER_OCR_LLM_FALLBACK", "1"):
        return None
    if not (path and Path(str(path)).is_file()):
        return None
    model = _llm_model(vision=True)
    if not model:
        return None
    key_ref = _llm_api_key_ref()
    if model.startswith("openrouter/") and not key_ref:
        return None
    try:
        from urirun_connector_llm.core import complete  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    prompt = (
        "Przepisz CAŁY tekst z tego paragonu/faktury dokładnie tak jak widać, linia po linii. "
        "Zwróć wyłącznie tekst, bez komentarzy."
    )
    try:
        # The key is passed by reference and resolved inside the connector under a
        # deny-by-default allow-list — never copied into this process's environment.
        res = complete(prompt, model=model, image=str(path), api_key=key_ref, secret_allow=key_ref)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(res, dict) or not res.get("ok"):
        return None
    text = str(res.get("response") or "").strip()
    if not text:
        return None
    return {"ok": True, "backend": "llm-vision", "text": text, "chars": len(text), "model": model}

def _normalized_document_text(text: str) -> str:
    if _dedup_normalize_text is not None:
        return _dedup_normalize_text(text)
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    folded = re.sub(r"[^a-zA-Z0-9.,:/@+\- ]+", " ", folded.lower())
    return re.sub(r"\s+", " ", folded).strip()


normalized_document_text = _normalized_document_text  # public alias used by document_sync


def _parse_document_date(text: str, fallback: str | None = None) -> str:
    candidates: list[date] = []
    # Guard ends with "not a digit" rather than \b: receipt OCR often glues the date to the
    # preceding word (e.g. "Betkowska06-03-2025"), where there is no word boundary between a
    # letter and a digit. (?<!\d)/(?!\d) still prevents slicing a date out of a longer number.
    for year, month, day in re.findall(r"(?<!\d)(20\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)", text):
        try:
            candidates.append(date(int(year), int(month), int(day)))
        except ValueError:
            pass
    for day, month, year in re.findall(r"(?<!\d)(\d{1,2})[-./](\d{1,2})[-./](20\d{2})(?!\d)", text):
        try:
            candidates.append(date(int(year), int(month), int(day)))
        except ValueError:
            pass
    if candidates:
        return min(candidates).isoformat()
    if fallback:
        match = re.search(r"(20\d{2}-\d{2}-\d{2})(?!\d)", fallback)
        if match:
            return match.group(1)
    return time.strftime("%Y-%m-%d", time.gmtime())

def _parse_amount(text: str) -> dict:
    amount_re = re.compile(r"(?<!\d)(\d{1,3}(?:[ \u00a0]?\d{3})*(?:[,.]\d{2})|\d+[,.]\d{2})(?!\d)")
    total_re = re.compile(r"(suma\s*pln|do\s*zap[lł]aty|razem\s*pln|razem|total\s*pln|warto[sś][cć]\s*brutto|nale[zż]no[sś][cć])", re.I)
    keyword_re = re.compile(r"(suma|do zaplaty|do zapłaty|naleznosc|należność|total|kwota|brutto)", re.I)
    tax_re = re.compile(r"(ptu|stawka\s*ptu|suma\s*ptu|vat\b|stawka\s*vat|opust|rabat|netto)", re.I)
    date_context_re = re.compile(r"\b(data|date|godzina|hour|czas|time)\b", re.I)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matches: list[tuple[int, float, str]] = []
    for idx, line in enumerate(lines):
        is_total = bool(total_re.search(line))
        is_tax = bool(tax_re.search(line))
        has_amount_keyword = bool(keyword_re.search(line))
        if date_context_re.search(line) and not (is_total or has_amount_keyword):
            continue
        for raw in amount_re.findall(line):
            normalized = raw.replace("\u00a0", "").replace(" ", "").replace(",", ".")
            try:
                value = float(normalized)
            except ValueError:
                continue
            if is_total:
                score = 50
            elif is_tax:
                score = -10
            elif has_amount_keyword:
                score = 15
            else:
                score = 0
            matches.append((score + idx, value, raw))
    if not matches:
        return {"amount": "", "currency": ""}
    best = max(matches, key=lambda item: (item[0], item[1]))
    return {"amount": f"{best[1]:.2f}", "currency": "PLN"}

def _document_type(text: str) -> str:
    lower = text.lower()
    if "paragon" in lower or "fiskal" in lower or "receipt" in lower:
        return "paragon"
    if "faktura" in lower or "invoice" in lower or ("nip" in lower and "vat" in lower):
        return "faktura"
    if "rachunek" in lower or "bill" in lower:
        return "rachunek"
    if "gwarancj" in lower:
        return "gwarancja"
    if "reklamacj" in lower:
        return "reklamacja"
    if "odstąpieni" in lower or "odstapieni" in lower:
        return "odstapienie"
    if "regulamin" in lower or "terms of service" in lower or "terms & conditions" in lower:
        return "regulamin"
    if "polityka prywatno" in lower or "privacy policy" in lower:
        return "polityka_prywatnosci"
    if any(k in lower for k in ("umowa o pracę", "umowa zlecenie", "umowa o dzieło", "umowa najmu", "umowa ramowa", "umowa spółki", "umowa sprzedaży", "umowa agencyjna")):
        return "umowa"
    payment_terms = ("contactless", "terminal", "karta", "kart", "obciazyc", "obciążyć", "eplatnosci", "epłatności")
    if any(term in lower for term in payment_terms):
        return "potwierdzenie"
    return "dokument"

def _parse_contractor(text: str) -> str:
    ignored = re.compile(
        r"^(faktura|paragon|rachunek|invoice|receipt|nip|vat|data|date|razem|suma|total|do zap|sprzedawca|nabywca|lp\.?|ilosc|ilość|cena|kwota|sprzedaz|sprzedaż)\b",
        re.I,
    )
    terminal_noise = re.compile(
        r"\b(pos\s*id|mid|aid|wazna\s*do|ważna\s*do|contactless|visa|uisa|mastercard|"
        r"polskie\s+e\s*p?[łl]atnosci|e\s*p?[łl]atnosci|podpis|autoryzacji|kod\s+autoryzacji)\b",
        re.I,
    )
    candidates: list[tuple[int, str]] = []
    for idx, raw in enumerate(text.splitlines()[:30]):
        line = re.sub(r"\s+", " ", raw.strip(" \t:-")).strip()
        if len(line) < 3 or len(line) > 70 or ignored.search(line):
            continue
        if terminal_noise.search(line):
            continue
        if not re.search(r"[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]", line):
            continue
        digit_ratio = sum(ch.isdigit() for ch in line) / max(1, len(line))
        if digit_ratio > 0.35:
            continue
        score = 100 - idx
        if re.search(r"\b(sp\.?|s\.a\.|s\.c\.|ltd|gmbh|inc|allegro|amazon|google|openai|microsoft|apple)\b", line, re.I):
            score += 30
        if line.upper() == line and len(line) >= 5:
            score += 8
        candidates.append((score, line))
    if not candidates:
        return "kontrahent-nieznany"
    return max(candidates, key=lambda item: item[0])[1]

_LLM_DOC_TYPES = ("paragon", "faktura", "rachunek", "potwierdzenie", "dokument")

def _load_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE .env reader (ignores comments / blanks / `export `)."""
    values: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                values[key] = val
    except OSError:
        return {}
    return values

def _llm_env_file() -> Path | None:
    """The .env that carries LLM config/credentials, if present.

    ``URIRUN_LLM_ENV_FILE``, then this repo's ``examples/.env``, then ``~/.urirun/llm.env``.
    Used both to read the (non-secret) model name and to *address* the API key by reference —
    never to copy the key into the process environment.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("URIRUN_LLM_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    try:
        candidates.append(Path(__file__).resolve().parents[5] / "examples" / ".env")
    except IndexError:
        pass
    candidates.append(Path("~/.urirun/llm.env").expanduser())
    for path in candidates:
        if path.is_file():
            return path
    return None

def _llm_model(*, vision: bool = False) -> str:
    """Resolve the LLM model name (config, not a secret).

    Env wins (``URIRUN_SCANNER_LLM_VISION_MODEL`` for the vision pass, then
    ``URIRUN_SCANNER_LLM_MODEL`` / ``LLM_MODEL``); otherwise ``LLM_MODEL`` is read from the
    .env file as plain config. The model name is never a credential, so reading it directly
    is fine — only the API key goes through the secret layer.
    """
    if vision and os.environ.get("URIRUN_SCANNER_LLM_VISION_MODEL"):
        return os.environ["URIRUN_SCANNER_LLM_VISION_MODEL"].strip()
    model = (os.environ.get("URIRUN_SCANNER_LLM_MODEL") or os.environ.get("LLM_MODEL") or "").strip()
    if model:
        return model
    env_file = _llm_env_file()
    if env_file:
        return str(_load_env_file(env_file).get("LLM_MODEL", "")).strip()
    return ""

def _llm_api_key_ref() -> str:
    """Return the API key as a *secret reference*, never the value.

    Honours ``URIRUN_SCANNER_LLM_API_KEY_REF`` (e.g. ``secret://keyring/openrouter#key``).
    Otherwise: if ``OPENROUTER_API_KEY`` is already in the process env, reference it with
    ``getv://OPENROUTER_API_KEY``; else point at the .env file via
    ``secret://dotenv/<file>#OPENROUTER_API_KEY``. Returns '' when nothing is configured.
    The value is resolved inside the llm connector under a deny-by-default allow-list — it is
    never copied into ``os.environ`` here.
    """
    explicit = os.environ.get("URIRUN_SCANNER_LLM_API_KEY_REF")
    if explicit:
        return explicit.strip()
    if os.environ.get("OPENROUTER_API_KEY"):
        return "getv://OPENROUTER_API_KEY"
    env_file = _llm_env_file()
    if env_file and "OPENROUTER_API_KEY" in _load_env_file(env_file):
        return f"secret://dotenv/{env_file}#OPENROUTER_API_KEY"
    return ""

def _coerce_amount(value: object) -> str:
    """Normalise an LLM-supplied amount to ``NNN.NN`` (or '' when not a number)."""
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    raw = raw.replace(" ", "").replace(" ", "")
    # Keep the last decimal separator, drop thousands separators.
    raw = re.sub(r"[^0-9,.\-]", "", raw)
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    else:
        raw = raw.replace(",", ".")
    try:
        return f"{float(raw):.2f}"
    except ValueError:
        return ""

_LLM_FIELDS_SPEC = (
    "Zwróć WYŁĄCZNIE obiekt JSON, bez komentarzy, z polami:\n"
    '{"type": jeden z ["paragon","faktura","rachunek","potwierdzenie","dokument"],\n'
    ' "date": data wystawienia/sprzedaży dokumentu w formacie YYYY-MM-DD (NIE dzisiejsza data),\n'
    ' "contractor": nazwa sprzedawcy/firmy (nie etykieta "Sprzedawca"),\n'
    ' "amount": kwota DO ZAPŁATY / SUMA / RAZEM jako liczba z kropką (np. "200.62"),\n'
    ' "currency": kod waluty ISO np. "PLN",\n'
    ' "nip": NIP sprzedawcy (same cyfry) lub "",\n'
    ' "number": numer dokumentu/faktury/paragonu lub ""}\n'
    "Gdy pola nie ma w dokumencie, użyj pustego stringa. Nie zgaduj daty — jeśli brak, zwróć \"\".\n"
)

def _llm_extract_metadata(ocr_text: str, *, captured_at: str | None = None,
                          image_path: str | None = None) -> dict | None:
    """Extract structured document fields with an LLM, from OCR text and/or the image itself.

    The regex parsers are brittle on real receipts (glued tokens, layout noise); an LLM reads
    the document in context and returns clean fields. Two modes:

    * **text** (default): the OCR text is sent to the model.
    * **vision** (``URIRUN_SCANNER_LLM_VISION=1``): the *image* is sent directly to a multimodal
      model (the OCR text, if any, rides along as a hint). This reads layout/totals the OCR may
      have mangled, and works even when OCR returned nothing.

    Returns ``None`` (caller keeps the regex result) when disabled, no model/key is configured,
    or the call/parse fails — always a safe augmentation, never a hard dependency. Pick the
    model with ``URIRUN_SCANNER_LLM_MODEL`` / ``LLM_MODEL`` (or ``URIRUN_SCANNER_LLM_VISION_MODEL``
    for the vision pass).
    """
    if not _truthy_env("URIRUN_SCANNER_LLM_EXTRACT", "1"):
        return None
    text = (ocr_text or "").strip()
    use_vision = bool(
        _truthy_env("URIRUN_SCANNER_LLM_VISION", "0")
        and image_path
        and Path(str(image_path)).is_file()
    )
    if not use_vision and len(text) < 8:
        return None
    model = _llm_model(vision=use_vision)
    if not model:
        return None
    key_ref = _llm_api_key_ref()
    if model.startswith("openrouter/") and not key_ref:
        return None
    res = _llm_complete_metadata(model, key_ref, text, use_vision=use_vision, image_path=image_path)
    data = _parse_llm_json_object(res)
    if data is None:
        return None
    return _normalize_llm_doc_fields(data, model=model, use_vision=use_vision)

def _llm_complete_metadata(model: str, key_ref: str | None, text: str, *,
                           use_vision: bool, image_path: str | None) -> dict | None:
    """Call the LLM connector (vision or text mode) and return its raw response envelope.

    The API key travels as a reference (getv:// or secret://dotenv/...) and is resolved inside
    the llm connector under a deny-by-default allow-list — never via os.environ here."""
    try:
        from urirun_connector_llm.core import complete  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    if use_vision:
        prompt = f"Przeanalizuj zdjęcie polskiego paragonu lub faktury i wyciągnij dane. {_LLM_FIELDS_SPEC}"
        if text:
            prompt += f"\nPomocniczy tekst z OCR (może zawierać błędy, zweryfikuj ze zdjęciem):\n{text[:3000]}"
        try:
            return complete(prompt, model=model, image=str(image_path), api_key=key_ref, secret_allow=key_ref)
        except Exception:  # noqa: BLE001
            return None
    prompt = (
        "Jesteś ekstraktorem danych z polskich paragonów i faktur. Poniżej tekst z OCR "
        f"(zachowana kolejność linii). {_LLM_FIELDS_SPEC}"
        f"\nTEKST OCR:\n{text[:6000]}"
    )
    try:
        return complete(prompt, model=model, api_key=key_ref, secret_allow=key_ref)
    except Exception:  # noqa: BLE001
        return None

def _parse_llm_json_object(res: Any) -> dict | None:
    """Pull the JSON object out of an LLM completion envelope (strips ```json fences)."""
    if not isinstance(res, dict) or not res.get("ok"):
        return None
    raw = str(res.get("response") or "").strip()
    if not raw:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    else:
        brace = re.search(r"\{.*\}", raw, re.S)
        if brace:
            raw = brace.group(0)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None

def _normalize_llm_doc_fields(data: dict, *, model: str, use_vision: bool) -> dict:
    """Coerce/validate the LLM's raw fields into the canonical document-metadata shape."""
    doc_type = str(data.get("type") or "").strip().lower()
    if doc_type not in _LLM_DOC_TYPES:
        doc_type = ""
    date_val = str(data.get("date") or "").strip()
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", date_val):
        date_val = ""
    else:
        try:
            date.fromisoformat(date_val)
        except ValueError:
            date_val = ""
    contractor = re.sub(r"\s+", " ", str(data.get("contractor") or "").strip())
    if len(contractor) > 70:
        contractor = contractor[:70].strip()
    amount = _coerce_amount(data.get("amount"))
    currency = re.sub(r"[^A-Za-z]", "", str(data.get("currency") or "")).upper()[:3]
    if amount and not currency:
        currency = "PLN"
    nip = re.sub(r"\D", "", str(data.get("nip") or ""))
    number = re.sub(r"\s+", " ", str(data.get("number") or "").strip())[:40]
    return {
        "type": doc_type,
        "date": date_val,
        "contractor": contractor,
        "amount": amount,
        "currency": currency,
        "nip": nip,
        "number": number,
        "model": model,
        "mode": "vision" if use_vision else "text",
    }

def _extract_document_metadata(ocr_text: str, *, captured_at: str | None = None,
                               image_path: str | None = None, use_llm: bool = True) -> dict:
    amount = _parse_amount(ocr_text)
    meta = {
        "type": _document_type(ocr_text),
        "date": _parse_document_date(ocr_text, captured_at),
        "contractor": _parse_contractor(ocr_text),
        "amount": amount["amount"],
        "currency": amount["currency"],
        "metaSource": "regex",
    }
    # LLM augmentation: an LLM reads the document in context and beats the regex parsers on
    # real-world receipts. With URIRUN_SCANNER_LLM_VISION=1 it reads the image directly. It
    # only overrides a field when it returns a confident value; everything it leaves blank
    # keeps the regex result. Failures fall back silently. ``use_llm=False`` keeps transient
    # candidate frames on the cheap regex path (no per-frame LLM cost in the live loop).
    llm = _llm_extract_metadata(ocr_text, captured_at=captured_at, image_path=image_path) if use_llm else None
    if llm:
        for key in ("type", "contractor", "amount", "currency", "date"):
            value = str(llm.get(key) or "").strip()
            if not value:
                continue
            if key == "type" and value == "dokument" and meta["type"] != "dokument":
                continue  # keep the more specific regex type over a generic LLM guess
            if key == "contractor" and value.lower() in {"kontrahent-nieznany", "sprzedawca", "sprzedauca"}:
                continue
            meta[key] = value
        for extra in ("nip", "number"):
            if str(llm.get(extra) or "").strip():
                meta[extra] = str(llm[extra]).strip()
        meta["metaSource"] = "llm"
        meta["llmModel"] = llm.get("model", "")
        meta["llmMode"] = llm.get("mode", "text")
    return meta

def shutil_which(binary: str) -> str | None:
    import shutil
    return shutil.which(binary)
