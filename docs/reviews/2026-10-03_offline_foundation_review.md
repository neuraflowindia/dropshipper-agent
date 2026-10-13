# T0 offline implementation and review

Date: 3 October 2026. This records implementation proof, not live source acceptance.

Historical checkpoint at 12:12 UTC. Later first-Meta CLI/recovery/capture implementation supersedes the capability limitations below; see the [current first-trial review](2026-10-03_first_meta_trial_review.md). Original test results/observations are preserved, not rewritten as current live proof.

## Requirement anchor and scope

One operator, one client, weekly local research, ₹1,500 monthly client price. Immediate job: prepare the first Meta request; prevent duplicate billing and erased spending after interruption/key changes; preserve useful data; distinguish synthetic checks from live proof. No hosting, database, scheduler, plugin framework, paid subscription or full report rebuild introduced.

Original 33 checks passed before editing. Existing keys were not read, changed, printed or copied. No Actor, commerce product search or Luna inference was executed. Only unauthenticated public Actor/build metadata was read: the restricted command environment initially could not resolve the API host; a narrowly approved public-read repeat succeeded. No authentication headers or private account access were involved.

## Changes

- `src/product_radar/models.py`: strict USD/IDs/time, private binding and reviewed Meta request contracts.
- `src/product_radar/storage.py`: one kernel lock, atomic saves, hash-linked append-only journal, account-period baseline, benchmark-wide caps, conservative reservations and safe recovery attachment.
- `src/product_radar/accounts.py`: bounded private binding load/save and key-change detection; no real binding created.
- `src/product_radar/providers/apify.py`: no-redirect bounded transport primitives, durable single-submission boundary and provisional saved-row mapping/capture; not a live collector command.
- `scripts/benchmark_providers.py`: prepare/status/synthetic replay only; default offline, no key dependency or live flag.
- `config/benchmarks/meta_first.json`: pinned public build/input/pricing facts and first US$0.03 request.
- Synthetic benchmark fixtures/tests and ignore rules for local research/run artifacts.
- Current documentation/account register now records the owner's company-wide Apify permission, without claiming independently inspected written approval or new credits.

## Review and correction register

All findings below came from code/contract review and synthetic reproduction, not reported live incidents.

| ID | Actual concern and expected behavior | Correction and repeat proof |
|---|---|---|
| OFF-01 | Account-local caps would let a different account reset the approved total. Trial/stage approval must span the benchmark. | Separate benchmark-wide totals from account-period availability; cross-account cap fixture rejects replenishment. |
| OFF-02 | A repeated capture could overwrite original evidence before a conflicting journal event failed. | Check committed data hash before replacement; conflicting capture test preserves original file. |
| OFF-03 | One-hour account binding alone would overlook external account use since a five-dollar baseline. | Fresh confirmed balance in request-bound preflight; current-balance reduction test blocks, settled charges are not deducted twice, pending reservations remain protected. |
| OFF-04 | Caller token and transport token could differ, defeating the account check. | Verify transport credential matches the bound credential before reservation; mismatch makes zero requests. |
| OFF-05 | Unknown receipt, rejected controls or missing dataset could lose the remote ID or release spend optimistically. | Persist submitting state before POST and receipt IDs before returned-control review; uncertain state/cap survives errors; no automatic retry. |
| OFF-06 | Saving a prepared file before failed budget checks could strand an otherwise safe retry. | Reuse only identical unsubmitted prepared input; after the missing budget is confirmed, exact retry submits once. Different input remains a conflict. |
| OFF-07 | Missing dataset on a genuinely failed run could permanently block handover even after proven no output. | Separate capture status; verified no-dataset closure needs terminal status and cannot bypass an existing dataset. No fabricated empty rows. |
| OFF-08 | Unvalidated manual recovery evidence could attach a run from the wrong account/config. | Bounded saved unique-match evidence and original account/generation/config/run binding required; wrong account rejected; synthetic lost-receipt recovery uses original run without repost. |
| OFF-09 | Float parsing intended for money could make fractional dataset values unsaveable; JSON NaN could pass through. | Money response numbers parsed as decimals; source-data copy uses ordinary JSON numbers; nonfinite JSON rejected. Fractional fixture captures successfully. Original HTTP bytes still need the live capture route. |
| OFF-10 | Boolean schema versions, malformed transitions or torn records could be silently treated as valid history. | Strict integer versions/sequences, hash linkage, transition checks and bounded log reads; corruption preserved and appends blocked. |
| OFF-11 | PID/age-only stale-lock handling risks concurrent starts or unrelated process interference. | WSL kernel lock owns concurrency; metadata only diagnostic; separate-process contention test blocks while active and releases afterward. No lock unlink/process kill. |
| OFF-12 | Duplicate ads/conflicting identities, missing dates or scraper error rows could inflate conclusions. | Duplicate references retained, separate advertisers retained, conflicting IDs quarantined, fields nullable, empty observation distinct from rejected rows, no final signal produced. |
| OFF-13 | Requiring already-proven storage reads or returned run controls before the first-ever run creates a circular gate and a misleading claim. | Preflight explicitly records reviewed output/control routes; actual returned controls/read access are verified after the one capped smoke request. Old overstated preflight fields rejected. |

## Proportionate architecture review

Applied the vanity-engineering-review skill to keep this implementation tied to the actual local service. Subjective requirement-to-complexity ratio: 3/10, not a measured performance score. No general interfaces/plugins, distributed service, second configuration language or third-party runtime dependency was added. The financial journal exists to prevent duplicate spend and preserve uncertain work; it is not a general event bus.

Top simplification findings: V1 risk of writing our own stale-PID manager was removed by using the existing kernel lock (small same-block change); V1 risk of treating tentative provider mapping as the full future evidence model was avoided by marking it provisional and deferring creative/relevance/report logic (no downstream framework added). No known ongoing vanity-maintenance hours can be measured at this pre-live stage; estimating monthly hours numerically would be speculation.

Hard question: does another abstraction prevent a concrete billing/data defect or help the next actual collector? If not, defer it. No substitute review by this same coding session is described as independent human acceptance.

### Stage-specific continuation and stop criteria

Owner: Manan, who did not author the implementation; Codex implements/tests safeguards and presents evidence. Evaluation window: T0 through T7 before weekly-build acceptance, not an invented thirty-day launch experiment. Approved initial Apify trial cap US$0.75; Meta US$0.20; first request US$0.03. Monthly client/cost decisions remain unchanged in the activation record.

Hard stops: missing/mismatched key/account/preflight; unresolved possible submission or cost; corrupted financial journal; exhausted trial/stage/account/current-balance limit; changed/rejected controls; unsafe local path or conflicting evidence. These stop affected charge-bearing work, not delete data, broadly terminate processes or shut down the project. Synthetic fixtures trigger and verify these guards before a live route is released.

Review triggers: provider cap overrun, live field/pricing/build mismatch, unusable sample, supplier-route failure or recurring cost above the existing ₹1,000 review threshold. Owner-directed review/reduced-scope decisions follow the existing plan; no new automatic purchase or arbitrary service shutdown is authorized.

Continuation gate: required offline checks pass; guided account handover succeeds; first live request has enforceable limits, retrievable/reviewed output and reconciled cost; T7 then proves the promised coverage/workload/cost before full implementation. Benchmark record plus journal is the measurement method. Checkpoint is after each T-stage; revisit simplicity at W1 and readiness at W4.

Anti-complexity rules: no new dependency without a concrete need and simpler-alternative review; no plugin/general framework for one actual implementation; no production/report code before its evidence gate; no extra framework for hypothetical multi-client scale. Team/bus-factor, latency dashboards, daily autonomous kill/decommission and external independent engineer criteria are inapplicable to this operator-run block, not fabricated achievements. Their omission does not substitute for owner acceptance and actual production evidence.

## Verification and limitations

Tests use temporary local folders, fictional credentials, mocked transport and a separate spawned lock-test process. The existing documentation tests regenerate approved brochure/support files only into temporary folders and compare bytes, preserving client outputs. Ignore behavior is checked in a disposable Git repository; workspace metadata is not initialized or changed. Final verification results are appended below after rerunning the final revision.

Remaining live work is explicit in the [operator guide](../operations/benchmarks/2026-10-03/offline_runner_guide.md): actual binding/balance, original response-byte capture, whole-stage deadline/status polling, automated unique-match inspection and actual provider/output quality. The current 30-second transport timeout is a socket-operation bound, not proof of an absolute whole-stage deadline. No live CLI is exposed until those paths are ready. T0 is Partial despite the offline sub-gate passing. No synthetic success is counted as T1 acceptance.

No further known defect remains within the tested offline scope after the recorded corrections. This is not a claim of an enterprise certification, bug-free product, reliable third-party service or completed application.

### Final proof — 3 October, 12:12 UTC

- `.venv/bin/python -m unittest discover -s tests -q`: **85 tests passed**; 33 existing plus 52 foundation cases.
- `RADAR_TEST_STORAGE_ROOT='data/benchmarks/2026-10-03' .venv/bin/python -m unittest discover -s tests -p test_benchmark_foundation.py -q`: **52 passed** on the actual project D: filesystem, in isolated synthetic temporary folders, all cleaned up automatically. These exercise append/sync/replace/locking/restart state here; they are not a simulated power cut or a hardware-durability guarantee.
- Default prepare command: first request saved, `ready_for_live=false`, no reservation/key/network/credit use. Artifact: `data/benchmarks/2026-10-03/meta-505a216a81354d7bbc4ffa320a3b3a4b/prepared.json`.
- Synthetic replay: Pass; status: zero saved provider attempts, no private key read, local records permit handover. That last result is not a claim that unrelated remote account activity was inspected.
- Compile checks: passed for new package, runner and foundation tests.
- Documentation links/layout, private-artifact ignore rules and approved preview regeneration checks: passed within the full suite.

The owner action is testing-account plan/remaining-credit confirmation first, then privately guided key replacement. Existing credentials and preview files remain untouched. Production-restoration reminder persists in the handover register. No paid collection, report generation, cloud purchase or account provisioning was performed.
