#!/usr/bin/env python3
"""Render the sample PDF into page images and contact sheets for visual review."""

from __future__ import annotations

import argparse
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont, ImageOps


CREAM = "#F9F3EC"
MAROON = "#6B1F2B"
NAVY = "#24384B"
LINE = "#D8D0C8"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def render_pages(pdf_path: Path, page_dir: Path, dpi: int) -> list[Path]:
    document = fitz.open(pdf_path)
    page_dir.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72
    matrix = fitz.Matrix(scale, scale)
    output_paths: list[Path] = []
    for page_number, page in enumerate(document, start=1):
        pixmap = page.get_pixmap(matrix=matrix, alpha=False, colorspace=fitz.csRGB)
        output_path = page_dir / f"page-{page_number:02d}.png"
        pixmap.save(output_path)
        output_paths.append(output_path)
    document.close()
    return output_paths


def make_contact_sheet(
    page_paths: list[Path],
    output_path: Path,
    *,
    columns: int,
    thumb_width: int,
    label: str,
) -> None:
    gap = 22
    margin = 34
    header = 72
    caption = 30
    with Image.open(page_paths[0]) as sample:
        thumb_height = round(thumb_width * sample.height / sample.width)
    rows = (len(page_paths) + columns - 1) // columns
    width = margin * 2 + columns * thumb_width + (columns - 1) * gap
    height = header + margin + rows * (thumb_height + caption) + max(0, rows - 1) * gap + margin
    canvas = Image.new("RGB", (width, height), CREAM)
    draw = ImageDraw.Draw(canvas)
    title_font = ImageFont.truetype(BOLD, 27)
    page_font = ImageFont.truetype(FONT, 16)
    draw.text((margin, 24), label, fill=NAVY, font=title_font)
    draw.line((margin, 61, margin + 72, 61), fill=MAROON, width=4)

    for index, page_path in enumerate(page_paths):
        row = index // columns
        column = index % columns
        x = margin + column * (thumb_width + gap)
        y = header + margin + row * (thumb_height + caption + gap)
        with Image.open(page_path) as page_image:
            thumb = ImageOps.fit(
                page_image.convert("RGB"),
                (thumb_width, thumb_height),
                method=Image.Resampling.LANCZOS,
            )
        canvas.paste(thumb, (x, y))
        draw.rectangle((x, y, x + thumb_width, y + thumb_height), outline=LINE, width=2)
        draw.text(
            (x, y + thumb_height + 8),
            f"Page {int(page_path.stem.split('-')[-1])}",
            fill=MAROON,
            font=page_font,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path(
            "outputs/client-preview/"
            "NeuraFlow_Product_Opportunity_Brief_Illustrative_2026-08-15.pdf"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("media/client-preview/pdf-review"),
    )
    parser.add_argument("--dpi", type=int, default=118)
    args = parser.parse_args()

    page_paths = render_pages(args.pdf, args.output_dir / "pages", args.dpi)
    make_contact_sheet(
        page_paths,
        args.output_dir / "00-all-pages-overview.png",
        columns=4,
        thumb_width=250,
        label="P5 · All 20 pages",
    )
    make_contact_sheet(
        page_paths[:5] + page_paths[17:],
        args.output_dir / "01-framework-pages.png",
        columns=2,
        thumb_width=430,
        label="P5 · Framework, supplier, coverage and method",
    )
    make_contact_sheet(
        page_paths[5:11],
        args.output_dir / "02-opportunities-01-06.png",
        columns=2,
        thumb_width=430,
        label="P5 · Opportunities 01–06",
    )
    make_contact_sheet(
        page_paths[11:17],
        args.output_dir / "03-opportunities-07-12.png",
        columns=2,
        thumb_width=430,
        label="P5 · Opportunities 07–12",
    )
    for path in page_paths:
        print(path)
    print(args.output_dir / "00-all-pages-overview.png")
    print(args.output_dir / "01-framework-pages.png")
    print(args.output_dir / "02-opportunities-01-06.png")
    print(args.output_dir / "03-opportunities-07-12.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
