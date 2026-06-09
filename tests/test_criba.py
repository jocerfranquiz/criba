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
    convert,
    extract,
    to_images,
    to_json,
    to_markdown, validate_output,
)
from schema import OUTPUT_SCHEMA

SAMPLE_PDF = Path(__file__).resolve().parent / "sample.pdf"


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


def test_coalesce_lines_does_not_mutate_input():
    """Inputs (list order and dict contents) are untouched; calls are repeatable."""
    import copy

    spans = [
        _span("Hello", x=0, y=0, w=30, h=12),
        _span("world", x=40, y=0, w=30, h=12),
    ]
    snapshot = copy.deepcopy(spans)

    first = _coalesce_lines(spans)
    assert spans == snapshot, "input list/dicts were mutated by the first call"

    # A second call on the same list must reproduce the first result exactly.
    second = _coalesce_lines(spans)
    assert spans == snapshot, "input list/dicts were mutated by the second call"
    assert [s["text"] for s in first] == [s["text"] for s in second] == ["Hello world"]


def test_coalesce_lines_line_overlap_param_splits_overlapping_spans():
    """High line_overlap=1.0 forces near-identical spans onto separate lines."""
    a = _span("top", x=0, y=0, w=30, h=12)
    b = _span("bot", x=0, y=6, w=30, h=12)  # overlaps by 6/12 = 0.5 exactly

    # With default 0.5 threshold (overlap must be *strictly* greater), these split.
    result_default = _coalesce_lines([a, b])
    assert len(result_default) == 2

    # With a very low threshold they merge into one line.
    result_low = _coalesce_lines([a, b], line_overlap=0.1)
    assert len(result_low) == 1


def test_coalesce_lines_space_gap_param_suppresses_space():
    """High space_gap suppresses the inserted space; low threshold allows it."""
    # gap=10pt; font size=12 -> gap/size=0.83, above default 0.25 -> space inserted
    result_no_space = _coalesce_lines(
        [_span("Hello", x=0, y=0, w=30, h=12), _span("world", x=40, y=0, w=30, h=12)],
        space_gap=1000.0,
    )
    assert result_no_space[0]["text"] == "Helloworld"

    result_space = _coalesce_lines(
        [_span("Hello", x=0, y=0, w=30, h=12), _span("world", x=40, y=0, w=30, h=12)],
        space_gap=0.0,
    )
    assert result_space[0]["text"] == "Hello world"


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


# ── convert ───────────────────────────────────────────────────────────────


def test_convert_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        convert(tmp_path / "does_not_exist.pdf", output_dir=tmp_path / "out")


def _password_error():
    return criba.PdfiumError("bad password", err_code=criba.FPDF_ERR_PASSWORD)


def test_convert_encrypted_raises_friendly_error(tmp_path, monkeypatch):
    """A password-coded PdfiumError becomes an EncryptedPDFError with guidance."""

    pdf = tmp_path / "secret.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def boom(*a, **k):
        raise _password_error()

    monkeypatch.setattr(criba, "PdfDocument", boom)

    with pytest.raises(EncryptedPDFError, match="supply a password"):
        convert(pdf, output_dir=tmp_path / "out")

    # With a (wrong) password supplied, the message reflects that instead.
    with pytest.raises(EncryptedPDFError, match="incorrect password"):
        convert(pdf, output_dir=tmp_path / "out", password="nope")


def test_convert_non_password_pdfium_error_propagates(tmp_path, monkeypatch):
    """Non-password PdfiumErrors are not masked as EncryptedPDFError."""

    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def boom(*a, **k):
        raise criba.PdfiumError("corrupt", err_code=1)

    monkeypatch.setattr(criba, "PdfDocument", boom)

    with pytest.raises(criba.PdfiumError):
        convert(pdf, output_dir=tmp_path / "out")


def test_convert_closes_handles_on_page_error(tmp_path, monkeypatch):
    """A failure mid-page must still close the page and document handles."""

    closed = {"page": False, "doc": False, "textpage": False}

    class FakeTextpage:
        def close(self):
            closed["textpage"] = True

    class FakePage:
        def get_width(self):
            return 612.0

        def get_height(self):
            return 792.0

        def get_textpage(self):
            return FakeTextpage()

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

    monkeypatch.setattr(criba, "PdfDocument", lambda *a, **k: FakeDoc())
    monkeypatch.setattr(criba, "_extract_metadata", lambda doc: {})
    monkeypatch.setattr(
        criba,
        "_extract_raw_text",
        lambda textpage: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        convert(pdf, output_dir=tmp_path / "out")

    assert closed["textpage"], "textpage handle was not closed on error"
    assert closed["page"], "page handle was not closed on error"
    assert closed["doc"], "document handle was not closed on error"


def test_extract_writes_nothing_to_disk(tmp_path, monkeypatch):
    """The pure path returns the dict without creating any files or directories."""

    class FakeTextpage:
        def close(self):
            pass

    class FakePage:
        def get_width(self):
            return 612.0

        def get_height(self):
            return 792.0

        def get_textpage(self):
            return FakeTextpage()

        def get_objects(self, filter):  # noqa: A002 - matches pdfium signature
            return []

        def close(self):
            pass

    class FakeDoc:
        def __len__(self):
            return 1

        def get_page(self, i):
            return FakePage()

        def close(self):
            pass

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(criba, "PdfDocument", lambda *a, **k: FakeDoc())
    monkeypatch.setattr(criba, "_extract_metadata", lambda doc: {"page_count": 1})
    monkeypatch.setattr(criba, "_extract_raw_text", lambda textpage: "hello\n")
    monkeypatch.setattr(criba, "_extract_text_spans", lambda *a, **k: [])

    before = set(tmp_path.iterdir())
    result = extract(pdf)
    after = set(tmp_path.iterdir())

    assert before == after, "extract wrote something to disk"
    assert result["source_file"] == "doc.pdf"
    assert result["pages"][0]["raw_text"] == "hello\n"
    assert result["pages"][0]["images"] == []  # no image objects on the page


# ── _extract_images ───────────────────────────────────────────────────────────


def test_extract_images_numbering_has_no_gap_on_failure(monkeypatch):
    """If the first image fails to extract, the next success is fig_001, not fig_002."""

    class FakeObj:
        raw = object()

        def get_bounds(self):
            return 0.0, 0.0, 10.0, 10.0

    class FakePage:
        def get_objects(self, filter):  # noqa: A002 - matches pdfium signature
            return [FakeObj(), FakeObj()]

    calls = {"n": 0}

    class FakeImage:
        def __init__(self, raw, page=None):
            pass

        def get_px_size(self):
            return (4, 4)

        def get_filters(self):
            return ["FlateDecode"]  # -> re-encoded PNG

        def extract(self, dest, fb_format="png"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise criba.PdfiumError("first image fails")
            dest.write(b"\x89PNG\r\n")  # mimic pdfium writing into the buffer

    monkeypatch.setattr(criba, "PdfImage", FakeImage)

    results = _extract_images(FakePage(), page_idx=0, page_height=100.0, stem="doc")

    assert len(results) == 1
    assert results[0]["index"] == 1
    assert results[0]["ext"] == "png"
    assert results[0]["file"] == "doc_images/page_001_fig_001.png"
    assert results[0]["data"] == b"\x89PNG\r\n"  # bytes held in memory, not on disk


# ── _extract_metadata ─────────────────────────────────────────────────────────


def test_extract_metadata_logs_pdfium_errors_at_debug(caplog):
    """PdfiumError during metadata extraction is logged at debug, not swallowed."""

    class FakeDoc:
        def __len__(self):
            return 3

        def get_metadata_dict(self):
            raise criba.PdfiumError("no metadata")

        def get_version(self):
            raise criba.PdfiumError("no version")

        def is_tagged(self):
            raise criba.PdfiumError("no tag info")

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


# ── schema ────────────────────────────────────────────────────────────────────

jsonschema = pytest.importorskip("jsonschema")


def _minimal_result():
    """A schema-conformant result with one fully-populated page."""
    return {
        "source_file": "doc.pdf",
        "metadata": {"page_count": 1},
        "pages": [
            {
                "page_number": 1,
                "width": 612.0,
                "height": 792.0,
                "raw_text": "hello\n",
                "text_spans": [
                    {
                        "text": "hello",
                        "bbox": {"x": 0.0, "y": 0.0, "w": 30.0, "h": 12.0},
                        "font": {"name": "Arial", "size": 12.0, "weight": 400},
                        "color": {"r": 0, "g": 0, "b": 0, "a": 255},
                    }
                ],
                "images": [
                    {
                        "index": 1,
                        "bbox": {"x": 0.0, "y": 0.0, "w": 10.0, "h": 10.0},
                        "size_px": {"width": 4, "height": 4},
                        "ext": "png",
                        "file": "doc_images/page_001_fig_001.png",
                    }
                ],
            }
        ],
    }


def test_output_schema_is_itself_valid():
    """OUTPUT_SCHEMA conforms to the JSON Schema meta-schema."""
    jsonschema.Draft202012Validator.check_schema(OUTPUT_SCHEMA)


def test_validate_output_accepts_conformant_result():
    validate_output(_minimal_result())  # must not raise


def test_validate_output_accepts_scanned_page_warning():
    result = _minimal_result()
    page = result["pages"][0]
    page["text_spans"] = []
    page["images"] = []
    page["warning"] = "no_text_layer"
    validate_output(result)  # must not raise


def test_validate_output_rejects_unknown_top_level_key():
    result = _minimal_result()
    result["extra"] = "nope"
    with pytest.raises(jsonschema.ValidationError):
        validate_output(result)


def test_validate_output_rejects_missing_page_count():
    result = _minimal_result()
    del result["metadata"]["page_count"]
    with pytest.raises(jsonschema.ValidationError):
        validate_output(result)


def test_validate_output_rejects_bad_warning_enum():
    result = _minimal_result()
    result["pages"][0]["warning"] = "something_else"
    with pytest.raises(jsonschema.ValidationError):
        validate_output(result)


# ── pipeline (real fixture PDF) ───────────────────────────────────────────────

pytestmark_fixture = pytest.mark.skipif(
    not SAMPLE_PDF.exists(),
    reason="sample.pdf fixture missing; run tests/make_sample_pdf.py",
)


@pytestmark_fixture
def test_extract_fixture_in_memory_no_disk_writes(tmp_path, monkeypatch):
    """extract() on the real fixture yields data + in-memory image bytes only."""
    monkeypatch.chdir(tmp_path)  # any stray writes would land here
    before = set(tmp_path.iterdir())

    result = extract(SAMPLE_PDF, validate=True)

    assert set(tmp_path.iterdir()) == before, "extract wrote to disk"
    assert result["metadata"]["title"] == "criba sample document"
    assert result["metadata"]["page_count"] == 2
    assert len(result["pages"]) == 2

    img = result["pages"][0]["images"][0]
    assert img["ext"] == "jpg"  # DCTDecode passthrough, not re-encoded
    assert img["data"][:2] == b"\xff\xd8"  # JPEG SOI magic
    assert result["pages"][1]["images"] == []  # second page has no images


@pytestmark_fixture
def test_to_json_excludes_image_data_and_validates(tmp_path):
    """to_json drops the in-memory bytes and emits a schema-conformant file."""
    import json

    result = extract(SAMPLE_PDF)
    dest = tmp_path / "out" / "sample.json"
    to_json(result, dest)

    loaded = json.loads(dest.read_text())
    assert "data" not in loaded["pages"][0]["images"][0]
    assert loaded["pages"][0]["images"][0]["file"].endswith(".jpg")
    validate_output(loaded)  # the on-disk JSON view conforms


@pytestmark_fixture
def test_to_images_writes_image_bytes(tmp_path):
    """to_images materialises exactly the in-memory bytes, only where images exist."""
    result = extract(SAMPLE_PDF)
    written = to_images(result, tmp_path)

    assert len(written) == 1
    dest = tmp_path / "sample_images" / "page_001_fig_001.jpg"
    assert written == [dest]
    assert dest.read_bytes() == result["pages"][0]["images"][0]["data"]


@pytestmark_fixture
def test_to_markdown_infers_headings_and_inlines_images():
    """to_markdown() ranks font sizes into headings and inlines image references."""
    text = to_markdown(extract(SAMPLE_PDF))

    assert "# Sample Document Title" in text  # 24pt -> h1
    assert "## Second Page Heading" in text  # 18pt -> h2
    assert "This is body text for testing extraction." in text  # 12pt body, no '#'
    assert "![](sample_images/page_001_fig_001.jpg)" in text


@pytestmark_fixture
def test_convert_writes_json_md_and_images(tmp_path):
    """The convenience wrapper produces the full json + md + images output set."""
    convert(SAMPLE_PDF, output_dir=tmp_path)

    assert (tmp_path / "sample.json").is_file()
    assert (tmp_path / "sample.md").is_file()
    assert (tmp_path / "sample_images" / "page_001_fig_001.jpg").is_file()
