#!/usr/bin/env python3
"""Prepare generated report imagery and write a deterministic provenance manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


ASSETS = [
    ("DEMO-001", "Oil Spray Bottle", "01-oil-spray-bottle.png"),
    ("DEMO-002", "Rechargeable Cordless Picture Light", "02-rechargeable-cordless-picture-light.png"),
    ("DEMO-003", "Motion-Activated Cat Ball", "03-motion-activated-cat-ball.png"),
    ("DEMO-004", "Silicone Suction Feeding Set", "04-silicone-suction-feeding-set.png"),
    ("DEMO-005", "Manual Vegetable Chopper and Slicer", "05-manual-vegetable-chopper-and-slicer.png"),
    ("DEMO-006", "Pleated Mushroom Table Lamp", "06-pleated-mushroom-table-lamp.png"),
    ("DEMO-007", "Self-Watering Plant Spike Set", "07-self-watering-plant-spike-set.png"),
    ("DEMO-008", "Self-Cleaning Pet Grooming Brush", "08-self-cleaning-pet-grooming-brush.png"),
    ("DEMO-009", "Stroller Organiser Bag", "09-stroller-organiser-bag.png"),
    ("DEMO-010", "Adjustable Baby-Proof Cabinet Lock Set", "10-adjustable-baby-proof-cabinet-lock-set.png"),
    ("DEMO-011", "Clip-On Sink Strainer Basket", "11-clip-on-sink-strainer-basket.png"),
    ("DEMO-012", "Silicone Paw Cleaner Cup", "12-silicone-paw-cleaner-cup.png"),
    ("COVER", "Product Research Still Life", "cover-product-research-still-life.png"),
]

BACKGROUND = "#F9F3EC"
NAVY = "#24384B"
MAROON = "#6B1F2B"
LINE = "#D8D0C8"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def prepare_asset(source: Path, destination: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        source_size = image.size
        image.thumbnail((1000, 1250), Image.Resampling.LANCZOS)
        report_size = image.size
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(
            destination,
            "JPEG",
            quality=84,
            optimize=True,
            progressive=True,
            subsampling="4:2:0",
            dpi=(144, 144),
        )
    return source_size, report_size


def build_contact_sheet(records: list[dict], report_dir: Path, output_path: Path) -> None:
    columns = 3
    tile_width = 420
    image_height = 525
    caption_height = 96
    gap = 22
    margin = 30
    rows = (len(records) + columns - 1) // columns
    width = margin * 2 + columns * tile_width + (columns - 1) * gap
    height = margin * 2 + rows * (image_height + caption_height) + (rows - 1) * gap
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    title_font = load_font("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    meta_font = load_font("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)

    for index, record in enumerate(records):
        row = index // columns
        column = index % columns
        x = margin + column * (tile_width + gap)
        y = margin + row * (image_height + caption_height + gap)
        image_path = report_dir / record["report_file"]
        with Image.open(image_path) as raw:
            image = raw.convert("RGB")
            fitted = ImageOps.fit(
                image,
                (tile_width, image_height),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
        canvas.paste(fitted, (x, y))
        draw.line((x, y + image_height + 10, x + 44, y + image_height + 10), fill=MAROON, width=3)
        label = "\n".join(textwrap.wrap(record["title"], width=34))
        draw.multiline_text(
            (x, y + image_height + 18),
            label,
            font=title_font,
            fill=NAVY,
            spacing=3,
        )
        draw.text(
            (x + tile_width - 2, y + image_height + caption_height - 12),
            record["asset_id"],
            font=meta_font,
            fill=MAROON,
            anchor="ra",
        )

    draw.rectangle((0, 0, width - 1, height - 1), outline=LINE, width=1)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("media/client-preview/product-visuals/source"),
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("media/client-preview/product-visuals/report"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("media/client-preview/product-visuals/manifest.json"),
    )
    parser.add_argument(
        "--contact-sheet",
        type=Path,
        default=Path("media/client-preview/product-visuals/contact-sheet.png"),
    )
    args = parser.parse_args()

    records: list[dict] = []
    for asset_id, title, source_name in ASSETS:
        source_path = args.source_dir / source_name
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        report_name = source_path.with_suffix(".jpg").name
        report_path = args.report_dir / report_name
        source_size, report_size = prepare_asset(source_path, report_path)
        records.append(
            {
                "asset_id": asset_id,
                "title": title,
                "source_file": source_name,
                "source_dimensions": list(source_size),
                "source_sha256": sha256(source_path),
                "report_file": report_name,
                "report_dimensions": list(report_size),
                "report_sha256": sha256(report_path),
                "generation_method": "OpenAI built-in image generation",
                "usage_boundary": "Illustrative product visual; not market evidence",
            }
        )

    build_contact_sheet(records, args.report_dir, args.contact_sheet)
    manifest = {
        "schema_version": "1.0",
        "created_for": "NeuraFlow Product Opportunity Radar manufactured preview",
        "created_on": "2026-08-15",
        "asset_count": len(records),
        "product_asset_count": 12,
        "cover_asset_count": 1,
        "assets": records,
        "contact_sheet": str(args.contact_sheet),
        "prompt_record": "docs/client-preview/design/image_generation_prompts.md",
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(args.manifest)
    print(args.contact_sheet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
