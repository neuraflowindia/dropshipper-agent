# Session P5 — Full Portrait PDF Production Plan

**Date:** 15 August 2026  
**Status:** Complete and owner-approved  
**Artifact target:** `NeuraFlow_Product_Opportunity_Brief_Illustrative_2026-08-15.pdf`  
**Scope boundary:** Manufactured client-preview report only; no application, live provider call, credential, real supplier claim, brochure, or paid activity

---

## 1. Objective

Create the complete 20-page portrait briefing that a prospective client can read comfortably on a phone and use as the primary weekly decision summary.

The PDF must:

- translate the approved Excel evidence into a concise editorial narrative;
- look premium, calm, restrained, and recognisably NeuraFlow;
- make the most important decisions understandable in less than five minutes;
- remain useful when the client reads individual opportunity pages in more detail;
- retain exact data and evidence meaning from the canonical JSON;
- distinguish opportunity evidence from quick-commerce context, supplier leads, compliance responsibility, and forecasts;
- contain true selectable text and clickable descriptive links;
- use the selected Signal Ledger direction without copying its inaccurate sample values or over-minimising useful structure;
- regenerate from source without manual PDF edits.

“Premium” means disciplined hierarchy, readable typography, coherent imagery, exact content, and purposeful restraint. It does not mean glossy advertising, excessive decoration, sparse pages with missing explanation, or invented claims.

---

## 2. Approved Inputs and Authority Order

1. `preview/sample-data/client_preview_dataset.json` controls all exact sample data.
2. `docs/client-preview/client_preview_package_and_demo_data_blueprint.md` controls the manufactured preview and fixed 20-page map.
3. `docs/product/india_dropshipping_product_opportunity_radar_product_brief.md` controls service boundaries, signal meaning, supplier language, and future live-operation rules.
4. `media/design-directions/session-2/02-signal-ledger-refined.png` controls visual mood and hierarchy only.
5. `media/NeuraFlow/long_maroon_path_transparent_1024.png` is the approved logo asset.
6. `media/NeuraFlow/Brand colours.txt` supplies the two fixed brand colours; approved muted supporting colours may be used with restraint.

If the visual reference and canonical data disagree, canonical data wins. If concise report copy and the Excel use different wording, their meaning and numbers must still reconcile exactly.

---

## 3. Fixed Output Contract

- A4 portrait pages, exactly 20.
- One cover disclosure only:

  > Illustrative preview using sample data; not a live market-research report.

- No repeated report-level disclosure on interior pages.
- A short “Illustrative product visual” caption beneath each generated product image is permitted because it describes the image, not the report data.
- Twelve opportunity pages in exact rank order.
- One representative supplier lead per product on page 18.
- Up to two supplier leads per opportunity page when space and evidence allow.
- One short horizontal rule beneath each folio; no paired vertical rails.
- Original maroon logo on cream or another sufficiently light surface only.
- No opportunity score, sales forecast, profit estimate, compliance conclusion, or verified-supplier claim.
- Final PDF below 10 MB.
- All generated or rendered visual assets stored under `media`.
- Final client artifact stored under `outputs/client-preview`.

---

## 4. Initial Execution Plan

1. Freeze P4 after owner approval and record P5 as active.
2. Create a deterministic report view model from the canonical JSON.
3. Generate a coherent illustrative image for each of the twelve products.
4. Build a single HTML/CSS report template containing all twenty pages.
5. Render it with local headless Google Chrome.
6. Validate page count, dimensions, text, links, file size, disclosure placement, and PDF-to-source equality.
7. Render every page to PNG.
8. Inspect all pages at full-page and reduced mobile-reading sizes.
9. Correct source-derived presentation or template logic, regenerate, and repeat.
10. Update project documentation only after the final report passes.

---

## 5. Critique and Strengthening

### 5.1 Mobile-readability critique

**Weakness:** A4 portrait can still become unreadable on a phone if it is treated like a desktop document.

**Correction:** Use one primary reading column for narrative pages. Product pages may use a split hero area, but evidence, caution, and supplier sections remain full-width. Maintain approximately 10.5–11 pt minimum body text, short paragraphs, generous line spacing, and no dense three-column table.

### 5.2 Twenty-page rigidity critique

**Weakness:** A fixed page count can tempt content padding or cramming.

**Correction:** The 20-page map has a real job for every page and exactly twelve product records. Keep content bounded per page. Do not add filler statistics. Do not move overflow into extra pages; instead shorten derived prose without removing required evidence.

### 5.3 Data-drift critique

**Weakness:** Hand-written report copy can quietly contradict Excel.

**Correction:** Generate all names, ranks, labels, statuses, signal values, counts, quick-commerce totals, compliance flags, warnings, listing bands, and supplier fields directly from JSON. Add an independent validator that extracts PDF text and reconciles every required field.

### 5.4 Signal-explanation critique

**Weakness:** Three coloured rows can look authoritative while concealing missing coverage or threshold meaning.

**Correction:** Every signal row must include the signal name, exact outcome label, compact evidence, and one plain-language interpretation. “Not positive” is never rewritten as “Negative.” “Missing” is never rewritten as zero.

### 5.5 Supplier-selection critique

**Weakness:** Selecting the first supplier record could surface the weakest example and make the report feel arbitrary.

**Correction:** Rank illustrative supplier examples deterministically: evidence clarity first, then URL status, then lead ID. Prefer Clear over Partial over Uncertain and Reachable over Redirected, Unchecked, Blocked, or Unreachable. This affects report display only and never the opportunity label.

### 5.6 Image-integrity critique

**Weakness:** Generated product imagery can resemble a seller listing, introduce packaging claims, or depict the wrong object.

**Correction:** Generate neutral editorial product still-lifes with no logos, packaging, text, badges, people, or performance claims. Inspect each image for recognisable product identity. Keep product imagery as treatment only; all exact text and evidence remain deterministic HTML.

### 5.7 Visual-direction critique

**Weakness:** Copying the refined direction literally would import its manufactured sample totals and could repeat the previous over-optimisation of empty space and dividers.

**Correction:** Preserve its cream ground, navy hierarchy, maroon identity, condensed display type, folio treatment, and editorial pacing. Rebuild all data and layouts from the approved report contract. Use dividers only to separate real reading groups.

### 5.8 Cover-contrast critique

**Weakness:** A navy cover field behind the maroon logo would repeat the rejected contrast problem.

**Correction:** Keep the logo on cream. Navy may appear as a side field or lower structural block, never directly behind the existing logo.

### 5.9 Disclosure-clutter critique

**Weakness:** Repeating the sample-data disclaimer throughout twenty pages would look defensive.

**Correction:** Show the exact approved disclosure once on the cover. Interior pages use only necessary content-specific boundaries such as “context only,” “possible supplier lead,” and “illustrative product visual.”

### 5.10 Renderer critique

**Weakness:** Maintaining both Chromium and WeasyPrint would create duplicated layout bugs and inconsistent PDFs.

**Correction:** Use one renderer only. Local Google Chrome is available and is the preferred product-brief route, so P5 selects headless Chrome. The generator creates HTML/CSS; Chrome creates the final PDF.

### 5.11 Accessibility critique

**Weakness:** A beautiful PDF can still fail if text is flattened, links are vague, colour is the only status cue, or reading order is incoherent.

**Correction:** Use semantic HTML headings, paragraphs, lists, tables, and links; pair every colour with an explicit text label; use descriptive link wording; verify extractable text, logical page order, contrast, and link annotations.

### 5.12 Review-loop critique

**Weakness:** “Keep improving” can become subjective polishing with no finish line.

**Correction:** Use explicit logic, content, visual, accessibility, commercial, and file-integrity passes. Stop only when all exit criteria pass and no known defect remains inside P5 scope.

---

## 6. Renderer and File Architecture

### Selected renderer

Headless Google Chrome printing a local HTML/CSS document to PDF.

### Why this renderer

- already installed;
- no additional browser download;
- supports modern paged CSS;
- preserves real text and hyperlinks;
- supports a future hosted-report path without forcing one now;
- matches the preference recorded in the product brief.

### Files

- `scripts/generate_sample_pdf.py` — builds deterministic HTML and the report manifest from JSON.
- `preview/report/` — generated local HTML/CSS preview files.
- `scripts/validate_sample_pdf.py` — validates source, manifest, and PDF.
- `scripts/render_pdf_review.py` — renders full-page and mobile-scale review images.
- `media/client-preview/product-visuals/` — generated product images.
- `media/client-preview/pdf-review/` — page renders and review contact sheets.
- `outputs/client-preview/NeuraFlow_Product_Opportunity_Brief_Illustrative_2026-08-15.pdf` — final report.

The HTML is a production intermediate, not the client deliverable. All local links and assets must resolve without network access.

---

## 7. Visual System

### Palette

- Cream: `#F9F3EC`
- Maroon: `#6B1F2B`
- Navy: `#24384B`
- Muted navy: `#647487`
- Muted mustard: `#C49A37`
- Muted sage: `#93A28E`
- Muted rose: `#C9949A`
- Charcoal: `#292C2F`
- Soft line: `#D8D0C8`

### Typography

- Condensed sans-serif for page titles and major figures.
- Serif for short editorial explanations where it improves warmth.
- Sans-serif for evidence labels, tables, captions, and small navigation.
- Body text approximately 10.5–11 pt with comfortable leading.
- No paragraph exceeds about 65 words.

### Page rhythm

- generous but not wasteful outer margins;
- consistent top folio and short rule;
- strong title hierarchy;
- one or two dominant visual groups per page;
- thin functional rules only where content changes meaning;
- matte imagery with restrained shadows;
- no card-wall dashboard treatment.

---

## 8. Page-by-Page Content Plan

### Page 1 — Cover

- cream logo zone;
- service title and “Weekly opportunity brief”;
- week and illustrative client name;
- one restrained research still-life or abstract product-study visual;
- exact one-time disclosure;
- no evidence statistics.

### Page 2 — This Week at a Glance

- 12 opportunities;
- 4 Strong, 6 Promising, 2 Worth Watching;
- 26 positive outcomes across 35 available assessments;
- 5 compliance flags;
- four category counts of 3;
- one sentence explaining balanced selection where evidence allowed.

### Page 3 — How to Read This Report

- the three signal families;
- label meaning based on positive signals;
- available versus missing coverage;
- weekly status meaning;
- quick-commerce as context only;
- possible supplier leads as starting points only.

### Page 4 — Category Overview

- four category groups;
- three selected products per category;
- label mix by category;
- explicit no-padding rule.

### Page 5 — Weekly Movement

- 5 New;
- 3 Returning;
- 2 Strengthened;
- 1 Weakened;
- 1 Coverage Changed;
- short explanation that coverage change is not upward or downward movement.

### Pages 6–17 — Opportunity pages

Every page contains:

- rank, product, category, label, and weekly status;
- one product visual and short image caption;
- positive and available core-signal counts;
- three signal-ledger rows;
- concise why-it-surfaced explanation;
- illustrative retail listing band;
- quick-commerce sampled-presence line ending in “context only”;
- main warning or compliance review panel;
- up to two deterministically selected possible supplier leads;
- Excel Evidence row pointer.

### Page 18 — Supplier Lead Digest

- one representative lead per product;
- four category sections;
- supplier, location, visible price, MOQ, and limitation;
- descriptive example.com source links;
- exact non-verification boundary from the blueprint;
- pointer to all 36 rows in Excel.

### Page 19 — Coverage and Limitations

- Meta coverage count;
- marketplace coverage count;
- search coverage count and rank 10 missing-search explanation;
- quick-commerce sample boundary;
- no sales, profit, conversion, return-rate, or demand forecast;
- why missing evidence is not treated as zero.

### Page 20 — Method and Client Responsibility

- compact five-step method;
- client responsibility for product choice, compliance, supplier checks, testing, and economics;
- supplier non-verification statement;
- report-use boundary;
- NeuraFlow contact placeholder;
- no repeated full demo disclosure.

---

## 9. Product-Page Evidence Rules

### Meta evidence

- available positive: active relevant ads, distinct advertisers, and oldest active-ad age;
- available not positive: same visible values with plain-language explanation that the threshold was not passed;
- missing: “Source unavailable for this sample run.”

### Marketplace evidence

- show Amazon evidence type and position when present;
- show Flipkart relevant-match position when present;
- do not imply a position is a sales volume;
- unavailable fields remain “Not available.”

### Search evidence

- show recent average, prior average, ratio, and absolute increase when available;
- use “Missing” when unavailable;
- never convert missing search into “Not positive.”

### Quick-commerce context

- use the exact number present out of nine sampled platform-city checks;
- do not use it in the market-signal label;
- end the sentence with “context only.”

---

## 10. Image Asset Contract

Each of the twelve images must:

- depict the correct generic product type clearly;
- use a consistent portrait-friendly ratio;
- use warm cream, stone, pale sage, muted navy, or dusty rose styling;
- use soft editorial daylight and matte surfaces;
- contain no readable text, logos, packaging, seller marks, badges, price tags, or people;
- avoid exaggerated effects or claim-like demonstrations;
- preserve usable negative space and a clean silhouette;
- remain an illustration rather than evidence.

The final report may use a quiet crop of one or more product images on the cover, but the cover itself must not imply a ranking or endorsement.

---

## 11. Automated Validation Contract

The validator must confirm:

- the canonical dataset still passes P4 validation;
- the report manifest contains exactly 20 pages and 12 product-page records;
- product page order is ranks 1–12;
- every exact shared field matches JSON and Excel;
- all overview and movement totals reconcile;
- page 18 contains exactly one lead per product;
- selected supplier records follow the deterministic priority rule;
- the full disclosure appears exactly once and on page 1;
- “Illustrative product visual” appears exactly twelve times;
- “context only” appears on every product page;
- required non-verification and client-responsibility text exists;
- all external hyperlinks use example.com;
- no unsafe local filesystem path remains in link annotations;
- PDF page count is 20;
- all pages are A4 portrait within normal rendering tolerance;
- text can be extracted from every page;
- every page contains its expected title or product name;
- file size is below 10 MB;
- the PDF contains no embedded files, JavaScript, forms, external resource actions, or encryption;
- all twelve expected product images were embedded.

---

## 12. Visual Review Passes

1. **Cover pass:** logo contrast, hierarchy, disclosure restraint, and cover visual.
2. **Overview pass:** figures, grouping, category balance, and movement explanation.
3. **Product-page pass:** inspect each of the twelve images, signal rows, text lengths, warning hierarchy, and suppliers.
4. **Long-content pass:** supplier digest, coverage, and methodology density.
5. **Folio pass:** page number, single short rule, title alignment, and page consistency.
6. **Mobile pass:** review all pages at a reduced width representative of phone viewing.
7. **Contrast pass:** confirm every label remains understandable without colour alone.
8. **Commercial pass:** read as a sceptical prospective client and remove vague or inflated promises.
9. **Cross-output pass:** compare PDF, Excel, manifest, and JSON.
10. **Final recursive pass:** correct and repeat every affected check until no known defect remains.

---

## 13. Exit Criteria

P5 is internally complete only when:

- exactly 20 coherent A4 portrait pages exist;
- all shared data reconciles with the canonical source and Excel;
- all twelve product visuals depict the intended generic items;
- no page contains clipped, overlapping, orphaned, or illegibly small text;
- the PDF remains readable at realistic phone width;
- the original logo has sufficient contrast;
- the single-disclosure rule is respected;
- supplier and compliance language stays within approved boundaries;
- links are descriptive and safe;
- text is selectable and extractable;
- file size remains below 10 MB;
- all pages have been visually inspected;
- a clean regeneration produces the same manifest and report meaning;
- no known logical, content, visual, accessibility, commercial, or documentation defect remains within P5 scope.

“Airtight” means the defined checks pass, the report has been visually inspected, and no known contradiction or defect remains within this manufactured preview. It is not a claim that live market research or future application behaviour can be literally error-free.

---

## 14. Completion Record

P5 completed its internal production and review cycle on 15 August 2026.

- The generator produced the fixed 20-page A4 portrait report from the canonical manufactured dataset.
- Twelve product visuals and one cover visual were generated as generic editorial illustrations, then optimised and recorded in an asset manifest.
- The PDF and Excel reconcile to the same dataset; no report value is maintained through a separate manual content path.
- The final automated suite passed 1,529 dataset, workbook, image, report-source, PDF-structure, security, and reconciliation checks.
- Two independent PDF readers confirmed 20 pages, extractable text, A4 portrait dimensions, and document tagging.
- All 20 pages were rendered to review images and inspected for hierarchy, contrast, crop quality, readability, clipping, overlap, footer collisions, line restraint, disclosure placement, and commercial-language boundaries.
- No known logical, content, visual, accessibility, commercial, or documentation defect remains within P5 scope.

The owner approved the PDF on 16 August 2026. That approval closed P5 and activated P6 brochure and demo-kit production.
