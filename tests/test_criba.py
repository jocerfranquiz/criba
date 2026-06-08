"""Starter tests for criba's pure helpers and basic error handling."""

from pathlib import Path

import pytest

import criba
from criba import (
    EncryptedPDFError,
    _bbox_union,
    _coalesce_lines,
    _extract_images,
    _normalize_bbox,
    _normalize_pdf_date,
    _strip_subset_prefix,
    extract_pdf,
)


def _span(text, x, y, w, h, size=12.0):
    """Build a minimal text-span dict for ordering tests."""
    return {
        "text": text,
        "bbox": {"x": x, "y": y, "w": w, "h": h},
        "font": {"name": "Test", "size": size, "weight": 400},
        "color": {"r": 0, "g": 0, "b": 0, "a": 255},
    }


# ── _coalesce_lines ───────────────────────────────────────────────────────────


def test_coalesce_lines_mixed_font_sizes_read_left_to_right():
    """A large span on the right must not sort before a small span on its left.

    Drop cap "T" (size 24, x=100) shares a baseline with body text (size 12,
    x=0) — different top-y, overlapping vertical extent. Reading order is L→R.
    """
    big = _span("T", x=100, y=0, w=20, h=24, size=24)  # taller, to the right
    small = _span("he rest", x=0, y=12, w=80, h=12, size=12)  # shorter, to the left

    result = _coalesce_lines([big, small])

    assert [s["text"] for s in result] == ["he rest", "T"]


def test_coalesce_lines_separates_stacked_lines_top_to_bottom():
    line2 = _span("second", x=0, y=30, w=50, h=12)
    line1 = _span("first", x=0, y=0, w=50, h=12)

    result = _coalesce_lines([line2, line1])

    assert [s["text"] for s in result] == ["first", "second"]


def test_coalesce_lines_merges_same_style_run_on_a_line():
    a = _span("Hello", x=0, y=0, w=30, h=12)
    b = _span("world", x=40, y=0, w=30, h=12)  # gap > 0.25*12 -> space inserted

    result = _coalesce_lines([a, b])

    assert len(result) == 1
    assert result[0]["text"] == "Hello world"


def test_coalesce_lines_empty():
    assert _coalesce_lines([]) == []


# ── _normalize_pdf_date ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Full timestamp with positive offset.
        ("D:20240115093000+05'30'", "2024-01-15T09:30:00+05:30"),
        # Negative offset.
        ("D:20240115093000-08'00'", "2024-01-15T09:30:00-08:00"),
        # Zulu / UTC.
        ("D:20240115093000Z", "2024-01-15T09:30:00+00:00"),
        # No "D:" prefix, still valid.
        ("20240115093000Z", "2024-01-15T09:30:00+00:00"),
        # Partial: year+month+day only -> time defaults to 00:00:00, no tz.
        ("D:20240115", "2024-01-15T00:00:00"),
        # Year only.
        ("D:2024", "2024-01-01T00:00:00"),
        # Offset hours without minutes.
        ("D:20240115093000+05", "2024-01-15T09:30:00+05:00"),
        # Impossible date -> returned unchanged.
        ("D:20241345000000", "D:20241345000000"),
        # Not a PDF date -> returned unchanged.
        ("January 15, 2024", "January 15, 2024"),
        ("", ""),
    ],
)
def test_normalize_pdf_date(raw, expected):
    assert _normalize_pdf_date(raw) == expected


# ── _strip_subset_prefix ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ABCDEF+Arial", "Arial"),  # valid 6-upper-letter subset prefix
        ("Helvetica-Bold", "Helvetica-Bold"),  # no prefix
        ("Arial", "Arial"),  # too short to have a prefix
        ("abcdef+Arial", "abcdef+Arial"),  # lowercase tag is not stripped
        ("ABC123+Arial", "ABC123+Arial"),  # non-alpha tag is not stripped
    ],
)
def test_strip_subset_prefix(name, expected):
    assert _strip_subset_prefix(name) == expected


# ── _normalize_bbox ───────────────────────────────────────────────────────────


def test_normalize_bbox_flips_origin():
    # PDF bottom-left coords -> top-left origin on a 792pt-tall page.
    bbox = _normalize_bbox(
        left=72.0, bottom=692.0, right=192.0, top=752.0, page_height=792.0
    )
    assert bbox == {"x": 72.0, "y": 40.0, "w": 120.0, "h": 60.0}


# ── _bbox_union ───────────────────────────────────────────────────────────────


def test_bbox_union_covers_both():
    a = {"x": 0.0, "y": 0.0, "w": 10.0, "h": 10.0}
    b = {"x": 5.0, "y": 5.0, "w": 10.0, "h": 10.0}
    assert _bbox_union(a, b) == {"x": 0.0, "y": 0.0, "w": 15.0, "h": 15.0}


# ── extract_pdf ───────────────────────────────────────────────────────────────


def test_extract_pdf_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_pdf(tmp_path / "does_not_exist.pdf", output_dir=tmp_path / "out")


def _password_error():
    return criba.pdfium.PdfiumError("bad password", err_code=criba._FPDF_ERR_PASSWORD)


def test_extract_pdf_encrypted_raises_friendly_error(tmp_path, monkeypatch):
    """A password-coded PdfiumError becomes an EncryptedPDFError with guidance."""

    pdf = tmp_path / "secret.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def boom(*a, **k):
        raise _password_error()

    monkeypatch.setattr(criba.pdfium, "PdfDocument", boom)

    with pytest.raises(EncryptedPDFError, match="supply a password"):
        extract_pdf(pdf, output_dir=tmp_path / "out")

    # With a (wrong) password supplied, the message reflects that instead.
    with pytest.raises(EncryptedPDFError, match="incorrect password"):
        extract_pdf(pdf, output_dir=tmp_path / "out", password="nope")


def test_extract_pdf_non_password_pdfium_error_propagates(tmp_path, monkeypatch):
    """Non-password PdfiumErrors are not masked as EncryptedPDFError."""

    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def boom(*a, **k):
        raise criba.pdfium.PdfiumError("corrupt", err_code=1)

    monkeypatch.setattr(criba.pdfium, "PdfDocument", boom)

    with pytest.raises(criba.pdfium.PdfiumError):
        extract_pdf(pdf, output_dir=tmp_path / "out")


def test_extract_pdf_closes_handles_on_page_error(tmp_path, monkeypatch):
    """A failure mid-page must still close the page and document handles."""

    closed = {"page": False, "doc": False}

    class FakePage:
        def get_width(self):
            return 612.0

        def get_height(self):
            return 792.0

        def close(self):
            closed["page"] = True

    class FakeDoc:
        def __len__(self):
            return 1

        def get_page(self, i):
            return FakePage()

        def close(self):
            closed["doc"] = True

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")  # presence-only; never really parsed

    monkeypatch.setattr(criba.pdfium, "PdfDocument", lambda *a, **k: FakeDoc())
    monkeypatch.setattr(criba, "_extract_metadata", lambda doc: {})
    monkeypatch.setattr(
        criba,
        "_extract_raw_text",
        lambda page: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        extract_pdf(pdf, output_dir=tmp_path / "out")

    assert closed["page"], "page handle was not closed on error"
    assert closed["doc"], "document handle was not closed on error"


# ── _extract_images ───────────────────────────────────────────────────────────


def test_extract_images_numbering_has_no_gap_on_failure(tmp_path, monkeypatch):
    """If the first image fails to extract, the next success is fig_001, not fig_002."""

    images_dir = tmp_path / "imgs"
    images_dir.mkdir()

    class FakeObj:
        raw = object()

        def get_bounds(self):
            return (0.0, 0.0, 10.0, 10.0)

    class FakePage:
        def get_objects(self, filter):  # noqa: A002 - matches pdfium signature
            return [FakeObj(), FakeObj()]

    calls = {"n": 0}

    class FakeImage:
        def __init__(self, raw, page=None):
            pass

        def get_px_size(self):
            return (4, 4)

        def extract(self, prefix, fb_format="png"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first image fails")
            Path(prefix + ".png").write_bytes(b"x")  # mimic pdfium writing the file

    monkeypatch.setattr(criba.pdfium, "PdfImage", FakeImage)

    results = _extract_images(
        FakePage(), page_idx=0, page_height=100.0, images_dir=images_dir
    )

    assert len(results) == 1
    assert results[0]["index"] == 1
    assert results[0]["file"].endswith("page_001_fig_001.png")


# ── _extract_metadata ─────────────────────────────────────────────────────────


def test_extract_metadata_logs_pdfium_errors_at_debug(caplog):
    """PdfiumError during metadata extraction is logged at debug, not swallowed."""

    class FakeDoc:
        def __len__(self):
            return 3

        def get_metadata_dict(self):
            raise criba.pdfium.PdfiumError("no metadata")

        def get_version(self):
            raise criba.pdfium.PdfiumError("no version")

        def is_tagged(self):
            raise criba.pdfium.PdfiumError("no tag info")

    with caplog.at_level("DEBUG", logger="criba"):
        meta = criba._extract_metadata(FakeDoc())

    # Degrades gracefully: page_count still set, optional fields skipped.
    assert meta == {"page_count": 3}
    # But each failure left a debug breadcrumb.
    assert len(caplog.records) == 3
    assert all(r.levelname == "DEBUG" for r in caplog.records)


def test_extract_metadata_omits_version_when_none(caplog):
    """get_version() returning None yields no pdf_version key and no error log."""

    class FakeDoc:
        def __len__(self):
            return 1

        def get_metadata_dict(self):
            return {}

        def get_version(self):
            return None

        def is_tagged(self):
            return False

    with caplog.at_level("DEBUG", logger="criba"):
        meta = criba._extract_metadata(FakeDoc())

    assert "pdf_version" not in meta
    assert meta["page_count"] == 1
    assert meta["tagged"] is False
    assert caplog.records == []
