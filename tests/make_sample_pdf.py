#!/usr/bin/env python3
"""Generate ``tests/sample.pdf`` — a tiny fixture with a text layer and one
embedded JPEG, used by the test suite and as a demo document.

Hand-builds a minimal PDF (no PDF-writer dependency) so the fixture is fully
reproducible.  The single binary dependency is Pillow, already required by
criba, used only to synthesise the embedded JPEG bytes.

Run from the repo root::

    python tests/make_sample_pdf.py

The committed ``sample.pdf`` is the source of truth for tests; only re-run this
if you intentionally want to change the fixture.
"""

from __future__ import annotations

import io

from pathlib import Path

from PIL import Image


def _jpeg_bytes() -> bytes:
    """A small RGB JPEG so the DCTDecode passthrough path is exercised."""
    img = Image.new("RGB", (64, 48), (200, 60, 60))
    # A couple of blocks so it isn't a single flat colour.
    for x in range(32, 64):
        for y in range(0, 48):
            img.putpixel((x, y), (60, 90, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _content_stream(lines: list[bytes]) -> bytes:
    return b"\n".join(lines) + b"\n"


def build_pdf() -> bytes:
    jpeg = _jpeg_bytes()

    # Page 1: title (24pt) + body (12pt) + the embedded image.
    page1_content = _content_stream(
        [
            b"BT /F1 24 Tf 72 720 Td (Sample Document Title) Tj ET",
            b"BT /F1 12 Tf 72 690 Td (This is body text for testing extraction.) Tj ET",
            b"q 100 0 0 75 72 540 cm /Im1 Do Q",
        ]
    )
    # Page 2: heading (18pt) + body (12pt), no image.
    page2_content = _content_stream(
        [
            b"BT /F1 18 Tf 72 720 Td (Second Page Heading) Tj ET",
            b"BT /F1 12 Tf 72 690 Td (More body text on the second page.) Tj ET",
        ]
    )

    # Each object's body, indexed 1..N (index 0 is the free head entry).
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",  # 1
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",  # 2
        (  # 3 — page 1
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> /XObject << /Im1 6 0 R >> >> "
            b"/Contents 7 0 R >>"
        ),
        (  # 4 — page 2
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 8 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",  # 5
        (  # 6 — image XObject (JPEG stream)
            b"<< /Type /XObject /Subtype /Image /Width 64 /Height 48 "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
            + str(len(jpeg)).encode()
            + b" >>\nstream\n"
            + jpeg
            + b"\nendstream"
        ),
        (  # 7 — page 1 content
            b"<< /Length "
            + str(len(page1_content)).encode()
            + b" >>\nstream\n"
            + page1_content
            + b"endstream"
        ),
        (  # 8 — page 2 content
            b"<< /Length "
            + str(len(page2_content)).encode()
            + b" >>\nstream\n"
            + page2_content
            + b"endstream"
        ),
        (  # 9 — document info
            b"<< /Title (criba sample document) /Author (criba) "
            b"/CreationDate (D:20240115093000+00'00') >>"
        ),
    ]

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_pos = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(n).encode()
        + b" /Root 1 0 R /Info 9 0 R >>\nstartxref\n"
        + str(xref_pos).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def main() -> None:
    dest = Path(__file__).resolve().parent / "sample.pdf"
    dest.write_bytes(build_pdf())
    print(f"wrote {dest} ({dest.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
