#!/usr/bin/env python3
"""Render P6 brochure and offer PDFs into inspection images."""

from __future__ import annotations

import argparse
from pathlib import Path

from generate_client_preview_package import BROCHURE_FILENAME, OFFER_FILENAME
from render_pdf_review import make_contact_sheet, render_pages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("media/client-preview/brochure-review-revised"),
    )
    parser.add_argument("--pdf-dir", type=Path, default=Path("outputs/client-preview"))
    parser.add_argument("--dpi", type=int, default=144)
    args = parser.parse_args()

    brochure_pages = render_pages(
        args.pdf_dir / BROCHURE_FILENAME,
        args.output_dir / "brochure-pages",
        args.dpi,
    )
    offer_pages = render_pages(
        args.pdf_dir / OFFER_FILENAME,
        args.output_dir / "offer-pages",
        args.dpi,
    )
    if len(brochure_pages) != 5 or len(offer_pages) != 1:
        raise RuntimeError("Unexpected P6 PDF page count during review rendering.")
    make_contact_sheet(
        brochure_pages,
        args.output_dir / "00-brochure-overview.png",
        columns=2,
        thumb_width=430,
        label="P6 · Product Opportunity Radar service overview",
    )
    make_contact_sheet(
        [brochure_pages[index] for index in (1, 2, 3, 4)],
        args.output_dir / "01-brochure-detail.png",
        columns=2,
        thumb_width=430,
        label="P6 · Outputs, selection, sample and monthly service",
    )
    make_contact_sheet(
        offer_pages,
        args.output_dir / "02-offer-summary.png",
        columns=1,
        thumb_width=720,
        label="P6 · Monthly service offer",
    )
    for path in brochure_pages + offer_pages:
        print(path)
    for name in ("00-brochure-overview.png", "01-brochure-detail.png", "02-offer-summary.png"):
        print(args.output_dir / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
