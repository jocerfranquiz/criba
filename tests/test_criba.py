"""Starter tests for criba's pure helpers and basic error handling."""

import pytest

from criba import (
    _bbox_union,
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
