#!/usr/bin/env python3
"""Print the synthetic fixture issue with the PRODUCTION report code and engine.

Builds the rich synthetic issue through the application's real writers
(``tests/report_redesign_fixture.py``; invented products, invented supplier
names, drawn placeholder images, no research data), renders both language
editions with ``live_outputs.render_html`` and prints them with
``scripts/render_live_pdf.py`` (native Windows Chrome via PowerShell, the same
flags and the same PDF inspection as production). Saves every page as a PNG
next to this script plus a greyscale check, and prints the page map. A stress
edition of each language (the title at its 100-character limit, every writing
slot at its budget, the updated type also compliance-flagged; printed with
``report_view.render_document``, never a deliverable) is printed too: its cover
and the hardest page one (flagged and updated) are saved as ``*-stress-*.png``.

Usage, from the repository root with the project venv:
    PYTHONPATH=src:tests python docs/client-preview/design/report-redesign-2026-10-16/production-renders/build_production_renders.py
"""

from pathlib import Path
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "scripts")]

import fitz  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

import render_live_pdf  # noqa: E402
from product_radar import live_outputs  # noqa: E402
from product_radar.live_outputs import render_html  # noqa: E402
from product_radar.report_view import render_document  # noqa: E402
from report_redesign_fixture import rich_issue, stress_edition  # noqa: E402
from test_report_redesign_chrome import product_spans  # noqa: E402

DPI = 110


def compress(path):
    """256-colour palette PNGs: text stays crisp, files shrink to about a third."""
    image = Image.open(path)
    mode = "L" if image.mode == "L" else "RGB"
    image.convert(mode).quantize(colors=256, method=Image.Quantize.MEDIANCUT if mode == "RGB" else None,
                                 dither=Image.Dither.NONE).save(path, optimize=True)


def main():
    for old in HERE.glob("*.png"):
        old.unlink()
    with tempfile.TemporaryDirectory(prefix="report-renders-") as folder:
        folder = Path(folder)
        _, issue, dataset = rich_issue(folder / "fixture")
        for language in ("en", "hi-Latn"):
            html = folder / f"report-{language}.html"
            html.write_text(render_html(issue, dataset, language=language, root=folder / "fixture"), encoding="utf-8")
            result = render_live_pdf.render(html, folder / f"report-{language}.pdf", timeout=120)
            with fitz.open(result["path"]) as document:
                spans = product_spans(document)
                if language == "en":
                    crowded = next(n for n, product in enumerate(dataset["products"])
                                   if product["opportunity_basis"]["parts"]["room"]["items"]["crowding"]["facts"]["level"] == "high")
                    grey_pages = (2, spans[crowded][0] + 2)  # the glance and a crowded product's market page
                print(language, "pages:", len(document), "html KB:", html.stat().st_size // 1024,
                      "pdf KB:", result["bytes"] // 1024, "product spans:", [pages for _, pages in spans])
                for number, page in enumerate(document, 1):
                    path = HERE / f"{language}-a4-page-{number:02d}.png"
                    page.get_pixmap(dpi=DPI).save(path)
            if "--keep-html" in sys.argv:
                shutil.copy(html, HERE / f"report-{language}.html")
            root = folder / "fixture"
            stressed, narrative = stress_edition(dataset, language)
            stress = folder / f"stress-{language}.html"
            stress.write_text(render_document(issue, stressed, narrative, language,
                                              logo=live_outputs._owned_asset(dataset, "brand-logo", root=root),
                                              images=[live_outputs._owned_asset(dataset, p["concept_id"], root=root)
                                                      for p in dataset["products"]]), encoding="utf-8")
            result = render_live_pdf.render(stress, folder / f"stress-{language}.pdf", timeout=120)
            with fitz.open(result["path"]) as document:
                spans = product_spans(document)
                updated = next(n for n, product in enumerate(stressed["products"]) if product["status"] == "Updated")
                print(language, "stress pages:", len(document), "product spans:", [pages for _, pages in spans])
                document[0].get_pixmap(dpi=DPI).save(HERE / f"{language}-stress-cover-100-character-title.png")
                document[spans[updated][0]].get_pixmap(dpi=DPI).save(
                    HERE / f"{language}-stress-page-one-flagged-updated-at-budgets.png")
        grey = [ImageOps.grayscale(Image.open(HERE / f"en-a4-page-{n:02d}.png")) for n in grey_pages]
        sheet = Image.new("L", (grey[0].width * 2 + 10, grey[0].height), 128)
        sheet.paste(grey[0], (0, 0))
        sheet.paste(grey[1], (grey[0].width + 10, 0))
        sheet.save(HERE / f"en-greyscale-check-pages-{grey_pages[0]:02d}-{grey_pages[1]:02d}.png")
    for path in HERE.glob("*.png"):
        compress(path)
    print("total MB:", round(sum(path.stat().st_size for path in HERE.glob("*.png")) / 1e6, 1))


if __name__ == "__main__":
    main()
