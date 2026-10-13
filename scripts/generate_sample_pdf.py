#!/usr/bin/env python3
"""Generate the deterministic 20-page HTML report and report manifest."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable


CLARITY_PRIORITY = {"Clear": 0, "Partial": 1, "Uncertain": 2}
URL_PRIORITY = {
    "Reachable": 0,
    "Redirected": 1,
    "Unchecked": 2,
    "Blocked": 3,
    "Unreachable": 4,
}
IMAGE_FILES = {
    "DEMO-001": "01-oil-spray-bottle.jpg",
    "DEMO-002": "02-rechargeable-cordless-picture-light.jpg",
    "DEMO-003": "03-motion-activated-cat-ball.jpg",
    "DEMO-004": "04-silicone-suction-feeding-set.jpg",
    "DEMO-005": "05-manual-vegetable-chopper-and-slicer.jpg",
    "DEMO-006": "06-pleated-mushroom-table-lamp.jpg",
    "DEMO-007": "07-self-watering-plant-spike-set.jpg",
    "DEMO-008": "08-self-cleaning-pet-grooming-brush.jpg",
    "DEMO-009": "09-stroller-organiser-bag.jpg",
    "DEMO-010": "10-adjustable-baby-proof-cabinet-lock-set.jpg",
    "DEMO-011": "11-clip-on-sink-strainer-basket.jpg",
    "DEMO-012": "12-silicone-paw-cleaner-cup.jpg",
}
CATEGORY_ORDER = ["Kitchen", "Home Decor", "Pet", "Baby"]
STATUS_ORDER = ["New", "Returning", "Strengthened", "Weakened", "Coverage Changed"]
STATUS_COPY = {
    "New": "Entered this week’s shortlist after passing the current evidence rules.",
    "Returning": "Remained at the same signal strength with comparable source coverage.",
    "Strengthened": "Moved to a stronger label with comparable source coverage.",
    "Weakened": "Moved to a weaker label with comparable source coverage.",
    "Coverage Changed": "Source availability changed, so no directional movement is claimed.",
}
LABEL_EXPLANATIONS = {
    "Strong": "Three positive core signals",
    "Promising": "Two positive core signals",
    "Worth Watching": "One positive core signal",
    "Weak": "No positive core signals",
}


CSS = r"""
:root {
  --cream: #F9F3EC;
  --cream-deep: #EEE5DB;
  --maroon: #6B1F2B;
  --navy: #24384B;
  --muted-navy: #647487;
  --mustard: #C49A37;
  --sage: #93A28E;
  --rose: #C9949A;
  --charcoal: #292C2F;
  --mid: #696A6C;
  --line: #D8D0C8;
  --pale-navy: #E5EAF0;
  --pale-maroon: #EEDDE0;
  --pale-mustard: #F1E5C5;
  --pale-sage: #E1E8DE;
  --pale-rose: #F0E1E3;
  --white: #FFFFFF;
  --display: "Arial Narrow", "Aptos Narrow", "Roboto Condensed", Arial, sans-serif;
  --sans: Aptos, "Segoe UI", Arial, sans-serif;
  --serif: Georgia, "Times New Roman", serif;
}

@page { size: A4 portrait; margin: 0; }

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: #D9D4CE; color: var(--charcoal); }
body { font-family: var(--sans); font-size: 10.6pt; line-height: 1.42; }
a { color: var(--maroon); text-decoration: none; border-bottom: 0.25mm solid rgba(107,31,43,.42); }
strong { font-weight: 700; }

.page {
  position: relative;
  width: 210mm;
  height: 297mm;
  padding: 14mm 16mm 15mm 17mm;
  overflow: hidden;
  background: var(--cream);
  break-after: page;
  page-break-after: always;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
.page:last-child { break-after: auto; page-break-after: auto; }
.page-inner { height: 100%; display: flex; flex-direction: column; }
.page-top { display: flex; justify-content: flex-end; min-height: 12mm; }
.folio { width: 12mm; text-align: center; color: var(--maroon); font-family: var(--display); font-weight: 700; font-size: 13pt; line-height: 1; }
.folio::after { content: ""; display: block; width: 8mm; height: .35mm; margin: 2.3mm auto 0; background: var(--maroon); }
.page-head { margin-top: 1mm; margin-bottom: 7mm; padding-right: 18mm; }
.kicker { margin: 0 0 2mm; color: var(--maroon); font-family: var(--sans); font-size: 8.4pt; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: 0; color: var(--navy); font-family: var(--display); font-stretch: condensed; font-size: 29pt; line-height: .98; letter-spacing: -.015em; text-transform: uppercase; }
h2 { color: var(--navy); font-family: var(--display); font-size: 17pt; line-height: 1.05; text-transform: uppercase; }
h3 { margin-bottom: 2mm; color: var(--maroon); font-family: var(--sans); font-size: 8.5pt; line-height: 1.2; letter-spacing: .09em; text-transform: uppercase; }
.lede { max-width: 145mm; margin: 3mm 0 0; font-family: var(--serif); font-size: 11.2pt; line-height: 1.5; color: #3D3B39; }
.page-content { flex: 1; min-height: 0; }
.page-footer { position: absolute; left: 17mm; right: 16mm; bottom: 7mm; display: flex; justify-content: space-between; color: var(--mid); font-size: 7.4pt; letter-spacing: .02em; }
.section-rule { height: .35mm; background: var(--line); border: 0; margin: 4mm 0; }
.section-rule.maroon { width: 27mm; background: var(--maroon); margin-left: 0; }
.muted { color: var(--mid); }
.serif { font-family: var(--serif); }
.small { font-size: 8.6pt; line-height: 1.42; }
.micro { font-size: 7.5pt; line-height: 1.35; }
.nowrap { white-space: nowrap; }

/* Cover */
.cover-page { padding: 0; display: grid; grid-template-columns: 112mm 98mm; }
.cover-copy { position: relative; z-index: 2; height: 297mm; padding: 18mm 13mm 0 18mm; background: var(--cream); }
.cover-logo { width: 49mm; height: 24mm; object-fit: contain; object-position: left center; }
.cover-title { margin-top: 43mm; color: var(--navy); font-family: var(--display); font-size: 32pt; font-weight: 800; line-height: .98; letter-spacing: -.02em; text-transform: uppercase; }
.cover-subtitle { margin-top: 7mm; padding-top: 5mm; border-top: .45mm solid var(--maroon); color: var(--maroon); font-family: var(--serif); font-size: 14pt; }
.cover-disclosure { position: absolute; left: 18mm; right: 13mm; bottom: 56mm; color: var(--mid); font-size: 8.8pt; font-style: italic; line-height: 1.45; }
.cover-band { position: absolute; left: 0; right: 0; bottom: 0; height: 46mm; padding: 10mm 13mm 8mm 18mm; background: var(--navy); color: var(--cream); }
.cover-band .week { font-family: var(--display); font-size: 15pt; font-weight: 700; text-transform: uppercase; }
.cover-band .client { margin-top: 3mm; font-family: var(--serif); font-size: 10.5pt; }
.cover-visual { position: relative; height: 297mm; overflow: hidden; }
.cover-visual img { width: 100%; height: 100%; object-fit: cover; }
.cover-visual::after { content: ""; position: absolute; inset: 0; background: linear-gradient(90deg, rgba(249,243,236,.17), rgba(249,243,236,0) 28%); }
.cover-folio { position: absolute; top: 17mm; right: 11mm; z-index: 4; color: var(--maroon); font-family: var(--display); font-size: 13pt; font-weight: 700; text-align: center; }
.cover-folio::after { content: ""; display: block; width: 8mm; height: .35mm; margin: 2.3mm auto 0; background: var(--maroon); }

/* Overview */
.metric-strip { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0; border-top: .35mm solid var(--line); border-bottom: .35mm solid var(--line); }
.metric { min-height: 34mm; padding: 6mm 4mm 4mm; border-right: .3mm solid var(--line); }
.metric:last-child { border-right: 0; }
.metric-value { color: var(--maroon); font-family: var(--display); font-size: 27pt; font-weight: 700; line-height: .9; }
.metric-label { margin-top: 2.7mm; font-family: var(--serif); font-size: 8.8pt; line-height: 1.25; }
.signal-total { display: grid; grid-template-columns: 48mm 1fr; align-items: end; gap: 7mm; margin: 8mm 0 7mm; }
.signal-total .ratio { color: var(--navy); font-family: var(--display); font-size: 40pt; font-weight: 700; line-height: .82; }
.signal-total .explain { padding-bottom: 1mm; font-family: var(--serif); font-size: 11pt; line-height: 1.45; }
.category-lines { border-top: .35mm solid var(--line); }
.category-line { display: grid; grid-template-columns: 38mm 1fr 14mm; align-items: center; min-height: 16mm; border-bottom: .3mm solid var(--line); }
.category-name { color: var(--navy); font-family: var(--display); font-weight: 700; text-transform: uppercase; }
.category-track { position: relative; height: .35mm; background: var(--line); }
.category-track::after { content: ""; position: absolute; top: -1.5mm; right: 0; width: 3.4mm; height: 3.4mm; border-radius: 50%; background: var(--maroon); }
.category-count { text-align: right; color: var(--maroon); font-family: var(--display); font-size: 16pt; font-weight: 700; }

/* Reading guide */
.signal-guide { border-top: .45mm solid var(--navy); }
.guide-row { display: grid; grid-template-columns: 36mm 1fr; gap: 7mm; padding: 5mm 0; border-bottom: .3mm solid var(--line); }
.guide-label { color: var(--navy); font-family: var(--display); font-size: 14pt; font-weight: 700; text-transform: uppercase; }
.guide-copy { font-family: var(--serif); font-size: 10.7pt; line-height: 1.48; }
.concept-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 5mm 8mm; margin-top: 7mm; }
.concept { padding-top: 3mm; border-top: .35mm solid var(--maroon); }
.concept p { margin-bottom: 0; font-size: 9.4pt; line-height: 1.45; }

/* Category overview */
.category-blocks { border-top: .45mm solid var(--navy); }
.category-block { display: grid; grid-template-columns: 39mm 1fr 30mm; gap: 6mm; padding: 4.7mm 0; border-bottom: .3mm solid var(--line); }
.category-block-title { color: var(--maroon); font-family: var(--display); font-size: 15pt; font-weight: 700; text-transform: uppercase; }
.category-product-list { margin: 0; padding: 0; list-style: none; font-family: var(--serif); font-size: 9.5pt; }
.category-product-list li + li { margin-top: 1.2mm; }
.category-mix { align-self: center; text-align: right; color: var(--navy); font-size: 8.5pt; line-height: 1.35; }
.no-padding-note { margin-top: 7mm; padding: 5mm 6mm; background: var(--pale-mustard); border-left: 1mm solid var(--mustard); font-family: var(--serif); font-size: 10.5pt; line-height: 1.5; }

/* Movement */
.movement-list { border-top: .45mm solid var(--navy); }
.movement-row { display: grid; grid-template-columns: 24mm 36mm 1fr; gap: 5mm; padding: 4.2mm 0; border-bottom: .3mm solid var(--line); align-items: start; }
.movement-count { color: var(--maroon); font-family: var(--display); font-size: 24pt; font-weight: 700; line-height: .9; }
.movement-status { color: var(--navy); font-family: var(--display); font-size: 12.5pt; font-weight: 700; text-transform: uppercase; }
.movement-products { font-family: var(--serif); font-size: 9.5pt; line-height: 1.45; }
.movement-explain { display: block; margin-top: 1mm; color: var(--mid); font-family: var(--sans); font-size: 8.3pt; }
.coverage-note { margin-top: 7mm; display: grid; grid-template-columns: 29mm 1fr; gap: 7mm; padding-top: 5mm; border-top: .45mm solid var(--maroon); }
.coverage-note .mark { color: var(--mustard); font-family: var(--display); font-size: 25pt; font-weight: 700; }

/* Product pages */
.product-page { padding-top: 11mm; }
.product-page .page-top { min-height: 10mm; }
.product-hero { display: grid; grid-template-columns: 1fr 70mm; gap: 8mm; min-height: 76mm; padding-bottom: 5mm; border-bottom: .45mm solid var(--maroon); }
.product-rank { color: var(--maroon); font-family: var(--display); font-size: 30pt; font-weight: 700; line-height: .8; }
.product-name { margin: 3mm 0 2mm; color: var(--navy); font-family: var(--display); font-size: 23pt; font-weight: 800; line-height: .98; text-transform: uppercase; }
.product-meta { color: var(--mid); font-family: var(--serif); font-size: 10.2pt; }
.label-row { display: flex; align-items: center; gap: 4mm; margin-top: 5mm; }
.label-chip { display: inline-block; padding: 2.1mm 3.2mm 1.8mm; font-size: 8.2pt; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }
.label-strong { background: var(--pale-sage); color: var(--navy); }
.label-promising { background: var(--pale-maroon); color: var(--maroon); }
.label-worth-watching { background: var(--pale-mustard); color: #6C4D0D; }
.signal-count { color: var(--navy); font-family: var(--display); font-size: 19pt; font-weight: 700; }
.available-count { color: var(--mid); font-size: 8.5pt; }
.listing-band { margin-top: 4mm; color: var(--mid); font-size: 8.8pt; }
.product-image { margin: 0; }
.product-image img { display: block; width: 70mm; height: 67mm; object-fit: cover; }
.product-image figcaption { margin-top: 1.5mm; color: var(--mid); font-size: 7.2pt; text-align: right; }
.ledger-title { margin: 4.5mm 0 2mm; color: var(--maroon); font-size: 8.2pt; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
.signal-ledger { border-top: .4mm solid var(--navy); }
.signal-row { display: grid; grid-template-columns: 27mm 24mm 63mm 1fr; gap: 4mm; min-height: 15mm; padding: 2.8mm 0; border-bottom: .28mm solid var(--line); align-items: start; }
.signal-name { color: var(--navy); font-family: var(--display); font-size: 10.6pt; font-weight: 700; text-transform: uppercase; }
.outcome { font-size: 8pt; font-weight: 700; text-transform: uppercase; }
.outcome-positive { color: #3F5B44; }
.outcome-not-positive { color: #7B5A16; }
.outcome-missing { color: var(--maroon); }
.signal-evidence { font-size: 8.5pt; line-height: 1.36; }
.signal-meaning { color: #3E3D3B; font-family: var(--serif); font-size: 8.9pt; line-height: 1.38; }
.signal-links { display: block; margin-top: 1mm; font-family: var(--sans); font-size: 7.2pt; }
.insight-grid { display: grid; grid-template-columns: 1.06fr .94fr; gap: 7mm; margin-top: 5mm; }
.insight { padding-top: 3mm; border-top: .35mm solid var(--navy); }
.insight p { margin-bottom: 0; font-family: var(--serif); font-size: 9.6pt; line-height: 1.46; }
.context-lines { font-size: 8.8pt; line-height: 1.45; }
.context-lines div + div { margin-top: 1.8mm; }
.review-panel { margin-top: 4.5mm; padding: 3.5mm 4mm; border-left: 1mm solid var(--sage); background: var(--pale-sage); }
.review-panel.flagged { border-color: var(--rose); background: var(--pale-rose); }
.review-title { margin-bottom: 1mm; color: var(--navy); font-size: 8pt; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.review-panel.flagged .review-title { color: var(--maroon); }
.review-copy { margin: 0; font-family: var(--serif); font-size: 8.8pt; line-height: 1.4; }
.supplier-strip { margin-top: 4.5mm; padding-top: 3mm; border-top: .35mm solid var(--maroon); }
.supplier-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 6mm; }
.supplier-mini { min-height: 20mm; }
.supplier-name { color: var(--navy); font-size: 8.7pt; font-weight: 700; }
.supplier-meta { margin: .8mm 0; color: var(--mid); font-size: 7.6pt; }
.supplier-limit { font-family: var(--serif); font-size: 7.9pt; line-height: 1.34; }
.excel-pointer { margin-top: 2mm; color: var(--mid); font-size: 7.4pt; text-align: right; }

/* Supplier digest */
.supplier-digest-page .page-head { margin-bottom: 4mm; }
.supplier-digest-note { margin-bottom: 3.5mm; padding: 3.2mm 4mm; background: var(--pale-mustard); border-left: 1mm solid var(--mustard); font-family: var(--serif); font-size: 8.8pt; line-height: 1.38; }
.digest-groups { border-top: .45mm solid var(--navy); }
.digest-category { margin-top: 2mm; }
.digest-category-title { margin: 0; padding: 1.7mm 2.5mm 1.4mm; background: var(--navy); color: var(--cream); font-size: 7.5pt; font-weight: 700; letter-spacing: .09em; text-transform: uppercase; }
.digest-row { display: grid; grid-template-columns: 47mm 49mm 1fr; gap: 4mm; min-height: 13mm; padding: 2.1mm 2.4mm; border-bottom: .25mm solid var(--line); }
.digest-product { color: var(--navy); font-size: 7.7pt; font-weight: 700; line-height: 1.3; }
.digest-supplier { font-size: 7.5pt; line-height: 1.32; }
.digest-limit { font-family: var(--serif); font-size: 7.4pt; line-height: 1.31; }
.digest-meta { display: block; margin-top: .7mm; color: var(--mid); font-size: 6.9pt; }

/* Coverage */
.coverage-stats { display: grid; grid-template-columns: repeat(4, 1fr); border-top: .4mm solid var(--navy); border-bottom: .35mm solid var(--line); }
.coverage-stat { min-height: 35mm; padding: 5mm 3.5mm 3mm; border-right: .3mm solid var(--line); }
.coverage-stat:last-child { border-right: 0; }
.coverage-value { color: var(--maroon); font-family: var(--display); font-size: 25pt; font-weight: 700; line-height: .9; }
.coverage-label { margin-top: 2.7mm; font-family: var(--serif); font-size: 8.5pt; line-height: 1.3; }
.limitation-list { margin: 7mm 0 0; padding: 0; list-style: none; border-top: .4mm solid var(--maroon); }
.limitation-list li { display: grid; grid-template-columns: 34mm 1fr; gap: 6mm; padding: 4.2mm 0; border-bottom: .25mm solid var(--line); }
.limitation-label { color: var(--navy); font-family: var(--display); font-size: 10pt; font-weight: 700; text-transform: uppercase; }
.limitation-copy { font-family: var(--serif); font-size: 9.5pt; line-height: 1.45; }

/* Method */
.method-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 10mm; }
.method-list { margin: 0; padding: 0; list-style: none; counter-reset: method; border-top: .4mm solid var(--navy); }
.method-list li { counter-increment: method; position: relative; min-height: 25mm; padding: 4mm 0 4mm 14mm; border-bottom: .25mm solid var(--line); font-family: var(--serif); font-size: 9.5pt; line-height: 1.42; }
.method-list li::before { content: counter(method, decimal-leading-zero); position: absolute; left: 0; top: 3.2mm; color: var(--maroon); font-family: var(--display); font-size: 15pt; font-weight: 700; }
.responsibility-box { padding: 5mm; background: var(--pale-navy); border-top: 1mm solid var(--navy); }
.responsibility-box ul { margin: 3mm 0 0; padding-left: 5mm; font-size: 9.2pt; line-height: 1.48; }
.responsibility-box li + li { margin-top: 2mm; }
.final-boundary { margin-top: 6mm; padding-top: 4mm; border-top: .45mm solid var(--maroon); font-family: var(--serif); font-size: 9.5pt; line-height: 1.5; }
.contact-block { margin-top: 7mm; display: flex; align-items: center; gap: 6mm; }
.contact-block img { width: 35mm; height: 17mm; object-fit: contain; object-position: left center; }
.contact-copy { color: var(--mid); font-size: 8pt; line-height: 1.45; }

@media print {
  html, body { background: transparent; }
  .page { box-shadow: none; margin: 0; }
}
"""


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_period(start: str, end: str) -> str:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    if start_date.month == end_date.month:
        return f"{start_date.day}–{end_date.day} {end_date.strftime('%B %Y')}"
    return f"{start_date.strftime('%-d %B')}–{end_date.strftime('%-d %B %Y')}"


def quick_presence(product: dict[str, Any]) -> int:
    return sum(
        state == "Present"
        for city_values in product["quick_commerce"].values()
        for state in city_values.values()
    )


def supplier_sort_key(lead: dict[str, Any]) -> tuple[int, int, str]:
    return (
        CLARITY_PRIORITY[lead["evidence_clarity"]],
        URL_PRIORITY[lead["url_status"]],
        lead["lead_id"],
    )


def supplier_display(lead: dict[str, Any]) -> str:
    price = lead["visible_price"] or "Price not shown"
    moq = lead["visible_moq"] or "MOQ not shown"
    return f"{price} · {moq}"


def market_signal_class(label: str) -> str:
    return label.lower().replace(" ", "-")


def outcome_class(result: str) -> str:
    return result.lower().replace(" ", "-")


def page_footer(period: str) -> str:
    return (
        '<footer class="page-footer">'
        '<span>NeuraFlow · Product Opportunity Radar</span>'
        f'<span>{e(period)}</span>'
        "</footer>"
    )


def interior_page(
    number: int,
    title: str,
    kicker: str,
    content: str,
    period: str,
    *,
    lede: str | None = None,
    classes: str = "",
) -> str:
    lede_html = f'<p class="lede">{e(lede)}</p>' if lede else ""
    return f"""
    <section class="page {e(classes)}" data-page="{number}" aria-label="Page {number}: {e(title)}">
      <div class="page-inner">
        <div class="page-top"><div class="folio">{number:02d}</div></div>
        <header class="page-head">
          <p class="kicker">{e(kicker)}</p>
          <h1>{e(title)}</h1>
          {lede_html}
        </header>
        <main class="page-content">{content}</main>
      </div>
      {page_footer(period)}
    </section>
    """


def cover_page(data: dict[str, Any], paths: dict[str, str], period: str) -> str:
    report = data["report"]
    return f"""
    <section class="page cover-page" data-page="1" aria-label="Page 1: Cover">
      <div class="cover-copy">
        <img class="cover-logo" src="{e(paths['logo'])}" alt="NeuraFlow">
        <div class="cover-title">India Product<br>Opportunity<br>Radar</div>
        <div class="cover-subtitle">Weekly opportunity brief</div>
        <p class="cover-disclosure">{e(report['disclosure'])}</p>
        <div class="cover-band">
          <div class="week">Week of {e(period)}</div>
          <div class="client">Prepared for {e(report['client_name'])}<br>India · All-category preview</div>
        </div>
      </div>
      <figure class="cover-visual">
        <img src="{e(paths['cover'])}" alt="Illustrative still life of generic home, kitchen, pet and baby products">
      </figure>
      <div class="cover-folio">01</div>
    </section>
    """


def glance_page(data: dict[str, Any], period: str) -> str:
    products = data["products"]
    labels = Counter(product["market_signal"] for product in products)
    compliance = sum(
        product["compliance_check"] == "Client Compliance Check Required"
        for product in products
    )
    positive = sum(product["positive_core_signals"] for product in products)
    available = sum(product["available_core_signals"] for product in products)
    metrics = [
        (len(products), "opportunities"),
        (labels["Strong"], "Strong"),
        (labels["Promising"], "Promising"),
        (labels["Worth Watching"], "Worth Watching"),
        (compliance, "compliance flags"),
    ]
    metric_html = "".join(
        f'<div class="metric"><div class="metric-value">{value}</div><div class="metric-label">{e(label)}</div></div>'
        for value, label in metrics
    )
    categories = Counter(product["category"] for product in products)
    category_html = "".join(
        f"""
        <div class="category-line">
          <div class="category-name">{e(category)}</div>
          <div class="category-track"></div>
          <div class="category-count">{categories[category]}</div>
        </div>
        """
        for category in CATEGORY_ORDER
    )
    content = f"""
      <div class="metric-strip">{metric_html}</div>
      <div class="signal-total">
        <div class="ratio">{positive}/{available}</div>
        <div class="explain">positive outcomes across available core-signal assessments. Missing coverage remains missing rather than becoming a zero.</div>
      </div>
      <div class="category-lines">{category_html}</div>
      <p class="lede" style="margin-top:7mm;max-width:155mm">A balanced shortlist where evidence allowed. Real reports never add weak products simply to fill a category or reach a target count.</p>
    """
    return interior_page(
        2,
        "This Week at a Glance",
        "Weekly decision summary",
        content,
        period,
        lede="Twelve products made the manufactured shortlist. The figures below describe evidence, not sales or profit.",
    )


def reading_page(period: str) -> str:
    guides = [
        (
            "Meta advertising",
            "Looks for sustained, relevant activity across several advertisers. A positive result requires both activity and age thresholds; a visible ad is not enough by itself.",
        ),
        (
            "Marketplace",
            "Looks for an approved Amazon list signal or relevant cross-platform visibility with a top-ten position. Position is evidence of visibility, not a sales count.",
        ),
        (
            "Google search",
            "Compares recent and prior four-week averages. A positive result requires both the growth ratio and the absolute increase to pass.",
        ),
    ]
    guide_html = "".join(
        f'<div class="guide-row"><div class="guide-label">{e(label)}</div><div class="guide-copy">{e(copy)}</div></div>'
        for label, copy in guides
    )
    concepts = [
        ("Signal label", "Strong means 3 positive core signals; Promising 2; Worth Watching 1; Weak 0."),
        ("Coverage", "Available tells you what could be assessed. Missing evidence is not a negative result."),
        ("Weekly status", "New, Returning, Strengthened and Weakened compare only with a comparable prior report."),
        ("Coverage Changed", "Source availability changed, so no upward or downward movement is claimed."),
        ("Quick commerce", "Nine platform-city checks provide availability and competition context only."),
        ("Supplier leads", "Possible starting points for investigation; not contacted, audited, recommended or verified."),
    ]
    concept_html = "".join(
        f'<div class="concept"><h3>{e(title)}</h3><p>{e(copy)}</p></div>'
        for title, copy in concepts
    )
    content = f'<div class="signal-guide">{guide_html}</div><div class="concept-grid">{concept_html}</div>'
    return interior_page(
        3,
        "How to Read This Report",
        "Evidence before interpretation",
        content,
        period,
        lede="Three independent signal families create the market-signal label. Context, supplier leads and client compliance checks remain separate.",
    )


def category_page(data: dict[str, Any], period: str) -> str:
    products_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in data["products"]:
        products_by_category[product["category"]].append(product)
    blocks: list[str] = []
    for category in CATEGORY_ORDER:
        products = products_by_category[category]
        product_items = "".join(
            f'<li><strong>{product["rank"]:02d}</strong> · {e(product["product"])} — {e(product["market_signal"])}</li>'
            for product in products
        )
        mix = Counter(product["market_signal"] for product in products)
        mix_lines = "<br>".join(
            f"{count} {e(label)}" for label, count in mix.items()
        )
        blocks.append(
            f"""
            <section class="category-block">
              <div class="category-block-title">{e(category)}</div>
              <ul class="category-product-list">{product_items}</ul>
              <div class="category-mix">{mix_lines}</div>
            </section>
            """
        )
    content = f"""
      <div class="category-blocks">{''.join(blocks)}</div>
      <div class="no-padding-note"><strong>Balance is modest, not forced.</strong> The manufactured sample contains three products in each category so the client can inspect the layout. In a live report, a category may contain fewer—or no—products when the evidence does not support them.</div>
    """
    return interior_page(
        4,
        "Category Overview",
        "Four practical product families",
        content,
        period,
        lede="The sample demonstrates balanced coverage across Kitchen, Home Decor, Pet and Baby without changing the rank order created by evidence strength.",
    )


def movement_page(data: dict[str, Any], period: str) -> str:
    products_by_status: dict[str, list[str]] = defaultdict(list)
    for product in data["products"]:
        products_by_status[product["weekly_status"]].append(product["product"])
    rows = []
    for status in STATUS_ORDER:
        products = products_by_status[status]
        rows.append(
            f"""
            <div class="movement-row">
              <div class="movement-count">{len(products)}</div>
              <div class="movement-status">{e(status)}</div>
              <div class="movement-products">{e(' · '.join(products))}<span class="movement-explain">{e(STATUS_COPY[status])}</span></div>
            </div>
            """
        )
    content = f"""
      <div class="movement-list">{''.join(rows)}</div>
      <div class="coverage-note">
        <div class="mark">≠</div>
        <div><h3>Coverage change is not direction</h3><p class="serif">Adjustable Baby-Proof Cabinet Lock Set has two positive signals out of two available this week because search evidence is unavailable. It is labelled Coverage Changed—not Strengthened or Returning.</p></div>
      </div>
    """
    return interior_page(
        5,
        "Weekly Movement",
        "Compared with the last comparable sample week",
        content,
        period,
        lede="These statuses describe changes in report evidence. They are not sales trends, forecasts, or claims about market growth.",
    )


def signal_row(
    name: str,
    result: str,
    evidence: str,
    meaning: str,
    links: Iterable[tuple[str, str]] = (),
) -> str:
    links_html = " ".join(
        f'<a href="{e(url)}">{e(label)}</a>' for label, url in links
    )
    if links_html:
        links_html = f'<span class="signal-links">{links_html}</span>'
    return f"""
    <div class="signal-row">
      <div class="signal-name">{e(name)}</div>
      <div class="outcome outcome-{e(outcome_class(result))}">{e(result)}</div>
      <div class="signal-evidence">{e(evidence)}{links_html}</div>
      <div class="signal-meaning">{e(meaning)}</div>
    </div>
    """


def product_signal_rows(product: dict[str, Any]) -> str:
    meta = product["meta"]
    if meta["available"]:
        meta_evidence = (
            f"{meta['active_relevant_ads']} active relevant ads · "
            f"{meta['distinct_advertisers']} advertisers · oldest {meta['oldest_active_ad_days']} days"
        )
        meta_meaning = (
            "Activity and age passed the sustained-advertising rule."
            if meta["result"] == "Positive"
            else "Visible activity did not pass both required thresholds."
        )
        meta_links = [("Open sample ad evidence", meta["example_links"][0])]
    else:
        meta_evidence = "Source unavailable for this sample run."
        meta_meaning = "No zero or negative result was inferred."
        meta_links = []

    market = product["marketplace"]
    if market["available"]:
        amazon_position = market["amazon_position"] if market["amazon_position"] is not None else "not shown"
        flipkart_position = market["flipkart_position"] if market["flipkart_position"] is not None else "not shown"
        market_evidence = (
            f"Amazon {market['amazon_evidence_type']} · position {amazon_position}; "
            f"Flipkart {market['flipkart_match']} · position {flipkart_position}"
        )
        market_meaning = (
            "At least one approved marketplace visibility rule was met."
            if market["result"] == "Positive"
            else "Available marketplace evidence did not pass an approved rule."
        )
        market_links = [
            ("Amazon sample", market["amazon_link"]),
            ("Flipkart sample", market["flipkart_link"]),
        ]
    else:
        market_evidence = "Source unavailable for this sample run."
        market_meaning = "No zero or negative result was inferred."
        market_links = []

    search = product["search"]
    if search["available"]:
        search_evidence = (
            f"Recent avg {search['recent_four_week_average']} · prior {search['prior_four_week_average']} · "
            f"{search['ratio']:.2f}× · +{search['absolute_increase']}"
        )
        search_meaning = (
            "Both growth ratio and absolute increase passed."
            if search["result"] == "Positive"
            else "Available search data did not pass both thresholds."
        )
    else:
        search_evidence = "Search source unavailable for this sample run."
        search_meaning = "Missing coverage was not treated as zero or negative."

    return "".join(
        [
            signal_row("Meta advertising", meta["result"], meta_evidence, meta_meaning, meta_links),
            signal_row("Marketplace", market["result"], market_evidence, market_meaning, market_links),
            signal_row("Google search", search["result"], search_evidence, search_meaning),
        ]
    )


def product_page(
    product: dict[str, Any],
    leads: list[dict[str, Any]],
    image_path: str,
    page_number: int,
    period: str,
) -> str:
    selected_leads = sorted(leads, key=supplier_sort_key)[:2]
    supplier_cards = []
    for lead in selected_leads:
        supplier_cards.append(
            f"""
            <div class="supplier-mini">
              <div class="supplier-name">{e(lead['supplier'])}</div>
              <div class="supplier-meta">{e(lead['city_state'])} · {e(supplier_display(lead))}</div>
              <div class="supplier-limit">{e(lead['lead_limitation'])} <a href="{e(lead['supplier_page'])}">Open sample supplier page</a></div>
            </div>
            """
        )
    flagged = product["compliance_check"] == "Client Compliance Check Required"
    review_title = product["compliance_check"]
    review_class = "review-panel flagged" if flagged else "review-panel"
    q_count = quick_presence(product)
    content = f"""
      <div class="product-hero">
        <div>
          <div class="product-rank">{product['rank']:02d}</div>
          <h1 class="product-name">{e(product['product'])}</h1>
          <div class="product-meta">{e(product['category'])} · {e(product['weekly_status'])}</div>
          <div class="label-row">
            <span class="label-chip label-{e(market_signal_class(product['market_signal']))}">{e(product['market_signal'])}</span>
            <span class="signal-count">{product['positive_core_signals']}/{product['available_core_signals']}</span>
            <span class="available-count">positive / available core signals</span>
          </div>
          <div class="listing-band"><strong>Illustrative retail listing band:</strong> {e(product['retail_listing_band'])}</div>
        </div>
        <figure class="product-image">
          <img src="{e(image_path)}" alt="Illustrative product visual of {e(product['product'])}">
          <figcaption>Illustrative product visual</figcaption>
        </figure>
      </div>
      <div class="ledger-title">Three-signal evidence ledger</div>
      <div class="signal-ledger">{product_signal_rows(product)}</div>
      <div class="insight-grid">
        <section class="insight">
          <h3>Why it surfaced</h3>
          <p>{e(product['why_it_surfaced'])}</p>
        </section>
        <section class="insight">
          <h3>Context</h3>
          <div class="context-lines">
            <div><strong>Quick commerce:</strong> present in {q_count} of 9 sampled platform-city checks; context only.</div>
            <div><strong>Coverage:</strong> {e(product['coverage_note'])}</div>
          </div>
          <div class="{review_class}">
            <div class="review-title">{e(review_title)}</div>
            <p class="review-copy">{e(product['main_warning'])}</p>
          </div>
        </section>
      </div>
      <section class="supplier-strip">
        <h3>Possible supplier leads · investigation only</h3>
        <div class="supplier-grid">{''.join(supplier_cards)}</div>
        <div class="excel-pointer">See Excel → Evidence row {product['rank'] + 6} and Supplier Leads for all three manufactured records.</div>
      </section>
    """
    return f"""
    <section class="page product-page" data-page="{page_number}" data-product-id="{e(product['product_id'])}" aria-label="Page {page_number}: {e(product['product'])}">
      <div class="page-inner">
        <div class="page-top"><div class="folio">{page_number:02d}</div></div>
        <main class="page-content">{content}</main>
      </div>
      {page_footer(period)}
    </section>
    """


def supplier_digest_page(
    data: dict[str, Any],
    leads_by_product: dict[str, list[dict[str, Any]]],
    period: str,
) -> tuple[str, list[str]]:
    products_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in data["products"]:
        products_by_category[product["category"]].append(product)
    representative_ids: list[str] = []
    groups = []
    for category in CATEGORY_ORDER:
        rows = []
        for product in products_by_category[category]:
            lead = sorted(leads_by_product[product["product_id"]], key=supplier_sort_key)[0]
            representative_ids.append(lead["lead_id"])
            rows.append(
                f"""
                <div class="digest-row">
                  <div class="digest-product">{product['rank']:02d} · {e(product['product'])}</div>
                  <div class="digest-supplier"><a href="{e(lead['supplier_page'])}">{e(lead['supplier'])}</a><span class="digest-meta">{e(lead['city_state'])} · {e(supplier_display(lead))}</span></div>
                  <div class="digest-limit">{e(lead['lead_limitation'])}</div>
                </div>
                """
            )
        groups.append(
            f'<section class="digest-category"><h2 class="digest-category-title">{e(category)}</h2>{"".join(rows)}</section>'
        )
    content = f"""
      <div class="supplier-digest-note"><strong>Supplier leads are starting points for client investigation.</strong> They have not been contacted, audited, or verified. Visible price is not landed cost. The Excel workbook contains all 36 manufactured records.</div>
      <div class="digest-groups">{''.join(groups)}</div>
    """
    return (
        interior_page(
            18,
            "Supplier Lead Digest",
            "One representative manufactured record per product",
            content,
            period,
            classes="supplier-digest-page",
        ),
        representative_ids,
    )


def coverage_page(data: dict[str, Any], period: str) -> str:
    products = data["products"]
    meta_available = sum(product["meta"]["available"] for product in products)
    market_available = sum(product["marketplace"]["available"] for product in products)
    search_available = sum(product["search"]["available"] for product in products)
    quick_total = sum(quick_presence(product) for product in products)
    stats = [
        (f"{meta_available}/12", "Meta assessments available"),
        (f"{market_available}/12", "Marketplace assessments available"),
        (f"{search_available}/12", "Search assessments available"),
        (f"{quick_total}/108", "Quick-commerce presences sampled"),
    ]
    stats_html = "".join(
        f'<div class="coverage-stat"><div class="coverage-value">{e(value)}</div><div class="coverage-label">{e(label)}</div></div>'
        for value, label in stats
    )
    limitations = [
        (
            "Search gap",
            "Rank 10 has no search assessment this week. Its two available signals are positive, but the missing family prevents a directional week-over-week claim.",
        ),
        (
            "Quick commerce",
            "The 108 checks sample Blinkit, Zepto and Swiggy Instamart across Mumbai, Delhi and Bengaluru. They describe availability and competition context only.",
        ),
        (
            "Supplier evidence",
            "Supplier pages may be stale, redirected, blocked, incomplete or commercially unsuitable. Leads require direct client investigation before use.",
        ),
        (
            "No forecast",
            "The report does not estimate demand, sales, margin, conversion, return rate, landed cost, or profitability. Market signals identify products worth human investigation.",
        ),
        (
            "Missing ≠ zero",
            "Unavailable evidence remains Missing. It is never converted into a negative result, a zero, or evidence that a product is weak.",
        ),
    ]
    list_html = "".join(
        f'<li><div class="limitation-label">{e(label)}</div><div class="limitation-copy">{e(copy)}</div></li>'
        for label, copy in limitations
    )
    content = f'<div class="coverage-stats">{stats_html}</div><ul class="limitation-list">{list_html}</ul>'
    return interior_page(
        19,
        "Coverage and Limitations",
        "What this report can and cannot say",
        content,
        period,
        lede="Partial coverage is shown plainly. A useful weekly report can continue with honest gaps, but it cannot turn those gaps into confidence.",
    )


def method_page(paths: dict[str, str], period: str) -> str:
    steps = [
        "Discover and normalise candidate product concepts across the approved India categories.",
        "Check Meta advertising, marketplace visibility and Google search as three separate core signal families.",
        "Apply deterministic thresholds, preserve missing coverage, and rank eligible opportunities without weak filler.",
        "Add quick-commerce context, lightweight client-review flags, and possible supplier leads after selection.",
        "Review the checked data and regenerate both the PDF and Excel from the same source before delivery.",
    ]
    responsibilities = [
        "decide which products, if any, deserve further testing;",
        "verify product compliance, warnings, age guidance and claims;",
        "contact and assess supplier identity, stock, terms and suitability;",
        "sample product quality, packaging, delivery and returns;",
        "calculate landed cost, pricing, margin and commercial risk independently.",
    ]
    step_html = "".join(f"<li>{e(step)}</li>" for step in steps)
    responsibility_html = "".join(f"<li>{e(item)}</li>" for item in responsibilities)
    content = f"""
      <div class="method-layout">
        <section>
          <h3>Five-step method</h3>
          <ol class="method-list">{step_html}</ol>
        </section>
        <section>
          <div class="responsibility-box">
            <h3>Client responsibility</h3>
            <p class="serif small">The client—not NeuraFlow—must:</p>
            <ul>{responsibility_html}</ul>
          </div>
          <div class="final-boundary"><strong>Supplier boundary.</strong> Possible supplier leads have not been contacted, audited, recommended or verified. They are starting points for independent investigation.</div>
          <div class="final-boundary"><strong>Report-use boundary.</strong> This briefing is decision support. It is not a guarantee of demand, sales, compliance, supplier suitability, product performance or profitability.</div>
          <div class="contact-block">
            <img src="{e(paths['logo'])}" alt="NeuraFlow">
            <div class="contact-copy"><strong>Prepared by NeuraFlow</strong><br>Contact details to be inserted before client presentation.<br>Detailed evidence remains in the companion Excel workbook.</div>
          </div>
        </section>
      </div>
    """
    return interior_page(
        20,
        "Method and Client Responsibility",
        "Operator-reviewed decision support",
        content,
        period,
        lede="The service reduces research effort and makes evidence inspectable. Final commercial, supplier, safety and compliance decisions remain with the client.",
    )


def relative_url(from_dir: Path, to_path: Path) -> str:
    return Path(os.path.relpath(to_path.resolve(), from_dir.resolve())).as_posix()


def generate(
    data_path: Path,
    image_manifest_path: Path,
    output_dir: Path,
    logo_path: Path,
) -> tuple[Path, Path]:
    data = json.loads(data_path.read_text(encoding="utf-8"))
    image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)

    assets_by_id = {asset["asset_id"]: asset for asset in image_manifest["assets"]}
    image_report_dir = image_manifest_path.parent / "report"
    paths = {
        "logo": relative_url(output_dir, logo_path),
        "cover": relative_url(output_dir, image_report_dir / assets_by_id["COVER"]["report_file"]),
    }
    for product_id, filename in IMAGE_FILES.items():
        asset = assets_by_id[product_id]
        if asset["report_file"] != filename:
            raise ValueError(f"Image manifest mismatch for {product_id}")
        paths[product_id] = relative_url(output_dir, image_report_dir / filename)

    products = sorted(data["products"], key=lambda product: product["rank"])
    leads_by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lead in data["supplier_leads"]:
        leads_by_product[lead["product_id"]].append(lead)

    period = format_period(data["report"]["period_start"], data["report"]["period_end"])
    pages: list[str] = [
        cover_page(data, paths, period),
        glance_page(data, period),
        reading_page(period),
        category_page(data, period),
        movement_page(data, period),
    ]
    manifest_pages: list[dict[str, Any]] = [
        {"page": 1, "type": "cover", "title": "India Product Opportunity Radar"},
        {"page": 2, "type": "overview", "title": "This Week at a Glance"},
        {"page": 3, "type": "guide", "title": "How to Read This Report"},
        {"page": 4, "type": "category", "title": "Category Overview"},
        {"page": 5, "type": "movement", "title": "Weekly Movement"},
    ]

    for product in products:
        page_number = product["rank"] + 5
        selected_leads = sorted(
            leads_by_product[product["product_id"]], key=supplier_sort_key
        )[:2]
        pages.append(
            product_page(
                product,
                leads_by_product[product["product_id"]],
                paths[product["product_id"]],
                page_number,
                period,
            )
        )
        manifest_pages.append(
            {
                "page": page_number,
                "type": "opportunity",
                "title": product["product"],
                "product_id": product["product_id"],
                "rank": product["rank"],
                "market_signal": product["market_signal"],
                "weekly_status": product["weekly_status"],
                "positive_core_signals": product["positive_core_signals"],
                "available_core_signals": product["available_core_signals"],
                "quick_commerce_presence": quick_presence(product),
                "supplier_lead_ids": [lead["lead_id"] for lead in selected_leads],
                "image_file": IMAGE_FILES[product["product_id"]],
            }
        )

    digest_html, representative_ids = supplier_digest_page(data, leads_by_product, period)
    pages.extend([digest_html, coverage_page(data, period), method_page(paths, period)])
    manifest_pages.extend(
        [
            {
                "page": 18,
                "type": "supplier_digest",
                "title": "Supplier Lead Digest",
                "representative_lead_ids": representative_ids,
            },
            {"page": 19, "type": "coverage", "title": "Coverage and Limitations"},
            {"page": 20, "type": "method", "title": "Method and Client Responsibility"},
        ]
    )

    html_path = output_dir / "report.html"
    css_path = output_dir / "report.css"
    manifest_path = output_dir / "report-manifest.json"
    css_path.write_text(CSS.strip() + "\n", encoding="utf-8")
    html_document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light only">
  <title>NeuraFlow Product Opportunity Brief · Illustrative</title>
  <style>
{CSS.strip()}
  </style>
</head>
<body>
{''.join(pages)}
</body>
</html>
"""
    html_path.write_text(html_document, encoding="utf-8")

    label_counts = Counter(product["market_signal"] for product in products)
    status_counts = Counter(product["weekly_status"] for product in products)
    manifest = {
        "schema_version": "1.0",
        "report_id": data["report"]["report_id"],
        "report_period": period,
        "renderer": "Google Chrome headless print-to-PDF",
        "page_size": "A4 portrait",
        "page_count": len(manifest_pages),
        "disclosure": data["report"]["disclosure"],
        "disclosure_page": 1,
        "data_file": str(data_path),
        "data_sha256": sha256(data_path),
        "image_manifest_file": str(image_manifest_path),
        "image_manifest_sha256": sha256(image_manifest_path),
        "summary": {
            "opportunities": len(products),
            "label_counts": dict(label_counts),
            "status_counts": dict(status_counts),
            "positive_signal_outcomes": sum(product["positive_core_signals"] for product in products),
            "available_signal_assessments": sum(product["available_core_signals"] for product in products),
            "compliance_flags": sum(
                product["compliance_check"] == "Client Compliance Check Required"
                for product in products
            ),
            "quick_commerce_presence": sum(quick_presence(product) for product in products),
            "quick_commerce_checks": len(products) * 9,
            "supplier_leads_in_excel": len(data["supplier_leads"]),
        },
        "pages": manifest_pages,
        "html_file": str(html_path),
        "css_file": str(css_path),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(html_path)
    print(css_path)
    print(manifest_path)
    return html_path, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("preview/sample-data/client_preview_dataset.json"),
    )
    parser.add_argument(
        "--image-manifest",
        type=Path,
        default=Path("media/client-preview/product-visuals/manifest.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("preview/report"),
    )
    parser.add_argument(
        "--logo",
        type=Path,
        default=Path("media/NeuraFlow/long_maroon_path_transparent_1024.png"),
    )
    args = parser.parse_args()
    generate(args.data, args.image_manifest, args.output_dir, args.logo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
