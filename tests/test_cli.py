"""Tests for the command-line entry point."""

from pathlib import Path

import pytest

import cli

SAMPLE_PDF = Path(__file__).resolve().parent / "sample.pdf"

pytestmark = pytest.mark.skipif(
    not SAMPLE_PDF.exists(),
    reason="sample.pdf fixture missing; run tests/make_sample_pdf.py",
)


def test_cli_writes_outputs_and_prints_summary(tmp_path, monkeypatch, capsys):
    """`python cli.py sample.pdf -o <dir>` writes json + md + images and reports."""
    monkeypatch.setattr(
        "sys.argv", ["cli.py", str(SAMPLE_PDF), "-o", str(tmp_path)]
    )

    cli.main()

    assert (tmp_path / "sample.json").is_file()
    assert (tmp_path / "sample.md").is_file()
    assert (tmp_path / "sample_images" / "page_001_fig_001.jpg").is_file()

    out = capsys.readouterr().out
    assert "2 pages" in out
    assert "json + md + images" in out


def test_cli_encrypted_pdf_exits_with_message(tmp_path, monkeypatch, capsys):
    """A friendly error (not a traceback) is shown for an encrypted PDF."""
    import criba

    pdf = tmp_path / "secret.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def boom(*a, **k):
        raise criba.PdfiumError("bad password", err_code=criba.FPDF_ERR_PASSWORD)

    monkeypatch.setattr(criba, "PdfDocument", boom)
    monkeypatch.setattr("sys.argv", ["cli.py", str(pdf), "-o", str(tmp_path / "out")])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert "encrypted" in str(exc.value)
