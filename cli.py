#!/usr/bin/env python3
"""Command-line entry point for criba.

Thin wrapper over the :mod:`criba` library: parses arguments, runs
:func:`criba.convert`, and prints a one-line summary.

Usage
-----
    python cli.py document.pdf [-o output] [-p PASSWORD]
"""

from __future__ import annotations

import argparse
import logging

from criba import (
    EncryptedPDFError,
    LINE_OVERLAP_RATIO,
    SPACE_GAP_RATIO,
    convert,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract raw data from a PDF into JSON.")
    ap.add_argument("pdf", help="Input PDF path")
    ap.add_argument(
        "-o", "--output", default="output", help="Output directory (default: ./output)"
    )
    ap.add_argument(
        "-p", "--password", default=None, help="Password for an encrypted PDF"
    )
    ap.add_argument(
        "--line-overlap",
        type=float,
        default=LINE_OVERLAP_RATIO,
        metavar="RATIO",
        help=f"Min vertical overlap fraction to group spans on the same line (default: {LINE_OVERLAP_RATIO})",
    )
    ap.add_argument(
        "--space-gap",
        type=float,
        default=SPACE_GAP_RATIO,
        metavar="RATIO",
        help=f"Min gap/font-size ratio to insert a space between merged spans (default: {SPACE_GAP_RATIO})",
    )
    ap.add_argument(
        "--validate",
        action="store_true",
        help="Validate the result against schema.OUTPUT_SCHEMA before writing "
        "(requires the optional 'jsonschema' package)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        result = convert(
            args.pdf,
            args.output,
            password=args.password,
            line_overlap=args.line_overlap,
            space_gap=args.space_gap,
            validate=args.validate,
        )
    except EncryptedPDFError as exc:
        raise SystemExit(f"error: {exc}")

    pages = len(result["pages"])
    spans = sum(len(p["text_spans"]) for p in result["pages"])
    imgs = sum(len(p["images"]) for p in result["pages"])
    warnings = sum(1 for p in result["pages"] if "warning" in p)

    print(
        f"✓ {pages} pages · {spans} spans · {imgs} images "
        f"→ {args.output}/ (json + md + images)"
    )
    if warnings:
        print(f"{warnings} page(s) with no text layer (scanned?)")


if __name__ == "__main__":
    main()
