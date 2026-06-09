"""JSON Schema for criba's PDF-extraction output.

Single source of truth for the structure that :func:`criba.extract` produces
and :func:`criba.to_json` writes. Keep this in sync with the dicts built across
``criba.py``; :func:`validate_output` (and the ``validate=`` flag on
``extract`` / ``convert``) checks a result against this schema.

Note this describes the **JSON-serialisable view** of a result: the in-memory
``data`` bytes carried on each image entry by :func:`criba.extract` are dropped
before serialisation (and before validation), so they are not part of the
schema.

The schema doubles as a machine-readable contract for downstream consumers
(e.g. an LLM agent calling criba as a tool) that previously had only the
prose description in ``README.md`` to rely on.
"""

from __future__ import annotations

OUTPUT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "criba PDF extraction output",
    "type": "object",
    "properties": {
        "source_file": {"type": "string"},
        "metadata": {"$ref": "#/$defs/metadata"},
        "pages": {"type": "array", "items": {"$ref": "#/$defs/page"}},
    },
    "required": ["source_file", "metadata", "pages"],
    "additionalProperties": False,
    "$defs": {
        "bbox": {
            "type": "object",
            "description": "Top-left origin; PDF points (1pt = 1/72 inch).",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "w": {"type": "number"},
                "h": {"type": "number"},
            },
            "required": ["x", "y", "w", "h"],
            "additionalProperties": False,
        },
        "metadata": {
            "type": "object",
            "description": (
                "Document metadata. Only 'page_count' is guaranteed; the "
                "string fields appear only when present in the PDF. "
                "creationdate/moddate are ISO-8601 when parseable, else the "
                "raw 'D:YYYYMMDD...' string."
            ),
            "properties": {
                "title": {"type": "string"},
                "author": {"type": "string"},
                "subject": {"type": "string"},
                "creator": {"type": "string"},
                "producer": {"type": "string"},
                "creationdate": {"type": "string"},
                "moddate": {"type": "string"},
                "keywords": {"type": "string"},
                "page_count": {"type": "integer"},
                "pdf_version": {"type": "string"},
                "tagged": {"type": "boolean"},
            },
            "required": ["page_count"],
            "additionalProperties": False,
        },
        "font": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "size": {"type": "number"},
                "weight": {"type": "integer"},
            },
            "required": ["name", "size", "weight"],
            "additionalProperties": False,
        },
        "color": {
            "type": "object",
            "description": "RGBA fill colour, 0-255 per channel.",
            "properties": {
                "r": {"type": "integer"},
                "g": {"type": "integer"},
                "b": {"type": "integer"},
                "a": {"type": "integer"},
            },
            "required": ["r", "g", "b", "a"],
            "additionalProperties": False,
        },
        "span": {
            "type": "object",
            "description": "A run of text sharing one font + size + weight + colour.",
            "properties": {
                "text": {"type": "string"},
                "bbox": {"$ref": "#/$defs/bbox"},
                "font": {"$ref": "#/$defs/font"},
                "color": {"$ref": "#/$defs/color"},
            },
            "required": ["text", "bbox", "font", "color"],
            "additionalProperties": False,
        },
        "image": {
            "type": "object",
            "properties": {
                "index": {"type": "integer"},
                "bbox": {"$ref": "#/$defs/bbox"},
                "size_px": {
                    "type": "object",
                    "properties": {
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                    },
                    "required": ["width", "height"],
                    "additionalProperties": False,
                },
                "ext": {
                    "type": "string",
                    "description": (
                        "Encoded image format / filename extension: 'jpg' or "
                        "'jp2' for passthrough streams, 'png' when re-encoded."
                    ),
                },
                "file": {"type": "string"},
            },
            "required": ["index", "bbox", "size_px", "ext", "file"],
            "additionalProperties": False,
        },
        "page": {
            "type": "object",
            "properties": {
                "page_number": {"type": "integer"},
                "width": {"type": "number"},
                "height": {"type": "number"},
                "raw_text": {"type": "string"},
                "text_spans": {"type": "array", "items": {"$ref": "#/$defs/span"}},
                "images": {"type": "array", "items": {"$ref": "#/$defs/image"}},
                "warning": {"type": "string", "enum": ["no_text_layer"]},
            },
            "required": [
                "page_number",
                "width",
                "height",
                "raw_text",
                "text_spans",
                "images",
            ],
            "additionalProperties": False,
        },
    },
}


