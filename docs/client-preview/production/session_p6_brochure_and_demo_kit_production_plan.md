# Session P6 — Brochure and Client Demo Kit Production Plan

**Date:** 15 August 2026  
**Status:** Superseded by the [P6 revision plan](session_p6_brochure_revision_plan.md)  
**Scope boundary:** Manufactured pre-sales package only; no application, live provider call, credential, real market claim, supplier verification, invoice, public website, or paid activity

> **Revision note:** This document records the first P6 production cycle. Its six-page structure, original client wording, and original brochure and offer filenames are retained only as history. The revised five-page service overview, plain-language offer, current filenames, and current validation record are controlled by the [P6 revision plan](session_p6_brochure_revision_plan.md).

---

## 1. Objective

Turn the owner-approved Excel workbook and 20-page portrait report into a concise sales package that lets a prospective client understand the service, see the output, evaluate the founding offer, and make a commercial decision without an application demo.

P6 must answer five client questions quickly:

1. What problem does the service reduce?
2. What will I receive each week?
3. How are products assessed?
4. What does the founding offer cost and include?
5. What must happen before the paid pilot begins?

The package must sell the value of disciplined weekly research without claiming guaranteed winners, sales, profit, automation, verified suppliers, compliance approval, real-time monitoring, or software access.

---

## 2. Approved Inputs and Authority Order

1. `docs/product/india_dropshipping_product_opportunity_radar_product_brief.md` controls the eventual service, commercial boundaries, evidence logic, and commitment gate.
2. `docs/client-preview/client_preview_package_and_demo_data_blueprint.md` controls the manufactured preview, founding offer, disclosure rule, and brochure messages.
3. The owner-approved PDF and Excel control what can be demonstrated as an existing output.
4. `preview/sample-data/client_preview_dataset.json` controls any sample figures reused from those outputs.
5. `media/NeuraFlow/long_maroon_path_transparent_1024.png` is the approved logo.
6. `media/NeuraFlow/Brand colours.txt` provides the fixed cream and maroon; the approved muted navy, sage, rose, and mustard may support hierarchy.
7. The approved Signal Ledger direction and P5 report govern the visual family.
8. The P6 commercial configuration controls the owner-approved tax wording, Monday delivery, email delivery with a WhatsApp notification and download link, and the neutral client-facing call to action. No generator may invent or silently change these fields.

If an exact value conflicts across these inputs, production stops until the documents are reconciled.

---

## 3. Fixed P6 Deliverables

### 3.1 Client-facing brochure

- Six A4 portrait pages.
- Primary pre-sales explanation of the weekly subscription.
- Premium, restrained editorial design that belongs to the same family as the approved report.
- One discreet sample-data disclosure, placed only where the manufactured output is shown.
- Output: `outputs/client-preview/NeuraFlow_Product_Opportunity_Radar_Founding_Pilot_Brochure_2026-08-15.pdf`.

### 3.2 Founding-offer summary

- One A4 portrait page.
- Exact price, cadence, included work, exclusions, pilot terms, tax wording, delivery arrangement, and next step.
- No sample output and therefore no repeated sample-data disclosure.
- Output: `outputs/client-preview/NeuraFlow_Founding_Client_Offer_2026-08-15.pdf`.

### 3.3 Operator demo walkthrough

- An internal Markdown guide, not a client promise.
- A short presentation order, plain-language talk track, questions to ask, objection handling, and the commitment gate.
- It must say which files to show, which files to send, and which internal files never to send.
- Output (current location): `docs/client-preview/guides/NeuraFlow_Client_Demo_Walkthrough_2026-08-15.md`.

### 3.4 Package guide and manifest

- A short operator-facing package guide listing the approved presentation sequence and client-shareable files.
- A machine-readable manifest with filenames, roles, page counts, sizes, hashes, generation inputs, and disclosure placement.
- Outputs (current locations): `docs/client-preview/guides/NeuraFlow_Client_Preview_Package_Guide_2026-08-15.md` and `preview/brochure/client-preview-package-manifest.json`.

The P4 Excel and P5 report remain part of the package. P6 does not duplicate or replace them.

---

## 4. Initial Execution Plan

1. Record the owner's P5 approval and mark P6 active.
2. Freeze the client-facing offer fields in one configuration source.
3. Draft the brochure and offer-summary copy from the approved product documents and outputs.
4. Perform a promise audit before composition.
5. Build deterministic HTML/CSS sources.
6. Render the brochure and offer summary through the same local Chrome path proven in P5.
7. Generate the operator walkthrough, package guide, and manifest.
8. Validate exact content, disclosure count, pricing equality, exclusions, source files, PDF structure, tagging, and file integrity.
9. Render every client-facing page to PNG inside `media/client-preview/brochure-review/`.
10. Inspect the cover, screenshots, offer page, mobile readability, density, contrast, hierarchy, and final call to action.
11. Correct every defect and repeat affected checks until no known P6 defect remains.

---

## 5. Plan Critique and Corrections

### 5.1 Risk: the brochure becomes a shortened copy of the 20-page report

**Why this is weak:** A prospect needs the service proposition and evidence of the output, not another research report.

**Correction:** The brochure explains the subscription, demonstrates two output formats, and presents the offer. Detailed sample findings stay in the report and Excel.

### 5.2 Risk: six pages become visually repetitive

**Why this is weak:** Repeating the same product-page ledger layout would make the brochure feel procedural rather than persuasive.

**Correction:** Keep the same typography, palette, logo treatment, and restrained rules, but vary composition by job: editorial cover, paired-output spread, three-signal explanation, sample-proof page, offer page, and start-pilot page.

### 5.3 Risk: screenshots become unreadable decoration

**Why this is weak:** A whole spreadsheet or 20-page contact sheet cannot be read when reduced to a small brochure panel.

**Correction:** Use carefully cropped, high-resolution previews with short captions. The brochure shows the nature of each format; the actual PDF and Excel provide inspection detail.

### 5.4 Risk: invented or unstable commercial wording

**Why this is weak:** Tax treatment, delivery day, delivery channel, and contact wording are business decisions rather than design filler.

**Correction:** Store them in one explicit configuration file and make final generation fail if a required field is unresolved. Never infer tax treatment.

### 5.5 Risk: internal cost assumptions leak into sales material

**Why this is weak:** Provider allowances, internal thresholds, operator-time warnings, and subsidised-pilot economics are operational controls, not client benefits.

**Correction:** Client-facing files show only the ₹1,500 founding service price, cadence, inclusions, exclusions, and pilot terms. Internal costs remain in the project documents.

### 5.6 Risk: the service is presented as software that already exists

**Why this is weak:** The current package proves outputs, not a portal, login, dashboard, real-time monitor, or completed automation.

**Correction:** Use “operator-reviewed weekly research subscription.” The brochure must not show application screens, sign-in language, or claims of full automation.

### 5.7 Risk: sample statistics look like live market proof

**Why this is weak:** The report and Excel are manufactured demonstrations.

**Correction:** Reuse sample figures only on the output-proof page and place the approved disclosure once on that page: “Illustrative preview using sample data; not a live market-research report.”

### 5.8 Risk: the offer appears to guarantee 12 products every week

**Why this is weak:** The service must not pad weak opportunities to hit a quota.

**Correction:** Say “up to 12 eligible opportunities” and state that a smaller list is valid when the evidence does not support more.

### 5.9 Risk: supplier leads or compliance flags are overstated

**Why this is weak:** Leads are investigation starting points, and compliance remains the client's responsibility.

**Correction:** Use “possible supplier leads, subject to evidence availability” and “lightweight client-review flags.” Include the client responsibility boundary on the final brochure page and offer summary.

### 5.10 Risk: pricing appears permanent

**Why this is weak:** ₹1,500 is a founding pilot hypothesis, not a validated long-term tariff.

**Correction:** Present it as a founding-client offer held for the first two paid monthly cycles and reviewed only before a later renewal. Never imply an indefinite lifetime price.

### 5.11 Risk: the sales package asks for an application build on verbal enthusiasm

**Why this is weak:** Compliments or general interest do not justify the seven-day build.

**Correction:** The call to action is written scope-and-price acceptance, preferably first-month payment or another explicit paid-pilot commitment. The build begins only after the commitment, branding, access, and benchmark gates are satisfied.

### 5.12 Risk: too many boxes, dividers, and price badges make the brochure look cheap

**Why this is weak:** The approved direction is elegant and editorial, not a discount flyer.

**Correction:** Use typographic hierarchy, open space, images, short functional rules, and at most one restrained price panel. Avoid card walls, gradients, glossy effects, fake urgency, strike-through prices, countdowns, or discount stickers.

### 5.13 Risk: separate brochure and offer sheet drift apart

**Why this is weak:** A single price, cadence, or exclusion mismatch damages trust.

**Correction:** Generate both from the same commercial configuration and validate every shared field across both PDFs and the package manifest.

### 5.14 Risk: the demo script overwhelms a non-technical client

**Why this is weak:** Walking through every spreadsheet column would hide the business value.

**Correction:** Use an eight-to-ten-minute sequence: problem, weekly deliverables, one report example, one Excel evidence trail, offer, responsibilities, and next step. Detailed evidence is opened only if the client asks.

### 5.15 Risk: contact details are fabricated or left looking broken

**Why this is weak:** An invented email or phone number is unacceptable, while a visible placeholder looks unfinished.

**Correction:** Use an owner-approved contact line or a neutral “Confirm the pilot with NeuraFlow” call to action. Never print placeholder brackets in a final client-facing PDF.

---

## 6. Commercial Configuration Contract

One configuration source must contain:

- service name;
- provider brand;
- founding price: ₹1,500;
- billing: monthly and paid in advance;
- package cadence: four scheduled weekly packages per monthly cycle;
- setup fee: none for the founding client;
- founding-price period: first two paid cycles;
- tax wording: “Plus legally applicable taxes, if any.”;
- normal delivery day: Monday;
- delivery channel: email delivery with a WhatsApp notification and download link;
- contact call to action: owner-approved or neutral;
- implementation target wording: seven implementation days only after commitment, branding, access, and benchmark gates;
- client responsibility summary;
- included and excluded scope.

The generator must reject blank required values, changes to these approved commercial fields, and any attempt to expose internal cost thresholds.

---

## 7. Brochure Page Map

### Page 1 — Cover: weekly clarity from scattered signals

- NeuraFlow logo on cream;
- service name: Product Opportunity Radar;
- concise promise: “A weekly product-research subscription for clearer test decisions”;
- restrained product-research visual drawn from approved P5 imagery;
- no price sticker and no sample-data disclosure because no sample claim is shown yet.

### Page 2 — One subscription, two useful formats

- what arrives each week;
- mobile-friendly portrait decision brief;
- detailed three-sheet Excel evidence workbook;
- four scheduled packages in each monthly cycle;
- cropped previews from the owner-approved PDF and Excel;
- captions explain decision view versus evidence view.

### Page 3 — How opportunities earn a place

- Meta advertising, marketplace visibility, and Google search interest as three independent core signal families;
- deterministic labels based on available evidence;
- quick-commerce as context only;
- operator review before delivery;
- category balance only among eligible products;
- no weak filler and no “winning product” promise.

### Page 4 — What the preview demonstrates

- selected manufactured figures from the approved sample: 12 opportunities, four categories, 26 positive outcomes across 35 available core assessments, and five client-review flags;
- one cropped report opportunity page and one evidence-workbook crop;
- concise explanation of weekly movement, coverage honesty, supplier starting points, and lightweight client-review flags;
- the single approved brochure disclosure appears here and nowhere else.

### Page 5 — Founding-client offer

- ₹1,500 per monthly billing cycle;
- paid in advance;
- four weekly PDF-and-Excel packages;
- no setup fee;
- founding price held for the first two paid cycles;
- clear included and not-included lists;
- exact tax and delivery wording from the commercial configuration.

### Page 6 — Start the pilot with clear responsibilities

- three-step start sequence;
- client decides what to test, checks compliance, verifies suppliers, samples quality, and calculates commercial viability;
- NeuraFlow researches, structures, reviews, and delivers the evidence package;
- implementation begins only after the commercial and access gates;
- calm call to action without fabricated contact details or urgency.

---

## 8. Founding-Offer Summary Contract

The standalone one-page offer summary contains:

- the exact ₹1,500 founding monthly price;
- four scheduled weekly PDF-and-Excel packages;
- monthly advance billing and no setup fee;
- price held for the first two paid cycles;
- tax wording, normal delivery day, and channel;
- up to 12 eligible opportunities per report, with fewer valid when evidence is weaker;
- included deliverables;
- principal exclusions;
- client responsibility and supplier non-verification boundaries;
- the commitment required to begin.

It must not contain internal costs, provider allowances, a fake discount, a testimonial, a guarantee, or a sample-data disclosure when it does not display sample results.

---

## 9. Demo Walkthrough Contract

The internal walkthrough uses this sequence:

1. **Set the context — 60 seconds:** explain the scattered-research problem and the operator-reviewed weekly service.
2. **Show the brochure — 90 seconds:** establish what arrives and why the two formats serve different jobs.
3. **Open the sample PDF — 3 minutes:** show the weekly overview, one strong opportunity, one lower-confidence opportunity, supplier digest, and limitations page.
4. **Open the Excel — 2 minutes:** trace the same product from Shortlist to Evidence to Supplier Leads.
5. **Show the offer summary — 60 seconds:** state cadence, price, scope, and exclusions without improvisation.
6. **Ask decision questions — 90 seconds:** confirm usefulness, preferred start timing, delivery arrangement, and willingness to enter a paid pilot.

The guide must include plain answers to likely objections:

- “Are these guaranteed winning products?”
- “Are the suppliers verified?”
- “Will I get exactly 12 every week?”
- “Does this include a dashboard or login?”
- “Can I request unlimited product searches?”
- “Who handles compliance and margins?”
- “Why are there both a PDF and Excel?”

The guide must tell the operator not to expose internal costs, source credentials, system notes, manifests, validation logs, or production plans.

---

## 10. Visual System

### 10.1 Continuity

- preserve the original maroon NeuraFlow logo on cream;
- retain the P5 cream, maroon, muted navy, pale sage, pale rose, and muted mustard family;
- retain condensed navy display type, readable sans-serif labels, and restrained serif narrative copy;
- use one short folio rule where folios appear;
- reuse approved product and report imagery without altering product identity or evidence meaning.

### 10.2 Brochure-specific evolution

- slightly larger type and shorter paragraphs than the report;
- more editorial image-led composition;
- fewer tables and fewer ledger rules;
- one restrained price field rather than multiple pricing cards;
- functional screenshots with enough size to communicate format;
- no device mockups that make the files look like an existing software interface.

### 10.3 Accessibility

- A4 portrait;
- true selectable text;
- descriptive links only when a real client action exists;
- no meaning communicated by colour alone;
- minimum practical body size for phone reading;
- sufficient contrast on all text and logos;
- tagged PDF where supported by the proven Chrome renderer;
- no text baked into generated imagery.

---

## 11. File Architecture

```text
preview/brochure/
  commercial-config.json
  brochure.html
  brochure.css
  offer-summary.html
  offer-summary.css
  client-preview-package-manifest.json

outputs/client-preview/
  NeuraFlow_Product_Opportunity_Radar_Founding_Pilot_Brochure_2026-08-15.pdf
  NeuraFlow_Founding_Client_Offer_2026-08-15.pdf
docs/client-preview/guides/
  NeuraFlow_Client_Demo_Walkthrough_2026-08-15.md
  NeuraFlow_Client_Preview_Package_Guide_2026-08-15.md

media/client-preview/brochure-review/
  brochure-pages/
  offer-pages/
  00-brochure-overview.png
  01-brochure-detail.png
  02-offer-summary.png
```

Reusable images remain in their existing `media` folders. P6 must not create duplicate source-image copies in `outputs`.

---

## 12. Automated Validation Contract

The validator must confirm:

- P4 Excel and P5 PDF still pass their existing validation;
- commercial configuration is complete and uses ₹1,500 exactly;
- brochure has exactly six A4 portrait pages;
- offer summary has exactly one A4 portrait page;
- required text is extractable from every page;
- brochure and offer summary share exact price, cadence, billing, tax, delivery, pilot-period, inclusion, and exclusion meaning;
- the approved sample disclosure appears exactly once in the brochure and not in the offer summary;
- sample figures reconcile to the manufactured dataset;
- “up to 12” appears and no guaranteed-count wording exists;
- operator review, supplier non-verification, and client responsibility are present;
- prohibited claims and internal costs are absent;
- no page implies a portal, login, real-time service, unlimited requests, verified suppliers, or guaranteed outcomes;
- source screenshots and logo hashes match their approved inputs;
- all PDFs are unencrypted and contain no JavaScript, forms, attachments, or unsafe file links;
- a package manifest lists every client-shareable and internal-support artifact with correct size and hash;
- final PDFs remain below 10 MB each.

---

## 13. Visual Review Passes

1. **Cover pass:** brand contrast, proposition clarity, visual balance, and absence of flyer-like sales devices.
2. **Output-proof pass:** screenshots are sharp, meaningful, correctly cropped, and not pretending to be application screens.
3. **Method pass:** the three core signals remain distinct and quick-commerce remains context.
4. **Sample-proof pass:** figures are accurate and the single disclosure is visible but discreet.
5. **Offer pass:** price, cadence, terms, inclusions, exclusions, and tax wording are immediately understandable.
6. **Responsibility pass:** supplier and compliance boundaries are firm without turning the brochure into legal text.
7. **Mobile pass:** every page remains easy to scan at realistic phone width.
8. **Continuity pass:** brochure, offer summary, P5 PDF, P4 Excel, and brand assets feel like one package.
9. **Sceptical-client pass:** remove vague promises, hidden conditions, inflated language, and unanswered commercial questions.
10. **Final recursive pass:** correct and repeat every affected test until no known P6 defect remains.

---

## 14. Exit Criteria

P6 is internally complete only when:

- all four new deliverables and the package manifest exist;
- the brochure has six coherent portrait pages;
- the offer summary has one coherent portrait page;
- all commercial fields are owner-approved and exact;
- every promise is demonstrated by P4/P5 or explicitly described as a future implementation step after commitment;
- internal costs and technical operating details are absent from client-facing files;
- the disclosure appears once in the correct brochure context;
- all client-facing pages have been rendered and visually inspected;
- all automated checks pass after a clean regeneration;
- no known logical, commercial, content, visual, accessibility, file-integrity, or documentation defect remains within P6 scope.

The remaining external gate after P6 is the prospective client's commercial response. Application work still does not begin without written scope-and-price acceptance and preferably payment or another explicit paid-pilot commitment.

---

## 15. Superseded Baseline Record

P6 completed its internal production and review cycle on 16 August 2026.

- The six-page founding-pilot brochure, one-page founding-offer summary, operator demo walkthrough, package guide, and final package manifest were generated from the approved product documents, commercial configuration, P4 workbook, and P5 report.
- The commercial configuration freezes ₹1,500 per monthly billing cycle plus legally applicable taxes, if any; Monday delivery; and email delivery with a WhatsApp notification and download link.
- Both client-facing PDFs use selectable text, A4 portrait pages, approved NeuraFlow branding, and the same local Chrome renderer proven in P5.
- The brochure uses the approved sample disclosure exactly once on its output-proof page. The standalone offer summary does not repeat it because that artifact shows no sample findings.
- The brochure, offer summary, P5 report, P4 workbook, walkthrough, and guide are recorded with roles, sizes, hashes, and shareability in the package manifest.
- The unchanged P4/P5 package passed its 1,529 checks; the new P6 package passed 299 checks, for 1,828 passing checks in the final combined cycle.
- All six brochure pages and the offer-summary page were rendered to review images and inspected for hierarchy, screenshot crop, contrast, legibility, density, disclosure placement, commercial accuracy, client responsibility, clipping, overlap, and visual continuity.
- No known logical, commercial, content, visual, accessibility, file-integrity, or documentation defect remains within P6 scope.

The first P6 PDFs were superseded after owner feedback and moved to `outputs/client-preview/archive/p6-v1/`. Do not send them to the client. Use the revised artifacts and review record named in the [P6 revision plan](session_p6_brochure_revision_plan.md).
