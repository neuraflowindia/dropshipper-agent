# Existing-key execution: changes, proof and remaining facts

Date: 3 October 2026. Scope: current owner instruction, first Meta trial only. Latest authority: reuse the existing Free account/key; no replacement or production switch-back task.

## Outcome

The first-trial path is already implemented; this pass removed its outdated replacement wording/gate, preserved financial and evidence safeguards, rechecked actual scoped access and reconciled the operating documents. The real Meta launch has not occurred. Missing current account/billing facts are a genuine factual boundary, not an unresolved permission to implement or spend within the approved caps.

One operator, one client, local weekly service and ₹1,500 monthly client charge remain unchanged. No hosting, database, account-rotation machinery, new dependency, paid plan, AI/model change, commerce purchase or preview redesign was added. Supplied logo/media, approved client files, other credentials, ChatGPT sign-in and Git metadata were preserved.

## Inspection, changes and review findings

Before changes, inspected existing account contracts/parser, transport/HTTP allowlist, CLI, controller preflight, public template, tests and current plans/setup records. Original full suite: **145 passed**. Then critiqued the reuse route, implemented targeted changes and repeated acceptance checks.

| Finding | Correction | Proof and boundary |
|---|---|---|
| A replacement-specific acknowledgement contradicted authorization to reuse the saved key. | Private handover v2 uses `owner_credential_confirmed`; CLI messages/preparations refer to credential use, not a required change. Version 1 can be read without rewriting or promoting false approval. | Fictional unchanged-key bind, unchanged file bytes, missing/false confirmation, legacy confirmed/unconfirmed tests. No fake confirmations entered actual configuration. |
| The entire providers file's modification time could wrongly invalidate a confirmed Apify token after editing another provider. | Compare the already confirmed binding's token digest privately before treating a newer file timestamp as a changed credential. Prior owner/access flags must be true. Unbound/different tokens still need current approval. | Fictional unchanged-file-touch test preserves generation/namespace/baseline; changed-token test blocks before network and retains original binding. Actual key file not edited. |
| API billing/profile denial could provoke a broad-access key or be misreported as an invalid credential. | Check scoped run-list access separately; add only optional exact current-period usage GET, no query/body, 512-KiB bound, existing worker/privacy/deadline. Use Console facts when denied. | Real run-list success alongside profile/usage HTTP 403; fake transport method/route/query tests. No permissions changed. |
| Reusing the account could be treated as independently funded testing or enough credit for the entire trial plus production. | Current route explicitly shares allowance. Preserve actual-account/period identity, production/emergency provisions and aggregate stage/trial caps. Explain US$0.40 headroom for a US$5 balance under current provisions. | Existing financial regressions retained; arithmetic reviewed. Current numeric balance/renewal still unknown; no allowance fabricated. |
| Old next-step/restoration instructions conflicted across plans and operator records. | Reconcile current index, brief, activation, setup, account register, execution/benchmark plans and live record. Prior checkpoint reviews are explicitly historical. | Relative-link/layout checks and targeted text review; current instructions do not request replacement. |

Prepared request body/build/cap/hash remains unchanged. New preparations use neutral credential-check labels. An older saved preparation's descriptive unresolved-check labels are historical; they are not a required key-change action. Submission remains at most once; no retries of an uncertain POST, silent budget reset, automatic scope widening or fabricated provider facts.

## Actual account checks

Three bounded authenticated reads used the existing key, around the 23:00 UTC checkpoint. `/v2/users/me` returned HTTP 403. `/v2/actor-runs` succeeded with zero returned entries. `/v2/users/me/usage/monthly` returned HTTP 403. The key therefore works for run-list access, but current identity/billing details are unavailable through these routes. Do not infer exact account usage/remaining credit from an empty run list.

Restricted command networking first failed DNS. Narrowly network-enabled read commands completed; they did not broaden account permissions. No Actor was started or Actor charge generated, no monetary reservation created, no complete private account body/key/private ID printed or retained. General provider credit usage was not independently reconciled, so do not label all reads proven zero-cost. No actual handover, binding or profile was populated with guessed facts.

The [documented current-period usage endpoint](https://docs.apify.com/api/v2/users-me-usage-monthly-get) reports accumulated usage and cycle dates, not an independently guaranteed remaining-credit value. The [official billing guide](https://help.apify.com/en/articles/13671754-navigating-apify-billing) identifies Billing → Current period for current usage. API denial does not justify changing account permissions just to avoid one Console check.

## Proportionate-design review

Applied vanity-engineering-review to the current single-client/local scope. Subjective requirement-to-complexity assessment remains **4/10**; maintenance hours are unmeasured. Top simplification findings: V1 unnecessary replacement journey removed (no further removal work); V1 whole-file-timestamp false roadblock corrected (no further removal work); V1 temptation to add account rotation/billing-write machinery avoided (zero removal hours, not introduced). These are qualitative findings, not measured savings.

Hard question: does this requirement protect one-time spending, correct account ownership or usable evidence, or merely create a setup task? Remove the latter; retain real factual checks. Implementer: Codex. Continuation/scope owner: Manan. Evaluation is the first actual capped sample and then T1–T7, not an invented daily-monitoring or thirty-day shutdown programme.

Continue once genuine ID/current credit-period/balance facts plus fresh request preflight pass. Pause affected chargeable launch on unknown identity/funds, corrupt evidence/finance, unresolved job/cost, changed unsafe controls or exhausted budget. Do not shut down unrelated work. After the first sample, inspect every returned row, scope/relevance and actual Actor/read costs; a useful result is not automatically a completed Meta benchmark. Reassess collection bounds and period scheduling if shared-account headroom is insufficient. No automatic purchase or reserve removal.

## Verification and next action

Final verification after corrections:

- Full suite: `.venv/bin/python -m unittest discover -s tests -q` — **154 passed** (145 baseline plus nine new cases).
- Actual project-drive isolated trial repeat: `RADAR_TEST_STORAGE_ROOT='data/benchmarks/2026-10-03' .venv/bin/python -m unittest discover -s tests -p test_meta_live_trial.py -q` — **69 passed**. Its 45.84-second runtime is offline filesystem testing, not live research runtime.
- Same project-drive foundation repeat with `-p test_benchmark_foundation.py` — **52 passed**. Fictional temporary folders were cleaned up by the tests; no customer/research data was removed.
- Compile checks passed; offline status shows **zero saved provider attempts** and fictional `meta_empty` replay passed. No credentials/network are used by these two commands.
- Document links/layout, private-file ignore checks and approved preview/support regeneration passed in the full suite. Regenerated previews stayed in temporary folders and matched existing approved files; client files were not changed.
- Shared-account boundary test confirms US$4.65 permits first-sample preflight and US$4.64999 is rejected with `CURRENT_BALANCE_INSUFFICIENT`, with no POST in either case.

Intermediate development checks caught references to the not-yet-created review file and an incorrect expected error name in the new boundary test. The review target was created and the assertion corrected to the existing, correctly returned fresh-balance error; final repeats passed. No guard was weakened to make tests pass. Original-byte capture, recovery, finance and privacy regressions remain passing. Offline tests are fictional provider evidence, not actual output/storage/source-quality or power-loss proof. No further material defect was found in the exercised changed scope; no bug-free/enterprise certification is claimed.

Owner's next single action: open the current Apify Console → Billing → Current period and send a cropped allowance/usage and renewal-date screenshot, hiding email/payment information. No key change or further Free-plan approval. Codex will interpret it, then guide the missing stable User ID if needed using the actual settings screen; the owner does not fill JSON or run commands. [Account and exact-step register](../operations/providers/apify_environment_handover.md).

Once facts are available: existing-key binding → fresh preflight → one saved first-Meta request (US$0.03 Actor plus US$0.02 read provision) → original-byte capture → actual row/cost review → remaining Meta → Amazon/Trends/commerce → representative Luna/suppliers → T7 build decision. This order proves the vulnerable source/price assumptions before the full weekly application. T0 remains Partial; T1–T7 Not started. No new key/restoration task remains.
