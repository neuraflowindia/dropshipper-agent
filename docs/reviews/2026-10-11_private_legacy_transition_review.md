# Private legacy report transition review — 11 October 2026

## Confirmed problem

The first two private reports can remain in the older product format even after
enough fully reviewed product types are ready for the new type-and-examples
format. The normal program keeps reusing those old reports and can reserve the
new types for a later report instead. This is not a supplier, image, permission,
or model-limit failure.

Private-only saving is not publication: the existing draft-only path leaves the
real reservation `reserved`, never `uploaded`, and does not write the client
release list. These old issues are therefore not protected against an ordinary
checked private replacement. Uploaded, published, and cancelled issues remain
protected.

## Isolated reproduction and receipt

Reproducer:

`data/operations/implementation-20261011/reproduce_private_legacy_transition.py`

Run from the repository root:

```sh
PYTHONPATH=src:tests .venv/bin/python data/operations/implementation-20261011/reproduce_private_legacy_transition.py
```

The original baseline run on 11 October 2026 exited successfully and confirmed
the old behavior recorded below. After implementing the repair, the same script
was changed to require a genuine first-issue upgrade and exact repeat reuse;
the original baseline findings remain preserved in this document. It uses disposable
fictional research in a temporary directory, a test model writer, a simple test
PDF renderer, and an in-memory private file store. No real provider, model,
research history, report, account, process, or publication was touched. The test
namespace declares `captured` only to exercise the ordinary private packet
validator; its records are still explicitly fictional fixture records. This is
not live acceptance or a release gate.

| Step | Actual result in the disposable fixture |
| --- | --- |
| Save the first two old-format bilingual reports through the normal program. | 2 complete revision-1 packets; 2 private byte-verification receipts; both real holds remain `reserved`. |
| Build new types through the actual accepted fixture writers. | 10 unrelated eligible types, with definitions, example memberships, query profiles, current ten-point reviews, observed market records, and completed supplier attempts. |
| Preview the existing allocator before the normal next pass. | One chronological functional-only preview gives the first issue 10 New products, all priority 1. Exactly 10 unique IDs are allocated; the second and later issues remain short. |
| Admit the new IDs through the normal discovery/follow-on path and advance. | All 10 IDs are in the program's reporting scope. The first two old packets remain revision 1, with identical package hashes and immutable bytes. |
| Inspect where the new types went. | The normal pass reserves all 10 to the third issue, `issue-default-2026-10-10`, with `language_pending`. |
| Repeat the normal reports function. | Both first issues are reused unchanged; their current-selection checks return no invalidation reason. |
| Check publication authority. | 0 publication-history rows and 0 release-index writes. |

The allocator preview must happen before the normal pass can reserve the new
cohort elsewhere. A preview made afterwards correctly respects the newly held
third issue and returns no available replacement for the first issue. That is a
fixture-order distinction, not a reason to disregard real holds.

## Cause and approved scope

Before the repair, `programme.py` had the following connected path:

1. `_programme_report_pool` enables the new format for ordinary work turns but
   retains valid older rows for compatibility.
2. `_current_report_selection` checks the selected rows, aliases, holds,
   ordinals, and input hashes. Unrelated newly ready types deliberately do not
   invalidate an otherwise current older selection.
3. `_reports` puts an unchanged frozen packet directly into the reusable work
   list. Only missing or invalidated issues reach allocation.
4. A later missing issue can therefore reserve the new cohort while the first
   two private issues remain permanently on the older format.

Relevant source at the time of reproduction:

- `src/product_radar/programme.py`: `_programme_report_pool`,
  `_current_report_selection`, `_reports`, `_functional_report_revision`, and
  `_private_staging`.
- `src/product_radar/issue_allocation.py`: `allocate_issues`,
  `choose_packet_cohort`, `_fresh_at_release`, and `reserve_issue`.
- `src/product_radar/private_drafts.py`: `stage_private_draft` and `_stage_files`.

The approved execution specification sections 26.9 and 26.14E require actual
new type-and-example production while preserving legacy readers and immutable
original packets. They do not permit relabelling old bytes, bypassing protected
issues, or endlessly revising a current new-format packet because unrelated
research changes.

## Narrow repair design

Status: the narrow repair is implemented after the coordinated `programme.py`
handoff. Its eighteen-case focused transition verification passed; the main
agent's final named connected group covers the last shared ownership-hint change.
This section does not claim that the live reports have been replaced or privately
downloaded.

1. Identify only unprotected, genuine old-format frozen issues, together with
   ordinary issues already needing allocation. Keep all real reservations,
   publication history, aliases, dates, and client rules intact.
2. Run one chronological allocation preview using only current, genuine
   functional-format eligible rows. This must be one shared preview, not one
   independent promise of the same ten products for each issue.
3. Carry only its complete, ready functional selections into the existing
   revision, asset/input freeze, and checked reservation path. Use those exact
   selections; a second mixed-format allocation could select legacy rows again.
4. Exclude those selected IDs from the remaining ordinary allocation. Reuse an
   unchanged old packet when its upgrade preview is short, held elsewhere,
   stale, or otherwise invalid. Do not manufacture holds for the preview.
5. Keep normal opportunity ordering within the new-format cohort. Do not lower
   fit/source rules or bypass the New/Updated limits to fill a replacement.
6. Preserve the original files and reservation until the checked replacement is
   accepted. A failed preparation must leave the old immutable bytes and hold
   intact. Once new format is current, unrelated growth must not trigger another
   revision or model run.
7. Never override uploaded, published, or cancelled issues, and never alter
   publication permissions, history, or the client release list.

Focused regression cases required before handoff: ten versus twenty new types
across two older issues and later pending slots; real opportunity ordering;
expired or elsewhere-held types; protected reservations; private-only saving;
repeat/cold reuse without revision or model churn; failed build preserving the
old bytes and hold; and unchanged ordinary legacy-only behavior. These are
focused fixture checks, not local or hosted release gates.

## Implemented behavior and review corrections

The source change is confined to `programme.py`'s report transition and nearby
private helpers. The existing household operation writer was also added to its
already guarded housekeeping entry, as separately requested by the main agent.
No provider, execution allowance, application policy, publication, account, or
schedule setting was changed.

- One shared chronological functional-only preview supplies exact disjoint
  complete selections. The unchanged ordinary allocator handles other slots;
  aliases and lineage overlaps from the selected preview are excluded there.
- A failed preparation leaves the actual old draft revision, input hash, files,
  and real reservation intact. Its separate issue-scoped `report_upgrade` retry
  is part of ordinary readiness and does not require a manual direct rerun.
- Reusing the old legacy packet cannot clear that waiting replacement. Only a
  genuine checked new-format hold clears the upgrade wait.
- The upgrade retry identity keeps selected IDs and order, date/client, identity,
  ordinal/language, source scope and exact producer hashes, fit/priority/status,
  example memberships, source-route projections, supplier terms/coverage, and
  actual support dates/expiry. It excludes generated freeze-cutoff wrappers,
  which otherwise change even when the underlying research has not changed.
- A genuine new input can survive a rejected reservation or a lost reply. An
  upgrade may recover that exact newer immutable input only while the actual
  own older reservation is still the exact supplied `reserved` owner. This is
  an explicit private current-selection check; the real hold is not removed or
  replaced in a simulated state. Every foreign hold, alias, ordinal, source
  currentness and freshness check remains active.
- Recovery also checks support at the current time and release time, the current
  logo/photo plan, original asset bytes and the original full dataset. It carries
  the original input and facts unchanged into ordinary freeze/reservation/writing;
  it does not copy old timestamps into a newly described report.
- Uploaded, published, cancelled, missing or different owners cannot use the
  replacement exception. Changes to sources, selected identity, terms, priority,
  language, ordinal or actual asset plan reject the stale proposal/reuse.
- Main execution and readiness use one shared newer-owner hint. It accepts a
  genuinely coherent newer reserved hold in either format, not just the new
  format: its actual selection hash must match the existing selection
  fingerprint, and both revisions must be valid integers in increasing order.
  The actual frozen-input and current-selection checks still run. An arbitrary
  higher hold with inconsistent selection bytes cannot hide the original
  mismatch, while a genuine legacy revision 2 can recover after its program
  metadata save was lost. This adds no duplicate research-history reads.

The independent report reviewer separately repaired ordinary readiness for a
genuine newer canonical hold hidden behind stale complete legacy metadata.
That remains local recovery, not an invented new model question. Completed
packets with only stale metadata can recover without a new model or PDF render.

### Failed focused runs and their resolution

These were actual failed checks, not successful acceptance receipts:

| Check | What happened | Resolution |
| --- | --- | --- |
| First ten-case run | 6 passed, 3 failed, 1 errored in 149.986 seconds. | The nominal twenty-type helper had only ten completed fit reviews; an external hold was dated between the first two issue slots, correctly changing their ordinals; and the expiry fixture advanced the clock for the legacy packet as well as new types. The fixtures now assert twenty actual eligible rows, keep the external hold later, and age only the intended new source records. A same-clock retry test was also moved to its actual retry deadline. |
| Corrected ten-case run | 8 passed, 1 failed, 1 errored in 159.594 seconds. | Both genuine twenty-type selections reached revision 2; prose still needed the next finite turn. The real defect was unchanged legacy reuse clearing the newer upgrade wait. The clear is now limited to a checked functional-format reservation. |
| After-freeze two-case run | 1 passed and 1 failed in 49.840 seconds. | A committed reservation with a lost reply already recovered correctly. An input saved before a rejected reservation was regenerated at a later cutoff; the producer-bound key and checked original-input recovery now reuse that actual saved revision. |
| Pure cutoff-key check | Failed in 17.627 seconds. | A later sixty-second clock changed the old `selection_hash`-only retry identity despite unchanged research. The new named-field key preserves real producer and expiry changes without binding generated cutoff clocks. |
| First corrected pair | 2 passed in 66.890 seconds. | Genuine twenty-type disjoint bilingual packets and failed-build ordinary readiness/wait/deadline recovery both succeeded. |
| Corrected key/recovery group | 3 passed in 67.058 seconds. | Later cutoff-key stability, saved input before rejected hold reusing the exact original revision, and failed-build scheduled retry all succeeded. |
| Full focused transition module, before the final downgrade repair | 16 passed in 274.537 seconds. | All sixteen cases listed below passed on the coordinated source fingerprint recorded here. |
| Four named existing authority cases | 3 passed and 1 failed in 10.149 seconds. | An arbitrary higher legacy hold with no authentic newer packet still preserved all bytes/holds and made no model/render calls, but the broad metadata override changed its diagnostic from `REPORT_RESERVATION_CHANGED` to `REPORT_FROZEN_SELECTION_INVALID`. A temporary functional-only guard restored that diagnostic. Final independent review replaced the format restriction with the shared coherent-selection-fingerprint guard, preserving both genuine legacy recovery and the existing mismatch refusal. Final shared-helper checks are separately recorded by the report reviewer and main agent. |
| Genuine already-functional invalidation, before no-downgrade repair | Failed in 34.463 seconds. | A real accepted definition changed, leaving nine usable functional types and ten freed legacy rows. The normal program wrote legacy revision 3 instead of retaining functional revision 2. |
| No-downgrade recovery plus existing different-reservation authority | 2 passed in 50.072 seconds. | The checked functional packet retains its real hold and exact bytes at a stable nine-type shortfall; one newly admitted genuinely reviewed type permits functional revision 3. The arbitrary different higher hold keeps its established refusal diagnostic. |
| Deliberately uninspected allocation | 1 passed in 0.004 seconds. | A different orphan packet's checked hold-adoption wait suppresses allocation. The program neither invents a zero-candidate shortage nor sends the functional slot through legacy fallback. |
| Final focused transition module, including no-downgrade and honesty | 18 passed in 305.734 seconds. | Includes all earlier sixteen cases, genuine functional nine-to-ten recovery and saved status, plus deliberately uninspected allocation honesty. This process imported before the last shared coherent-owner hint/main predicate substitution; the final connected group separately verifies that combined source. |
| Final shared-owner hint, independently checked by the report reviewer | 3 passed in 15.262 seconds. | Genuine legacy revision 2 recovers with zero extra model writing or PDF renders and then settles; malformed/incoherent higher holds are refused; the established arbitrary different-reservation diagnostic is preserved. Uses the final shared source fingerprint below. |

The sixteen-case module includes actual
unreviewed source replacement, a valid selected-alias review, changed current
logo, protected/missing owners, short/stale/elsewhere-held cohorts, private-only
staging, genuine twenty-type priorities, unchanged reuse and both post-freeze
failure boundaries. Its completed result is recorded above. The standalone
normal-caller script also exited 0 after the repair: the first issue genuinely
upgraded to functional revision 2, the second legacy issue remained revision 1,
repeat reused both, both old immutable packets stayed exact, and publication
history/release-index writes remained zero.

The independent final review subsequently confirmed one additional existing
problem: if an already functional packet loses current support and only nine
usable functional types remain, the ordinary mixed fallback can choose ten
available legacy rows and downgrade the issue. The implemented final correction
is confined to previously functional issue slots, identified by the saved input
or their actual canonical hold. The same one chronological preview now supplies
exact shortage counts as well as complete selections. An incomplete functional
slot retains its old immutable bytes and real hold, records a stable honest
shortage, and is removed only from legacy mixed fallback. It does not create a
new timed outage retry. Other legacy and pending slots keep working.

The genuine definition-change regression proves nine current functional types
plus ten available legacy products cannot downgrade the issue, that the shortage
survives an ordinary save/load, that unchanged readiness settles without model
or PDF work, and that admitting one genuinely reviewed tenth type wakes normal
work and creates a functional revision 3. The same final module includes a light
orphan-adoption case: a deliberately skipped allocation is not reported as a
zero-result search. All eighteen cases passed in 305.734 seconds; these two added
cases are not included in the earlier sixteen-case pass count.

Focused commands:

```sh
PYTHONPATH=src:tests .venv/bin/python -m unittest test_programme_legacy_upgrade
PYTHONPATH=src:tests .venv/bin/python data/operations/implementation-20261011/reproduce_private_legacy_transition.py
```

### Source loading boundary

The ordinary scheduled process launched at 11:00:46 UTC. An earlier failure
bookkeeping fix was saved at 11:00:58.368694300 UTC, after that launch. It is not
claimed that that process loaded the final fix. The coordinated final source
at 11:26:03.871057700 UTC has SHA-256
`b5fff139560c38f895bb895284298330c90cc917476defd64be8a928a6c0e207`.
Only the main agent controls or verifies any later safe process load. All counts
above are disposable fixture checks, not live report, restart or release proof.

After the final no-downgrade/honesty repair and the independently reviewed shared
coherent newer-owner hint, `programme.py` was stable at
11:46:18.403224200 UTC with SHA-256
`60b1165d70da093976329b4277d8673549132a2bcdf45911d5944adce918457c`.
The eighteen-case transition run passed in 305.734 seconds but started before the
one-predicate caller/shared hint correction; the main agent's final named
connected group will verify the combined stable source. No process-loading claim
follows from this fingerprint.
