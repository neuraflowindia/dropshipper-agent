# Project tracker

Latest state: **15 October 2026, about 15:00 UTC** — **Plan R10 is live.** The switch-over (W13) was done on 15 October with the owner's explicit approval: `r10` merged into `main` and pushed, the research store converted to bounded checkpoints (`research-segments-v1`), the old-rule stock expired for re-judging (`scripts/r10_cutover.py apply`: 132 old intake items), the Windows app rebuilt and reinstalled (native self-test passes), and research restarted at 12:42 UTC. Fresh start was 13 October 2026 at 18:38:54 UTC (owner decision). This is the sole current progress summary. Requirements belong to the [product contract](../product/india_dropshipping_product_opportunity_radar_product_brief.md); technical changes and receipts belong to the [implementation record](../operations/implementation_2026-10-11.md) (fresh start: R08; Plan R10: R10).

## Current position — 15 October 2026

| Measure (owner meter, 15 October 14:20 UTC; first hours on the R10 code) | Count |
|---|---:|
| Candidates | 33 |
| Accepted narrow product types | 30 |
| Checklist-assessed types (re-judging under checklist v5) | 9 |
| Passing the checklist (≥5 Meets) | 8 |
| **Report-eligible types** (need 20 across the two sets) | **0** (expected at switch-over; rebuilds as the new sources come online) |
| Report sets produced | 0 / 2 |
| AI jobs | 356 |
| Provider (Apify) money since the fresh start | USD 0 |

Counts move both ways: new evidence correctly sends a type back for re-checking. The drop from 11 eligible (old rules) to 0 was accepted in advance (A2): every type is re-judged under checklist v5 and every saved listing under relevance review v5, and the crowding check and opportunity score need Amazon/Flipkart crowding answers first.

**Plan R10** ([plan](plan-2026-10-14-speed-sources-and-selection.md), approved 14 October) is live: signed-in research browser (Google Trends, IndiaMART, Dropdash), three dropshipping platforms (DeoDap and BaapStore read from their public catalogues without signing in; Dropdash signed in; Dropship India was dropped by the owner on 15 October because it showed nothing useful), quick commerce through the paid API, Amazon/Flipkart as a crowding check, the **opportunity score out of 100** with the owner's weights (want 20, early 30, room 25, product 25), wider discovery, the product map with AI expansion, Apify depth reads and the spend watch.

**Why research went quiet on 15 October (corrected diagnosis):** the 16 MB summary file (`index.json`) was never read by anything, so hitting its limit did not stop work. The real cause was the slow local "readiness" step between rounds: it re-checked every saved review from scratch and had grown from seconds (13 October) to 11–13 minutes per pass (two passes per round), so most wall-clock time went to preparation, plus a round tail with no reads left. Fixed on 15 October (readiness reuses its own reads within one decision: about 690 s → 21 s on a copy of the live store, byte-identical results). Also added on 15 October: the **progress watch** — heartbeats inside long local steps, time limits on them, a worker watchdog that restarts research after 45 minutes with no recorded activity (the owner's on/off switch stays on), plain-language "needs attention" lines and Windows notifications in the app, last-run reason and repeated-failure warnings, and a warning when scheduled runs stop starting.

**Owner's "stuck for 31 minutes" notifications (15 October evening): false alarms.** They came from the Windows app's own self-test, which used made-up research text ("no progress for 31 minutes… 45 minutes") and, until then, showed it as real Windows notifications each time an agent rebuilt or checked the app (about 13:30–15:00 UTC). Real research never hit that warning. Fixed (`9f9123e`): the self-test now counts notifications without showing them (32/32 checks). Separately, real research was slow: one round's "match" step took 4–5 minutes per review group (16 groups, about an hour with nothing new to show). Cause: each pass re-checked every type's saved reviews six times. Fixed (`c149294`): only the types a group names are read (about 4 min → 1–2 min per group, identical results). Merged and research restarted on 15 October about 17:30 UTC.

**Owner sign-in still needed** (one time, through the app's **Sign in to research sites** button): Google account / Google Trends, IndiaMART and Dropdash. Until then those sources wait; everything else runs. Dropdash's sign-in form takes a **10-digit mobile number** and password, not an e-mail address.

**Known gaps:** Brand names show on only about 12% of Amazon result cards and none on Flipkart, so the "many brands" crowding rule rarely decides. The live W13 checks that need the owner's sign-in or real data (platform page layouts incl. Dropdash's real product pages, Trends proof gate 3/3, Apify field names, crowding calibration after 50 types, round-time measurement) are still open. The Apify key's limit was lifted by the owner on 15 October (usage reads now return HTTP 200).

Next actions, in order: (1) owner's one-time sign-in; (2) remaining W13 live checks; (3) W14 remaining documents and the single-process test leak; (4) research to 20 eligible types, the two bilingual report sets, the portal deploy and private-delivery checks.

## Fresh start — 13 October 2026

- Every earlier research record, AI answer, report draft/file, run log, receipt and `media/live` file was moved unchanged to `archive/2026-10-13-before-fresh-start/` (91,219 files, 3,140,062,083 bytes; nothing deleted). Research and reports restart from an empty store with the current code.
- Working records now live on the Linux disk at `/home/anan/neuraflow-dropshipper-data/`; daily backups still go to the project's `data/backups/` on D:.
- Spending safety carried over unchanged: the Apify ledger (opening USD 5.00 for the period ending 2 November; USD 0.50040006 known charges, USD 0.88 held, USD 3.06755 earmarked for sources, USD 0.10204994 unallocated headroom) and its 44 prepared requests. Account settings stay in `config/local`.
- Research is **stopped** (the owner's saved switch). Nothing new runs until the owner clicks **Start**.
- Progress, money and time since the fresh start: `.venv/bin/python scripts/run_programme.py --meter`. At the start every research and report count was 0.
- Starting search map: on the first research turn after **Start**, the store prepares its own seed from the repository's branch inventory (28 branches, 145 examples; local work, about 2 seconds), then the AI reviews one branch per round. The inventory's 21 public list pages from 8 October were copied into the store for this; the old seed answers stay archived.
- The automatic research programme does not spend Apify money (its routes are free websites plus quick-commerce credits). The Apify earmark is the account-period allocation for owner-approved paid runs; nothing in it was tied to archived weeks or reports.

The scope and completion checks below still apply. Sections titled "before the fresh start" are archived history; their evidence files are in the archive.

## Scope and remaining work

Complete the connected research programme and **two actual updated private report sets**. Each set contains an English PDF, a Roman-Hinglish PDF and one shared spreadsheet: **six owner-download files total**.

| Next action | Required result |
|---|---|
| Finish research | Current practical assessments, relevant source checks, supplier effort and photo attempts; preserve exact reasons and next actions for unresolved items. A filled report does not end research. |
| Resolve older-draft handling | Closed by the fresh start: the older drafts and their reservations are archived; the first two produced sets are the bilingual ones. |
| Finish both updated sets | At least ten distinct eligible narrow product types per set; at least twenty across the two sets. Inspect every PDF page, figures, photos, supplier limitations, hyperlinks and both five-sheet workbooks. |
| Verify private delivery | Ten private objects across the two sets and six byte-matched owner downloads through the existing connection. Renewing the existing owner sign-in is already approved if required. |
| Retain final files | Include the actual finished reports in the separate final implementation backup. Earlier backup proof cannot cover files that did not yet exist. |

Focused named checks and ordinary approved research are allowed. No local/hosted release gates, broad full-suite run, client publication/access change, supplier contact, paid upgrade or credit purchase. The deferred client email does not prevent owner-only draft work.

## Completion checks

The existing audit has **eight areas × four named checks = 32 checks**. **23 passed; nine remain; 23 ÷ 32 = 71.9%.** Checks differ in size; this is not a percentage of hours, code or delivered reports. No reliable remaining-time estimate is established.

| Area | Passed | Percentage | Open checks | Remaining size |
|---|---:|---:|---|---|
| Search planning and renewal | 3/4 | 75% | A4 | Medium |
| Website collection | 3/4 | 75% | B4 | Large |
| Assessments and repeat recognition | 2/4 | 50% | C3, C4 | Large |
| Suppliers and photos | 2/4 | 50% | D3, D4 | Large |
| Report production | 2/4 | 50% | E3, E4 | Large |
| Private delivery | 3/4 | 75% | F4 | Small after reports |
| Automatic operation and activity records | 4/4 | 100% | None | Named checks passed |
| Money, backup and ordinary-run proof | 4/4 | 100% | None | Named checks passed with limits |
| **Total** | **23/32** | **71.9%** | **9** | Size labels are judgments, not measured hours |

The report tools account for two passed report checks; actual report output is measured separately below. The [original audit](../reviews/2026-10-10_programme_operation_audit.md) defines the checks. Later B3/G3/G4 and limited H3/H4 acceptance are evidenced in the [implementation record](../operations/implementation_2026-10-11.md#acceptance-evidence).

## Before the fresh start: last verified operation and outputs (archived)

[Read-only observation](../../archive/2026-10-13-before-fresh-start/data/operations/implementation-20261011/resumption-status-20261013-1250.json):

| Observation | Result and boundary |
|---|---|
| Existing research process | Exactly one: PID7146/start57153, automatically started at 07:30:43 UTC with the reviewed code. This is a dated identity, not a command target for future interruption. |
| Saved-work recovery | Original unfinished-turn reservations/counters preserved; [recovery proof](../../archive/2026-10-13-before-fresh-start/data/operations/implementation-20261011/natural-power-interruption-turn-recovery-20261013-0751.json). |
| Latest recorded round | Finished at 12:38:12 UTC, followed by continued preparation. This operating-log observation is not a replay of every original research event. |
| Useful saved results since restart | 33 recorded results, not 33 products or reports. Last useful result in the inspected status: usable image saved at 12:33:14 UTC. |
| Actual report inputs | All 15 inspected inputs remain older-format. None satisfies the updated narrow-product-type contract; **0/2 updated sets (0%)**. |

The exact loaded version, source fingerprint and focused repair results are recorded in the [loading and verification record](../operations/implementation_2026-10-11.md#loading-and-verification). Passing code checks are not finished report evidence.

## Before the fresh start: dated research counts (archived)

The last separately verified whole-pool research snapshot here is **13 October, 04:58:55 UTC**, not the later process/output observation. [Original count receipt](../../archive/2026-10-13-before-fresh-start/data/operations/implementation-20261011/current-functional-funnel-20261013-045855.json):

| Step | Count | Meaning |
|---|---:|---|
| Saved candidates | **169 = 156 original + 13 later** | Research subjects, not approved recommendations. |
| Accepted narrow product-type descriptions | **10/169 = 5.9%** | Manufacturer-independent meanings, not necessarily suitable products. |
| Those ten with a current checklist assessment | **4/10 = 40%** | Six still required a current assessment at this cutoff. |
| Current-format report-eligible types | **0** | Required information was unfinished; this is not a claim that all ten failed. |
| Separately retained older-format eligible entries | **28** | Compatibility stock, not current-format report stock. |

A later 06:16:37 [same-snapshot summary check](../../archive/2026-10-13-before-fresh-start/data/operations/implementation-20261011/owner-summary-real-prefix-review-20261013.json) confirmed the current/older-format separation. Do not combine these dated counts with later operating-result counters to manufacture a fresh yield.

## Report-handling choice (closed by the fresh start)

The fresh start archived the older drafts together with their reservations, so new reports start at position 1 and the first two produced sets are the bilingual ones. The text below is the pre-fresh-start record.

Three unfinished older private drafts reserve 30 older product entries. Their positions make upcoming reports number 4/5/6 and request an ongoing language before the required two actual updated bilingual sets exist. [Exact language-order probe](../../archive/2026-10-13-before-fresh-start/data/operations/implementation-20261011/older-draft-language-order-review-20261013.json).

**Recommended, not approved:** retain old files as an archive, release their unpublished product reservations for upcoming reports, and ensure the first two actual updated sets are bilingual. Do not backdate evidence or change published/client reports. No reservations, dates or language policy have been changed.

This is separate from unfinished research. The [overlap review](../../archive/2026-10-13-before-fresh-start/data/operations/implementation-20261011/legacy-hold-current-overlap-review-20261013.json) found only one direct overlap with the ten accepted types; it does not establish old reservations as the cause of zero current stock.

## Cost and recovery limitations

The [cost plan](../operations/build_activation_and_costs_2026-10-03.md) owns money rules. The [implementation evidence](../operations/implementation_2026-10-11.md#backup-and-cost-boundaries) records dated confirmed charges, unknown read prices, selected-file restore results and historical gaps. Neither proves an exclusive all-in report price, recovery of every historical attachment, restored sign-ins or protection against losing the physical drive.
