from __future__ import annotations

from urirun_connector_scanner.document_metadata import (
    _document_type,
    _parse_amount,
    _parse_contractor,
    _parse_document_date,
)


# ─── _parse_document_date ────────────────────────────────────────────────────

def test_parse_date_iso_format():
    assert _parse_document_date("Data: 2025-06-03") == "2025-06-03"


def test_parse_date_dmy_format():
    assert _parse_document_date("03.06.2025") == "2025-06-03"


def test_parse_date_slash_format():
    assert _parse_document_date("03/06/2025") == "2025-06-03"


def test_parse_date_picks_earliest():
    text = "Data wystawienia: 2025-06-15\nTermin platnosci: 2025-07-15"
    assert _parse_document_date(text) == "2025-06-15"


def test_parse_date_glued_to_word():
    # OCR often glues date to preceding text
    assert _parse_document_date("Betkowska06-03-2025") == "2025-03-06"


def test_parse_date_fallback_from_filename():
    result = _parse_document_date("brak daty", fallback="2025-01-15_receipt.jpg")
    assert result == "2025-01-15"


def test_parse_date_returns_today_when_no_match():
    import time
    result = _parse_document_date("brak daty")
    assert result == time.strftime("%Y-%m-%d", time.gmtime())


# ─── _parse_amount ───────────────────────────────────────────────────────────

def test_parse_amount_basic():
    result = _parse_amount("Razem: 123,45")
    assert result["amount"] == "123.45"
    assert result["currency"] == "PLN"


def test_parse_amount_total_keyword_wins():
    text = "Item 1: 10,00\nItem 2: 20,00\nRazem: 30,00"
    result = _parse_amount(text)
    assert result["amount"] == "30.00"


def test_parse_amount_no_match_returns_empty():
    result = _parse_amount("Brak kwoty")
    assert result["amount"] == ""
    assert result["currency"] == ""


def test_parse_amount_skips_date_context():
    text = "Data: 03.06.2025\nRazem: 99,00"
    result = _parse_amount(text)
    assert result["amount"] == "99.00"


def test_parse_amount_thousand_separator():
    result = _parse_amount("Suma: 1 234,56")
    assert result["amount"] == "1234.56"


# ─── _document_type ──────────────────────────────────────────────────────────

def test_document_type_paragon():
    assert _document_type("PARAGON FISKALNY") == "paragon"


def test_document_type_faktura():
    assert _document_type("FAKTURA VAT") == "faktura"


def test_document_type_nip_vat():
    assert _document_type("NIP: 123-456-78-90\nVAT 23%") == "faktura"


def test_document_type_rachunek():
    assert _document_type("RACHUNEK za usługi") == "rachunek"


def test_document_type_potwierdzenie():
    assert _document_type("Płatność kartą CONTACTLESS") == "potwierdzenie"


def test_document_type_default():
    assert _document_type("Nieznany dokument") == "dokument"


# ─── _parse_contractor ───────────────────────────────────────────────────────

def test_parse_contractor_company_name():
    text = "FIRMA POLSKA SP. Z O.O.\nul. Testowa 1\nNIP: 123-456-78-90"
    result = _parse_contractor(text)
    assert result == "FIRMA POLSKA SP. Z O.O."


def test_parse_contractor_skips_short_lines():
    text = "AB\nKsiegarnia Naukowa\nNIP 123"
    result = _parse_contractor(text)
    assert result == "Ksiegarnia Naukowa"


def test_parse_contractor_ignores_noise_keywords():
    text = "PARAGON\nSKLEP SPOŻYWCZY KOWALSKI\nRAZEM"
    result = _parse_contractor(text)
    assert "SKLEP SPOŻYWCZY KOWALSKI" in result or result != "PARAGON"


def test_parse_contractor_unknown_when_all_noise():
    text = "NIP: 123\nVAT: 23%\n12345678"
    result = _parse_contractor(text)
    assert result == "kontrahent-nieznany"


def test_parse_contractor_skips_high_digit_ratio():
    text = "1234567890123\nFirma ABC\nNIP"
    result = _parse_contractor(text)
    assert "Firma ABC" in result
