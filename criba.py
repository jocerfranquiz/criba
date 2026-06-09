#!/usr/bin/env python3
"""
criba.py  Minimal, fully-local PDF extractor.

Uses pypdfium2 (PDFium) to read native-text PDFs into a structured, in-memory
result — document metadata, per-page text spans with font/size/color/bbox, raw
reading-order text, and embedded images decoded to bytes — then serialises it
to JSON (structure), Markdown (RAG text), and image files.

A small ETL pipeline: :func:`extract` reads a PDF into a dict; :func:`to_json`,
:func:`to_markdown`, and :func:`to_images` serialise it; :func:`convert` runs
the whole thing and writes every output.

Coordinates are normalised to a **top-left origin** (y increases downward).

Usage
-----
    python cli.py document.pdf [-o output]    # command-line entry point

    from criba import extract, convert        # or use the library directly

Produces::

    output/
    ├── document.json
    ├── document.md
    └── document_images/
        ├── page_003_fig_001.png
        └── ...

Dependencies: pypdfium2, Pillow (fallback image encoding)
"""

from __future__ import annotations

import ctypes
import io
import json
import logging
import re

from collections import Counter
from datetime import datetime
from jsonschema import validate as validate_json
from pathlib import Path

from pypdfium2 import (
    PdfDocument,
    PdfiumError,
    PdfTextObj,
    PdfTextPage,
    PdfPage,
    PdfImage,
)
from pypdfium2.raw import (
    FPDF_PAGEOBJ_IMAGE,
    FPDF_PAGEOBJ_TEXT,
    FPDF_ERR_PASSWORD,
    FPDFPageObj_GetFillColor,
)

from schema import OUTPUT_SCHEMA

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

# PDF date string: D:YYYYMMDDHHmmSSOHH'mm'  (everything after the year optional).
_PDF_DATE_RE = re.compile(
    r"^(?:D:)?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?"
    r"(?:([Zz+\-])(\d{2})?'?(\d{2})?'?)?$"
)

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
    x0: float = min(a["x"], b["x"])
    y0: float = min(a["y"], b["y"])
    x1: float = max(a["x"] + a["w"], b["x"] + b["w"])
    y1: float = max(a["y"] + a["h"], b["y"] + b["h"])
    return {
        "x": round(x0, 2),
        "y": round(y0, 2),
        "w": round(x1 - x0, 2),
        "h": round(y1 - y0, 2),
    }


def _normalize_pdf_date(raw: str) -> str:
    """``D:20240115093000+05'30'`` → ``2024-01-15T09:30:00+05:30``; returns *raw*
    unchanged if it doesn't match the format or encodes an impossible date.
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

# ── Metadata ─────────────────────────────────────────────────────────────────


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

def _coalesce_lines(
    raw_spans: list[dict],
    line_overlap: float = LINE_OVERLAP_RATIO,
    space_gap: float = SPACE_GAP_RATIO,
) -> list[dict]:
    """Order spans into reading order and coalesce same-style runs.

    Spans group into lines by vertical *overlap* (not top-y), so mixed-size
    baseline-aligned text reads left→right; lines are ordered top→bottom.
    """
    if not raw_spans:
        return []

    # Build lines greedily: a span joins the first existing line whose vertical
    # band it mostly overlaps; otherwise it starts a new line.  Sort a copy so
    # the caller's list is left untouched.
    ordered = sorted(raw_spans, key=lambda _: _["bbox"]["y"])
    lines: list[dict] = []
    for span in ordered:
        top = span["bbox"]["y"]
        bottom = top + span["bbox"]["h"]
        for line in lines:
            line_top: float = line["top"]
            line_bottom: float = line["bottom"]
            overlap: float = min(bottom, line_bottom) - max(top, line_top)
            min_h: float = min(bottom - top, line_bottom - line_top)
            if min_h > 0 and overlap > line_overlap * min_h:
                line["spans"].append(span)
                line["top"] = min(line_top, top)
                line["bottom"] = max(line_bottom, bottom)
                break
        else:
            lines.append({"top": top, "bottom": bottom, "spans": [span]})

    lines.sort(key=lambda _: _["top"])

    # Within each line, read left→right and merge consecutive same-style runs.
    # Each output span is a fresh dict (never an input span), so callers' span
    # dicts are never mutated — accumulation happens on the copy.
    merged: list[dict] = []
    for line in lines:
        spans = sorted(line["spans"], key=lambda _: _["bbox"]["x"])
        line_merged: list[dict] = [dict(spans[0])]
        for span in spans[1:]:
            prev = line_merged[-1]
            same_style = prev["font"] == span["font"] and prev["color"] == span["color"]
            if same_style:
                gap = span["bbox"]["x"] - (prev["bbox"]["x"] + prev["bbox"]["w"])
                sep = " " if gap > prev["font"]["size"] * space_gap else ""
                prev["text"] = prev["text"] + sep + span["text"]
                prev["bbox"] = _bbox_union(prev["bbox"], span["bbox"])
            else:
                line_merged.append(dict(span))
        merged.extend(line_merged)

    return merged


def _extract_text_spans(
    page: PdfPage,
    textpage: PdfTextPage,
    page_height: float,
    line_overlap: float = LINE_OVERLAP_RATIO,
    space_gap: float = SPACE_GAP_RATIO,
) -> list[dict]:
    """Pull text page-objects and coalesce same-font/colour runs per line.

    *textpage* is owned by the caller (shared with raw-text extraction); this
    function does not close it.
    """
    raw_spans: list[dict] = []

    for obj in page.get_objects(filter=[FPDF_PAGEOBJ_TEXT]):
        text_obj = PdfTextObj(obj.raw, textpage=textpage)

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

    return _coalesce_lines(raw_spans, line_overlap=line_overlap, space_gap=space_gap)

# ── Raw text ─────────────────────────────────────────────────────────────────


def _extract_raw_text(textpage: PdfTextPage) -> str:
    """Full page text in PDFium's built-in reading order.

    *textpage* is owned by the caller; this function does not close it.
    """
    n = textpage.count_chars()
    text = textpage.get_text_range(0, n) if n > 0 else ""
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ── Images ───────────────────────────────────────────────────────────────────


def _image_ext(img: PdfImage) -> str:
    """Extension matching what :meth:`PdfImage.extract` emits: ``jpg``/``jp2``
    for verbatim JPEG/JPEG-2000 passthrough, else ``png`` (re-encoded).
    """
    try:
        filters = img.get_filters()
    except PdfiumError:
        return "png"
    last = filters[-1] if filters else ""
    if last == "DCTDecode":
        return "jpg"
    if last == "JPXDecode":
        return "jp2"
    return "png"


def _extract_images(
    page: PdfPage, page_idx: int, page_height: float, stem: str
) -> list[dict]:
    """Extract embedded images as in-memory bytes (no disk writes).

    Each result carries the encoded ``data`` plus a relative ``file`` path for a
    later :func:`to_images` call; JPEG/JP2 pass through, else re-encoded to PNG.
    """
    results: list[dict] = []

    for obj in page.get_objects(filter=[FPDF_PAGEOBJ_IMAGE]):
        # Allocate the figure number from successful extractions only, so a
        # failure doesn't leave a gap (the next success reuses this number).
        fig_num = len(results) + 1
        img = PdfImage(obj.raw, page=page)

        # Bounding box
        left, bottom, right, top = obj.get_bounds()
        bbox = _normalize_bbox(left, bottom, right, top, page_height)

        # Native pixel size
        try:
            w_px, h_px = img.get_px_size()
        except PdfiumError:
            w_px, h_px = 0, 0

        # Extract into memory.  Writing to a BytesIO keeps PDFium's quality-
        # preserving passthrough (the extension is computed separately, since
        # pdfium only reports it when writing to a file path).
        ext = _image_ext(img)
        buf = io.BytesIO()
        try:
            img.extract(buf, fb_format="png")
        except (PdfiumError, OSError, ValueError) as exc:
            logger.warning(
                "Image extraction failed p%d fig%d: %s", page_idx + 1, fig_num, exc
            )
            continue

        data = buf.getvalue()
        if not data:
            logger.warning("Empty image data for p%d fig%d", page_idx + 1, fig_num)
            continue

        results.append(
            {
                "index": fig_num,
                "bbox": bbox,
                "size_px": {"width": w_px, "height": h_px},
                "ext": ext,
                "file": f"{stem}_images/page_{page_idx + 1:03d}_fig_{fig_num:03d}.{ext}",
                "data": data,
            }
        )

    return results


# ── Orchestrator ─────────────────────────────────────────────────────────────


def _open_document(pdf_path: Path, password: str | None) -> PdfDocument:
    """Open *pdf_path*, mapping a password failure to ``EncryptedPDFError``."""
    try:
        return PdfDocument(str(pdf_path), password=password)
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


def _build_result(
    doc: PdfDocument,
    source_name: str,
    stem: str,
    line_overlap: float,
    space_gap: float,
) -> dict:
    """Assemble the result dict from an already-open *doc*, in memory.

    No disk writes (images decoded to bytes). The caller owns *doc*; each
    per-page handle is opened and closed here.
    """
    result: dict = {
        "source_file": source_name,
        "metadata": _extract_metadata(doc),
        "pages": [],
    }

    for i in range(len(doc)):
        page = doc.get_page(i)
        try:
            w, h = page.get_width(), page.get_height()

            # One textpage per page, shared by raw-text and span extraction.
            textpage = page.get_textpage()
            try:
                raw_text = _extract_raw_text(textpage)
                spans = _extract_text_spans(
                    page, textpage, h, line_overlap=line_overlap, space_gap=space_gap
                )
            finally:
                textpage.close()

            images = _extract_images(page, i, h, stem)

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

    return result


def extract(
    pdf_path: str | Path,
    *,
    password: str | None = None,
    line_overlap: float = LINE_OVERLAP_RATIO,
    space_gap: float = SPACE_GAP_RATIO,
    validate: bool = False,
) -> dict:
    """Extract *pdf_path* into a result dict, fully in memory (no disk writes).

    Images are decoded to in-memory bytes, so the dict is self-contained for the
    ``to_*`` serialisers or direct use. See the README for params and exceptions.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    doc = _open_document(pdf_path, password)
    try:
        result = _build_result(
            doc, pdf_path.name, pdf_path.stem, line_overlap, space_gap
        )
    finally:
        doc.close()

    if validate:
        validate_output(_json_view(result))

    return result


# ── Serialisers ──────────────────────────────────────────────────────────────


def _json_view(result: dict) -> dict:
    """Return *result* without the in-memory image ``data`` bytes.

    This is the JSON-serialisable shape (``schema.OUTPUT_SCHEMA``); bytes are
    materialised separately by :func:`to_images`.
    """
    view = {k: v for k, v in result.items() if k != "pages"}
    view["pages"] = []
    for page in result["pages"]:
        page_view = dict(page)
        page_view["images"] = [
            {k: v for k, v in img.items() if k != "data"} for img in page["images"]
        ]
        view["pages"].append(page_view)
    return view


def to_json(result: dict, path: str | Path | None = None) -> str:
    """Serialise the document to a JSON string; write it too if *path* is given.

    Image bytes are omitted; each image keeps its ``file`` reference for a
    companion :func:`to_images` call.
    """
    text = json.dumps(_json_view(result), indent=2, ensure_ascii=False)
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return text


def to_images(result: dict, base_dir: str | Path) -> list[Path]:
    """Write every extracted image under *base_dir* (honouring its ``file`` path).

    Creates ``<stem>_images/`` only when needed and returns the paths written;
    passthrough images keep ``.jpg``/``.jp2``, others are PNG.
    """
    base_dir = Path(base_dir)
    written: list[Path] = []
    for page in result["pages"]:
        for img in page["images"]:
            dest = base_dir / img["file"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(img["data"])
            written.append(dest)
    return written


def _emphasise(span: dict) -> str:
    """Wrap a span's text in Markdown emphasis inferred from its font."""
    text = span["text"]
    name = span["font"]["name"].lower()
    bold = span["font"].get("weight", 0) >= 700 or "bold" in name
    italic = "italic" in name or "oblique" in name
    if bold:
        text = f"**{text}**"
    if italic:
        text = f"*{text}*"
    return text


def _group_lines(spans: list[dict]) -> list[dict]:
    """Regroup the flat span list into visual lines by vertical position.

    Spans whose tops sit within half a line height are grouped, then ordered
    top→bottom and left→right.
    """
    lines: list[dict] = []
    for span in sorted(spans, key=lambda s: (s["bbox"]["y"], s["bbox"]["x"])):
        y: float = span["bbox"]["y"]
        h: float = span["bbox"]["h"]
        size: float = span["font"]["size"]
        for line in lines:
            line_y: float = line["y"]
            line_h: float = line["h"]
            if abs(y - line_y) <= 0.5 * max(h, line_h):
                line["spans"].append(span)
                line["size"] = max(line["size"], size)
                line["h"] = max(line_h, h)
                break
        else:
            lines.append({"y": y, "h": h, "size": size, "spans": [span]})

    for line in lines:
        line["spans"].sort(key=lambda s: s["bbox"]["x"])
    lines.sort(key=lambda _: _["y"])
    return lines


def _heading_levels(pages_lines: list[list[dict]]) -> dict[int, int]:
    """Map (rounded) font sizes to heading levels across the document.

    Body = the size with the most *characters*; larger sizes become headings
    ranked '#'/'##'/'###' by descending size, document-wide for consistency.
    """
    weights: Counter = Counter()
    for lines in pages_lines:
        for line in lines:
            weights[round(line["size"])] += sum(len(s["text"]) for s in line["spans"])
    if not weights:
        return {}
    body_size = weights.most_common(1)[0][0]
    heading_sizes = sorted((s for s in weights if s > body_size), reverse=True)
    return {size: min(i + 1, 6) for i, size in enumerate(heading_sizes)}


def _md_page_blocks(
    lines: list[dict], page: dict, level_of: dict[int, int]
) -> list[tuple[float, str]]:
    """Best-effort Markdown blocks for one page, each tagged with its y-position."""
    blocks: list[tuple[float, str]] = []
    for line in lines:
        size = round(line["size"])
        if size in level_of:
            text = " ".join(s["text"] for s in line["spans"]).strip()
            rendered = f"{'#' * level_of[size]} {text}"
        else:
            rendered = " ".join(_emphasise(s) for s in line["spans"]).strip()
        if rendered:
            blocks.append((line["y"], rendered))

    for img in page.get("images", []):
        blocks.append((img["bbox"]["y"], f"![]({img['file']})"))

    blocks.sort(key=lambda b: b[0])
    return blocks


def to_markdown(result: dict, path: str | Path | None = None) -> str:
    """Render *result* as best-effort Markdown for RAG; write it if *path* given.

    Headings are inferred from font sizes, emphasis from weight/name, images
    inlined as ``![](file)``. Retrieval quality, not visual fidelity; see README.
    """
    pages_lines = [_group_lines(page.get("text_spans", [])) for page in result["pages"]]
    level_of = _heading_levels(pages_lines)

    rendered_pages = []
    for lines, page in zip(pages_lines, result["pages"]):
        blocks = _md_page_blocks(lines, page, level_of)
        text = "\n\n".join(block for _, block in blocks)
        if text:
            rendered_pages.append(text)

    body = "\n\n".join(rendered_pages)
    text = body + "\n" if body else ""
    if path is not None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return text


def convert(
    pdf_path: str | Path,
    output_dir: str | Path = "output",
    password: str | None = None,
    line_overlap: float = LINE_OVERLAP_RATIO,
    space_gap: float = SPACE_GAP_RATIO,
    validate: bool = False,
) -> dict:
    """Run the full pipeline: extract *pdf_path*, write json + md + images.

    Writes ``<stem>.{json,md}`` and ``<stem>_images/`` to *output_dir*; returns
    the result dict. See the README for params/exceptions.
    """
    pdf_path = Path(pdf_path)
    result = extract(
        pdf_path,
        password=password,
        line_overlap=line_overlap,
        space_gap=space_gap,
        validate=validate,
    )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = pdf_path.stem
    json_path = out / f"{stem}.json"
    to_json(result, json_path)
    to_markdown(result, out / f"{stem}.md")
    to_images(result, out)

    logger.info("Wrote %s", json_path)
    return result


def validate_output(result: dict) -> None:
    """Validate *result* against :data:`OUTPUT_SCHEMA`.
    """

    validate_json(instance=result, schema=OUTPUT_SCHEMA)
