"""Pure PDF generation helper extracted from document_sync."""
from __future__ import annotations

import io
import textwrap
import unicodedata
from pathlib import Path
from typing import Any


def pdf_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return text


def pdf_stream(data: bytes) -> bytes:
    return b"<< /Length " + str(len(data)).encode("ascii") + b" >>\nstream\n" + data + b"\nendstream"


def write_document_pdf(image_path: str | Path, pdf_path: str | Path, *, metadata: dict, ocr_text: str) -> None:
    from PIL import Image, ImageOps

    source = Path(image_path).expanduser().resolve()
    target = Path(pdf_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        try:
            from urirun_connector_smart_crop import orient_document_image

            image, _orientation = orient_document_image(image, auto_orient=True, prefer_portrait=True)
        except Exception:  # noqa: BLE001 - PDF generation must not fail when smart-crop is unavailable
            pass
        image_bytes = io.BytesIO()
        image.save(image_bytes, format="JPEG", quality=92, optimize=True)
        jpeg = image_bytes.getvalue()
        image_width, image_height = image.size

    page_width = 595.0
    page_height = 842.0
    margin = 36.0
    scale = min((page_width - margin * 2) / image_width, (page_height - margin * 2) / image_height)
    draw_width = image_width * scale
    draw_height = image_height * scale
    draw_x = (page_width - draw_width) / 2.0
    draw_y = (page_height - draw_height) / 2.0
    image_content = f"q {draw_width:.2f} 0 0 {draw_height:.2f} {draw_x:.2f} {draw_y:.2f} cm /Im0 Do Q".encode("ascii")

    header_lines = [
        f"Document ID: {metadata.get('docId', '')}",
        f"Type: {metadata.get('type', '')}",
        f"Date: {metadata.get('date', '')}",
        f"Contractor: {metadata.get('contractor', '')}",
        f"Amount: {metadata.get('amount', '')} {metadata.get('currency', '')}".strip(),
        f"Source: {metadata.get('sourcePath', '')}",
        "",
        "OCR text:",
    ]
    text_lines = header_lines
    for paragraph in (ocr_text or "").splitlines():
        if not paragraph.strip():
            text_lines.append("")
            continue
        text_lines.extend(textwrap.wrap(paragraph.strip(), width=92) or [""])
    text_lines = text_lines[:66]
    ops = ["BT /F1 10 Tf 12 TL 44 792 Td"]
    for line in text_lines:
        ops.append(f"({pdf_text(line)}) Tj T*")
    ops.append("ET")
    text_content = "\n".join(ops).encode("ascii", "ignore")

    info = (
        f"<< /Title ({pdf_text(target.stem)}) "
        f"/Creator ({pdf_text('urirun host dashboard')}) "
        f"/Subject ({pdf_text(metadata.get('docId', ''))}) "
        f"/Keywords ({pdf_text('urirun,ocr,document,' + str(metadata.get('type', '')))}) >>"
    ).encode("ascii", "ignore")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 7 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
        pdf_stream(image_content),
        (
            b"<< /Type /XObject /Subtype /Image /Width "
            + str(image_width).encode("ascii")
            + b" /Height "
            + str(image_height).encode("ascii")
            + b" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
            + str(len(jpeg)).encode("ascii")
            + b" >>\nstream\n"
            + jpeg
            + b"\nendstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 6 0 R >> >> /Contents 8 0 R >>",
        pdf_stream(text_content),
        info,
    ]

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{idx} 0 obj\n".encode("ascii"))
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 9 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    target.write_bytes(bytes(pdf))
