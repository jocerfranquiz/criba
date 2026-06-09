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
python cli.py document.pdf [-o output_dir] [-p PASSWORD]
```

Use `-p/--password` for encrypted PDFs. Without it, an encrypted document
exits with a clear `error: ... is encrypted` message rather than a raw traceback.

### Output

```
output/
├── document.json          # structure: metadata, text spans, image refs
├── document.md            # best-effort Markdown for RAG / agents
└── document_images/
    ├── page_001_fig_001.png
    ├── page_003_fig_002.jpg
    └── ...
```

### Programmatic

criba is a small ETL pipeline: **extract** once into an in-memory dict, then
serialise to whichever outputs you need.

```python
from criba import extract, to_json, to_markdown, to_images

result = extract("document.pdf")   # pure: no disk writes; images held in memory

markdown = to_markdown(result)             # structure-aware Markdown string for RAG
json_str = to_json(result)                 # JSON string (image bytes omitted)
to_markdown(result, "out/document.md")     # pass a path to also write it
to_json(result, "out/document.json")       # structure for later md/html rendering
to_images(result, "out")                   # write images under out/<stem>_images/
```

`extract` returns a self-contained dict — image bytes ride along in each image
entry's `data` field — so an agent/tool can consume it directly without touching
the filesystem. `to_markdown` and `to_json` **return** their text (and optionally
write it when given a path); `to_images` writes the embedded bitmaps and returns
the paths. `convert("document.pdf", output_dir="output")` is the convenience
pipeline that runs `extract` and writes all three outputs at once.

`to_markdown()` is **best-effort, RAG-oriented**: it infers headings from
font-size clusters, bold/italic from font weight and name, and inlines image
references. The goal is retrieval quality, not visual fidelity — faithful
Markdown/HTML rendering is a separate concern built on top of the JSON.

### API reference

```python
extract(pdf_path, *, password=None, line_overlap=0.5, space_gap=0.25, validate=False) -> dict
convert(pdf_path, output_dir="output", password=None, line_overlap=0.5, space_gap=0.25, validate=False) -> dict
to_json(result, path=None) -> str            # returns JSON; writes if path given
to_markdown(result, path=None) -> str        # returns Markdown; writes if path given
to_images(result, base_dir) -> list[Path]    # writes image files; returns paths written
```

**Parameters** (shared by `extract` and `convert`):

| Param | Meaning |
|---|---|
| `password` | Password for an encrypted PDF. |
| `line_overlap` | Min vertical overlap, as a fraction of the shorter span's height, for two spans to be grouped on the same line (default `0.5`). |
| `space_gap` | Min horizontal gap, as a fraction of font size, that inserts a space between merged spans (default `0.25`). |
| `validate` | Validate the JSON-serialisable view against `schema.OUTPUT_SCHEMA` before returning. |
| `output_dir` | *(`convert` only)* Where to write `<stem>.json`, `<stem>.md`, and `<stem>_images/` (default `output`). |

**Exceptions** (raised by `extract` and `convert`):

| Exception | When |
|---|---|
| `FileNotFoundError` | `pdf_path` does not exist. |
| `EncryptedPDFError` | The PDF is encrypted and `password` is missing or wrong. |
| `jsonschema.ValidationError` | `validate=True` and the result does not conform to the schema. |

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
          "ext": "png",   // jpg/jp2 for passthrough streams, png when re-encoded
          "file": "document_images/page_001_fig_001.png"
          // in-memory results also carry "data" (raw bytes); to_json omits it
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

| Limitation           | Detail                                                                                                                                                                                                                                                                                                                                                     |
|----------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Reading order**    | Spans are grouped into lines by vertical overlap (so baseline-aligned text of mixed font sizes reads left→right correctly), then ordered top→bottom. Multi-column layouts may still interleave, and super/subscripts may split into their own line. `raw_text` uses PDFium's heuristic reading order, which is better for complex layouts but not perfect. |
| **Font weight**      | `weight` is only populated when the font descriptor includes it. Many standard fonts report 0; infer boldness from the font name if needed.                                                                                                                                                                                                                |
| **Scanned PDFs**     | Pages with no text layer produce empty `text_spans` and `raw_text`, flagged with `"warning": "no_text_layer"`. OCR is out of scope.                                                                                                                                                                                                                        |
| **Image edge cases** | Alpha masks (SMask), stripped images, and inline images may extract incorrectly or be skipped. A warning is logged.                                                                                                                                                                                                                                        |
| **Form fields**      | Interactive form data is not extracted.                                                                                                                                                                                                                                                                                                                    |

## Dependencies

- **pypdfium2** Python bindings for PDFium (Apache 2.0 / BSD-3)
- **Pillow** fallback image encoding when PDFium can't extract natively
