#!/usr/bin/env python3
"""Validate the revised P6 service overview, offer, demo kit, and manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import fitz
from pypdf import PdfReader

from generate_client_preview_package import (
    APPROVED_COMMERCIAL_FIELDS,
    BROCHURE_FILENAME,
    GUIDE_FILENAME,
    OFFER_FILENAME,
    REPORT_FILENAME,
    SUPPORT_DOCS_DIR,
    WALKTHROUGH_FILENAME,
    WORKBOOK_FILENAME,
    validate_config,
)


BROCHURE_TITLES = [
    "PRODUCT OPPORTUNITY RADAR",
    "WHAT YOU RECEIVE EVERY WEEK",
    "HOW PRODUCTS ARE SHORTLISTED",
    "SEE THE OUTPUT IN ACTION",
    "SIMPLE PRICE. CLEAR SCOPE.",
]
BROCHURE_PAGE_WORD_LIMITS = [32, 90, 135, 95, 168]
BROCHURE_TOTAL_WORD_LIMIT = 520
OFFER_WORD_LIMIT = 190

CLIENT_FORBIDDEN_PHRASES = [
    "founding",
    "pilot",
    "billing cycle",
    "monthly cycle",
    "commercial gate",
    "commercial acceptance",
    "operator-reviewed",
    "core signal",
    "evidence family",
    "provider benchmark",
    "deterministic",
    "initial service checks",
    "guaranteed winning products",
    "fully automated",
    "compliance approved",
    "real-time intelligence",
    "guaranteed 12 products",
    "unlimited research included",
    "suppliers are verified",
    "verified and recommended suppliers",
]
INTERNAL_FORBIDDEN_TERMS = [
    "Apify",
    "QuickCommerceAPI",
    "Gemini",
    "₹750",
    "₹1,000",
    "₹600",
    "provider allowance",
    "operator hours",
]
REQUIRED_WALKTHROUGH_QUESTIONS = [
    "Are these guaranteed winning products?",
    "Are the suppliers verified?",
    "Will I receive exactly 12 products every week?",
    "Does this include a dashboard or login?",
    "Can I request unlimited product searches?",
    "Who handles compliance and margins?",
    "Why are there both a PDF and Excel?",
    "How much does it cost you to operate?",
]
REQUIRED_CLIENT_FACTS = [
    "Four weekly PDF-and-Excel reports each month.",
    "Paid monthly in advance.",
    "Free setup.",
    "Price fixed for the first two months.",
    "Up to 12 eligible opportunities in each weekly report.",
    "Supplier leads are starting points, not verified suppliers.",
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


def contains(validation: Validation, text: str, value: object, message: str) -> None:
    expected = normalise(str(value))
    validation.require(expected in text, f"{message}: {expected!r} not found.")


def validate_client_language(validation: Validation, text: str, label: str) -> None:
    text_normal = normalise(text)
    for phrase in CLIENT_FORBIDDEN_PHRASES:
        validation.require(
            normalise(phrase) not in text_normal,
            f"{label} contains unwanted client language {phrase!r}.",
        )
    for term in INTERNAL_FORBIDDEN_TERMS:
        validation.require(
            normalise(term) not in text_normal,
            f"{label} exposes internal term {term!r}.",
        )


def validate_html(
    path: Path,
    expected_pages: int,
    config: dict,
    disclosure: str,
    validation: Validation,
    *,
    is_brochure: bool,
) -> None:
    source = path.read_text(encoding="utf-8")
    source_normal = normalise(source)
    validation.require(
        source.lower().count('<section class="page') == expected_pages,
        f"{path.name} must contain {expected_pages} page sections.",
    )
    validation.require(source.lower().count("<script") == 0, f"{path.name} contains JavaScript.")
    validation.require("file:///" not in source, f"{path.name} contains an absolute file URL.")
    validation.require(
        "/mnt/" not in source and "/home/" not in source,
        f"{path.name} exposes a local filesystem path.",
    )
    validation.require("TBC" not in source and "TODO" not in source, f"{path.name} contains a placeholder.")

    image_sources = re.findall(r'<img[^>]+src="([^"]+)"', source)
    validation.require(bool(image_sources), f"{path.name} contains no visual assets.")
    for image_source in image_sources:
        validation.require(
            not image_source.startswith(("http://", "https://", "file:")),
            f"{path.name} contains a remote or absolute image source: {image_source}",
        )
        image_path = (path.parent / image_source).resolve()
        validation.require(image_path.exists(), f"{path.name} references missing image {image_source}.")

    expected_disclosures = 1 if is_brochure else 0
    validation.require(
        source.count(disclosure) == expected_disclosures,
        f"{path.name} must contain the disclosure {expected_disclosures} time(s).",
    )
    for value in (
        config["price_display"],
        config["price_period"],
        config["tax_wording"],
        config["billing"],
        config["delivery_cadence"],
        config["normal_delivery_day"],
        config["delivery_channel"],
        config["setup_fee"],
        config["initial_price_period"],
    ):
        contains(validation, source_normal, value, f"{path.name} client fact")
    for fact in REQUIRED_CLIENT_FACTS:
        contains(validation, source_normal, fact, f"{path.name} required wording")
    validate_client_language(validation, source, path.name)

    urls = re.findall(r'href="([^"]+)"', source)
    external = [url for url in urls if url.startswith(("http://", "https://"))]
    validation.require(
        all(urlparse(url).scheme == "https" for url in external),
        f"{path.name} contains a non-HTTPS external URL.",
    )


def validate_pdf(
    path: Path,
    expected_pages: int,
    expected_title: str,
    expected_page_titles: list[str],
    config: dict,
    disclosure: str,
    validation: Validation,
    *,
    disclosure_page: int | None,
    page_word_limits: list[int],
    total_word_limit: int,
) -> None:
    reader = PdfReader(path)
    validation.require(not reader.is_encrypted, f"{path.name} must not be encrypted.")
    validation.require(len(reader.pages) == expected_pages, f"{path.name} has the wrong page count.")
    validation.require(reader.metadata.get("/Title") == expected_title, f"{path.name} title metadata changed.")
    root = reader.trailer["/Root"]
    validation.require("/StructTreeRoot" in root, f"{path.name} is not tagged with a structure tree.")
    validation.require(bool(root.get("/MarkInfo", {}).get("/Marked")), f"{path.name} marked flag is missing.")
    validation.require(bool(root.get("/Lang")), f"{path.name} language metadata is missing.")
    validation.require(root.get("/AcroForm") is None, f"{path.name} contains an interactive form.")
    validation.require(root.get("/Names") is None, f"{path.name} contains scripts or embedded-file names.")

    with fitz.open(path) as document:
        validation.require(document.page_count == expected_pages, f"{path.name} reader page counts disagree.")
        page_texts: list[str] = []
        page_word_counts: list[int] = []
        all_links: list[dict] = []
        for index, page in enumerate(document):
            page_number = index + 1
            validation.require(
                abs(page.rect.width - 595.0) < 1.0 and abs(page.rect.height - 841.9) < 1.0,
                f"{path.name} page {page_number} is not A4 portrait.",
            )
            validation.require(page.rotation == 0, f"{path.name} page {page_number} is rotated.")
            raw_text = page.get_text("text")
            text = normalise(raw_text)
            words = page.get_text("words")
            page_texts.append(text)
            page_word_counts.append(len(words))
            validation.require(bool(text), f"{path.name} page {page_number} has no extractable text.")
            contains(validation, text, expected_page_titles[index], f"{path.name} page {page_number} title")
            validation.require(
                len(words) <= page_word_limits[index],
                f"{path.name} page {page_number} has {len(words)} words; limit is {page_word_limits[index]}.",
            )
            for word in words:
                x0, y0, x1, y1 = word[:4]
                validation.require(
                    x0 >= -0.5 and y0 >= -0.5 and x1 <= page.rect.width + 0.5 and y1 <= page.rect.height + 0.5,
                    f"{path.name} page {page_number} has text outside the page boundary.",
                )
            links = page.get_links()
            all_links.extend(links)
            validation.require(
                not any(link.get("file") for link in links),
                f"{path.name} page {page_number} contains a local-file link.",
            )

        validation.require(
            sum(page_word_counts) <= total_word_limit,
            f"{path.name} has {sum(page_word_counts)} words; limit is {total_word_limit}.",
        )
        disclosure_normal = normalise(disclosure)
        disclosure_pages = [
            index + 1 for index, page_text in enumerate(page_texts) if disclosure_normal in page_text
        ]
        expected_disclosure_pages = [] if disclosure_page is None else [disclosure_page]
        validation.require(
            disclosure_pages == expected_disclosure_pages,
            f"{path.name} disclosure pages are {disclosure_pages}, expected {expected_disclosure_pages}.",
        )
        all_text = " ".join(page_texts)
        for value in (
            config["price_display"],
            config["price_period"],
            config["tax_wording"],
            config["billing"],
            config["delivery_cadence"],
            config["normal_delivery_day"],
            config["delivery_channel"],
            config["setup_fee"],
            config["initial_price_period"],
        ):
            contains(validation, all_text, value, f"{path.name} client fact")
        contains(validation, all_text, "₹1,500 per month", f"{path.name} natural price wording")
        for fact in REQUIRED_CLIENT_FACTS:
            contains(validation, all_text, fact, f"{path.name} required wording")
        validate_client_language(validation, all_text, path.name)
        uri_links = [link["uri"] for link in all_links if link.get("uri")]
        validation.require(
            all(urlparse(uri).scheme == "https" for uri in uri_links),
            f"{path.name} contains a non-HTTPS external link.",
        )

    validation.require(path.stat().st_size < 10 * 1024 * 1024, f"{path.name} exceeds 10 MB.")
    validation.require(path.stat().st_size > 100 * 1024, f"{path.name} is implausibly small.")


def validate_manifest(
    manifest_path: Path,
    config_path: Path,
    data_path: Path,
    validation: Validation,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation.require(manifest["schema_version"] == "1.1", "Manifest schema version changed.")
    validation.require(manifest["status"] == "rendered", "Package manifest status is not rendered.")
    validation.require(manifest["page_contract"] == {"brochure": 5, "offer_summary": 1}, "Page contract changed.")
    validation.require(manifest["commercial_config_sha256"] == sha256(config_path), "Config hash is stale.")
    validation.require(manifest["sample_data_sha256"] == sha256(data_path), "Sample data hash is stale.")
    validation.require(manifest["disclosure_artifact"] == BROCHURE_FILENAME, "Disclosure artifact changed.")
    validation.require(manifest["disclosure_page"] == 4, "Disclosure page changed.")

    for label, record in manifest["source_artifacts"].items():
        path = Path(record["path"])
        validation.require(path.exists(), f"Manifest source {label} is missing.")
        if path.exists():
            validation.require(record["sha256"] == sha256(path), f"Manifest source hash is stale for {label}.")
            validation.require(record["bytes"] == path.stat().st_size, f"Manifest source size is stale for {label}.")
    for path_text, record in manifest["generated_sources"].items():
        path = Path(path_text)
        validation.require(path.exists(), f"Generated source is missing: {path}.")
        if path.exists():
            validation.require(record["sha256"] == sha256(path), f"Generated-source hash is stale for {path}.")
            validation.require(record["bytes"] == path.stat().st_size, f"Generated-source size is stale for {path}.")

    artifacts = manifest["artifacts"]
    validation.require(len(artifacts) == 6, "Manifest must contain six package artifacts.")
    validation.require(len({record["filename"] for record in artifacts}) == 6, "Artifact filenames are not unique.")
    expected_names = {
        BROCHURE_FILENAME,
        REPORT_FILENAME,
        WORKBOOK_FILENAME,
        OFFER_FILENAME,
        WALKTHROUGH_FILENAME,
        GUIDE_FILENAME,
    }
    validation.require({record["filename"] for record in artifacts} == expected_names, "Artifact set changed.")
    for record in artifacts:
        path = Path(record["path"])
        validation.require(path.exists(), f"Manifest artifact is missing: {path}.")
        if path.exists():
            validation.require(record["sha256"] == sha256(path), f"Artifact hash is stale for {path}.")
            validation.require(record["bytes"] == path.stat().st_size, f"Artifact size is stale for {path}.")
    shareable = {record["filename"] for record in artifacts if record["client_shareable"]}
    validation.require(
        shareable == {BROCHURE_FILENAME, REPORT_FILENAME, WORKBOOK_FILENAME, OFFER_FILENAME},
        "Client-shareable artifact set changed.",
    )
    page_counts = {record["filename"]: record.get("page_count") for record in artifacts}
    validation.require(page_counts[BROCHURE_FILENAME] == 5, "Manifest brochure page count changed.")
    validation.require(page_counts[OFFER_FILENAME] == 1, "Manifest offer page count changed.")
    validation.require(page_counts[REPORT_FILENAME] == 20, "Manifest sample-report page count changed.")
    return manifest


def validate_support_documents(
    walkthrough_path: Path,
    guide_path: Path,
    config: dict,
    validation: Validation,
) -> None:
    walkthrough = walkthrough_path.read_text(encoding="utf-8")
    walkthrough_normal = normalise(walkthrough)
    guide = guide_path.read_text(encoding="utf-8")
    guide_normal = normalise(guide)
    for question in REQUIRED_WALKTHROUGH_QUESTIONS:
        contains(validation, walkthrough_normal, question, "Walkthrough objection")
    for value in (
        config["price_display"],
        config["price_period"],
        config["tax_wording"],
        config["normal_delivery_day"],
        "email with a WhatsApp notification and download link",
        "written acceptance of the scope and price",
        "first-month payment",
    ):
        contains(validation, walkthrough_normal, value, "Walkthrough service wording")
    validation.require("TBC" not in walkthrough and "TODO" not in walkthrough, "Walkthrough contains a placeholder.")
    for filename in (BROCHURE_FILENAME, REPORT_FILENAME, WORKBOOK_FILENAME, OFFER_FILENAME):
        validation.require(filename in walkthrough, f"Walkthrough omits {filename}.")
        validation.require(filename in guide, f"Package guide omits {filename}.")
    validation.require(WALKTHROUGH_FILENAME in guide, "Package guide omits the internal walkthrough.")
    contains(validation, guide_normal, "Do not begin application development", "Guide application boundary")
    validation.require("TBC" not in guide and "TODO" not in guide, "Package guide contains a placeholder.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("preview/brochure/commercial-config.json"))
    parser.add_argument("--data", type=Path, default=Path("preview/sample-data/client_preview_dataset.json"))
    parser.add_argument("--preview-dir", type=Path, default=Path("preview/brochure"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/client-preview"))
    parser.add_argument("--docs-dir", type=Path, default=SUPPORT_DOCS_DIR)
    args = parser.parse_args()

    validation = Validation()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    data = json.loads(args.data.read_text(encoding="utf-8"))
    try:
        validate_config(config)
    except (ValueError, KeyError) as error:
        validation.require(False, f"Commercial configuration failed: {error}")
    else:
        validation.require(True, "Commercial configuration passed.")
    for key, value in APPROVED_COMMERCIAL_FIELDS.items():
        validation.require(config.get(key) == value, f"Approved commercial field changed: {key}.")

    disclosure = data["report"]["disclosure"]
    brochure_html = args.preview_dir / "brochure.html"
    offer_html = args.preview_dir / "offer-summary.html"
    validate_html(brochure_html, 5, config, disclosure, validation, is_brochure=True)
    validate_html(offer_html, 1, config, disclosure, validation, is_brochure=False)

    brochure_pdf = args.output_dir / BROCHURE_FILENAME
    offer_pdf = args.output_dir / OFFER_FILENAME
    validate_pdf(
        brochure_pdf,
        5,
        "NeuraFlow Product Opportunity Radar · Service Overview",
        BROCHURE_TITLES,
        config,
        disclosure,
        validation,
        disclosure_page=4,
        page_word_limits=BROCHURE_PAGE_WORD_LIMITS,
        total_word_limit=BROCHURE_TOTAL_WORD_LIMIT,
    )
    validate_pdf(
        offer_pdf,
        1,
        "NeuraFlow Monthly Service Offer",
        ["WEEKLY RESEARCH, CLEARLY PACKAGED"],
        config,
        disclosure,
        validation,
        disclosure_page=None,
        page_word_limits=[OFFER_WORD_LIMIT],
        total_word_limit=OFFER_WORD_LIMIT,
    )

    validate_manifest(args.preview_dir / "client-preview-package-manifest.json", args.config, args.data, validation)
    validate_support_documents(
        args.docs_dir / WALKTHROUGH_FILENAME,
        args.docs_dir / GUIDE_FILENAME,
        config,
        validation,
    )

    if validation.errors:
        print(f"FAILED: {len(validation.errors)} issue(s) across {validation.checks} checks")
        for error in validation.errors:
            print(f"- {error}")
        return 1
    print(
        "PASS: revised P6 service overview, monthly offer, client demo kit, "
        f"and package manifest passed {validation.checks} checks"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
