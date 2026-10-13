# First Meta trial: implementation and review

Date: 3 October 2026. Scope: section 17 of the [existing execution specification](../operations/benchmarks/2026-10-03/execution_specification.md). Implemented route, not a completed live benchmark or full application.

Historical checkpoint: this review predates the latest owner instruction to reuse the existing key. Its missing replacement/switch-back tasks and “no key read” statements describe that earlier block only. For current authority, actual authenticated checks and next actions use the [credential-reuse review](2026-10-03_existing_key_execution_review.md) and [account register](../operations/providers/apify_environment_handover.md). No replacement/restoration task remains.

## Objective, inspection and authority

One operator, one client, local weekly research, ₹1,500 client monthly charge. The owner requested a detailed next-block plan, rigorous review and implementation while away. Inspected the current contracts, journal/lock, account helpers, diagnostics, CLI, fixtures and current operational documents before changing them. All original 85 tests passed first. Approved preview generators/files, other keys, ChatGPT sign-in and Git metadata were out of scope and preserved.

No real key file was read, changed or copied; no testing-account confirmation was invented. No Actor/model/commerce request, credit use, purchase or account creation occurred. Only unauthenticated public Actor/build metadata was reread. The ordinary command environment could not resolve the host; a narrowly approved public-read command succeeded. It used no authentication and could not launch an Actor. Pinned build `2.7.26`, URL-object schema, LIMITED_PERMISSIONS and the current PAY_PER_EVENT ad/start prices still match the reviewed configuration. Public metadata is not authenticated execution/output proof. [Build definition](https://api.apify.com/v2/actor-builds/wYDFs3xTPOVCo9Wag), [Actor pricing metadata](https://api.apify.com/v2/acts/curious_coder~facebook-ads-library-scraper).

## Implementation

- Existing specification section 17 now contains objective/exclusions, file map, account handover, request-bound preflight, cost contracts, worker deadlines/privacy, durable start/resume, original-byte capture, review, exact actions, tests and continuation boundaries.
- `models.py`/`storage.py`: strict duplicate/nonfinite JSON rejection, atomic byte saves, separate Actor/read commitments, validated accounting/review transitions and non-mutating budget preflight. Legacy foundation reservations remain compatible.
- `http_io.py` plus `scripts/apify_http_worker.py`: standard-library, one-operation process; token through input only, curated HTTPS paths, no redirects/proxy inheritance, bounded original body, parent deadline and narrowly owned cleanup. No worker pool/framework.
- `accounts.py`: genuine handover required before token read, optional API identity or explicit Console-ID fallback, stable actual-account namespace, fresh access/balance, independent/shared provision and no silent key fallback/reset.
- `providers/apify.py`: curated documented reads, one durable POST boundary, response bytes saved before decode, provisional optional text/link fields with no fetching/execution.
- `benchmark.py`: one first-trial controller, 360-second status/480-second overall networking window, safe unique recovery, paginated original-byte capture/progress reuse, actual Actor bill versus reserved read amount, structured offline review and deliberate accounting resolution.
- Existing CLI: offline prepare/status/replay default; explicit `bind/preflight/run/resume --live`; saved-artifact `review/settle/resolve`. Missing/mismatched confirmation stops affected live work. No source/report/AI expansion.

## Review findings and corrections

All cases below are code/contract findings exercised with fictional evidence, not reproduced customer/provider incidents.

| ID | Actual risk; expected behavior | Correction and repeat proof |
|---|---|---|
| META-01 | Actor cap alone could be represented as all-in cost; zero/missing billing could release unknown spend. | First request commits US$0.03 Actor plus US$0.02 read provision inside unchanged stage/trial caps. Authenticated terminal actual is separate; nonzero charged events with zero actual stay unknown. Failed jobs retain bill; both overruns block acceptance/spending. |
| META-02 | Hourly owner key acknowledgements, same-key refresh prohibition or renamed identity could create friction/reset funds. | Fresh balance/access, persistent acknowledged key until it changes; stable provider-ID namespace, same-key refresh allowed, original baseline retained. 403 needs Console user ID, not invented alias. |
| META-03 | A socket timeout would not bound DNS/body stalls; worker output/env might leak a key or unrelated processes might be stopped. | One owned subprocess with private input, discarded output, environment allowlist, parent timeout/cleanup. Real offline child timeout/argument/env/result tests, no actual sockets. |
| META-04 | Lost POST replies could be resent or matched by time alone; launch freshness could prevent recovery. | Persist intent/raw receipt, never retry POST; exact account/build/body/tag/default-input unique match or hold. Existing-run recovery ignores stale launch approval; expired periods permit status but require storage-funding review. No/multiple/incomplete matches preserve uncertainty. |
| META-05 | Restart echo absence could be falsely rejected; later unsafe controls or failed quality could hide a bill/data. | Sent false setting and actual stats distinguished; absent echo allowed, explicit bad controls/restart rejected. Known terminal cost/useful rows preserved independently. Later controls drift cannot restore acceptance from an earlier good echo. |
| META-06 | Interrupted/corrupt pagination could repeat paid reads or turn missing/rejected rows into an empty observation. | Original page bytes/headers/hash saved first, validated progress/orphan reuse, total/offset/limits/size caps, corruption hold without recollection. Empty, rejected, failed, incomplete and truncated states remain distinct. |
| META-07 | Quality success could be fabricated from counts; failed/no-dataset accounting could become permanently stuck; recovery read fees could be erased after confirmed no submission. | Reverify original pages/aggregate/pure mapping and structured inspection before acceptance. Separate offline settlement and deliberate non-submission proof include verified read costs. FunctionalPass is first-sample-only and blocks known missing proof/overruns. |

Review loop: plan critique → dependent implementation → original regressions → complete fake workflow/failure tests → correction of observed formatting, controls drift, no-output settlement and non-submission read-accounting paths → full/file-system repeats → documentation reconciliation. Final results are recorded below after the last code revision. No finite review can guarantee an error-free product or third-party reliability.

## Proportionate architecture and continuation criteria

Applied vanity-engineering-review: reused the existing lock, contracts, journal, CLI and Python environment; added one trial controller and only the HTTP process boundary needed for blocking requests. No database, hosting, scheduler, plugin layer, account rotation manager or runtime dependency. Requirement-to-complexity assessment: **4/10**, subjective rather than measured, concentrated in billing/interruption proof rather than product features. Maintenance hours are not yet measurable.

Top simplification findings, all V1 risks: HTTP boundary in `http_io.py` avoids a general worker/service framework (resolved with one subprocess, zero further removal hours); identity in `accounts.py` avoids account alias/rotation machinery (resolved with stable provider ID, zero further removal hours); billing in the controller avoids cross-month migration/refund engines (not introduced, zero removal hours, only targeted review at a genuine funding boundary). These are avoided complexity risks, not claims that an existing framework was deleted. No invented future-scale capacity or monthly time savings.

Hard question: would removing this helper lose demonstrated one-time launch, funding safety or evidence recovery? If not, remove/defer it. Reassess controller size before the next source; do not copy the orchestration into every adapter or generalize it ahead of tested requirements.

Implementer: Codex. Continuation/authority owner: Manan, distinct from implementer. Evaluation: first live sample, then T1–T7 before the weekly build, not an invented thirty-day/daily monitoring programme. Success threshold: one source-relevant, fully inspected, coherent first sample with exact one-time launch/recovery and reconciled Actor/read amount inside approved bounds; this is a stage gate, not full T1. Measurement: saved verified artifacts, journal and benchmark record.

Hard stops for affected live work: absent/mismatched handover/key/account/preflight, unknown submission/cost, corrupt finance/evidence, exhausted trial/account/current-balance limit, control/identity defects or unsafe paths. Do not erase data or shut down the project. Review triggers: empty/unusable sample, schema/pricing drift, overrun, insufficient future funding or provider-period expiry before new storage reads. Thresholds are the existing first Actor US$0.03/read provision US$0.02, Meta US$0.20, trial US$0.75 and monthly ₹750 target/₹1,000 review/₹1,500 accepted maximum; not new spending approvals. Owner decisions apply when scope/funding changes. Restore production key explicitly before production preflight.

Unrelated team staffing, hosted latency dashboards, autonomous decommissioning and daily shutdown rules are not applicable to this local single-operator block. They are not claimed as achievements. Backups/device-level opening, live schema/relevance, provider billing/output access and production acceptance remain later actual evidence gates.

## Remaining boundaries

Real test-account plan, balance/provider period, independent funding and replacement acknowledgement are absent; no live command has been executed against an account. T0 remains Partial and T1–T7 Not started. Price-sensitive/storage work spanning a new provider period needs explicit funding reconciliation rather than automatic migration. Financial corrections/refunds and damaged-journal repair remain separately reviewed targeted work, not automatic rewriting.

Provisional Meta field mapping and optional snapshot display fields need actual row inspection. No automatic India targeting claim, relevance ranking, product profitability assessment, supplier scraping, image downloads or whole weekly report is implemented by this block. Other stage adapters/AI/live templates remain in the existing approved order.

## Verification

All fake accounts/credentials and worker responses are labelled fictional and kept in isolated temporary folders; no fake owner confirmation enters actual private configuration. Native Excel/phone behavior, power-loss durability and real provider behavior are not covered by these tests.

Final proof on 3 October:

- `.venv/bin/python -m unittest discover -s tests -q`: **145 passed**, including the 85 baseline/regression checks and 60 new first-trial cases. Fictional complete workflow, lost/ambiguous receipts, owned-worker timeout/secret boundaries, stale/incorrect accounts, known/unknown/overrun costs, pagination/interruption/corruption, original bytes, empty/failed samples, controls drift, offline settlement and review acceptance were exercised.
- `RADAR_TEST_STORAGE_ROOT='data/benchmarks/2026-10-03' .venv/bin/python -m unittest discover -s tests -p test_benchmark_foundation.py -q`: **52 passed** on the actual project D: filesystem.
- Same isolated D: command with `-p test_meta_live_trial.py`: **60 passed**. These test sync/replace/lock/restart behavior on this filesystem, not simulated power loss. Fictional temporary folders were automatically removed; no customer/research files were deleted.
- Compile checks for the package, CLI, worker and new tests: passed. Help exposes only reviewed actions/flags; default and replay remain offline.
- `scripts/benchmark_providers.py status`: zero saved real provider attempts; local records permit handover; no private key read/network request. This does not inspect unrelated remote account work.
- `scripts/benchmark_providers.py replay --fixture meta_empty`: passed, explicitly fictional, not source evidence.
- Full-suite document links/layout, ignored private-artifact rules and approved brochure/support regeneration checks passed. Preview regeneration was into temporary folders and byte-identical; actual approved outputs/generators were not edited.
- Header/body credential-reflection fixtures confirm that a reflected fictional key is not saved in response metadata or parent output. Tokens are never process arguments or inherited environment values.

No further material defect was found in the exercised first-trial scope after the corrections and repeats. This is implementation evidence, not an enterprise certification, guarantee of no bugs or acceptance of third-party source quality. The first actual request remains held at the genuine account boundary.

Next owner action: open the authorized testing Apify Console's Billing/Usage screen and report plan/remaining credit, or a cropped nonsecret screenshot. Keep the current key unchanged until Codex gives the private replacement step. Then bind/preflight/one capped run → inspect every real row and actual billing → remaining Meta discovery/validation → Amazon/Trends/commerce → representative Luna/suppliers → T7 build decision. This order tests the fragile source/cost assumptions before the full weekly build.
