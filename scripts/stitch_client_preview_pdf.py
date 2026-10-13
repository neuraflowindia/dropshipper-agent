#!/usr/bin/env python3
"""Combine the client-facing P6 PDFs into one continuous preview document."""

from __future__ import annotations

import argparse
from pathlib import Path

from pypdf import PdfReader, PdfWriter


DEFAULT_INPUTS = (
    "NeuraFlow_Product_Opportunity_Radar_Service_Overview_2026-08-15.pdf",
    "NeuraFlow_Product_Opportunity_Brief_Illustrative_2026-08-15.pdf",
    "NeuraFlow_Monthly_Service_Offer_2026-08-15.pdf",
)
DEFAULT_OUTPUT = "NeuraFlow_Product_Opportunity_Radar_Client_Preview_2026-08-15.pdf"


def stitch(input_paths: list[Path], output_path: Path) -> int:
    missing = [path for path in input_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Missing PDF input(s): " + ", ".join(str(path) for path in missing)
        )

    writer = PdfWriter()
    expected_pages = 0
    for path in input_paths:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError(f"Encrypted input cannot be combined: {path}")
        expected_pages += len(reader.pages)
        writer.append(path)

    writer.add_metadata(
        {
            "/Title": "NeuraFlow Product Opportunity Radar · Client Preview",
            "/Author": "NeuraFlow",
            "/Subject": "Service overview, illustrative weekly report, and monthly offer",
        }
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        writer.write(handle)

    combined = PdfReader(output_path)
    if combined.is_encrypted:
        raise RuntimeError("Combined PDF is unexpectedly encrypted.")
    if len(combined.pages) != expected_pages:
        raise RuntimeError(
            f"Combined PDF has {len(combined.pages)} pages; expected {expected_pages}."
        )
    if output_path.stat().st_size < 100_000:
        raise RuntimeError("Combined PDF is implausibly small.")
    return expected_pages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=Path("outputs/client-preview"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    input_paths = [args.pdf_dir / name for name in DEFAULT_INPUTS]
    output_path = args.output or args.pdf_dir / DEFAULT_OUTPUT
    page_count = stitch(input_paths, output_path)
    print(output_path)
    print(f"{page_count} pages")
    print(f"{output_path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
