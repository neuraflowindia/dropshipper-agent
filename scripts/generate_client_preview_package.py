#!/usr/bin/env python3
"""Generate the deterministic P6 service overview and client demo-kit documents."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
from typing import Any


APPROVED_COMMERCIAL_FIELDS = {
    "price_inr": 1500,
    "price_display": "₹1,500",
    "price_period": "per month",
    "tax_wording": "Plus legally applicable taxes, if any.",
    "billing": "Paid monthly in advance.",
    "delivery_count": 4,
    "delivery_cadence": "Four weekly PDF-and-Excel reports each month.",
    "normal_delivery_day": "Monday",
    "delivery_channel": "Email delivery with a WhatsApp notification and download link.",
    "setup_fee": "Free setup.",
    "initial_price_period": "Price fixed for the first two months.",
}

BROCHURE_FILENAME = (
    "NeuraFlow_Product_Opportunity_Radar_Service_Overview_2026-08-15.pdf"
)
OFFER_FILENAME = "NeuraFlow_Monthly_Service_Offer_2026-08-15.pdf"
WALKTHROUGH_FILENAME = "NeuraFlow_Client_Demo_Walkthrough_2026-08-15.md"
GUIDE_FILENAME = "NeuraFlow_Client_Preview_Package_Guide_2026-08-15.md"
SUPPORT_DOCS_DIR = Path("docs/client-preview/guides")
REPORT_FILENAME = "NeuraFlow_Product_Opportunity_Brief_Illustrative_2026-08-15.pdf"
WORKBOOK_FILENAME = "NeuraFlow_Product_Opportunity_Evidence_Illustrative_2026-08-15.xlsx"


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
  --pale-mustard: #F1E5C5;
  --pale-sage: #E1E8DE;
  --pale-rose: #F0E1E3;
  --display: "Arial Narrow", "Aptos Narrow", "Roboto Condensed", Arial, sans-serif;
  --sans: Aptos, "Segoe UI", Arial, sans-serif;
  --serif: Georgia, "Times New Roman", serif;
}

@page { size: A4 portrait; margin: 0; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: #D9D4CE; color: var(--charcoal); }
body { font-family: var(--sans); font-size: 10.5pt; line-height: 1.42; }
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
.folio {
  width: 12mm;
  color: var(--maroon);
  font-family: var(--display);
  font-size: 13pt;
  font-weight: 700;
  line-height: 1;
  text-align: center;
}
.folio::after {
  content: "";
  display: block;
  width: 8mm;
  height: .35mm;
  margin: 2.3mm auto 0;
  background: var(--maroon);
}
.page-head { margin: 1mm 18mm 7mm 0; }
.kicker {
  margin: 0 0 2mm;
  color: var(--maroon);
  font-size: 8.1pt;
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
}
h1, h2, h3, p { margin-top: 0; }
h1 {
  margin-bottom: 0;
  color: var(--navy);
  font-family: var(--display);
  font-size: 29pt;
  line-height: .98;
  letter-spacing: -.015em;
  text-transform: uppercase;
}
h2 {
  margin: 0;
  color: var(--navy);
  font-family: var(--display);
  font-size: 18pt;
  line-height: 1.02;
  text-transform: uppercase;
}
h3 {
  margin: 0 0 2mm;
  color: var(--maroon);
  font-size: 8.2pt;
  line-height: 1.2;
  letter-spacing: .09em;
  text-transform: uppercase;
}
.lede {
  max-width: 150mm;
  margin: 3mm 0 0;
  color: #3D3B39;
  font-family: var(--serif);
  font-size: 11.1pt;
  line-height: 1.48;
}
.page-content { flex: 1; min-height: 0; }
.page-footer {
  position: absolute;
  left: 17mm;
  right: 16mm;
  bottom: 7mm;
  display: flex;
  justify-content: space-between;
  color: var(--mid);
  font-size: 7.3pt;
}
.label {
  color: var(--maroon);
  font-size: 7.6pt;
  font-weight: 700;
  letter-spacing: .09em;
  text-transform: uppercase;
}

/* Cover */
.cover-page { padding: 0; display: grid; grid-template-columns: 116mm 94mm; }
.cover-copy { position: relative; height: 297mm; padding: 18mm 14mm 18mm 18mm; background: var(--cream); }
.cover-logo { width: 49mm; height: 24mm; object-fit: contain; object-position: left center; }
.cover-kicker {
  margin: 50mm 0 0;
  color: var(--maroon);
  font-size: 8.3pt;
  font-weight: 700;
  letter-spacing: .13em;
  text-transform: uppercase;
}
.cover-title {
  margin-top: 4mm;
  color: var(--navy);
  font-family: var(--display);
  font-size: 35pt;
  font-weight: 800;
  line-height: .96;
  letter-spacing: -.02em;
  text-transform: uppercase;
}
.cover-subtitle {
  max-width: 76mm;
  margin-top: 8mm;
  padding-top: 5mm;
  border-top: .45mm solid var(--maroon);
  color: var(--maroon);
  font-family: var(--serif);
  font-size: 14.2pt;
  line-height: 1.42;
}
.cover-note {
  position: absolute;
  left: 18mm;
  right: 14mm;
  bottom: 35mm;
  color: var(--mid);
  font-family: var(--serif);
  font-size: 9.5pt;
  line-height: 1.52;
}
.cover-visual { position: relative; height: 297mm; overflow: hidden; }
.cover-visual img { width: 100%; height: 100%; object-fit: cover; }
.cover-visual::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, rgba(249,243,236,.16), rgba(249,243,236,0) 31%);
}
.cover-folio {
  position: absolute;
  top: 17mm;
  right: 11mm;
  z-index: 4;
  color: var(--maroon);
  font-family: var(--display);
  font-size: 13pt;
  font-weight: 700;
  text-align: center;
}
.cover-folio::after {
  content: "";
  display: block;
  width: 8mm;
  height: .35mm;
  margin: 2.3mm auto 0;
  background: var(--maroon);
}

/* Weekly outputs */
.output-grid { display: grid; grid-template-columns: .82fr 1.18fr; gap: 9mm; }
.output-block { min-width: 0; }
.output-block h2 { margin: 2mm 0 2mm; font-size: 17pt; }
.output-block p { margin-bottom: 4mm; font-family: var(--serif); font-size: 9.5pt; line-height: 1.46; }
.preview-frame {
  position: relative;
  height: 124mm;
  overflow: hidden;
  background: var(--cream-deep);
  box-shadow: 0 1.6mm 4mm rgba(36,56,75,.10);
}
.report-preview { width: 100%; height: 100%; object-fit: contain; padding: 4mm; background: #E6E0D9; }
.excel-preview { width: 210%; height: 112%; object-fit: cover; object-position: left top; transform: translateY(-12%); }
.preview-label {
  position: absolute;
  left: 4mm;
  bottom: 4mm;
  padding: 1.5mm 2.4mm;
  background: rgba(249,243,236,.95);
  color: var(--maroon);
  font-size: 7.1pt;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
}
.delivery-line {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  margin-top: 9mm;
  padding-top: 5mm;
  border-top: .45mm solid var(--navy);
}
.delivery-fact { min-height: 25mm; padding-right: 4mm; }
.delivery-fact strong {
  display: block;
  color: var(--maroon);
  font-family: var(--display);
  font-size: 16.5pt;
  line-height: 1;
  text-transform: uppercase;
}
.delivery-fact span { display: block; margin-top: 2mm; font-family: var(--serif); font-size: 8.3pt; line-height: 1.3; }

/* Selection method */
.signal-list { display: grid; gap: 3mm; }
.signal-band {
  display: grid;
  grid-template-columns: 18mm 49mm 1fr;
  gap: 6mm;
  align-items: center;
  min-height: 34mm;
  padding: 6mm 7mm;
  background: var(--cream-deep);
}
.signal-band:nth-child(2) { background: var(--pale-sage); }
.signal-band:nth-child(3) { background: var(--pale-navy); }
.signal-number { color: var(--maroon); font-family: var(--display); font-size: 21pt; font-weight: 700; line-height: .9; }
.signal-name { color: var(--navy); font-family: var(--display); font-size: 13.5pt; font-weight: 700; line-height: 1; text-transform: uppercase; }
.signal-copy { margin: 0; font-family: var(--serif); font-size: 9.4pt; line-height: 1.43; }
.agreement {
  margin-top: 7mm;
  padding: 5mm 6mm;
  background: var(--maroon);
  color: var(--cream);
  font-family: var(--serif);
  font-size: 12pt;
  line-height: 1.4;
  text-align: center;
}
.context-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 5mm; margin-top: 8mm; }
.context-item { padding-top: 3mm; border-top: .55mm solid var(--sage); }
.context-item:nth-child(2) { border-color: var(--muted-navy); }
.context-item:nth-child(3) { border-color: var(--mustard); }
.context-item:nth-child(4) { border-color: var(--rose); }
.context-item strong { display: block; color: var(--navy); font-size: 8pt; text-transform: uppercase; }
.context-item span { display: block; margin-top: 1.5mm; font-family: var(--serif); font-size: 8.2pt; line-height: 1.36; }
.honesty-note { margin: 8mm 0 0; color: var(--mid); font-family: var(--serif); font-size: 9.3pt; line-height: 1.45; text-align: center; }

/* Sample output */
.sample-metrics { display: grid; grid-template-columns: repeat(4, 1fr); padding: 4mm 0 5mm; border-top: .45mm solid var(--navy); }
.sample-metric strong { display: block; color: var(--maroon); font-family: var(--display); font-size: 20pt; line-height: 1; }
.sample-metric span { display: block; margin-top: 2mm; font-family: var(--serif); font-size: 8pt; line-height: 1.3; }
.sample-visuals { display: grid; grid-template-columns: 60mm 1fr; gap: 9mm; margin-top: 4mm; }
.report-shot { height: 137mm; overflow: hidden; background: #E6E0D9; box-shadow: 0 1.5mm 4mm rgba(36,56,75,.09); }
.report-shot img { width: 100%; height: 100%; object-fit: contain; padding: 2.5mm; }
.evidence-column { min-width: 0; }
.evidence-shot { height: 95mm; overflow: hidden; background: var(--cream-deep); box-shadow: 0 1.5mm 4mm rgba(36,56,75,.09); }
.evidence-shot img { width: 205%; height: 100%; object-fit: cover; object-position: left top; }
.proof-points { display: grid; grid-template-columns: 1fr 1fr; gap: 5mm 7mm; margin-top: 6mm; }
.proof-point { padding-top: 2.5mm; border-top: .4mm solid var(--maroon); }
.proof-point strong { display: block; color: var(--navy); font-size: 8.2pt; }
.disclosure {
  margin-top: 7mm;
  padding: 3mm 4mm;
  background: var(--pale-mustard);
  border-left: 1mm solid var(--mustard);
  color: #4E432D;
  font-family: var(--serif);
  font-size: 8.4pt;
  font-style: italic;
}

/* Monthly service */
.service-hero { display: grid; grid-template-columns: 78mm 1fr; gap: 11mm; align-items: end; padding: 2mm 0 7mm; }
.price { color: var(--maroon); font-family: var(--display); font-size: 39pt; font-weight: 800; line-height: .85; }
.price-period { margin-top: 2mm; color: var(--mid); font-family: var(--serif); font-size: 9.1pt; }
.service-intro { margin: 0; font-family: var(--serif); font-size: 11pt; line-height: 1.48; }
.term-strip { display: grid; grid-template-columns: repeat(4, 1fr); padding: 5mm 0 4mm; border-top: .45mm solid var(--navy); }
.term strong { display: block; color: var(--navy); font-size: 8pt; text-transform: uppercase; }
.term span { display: block; margin-top: 1.6mm; font-family: var(--serif); font-size: 7.8pt; line-height: 1.32; }
.scope-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10mm; margin-top: 6mm; }
.scope-list { margin: 0; padding: 0; list-style: none; }
.scope-list li { position: relative; padding: 2mm 0 2mm 5mm; font-family: var(--serif); font-size: 8.3pt; line-height: 1.32; }
.scope-list li::before { content: ""; position: absolute; left: 0; top: 3.9mm; width: 2mm; height: 2mm; border-radius: 50%; background: var(--sage); }
.scope-list.excluded li::before { border-radius: 0; background: var(--rose); transform: rotate(45deg); }
.service-details { display: grid; grid-template-columns: 1fr 1fr; gap: 9mm; margin-top: 6mm; }
.service-details > div { padding-top: 3mm; border-top: .4mm solid var(--maroon); }
.service-details p { margin: 1mm 0 0; font-family: var(--serif); font-size: 8.4pt; line-height: 1.4; }
.boundary {
  margin-top: 6mm;
  padding: 3.5mm 4.5mm;
  background: var(--pale-navy);
  border-left: 1mm solid var(--muted-navy);
  font-family: var(--serif);
  font-size: 8.3pt;
  line-height: 1.42;
}
.cta {
  position: absolute;
  left: 17mm;
  right: 16mm;
  bottom: 16mm;
  display: grid;
  grid-template-columns: 1fr 45mm;
  gap: 8mm;
  align-items: end;
  padding-top: 5mm;
  border-top: .5mm solid var(--maroon);
}
.cta strong { display: block; color: var(--navy); font-family: var(--display); font-size: 16pt; text-transform: uppercase; }
.cta span { display: block; margin-top: 2mm; font-family: var(--serif); font-size: 8.8pt; line-height: 1.4; }
.cta img { width: 43mm; height: 20mm; object-fit: contain; object-position: right center; }

/* Standalone offer */
.offer-page { padding: 15mm 16mm 14mm 17mm; }
.offer-brand { display: flex; align-items: center; justify-content: space-between; }
.offer-brand img { width: 43mm; height: 20mm; object-fit: contain; object-position: left center; }
.offer-brand .date { color: var(--mid); font-size: 7.6pt; text-transform: uppercase; }
.summary-price {
  display: grid;
  grid-template-columns: 72mm 1fr;
  gap: 10mm;
  margin-top: 11mm;
  padding: 7mm 0;
  border-top: .45mm solid var(--maroon);
}
.summary-price .price { font-size: 42pt; }
.summary-price h1 { font-size: 24pt; }
.summary-price .lede { font-size: 10.4pt; }
.offer-page .term-strip { margin-top: 4mm; }
.offer-page .scope-grid { margin-top: 7mm; gap: 9mm; }
.offer-page .scope-list li { font-size: 7.8pt; padding-top: 1.8mm; padding-bottom: 1.8mm; }
.offer-page .service-details { margin-top: 5mm; }
.responsibility-note {
  margin-top: 5mm;
  padding: 3mm 4mm;
  background: var(--pale-navy);
  border-left: 1mm solid var(--muted-navy);
  font-family: var(--serif);
  font-size: 7.9pt;
  line-height: 1.38;
}
.summary-bottom {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  min-height: 32mm;
  padding: 7mm 17mm 6mm;
  background: var(--navy);
  color: var(--cream);
}
.summary-bottom strong { display: block; font-family: var(--display); font-size: 14.5pt; text-transform: uppercase; }
.summary-bottom span { display: block; margin-top: 2mm; max-width: 150mm; font-family: var(--serif); font-size: 8.5pt; line-height: 1.4; }
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_config(config: dict[str, Any]) -> None:
    required = {
        "provider_brand",
        "service_name",
        "service_descriptor",
        "price_inr",
        "price_display",
        "price_period",
        "tax_wording",
        "billing",
        "delivery_count",
        "delivery_cadence",
        "normal_delivery_day",
        "delivery_channel",
        "setup_fee",
        "initial_price_period",
        "opportunity_limit",
        "implementation_target",
        "call_to_action",
        "included",
        "excluded",
        "client_responsibilities",
    }
    missing = sorted(key for key in required if key not in config or config[key] in (None, "", []))
    if missing:
        raise ValueError(f"Commercial configuration is incomplete: {', '.join(missing)}")
    for key, value in APPROVED_COMMERCIAL_FIELDS.items():
        if config.get(key) != value:
            raise ValueError(f"Commercial field {key!r} differs from the owner-approved value.")
    if len(config["included"]) != 6 or len(config["excluded"]) != 6:
        raise ValueError("The fixed included/excluded scope counts changed.")
    if len(config["client_responsibilities"]) != 5:
        raise ValueError("The fixed client-responsibility count changed.")


def page_shell(page_number: int, kicker: str, title: str, lede: str, body: str) -> str:
    return f"""
<section class="page" aria-label="Page {page_number}: {esc(title)}">
  <div class="page-inner">
    <div class="page-top"><div class="folio">{page_number:02d}</div></div>
    <header class="page-head">
      <p class="kicker">{esc(kicker)}</p>
      <h1>{esc(title)}</h1>
      <p class="lede">{esc(lede)}</p>
    </header>
    <main class="page-content">{body}</main>
  </div>
  <footer class="page-footer"><span>NeuraFlow · Product Opportunity Radar</span><span>Weekly research service</span></footer>
</section>
"""


def build_brochure(config: dict[str, Any], data: dict[str, Any]) -> str:
    products = data["products"]
    disclosure = data["report"]["disclosure"]
    category_count = len({product["category"] for product in products})
    flags = sum(
        product["compliance_check"] == "Client Compliance Check Required"
        for product in products
    )
    if (len(products), category_count, flags) != (12, 4, 5):
        raise ValueError("Manufactured sample summary changed; brochure copy requires review.")

    logo = "../../media/NeuraFlow/long_maroon_path_transparent_1024.png"
    cover = "../../media/client-preview/product-visuals/report/cover-product-research-still-life.jpg"
    report_overview = "../../media/client-preview/pdf-review/pages/page-02.png"
    shortlist = "../../media/client-preview/excel-review/01-shortlist-full.png"
    product_page = "../../media/client-preview/pdf-review/pages/page-06.png"
    evidence = "../../media/client-preview/excel-review/03-evidence-market-search.png"

    cover_page = f"""
<section class="page cover-page" aria-label="Page 1: Product Opportunity Radar">
  <div class="cover-copy">
    <img class="cover-logo" src="{logo}" alt="NeuraFlow">
    <p class="cover-kicker">Weekly research service</p>
    <div class="cover-title">Product<br>Opportunity<br>Radar</div>
    <div class="cover-subtitle">Weekly product research for clearer test decisions.</div>
    <p class="cover-note">See the strongest product opportunities, their supporting signals and the questions to check before testing.</p>
  </div>
  <div class="cover-visual"><img src="{cover}" alt="Editorial still life of representative product concepts"></div>
  <div class="cover-folio">01</div>
</section>
"""

    output_body = f"""
<div class="output-grid">
  <article class="output-block">
    <p class="label">Mobile PDF</p><h2>Decision brief</h2>
    <p>Scan the shortlist, reasons, weekly changes and review points comfortably on a phone.</p>
    <div class="preview-frame"><img class="report-preview" src="{report_overview}" alt="Sample portrait report weekly overview"><span class="preview-label">Quick decision view</span></div>
  </article>
  <article class="output-block">
    <p class="label">Excel workbook</p><h2>Evidence view</h2>
    <p>Check the research behind each product, including sources, gaps and possible supplier leads.</p>
    <div class="preview-frame"><img class="excel-preview" src="{shortlist}" alt="Sample Excel shortlist sheet"><span class="preview-label">Detailed evidence view</span></div>
  </article>
</div>
<div class="delivery-line">
  <div class="delivery-fact"><strong>4 reports</strong><span>each month</span></div>
  <div class="delivery-fact"><strong>{esc(config['normal_delivery_day'])}</strong><span>normal delivery day</span></div>
  <div class="delivery-fact"><strong>Reviewed</strong><span>before delivery</span></div>
  <div class="delivery-fact"><strong>PDF + Excel</strong><span>two useful formats</span></div>
</div>
"""
    page2 = page_shell(
        2,
        "Weekly delivery",
        "What you receive every week",
        "A quick mobile briefing for decisions and a detailed workbook for evidence.",
        output_body,
    )

    method_body = """
<div class="signal-list">
  <div class="signal-band"><div class="signal-number">01</div><div class="signal-name">Advertising activity</div><p class="signal-copy">Looks for continued product advertising across different advertisers.</p></div>
  <div class="signal-band"><div class="signal-number">02</div><div class="signal-name">Marketplace visibility</div><p class="signal-copy">Checks whether a product appears meaningfully across relevant shopping platforms.</p></div>
  <div class="signal-band"><div class="signal-number">03</div><div class="signal-name">Search interest</div><p class="signal-copy">Compares recent India search interest with the previous four weeks.</p></div>
</div>
<div class="agreement">When more of these views agree, the opportunity is stronger.</div>
<div class="context-row">
  <div class="context-item"><strong>Weekly change</strong><span>Shows what is new, returning, stronger or weaker.</span></div>
  <div class="context-item"><strong>Quick commerce</strong><span>Adds availability and competition context.</span></div>
  <div class="context-item"><strong>Possible suppliers</strong><span>Lists research starting points when evidence is available.</span></div>
  <div class="context-item"><strong>Review flags</strong><span>Highlights questions the client should check.</span></div>
</div>
<p class="honesty-note">Weak products are never added simply to fill the list. Missing evidence stays visible.</p>
"""
    page3 = page_shell(
        3,
        "Selection method",
        "How products are shortlisted",
        "Three market views help separate stronger opportunities from products that need more proof.",
        method_body,
    )

    proof_body = f"""
<div class="sample-metrics">
  <div class="sample-metric"><strong>{len(products)}</strong><span>sample opportunities</span></div>
  <div class="sample-metric"><strong>{category_count}</strong><span>product categories</span></div>
  <div class="sample-metric"><strong>{flags}</strong><span>review flags</span></div>
  <div class="sample-metric"><strong>2</strong><span>useful formats</span></div>
</div>
<div class="sample-visuals">
  <div class="report-shot"><img src="{product_page}" alt="Sample opportunity page for an oil spray bottle"></div>
  <div class="evidence-column">
    <div class="evidence-shot"><img src="{evidence}" alt="Sample marketplace and Google search evidence"></div>
    <div class="proof-points">
      <div class="proof-point"><strong>Weekly change</strong></div>
      <div class="proof-point"><strong>Visible gaps</strong></div>
      <div class="proof-point"><strong>Supplier starting points</strong></div>
      <div class="proof-point"><strong>Client review reminders</strong></div>
    </div>
  </div>
</div>
<div class="disclosure">{esc(disclosure)}</div>
"""
    page4 = page_shell(
        4,
        "Sample output",
        "See the output in action",
        "The same shortlist appears as a clear briefing and a traceable workbook.",
        proof_body,
    )

    included_items = "".join(f"<li>{esc(item)}</li>" for item in config["included"][1:4])
    excluded_items = "".join(f"<li>{esc(item)}</li>" for item in config["excluded"][:2])
    offer_body = f"""
<div class="service-hero">
  <div><div class="price">{esc(config['price_display'])}</div><div class="price-period">{esc(config['price_period'])}</div></div>
  <p class="service-intro">A monthly service with clear outputs and limits.</p>
</div>
<div class="term-strip">
  <div class="term"><strong>Payment</strong><span>{esc(config['billing'])}</span></div>
  <div class="term"><strong>Delivery</strong><span>Four weekly reports.</span></div>
  <div class="term"><strong>Setup</strong><span>{esc(config['setup_fee'])}</span></div>
  <div class="term"><strong>Price</strong><span>{esc(config['initial_price_period'])}</span></div>
</div>
<div class="scope-grid">
  <div><h3>Included</h3><ul class="scope-list">{included_items}</ul></div>
  <div><h3>Service limits</h3><ul class="scope-list excluded">{excluded_items}</ul></div>
</div>
<div class="service-details">
  <div><span class="label">Delivery</span><p>{esc(config['normal_delivery_day'])}. {esc(config['delivery_channel'])}</p></div>
  <div><span class="label">Price details</span><p>{esc(config['tax_wording'])} {esc(config['opportunity_limit'])}</p></div>
</div>
<div class="boundary"><strong>Clear responsibility.</strong> Supplier leads are starting points, not verified suppliers. You decide what to test and remain responsible for compliance, supplier checks, product samples, costs, pricing and margins.</div>
<div class="cta"><div><strong>Ready to begin?</strong><span>{esc(config['call_to_action'])}</span></div><img src="{logo}" alt="NeuraFlow"></div>
"""
    page5 = page_shell(
        5,
        "Monthly service",
        "Simple price. Clear scope.",
        config["delivery_cadence"],
        offer_body,
    )

    return f"""<!doctype html>
<html lang="en-IN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NeuraFlow Product Opportunity Radar · Service Overview</title>
  <link rel="stylesheet" href="brochure.css">
</head>
<body>{cover_page}{page2}{page3}{page4}{page5}</body>
</html>
"""


def build_offer(config: dict[str, Any]) -> str:
    logo = "../../media/NeuraFlow/long_maroon_path_transparent_1024.png"
    included = "".join(f"<li>{esc(item)}</li>" for item in config["included"][:4])
    excluded = "".join(f"<li>{esc(item)}</li>" for item in config["excluded"][:3])
    return f"""<!doctype html>
<html lang="en-IN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NeuraFlow Monthly Service Offer</title>
  <link rel="stylesheet" href="brochure.css">
</head>
<body>
<section class="page offer-page" aria-label="Monthly service offer">
  <header class="offer-brand"><img src="{logo}" alt="NeuraFlow"><span class="date">Service offer · August 2026</span></header>
  <div class="summary-price">
    <div><div class="price">{esc(config['price_display'])}</div><div class="price-period">{esc(config['price_period'])}</div></div>
    <div><p class="kicker">Product Opportunity Radar</p><h1>Weekly research, clearly packaged</h1><p class="lede">A month-to-month research service for clearer product test decisions.</p></div>
  </div>
  <div class="term-strip">
    <div class="term"><strong>Payment</strong><span>{esc(config['billing'])}</span></div>
    <div class="term"><strong>Delivery</strong><span>{esc(config['delivery_cadence'])}</span></div>
    <div class="term"><strong>Normal day</strong><span>{esc(config['normal_delivery_day'])}</span></div>
    <div class="term"><strong>Setup</strong><span>{esc(config['setup_fee'])}</span></div>
  </div>
  <div class="scope-grid">
    <div><h3>Included</h3><ul class="scope-list">{included}</ul></div>
    <div><h3>Service limits</h3><ul class="scope-list excluded">{excluded}</ul></div>
  </div>
  <div class="service-details">
    <div><span class="label">Price details</span><p>{esc(config['tax_wording'])} {esc(config['initial_price_period'])}</p></div>
    <div><span class="label">Delivery</span><p>{esc(config['delivery_channel'])}</p></div>
  </div>
  <p class="responsibility-note"><strong>{esc(config['opportunity_limit'])}</strong> Supplier leads are starting points, not verified suppliers. You decide what to test and remain responsible for compliance, supplier checks, product samples, costs, pricing and margins.</p>
  <div class="summary-bottom"><strong>Choose a start date</strong><span>{esc(config['call_to_action'])}</span></div>
</section>
</body>
</html>
"""


def build_walkthrough(config: dict[str, Any]) -> str:
    return f"""# NeuraFlow Client Demo Walkthrough

## Purpose

Show the service outputs, explain their value in plain language, and learn whether the prospect wants to begin the monthly service. This is an internal guide; do not send it to the client.

## What to open

1. `{BROCHURE_FILENAME}`
2. `{REPORT_FILENAME}`
3. `{WORKBOOK_FILENAME}`
4. `{OFFER_FILENAME}`

Keep source data, scripts, manifests, review images, credentials, provider costs and internal limits closed.

## Suggested ten-minute conversation

### 0:00–2:00 — Explain the service

> Product research is scattered across advertising, marketplaces, search behaviour, quick-commerce checks and supplier pages. Product Opportunity Radar brings those views into one weekly shortlist. It supports investigation; it does not promise a winner.

### 2:00–4:00 — Show the two outputs

> Every week you receive a portrait PDF for quick decisions and an Excel workbook for checking the evidence. Advertising activity, marketplace visibility and Google search interest help us decide which products deserve attention.

Open the brochure’s output page, then the report overview. Do not read every line.

### 4:00–7:00 — Trace one product

Use Oil Spray Bottle as the example:

1. In the PDF, show the label, weekly change and reasons for selection.
2. In the workbook, open `Shortlist`, then `Evidence`, then `Supplier Leads`.
3. Point out that missing information remains visible and supplier leads still need checking.

Say:

> The PDF and Excel come from the same checked source. The PDF is the quick view; Excel is the evidence behind it.

### 7:00–8:30 — Explain the offer

Say:

> The service is {config['price_display']} {config['price_period']}. It includes four weekly PDF-and-Excel reports each month, normally delivered on {config['normal_delivery_day']} by email with a WhatsApp notification and download link. Payment is monthly in advance, setup is free, and the price is fixed for the first two months.

Then add:

> Each report can contain up to 12 eligible opportunities. We use a shorter list when the evidence is weaker.

### 8:30–10:00 — Ask for a clear response

Ask:

1. Would this PDF-and-Excel service improve the way you research products today?
2. Is Monday useful for your normal product-review routine?
3. Is the scope clear, including what is not included?
4. If it is useful, which date would you like the service to begin?

Do not start application development because the prospect likes the presentation. First obtain written acceptance of the scope and price, preferably with the first month’s payment or another clear paid commitment.

## Plain answers to likely questions

### “Are these guaranteed winning products?”

No. The service shows products supported by visible market signals. It cannot see another seller’s sales, returns, margin or profit.

### “Are the suppliers verified?”

No. They are possible Indian supplier leads and research starting points. The client must contact and assess them.

### “Will I receive exactly 12 products every week?”

No. Each report can contain up to 12 eligible opportunities. A shorter, stronger list is better than weak filler.

### “Does this include a dashboard or login?”

No. The service includes weekly PDF-and-Excel reports. A client portal or application is not included.

### “Can I request unlimited product searches?”

No. The price covers four scheduled reports each month. Custom or on-demand research is outside this scope.

### “Who handles compliance and margins?”

The client does. The report provides review reminders but does not make legal, safety, certification, cost, pricing, margin or profit decisions.

### “Why are there both a PDF and Excel?”

The PDF is easy to read on a phone. Excel preserves the detailed evidence and supplier starting points.

### “How much does it cost you to operate?”

> NeuraFlow manages its research tools and operating costs internally. The client price is based on the agreed reports and service scope.

Do not disclose provider allowances, internal cost limits, time limits or credentials.

## After the conversation

Send only the brochure, sample portrait report, sample Excel workbook and monthly service offer. State that the sample outputs are illustrative.

If the client wants to proceed, capture:

- written acceptance of {config['price_display']} {config['price_period']} and {config['tax_wording'].lower()}
- preferred start date;
- confirmation of Monday delivery and the email and WhatsApp destination;
- final client-facing branding;
- any access needed for the agreed research sources;
- first-month payment or another clear paid commitment.
"""


def build_guide() -> str:
    return f"""# NeuraFlow Client Preview Package Guide

## Recommended presentation order

1. `{BROCHURE_FILENAME}` — explain the service, weekly outputs, selection method and price.
2. `{REPORT_FILENAME}` — show the mobile-friendly weekly decision briefing.
3. `{WORKBOOK_FILENAME}` — trace one opportunity through Shortlist, Evidence and Supplier Leads.
4. `{OFFER_FILENAME}` — close with the exact monthly scope, price, delivery and next step.

## Client-shareable files

- `{BROCHURE_FILENAME}`
- `{REPORT_FILENAME}`
- `{WORKBOOK_FILENAME}`
- `{OFFER_FILENAME}`

The report, workbook and brochure sample-output page state that the preview uses sample data.

## Internal-only file

- `{WALKTHROUGH_FILENAME}`

Do not send source data, HTML or CSS, scripts, production plans, manifests, review images, validation logs, credentials, provider pricing, internal cost limits or time assumptions.

## Application boundary

Do not begin application development because the prospect liked the presentation. Proceed only after written acceptance of the scope and price, preferably with first-month payment or another clear paid commitment. Final branding, the start date and any required access must also be ready.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("preview/brochure/commercial-config.json"))
    parser.add_argument("--data", type=Path, default=Path("preview/sample-data/client_preview_dataset.json"))
    parser.add_argument("--preview-dir", type=Path, default=Path("preview/brochure"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/client-preview"))
    parser.add_argument("--docs-dir", type=Path, default=SUPPORT_DOCS_DIR)
    args = parser.parse_args()

    config = read_json(args.config)
    data = read_json(args.data)
    validate_config(config)
    args.preview_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.docs_dir.mkdir(parents=True, exist_ok=True)

    brochure_html = args.preview_dir / "brochure.html"
    brochure_css = args.preview_dir / "brochure.css"
    offer_html = args.preview_dir / "offer-summary.html"
    manifest_path = args.preview_dir / "client-preview-package-manifest.json"
    walkthrough_path = args.docs_dir / WALKTHROUGH_FILENAME
    guide_path = args.docs_dir / GUIDE_FILENAME

    brochure_html.write_text(build_brochure(config, data), encoding="utf-8")
    brochure_css.write_text(CSS.strip() + "\n", encoding="utf-8")
    offer_html.write_text(build_offer(config), encoding="utf-8")
    walkthrough_path.write_text(build_walkthrough(config), encoding="utf-8")
    guide_path.write_text(build_guide(), encoding="utf-8")

    source_paths = {
        "commercial_config": args.config,
        "sample_dataset": args.data,
        "logo": Path("media/NeuraFlow/long_maroon_path_transparent_1024.png"),
        "p5_report": args.output_dir / REPORT_FILENAME,
        "p4_workbook": args.output_dir / WORKBOOK_FILENAME,
        "p5_page_02": Path("media/client-preview/pdf-review/pages/page-02.png"),
        "p5_page_06": Path("media/client-preview/pdf-review/pages/page-06.png"),
        "excel_shortlist_review": Path("media/client-preview/excel-review/01-shortlist-full.png"),
        "excel_evidence_review": Path("media/client-preview/excel-review/03-evidence-market-search.png"),
        "cover_visual": Path("media/client-preview/product-visuals/report/cover-product-research-still-life.jpg"),
    }
    for label, path in source_paths.items():
        if not path.exists():
            raise FileNotFoundError(f"Required P6 source {label} is missing: {path}")

    manifest = {
        "schema_version": "1.1",
        "package_id": "NEURAFLOW-PREVIEW-2026-08-15",
        "status": "sources-generated-render-pending",
        "page_contract": {"brochure": 5, "offer_summary": 1},
        "renderer": "Google Chrome headless print-to-PDF",
        "commercial_config_sha256": sha256(args.config),
        "sample_data_sha256": sha256(args.data),
        "disclosure": data["report"]["disclosure"],
        "disclosure_artifact": BROCHURE_FILENAME,
        "disclosure_page": 4,
        "source_artifacts": {
            label: {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size}
            for label, path in source_paths.items()
        },
        "generated_sources": {
            str(path): {"sha256": sha256(path), "bytes": path.stat().st_size}
            for path in (brochure_html, brochure_css, offer_html, walkthrough_path, guide_path)
        },
        "artifacts": [],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for path in (brochure_html, brochure_css, offer_html, walkthrough_path, guide_path, manifest_path):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
