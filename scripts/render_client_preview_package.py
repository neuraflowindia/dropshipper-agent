#!/usr/bin/env python3
"""Render P6 client-facing PDFs and finalize the package manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import fitz

from generate_client_preview_package import (
    BROCHURE_FILENAME,
    GUIDE_FILENAME,
    OFFER_FILENAME,
    REPORT_FILENAME,
    SUPPORT_DOCS_DIR,
    WALKTHROUGH_FILENAME,
    WORKBOOK_FILENAME,
)
from render_sample_pdf import find_chrome, render


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_record(path: Path, role: str, client_shareable: bool) -> dict:
    record = {
        "path": str(path),
        "filename": path.name,
        "role": role,
        "client_shareable": client_shareable,
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
    }
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as document:
            record["page_count"] = len(document)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-dir", type=Path, default=Path("preview/brochure"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/client-preview"))
    parser.add_argument("--docs-dir", type=Path, default=SUPPORT_DOCS_DIR)
    parser.add_argument("--chrome", type=Path)
    args = parser.parse_args()

    chrome = find_chrome(args.chrome)
    brochure_pdf = args.output_dir / BROCHURE_FILENAME
    offer_pdf = args.output_dir / OFFER_FILENAME
    render(args.preview_dir / "brochure.html", brochure_pdf, chrome)
    render(args.preview_dir / "offer-summary.html", offer_pdf, chrome)

    artifacts = [
        artifact_record(brochure_pdf, "client brochure", True),
        artifact_record(args.output_dir / REPORT_FILENAME, "illustrative weekly report", True),
        artifact_record(args.output_dir / WORKBOOK_FILENAME, "illustrative evidence workbook", True),
        artifact_record(offer_pdf, "monthly service offer", True),
        artifact_record(args.docs_dir / WALKTHROUGH_FILENAME, "operator demo walkthrough", False),
        artifact_record(args.docs_dir / GUIDE_FILENAME, "operator package guide", False),
    ]
    manifest_path = args.preview_dir / "client-preview-package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "rendered"
    manifest["finalized_on"] = "2026-08-16"
    manifest["artifacts"] = artifacts
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
