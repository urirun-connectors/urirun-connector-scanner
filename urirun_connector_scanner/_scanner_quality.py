"""Document-frame quality scoring helpers extracted from scanner_bridge."""
from __future__ import annotations

from pathlib import Path


def bounded(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def frame_visual_metrics(path: str | Path) -> dict:
    try:
        from PIL import Image, ImageOps

        with Image.open(Path(path).expanduser().resolve()) as opened:
            image = ImageOps.exif_transpose(opened).convert("L")
            scale = min(1.0, 420 / max(image.size))
            if scale < 1.0:
                image = image.resize((max(1, int(image.size[0] * scale)), max(1, int(image.size[1] * scale))))
            width, height = image.size
            pixels = list(image.tobytes())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "sharpness": 0.0, "contrast": 0.0, "brightness": 0.0}
    if not pixels:
        return {"ok": False, "error": "empty image", "sharpness": 0.0, "contrast": 0.0, "brightness": 0.0}
    mean = sum(pixels) / len(pixels)
    variance = sum((value - mean) ** 2 for value in pixels) / len(pixels)
    contrast = variance ** 0.5
    diffs = []
    stride = max(1, width // 160)
    for y in range(0, height - 1, stride):
        row = y * width
        next_row = (y + 1) * width
        for x in range(0, width - 1, stride):
            idx = row + x
            diffs.append(abs(pixels[idx] - pixels[idx + 1]))
            diffs.append(abs(pixels[idx] - pixels[next_row + x]))
    sharpness = sum(diffs) / max(1, len(diffs))
    brightness_score = bounded(1.0 - abs(mean - 190.0) / 190.0)
    return {
        "ok": True,
        "width": width,
        "height": height,
        "brightness": round(mean, 3),
        "brightnessScore": round(brightness_score, 4),
        "contrast": round(contrast, 3),
        "contrastScore": round(bounded(contrast / 72.0), 4),
        "sharpness": round(sharpness, 3),
        "sharpnessScore": round(bounded(sharpness / 18.0), 4),
    }


def crop_dimensions(crop: dict) -> tuple[int, int]:
    return (
        int(crop.get("width") or crop.get("cropWidth") or 0),
        int(crop.get("height") or crop.get("cropHeight") or 0),
    )


def crop_geometry_score(crop: dict, reasons: list[str]) -> float:
    score = 0.0
    width, height = crop_dimensions(crop)
    if min(width, height) >= 220 and max(width, height) >= 420:
        score += 12.0
        reasons.append("size")
    orientation = crop.get("orientation") if isinstance(crop.get("orientation"), dict) else {}
    if orientation.get("enabled") and int(orientation.get("height") or height) >= int(orientation.get("width") or width):
        score += 5.0
        reasons.append("portrait")
    return score


def crop_quality_score(crop: dict, reasons: list[str]) -> float:
    if not crop.get("ok"):
        if crop.get("partialEdge"):
            reasons.append("partial-edge")
        elif crop.get("reason"):
            reasons.append("crop-rejected")
        return -20.0
    score = 42.0
    reasons.append("crop")
    bbox_area = float(crop.get("bboxArea") or 0.0)
    if bbox_area:
        score += 18.0 * bounded(1.0 - abs(bbox_area - 0.42) / 0.42)
    score += crop_geometry_score(crop, reasons)
    return score


def doctype_quality_score(doc_type: str, reasons: list[str]) -> float:
    if doc_type in {"paragon", "faktura"}:
        reasons.append(doc_type)
        return 32.0
    if doc_type in {"rachunek", "potwierdzenie"}:
        reasons.append(doc_type)
        return 20.0
    if doc_type != "dokument":
        return 10.0
    return 0.0


def metadata_quality_score(metadata: dict, reasons: list[str]) -> float:
    score = 0.0
    if metadata.get("date"):
        score += 8.0
        reasons.append("date")
    if metadata.get("amount"):
        score += 10.0
        reasons.append("amount")
    return score


def ocr_quality_score(ocr: dict, chars: int, reasons: list[str]) -> float:
    if ocr.get("ok") and chars:
        reasons.append("ocr")
        return min(36.0, chars / 4.0)
    return 0.0


def visual_quality_score(visual: dict, reasons: list[str]) -> float:
    if not visual.get("ok"):
        return 0.0
    reasons.append("visual")
    return (
        18.0 * float(visual.get("sharpnessScore") or 0.0)
        + 10.0 * float(visual.get("contrastScore") or 0.0)
        + 7.0 * float(visual.get("brightnessScore") or 0.0)
    )


def document_frame_quality(crop: dict, ocr: dict, metadata: dict, display_path: str | Path) -> dict:
    visual = frame_visual_metrics(display_path)
    reasons: list[str] = []
    doc_type = str(metadata.get("type") or "dokument")
    chars = int(ocr.get("chars") or len(str(ocr.get("text") or "")))
    score = (
        crop_quality_score(crop, reasons)
        + doctype_quality_score(doc_type, reasons)
        + metadata_quality_score(metadata, reasons)
        + ocr_quality_score(ocr, chars, reasons)
        + visual_quality_score(visual, reasons)
    )
    document_like = bool(crop.get("ok") and (doc_type in {"paragon", "faktura", "rachunek", "potwierdzenie"} or chars >= 36))
    return {
        "score": round(max(0.0, score), 3),
        "documentLike": document_like,
        "reasons": reasons,
        "cropReason": str(crop.get("reason") or ""),
        "visual": visual,
    }


def orientation_summary(crop: dict) -> dict:
    o = crop.get("orientation") if isinstance(crop, dict) and isinstance(crop.get("orientation"), dict) else {}
    source = o.get("source")
    if not source and o.get("enabled"):
        source = "osd" if (o.get("osd") or {}).get("appliedAngle") is not None else "geometry"
    return {
        "source": source,
        "angle": int(o.get("angle") or 0),
        "rotated": bool(o.get("rotated")),
        "score": o.get("score"),
    }
