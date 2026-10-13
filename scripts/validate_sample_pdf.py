#!/usr/bin/env python3
"""Validate the P5 report source, manifest, PDF, and Excel/data reconciliation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import fitz
from pypdf import PdfReader

from validate_preview_dataset import validate_dataset, validate_workbook


CLARITY_PRIORITY = {"Clear": 0, "Partial": 1, "Uncertain": 2}
URL_PRIORITY = {
    "Reachable": 0,
    "Redirected": 1,
    "Unchecked": 2,
    "Blocked": 3,
    "Unreachable": 4,
}
CATEGORY_ORDER = ["Kitchen", "Home Decor", "Pet", "Baby"]
EXPECTED_PAGE_TITLES = [
    "INDIA PRODUCT OPPORTUNITY RADAR",
    "THIS WEEK AT A GLANCE",
    "HOW TO READ THIS REPORT",
    "CATEGORY OVERVIEW",
    "WEEKLY MOVEMENT",
    "OIL SPRAY BOTTLE",
    "RECHARGEABLE CORDLESS PICTURE LIGHT",
    "MOTION-ACTIVATED CAT BALL",
    "SILICONE SUCTION FEEDING SET",
    "MANUAL VEGETABLE CHOPPER AND SLICER",
    "PLEATED MUSHROOM TABLE LAMP",
    "SELF-WATERING PLANT SPIKE SET",
    "SELF-CLEANING PET GROOMING BRUSH",
    "STROLLER ORGANISER BAG",
    "ADJUSTABLE BABY-PROOF CABINET LOCK SET",
    "CLIP-ON SINK STRAINER BASKET",
    "SILICONE PAW CLEANER CUP",
    "SUPPLIER LEAD DIGEST",
    "COVERAGE AND LIMITATIONS",
    "METHOD AND CLIENT RESPONSIBILITY",
]


class Validation:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.checks = 0

    def require(self, condition: bool, message: str) -> None:
        self.checks += 1
        if not condition:
            self.errors.append(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalise(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = re.sub(r"-\s+", "-", text)
    return " ".join(text.split()).casefold()


def quick_presence(product: dict) -> int:
    return sum(
        state == "Present"
        for city_values in product["quick_commerce"].values()
        for state in city_values.values()
    )


def supplier_key(lead: dict) -> tuple[int, int, str]:
    return (
        CLARITY_PRIORITY[lead["evidence_clarity"]],
        URL_PRIORITY[lead["url_status"]],
        lead["lead_id"],
    )


def supplier_display(lead: dict) -> str:
    price = lead["visible_price"] or "Price not shown"
    moq = lead["visible_moq"] or "MOQ not shown"
    return f"{price} · {moq}"


def contains(validation: Validation, page_text: str, value: object, message: str) -> None:
    expected = normalise(str(value))
    validation.require(expected in page_text, f"{message}: {expected!r} not found.")


def validate_manifest(
    manifest_path: Path,
    image_manifest_path: Path,
    data_path: Path,
    data: dict,
    validation: Validation,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
    products = sorted(data["products"], key=lambda product: product["rank"])
    leads_by_product: dict[str, list[dict]] = defaultdict(list)
    for lead in data["supplier_leads"]:
        leads_by_product[lead["product_id"]].append(lead)

    validation.require(manifest["page_count"] == 20, "Manifest must contain 20 pages.")
    validation.require(len(manifest["pages"]) == 20, "Manifest page-record count must be 20.")
    validation.require(
        [page["page"] for page in manifest["pages"]] == list(range(1, 21)),
        "Manifest page numbers must be 1–20 in order.",
    )
    validation.require(manifest["page_size"] == "A4 portrait", "Manifest page size changed.")
    validation.require(
        manifest["renderer"] == "Google Chrome headless print-to-PDF",
        "Unexpected PDF renderer in manifest.",
    )
    validation.require(manifest["data_sha256"] == sha256(data_path), "Manifest data hash is stale.")
    validation.require(
        manifest["image_manifest_sha256"] == sha256(image_manifest_path),
        "Manifest image hash is stale.",
    )
    validation.require(
        manifest["disclosure"] == data["report"]["disclosure"]
        and manifest["disclosure_page"] == 1,
        "Manifest disclosure contract changed.",
    )

    summary = manifest["summary"]
    label_counts = Counter(product["market_signal"] for product in products)
    status_counts = Counter(product["weekly_status"] for product in products)
    validation.require(summary["opportunities"] == 12, "Manifest opportunity total is not 12.")
    validation.require(summary["label_counts"] == dict(label_counts), "Manifest label totals differ from source.")
    validation.require(summary["status_counts"] == dict(status_counts), "Manifest movement totals differ from source.")
    validation.require(summary["positive_signal_outcomes"] == 26, "Manifest positive total is not 26.")
    validation.require(summary["available_signal_assessments"] == 35, "Manifest available total is not 35.")
    validation.require(summary["compliance_flags"] == 5, "Manifest compliance total is not 5.")
    validation.require(summary["quick_commerce_presence"] == 50, "Manifest quick-commerce total is not 50.")
    validation.require(summary["quick_commerce_checks"] == 108, "Manifest quick-commerce checks are not 108.")
    validation.require(summary["supplier_leads_in_excel"] == 36, "Manifest supplier total is not 36.")

    product_pages = [page for page in manifest["pages"] if page["type"] == "opportunity"]
    validation.require(len(product_pages) == 12, "Manifest must contain 12 opportunity pages.")
    for page, product in zip(product_pages, products, strict=True):
        validation.require(page["page"] == product["rank"] + 5, f"{product['product_id']}: wrong PDF page.")
        validation.require(page["product_id"] == product["product_id"], f"{product['product_id']}: manifest ID mismatch.")
        validation.require(page["rank"] == product["rank"], f"{product['product_id']}: manifest rank mismatch.")
        validation.require(page["title"] == product["product"], f"{product['product_id']}: manifest title mismatch.")
        validation.require(page["market_signal"] == product["market_signal"], f"{product['product_id']}: label mismatch.")
        validation.require(page["weekly_status"] == product["weekly_status"], f"{product['product_id']}: status mismatch.")
        validation.require(
            page["positive_core_signals"] == product["positive_core_signals"],
            f"{product['product_id']}: positive count mismatch.",
        )
        validation.require(
            page["available_core_signals"] == product["available_core_signals"],
            f"{product['product_id']}: available count mismatch.",
        )
        validation.require(
            page["quick_commerce_presence"] == quick_presence(product),
            f"{product['product_id']}: quick-commerce count mismatch.",
        )
        expected_leads = [
            lead["lead_id"]
            for lead in sorted(leads_by_product[product["product_id"]], key=supplier_key)[:2]
        ]
        validation.require(
            page["supplier_lead_ids"] == expected_leads,
            f"{product['product_id']}: displayed supplier priority differs.",
        )

    digest = manifest["pages"][17]
    expected_representatives = []
    for category in CATEGORY_ORDER:
        for product in [item for item in products if item["category"] == category]:
            expected_representatives.append(
                sorted(leads_by_product[product["product_id"]], key=supplier_key)[0]["lead_id"]
            )
    validation.require(
        digest["representative_lead_ids"] == expected_representatives,
        "Supplier digest priority or category order differs.",
    )

    validation.require(image_manifest["asset_count"] == 13, "Image manifest must contain 13 assets.")
    validation.require(image_manifest["product_asset_count"] == 12, "Image manifest must contain 12 product assets.")
    image_ids = [asset["asset_id"] for asset in image_manifest["assets"]]
    validation.require(
        image_ids == [product["product_id"] for product in products] + ["COVER"],
        "Image manifest order or IDs differ.",
    )
    report_dir = image_manifest_path.parent / "report"
    for asset in image_manifest["assets"]:
        report_path = report_dir / asset["report_file"]
        validation.require(report_path.exists(), f"Missing report image {report_path}.")
        if report_path.exists():
            validation.require(
                sha256(report_path) == asset["report_sha256"],
                f"Report image hash differs for {asset['asset_id']}.",
            )
            validation.require(
                asset["report_dimensions"] == [1000, 1250],
                f"Unexpected report image dimensions for {asset['asset_id']}.",
            )
    return manifest


def validate_html(html_path: Path, data: dict, validation: Validation) -> None:
    document = html_path.read_text(encoding="utf-8")
    validation.require(document.lower().count("<section class=\"page") == 20, "HTML must contain 20 page sections.")
    validation.require(
        document.count(data["report"]["disclosure"]) == 1,
        "HTML must contain the full disclosure exactly once.",
    )
    validation.require(
        document.count("<figcaption>Illustrative product visual</figcaption>") == 12,
        "HTML must contain 12 product-image captions.",
    )
    validation.require(document.lower().count("<script") == 0, "HTML must not contain JavaScript.")
    validation.require("file:///" not in document, "HTML contains an absolute file URL.")
    validation.require("/mnt/" not in document and "/home/" not in document, "HTML exposes a local filesystem path.")
    urls = re.findall(r'href="([^"]+)"', document)
    external_urls = [url for url in urls if url.startswith(("http://", "https://"))]
    validation.require(external_urls, "HTML contains no external evidence links.")
    validation.require(
        all(urlparse(url).hostname == "example.com" for url in external_urls),
        "HTML contains an external link outside example.com.",
    )
    image_sources = re.findall(r'<img[^>]+src="([^"]+)"', document)
    validation.require(len(image_sources) == 15, f"Expected 15 image placements, found {len(image_sources)}.")
    validation.require(
        all(not source.startswith(("http://", "https://", "file:")) for source in image_sources),
        "HTML contains a remote or absolute image source.",
    )


def validate_pdf(
    pdf_path: Path,
    data: dict,
    manifest: dict,
    validation: Validation,
) -> None:
    reader = PdfReader(pdf_path)
    validation.require(not reader.is_encrypted, "PDF must not be encrypted.")
    validation.require(len(reader.pages) == 20, f"Expected 20 PDF pages, found {len(reader.pages)}.")
    root = reader.trailer["/Root"]
    validation.require("/StructTreeRoot" in root, "PDF is not tagged with a structure tree.")
    validation.require(bool(root.get("/MarkInfo", {}).get("/Marked")), "PDF marked-content flag is missing.")
    validation.require(root.get("/AcroForm") is None, "PDF contains an interactive form.")
    names = root.get("/Names")
    validation.require(names is None, "PDF contains a Names dictionary; scripts or embedded files may be present.")
    validation.require(
        reader.metadata.get("/Title") == "NeuraFlow Product Opportunity Brief · Illustrative",
        "PDF title metadata differs from the approved report title.",
    )

    document = fitz.open(pdf_path)
    validation.require(document.page_count == 20, "PyMuPDF page count differs from pypdf.")
    page_texts: list[str] = []
    all_links: list[dict] = []
    image_instances = 0
    for index, page in enumerate(document):
        page_number = index + 1
        validation.require(
            abs(page.rect.width - 595.0) < 1.0 and abs(page.rect.height - 841.9) < 1.0,
            f"Page {page_number} is not A4 portrait: {page.rect}.",
        )
        validation.require(page.rotation == 0, f"Page {page_number} has unexpected rotation.")
        page_text = normalise(page.get_text("text"))
        page_texts.append(page_text)
        validation.require(bool(page_text), f"Page {page_number} contains no extractable text.")
        contains(validation, page_text, EXPECTED_PAGE_TITLES[index], f"Page {page_number} title")
        links = page.get_links()
        all_links.extend(links)
        image_count = len(page.get_images(full=True))
        image_instances += image_count
        if 6 <= page_number <= 17:
            validation.require(image_count == 1, f"Page {page_number} must contain one product image.")
            validation.require(len(links) >= 5, f"Page {page_number} has too few evidence/supplier links.")
        if page_number == 1:
            validation.require(image_count == 2, "Cover must contain logo and cover visual.")
        if page_number == 18:
            validation.require(len(links) >= 12, "Supplier digest must contain 12 supplier links.")
        if page_number == 20:
            validation.require(image_count == 1, "Method page must contain the NeuraFlow logo.")
    validation.require(image_instances == 15, f"Expected 15 image placements, found {image_instances}.")

    disclosure = normalise(data["report"]["disclosure"])
    disclosure_pages = [index + 1 for index, text in enumerate(page_texts) if disclosure in text]
    validation.require(disclosure_pages == [1], f"Disclosure must appear only on page 1, found {disclosure_pages}.")
    caption_count = sum(text.count("illustrative product visual") for text in page_texts)
    validation.require(caption_count == 12, f"Expected 12 product-image captions, found {caption_count}.")
    validation.require(
        all("context only" in page_texts[page_number - 1].lower() for page_number in range(6, 18)),
        "Every product page must contain the quick-commerce context-only boundary.",
    )

    products = sorted(data["products"], key=lambda product: product["rank"])
    leads_by_product: dict[str, list[dict]] = defaultdict(list)
    for lead in data["supplier_leads"]:
        leads_by_product[lead["product_id"]].append(lead)
    for product in products:
        page_number = product["rank"] + 5
        text = page_texts[page_number - 1]
        contains(validation, text, product["product"], f"Page {page_number} product")
        contains(validation, text, product["category"], f"Page {page_number} category")
        contains(validation, text, product["market_signal"], f"Page {page_number} label")
        contains(validation, text, product["weekly_status"], f"Page {page_number} weekly status")
        contains(
            validation,
            text,
            f"{product['positive_core_signals']}/{product['available_core_signals']}",
            f"Page {page_number} signal count",
        )
        contains(validation, text, product["retail_listing_band"], f"Page {page_number} listing band")
        contains(validation, text, product["why_it_surfaced"], f"Page {page_number} surfaced explanation")
        contains(validation, text, product["main_warning"], f"Page {page_number} warning")
        contains(validation, text, product["coverage_note"], f"Page {page_number} coverage note")
        contains(
            validation,
            text,
            f"present in {quick_presence(product)} of 9 sampled platform-city checks; context only.",
            f"Page {page_number} quick-commerce line",
        )
        for signal in (product["meta"], product["marketplace"], product["search"]):
            contains(validation, text, signal["result"], f"Page {page_number} signal outcome")

        meta = product["meta"]
        contains(
            validation,
            text,
            f"{meta['active_relevant_ads']} active relevant ads · {meta['distinct_advertisers']} advertisers · oldest {meta['oldest_active_ad_days']} days",
            f"Page {page_number} Meta evidence",
        )
        market = product["marketplace"]
        contains(validation, text, market["amazon_evidence_type"], f"Page {page_number} Amazon evidence")
        contains(validation, text, market["flipkart_match"], f"Page {page_number} Flipkart evidence")
        search = product["search"]
        if search["available"]:
            contains(
                validation,
                text,
                f"Recent avg {search['recent_four_week_average']} · prior {search['prior_four_week_average']} · {search['ratio']:.2f}× · +{search['absolute_increase']}",
                f"Page {page_number} search evidence",
            )
        else:
            contains(
                validation,
                text,
                "Search source unavailable for this sample run.",
                f"Page {page_number} missing search evidence",
            )

        selected_leads = sorted(leads_by_product[product["product_id"]], key=supplier_key)[:2]
        for lead in selected_leads:
            contains(validation, text, lead["supplier"], f"Page {page_number} supplier")
            contains(validation, text, lead["city_state"], f"Page {page_number} supplier location")
            contains(validation, text, supplier_display(lead), f"Page {page_number} supplier commercial fields")
            contains(validation, text, lead["lead_limitation"], f"Page {page_number} supplier limitation")

    overview = page_texts[1]
    for token in ["12 opportunities", "4 Strong", "6 Promising", "2 Worth Watching", "5 compliance flags", "26/35"]:
        contains(validation, overview, token, "Overview total")
    movement = page_texts[4]
    status_counts = Counter(product["weekly_status"] for product in products)
    for status, count in status_counts.items():
        contains(validation, movement, f"{count} {status}", f"Movement total for {status}")

    digest_text = page_texts[17]
    for category in CATEGORY_ORDER:
        for product in [item for item in products if item["category"] == category]:
            lead = sorted(leads_by_product[product["product_id"]], key=supplier_key)[0]
            contains(validation, digest_text, product["product"], "Supplier digest product")
            contains(validation, digest_text, lead["supplier"], "Supplier digest supplier")
            contains(validation, digest_text, lead["city_state"], "Supplier digest location")
            contains(validation, digest_text, supplier_display(lead), "Supplier digest commercial fields")
            contains(validation, digest_text, lead["lead_limitation"], "Supplier digest limitation")
    contains(
        validation,
        digest_text,
        "Supplier leads are starting points for client investigation. They have not been contacted, audited, or verified.",
        "Supplier digest boundary",
    )

    coverage_text = page_texts[18]
    for token in ["12/12 Meta", "12/12 Marketplace", "11/12 Search", "50/108 Quick-commerce"]:
        contains(validation, coverage_text, token, "Coverage total")
    method_text = page_texts[19]
    contains(
        validation,
        method_text,
        "This briefing is decision support. It is not a guarantee of demand, sales, compliance, supplier suitability, product performance or profitability.",
        "Method report-use boundary",
    )

    uri_links = [link["uri"] for link in all_links if link.get("uri")]
    validation.require(uri_links, "PDF contains no external evidence links.")
    validation.require(
        all(urlparse(uri).hostname == "example.com" for uri in uri_links),
        "PDF contains an external URI outside example.com.",
    )
    validation.require(
        not any(link.get("file") for link in all_links),
        "PDF contains a local file link.",
    )
    validation.require(
        not any("/mnt/" in text or "/home/" in text or "file:///" in text for text in page_texts),
        "PDF exposes a local filesystem path.",
    )
    document.close()

    validation.require(pdf_path.stat().st_size < 10 * 1024 * 1024, "PDF exceeds the 10 MB target.")
    validation.require(pdf_path.stat().st_size > 500 * 1024, "PDF is implausibly small for the approved visual report.")
    validation.require(manifest["page_count"] == len(reader.pages), "Manifest and PDF page count differ.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("preview/sample-data/client_preview_dataset.json"),
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        default=Path(
            "outputs/client-preview/"
            "NeuraFlow_Product_Opportunity_Evidence_Illustrative_2026-08-15.xlsx"
        ),
    )
    parser.add_argument(
        "--image-manifest",
        type=Path,
        default=Path("media/client-preview/product-visuals/manifest.json"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("preview/report/report-manifest.json"),
    )
    parser.add_argument(
        "--html",
        type=Path,
        default=Path("preview/report/report.html"),
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=Path(
            "outputs/client-preview/"
            "NeuraFlow_Product_Opportunity_Brief_Illustrative_2026-08-15.pdf"
        ),
    )
    args = parser.parse_args()

    base_validation, data = validate_dataset(args.data)
    validate_workbook(args.workbook, data, base_validation)
    validation = Validation()
    manifest = validate_manifest(
        args.manifest,
        args.image_manifest,
        args.data,
        data,
        validation,
    )
    validate_html(args.html, data, validation)
    validate_pdf(args.pdf, data, manifest, validation)
    errors = base_validation.errors + validation.errors
    total_checks = base_validation.checks + validation.checks
    if errors:
        print(f"FAILED: {len(errors)} issue(s) across {total_checks} checks")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"PASS: dataset, workbook, image assets, report source and PDF passed {total_checks} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
