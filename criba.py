#!/usr/bin/env python3
"""
criba.py  Minimal, fully-local PDF → JSON extractor.

Uses pypdfium2 (PDFium) to read native-text PDFs and emit structured JSON
containing document metadata, per-page text spans with font/size/color/bbox,
raw reading-order text, and extracted embedded images.

Coordinates are normalised to a **top-left origin** (y increases downward).

Usage
-----
    python criba.py document.pdf [-o output]

Produces::

    output/
    ├── document.json
    └── document_images/
        ├── page_003_fig_001.png
        └── ...

Dependencies: pypdfium2, Pillow (fallback image encoding)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import re

from datetime import datetime
from pathlib import Path

from pypdfium2 import PdfDocument, PdfiumError, PdfTextObj, PdfPage, PdfImage
from pypdfium2.raw import (
    FPDF_PAGEOBJ_IMAGE,
    FPDF_PAGEOBJ_TEXT,
    FPDF_ERR_PASSWORD,
    FPDFPageObj_GetFillColor,
)

logger = logging.getLogger(__name__)


class EncryptedPDFError(Exception):
    """Raised when a PDF is encrypted and no/incorrect password was supplied."""


# ── Constants ────────────────────────────────────────────────────────────────

# Two spans share a line when their vertical extents overlap by at least this
# fraction of the shorter span's height.  Using overlap (not top-y proximity)
# keeps baseline-aligned text of different font sizes on the same line.
LINE_OVERLAP_RATIO = 0.5

# Minimum gap (as fraction of font size) to insert a space between merged spans.
SPACE_GAP_RATIO = 0.25


# ── Helpers ──────────────────────────────────────────────────────────────────


def _strip_subset_prefix(name: str) -> str:
    """``ABCDEF+Arial`` → ``Arial``.  Leaves other names unchanged."""
    if len(name) > 7 and name[6] == "+" and name[:6].isalpha() and name[:6].isupper():
        return name[7:]
    return name


def _fill_color(raw_handle) -> dict:
    """RGBA fill colour of a page object via the raw C API."""
    r = ctypes.c_uint(0)
    g = ctypes.c_uint(0)
    b = ctypes.c_uint(0)
    a = ctypes.c_uint(0)
    ok = FPDFPageObj_GetFillColor(
        raw_handle,
        ctypes.byref(r),
        ctypes.byref(g),
        ctypes.byref(b),
        ctypes.byref(a),
    )
    if ok:
        return {"r": r.value, "g": g.value, "b": b.value, "a": a.value}
    return {"r": 0, "g": 0, "b": 0, "a": 255}


def _normalize_bbox(
    left: float, bottom: float, right: float, top: float, page_height: float
) -> dict:
    """PDF coords (origin bottom-left) → top-left origin ``{x, y, w, h}``."""
    return {
        "x": round(left, 2),
        "y": round(page_height - top, 2),
        "w": round(right - left, 2),
        "h": round(top - bottom, 2),
    }


def _bbox_union(a: dict, b: dict) -> dict:
    x0 = min(a["x"], b["x"])
    y0 = min(a["y"], b["y"])
    x1 = max(a["x"] + a["w"], b["x"] + b["w"])
    y1 = max(a["y"] + a["h"], b["y"] + b["h"])
    return {
        "x": round(x0, 2),
        "y": round(y0, 2),
        "w": round(x1 - x0, 2),
        "h": round(y1 - y0, 2),
    }


# ── Metadata ─────────────────────────────────────────────────────────────────

# PDF date string: D:YYYYMMDDHHmmSSOHH'mm'  (everything after the year optional).
_PDF_DATE_RE = re.compile(
    r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:([Zz+\-])(\d{2})?'?(\d{2})?'?)?$"
)


def _normalize_pdf_date(raw: str) -> str:
    """``D:20240115093000+05'30'`` → ``2024-01-15T09:30:00+05:30``.

    Returns *raw* unchanged if it doesn't match the PDF date format or encodes
    an impossible date (so no information is ever lost).
    """
    m = _PDF_DATE_RE.match(raw.strip())
    if not m:
        return raw

    year, month, day, hour, minute, second, tzsign, tzh, tzm = m.groups()
    month, day = month or "01", day or "01"
    hour, minute, second = hour or "00", minute or "00", second or "00"

    try:  # reject impossible dates (e.g. month 13) rather than emit garbage
        datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
    except ValueError:
        return raw

    iso = f"{year}-{month}-{day}T{hour}:{minute}:{second}"
    if tzsign in ("Z", "z"):
        iso += "+00:00"
    elif tzsign in ("+", "-"):
        iso += f"{tzsign}{tzh or '00'}:{tzm or '00'}"
    return iso


def _extract_metadata(doc: PdfDocument) -> dict:
    meta: dict = {}
    try:
        raw = doc.get_metadata_dict()
        for key in (
            "Title",
            "Author",
            "Subject",
            "Creator",
            "Producer",
            "CreationDate",
            "ModDate",
            "Keywords",
        ):
            v = raw.get(key, "")
            if v:
                if key in ("CreationDate", "ModDate"):
                    v = _normalize_pdf_date(v)
                meta[key.lower()] = v
    except PdfiumError:
        logger.debug("Metadata dict extraction failed", exc_info=True)

    meta["page_count"] = len(doc)

    try:
        v = doc.get_version()  # None if new doc or version undeterminable
        if v is not None:
            meta["pdf_version"] = f"{v // 10}.{v % 10}"  # 14 → "1.4", 20 → "2.0"
    except PdfiumError:
        logger.debug("PDF version extraction failed", exc_info=True)

    try:
        meta["tagged"] = doc.is_tagged()
    except PdfiumError:
        logger.debug("Tagged-PDF check failed", exc_info=True)

    return meta


# ── Text spans ───────────────────────────────────────────────────────────────


def _extract_text_spans(
    page: PdfPage,
    page_height: float,
    line_overlap: float = LINE_OVERLAP_RATIO,
    space_gap: float = SPACE_GAP_RATIO,
) -> list[dict]:
    """
    Pull every text page-object, sort into approximate reading order,
    and coalesce consecutive runs that share font + colour on the same line.
    """
    tp = page.get_textpage()
    raw_spans: list[dict] = []

    try:
        for obj in page.get_objects(filter=[FPDF_PAGEOBJ_TEXT]):
            text_obj = PdfTextObj(obj.raw, textpage=tp)

            text = text_obj.extract()
            if not text:
                continue

            font = text_obj.get_font()
            font_name = _strip_subset_prefix(
                font.get_base_name() or font.get_family_name() or "unknown"
            )
            font_size = round(text_obj.get_font_size(), 2)
            font_weight = font.get_weight()
            color = _fill_color(obj.raw)

            left, bottom, right, top = obj.get_bounds()
            bbox = _normalize_bbox(left, bottom, right, top, page_height)

            raw_spans.append(
                {
                    "text": text,
                    "bbox": bbox,
                    "font": {
                        "name": font_name,
                        "size": font_size,
                        "weight": font_weight,
                    },
                    "color": color,
                }
            )
    finally:
        tp.close()

    return _coalesce_lines(raw_spans, line_overlap=line_overlap, space_gap=space_gap)


def _coalesce_lines(
    raw_spans: list[dict],
    line_overlap: float = LINE_OVERLAP_RATIO,
    space_gap: float = SPACE_GAP_RATIO,
) -> list[dict]:
    """
    Order spans into approximate reading order and coalesce same-style runs.

    Spans are grouped into lines by *vertical overlap* rather than top-y
    proximity, so baseline-aligned text of different font sizes (e.g. a drop
    cap beside body text) stays on one line and is read left→right instead of
    interleaving.  Lines are then ordered top→bottom.
    """
    if not raw_spans:
        return []

    # Build lines greedily: a span joins the first existing line whose vertical
    # band it mostly overlaps; otherwise it starts a new line.
    raw_spans.sort(key=lambda s: s["bbox"]["y"])
    lines: list[dict] = []
    for span in raw_spans:
        top = span["bbox"]["y"]
        bottom = top + span["bbox"]["h"]
        for line in lines:
            overlap = min(bottom, line["bottom"]) - max(top, line["top"])
            min_h = min(bottom - top, line["bottom"] - line["top"])
            if min_h > 0 and overlap > line_overlap * min_h:
                line["spans"].append(span)
                line["top"] = min(line["top"], top)
                line["bottom"] = max(line["bottom"], bottom)
                break
        else:
            lines.append({"top": top, "bottom": bottom, "spans": [span]})

    lines.sort(key=lambda _: _["top"])

    # Within each line, read left→right and merge consecutive same-style runs.
    merged: list[dict] = []
    for line in lines:
        spans = sorted(line["spans"], key=lambda _: _["bbox"]["x"])
        line_merged: list[dict] = [spans[0]]
        for span in spans[1:]:
            prev = line_merged[-1]
            same_style = prev["font"] == span["font"] and prev["color"] == span["color"]
            if same_style:
                gap = span["bbox"]["x"] - (prev["bbox"]["x"] + prev["bbox"]["w"])
                sep = " " if gap > prev["font"]["size"] * space_gap else ""
                prev["text"] += sep + span["text"]
                prev["bbox"] = _bbox_union(prev["bbox"], span["bbox"])
            else:
                line_merged.append(span)
        merged.extend(line_merged)

    return merged


# ── Raw text ─────────────────────────────────────────────────────────────────


def _extract_raw_text(page: PdfPage) -> str:
    """Full page text in PDFium's built-in reading order."""
    tp = page.get_textpage()
    try:
        n = tp.count_chars()
        text = tp.get_text_range(0, n) if n > 0 else ""
    finally:
        tp.close()
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ── Images ───────────────────────────────────────────────────────────────────


def _extract_images(
    page: PdfPage, page_idx: int, page_height: float, images_dir: Path
) -> list[dict]:
    results: list[dict] = []

    for obj in page.get_objects(filter=[FPDF_PAGEOBJ_IMAGE]):
        # Allocate the figure number from successful writes only, so a failed
        # extraction doesn't leave a gap (next success reuses this number).
        fig_num = len(results) + 1
        img = PdfImage(obj.raw, page=page)

        # Bounding box
        left, bottom, right, top = obj.get_bounds()
        bbox = _normalize_bbox(left, bottom, right, top, page_height)

        # Native pixel size
        try:
            w_px, h_px = img.get_px_size()
        except Exception:
            w_px, h_px = 0, 0

        # Extract to file  (pypdfium2 appends the real extension)
        stem = f"page_{page_idx + 1:03d}_fig_{fig_num:03d}"
        dest_prefix = images_dir / stem

        try:
            img.extract(str(dest_prefix), fb_format="png")
        except Exception as exc:
            logger.warning(
                "Image extraction failed p%d fig%d: %s", page_idx + 1, fig_num, exc
            )
            continue

        # Discover the file that was actually written
        written = None
        for ext in (".png", ".jpg", ".jp2"):
            candidate = images_dir / (stem + ext)
            if candidate.exists():
                written = candidate
                break

        if written is None:
            logger.warning("No output file found for p%d fig%d", page_idx + 1, fig_num)
            continue

        results.append(
            {
                "index": fig_num,
                "bbox": bbox,
                "size_px": {"width": w_px, "height": h_px},
                "file": str(written.relative_to(images_dir.parent)),
            }
        )

    return results


# ── Orchestrator ─────────────────────────────────────────────────────────────


def extract_pdf(
    pdf_path: str | Path,
    output_dir: str | Path = "output",
    password: str | None = None,
    line_overlap: float = LINE_OVERLAP_RATIO,
    space_gap: float = SPACE_GAP_RATIO,
) -> dict:
    """
    Extract all raw data from *pdf_path* and write the result to *output_dir*.

    Pass *password* to open an encrypted/password-protected PDF.
    *line_overlap* controls how much two spans must vertically overlap (as a
    fraction of the shorter span's height) to be grouped on the same line.
    *space_gap* controls the minimum horizontal gap (as a fraction of font size)
    that triggers an inserted space between merged spans.

    Returns the full result dict (same object serialised as JSON).

    Raises:
        FileNotFoundError: if *pdf_path* does not exist.
        EncryptedPDFError: if the PDF is encrypted and *password* is missing/wrong.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    stem = pdf_path.stem
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    images_dir = out / f"{stem}_images"
    images_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = PdfDocument(str(pdf_path), password=password)
    except PdfiumError as exc:
        if getattr(exc, "err_code", None) == FPDF_ERR_PASSWORD:
            hint = (
                "incorrect password"
                if password is not None
                else "encrypted; supply a password via the 'password' argument "
                "(or --password on the CLI)"
            )
            raise EncryptedPDFError(f"{pdf_path.name} is {hint}.") from exc
        raise

    try:
        result: dict = {
            "source_file": pdf_path.name,
            "metadata": _extract_metadata(doc),
            "pages": [],
        }

        for i in range(len(doc)):
            page = doc.get_page(i)
            try:
                w, h = page.get_width(), page.get_height()

                raw_text = _extract_raw_text(page)
                spans = _extract_text_spans(
                    page, h, line_overlap=line_overlap, space_gap=space_gap
                )
                images = _extract_images(page, i, h, images_dir)

                page_data: dict = {
                    "page_number": i + 1,
                    "width": round(w, 2),
                    "height": round(h, 2),
                    "raw_text": raw_text,
                    "text_spans": spans,
                    "images": images,
                }

                # Heuristic: rendered page but zero text → probably scanned
                if not raw_text.strip() and not spans:
                    page_data["warning"] = "no_text_layer"

                result["pages"].append(page_data)
            finally:
                page.close()
    finally:
        doc.close()

    # Persist JSON
    json_path = out / f"{stem}.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)

    # Remove images dir if empty
    if not any(images_dir.iterdir()):
        images_dir.rmdir()

    logger.info("Wrote %s", json_path)
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract raw data from a PDF into JSON.")
    ap.add_argument("pdf", help="Input PDF path")
    ap.add_argument(
        "-o", "--output", default="output", help="Output directory (default: ./output)"
    )
    ap.add_argument(
        "-p", "--password", default=None, help="Password for an encrypted PDF"
    )
    ap.add_argument(
        "--line-overlap",
        type=float,
        default=LINE_OVERLAP_RATIO,
        metavar="RATIO",
        help=f"Min vertical overlap fraction to group spans on the same line (default: {LINE_OVERLAP_RATIO})",
    )
    ap.add_argument(
        "--space-gap",
        type=float,
        default=SPACE_GAP_RATIO,
        metavar="RATIO",
        help=f"Min gap/font-size ratio to insert a space between merged spans (default: {SPACE_GAP_RATIO})",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        result = extract_pdf(
            args.pdf,
            args.output,
            password=args.password,
            line_overlap=args.line_overlap,
            space_gap=args.space_gap,
        )
    except EncryptedPDFError as exc:
        raise SystemExit(f"error: {exc}")

    pages = len(result["pages"])
    spans = sum(len(p["text_spans"]) for p in result["pages"])
    imgs = sum(len(p["images"]) for p in result["pages"])
    warnings = sum(1 for p in result["pages"] if "warning" in p)

    print(f"✓ {pages} pages · {spans} spans · {imgs} images → {args.output}/")
    if warnings:
        print(f"{warnings} page(s) with no text layer (scanned?)")


if __name__ == "__main__":
    main()
