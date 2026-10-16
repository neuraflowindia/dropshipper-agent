# Source, interpretation and recurring-service measurement protocol

Updated: 16 October 2026. This document specifies **how to measure** a bounded experiment, rehearsal or live proof. The [product brief](../../../product/india_dropshipping_product_opportunity_radar_product_brief.md) owns delivery, source-role and ranking requirements; [Plan R10](../../../project/plan-2026-10-14-speed-sources-and-selection.md) and the [implementation record, R10](../../implementation_2026-10-11.md#r10-plan-r10-sources-crowding-check-product-map-and-spend-watch-1415-october) own the current source set and what was built; the [execution specification](execution_specification.md) owns older mechanisms; the [cost plan](../../build_activation_and_costs_2026-10-03.md) owns funded limits; [Progress](../../../project/pj-tracker.md) owns current status; the [benchmark record](benchmark_record.md) keeps historical measured results. Do not duplicate those authorities here.

Plan R10 has been live since 15 October 2026. Its pending live checks (Trends after sign-in, IndiaMART and Dropdash signed in, the DeoDap and BaapStore feeds, quick commerce in three cities, one Apify depth read, crowding on at least ten types, round time) are measured with the rules below and recorded in the implementation record, not as a new narrative document. The isolated direct Amazon India Bestsellers trial and the P2 block (local ten-criterion migration, bounded AI reviews, internal report sample) are complete history; their [results](benchmark_record.md) keep their own dates and scope.

## 1. Measurement units

| Unit | Count separately |
|---|---|
| Logical research question | Family/phrase/source/purpose or named concept/list question; exact scope/options. |
| Physical acquisition | Actual browser/provider read/capture. Shared capture counted once; replay is not acquisition. |
| Source output | Returned parent ads/cards/listings/pages, usable/mapped/uninterpretable/partial/noise and cap truncation. |
| Concept | Functional identity after matching; source seller/colour/pack count is not concept count. |
| Screening | Finished checklist/relevance/market work versus not tried/pending/unavailable. |
| Recommendation supply | Eligible never-published New, eligible unpublished carryover, material updates, already queued and set aside. |
| Report | Distinct logical issue/revision; two languages are one issue, not two research costs. |
| Expense/effort | Collection fee, known read fee, unknown hold, direct-page attempts, model usage/time, supplier queries and review time. |

Always show denominator and checkpoint date. Keep collection success, semantic relevance, useful product yield, funds and report readiness distinct.

## 2. Direct-Amazon trial protocol (completed; reusable pattern)

The completed [section19](execution_specification.md#19-standalone-direct-amazon-experiment) trial used three inspected India departments and six corrected verification visits, retaining initial price-incomplete visits and the local startup failure. One page/up to100cards per attempt; observed30 per successful capture. No Apify/model calls or production writes. Amazon pages are now read only for the crowding check (section 4), so this protocol is a reusable pattern for a separately authorised new source trial, not an automatic repeat.

Report by case/round:

1. Attempts and access-success ratio.
2. Correct-country/list-success ratio; challenge 200 is not success.
3. Captured/parsed/usable card counts and missing ASIN/price/photo/position fields.
4. Usable-capture/attempt ratio, elapsed time and exact partial/error reason.
5. Unique ASIN/title examples, not inferred functional product/pass yield.
6. Repeatability/replay/isolation checks; a same-day round does not prove weekly freshness.

No automatic promotion. Regroup with actual results; direct adapter integration is a distinct later block. Failed direct access can be useful diagnostic evidence without being a product-market negative.

## 3. Connected-source and interpretation measurements

For an approved coherent implementation block:

1. Inspect current actual route/input/control/schema, target purpose and existing equivalent captures/jobs.
2. Prepare a finite scope with item/action/model/request/spend/deadline limits. State the actual missing question and baseline.
3. Test relevant deterministic failure/replay paths before live credentials/network where risk warrants it.
4. Paid work reserves funds and durable intent under the planned exact-attempt/short-lock contract; direct work saves the actual acquisition identity. No fictitious paid attempt for a free browser capture.
5. Retain bounded original response/URL/scope/time/hash and parser status. Preserve partial/noise and omitted counts.
6. Review identity/relevance/checklist/supplier interpretations independently against actual material; validate references and record reasoned judgments, not schema-only pass.
7. Reconcile expense and retain unknowns. Count public zero-fee effort and skipped/not-applicable source checks.
8. Save Pass/Partial/Fail/Blocked with exact stage, confirmed/suspected cause, next trigger and unaffected work.
9. Replay from saved data after mapper/template corrections rather than recollect by default.

Use route-specific actual scope. Meta parent ads differ from cards/advertisers; marketplace search order differs from sales rank; Trends relative indices/bounds differ from absolute demand; QC depends on location/pack/time; a dropshipping-platform match is a local match against a saved catalogue, not a site search; supplier terms depend on company/product/workflow.

## 4. Source-specific acceptance questions

| Area | Required evidence |
|---|---|
| Meta | Genuine IDs/advertisers/active dates, relevant versus unrelated records, repeated retailer catalogues, distinct discovery versus product validation. The free Ad Library read and the paid deeper read (`meta-ads-depth`, asked only when the free read lacks advertisers or start dates) count as one site; merged ads are not double-counted. |
| Amazon/Flipkart crowding | **Crowding check only** (no discovery, no want points): first-page cards of Amazon search, Amazon bestseller lists and Flipkart search, read with the current card parser (ratings count, stars, brand where shown, "bought in past month", sponsored, best-seller badge, total results). Measure the Low / Medium / High / Not checked split from AI-reviewed relevant listings only; Not checked is never Low. After 50 checked types the High share is checked once against the 10%–70% band (`config/research/crowding.json`). Amazon Movers & Shakers is retired. |
| Trends (signed-in browser) | Proof gate after each sign-in: three complete reads including at least one verified chart export; refusals kept against the signed-in identity with escalating waits; actual term/geo/window, partial periods, interval values and Rising queries. No Apify Trends fallback. |
| Quick commerce | Paid provider API only (the free browser route is deleted). Admitted types only (checklist passed, crowding Low or Medium), one main phrase, Mumbai on all three apps first, Delhi NCR and Bengaluru only after a relevant Mumbai result. Record platform+location+product/pack/date, available/no match/unserved/blocked, credits charged, and held checks as "reserved for later", never as failures. Earns at most 2 want points; never discovery. |
| Dropshipping platforms | DeoDap and BaapStore public catalogue feeds and Dropdash signed in: feed completeness or sample depth, list membership (bestseller/trending/new arrivals), local match quality per type, plan-wall and layout waits. A listing is a presence fact and a supplier lead, not a market vote. |
| Suppliers | IndiaMART (signed in), TradeIndia, Google Maps (browser read first) and discovered company pages: real queries/pages, exact/family/company lead, MOQ versus dispatch, subscription versus signup/optional tier, separate e-commerce/packaging/private-label fields. Google web search for suppliers is retired (no approved official route). |
| AI | Useful grouped-description/identity/relevance/supplier cases, accepted/rejected siblings, actual refs and missing-data meaning. Relevance review judges only whether each record is the type; it gives no market opinion. Usage is read through the spend watch; no all-role-per-product or automatic second checker. |
| Photos | Real byte/source/product ownership and meaningful selected image; bounded safe decoding is necessary but not semantic proof. |
| Reports | Same canonical facts across English/Roman-Hinglish/workbook: all ten active original-ID reasons, Good/Decent fit, "Opportunity score NN/100" with its four parts and label, Amazon/Flipkart crowding with its counts, exact source metrics/bounds, useful suppliers, real images and actual native page review; legacy thirteen-row replay preserved. |
| Website | Private object readback, owner/client allow-and-deny, immutable/CAS conflicts, the portal's mirrored route set and score check, actual approval/server schedule/history and unavailable-owner independence. |

## 5. Limits and legacy evidence

Apify is the free plan only: US$5 of usage each month, earmarked automatically for the deeper Meta read (US$4.45, with US$0.55 kept back; `config/research/apify_allocation.json`), at most US$0.03 a run plus a US$0.02 read hold. Older trial ceilings (original US$0.75 total and US$3.75 future allocation) stay in the journal as history and are not replenished by a new test, key or date; only the one funded account is used and original recovery stays origin-bound. Quick-commerce credits are the prepaid packs (one credit per successful search; packs expire 30 days after purchase); the owner buys a pack only when asked. The spend watch flags every allowance at 50/75/90/100% and a money route waits at 100% while free work continues. See the [command reference](offline_runner_guide.md) and the [operator guide](../../local_execution_and_provider_setup.md) for actual commands and owner steps.

Public/direct and signed-in reads incur no provider fee, but preserve all effort, pacing and provider restrictions. Never buy a paid plan, membership, search/proxy pack or credit pack automatically.

AI runs through the owner's signed-in ChatGPT subscription (GPT-6 Luna, high effort, no paid fallback; `config/ai/codex_luna.json`). The runner allows at most256KiB of public input per job and180 seconds; a round makes at most10 AI calls (`config/research/round_limits.json`). The separate90-second feasibility probe is not the normal runner deadline. Owned-process receipts and exact original job/runner replay apply. Usage percentages are read without a model call and only ever flag; research AI waits only to protect paid Codex credits. Preserve completed output/unknown usage; no blind retry of potentially running jobs or new paid route.

## 6. Supplier and selection samples

Start a small supplier experiment with a few actual coherent products,1–2 grounded queries and initially up to3 relevant pages each; terms/FAQ page when readily useful. New versioned search plans can explicitly allow up to5. Keep old three-lead replay and actual no-result/blocked outcomes. Dropshipping-platform listings are supplier leads with the platform's stated terms; wider-discovery supplier listings are labelled "Supplier listing (lead)" and never shown as market examples.

Exercise exact/close/family lead, stated no subscription/required/optional/unknown, monthly/annual/setup/deposit/per-order fees, single-piece purchase/dispatch/bulkstockthenindividual, COD/integration/neutralpack/private label and quoted pack/MOQ. MOQ 1 alone does not prove dropshipping. Supplier relevance and stated terms are not verification. No supplier contact/purchase.

Selection cases cover broad categories/moderate brands, the exact ten active original IDs, Good>=6/Decent=5 both included, retired rows excluded, legacy13 compatibility and unknown versus supported negative, useful inference, all-history identity/novelty and queued holds. Ranking is the **opportunity score out of 100** (`opportunity-score-v3`, owner weights want 20 / early 30 / room 25 / product 25; settings `config/research/opportunity_score.json`): cases cover each part's boundaries, the waits `fit_information_pending` / `crowding_pending` / `demand_too_quiet`, the four labels, Low/Medium crowding before High (High only while fewer than ten Low/Medium types are ready for the issue), the in-group tie order and the one-time calibration after 50 scored types. The old four priority tiers and site counts are retired. Zero substantiated supplier leads after a completed cursory search remains acceptable; suppliers never add points.

## 7. Repeat supply and capacity measurement

Measure current checkpoint separately from a later collection. Freeze prior concept/query/history baseline; choose next questions using actual feedback; report new-to-research, carryover, new-to-client, duplicate-heavy/unrelated output and semantic uncertainty. A same-day pair proves rotation/replay, not recurring market replenishment.

At the legacy seven-of-thirteen checkpoint 11/156=7.05% ready with 38 unfinished; this is historical and not measured yield under the current checklist and score. Current yield is measured as scored, report-eligible types per hour of research over at least three hours (the W13 live check), with its own denominator. Use 5% planning/3% stress scenarios without promising either floor. Include failed/rejected research, read holds, quick-commerce credits, update validation, supplier/model effort, new-source mix and inventory depletion in costs.

A representative approved weekly rehearsal aims at three distinct issues of 10–20 products (target 15; `config/service.json`), minimums before extras and>=80% New each. First complete approved issue can proceed independently; shortfall elsewhere stays visible. Filling one issue continues only the remaining finite research allowance.

Generate actual Tuesday/Thursday/Saturday 09:00 India dates, including 12–14 monthly releases and asynchronous account periods. Check source expiry at intended availability; retained archives are historical, not refreshed daily. No multiplication of one weekly capture solely because it supports three files.

Test first-two bilingual ordinals/cancellation; third language-pending facts and workbook-only finishing path. Use isolated simulated history for tests, never manufacture live publication history.

## 8. Findings and decision rule

Each experiment/rehearsal reports actual attempts/results/yield/cost/time, concrete defects, known uncertainty, independent work available and next scoped action. If volume and approximate funded headroom conflict, discuss actual figures and options; no weak padding, automatic short publication, relaxed novelty/checklist, paid upgrade or invented supplier/market facts.

Store detailed evidence in existing trial/research/job/provider directories under the working-data folder (logical `data/`, physically `/home/anan/neuraflow-dropshipper-data/`). Record new measured results in the implementation record and current status in Progress; the benchmark record keeps the earlier checkpoints. Do not create narrative documents per correction. A successful trial is evidence for its route only; a full service still needs its explicit integration and real-output acceptance.
