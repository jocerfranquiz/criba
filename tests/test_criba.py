"""Starter tests for criba's pure helpers and basic error handling."""

from pathlib import Path

import pytest

import criba
from criba import (
    _bbox_union,
    _extract_images,
    _normalize_bbox,
    _strip_subset_prefix,
    extract_pdf,
)


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
