# Proposal — speed, product universe, sources and selection (14 October 2026)

**Project stage: Development and testing.** Status: **proposal awaiting owner decisions** (questions at the end). Nothing in "Proposed" sections is built. Facts come from read-only diagnoses of the live store and run logs on 14 October (snapshots 04:35–05:15 UTC); estimates are labelled.

## Why this exists

The owner's concerns (14 October): about 8–9 hours of research after the fresh start produced only 6–7 report-ready product types; the product universe may be too small for three issues a week with at least 80% new products; and selection may be stricter than the agreed checklist. The client also asked (13 October) that products already well established on Amazon India and Flipkart be pushed later in the queue, and named four dropshipping platforms to consider.

## Findings

### 1. Speed: the program slows down as it learns

- Since the fresh start: 24 candidate types, all described and checklist-assessed, 18 passing (≥5 Meets), 7 report-eligible (05:15 UTC).
- Of 9.3 hours of logged run time, AI calls took about 55 minutes and website reads about 26 minutes. **About 84% was local recalculation.**
- Root cause: the report pool (`report_selection.build_report_pool`) is rebuilt from scratch several times per round (photos stage, reports stage, round-start readiness), and each rebuild re-hashes evidence about 1.5 million times (`research_scope.validated_scoped_evidence`, `_current_dependencies`). One rebuild takes about 144 seconds for only 24 types, and it grows with the store. Each state snapshot also deep-copies about 30 MB.
- Effect: rounds grew from 7–12 minutes (20:15 UTC) to about 70 minutes (03:44 UTC) while doing the same work per round (about 10 AI calls, 14–16 page reads). New types found per hour fell from about 8 to under 1. This is a performance fault, not a shortage of ideas or a rule.
- The per-round limits (12 source reads, 10 AI calls in `work_turns.py`) are sensible only when rounds are short.

### 2. Strictness: the rules match the contract; the AI is too cautious

- **Fit information pending (6 types):** the rule matches the contract (§4). The AI answers "unclear" too often (confidence 20/24 types, saves money 18/24, perceived value 17/24) with reasons like "description does not establish", although the contract allows practical inference.
- **Market coverage required (9 types):** the contract needs at least one usable related market site. All nine already have a saved Amazon bestseller result that is only waiting for AI review ("interpretation pending"; 57 such results across the pool).
- **Supplier search required (9 types):** the code needs a recorded supplier attempt (not a result), which the contract allows, but it only queues supplier work once that is the sole remaining blocker.

### 3. Sources: what works

| Route | Role | State since the fresh start |
|---|---|---|
| Meta Ad Library (direct browser) | discovery + market | 12/23 usable (Meta completeness fix pending restart) |
| Amazon Bestsellers | discovery + market | 17/17 |
| Amazon Movers & Shakers | discovery + market | Amazon publishes no list for our categories ("no movers and shakers available"); never worked |
| Amazon search, Flipkart search | discovery + market | 21/21, 22/22 |
| Google Trends | market | Google returns HTTP 429 to the program's own hidden (headless) browser on every attempt ever (44/44); it only worked on 6–7 October through a person-driven connected browser |
| Blinkit / Zepto / Instamart | neutral market context | Never run: no delivery location is set for the programme |
| TradeIndia | supplier | Works through the browser (10/10); queued behind per-round limits |
| IndiaMART | supplier | Login wall with placeholder listings for every query; never worked |
| Google Maps | supplier | Browser read restored (pending restart); owner confirmed permission on 14 October |
| Google web search | supplier | Blocked by Google (JavaScript gate / "unusual traffic"); being retired from automatic research |
| Company pages, photos | supplier, photo | Working |

**Paid search ($0):** the automatic programme never uses Apify. Meta is read directly in the program's own browser; Amazon and Flipkart are read directly. The USD 0.50 in the ledger was spent on trial runs before 7 October. The USD 3.07 earmark could buy roughly 350 deeper Meta runs (advertiser counts and ad start dates the direct read often lacks).

### 4. Product universe: bounded and Amazon-built

- Research starts from 224 headings in 28 sub-areas (`config/research/catalogue.json`), grown by AI review of 145 seed examples (113 of them Amazon bestseller titles) to 241 headings so far. Travel and Outdoor branches had not been reached.
- New candidates come only from Meta, Amazon and Flipkart searches of heading phrases and four Amazon department lists. **All 24 types since the fresh start came from Amazon.**
- The AI heading expansion (`run_expansion`) exists but is not wired into the programme; there is no category-tree crawling. Headings stop growing once the seed review finishes.
- Questions are processed alphabetically by heading, about four per round, so one pass over all headings takes about 15 days at current speed.
- Estimate: 1–3 eligible types per heading → 270–810 types in total → **about 11–34 weeks** of supply at 24 new types a week. The client's new Amazon/Flipkart rule shortens this further.

### 5. The client's Amazon/Flipkart rule conflicts with the current design

Today Amazon and Flipkart are both **discovery sources** and **positive market votes** (contract §6; `discovery.py`, `opportunity_policy.py`, `catalogue_discovery.py`). The client wants established Amazon/Flipkart presence to count **against** a product (later in the queue). Our Amazon/Flipkart readers currently keep only listing ID, price and position, not ratings, sellers or result totals.

### 6. Dropshipping platforms

See [platform research](../research/sources/2026-10-14_dropshipping_platforms_and_search_services.md). DeoDap (about 12,400 public products, all four categories, official Shopify feeds) is the strongest; BaapStore is fashion-heavy; Dropship India and Dropdash need a login. All four forbid automated copying without written permission.

## Proposed (not built)

| # | Proposal | Contract change? | Effort (estimate) | Expected effect (estimate) |
|---|---|---|---|---|
| P1 | Fix the slowdown (report-pool hashing) | No | **Done 14 October** (commit `0a71c9c`): pool build about 6–8× faster, identical output. Further gains possible (pool reuse across stages, cheaper snapshots) | Rounds expected to return towards 10–20 minutes; to be measured after the restart |
| P2 | Tell the AI to use the contract's practical-inference rules (sensible, recorded guesses; concise reasons) and give fit-pending types one recorded review | No | Small | Pass-to-eligible conversion from about 29% to 60%+ |
| P3 | Finish nearly ready products first (review saved market results, supplier search for every fit-passing type) before new discovery | No | Small | Shorter lag to report-ready |
| P4 | Market support as one recorded AI judgment over all saved evidence per type | No if at least one usable site is still required; **yes** if a type may qualify with zero sites | Small | More types qualify |
| P5 | Amazon/Flipkart become a **saturation check** (not a vote, not a discovery source): readers also save ratings count, stars, seller/brand, "bought in past month", sponsored flag, result total; types ranked Low → Medium → High saturation within each fit tier | **Yes** | 3–5 days | Matches the client's request |
| P6 | Draft saturation levels (to calibrate on the first 50 types): High = bestseller-list presence, or ≥3 relevant listings with ≥1,000 ratings, or ≥8 relevant listings from ≥5 sellers; Low = ≤2 relevant listings and none with ≥200 ratings; Medium = otherwise | Part of P5 | — | — |
| P7 | New discovery sources: Meta (including each advertiser's other ads; optional Apify depth within the earmark), quick-commerce new launches (needs locations), DeoDap new arrivals/bestsellers (after permission), IndiaMART/TradeIndia category trees, optionally YouTube/Instagram official APIs | Yes (sources) | 1–2 days per source | Breaks the Amazon dependence |
| P8 | Universe map: 4 categories × about 12 sub-areas × about 25 headings ≈ 1,200 headings, one column per source plus saturation; built from permitted category trees and the existing AI branch review; renewed weekly from new arrivals, rising queries and new advertisers; about 25% refreshed each quarter | Yes (catalogue) | 4–6 days build, 1–2 days first generation; about 150 AI jobs, USD 0 Apify | Roughly 1,000–1,800 new types a year |
| P9 | Stop wasted reads: escalating waits for permanent blocks (Trends, IndiaMART, Google), IndiaMART wall recorded as blocked, Google web search retired | No | **Done 14 October** (commit `0a71c9c`) | Fewer futile reads; Maps no longer held up by Google search |
| P10 | Google Maps straight to the browser (skip the plain read that always returns an empty app shell) | Internal design change under the owner's Maps permission | Small | Halves Google requests per Maps question |

## Questions for the owner

1. **AI judgment (P2–P4):** may the AI make sensible, recorded guesses on the checklist and market support, within the agreed rules? May a product qualify with **no** usable market site at all, on AI judgment alone?
2. **Amazon/Flipkart rule (P5–P6):** do you approve turning Amazon/Flipkart into a "how crowded is it" check? Are the draft High/Low levels acceptable? Do sponsored-only listings or Meesho count? Should crowded products only go later in the queue, or be left out when enough others exist? Should the client see the crowding level in the report?
3. **New sources (P7):** which to add first? Will you obtain written permission from DeoDap (recommended first), BaapStore, Dropship India and Dropdash?
4. **Universe map (P8):** build the larger map, rebuilt mainly from non-Amazon sources?
5. **Quick commerce:** which delivery locations (city/pincode) should Blinkit, Zepto and Instamart checks use, and may their credits be spent automatically?
6. **Apify:** may automatic research use the remaining USD 3.07 earmark for deeper Meta reads?
7. **Google Trends:** leave it off; or allow one diagnostic read in a visible (not hidden) browser window; or apply for Google's official Trends API (alpha, by application)?
8. **AI usage limits:** which Codex/ChatGPT plan funds GPT-6 Luna? Faster research means more AI calls per hour.
9. **Maps (P10):** go straight to the browser for Google Maps?
10. **Private download website:** the portal's packet checker changed (Google search row removed, supplier outcome rule). The online copy must be updated before the first new report can be uploaded. May the agent deploy the portal update to the existing Cloudflare Worker when the first report set is ready?
11. **GitHub author address:** commit `0a71c9c` was pushed with the business Gmail address as author instead of the private no-reply address, because the computer's global Git setting uses the Gmail address. Future commits now use the no-reply address. Rewrite that one commit's author on GitHub (a "force push" that replaces it), or leave it?
