#!/usr/bin/env python3
"""Render the mock-ups with the production PDF engine and save PNG previews.

1. PDF: Windows Chrome headless --print-to-pdf with the same flags as
   scripts/render_live_pdf.py (the production renderer), run from WSL.
2. The PDF is checked the way production checks it (portrait pages, text on
   every page) and each page is saved as PNG with PyMuPDF.
3. Phone view: the HTML at 390 px wide (screen media) with Playwright Chromium.
4. "PDF on a phone": page images at 390 px wide (fit-to-width on a phone),
   plus 2x zoom crops, to judge legibility of the A4 PDF itself.

Usage (from the project venv):  python render_previews.py
"""

from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import quote

import fitz
from PIL import Image

HERE = Path(__file__).resolve().parent
OUT = HERE.parent
RENDERS = OUT / "renders"
CHROME = Path("/mnt/c/Program Files/Google/Chrome/Application/chrome.exe")


def win(path):
    return subprocess.run(["wslpath", "-w", str(Path(path).absolute())], capture_output=True, text=True, check=True).stdout.strip()


def print_pdf(html, pdf):
    with tempfile.TemporaryDirectory(prefix="mock-chrome-", dir=HERE) as profile:
        args = [str(CHROME), "--headless=new", "--disable-gpu", "--disable-extensions", "--disable-background-networking",
                "--disable-default-apps", "--no-first-run", "--allow-file-access-from-files",
                "--run-all-compositor-stages-before-draw", "--virtual-time-budget=2500",
                "--no-pdf-header-footer", "--print-to-pdf-no-header", "--user-data-dir=" + win(profile),
                "--print-to-pdf=" + win(pdf), "file:///" + quote(win(html).replace("\\", "/"), safe="/:")]
        subprocess.run(args, check=True, timeout=120, capture_output=True)
    raw = pdf.read_bytes()
    with fitz.open(stream=raw, filetype="pdf") as doc:
        for number, page in enumerate(doc, 1):
            assert page.rect.width < page.rect.height, f"page {number} not portrait"
            assert page.get_text().strip(), f"page {number} has no extractable text"
        fonts = sorted({f[3] for page in doc for f in page.get_fonts()})
        return len(doc), fonts


def pages_to_png(pdf, prefix, dpi=110):
    names = []
    with fitz.open(pdf) as doc:
        for i, page in enumerate(doc, 1):
            name = RENDERS / f"{prefix}-a4-page-{i:02d}.png"
            page.get_pixmap(dpi=dpi).save(name)
            names.append(name)
    return names


def phone_fit(pdf, prefix, pages):
    """What a phone shows with the A4 page fitted to a 390 px wide screen (at 3x pixel density)."""
    with fitz.open(pdf) as doc:
        for i in pages:
            page = doc[i - 1]
            zoom = 390 * 3 / page.rect.width
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            pix.save(RENDERS / f"{prefix}-pdf-on-phone-fit-width-page-{i:02d}.png")


def phone_zoom(pdf, prefix, page_number):
    """A phone at 2x zoom on the A4 PDF: one half-page column fills the 390 px screen (3x pixel density)."""
    with fitz.open(pdf) as doc:
        page = doc[page_number - 1]
        half = page.rect.width / 2
        zoom = 390 * 3 / half
        for side, x0 in (("left", 0), ("right", half)):
            clip = fitz.Rect(x0, 0, x0 + half, half * 844 / 390)
            page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip).save(
                RENDERS / f"{prefix}-pdf-on-phone-2x-zoom-page-{page_number:02d}-{side}.png")


def phone_html(html, prefix):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        shells = sorted(Path.home().glob(".cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
        browser = p.chromium.launch(executable_path=str(shells[-1])) if shells else p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        page.goto(html.as_uri())
        page.emulate_media(media="screen")
        page.wait_for_timeout(400)
        full = RENDERS / f"{prefix}-phone-390px-full.png"
        page.screenshot(path=str(full), full_page=True)
        browser.close()
    # Slice the very tall capture into readable screen-height chunks.
    image = Image.open(full)
    w, h = image.size
    step = 844 * 2 * 2  # two phone screens per slice
    for n, top in enumerate(range(0, h, step), 1):
        image.crop((0, top, w, min(h, top + step))).save(RENDERS / f"{prefix}-phone-390px-part-{n:02d}.png")
    full.unlink()


def greyscale_check():
    """Dashboard and a crowded product page in greyscale: meaning must survive without colour."""
    from PIL import ImageOps
    a = ImageOps.grayscale(Image.open(RENDERS / "en-a4-page-02.png"))
    b = ImageOps.grayscale(Image.open(RENDERS / "en-a4-page-09.png"))
    sheet = Image.new("L", (a.width * 2 + 10, a.height), 128)
    sheet.paste(a, (0, 0))
    sheet.paste(b, (a.width + 10, 0))
    sheet.save(RENDERS / "en-greyscale-check-pages-02-09.png")


def compress_pngs():
    """256-colour palette PNGs: text stays crisp, files shrink to roughly a third."""
    for path in RENDERS.glob("*.png"):
        image = Image.open(path)
        mode = "L" if image.mode == "L" else "RGB"
        image.convert(mode).quantize(colors=256, method=Image.Quantize.MEDIANCUT if mode == "RGB" else None,
                                     dither=Image.Dither.NONE).save(path, optimize=True)


def main():
    if RENDERS.exists():
        shutil.rmtree(RENDERS)
    RENDERS.mkdir()
    for lang in ("en", "hi-Latn"):
        html = OUT / f"mockup-{lang}.html"
        pdf = OUT / f"mockup-{lang}.pdf"
        if pdf.exists():
            pdf.unlink()
        count, fonts = print_pdf(html, pdf)
        print(lang, "pages:", count, "fonts:", ", ".join(fonts))
        pages_to_png(pdf, lang)
        phone_fit(pdf, lang, [1, 2, 4] if lang == "en" else [1, 3])
        phone_zoom(pdf, lang, 4 if lang == "en" else 3)
        phone_html(html, lang)
    greyscale_check()
    compress_pngs()


if __name__ == "__main__":
    main()
