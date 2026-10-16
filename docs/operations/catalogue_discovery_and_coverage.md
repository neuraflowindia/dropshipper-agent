# Product research breadth, fresh discovery and exact search accounting

Updated: 16 October 2026 (Plan R10 product map with the soft target, the AI top-up, the row expander and the runway line; heading order, wider discovery and platform discovery, built on branch `r10`). This is the operating contract for the product map (the catalogue of product areas and headings), fresh discovery and coverage measurements. The
[execution specification](benchmarks/2026-10-03/execution_specification.md#30-current-source-routes-after-plan-r10-15-october)
owns the current source routes; the [implementation record](implementation_2026-10-11.md#r10-plan-r10-sources-crowding-check-product-map-and-spend-watch-1415-october) owns implemented details and wait codes. This document does not
approve a different provider, paid collection or visual redesign.

## 1. What the numbers mean

Keep five different counts separate. A public page can contain repeated sellers,
irrelevant services and known products; downloaded cards are not fresh products.

| Count | Meaning | What it does not mean |
|---|---|---|
| Public reads | Actual bounded page visits with saved navigation and source receipts. | Every result was a useful product. |
| Raw cards | Rows extracted from those pages within the saved limit. | Distinct types, qualifying products or fresh report inventory. |
| Staged map material | Exact source lines (platform categories and items, rising Trends queries, advertisers' ads, supplier listings) staged for the seed C call. | Accepted new headings, confirmed demand or suppliers. |
| Accepted family/phrase choices | Admitted functional directions and source-specific wording. | Each wording produces a different product, or has never been visited. |
| New admitted products | Existing A description and B identity work resolved a genuinely new concept. | Checklist/market/supplier/photo work is complete or a report is ready. |

## 2. Where the product map comes from

`config/research/catalogue.json` (revision `2026-10-16-r11-map`) holds four categories, **55 sub-areas** (13 or 14 per category) and **1,694 headings** (16 October 2026). The target is 40 headings per sub-area (`config/research/product_map.json`). The target is a soft cap: it decides which sub-area is built first, never that a sub-area stops growing. Headings are research directions with search phrases; they are not client recommendations.

**A reviewed list of headings becomes a new revision with the row expander** (`scripts/expand_catalogue_rows.py`, logic in `catalogue_rows.py`). Its input is one or more JSONL files of compact rows: `sub_area_id`, `label`, `purpose` (a verb phrase), `included`, `excluded`, `meta_phrase`, optional `trends_phrase`, and `season` (`"evergreen"` or `{"kind": "seasonal", "months": [...]}`); a line `{"new_sub_area": {"id": "branch-hk-...", "name": "...", "routing_words": [...]}}` adds a sub-area (its category comes from the id: `hd`, `hk`, `bb`, `to`). Each row becomes a full heading in the house style: id `<category code>-<label in kebab case>`, the standard definition and problem sentences built from the purpose, the standard brand/colour/pack exclusion, one plausible-extension example, a rationale saying it is an AI-assisted research direction (not demand evidence), Meta discovery, marketplace validation and Trends validation phrases with the standard reasons, and a review date one month on. New sub-areas get their routing words in `product_map.json`. The expander refuses, and writes nothing, when a row names an unknown sub-area, repeats an existing label or alias (or another row's label), names an excluded kind (jewellery, cosmetics, clothing, phone accessories, chargers, stationery, pets and the other excluded words of `dropship_platforms.json`) in its label, purpose, example or phrases, or would break the catalogue or settings checks. A Meta phrase the client-category gate does not place in the row's category, or one another heading already uses, is only a warning. The output is sorted, one-space-indented JSON; the catalogue may be up to 16 MB (about 6,000 headings). The next revision is `2026-10-16-r11-map`. Example: `.venv/bin/python scripts/expand_catalogue_rows.py rows/*.jsonl --dry-run`, then again without `--dry-run`.

The first 224 headings were built from Amazon-led research before Plan R10 (revision `2026-10-15-r10-w10`); the r11 revision added 1,470 AI-assisted headings and 7 sub-areas through the row expander below; a seed lane then fed 145 further examples from 8 October Amazon India category and bestseller pages (28 branches). **That Amazon-page seed lane is retired** (owner decision D4, Plan R10 W10): Amazon and Flipkart now only answer "how crowded is this product already?", and 113 of its 145 examples were old bestseller card titles a new capture could not reproduce. Its inventory file, staging script and seed-cycle turns were deleted; the headings it produced stay.

The map now grows only from material the programme already saves (`map_materials.py`; nothing here reads a website):

| Material | Where it comes from |
|---|---|
| `platform-collection` | A dropshipping platform's own categories (DeoDap's and BaapStore's collection trees, the signed-in platforms' category links) and its product types with at least three items. |
| `platform-item` | A platform catalogue item, list items first (bestsellers, trending, new arrivals). |
| `trends-rising` | A rising related query saved with a signed-in Google Trends read. |
| `meta-advertiser` | Another active ad of an advertiser behind a relevant reviewed ad (wider discovery). |
| `supplier-listing` | An IndiaMART or TradeIndia listing found under a mapped category term (wider discovery). |

Each candidate is looked at once. It must pass the client-category gate (jewellery, cosmetics, clothing, phone accessories, pets and similar kinds never pass) and fit one of the four categories. The gate reads the category words of `dropship_platforms.json`; when words of several categories appear, the longest matching phrase decides ("neck pillow" is Travel and Outdoor although "pillow" alone is Home and Decor), and the file's order breaks a tie. The category words are deliberately few and category-defining (a few were added on 16 October for the newer sub-areas: swaddle, pregnancy, maternity, colouring, craft kit, balloon, desk, bulb, swimming, screwdriver, home repair); the sub-areas' routing words are not copied into them, because broad routing words ("rice", "cake", "led") would let food or electronics through. One-word wording ("Lights", "Toys") is too broad. Wording that already covers a known heading, alias, phrase or type (two words at least) is known, not new. Variants of material still waiting for its AI decision are looked at again once that decision is saved. A candidate is placed in one sub-area by the sub-area's routing words (`product_map.json`) and heading words (`map_materials.place`). Without a client category from the gate, only a multi-word routing-word or heading match places it ("egg boiler", "sleeping bag"); one generic routing word never does, so "melatonin sleep gummies" or a "smart LED TV" are outside the categories. With a gate category, it goes to the sub-area of that category with a clear margin of routing-word and heading hits (between two equally close sub-areas, the one sharing clearly more words), unless another category is clearly meant: a multi-word match at least as strong ("table tennis" over "table") or two or more of its routing words clearly stronger ("piping" and "icing" over "bag"). Otherwise it waits until the map has grown by 25 headings. A routed candidate is saved once as seed material (a small page holding the exact source line). A sub-area holds at most 20 waiting examples at a time; a sub-area at or above its target still takes material.

## 3. How the map grows (one round at a time)

The product-map lane (`product_map.map_step`) runs inside the programme's discovery step and its ordinary AI allowance; there is no separate bulk run.

| Step | Exact work | Cost and continuation |
|---|---|---|
| Stage | New material is looked at and the useful part staged; the fresh-signal list the heading order reads is refreshed. | Local only; at most 400 candidates examined and 30 staged a round. |
| Build | One seed C call (`catalogue_seed.build_catalogue_branch`, prompt `research-c-catalogue-seed-v2`) for the sub-area with the fewest headings that has waiting material. Sub-areas below the target (40) always go first; one at or above it is built only when no sub-area below it has waiting material ready (one on a retry wait does not hold the others up). Within that, sub-areas with at least five waiting examples first, categories in turn on ties; at most ten examples a call. | One ordinary work-turn AI call a round, only while the round still has an AI allowance. |
| Renew | When new types have arrived that the map has not considered (from new arrivals, rising queries, new advertisers and the other discovery lanes), one `catalogue_expansion.run_expansion` call with at most five of them. | At most one more AI call a round. |
| Top up | Only when the build had nothing to build (or made no call) and fewer than **300** usable headings were never searched: one C call (`map_topup`, prompt `research-c-catalogue-topup-v1`) for the sub-area with the fewest never-searched headings, then the fewest headings, categories in turn. Left out: a sub-area at its ceiling (3 × the target, 120 usable headings), one resting (its last two top-ups each added fewer than 3 headings: it rests 30 days from the latest), and one on a wait after a failed call. The AI gets the category, the sub-area's name, all its heading labels, the 400 other labels of the category most like the sub-area (sharing words with its name and headings) and the client's rules (India dropshipping; small, light, generic, unbranded; never jewellery, cosmetics, clothing, phone accessories, chargers or earphones, stationery, pets, food or consumables, medicines) and returns at most **12** headings (extra ones are trimmed, not a reason to drop the answer): label, purpose, one included example, one excluded neighbour, a Meta discovery phrase and season. | At most **6** calls a day (UTC), one a round, only while the round still has an AI allowance. "Never searched" means never planned into a discovery wave or cycle and never searched (the same saved facts the heading order uses). |
| Admit | C chooses same heading, new heading, unclear or no useful addition, with at most three short phrases. **Every new heading must carry a Meta discovery phrase**, so it can be searched; phrase sources may include `dropship` wording for the platforms' catalogues (`research-c-v2`). Exact known labels and aliases merge locally. A top-up heading whose label or alias is already in the map, or the same words in another form (plural, hyphen, word order), or a variant whose words include a known heading's, is the same heading (nothing changes); one whose Meta phrase another heading already searches becomes that heading's alias; one naming an excluded kind, with a bad season ("evergreen" with months, months outside 1-12), or clearly of another category (a multi-word routing match in another category's sub-area, or the gate reading it as another category while routing does not place it in the asked one) is refused, that heading only; one the routing words place in another sub-area of the same category joins the sub-area it was asked for; the rest become new headings in the expander's house style (basis `plausible_extension`, author "AI top-up of the product map"). | An admitted heading joins the effective catalogue at once. A saved answer is reapplied after a local failure or a crash, never bought twice; one that can never be admitted (unreadable, or its sub-area has left the map) is closed with one rejected record instead of being retried forever. |

A failed build or renewal call waits 30 minutes, a failed top-up call 60 minutes (and counts towards the day's six) and puts its sub-area on the same wait, so the rotation moves on; each reuses its saved original job. A top-up whose saved original still waits for reconciliation sends nothing, counts nothing and holds only its sub-area, for a day. Material that cannot be read is set aside for a day so its sub-area moves on. The lane's small state is one `batch_runs` record per programme (including the top-up's wait, its per-sub-area waits, the day's call count and the last topped-up category; it is saved before the top-up runs, so a top-up failure never loses the build's or renewal's state); local speed aids live under `product_map/` in the store and are rebuilt when malformed. The wake hint wakes the programme for a due top-up (when some sub-area is open for one) and for a saved top-up answer still to be admitted; a top-up defect never hides the build and renewal hints.

**The runway line.** At the end of each round the lane saves `product_map/runway.json` in the store: usable headings, how many were never searched, and how many were first searched in the last seven days. The pace is that count per day (over the days since the first search ever, when that is under a week). The owner meter (`run_programme.py --meter`, field `product_map`) and the desktop window (the line after the opportunity score, `map_line`) show it in plain words, for example: "Product map: 2,140 headings, 1,980 not searched yet; about 70 searched a day lately, so roughly 4 weeks until every heading has had its first search. After that, headings are searched again for new ads, and the AI adds new headings whenever fewer than 300 are left unsearched." Before any search it says the pace is not measured yet.

## 4. How fresh discovery proceeds

The discovery step (`create_catalogue_discovery_step`, renewable waves `discovery-wave-v2`) runs every round under the programme's writer. The first use of the two AI calls a round held for discovery rotates among four lanes: heading searches, the dropshipping platforms, wider discovery and the product map.

1. **Heading searches.** Each wave serves headings through Meta, the one search discovery route. Amazon and Flipkart searches and Amazon lists no longer produce candidates (D4); quick commerce is never a discovery source (its credits go only to admitted per-type checks). Exact unchanged searches within seven days reuse the saved capture.
2. **Heading order** (`heading_order.py`, replacing alphabetical order): headings whose types all came out High crowding go last; never-served headings first; then headings with fresh signals since last served (new platform list items, rising Trends queries, relevant advertisers' new ads whose words include the heading's), more signals first; then never-served headings the AI top-up suggested (ideas without observed material); then the heading served longest ago. Within each step the categories take turns, so a wave reaches every category after a few headings.
3. **Dropshipping platforms** (their own lane every round): in-stock, in-category list items (bestseller / high-demand / winning first, then trending, then new arrivals; newest first; platforms in turn), at most 5 a round and 30 a day, nominated through the existing candidate admission.
4. **Wider discovery** (after the heading searches): relevant advertisers' other active India ads; rising Trends queries, each used once as a Meta search; IndiaMART and TradeIndia results for mapped category terms (`config/research/wider_discovery.json`), where a listing not seen before is a candidate. IndiaMART reads yield to type supplier searches.

Every lane checks the client categories before any read or AI call, skips anything already captured or covering a known type, stops nominating while earlier captures are still being admitted, and respects daily read limits. Admission is the existing A description and B identity flow; a newly admitted type gets its own follow-on source review scope.

| Situation | Actual continuation | Honest result |
|---|---|---|
| Meta is refused or fails for a heading | The question waits under Meta's shared access wait; platforms, wider discovery and the map continue. No Amazon/Flipkart fallback. | No invented family or demand. |
| Cards or items are captured | A/B description and identity work on those captures. | Raw, described, known, new and deferred counts stay separate. |
| Navigation may already have started | Reconcile the original saved acquisition/receipt only. | No replacement read disguised as a retry. |
| A site refuses one lane | The refusal holds the same site's reads in every lane (shared access wait); its probe is shared. | No silent workaround. |
| A lane fails locally | That lane records its own wait; the other lanes still run that round. | No global claim that discovery stopped. |
| New identity is admitted | Immutable new concept IDs go to a separate follow-on review scope. | Discovery is not report readiness. |

Live discovery under these rules starts at the W13 cutover; fixture tests are local proof only.

## 5. Exact search pool and six-month workload

The retained snapshot at **09:49:46 UTC on 8 October 2026** (the last full count; the map has since grown to 55 sub-areas and 1,694 headings, and fresh counts come from `catalogue_coverage.catalogue_matrix` on the live store) has the following actual sums. This is not
category count multiplied by an invented universal phrase count.

| Category | Accepted families | Family/phrase pairs | Source/purpose options | Meta discovery choices | Meta actually tried | Meta not dispatched |
|---|---:|---:|---:|---:|---:|---:|
| Home and Decor | 56 | 110 | 170 | 58 | 6 | 52 |
| Baby Products | 56 | 109 | 169 | 57 | 8 | 49 |
| Home and Kitchen | 56 | 109 | 169 | 57 | 9 | 48 |
| Travel and Outdoor | 56 | 110 | 169 | 57 | 7 | 50 |
| Total | 224 | 438 | 677 | 229 | 30 | 199 |

`catalogue_coverage.catalogue_matrix` keys every actual usable choice by family,
normalized phrase, source, purpose, language and country. A different reason
does not create another choice. Bound original captures or actual submitted
Actor receipts establish attempts; preparation does not. Uncertain attempts
remain uncertain. Department lists, alternate-source retrieval and unmapped
product checks are listed separately, not used to claim fresh unused pool.

At three issues per week for 26 weeks, the planning horizon is **78 issues and
780 entries** at ten entries per issue. At most two useful updates per issue
means at least **624 fresh entries**, if useful updates actually exist. Without
them it means 780 fresh entries. The small existing pool does not on its own
establish six months of supply.

| Explicit assumption | Fresh entries per issue | New candidates needed per issue | Queries per issue at five genuinely new candidates per query | Queries for 78 issues |
|---|---:|---:|---:|---:|
| 5% useful yield, maximum useful updates available | 8 | 160 | 32 | 2,496 |
| 5% useful yield, no useful updates available | 10 | 200 | 40 | 3,120 |
| 3% stress yield, maximum useful updates available | 8 | 267 | 54 | 4,212 |
| 3% stress yield, no useful updates available | 10 | 334 | 67 | 5,226 |

Five genuinely new candidates per query is an assumption, not measured from
the 660 raw cards. The new relaxed checklist changes useful yield, so measure
it rather than carrying a legacy rejection percentage forward as truth. The
system needs continuing functional breadth, real list renewal, source-mix
learning and meaningful revisits. Useful similar free wording is allowed but must not be counted as thousands of new types or guaranteed fresh runway. The
[cost plan](build_activation_and_costs_2026-10-03.md#5-multi-account-financial-implementation-and-remaining-activation)
owns failed/duplicate collection, AI effort, exact charges, pooled-account
funding and lower-yield cost scenarios. Zero Apify charges for these page reads
does not mean zero computer time or zero later model cost.

## 6. Settings and verification

| File | Holds |
|---|---|
| `config/research/catalogue.json` | Categories, sub-areas, headings, phrases (up to 16 MB; written by `scripts/expand_catalogue_rows.py`). |
| `config/research/product_map.json` | Soft target per sub-area (40), build and renewal sizes, the top-up (`min_unsearched_headings` 300, `headings_per_call` 12, `max_calls_per_day` 6, `retry_minutes` 60), order windows, routing words. |
| `config/research/wider_discovery.json` | Wider lanes: reads per round, recheck days, supplier category terms. |
| `config/research/dropship_platforms.json` | Platform list pages, discovery limits, category words and excluded kinds (also used by the row expander and the top-up). |

```text
.venv/bin/python -m unittest tests.test_product_map tests.test_catalogue_rows tests.test_live_research_config tests.test_heading_order tests.test_wider_discovery tests.test_catalogue_seed tests.test_catalogue_expansion tests.test_discovery_waves tests.test_owner_meter tests.test_desktop_control -v
```

The map-lane and catalogue tests check exact outcomes against frozen r10 copies of the three settings files (`tests/fixtures/research_config`, 224 headings, applied by `tests/frozen_research_config.py`), so they do not change as the live map grows. `tests/test_live_research_config.py` checks the live files instead: the catalogue validates (at least 48 sub-areas and 1,600 headings, no empty sub-area, every heading searchable), every sub-area has routing words and the settings match the catalogue, the top-up threshold is below the map's size, every category word of the platforms' settings gates to its own category, and a table of known products gates and stages correctly (basmati rice, sleep gummies and an LED TV to none and never staged; plant pots, feeding spoons, car cup holders, swaddles, rice lights, sleeping bags and the newer sub-areas' products to a sub-area of their category; a bicycle tool kit is the known exception: the home-repair phrase "tool kit" wins).

Measured with a synthetic 2,024-heading catalogue (16 October, WSL): loading and checking the catalogue file 38 ms, building the effective catalogue 62 ms, the heading order (facts and sort) about 50 ms, the runway summary about 1 ms, routing 400 candidates 31 ms. The discovery readiness check, which looks at every heading's Meta question, grows in step with the headings (about 0.25 ms a heading: 0.5 s at 2,000 headings, without source hooks).

These are local fixture proofs, not a claim that live recurring discovery, six-month capacity or client release has been accepted.

## 7. Long-lived research and one joined counting system

The client recommendation is a narrow functional type using the existing concept identity; concrete listings/offers are its examples. Catalogue headings and words help find and investigate those types, but do not themselves count as client recommendations. The planned scope bindings join heading/direction research, actual concepts and original captures. Trends normally uses common type wording; suppliers/marketplaces also need actual offer-specific facts. One broad chart does not prove every narrow subtype.

The dated233-heading/470-wording-pair/710-instruction view is explained, together with the44 attempts that produced the original156 concepts, in [section26.11](benchmarks/2026-10-03/execution_specification.md#2611-counts-provenance-yield-and-expense-that-the-owner-can-follow). Those710 instructions are an existing partial source assignment, not every possible application search.470 ×14 =6,580 possible wording/route cells is a coverage view, not a mandatory bill or product inventory. Aliases share a subject; actual compatible requests share one acquisition; every target retains scoped relevance and route outcome. The153 ×14 product audit remains a different joined view, not an unrelated competing search system.

Target2,000 logical issues through renewed researched directions and actual market supply. Similar free searches are permitted; exact pointless repeats reuse saved work, and unchanged AI answers are not purchased again. At ten entries/issue,2,000 issues require20,000 appearances and at least16,000 genuinely New types if useful updates fill the entire allowance. This is a longevity workload, not evidence that the present seed guarantees that supply. Measure distinct functional discovery, completed practical screening, eligible unused inventory, meaningful updates and real expense separately. Do not claim a number of months from unused wording counts alone.
