# Session P4 — Sample Excel Production Plan

**Date:** 15 August 2026  
**Status:** Completed; implementation and recursive review recorded below  
**Artifact target:** NeuraFlow_Product_Opportunity_Evidence_Illustrative_2026-08-15.xlsx  
**Scope boundary:** Preview artifact only; no application, live provider calls, credentials, or paid activity

---

## 1. Objective

Create a client-presentable Excel evidence workbook that demonstrates the likely weekly service output without pretending that live research data will arrive perfectly cleaned.

The workbook must be:

- credible enough to represent a real research workflow;
- polished enough to support a sales conversation;
- structured enough to inspect and filter;
- uneven enough to show realistic source limitations;
- internally consistent enough that every conclusion can be traced;
- visually aligned with the selected Signal Ledger direction;
- generated from one reusable structured source rather than edited cell by cell.

“Perfect” means fit for this objective. It does not mean visually sterilising every source record.

---

## 2. Non-Objectives

This session does not:

- build the eventual application;
- call Meta, Google, marketplaces, quick-commerce services, or suppliers;
- create the portrait PDF;
- create the brochure;
- make real market claims;
- verify suppliers;
- forecast sales, margin, conversion, or profitability;
- add dashboards, macros, pivots, charts, or external data connections.

---

## 3. Approved Constraints

1. The workbook contains exactly three sheets: Shortlist, Evidence, and Supplier Leads.
2. Shortlist contains one discreet demo disclosure; the other sheets do not repeat it.
3. All three sheets come from one canonical structured source.
4. The source contains 12 products, 36 supplier leads, and nine quick-commerce checks per product.
5. Evidence logic remains deterministic even when source presentation is messy.
6. Quick-commerce and supplier data never change the market-signal label.
7. Missing is not zero, and uncertain is not false.
8. The ₹1,500 figure is the client charge ceiling, not a provider-cost ceiling.
9. Monthly operating cost targets ₹750, triggers review above ₹1,000, and may reach ₹1,500 only as a consciously subsidised pilot.
10. All generated or review media stays under media.

---

## 4. Initial Execution Plan

1. Translate the approved 12-product blueprint into a structured JSON source.
2. Add realistic source variation without changing signal outcomes.
3. Create a deterministic workbook generator.
4. Build the three worksheets.
5. Apply NeuraFlow styling.
6. Run automated reconciliation checks.
7. Render or export the worksheets for visual review.
8. Inspect every sheet section by section.
9. Correct the source or generator.
10. Regenerate and repeat until all exit criteria pass.

---

## 5. Critique of the Initial Plan

### 5.1 Data realism critique

**Weakness:** “Add realistic variation” is too vague. It could produce decorative randomness, contradictory data, or a workbook that merely looks untidy.

**Correction:** Define an explicit realism contract. Variation is allowed only in source titles, aliases, missing optional fields, price formatting, marketplace phrasing, evidence clarity, URL status, note length, and provider-return detail. Counts, labels, ranks, and weekly statuses remain controlled.

### 5.2 Client-comprehension critique

**Weakness:** A wide Evidence sheet can overwhelm the client even when every column is useful.

**Correction:** Keep high-value decision columns on the left, freeze identity columns, group secondary source-detail columns, use short plain-language headers, and provide a compact purpose line. Do not hide evidence required to interpret a label.

### 5.3 Visual-design critique

**Weakness:** Copying the PDF direction too literally could make Excel sparse, decorative, or hard to scan.

**Correction:** Translate the visual direction rather than imitate it. Use cream title rows, muted navy headers, maroon identity accents, restrained mustard, subtle section bands, and functional borders. Excel remains denser than the PDF.

### 5.4 Evidence-integrity critique

**Weakness:** A polished workbook can conceal discrepancies between sheets.

**Correction:** Validate every repeated field across the source, Shortlist, Evidence, and Supplier Leads. Generate all repeated values from the same product object. Add no worksheet formula that independently recalculates the business label.

### 5.5 Supplier-realism critique

**Weakness:** Thirty-six identical “Sample Lead” rows with the same status would look artificial.

**Correction:** Use plausible fictional supplier names, varied but controlled evidence notes, mixed URL statuses, uneven price and MOQ availability, varied supplier types, and product-specific limitations. Use reserved example.com links only.

### 5.6 Disclosure critique

**Weakness:** Repeating the demo disclaimer on every sheet would look defensive and cluttered.

**Correction:** Display it once on Shortlist. Keep it visible but secondary to the workbook title.

### 5.7 Technical-verification critique

**Weakness:** A workbook can pass data checks but still open with repair warnings, broken hyperlinks, clipped text, or unusable widths.

**Correction:** Inspect the ZIP/XML package, reopen it with the generating library, and use a spreadsheet renderer if available. Produce review media for all sheets and inspect it visually.

### 5.8 Review-loop critique

**Weakness:** “Review until perfect” has no terminal condition and can encourage arbitrary polishing.

**Correction:** Use defined passes and exit criteria. Stop only when no known defect remains in logic, realism, cross-sheet consistency, readability, styling, disclosure placement, file integrity, or documentation alignment.

---

## 6. Strengthened Final Execution Plan

### Phase A — Contract alignment

1. Update the blueprint and product brief for the single-disclosure rule and corrected cost boundaries.
2. Search for and remove stale hard-stop or repeated-disclosure language.

### Phase B — Canonical source

3. Create one JSON file containing report metadata, products, signal evidence, quick-commerce checks, prior-week comparison, warnings, and supplier leads.
4. Encode explicit missing values as null, not zero or invented text.
5. Add controlled variation according to the realism contract.
6. Validate JSON structure, allowed labels, signal arithmetic, rank order, weekly status, category counts, quick-commerce totals, and lead counts.

### Phase C — Workbook generation

7. Create one deterministic generation script.
8. Build Shortlist with 12 ranked rows and one discreet disclosure.
9. Build Evidence with 12 detailed rows, frozen identity columns, grouped secondary columns, and source-level variation.
10. Build Supplier Leads with 36 varied rows and product-specific limitations.
11. Apply the Signal Ledger visual translation and NeuraFlow logo on a light surface.
12. Save the workbook to outputs/client-preview.

### Phase D — Automated review

13. Reopen the workbook programmatically.
14. Verify sheet names, row counts, headers, freeze panes, filters, grouping, hyperlinks, values, data types, widths, heights, styles, print settings, and absence of formulas, macros, external links, or secrets.
15. Compare workbook values with the JSON source and recalculate all aggregate counts.
16. Inspect the XLSX package for malformed relationships or unexpected files.

### Phase E — Visual review

17. Render or export every sheet.
18. Store review images under media/client-preview/excel-review.
19. Inspect title hierarchy, disclosure prominence, logo contrast, density, column widths, wrapping, truncation, banding, warning hierarchy, and whether the workbook looks realistically uneven rather than randomly messy.
20. Review Shortlist, Evidence, and Supplier Leads separately because each has a different reading job.

### Phase F — Recursive correction

21. Correct data problems in JSON and presentation problems in the generator.
22. Regenerate rather than manually editing the workbook.
23. Repeat automated and visual passes after each material correction.
24. Align the blueprint and product brief if the approved workbook reveals a better contract.

### Phase G — Closeout

25. Record what was intentionally messy, what remained strictly normalised, what checks passed, and what remains for the PDF session.

---

## 7. Realism Contract

### 7.1 Intentionally uneven

- Product aliases range from one to three terms.
- Some marketplace positions are missing because the relevant match was absent.
- Supplier prices may be numeric, range-like, “Quote on request,” or missing.
- MOQs may be exact, pack-based, negotiable, or missing.
- Evidence notes vary in length according to what the source exposed.
- Collection times vary naturally within the illustrative run window.
- URL statuses include Reachable, Redirected, Unchecked, Blocked, and Unreachable.
- Evidence clarity includes Clear, Partial, and Uncertain.
- Marketplace titles may contain pack sizes, colour language, or common seller phrasing.
- Quick-commerce matches vary by city and platform.
- Warnings are specific to each product rather than templated.

### 7.2 Strictly normalised

- Product ID, generic product name, final rank, and category.
- Market-signal label and weekly status.
- Positive- and available-signal counts.
- Signal thresholds and outcomes.
- Core coverage language.
- Compliance flag values.
- Supplier-to-product mapping.
- Quick-commerce present-count totals.
- Canonical dates and reporting period.
- Cross-sheet repeated values.

### 7.3 Never introduced as “realism”

- contradictory totals;
- random spelling errors;
- broken formulas;
- accidental duplicated leads;
- invalid URLs outside reserved example.com;
- unsupported claims;
- fake certifications;
- supplier verification language;
- arbitrary colours or alignments;
- inconsistent date systems;
- invisible critical fields;
- zero substituted for missing.

---

## 8. Workbook Reading Design

### Shortlist

**Job:** Let the client decide where to look first.

Design:

- compact title area;
- logo on cream;
- one muted disclosure line;
- frozen header and first four columns;
- strongest decision fields kept visible on the left;
- restrained signal and status tints;
- no embedded product images;
- evidence links jump to the matching Evidence row;
- filters across the full table.

### Evidence

**Job:** Let the client understand why each product received its label.

Design:

- identity and final-result columns first;
- Meta, Marketplace, Search, Quick Commerce, Review, and Provenance sections;
- section-coloured header bands;
- secondary URL and timestamp columns grouped for optional collapse;
- unavailable values visibly blank or marked “Not available,” never zero;
- no composite opportunity score;
- freeze panes after identity columns.

### Supplier Leads

**Job:** Provide credible starting points for independent investigation.

Design:

- rank, product, supplier, location, type, price, and MOQ first;
- evidence clarity and limitation visible without horizontal scrolling where practical;
- hyperlink and source-domain fields later;
- mixed but plausible missingness;
- no verified/recommended badge;
- subtle clarity tint only;
- one purpose line explaining that visible price is not landed cost.

---

## 9. Review Passes

1. **Contract pass:** sheet names, columns, counts, disclosure placement, filenames.
2. **Logic pass:** signal thresholds, labels, ranks, coverage, weekly statuses.
3. **Realism pass:** variation, missingness, source phrasing, supplier unevenness.
4. **Narrative pass:** why-it-surfaced notes, warnings, plain-language clarity.
5. **Design pass:** hierarchy, colour, density, alignment, wrapping, widths, borders.
6. **Usability pass:** filters, freeze panes, grouping, hyperlinks, print settings.
7. **Safety pass:** formula injection, secrets, real supplier links, unsupported claims.
8. **Cross-sheet pass:** every repeated value compared with the canonical source.
9. **File-integrity pass:** reopen, inspect package, verify no repair warning indicators.
10. **Final sceptical-client pass:** determine whether the workbook feels useful, credible, and appropriately imperfect.

---

## 10. Exit Criteria

The workbook is complete only when:

- exactly three correctly named sheets exist;
- Shortlist has 12 products;
- Evidence has 12 corresponding products;
- Supplier Leads has 36 unique leads and three per product;
- signal totals equal 26 positive outcomes across 35 available assessments;
- quick-commerce totals equal 50 presences across 108 checks;
- rank order and weekly statuses match the canonical source;
- all fictional supplier URLs remain under example.com;
- missing values are preserved honestly;
- no label is calculated from quick-commerce or supplier evidence;
- no formula, macro, external connection, credential, or secret is present;
- the single disclosure appears on Shortlist only;
- all three sheets are visually inspected;
- no known truncation, illegible density, misleading styling, or unexplained inconsistency remains;
- the workbook can be regenerated from source without manual cell edits.

---

## 11. Completion and Review Record

### 11.1 Produced artifacts

- Canonical manufactured source: `preview/sample-data/client_preview_dataset.json`
- Deterministic generator: `scripts/generate_sample_excel.py`
- Independent validator: `scripts/validate_preview_dataset.py`
- Visual-review renderer: `scripts/render_excel_review.py`
- Client workbook: `outputs/client-preview/NeuraFlow_Product_Opportunity_Evidence_Illustrative_2026-08-15.xlsx`
- Review images: `media/client-preview/excel-review/`

### 11.2 Final automated result

The dataset and workbook passed 835 checks together. The checks cover:

- signal arithmetic, label rules, ranking, category balance, and weekly-status logic;
- exact row-by-row reconciliation of every displayed value and hyperlink against the canonical source;
- the 12-product, 36-lead, 35-available-signal, 26-positive-signal, and 108-check contracts;
- deliberately uneven supplier status, clarity, type, price, and MOQ distributions;
- sheet names, headers, dimensions, filters, frozen panes, grouping, logo presence, and disclosure placement;
- formula-injection prefixes, formulas, macros, external workbook links, non-example.com links, and file-package integrity.

### 11.3 Visual result

All three sheets were rendered from the actual XLSX values and styles and reviewed in five views:

1. complete Shortlist;
2. Evidence identity and Meta sections;
3. Evidence marketplace and search sections;
4. Evidence quick-commerce, review, and provenance sections;
5. complete Supplier Leads ledger.

No known clipping, contrast failure, misleading status styling, unexplained decorative divider, or cross-sheet visual inconsistency remains in those views. The workbook deliberately remains wider and denser than the mobile PDF because its job is detailed inspection.

### 11.4 Verification boundary

Microsoft Excel and LibreOffice were not available in the working environment. Visual review therefore used a project-local renderer that reads the generated XLSX directly. The file also reopened successfully through the workbook library and its package structure passed validation. A final native-app viewing by the owner is recommended before client sharing, specifically to confirm local font substitution and print-preview pagination; it is not a reason to rebuild the workbook.

### 11.5 Final audit outcome

The last audit removed a stale duplicated supplier table from the blueprint and made the structured JSON the sole exact-row authority. It also strengthened the validator from partial-field checks to complete displayed-field and hyperlink reconciliation. No known logical, structural, safety, or documentation defect remains within the approved P4 scope.
