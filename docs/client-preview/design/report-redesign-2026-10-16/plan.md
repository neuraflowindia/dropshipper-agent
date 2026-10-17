# Client report redesign — plan and specification (16 October 2026)

**Project stage:** Development and testing (confirmed 13 October 2026, `docs/project/pj-brief.md`). No client has received a live report, so the new layout simply replaces the old one in place: no side-by-side versions, no migration of old report stages.

**Status of this document:** design direction and mock-up for the owner to approve. **Nothing in production code, configuration or tests has been changed.** The production build starts only after the owner approves the direction and answers the open choices in section 14.

**What is in this folder**

**17 October 2026:** only `plan.md` is still here. The mock-ups, `renders/` and `source/` listed below (and every later mention of them in this plan) were removed from the working folder and are kept in git history at commit `02c8326`, for example `git show 02c8326:docs/client-preview/design/report-redesign-2026-10-16/mockup-en.html`. The built report engine lives on the `r10` branch.

| File | What it is |
|---|---|
| `plan.md` | This plan: direction, exact specifications, build approach, owner choices |
| `mockup-en.html` / `mockup-en.pdf` | Full English mock-up, 10 A4 pages: cover, at a glance, how to read, three products (two pages each), method and notes |
| `mockup-hi-Latn.html` / `mockup-hi-Latn.pdf` | Roman Hinglish mock-up, 4 pages: cover, at a glance, one product (the longest-text case: compliance flag + updated) |
| `renders/` | 42 PNG previews: every A4 page (`*-a4-page-NN.png`), the HTML at 390 px phone width (`*-phone-390px-part-NN.png`), the A4 PDF as a phone shows it fitted to width and at 2× zoom (`*-pdf-on-phone-*.png`), and a greyscale check (`en-greyscale-check-pages-02-09.png`) |
| `source/` | Everything needed to rebuild the mock-up: `sample_data.py`, `copy_text.py`, `components.py` (all charts), `build_mockup.py` (pages + stylesheet), `render_previews.py` (PDF + PNG), `assets/fonts` (9 subset font files + both OFL licences), `assets/images` (NeuraFlow’s own illustrative still lifes from the August preview + logo) |

Rebuild: `python source/build_mockup.py && python source/render_previews.py` with the project venv (PyMuPDF, Pillow, Playwright).

**All figures, supplier names and listings in the mock-up are invented sample data**, clearly marked on every page ("Sample data · design mock-up"). The numbers obey the real scoring rules (`config/research/opportunity_score.json`), so the visuals show believable combinations. Images are NeuraFlow-owned illustrative still lifes, not product photos.

---

## 1. The direction in plain words

The current report is a plain list of text and tables. The new one reads like a short magazine about products:

1. **A cover that sells the issue**: the date, a one-line summary, four big numbers (products, new, updated, sweet spots) and a photo grid of the top six products, each with its score and label.
2. **"This issue at a glance"**: one page that ranks every product. Each row shows a small four-part score bar, the label, a three-step crowding meter and the checklist count, with small charts for labels, crowding and categories.
3. **"How to read a product page"**: one page that explains every symbol once, in plain words.
4. **Every product gets exactly two pages.**
   - **Page one, the product:** photo, a score ring out of 100, how the score adds up, why it made the list, and the 10-point checklist with symbols.
   - **Page two, the market and sourcing:** a crowding gauge, a timeline of when advertisers started, a 12-month Google search line, platform and quick-commerce grids, prices seen, example listings, a supplier comparison table, and dated sources.
5. **A closing page**: how each issue is made (four steps), the sites checked, and the important notes (no profit or RTO predictions, suppliers are leads only).

The brand stays: cream paper, navy, maroon, a serif voice. It gains open-licence fonts embedded in the file, real charts drawn as vector graphics, and one consistent symbol language. Nothing new is claimed: every number and word on the page comes from facts the system already records.

---

## 2. Engine, constraints and how the mock-up proves them

| Item | Finding | Consequence for the design |
|---|---|---|
| PDF engine | `scripts/render_live_pdf.py` prints the HTML with **native Windows Chrome (v155) headless** via PowerShell, flags `--headless=new --print-to-pdf --no-pdf-header-footer --virtual-time-budget=2500 …` | The mock-up was printed with the same Chrome and the same flags (`source/render_previews.py`). What you see in `renders/` is what production would print. |
| Forbidden HTML | The renderer refuses any `<script>`, `<iframe>`, `<object>`, `<embed>` | No scripts at all. Every chart is inline SVG; no hover tooltips (print has none anyway). |
| PDF checks | Production (`inspect_pdf`, `delivery_package.py`) requires portrait pages with extractable text on every page | Verified: both mock-up PDFs pass the same checks; all chart labels are real text. |
| Fonts | Windows has Georgia, Segoe UI, Sitka, Bahnschrift but **not Aptos** (the current CSS silently falls back to Segoe UI). Clients' phones have none of these. | Embed open-licence fonts as data in the HTML; Chrome embeds small subsets in the PDF. Proven: PDF fonts are `IBMPlexSans` and `SourceSerif4` subsets. |
| Running footers | Chrome 131+ prints CSS `@page` margin boxes | Footer "NeuraFlow · Product Opportunity Brief · date" and "Page n of N" on every page except the cover, which is full-bleed through a named page (`@page cover {margin:0}`). |
| Page colour | Chrome does not paint the document background into page margins; it does honour `@page {background}` | Cream margins come from `@page {background:#F9F3EC}`. |
| Chrome quirk | An outer box-shadow "ring" on the first element of a page bleeds 0.7 pt onto the bottom of the previous page | Rings are drawn as inner borders; checked: no drawing crosses a page bottom. |
| Page numbers inside the content | Chrome cannot print "see page N" (no `target-counter`) | The dashboard links titles to their pages (clickable in the PDF) instead of printing page numbers. |
| Text extraction | Chrome may print variable fonts as unreadable outlines | Fonts are cut to **static** weights (no variable axes). |

---

## 3. Design system: colour

All colours are hex tokens; the brand base is unchanged from the approved P5 preview.

| Token | Hex | Use |
|---|---|---|
| `--paper` | `#F9F3EC` | Page (incl. margins via `@page`) |
| `--paper-deep` | `#EEE5DB` | Empty photo plate, note boxes, Trends "previous 4 weeks" band |
| `--card` | `#FFFCF7` | Tiles, supplier cards (phone), site chips — lifted surface |
| `--plate` | `#FFFFFF` | Photo plate (marketplace photos have white backgrounds) |
| `--line` / `--line-soft` | `#D8D0C8` / `#E7DFD6` | Rules / hairlines and tile borders (0.25–0.3 mm) |
| `--ink` | `#292C2F` | Body text (12.7:1 on paper) |
| `--ink-2` | `#5F6062` | Secondary text, captions (5.7:1 on paper; 5.1:1 on paper-deep) |
| `--navy` | `#24384B` | Headings, score fill, "Meets", data marks (10.9:1) |
| `--navy-2` | `#647487` | "Unclear" half-dot, role chips, empty-photo icon |
| `--track` | `#DCE3EA` | Unearned score (meter tracks) |
| `--maroon` | `#6B1F2B` | Accent: ranks, kickers, recency, compliance, links (10.3:1) |
| `--maroon-pale` | `#F1DFE2` | Compliance bar, bulk-quote note, Trends "recent 4 weeks" band, last-30-days band |
| Label: Sweet spot | glyph `#2A6CA3`, chip `#E3ECF5` | Target glyph ◎ |
| Label: Early bet | glyph `#B07A12`, chip `#F3E7C9` | Up-arrow glyph |
| Label: Proven but crowded | glyph `#9A2F3E`, chip `#F1DFE2` | Three-bars glyph |
| Crowding Low / Medium / High | `#C99AA2` / `#9C4E5B` / `#6B1F2B`, unfilled `#E9E0D7` | One-hue ordinal ramp (light → dark = more crowded) |
| Grey symbol | `#8E8F91` | "Does not meet", "No", "Not stated" rings |

**Validation (dataviz skill validator, run, not eyeballed):** the three label hues pass all categorical checks on the cream surface with all-pairs testing (lightness band, chroma ≥ 0.10, worst colour-blind pair ΔE 16.1, normal-vision floor 20.1, contrast ≥ 3:1). The first green/red attempt failed colour-blind separation (ΔE 4.8) and was replaced by blue. The crowding ramp passes the ordinal checks (monotone lightness, step gaps, light end 2.21:1). Every text/surface pair above clears WCAG 4.5:1.

**Greyscale rule:** meaning never rides on colour alone. Labels carry distinct glyph shapes; checklist and term states are filled tick / crossed ring / half-filled / dashed ring; crowding is a lightness ramp plus a needle plus a word; recent advertisers are a ringed dot, not just a maroon dot. Checked in `renders/en-greyscale-check-pages-02-09.png`.

---

## 4. Design system: type

**Families (2, per the typography rule "max two fonts"):**
- **Serif — Source Serif 4** (Adobe, SIL Open Font Licence): titles and reading text. Static cuts: Text 400 and 600 (optical size 11), Text Italic 400, Head 600 (optical size 24).
- **Sans — IBM Plex Sans** (IBM, SIL OFL): labels, numbers, tables. Static cuts: 400, 500, 600 at normal width, plus **Plex Sans Condensed (85 % width) 500 and 600** for kickers, ranks and column heads. The condensed cut keeps the brand's narrow-heading feel and saves room for longer Hinglish labels.
- Both include the rupee sign ₹. Files are subset to Latin + ₹ + arrows/marks: 9 files, 200 KB in total (about 270 KB once embedded; Chrome then embeds small subsets in each PDF).
- Fallbacks if a font ever fails: Georgia / Segoe UI / Arial Narrow.

**Type scale** (A4 in points; phone HTML in pixels). Adjacent working levels differ by 0.5–1.5 pt; the only larger steps are numbers and titles that never sit next to body text.

| Role | A4 | Phone | Family / weight / line height | Where |
|---|---|---|---|---|
| micro | 8 pt | 12 px | Sans 400, 1.35–1.45 | Sources, captions, axis ticks, footers, tile sources. **Nothing smaller anywhere** (checked in both PDFs) |
| kicker | 8.5 pt | 12 px | Condensed 600, caps, +0.08 em | Kickers, column heads, note labels |
| label | 9 pt | 13 px | Sans 400 / 600 | Checklist reasons, tile text, chips, table cells |
| strong | 9.5 pt | 14 px | Sans 600 | Criterion names, module titles, product names in tables |
| note | 10 pt | 14.5 px | Serif 400 / italic | Reason points, "Keep in mind" |
| body | 10.5 pt | 15 px | Serif 400, 1.47 | "Why this product" paragraph |
| lead | 11.5 pt | 16 px | Serif 400, 1.45–1.5 | Deks on cover / dashboard / key |
| module | 12.5 pt | 17 px | Serif Head 600 / Sans 600 | Page-two title strip, price figures, step titles |
| section | 14 pt | 19 px | Serif Head 600, 1.2 | "Why this product", "Product fit" |
| stat | 15.5 pt | 21 px | Sans 600, 1.1 | Tile figures ("High", "7 advertisers") |
| title | 18 pt | 23 px | Serif Head 600, 1.15, balanced wrap | Product and page titles |
| kpi | 20 pt | 26 px | Sans 600 / Condensed 600 | KPI numbers, rank badge |
| display | 28 pt | 30 px | Serif Head 600, 1.08 | Cover title only |
| score figure | 22 pt in a 30 mm ring | 26 px | Sans 600 | One per product page (declared exception) |

Rules applied: sentence-case titles, with caps only on short letter-spaced kickers (the typography rule wins over the old uppercase headings); curly quotes and real en dashes; tabular figures only in aligned columns; prose capped at about 168 mm (90–95 characters per line); no bold-plus-italic.

---

## 5. Grid, margins and print rules

- **A4 portrait, `@page` margins 15 / 16 / 17 / 16 mm** (top / right / bottom / left), giving a **178 × 265 mm** content area. All text stays at least 15 mm from any edge, so it is safe for printing and trimming. The cover is full-bleed (`@page cover {margin:0}`) with 15–16 mm inner padding.
- **Columns:** halves (86.5 mm, 5 mm gap), thirds (≈ 57 mm, 3.2 mm gap), quarters (≈ 41 mm, 5 mm gap). Page-one header: 50 mm photo | flexible title column | 30 mm score ring, with 6 mm gaps.
- **Rhythm:** 1.2–2 mm inside modules; 3.2–4.5 mm between modules; hairline rules (0.25–0.3 mm) instead of boxes wherever spacing alone is not enough. Tiles get a 0.25 mm border and 2 mm radius; no heavy shadows (they also misprint, see section 2).
- **Page breaks:** every page section forces a break after itself, so each product's page one and page two always start on fresh pages. Headings never end a page (`break-after: avoid`). Tiles, checklist items, table rows, supplier rows, the price/example block and sources never split (`break-inside: avoid`). If real text ever exceeds a page, the overflow moves whole items to an extra page; nothing is clipped or hidden (`overflow` stays visible).
- **Colour printing:** `print-color-adjust: exact` everywhere; all fills are flat colour (no gradients, no transparency except the 10 % Trends area wash).
- **Links:** product titles on the dashboard and cover link to the product page; every source is a live link in maroon with a hairline underline.
- **Sample marker (mock-up only):** a top-right margin box "SAMPLE DATA · DESIGN MOCK-UP". Production shows it only for synthetic issues (replacing the old notice line).

---

## 6. Cover

**Design intent.** The first thing the client sees on a phone or laptop, three times a week. Its job: feel like a premium, trustworthy research product worth opening ("there are good things to test in here"), show at a glance how big the issue is, and preview the top products by picture. Feel: calm, editorial, confident; never salesy, no hype words.

**Implementation specs**
- Anatomy, top to bottom:
  - logo 42 mm wide (maroon wordmark), with the issue date at right (condensed caps 8.5 pt maroon, e.g. "TUESDAY 20 OCTOBER 2026");
  - 16 mm gap, then kicker "INDIA PRODUCT OPPORTUNITY BRIEF";
  - display title (the AI report title, ≤ 100 characters, so 1–2 lines at 28 pt; max width 156 mm);
  - dek in serif 11.5 pt: the code-owned count sentence plus research window;
  - KPI band with 4 equal columns between hairlines: products / new / updated / sweet spots (20 pt figures, 9 pt labels);
  - **photo mosaic** of the top six products in a 3 × 2 grid, each a 50 mm high white plate plus caption (rank in condensed maroon, title 9 pt semibold, score + small label chip);
  - full-width navy foot band: "Research suggestions to test, not a promise of sales…" (9 pt cream text).
- States:
  - **No photo:** a paper-deep tile with the category's line icon (Home and Kitchen pot, Home and Decor lamp, Baby Products bottle, Travel and Outdoor case) and "No product photo in this issue".
  - **Fewer than six products:** the grid keeps 3 columns and shows only what exists.
  - **Long titles:** wrap to 2 lines; the caption grows (the mosaic has about 25 mm spare).
- Photos are **never cropped** (`object-fit: contain` on white): marketplace photos keep their whole product.

---

## 7. "This issue at a glance" (dashboard)

**Design intent.** The decision page. The reader's job is to answer "which three or four do I look at first, and why?" in under a minute. The table ranks; the small charts show the shape of the issue (how many sweet spots, how crowded, which categories). Feel: a clean data spread, like a good newspaper league table.

**Implementation specs**
- Header: kicker "THIS ISSUE · date", title 18 pt, dek 11.5 pt (code-owned sentence explaining the ranking rule: crowded types listed last).
- KPI band, 4 columns: product types / new / updated / **need a compliance check** (counted from the flags).
- Three mini charts in a third-width grid:
  - **Labels:** a 100 % stacked bar, 52 × 4 mm, in the three label hues with 0.5 mm gaps, plus a legend with glyph, name and count.
  - **Amazon & Flipkart crowding:** the same stacked bar in the ordinal ramp, legend with count.
  - **Categories:** single-hue navy bars, one per category, sorted by count, 2.6 mm thick, longest 18 mm, count at the tip. One hue on purpose: categories have no order, so colour would only repeat what length already shows.
- **League table** (`table-layout: fixed`). Columns: # 8 mm | product type (auto ≈ 62 mm) | opportunity score 38 mm | label 36 mm | crowding 23 mm | fit 11 mm.
  - **Product type cell:** title in 9.5 pt semibold; a compliance shield glued to the last word, so it never sits alone on a line; then "category · New/Updated" at 8 pt.
  - **Score cell:** a **compartment bar** 30 × 2.6 mm on a 0–100 axis, split into Want 0–20, Early 20–50, Room 50–75 and Product 75–100. Each compartment is filled navy in proportion to its points, with a pale track for the rest and 0.5 mm gaps. The score number follows in 9.5 pt.
  - **Label cell:** a small chip (glyph + name, 8 pt), allowed to wrap to 2 lines for Hinglish ("Bikta hai, par bheed zyada").
  - **Crowding cell:** three 2.3 mm squares filled up to the level in the ramp, plus the word.
  - **Fit cell:** "8/10".
  - Rows have 1.5 mm vertical padding and hairline separators, and never split.
  - **Group divider:** before the first High-crowding row, a maroon kicker "CROWDED ON AMAZON AND FLIPKART · LISTED LAST", matching the ranking rule in `opportunity_sort_key`.
- Key line under the table (8 pt): the part order and what navy and the shield mean.
- **Capacity:** 12 rows fit with all mini charts (21 pt spare). For **13–15 rows** use the dense variant:
  - mini-chart legends set inline (saves about 14 mm);
  - row padding 1.1 mm (saves about 12 mm);
  - the dek trimmed to one line.
  If more than 15 ever occur, the table continues on a second page with its header repeated (`thead` repeats in Chrome).
- Phone (HTML): KPIs in 2 × 2; charts stacked; each table row becomes a three-line card (rank | title; score bar + fit; label + crowding).

---

## 8. "How to read a product page" (key)

**Design intent.** A one-page legend the client can come back to, so that no symbol needs explaining twice. It also sets expectations honestly. Feel: a friendly field guide with real miniature versions of every visual, not a wall of text.

**Implementation specs**
- Dek: "Every product gets two pages: the product and its score first, then the market and sourcing."
- **Full-width score block:**
  - a sample ring (30 mm) with a plain explanation;
  - four quarter-width columns (navy 0.6 mm top rule) for Want (up to 20), Early (up to 30), Room (up to 25) and Product (up to 25), each with one sentence on what earns points. These are taken from the real point rules: Room Low 25 / Medium 12 / High 0; Product 5 points met = 10, +3 per extra point.
- **Two columns below.**
  - Left:
    - the three label chips with meanings;
    - the crowding gauge with how it is decided;
    - the compliance chip and when it appears.
  - Right:
    - the checklist symbols, the "estimated" tag, and the fit pips (Good fit 6–10, Decent 5);
    - the market-signal symbols (navy dot / ringed maroon dot, sand / rose Trends windows, platform role chip, quick-commerce ticks);
    - the supplier-term symbols (yes / no / not stated).
- One paper-deep line at the bottom: "This report does not predict sales, profit, margin or return-to-origin (RTO) rates. Suppliers are leads, not endorsements. Full notes are on the last page."
- The mock-up key is English only. Production needs the Hinglish key (same layout; about 15 % more text fits, since the page has 25 pt spare).

---

## 9. Product page one — "the product"

**Design intent.** In about 20 seconds the reader learns what the product is, how strong the opportunity is, and why it made the list. Then, if interested, they check the product against the 10-point checklist. Feel: an editorial product feature. The photo and score ring do the selling; the words stay plain and honest.

**Implementation specs (top to bottom)**

1. **Header** (grid 50 mm | flexible | 30 mm, bottom hairline):
   - **Photo plate:** 50 × 50 mm, white, 0.25 mm border, 2 mm radius, 2.5 mm padding, photo contained, never cropped.
     - **Rank badge:** a maroon 10 × 8 mm tab in the top-left corner with the 12.5 pt white condensed numeral.
     - **Photo note:** sits inside the plate bottom-right on a white veil (8 pt). Production shows it only when the photo is "of a similar product type" (`image_provenance.relationship == "relevant_family"`).
     - **No photo:** paper-deep plate with a 15 mm category line icon, "No product photo in this issue", and "Example listings are on the next page."
   - **Title column:**
     - kicker "CATEGORY · NEW THIS ISSUE / UPDATED THIS ISSUE";
     - title 18 pt (balanced, up to 3 lines);
     - chips: the label chip and the "Good fit" / "Decent fit" outline chip;
     - label meaning (glyph + one code-owned sentence; the label name isn't repeated);
     - **"What changed"** (Updated products only): a 2.6 mm maroon left rule, "What changed:" in maroon, then the code-checked update sentence (≤ 250 characters).
   - **Score ring:**
     - 30 mm, ring 3.3 mm thick, split into four arcs proportional to each part's maximum (68.8°, 103.2°, 86° and 86° for Want, Early, Room, Product) with 4° gaps, starting at 12 o'clock and running clockwise;
     - each arc's earned share in navy, the rest in the track colour;
     - centre: the score at 22 pt with "out of 100" at 8 pt; caption "Opportunity score" (9 pt semibold navy) below.
2. **Compliance bar** (only when flagged): full width, maroon-pale fill, 1 mm maroon left border, shield icon, one paragraph: "**Client compliance check required.** + reason" (9 pt; maroon semibold lead-in). It is placed full-width under the header, not in the title column, because Hinglish reasons are long. The duplicate chip was dropped.
3. **"How the score adds up"** in four quarter columns:
   - each column: the part name (9.5 pt semibold), "17/20" right-aligned, then a 38 mm meter (track = maximum, navy = points, 2.2 mm, rounded ends);
   - under each meter, the code-owned sub-points at 8 pt with bold numbers:
     - Want: "Facebook ads 8 · Google searches 5 · Platform lists 4 · Quick commerce 0";
     - Early: "Rising searches 8 · New advertisers 3 · New arrivals 6";
     - Room: "Crowding level: Low";
     - Product: "Meets 8 · Unclear 1 · Does not meet 1".
   - All values come straight from `opportunity_basis.parts[*].items[*].points`; nothing is recomputed in the template.
4. **"Why this product"** (section 14 pt):
   - the AI reason paragraph in serif 10.5 pt;
   - then up to 3 numbered points (serif 10 pt, maroon condensed numerals);
   - then "KEEP IN MIND" with the limitation in serif italic 10 pt above a hairline.
5. **"Product fit"** (section 14 pt):
   - right-aligned summary: 10 fit pips in checklist order (filled = Meets, half = Unclear, ring = Does not meet) + "**Good fit** · 8 of 10 checklist points met";
   - then a two-column list (CSS columns, 6.5 mm gap), one item per criterion in the fixed order 1, 2, 5, 6, 7, 8, 9, 11, 12, 13:
     - 3.8 mm state symbol;
     - criterion name (production names from `CRITERIA_BY_ID`, 9.5 pt semibold);
     - the state word **only for exceptions** ("Unclear", "Does not meet" in maroon 9 pt). "Meets" is carried by the filled tick and kept as hidden text for screen readers and text extraction;
     - the "estimated" tag (8 pt navy-2 outline) when `basis == "inferred"`;
     - the reason (9 pt).

**Measured fit (Windows Chrome):**
- English page ones have 29.5 mm spare (plain product), 29.2 mm (no photo, high crowding) and 16.5 mm (compliance + updated).
- The Hinglish compliance + updated page fits with only **2.7 mm** spare. That page drives the text budgets in section 11.

---

## 10. Product page two — "the market and sourcing"

**Design intent.** The evidence behind the score, laid out so the client can decide whether to test and where to buy, without reading tables. Every tile answers one question:
- How crowded is Amazon and Flipkart?
- Are advertisers already in, and recently?
- Are searches rising?
- Do dropshipping platforms carry it?
- Is it on quick-commerce shelves?
- What do things cost?
- Who could supply it?

Feel: an infographic evidence board. Dense but calm, dated and sourced.

**Implementation specs (top to bottom)**

1. **Title strip:** rank (condensed 12.5 pt maroon), title (serif 12.5 pt), "MARKET AND SOURCING" kicker inline, label chip, score "33/100" right; hairline below. It keeps the product identifiable even if the page is read alone.
2. **Row of two tiles** (halves):
   - **Amazon & Flipkart crowding.**
     - Left: a half-ring **gauge** 30 mm wide with three 60° segments (ramp colours filled up to the level, unfilled `#E9E0D7`), a needle to the level's centre, and Low / High at the ends (8 pt). Under it, the level word (stat 15.5 pt) and "Room: 12 of 25 points".
     - Right, after a hairline: one-line facts from `crowding_text` counts, e.g. "Similar listings: **Amazon 11 · Flipkart 7**", "Brands or sellers: **9** (n show no brand)", "Most ratings on one listing: **1,846**" (Indian digit grouping), "On a bestseller list: **1**".
   - **Meta ads (Facebook & Instagram).**
     - Stat line: "**7** advertisers", then "19 relevant ads · 12 still active".
     - **Advertiser timeline** (76 × 15 mm): a 12-month axis ending on the observation date, with month ticks and labels at Jan / Apr / Jul / Oct (8 pt) and the last 30 days shaded maroon-pale.
     - **One dot per advertiser** at the start date of its earliest relevant ad: navy 2.5 mm dot for older starts, a **ringed maroon dot** for starts within 30 days (the same definition as `recent_start_advertisers`). Close dots stack upwards (2.7 mm collision rule).
     - Legend under the timeline.
3. **Row of three tiles** (thirds):
   - **Google searches in India.**
     - State line: "▲ Rising", "▲ Rising strongly" or "— Not rising" (the same tests as the score: positive; strong = recent mean ≥ 1.5 × previous, or breakout).
     - **Sparkline** (48 × 18.5 mm): 52 weekly points on a 0–100 relative scale, a 2 px navy line with a 10 % navy area wash, a maroon end dot, and "Oct 2025 / Oct 2026" ends. The **previous four weeks are shaded sand and the recent four rose**: exactly the windows the score compares.
     - Caption: "Last 4 weeks 91–96, the 4 before 63–68. Relative interest out of 100, not sales."
   - **Dropshipping platforms:**
     - one row each for DeoDap, BaapStore and Dropdash: name (semibold), state symbol and word (Listed / Not found / Not checked), dropship price right-aligned (₹, tabular);
     - role chips on a second line (Bestseller, Trending, High demand, Winning, New arrival; outline chips 8 pt);
     - note "₹ = dropship price".
   - **Quick commerce:**
     - a 3 × 3 matrix of Blinkit / Zepto / Instamart against the cities checked: filled tick = in stock, crossed ring = not in stock or not listed, dashed ring = not checked;
     - summary "In stock in k of n cities checked" (semibold);
     - one-line legend.
4. **Prices and examples**, a 2 : 1 block:
   - **"Prices seen"**, two columns: retail listings and supplier/dropship quotes. Each row: ₹ figure (12.5 pt navy), what the price covers ("pack of 4", "per piece, minimum order 100 pieces"), and source · seen date (8 pt). Under both, the **bulk-quote note** (maroon left rule, pale fill), from `_bulk_order_notice`.
   - **"Example listings"**, right third: up to 3 rows of title (semibold) + quote, then "source · seller · pack · seen date", with the standing caveat "Quotes are as shown on each listing; unit and price basis not verified."
   - **Retail and supplier prices are deliberately not drawn on one shared price line.** A shared line would invite a margin reading, which the service must not imply.
5. **Supplier leads comparison table** (replaces the old paragraphs). Columns:
   - supplier lead (50 mm: name, then "city · via platform"),
   - minimum order,
   - single-piece dispatch,
   - cash on delivery,
   - plain packaging,
   - private label,
   - subscription or fees.

   Each term cell shows the state symbol and word. Production maps `terms[...]` values with their basis: observed yes → Yes, observed no → No, not_stated / unclear → "Not stated", **conflicting → "Conflicting" (dashed ring)**. Footer: "Leads to look into, not endorsements. NeuraFlow has not contacted or checked these suppliers." On phones each row becomes a card with label: value lines.
6. **"Sources and dates seen"**: inline list of maroon links with dates (8 pt, wraps to 2–3 lines).

**Measured fit:**
- English, 3 suppliers and 10 sources: 1.5 mm spare.
- English, 1–2 suppliers: 17.9–26.9 mm spare.
- Hinglish, 2 suppliers: 8.7 mm spare.

Five suppliers would push sources and part of the table to a third page; that is allowed (rows never split). The plan's rule is "two pages in the normal case, a third page only when the data is genuinely long".

---

## 11. States, edge cases and text budgets

| Situation | What the page shows |
|---|---|
| No photo | Paper-deep plate, category icon, "No product photo in this issue / Example listings are on the next page" (cover tile: icon + first line) |
| Photo of a similar type | Small note inside the plate: "Photo of a similar product type" (existing production wording) |
| 2–3 photos | Page one keeps one hero (photo 1). Photos 2–3 appear as two 24 mm square thumbnails at the top of the "Example listings" column on page two (owner choice 6) |
| High crowding | Gauge needle in the dark segment, Room 0/25 visible as an empty compartment, crimson three-bars chip "Proven but crowded", dashboard group divider |
| Compliance flag | Full-width compliance bar on page one; shield beside the title on the dashboard; KPI count |
| Updated | "UPDATED THIS ISSUE" kicker + "What changed" line in the header; dashboard "· Updated" |
| Estimated checklist answer | "estimated" outline tag after the criterion name |
| Meta: no relevant ads | Stat "0 advertisers", empty axis with the 30-day band, line "No relevant ads found in the saved sample" |
| Meta: ads without start dates | Dots only for dated advertisers; note "n advertisers had no start date" (from `ads_without_start_date`) |
| Trends unavailable | State line "No usable Google Trends data this time", an empty framed chart area, no invented line |
| Platform / quick commerce not checked | Dashed-ring symbol + "Not checked" (never shown as "not found") |
| No price / no supplier / no example | The existing production sentences ("Price not stated", "Supplier search could not be completed", "No reviewed product example…") in the same position |
| Scoped suppliers (offer-level terms) | Supplier row spans the term columns with "Terms differ by offer" and lists offers beneath (from `scoped_supplier_lines`) |
| "Too quiet" / crowding "Not checked" | Cannot reach a report (only `opportunity_scored` products are eligible). The renderer should **fail closed** if it ever sees one |

**Hinglish tolerance.** Roman Hinglish runs about 15–25 % longer. All labels allow wrapping (chips wrap in the table; kickers wrap; columns use `minmax`). The binding constraint is page one of a product that is both compliance-flagged and updated. To keep products at two pages, set these **writing budgets** for the AI narration, in characters (production limits today are reason 450, points 3 × 220, limitation 1,000, update 250, checklist phrase 220):

| Field | English | Hinglish | Hardest mocked case (Hinglish, compliance + updated) |
|---|---|---|---|
| Reason paragraph | ≤ 320 | ≤ 360 | 329 |
| Each point (max 3) | ≤ 90 | ≤ 105 | 61 / 57 / 100 |
| Limitation ("Keep in mind") | ≤ 160 | ≤ 185 | 173 |
| Update sentence (sits in the header) | ≤ 130 | ≤ 150 | 92 |
| Checklist phrase (each of 10) | ≤ 90 | ≤ 105 | 37–103 |

That hardest case fits with 2.7 mm spare at lengths at or just under these budgets. Products without a compliance bar gain about 15 mm. If every field hit its maximum at once on a flagged, updated product, a third page is possible; the overflow rule (whole checklist items move, nothing is cut) handles it, and the production render check in section 15 reports any product longer than two pages. The brief mentions 2–4 points; the code allows at most 3, and the design is built for up to 3.

---

## 12. Closing page — method and notes

**Design intent.** Builds trust by being open about method and limits, and keeps the legal-style notes in one calm place instead of scattered warnings. Feel: a colophon.

**Implementation specs**
- Four numbered steps in quarter columns, each with a navy 0.6 mm top rule, a maroon 6 mm number disc, a serif step title and 9 pt text:
  1. **Find** (agreed websites, four categories);
  2. **Check** (checklist, Meta, Trends, platforms, quick commerce, Amazon/Flipkart);
  3. **Score** (Want 20, Early 30, Room 25, Product 25);
  4. **Shortlist** (ranked, crowded last, supplier leads, dated links).
- "Sites checked for this issue" as small card chips (production: only the sites actually used in the issue).
- "This issue": issue date, research window, product counts, languages, workbook.
- "Important notes" box (paper-deep):
  - no sales / profit / margin / RTO predictions; a high score is not a promise;
  - prices and terms are as seen on the dates shown;
  - suppliers are leads only, not contacted, checked or endorsed;
  - compliance-flagged items need the client's own legal and safety check;
  - what "estimated" means;
  - Google Trends is relative interest, not searches or sales.

---

## 13. Excel workbook restyle

Keep the **five sheets, their names, column headers, row order and cell values exactly as they are**. `evidence_workbook_rows` and `supplier_workbook_rows` are shared with the delivery checker, and the deterministic-bytes ZIP normalisation stays. Only styling changes, all of it deterministic (openpyxl writes conditional formatting as static XML):

- **Header band** (rows 1–5, `add_brand_header`): unchanged logo and title; row 6 column heads stay navy with cream bold text, height 38 pt; add a 1 pt maroon bottom border.
- **Fonts:** Aptos 10 (the Office default; Calibri fallback on older Excel). Numbers right-aligned; scores and parts number format `0`.
- **Freeze panes** stay at `C7` (title rows + rank + product visible while scrolling); autofilter on row 6 stays; zebra banding stays (static fills cream / light grey); hyperlinks stay "Open source" in maroon.
- **Shortlist:**
  - **data bars** on Score (fixed 0–100) and on each part column with fixed maxima (20 / 30 / 25 / 25), navy `24384B`;
  - Label cells filled by value (equal-to rules): Sweet spot `E3ECF5`, Early bet `F3E7C9`, Proven but crowded `F1DFE2`;
  - Crowding cells filled with the ordinal ramp, light to dark (Low `F3E6E8`, Medium `E2C3C8`, High `C99AA2`) and the text kept;
  - Checklist "8/10" bold.
- **Checklist:** State cells, Meets = pale navy `E5EAF0`, Unclear = pale mustard `F1E5C5`, Does not meet = pale grey `ECECEC`; Basis "inferred" in italic grey.
- **Evidence:** State column, Positive pale navy, Not-positive pale grey, Insufficient/Unavailable pale mustard.
- **Supplier leads:** dispatch and subscription columns, "supported" / "No subscription stated" pale navy, "not supported" / "Subscription stated" pale rose.
- **Print setup:** landscape A4, fit to one page wide, repeat row 6 (`configure_page` already exists).
- Not recommended: icon sets (traffic lights are off-brand and colour-only), a sixth "Read me" sheet (it changes the five-sheet contract; see owner choice 8).

---

## 14. Owner's open choices (each with a recommendation)

1. **Fonts.** (a) Embed the open-licence fonts Source Serif 4 + IBM Plex Sans (licences included), or (b) use Windows system fonts on the report computer (Georgia + Segoe UI). **Recommended: (a).** It looks identical on every phone and laptop, does not depend on the report computer, and adds about 270 KB per HTML (the PDF grows by about 0.1 MB).
2. **Page budget per product.** (a) Exactly two pages in the normal case, a third page only when data is unusually long, or (b) one dense page. **Recommended: (a).** One page is unreadable on a phone and forces tiny text.
3. **Writing budgets for the AI text** (section 11 table). **Recommended: yes**, tighten them. It is the only way to guarantee page one fits in Hinglish, and shorter text reads better anyway.
4. **Four small, code-owned data additions** that the new charts need. Each is computed from records the system already saves; none is a new claim.
   - (i) The **Google Trends weekly series**: 52 numbers + week dates, from `trends_evidence.parsed.series`.
   - (ii) The **start date of each advertiser's first relevant ad**: a list of dates from the saved Meta ads behind `depth_facts`.
   - (iii) The **quick-commerce matrix** of app × city in stock / not / not checked, from the saved captures and their locations.
   - (iv) A **platform summary** per platform: listed / roles / dropship price, from `current_platform_presence`.

   **Recommended: yes to all four.** Without them the charts fall back to text-only summaries: "advertisers started between Mar and Oct 2026", "rising", "in stock in 2 of 3 cities".
5. **Compliance flag source.** (a) Code-owned: a fixed rule by product class (baby, ingestible, topical, medical, electrical) with fixed English and Hinglish reason sentences, or (b) show the AI's free-text `compliance_note` (English only today, not shown in the current report). **Recommended: (a).** It is predictable, identical in both languages and testable, with the AI note kept in the workbook only.
6. **Extra photos (2–3 per product).** **Recommended:** one hero on page one, and two 24 mm thumbnails at the top of "Example listings" on page two. The alternative is a hero-only report.
7. **A phone edition.** On a phone, an A4 PDF must be zoomed: at 2× zoom one half-page column fills the screen and is comfortable (see `renders/*-pdf-on-phone-2x-zoom-*`), but full-width paragraphs need sideways panning. Chrome can print the same HTML a second time on a narrow page (about 120 × 260 mm) using the phone layout already built in the mock-up. **Recommended:** yes, but as a **second phase** after the A4 redesign ships, because it adds two PDFs per issue and a portal listing change.
8. **Workbook "Read me" sheet.** **Recommended: no.** Keep five sheets. The PDF's "How to read" page covers it.
9. **Cover photos.** **Recommended:** top six products (as mocked). The alternative is the top three, larger.
10. **Hinglish checklist names.** **Recommended:** keep the ten criterion names in English, as production does today (they are the shared checklist contract), but translate the state words (Poora karta hai / Poora nahi karta / Saaf nahi, plus andaaza).

---

## 15. Production build approach (after approval)

**What changes**
- `src/product_radar/live_outputs.py`: `render_html(issue, dataset, language, root)` keeps its **signature, its validation call and its output contract** (one self-contained HTML string, no scripts). Its body is replaced by calls into new small modules:
  - `report_theme.py`: colour and type tokens, the stylesheet, embedded `@font-face` data.
  - `report_charts.py`: pure SVG functions (score ring, part meter, compartment bar, crowding gauge and pips, fit pips, advertiser timeline, Trends sparkline, quick-commerce matrix, glyphs, icons). These are ported almost line for line from `source/components.py`.
  - `report_pages.py`: cover, glance, key, product page one, product page two, closing.
  - `report_copy.py`: all fixed EN/Hinglish wording, moving `COPY`, `HI_*` and the new labels out of `live_outputs.py`.
- **Dataset additions** (owner choice 4) in `build_issue_dataset` / report selection: four new code-owned fields per product plus a `compliance` object, each validated in `validate_dataset`. Checks: types, ranges, 52 points 0–100, dates ≤ observation, cities from the configured list, and references to saved source ids. New fields are copied into the frozen stage so provenance checks keep working.
- `assets/fonts/`: 9 woff2 files + 2 OFL licence texts (copy from `source/assets/fonts`).
- Render-version constants bump (e.g. `functional-report-stages-v3-print-v3`). In this development stage old saved stages are simply regenerated; no compatibility path is kept.
- `_workbook_bytes`: styling only (section 13).
- `report_narration.py`: per-language length limits (owner choice 3) and the prompt text that states them.

**What stays exactly as is:**
- `validate_dataset` checks (extended, never loosened);
- `price_display`, `supplier_display`, `supplier_terms_display`, `_bulk_order_notice`, `score_text` / `crowding_text` wording (reused inside tiles);
- `display_market_checks` (still the source of the workbook Evidence rows);
- the stage / provenance / receipt machinery;
- `scripts/render_live_pdf.py` (no change needed: no scripts, same flags);
- the PDF inspection rules;
- the portal.

**Tests**
- Update the class-name assertions in `tests/test_live_outputs.py` (57 tests) and `tests/test_report_print_layout.py` (14).
- Add unit tests for every chart function (arc maths, fills at 0 / partial / max, pip counts, timeline placement and 30-day rule, sparkline windows, gauge needle per level), for language parity (the same numbers in EN and Hinglish HTML), for no `<script>` / external URL, for the minimum 8 pt font size, and for every state in section 11.
- Add an integration check through Windows Chrome: each product starts on a new page and takes ≤ 2 pages for the fixture issue in both languages.
- Coverage stays ≥ 90 % for the new modules.

**Estimated size of work:** large.
- About 1,200–1,600 new or changed lines of Python (most is the ported mock-up);
- about 80–110 tests added or updated;
- 3–5 long agent sessions, roughly 8–12 developer-days of equivalent work.

Split, in dependency order:

| Step | Effort |
|---|---|
| Dataset additions + validation | 2–3 days |
| Theme, charts and pages port | 2–3 days |
| Copy and Hinglish | ½–1 day |
| Workbook styling | ½–1 day |
| Tests | 2–3 days |
| Visual QA with a real issue in both languages | 1 day |

These are estimates.

**Risks**
- Real Hinglish text longer than the budgets: mitigated by choice 3 plus the overflow rule.
- Low-resolution marketplace photos: 600 × 400 px prints at about 240 dpi in the 50 mm plate, which is fine.
- Chrome print quirks: rings avoided; checked with a drawing scan.
- HTML size: about +0.3 MB per edition, still far below the 32 MB limit.

---

## 16. Skills consulted and judgement calls

- **dataviz** (used): form choice (meters and compartment bars rather than pies; one hue for nominal categories; an ordinal ramp for crowding; emphasis colour for recent advertisers), the palette validator, mark specs (2 px lines, ≥ 8 px dots, 2 px gaps, hairline axes), and the anti-pattern check.
  - Two dataviz defaults were deliberately not applied:
    - the **hover layer**, because print has no hover and scripts are forbidden;
    - the **≥ 48 px hero number**, because the owner's rule against oversized type wins. The score figure is 22 pt in a 30 mm ring.
- **ui-typography** (used): two families, sentence-case headings, letter-spaced short caps only, curly quotes and dashes, line length, tabular figures in columns only, no underlines except links.
- **impeccable** and **frontend-design** were not loaded, to avoid mixing a second set of possibly conflicting visual rules into an already defined brand.
- Brand continuity: palette, cream paper, serif voice, maroon accents and condensed kickers come from the approved P5 preview; long uppercase headings were dropped (the typography rule) in favour of sentence-case serif titles.
- Sample data: invented and marked on every page; supplier names are fictional; real marketplace photos from the research store were **not** used, because they are research data that must not enter version control. NeuraFlow's own illustrative still lifes were used instead.
