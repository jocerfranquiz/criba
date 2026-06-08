# criba

Minimal, fully-local PDF to JSON raw data extractor.  
Reads native-text PDFs via **pypdfium2** (Chrome's PDFium engine) and emits structured JSON with raw text, text spans, and extracted images.

## Install

```bash
pip install -r requirements.txt
```

For development (linting, tests, git hooks):

```bash
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install
```

## Usage

```bash
python criba.py document.pdf [-o output_dir] [-p PASSWORD]
```

Use `-p/--password` for encrypted PDFs. Without it, an encrypted document
exits with a clear `error: ... is encrypted` message rather than a raw traceback.

### Output

```
output/
├── document.json
└── document_images/
    ├── page_001_fig_001.png
    ├── page_003_fig_002.jpg
    └── ...
```

### Programmatic

```python
from criba import extract_pdf
result = extract_pdf("document.pdf", output_dir="output")
```

## JSON Schema

```jsonc
{
  "source_file": "document.pdf",
  "metadata": {
    "title": "...",
    "author": "...",
    "page_count": 10,
    "pdf_version": "1.7",
    "tagged": false
    // also: subject, creator, producer, creationdate, moddate, keywords
    // creationdate/moddate are normalised to ISO-8601 (e.g. "2024-01-15T09:30:00+05:30");
    // if a date can't be parsed, the raw PDF string (D:YYYYMMDD...) is kept as-is
  },
  "pages": [
    {
      "page_number": 1,
      "width": 612.0,       // points (1pt = 1/72 inch)
      "height": 792.0,
      "raw_text": "Full page text in reading order...\n",
      "text_spans": [
        {
          "text": "Chapter 1",
          "bbox": { "x": 72.0, "y": 54.6, "w": 120.0, "h": 17.7 },
          "font": { "name": "Helvetica-Bold", "size": 24.0, "weight": 700 },
          "color": { "r": 0, "g": 0, "b": 0, "a": 255 }
        }
      ],
      "images": [
        {
          "index": 1,
          "bbox": { "x": 72.0, "y": 200.0, "w": 468.0, "h": 300.0 },
          "size_px": { "width": 1024, "height": 768 },
          "file": "document_images/page_001_fig_001.png"
        }
      ],
      "warning": "no_text_layer"  // only present when applicable
    }
  ]
}
```

## Coordinate System

All bounding boxes use **top-left origin** (y increases downward), in PDF points (1pt = 1/72 in).  
This is a deliberate normalisation from PDF's native bottom-left origin, so downstream vision/layout tools don't have to flip.

- `bbox.x`, `bbox.y` top-left corner of the bounding box
- `bbox.w`, `bbox.h` width and height

## Text Spans

Each span is a run of text sharing the same **font name + size + weight + color**, coalesced from individual PDF text objects.  Spans are sorted into approximate reading order (top→bottom, left→right).

Font subset prefixes (e.g. `ABCDEF+Arial`) are stripped automatically.

`raw_text` is the full page text in PDFium's built-in reading order (separate from spans).  It's redundant by design — downstream consumers can pick whichever representation suits them.

## Image Extraction

Embedded image objects are extracted natively (JPEG/JP2 pass-through when possible, PNG fallback via Pillow).  These are the actual embedded bitmaps, not rasterised page screenshots.

## Known Limitations

| Limitation | Detail |
|---|---|
| **Reading order** | Text span order follows content-stream position, sorted by (y, x). Multi-column layouts may interleave. `raw_text` uses PDFium's heuristic reading order, which is better but not perfect. |
| **Font weight** | `weight` is only populated when the font descriptor includes it. Many standard fonts report 0; infer boldness from the font name if needed. |
| **Scanned PDFs** | Pages with no text layer produce empty `text_spans` and `raw_text`, flagged with `"warning": "no_text_layer"`. OCR is out of scope. |
| **Image edge cases** | Alpha masks (SMask), stripped images, and inline images may extract incorrectly or be skipped. A warning is logged. |
| **Form fields** | Interactive form data is not extracted. |

## Dependencies

- **pypdfium2** Python bindings for PDFium (Apache 2.0 / BSD-3)
- **Pillow** fallback image encoding when PDFium can't extract natively
