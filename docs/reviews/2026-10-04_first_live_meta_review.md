# First live Meta sample: implementation and evidence review

Date: 4 October 2026. Scope: one approved first Meta sample and corrections revealed by its actual output. Not the complete weekly application, all of T1, or a production release.

## Outcome

Account setup is closed. Console screenshots and private User ID were received; genuine account binding and request-specific live preflight passed. The saved request was launched once with the unchanged key. It succeeded and supplied 27 ads and 37 creative cards. All were inspected; the corrected saved-data mapping and structured review passed. First-sample verdict is `QualityPass`; exact post-run read cost is unknown with US$0.02 retained. T0 preparation Pass; T1 In progress; T2–T7 Not started.

The [results record](../operations/benchmarks/2026-10-03/benchmark_record.md) contains the actual times, request bounds, observations and remaining work. The [execution specification, section 18](../operations/benchmarks/2026-10-03/execution_specification.md) records the corrective design before implementation. No new launch was made to fix display fields.

## Actual evidence, not assumptions

- Genuine binding uses Console-confirmed User ID because optional identity API access is denied. Account facts/binding remain in ignored `config/local/`; no token, token digest or account ID is copied here. Period dates have day precision; 2 November 00:00 UTC is a conservative funding cutoff, not a claimed exact renewal hour.
- First run: 05:42:42.464–05:42:55.047 UTC, elapsed 12.583 seconds. Successful status, expected build `2.7.26`, timeout 300 seconds, requested charge bound US$0.03 and restart disabled were observed. This does not prove weekly runtime or tested remote cancellation.
- Output: 27 parent ads after five requested, one complete page, no capture truncation; 37 nested cards. Every query reference carries IN and every returned active flag is true. Per-ad audience-country fields are empty; actual India targeting remains unverified. Dates and active flags are observations, not lifetime/ad-spend estimates.
- The authenticated terminal response reports Actor charge US$0.00005, one charged start event, zero charged ad events. Do not extrapolate free ads or monthly costs from this atypical observation. Exact separate read cost is unknown, not zero; its US$0.02 allowance remains reserved. No rounded balance, rate estimate or seller claim is recorded as verified actual cost.
- Useful discovery examples include sink organisers, spice jars, countertop shelving, storage bags and kitchen labels. App promotions, interior-design services, mixed catalogues and adjacent products are also present. All 27 are ads, not 27 selected products. Two Geariva ads share one advertiser/product family; repeated cards do not add independent evidence.
- Seller destinations, image contents, advertised prices/claims, suppliers and profitability have not been independently verified. Saved image links are references only. This sample is enough to continue discovery testing, not recommend client investment.

## Findings and corrections

| Finding | Practical consequence | Implemented correction | Proof and remaining boundary |
|---|---|---|---|
| Parent template titles hid concrete nested products | Useful candidate evidence was present in raw output but absent from normalized display fields | Small display helper for advertiser/text/HTTP(S) links, nested cards with original indices, template flags; 20-card display bound with explicit total/truncation | Original raw bytes/hash unchanged, initial derived mapping retained. First sample retains 27 parents and zero rejections. No card is promoted into an independent ad |
| Exact microscopic read settlement blocked every next sample | Scoped billing access and cent-rounded Console values would create a recurring owner dependency despite full fee provision | Separate data acceptance (`QualityPass`) from complete accounting (`FunctionalPass`); shared predicate for preflight/run | Unknown read hold stays fully reserved; settled overrun, bad controls, failed/empty/truncated/uninspected/corrupt data still reject acceptance/continuation. No invented financial evidence |
| Buffered command progress | The owner could see silence while a short external job ran | Flush each controller progress message immediately | Normal CLI still prints only safe status/path/check messages, not secrets or raw private response bodies |
| Current guides still requested User ID/binding and claimed no actual sample | A resumed session could repeat completed setup or misstate evidence | Reconcile current index, plan, register, setup, brief and result record; label old checkpoints historical | No new owner setup action. First sample and full stage/production remain distinct |

## Verification

First the existing 154 tests passed; after initial corrections and 15 new cases, all 169 passed. Final review added explicit rejection tests for unperformed manual checks and unresolved defects, and extended capture-truncation coverage. Final outcomes: **171/171 full offline tests passed**, **86/86 first-Meta tests passed with isolated storage on the actual D: project filesystem**, and **52/52 foundation tests passed on D:**. Compile, help, offline status and fictional fixture replay passed. All four documentation-layout/approved-preview-regeneration checks passed; existing preview sources/support documents remain byte-identical in isolated temporary output. D: tests establish exercised file/lock behavior, not power-loss proof. An intermediate suite ran while the new review document was still being created and reported seven unresolved links to that file; once the target existed, final full and documentation runs passed. No provider call was used to fix those links.

Saved capture verification independently rebuilt aggregate rows from original HTTP pages, checked pagination/hash/count/completeness, and compared regenerated mapping with the saved revised mapping. Original HTTP and aggregate bytes were compared before/after the derived-field revision and were unchanged. Offline actual `review` passed; no additional provider request or charge occurred for this re-review. Private `row_review.json`, `mapping_revision.json`, `mapped_rows_initial.json` and `operator_review.json` retain judgments/provenance.

Live proof covers this one successful launch/read/schema. Fictional tests cover recovery/failure paths; they are not real outage/fee/renewal/cancellation observations. Existing approved client preview files, other provider credentials, ChatGPT sign-in and root Git metadata remain unchanged. No purchase, hosting, new dependency, wider permissions or automatic client delivery.

## Simplicity assessment

The vanity-engineering-review skill was used to check that safeguards serve the approved product, not an elaborate provider framework. Requirement anchor: one operator, one client, one local weekly service at ₹1,500/month; retrieve evidence, distinguish product leads from noise, preserve provenance, create readable outputs and permit safe recovery. Codex implements; Manan operates and decides release. No multi-tenant scale or new staffing is assumed.

Requirement-to-complexity ratio: approximately **4/10**, a subjective scoped assessment rather than a measured quality score. Durable submission/accounting guards are justified by real external charges and interruption risk. They must not grow into unnecessary account-management infrastructure.

Top simplicity findings (all corrected in this block):

- **V1 — billing precision bottleneck**, `benchmark.py`/`storage.py`: making useful research wait for tiny exact read fees offers no benefit while those fees are fully held. Simpler alternative implemented: quality acceptance plus unchanged reservation. Remaining removal effort: 0 hours; do not remove the accounting hold.
- **V1 — incomplete presentation of captured evidence**, `providers/apify.py`: adding a separate creative service would be unnecessary. Simpler alternative implemented: one private helper shared by parent and card display, no new dependency. Remaining removal effort: 0 hours.
- **V1 — stale operational instructions**, current docs: duplicated outdated setup steps create repeated work. Simpler alternative implemented: current records point to one account register and retain dated historical checkpoints. Remaining removal effort: 0 hours.

Vanity-debt maintenance hours/month are **not yet measured**; reporting zero would be unjustified. Record real correction/manual work during the remaining benchmarks before estimating service profitability. Hard question: can the operator get useful weekly recommendations at this price without repeatedly rescuing collector output? A successful 12.6-second sample does not answer that; representative sources, candidate matching and end-to-end rehearsal must.

## Stop, review and continuation criteria

Evaluation window: this first sample, then remaining T1–T7 and pre-delivery rehearsal. Implementer: Codex. Enforcement/release owner: Manan. Existing trial ceilings: Meta US$0.20 and initial Apify total US$0.75, including corrections/read provisions. Client charge ₹1,500/month is not a provider-only spending ceiling; production cost targets/review points remain in the activation record. No new numeric SLA, staffing target or monthly maintenance limit is invented.

Hard holds apply only to affected chargeable work: corrupt journal/capture, uncertain possible duplicate submission, invalid execution controls, unknown Actor charge, known cap overrun or insufficient funds. Preserve evidence; recover the original attempt where possible; keep independent offline work available. Unknown read cost alone does not hold continuation when its full allowance is reserved and quality accepted. Secret exposure requires stopping that credential's use and targeted owner-guided remediation, not broad shutdown/deletion.

Review before expanding adapters if output cannot separate product leads from services/noise, candidate identity remains ambiguous, manual intervention defeats the lightweight promise, or provisional earmarks obstruct useful testing. Prefer a simpler bounded input/filter/capture correction; do not buy access, widen permissions, silently increase caps or create a new account to avoid an issue. Report the actual restriction and what can continue.

Continuation requires useful inspected evidence, unchanged original provenance, successful verified controls, retained funding and no unresolved review defect. Full T1 needs remaining discovery and product-specific validation; T7 needs measured representative source/analysis/supplier outcomes. Production requires the operator rehearsal, matched Excel/PDF, backup/restore and owner release/delivery review. Manan has not yet independently accepted this new live outcome.

Anti-complexity rules: a new abstraction needs multiple concrete consumers; a new dependency needs a written problem/simpler-alternative justification; each design change must say what could be simpler; no speculative account/platform framework for one operator. Operator documentation must remain understandable without technical JSON. Daily traffic, uptime/latency SLAs, staff bus-factor, 30-day inactivity shutdowns and automatic decommissioning are excluded because this is a manually started weekly local service, not an always-on multi-team platform. No review criterion authorizes destructive cleanup.

## Recommended next block and how

1. Plan the remaining Meta inputs against observed data: modest category breadth where evidence supports it, then narrow product-specific searches. Keep services/noise separate, split catalogue product leads by card, and count independent advertisers rather than ads/cards.
2. Prepare immutable bounded requests; run fresh unchanged-account/balance/build/schema/pricing checks and reserve each attempt. Do not replay the first launch or call old balance evidence fresh. Inspect returned rows and retain original evidence/unknown-cost holds. No new owner setup action is currently needed; explain any genuinely necessary fresh funding fact when requested.
3. Finish T1's representative discovery/validation before testing Amazon/Trends, commerce, Luna analysis and supplier retrieval. Use measured quality/cost/manual effort to decide routes before the complete weekly engine, Excel/PDF integration and rehearsal.

Why: this first sample exposed real noisy output and useful nested product evidence. Testing narrow validation and independent evidence next prevents building polished recommendations on unrelated ads, duplicate cards or unsupported commercial claims.
