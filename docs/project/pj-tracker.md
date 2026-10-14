# Project tracker

Latest state: **14 October 2026, 10:37 UTC** — research running on commit `0a71c9c` (restarted 09:00 UTC). Fresh start was 13 October 2026 at 18:38:54 UTC (owner decision). This is the sole current progress summary. Requirements belong to the [product contract](../product/india_dropshipping_product_opportunity_radar_product_brief.md); technical changes and receipts belong to the [implementation record](../operations/implementation_2026-10-11.md) (fresh start: R08).

## Current position — 14 October 2026

| Measure (owner meter, 10:37 UTC; about 16 hours after the fresh start) | Count |
|---|---:|
| Candidates | 30 |
| Accepted narrow product types | 24 |
| Checklist-assessed types | 24 |
| Passing the checklist (≥5 Meets) | 18 |
| **Report-eligible types** (need 20 across the two sets) | **6** |
| Report sets produced | 0 / 2 |
| AI jobs | 227 |
| Provider (Apify) money since the fresh start | USD 0 |
| Quick-commerce credits left (free sign-up credits) | 44 of 100 |

Counts move both ways: new evidence correctly sends a type back for re-checking.

What happened on 13–14 October:
- A power cut stopped research at about 21:59 UTC on 13 October; it restarted by itself and recovered cleanly.
- Repairs (implementation record [R09](../operations/implementation_2026-10-11.md#r09-source-routes-access-waits-and-report-pool-speed-1314-october)): Google Maps browser read restored; Google web supplier search retired; IndiaMART's login wall and Google gates recorded as blocked; escalating waits; Meta completeness fixed; supplier outcome wording fixed; report pool rebuilt about 6–8× faster. Rounds now start every 1–8 minutes instead of about every 70.
- The owner answered the proposal's questions on 14 October. The agreed work is written up as **[Plan R10](plan-2026-10-14-speed-sources-and-selection.md)**: signed-in research browser (Google Trends, IndiaMART, Dropship India, Dropdash), the four dropshipping platforms as sources, quick commerce switched on, Amazon/Flipkart turned into a crowding check, a new market-site set, practical AI judgments and finishing nearly-ready products first, a product map of about 1,200 headings with AI expansion on, Apify's free monthly allowance for deeper Meta reads, Google Maps straight to the browser, and a spend and allowance watch with 50/75/90% flags. **Not built yet; waiting for the owner's approval of the whole plan (its question A1) and the plan's other short questions.**

**Plan R10 building (14 October, evening):** approved by the owner (A1). Work happens on branch `r10` in the build copy `/home/anan/dropshipper-r10`; the live research keeps running the old code on `main` until the W13 switch-over. **Wave 1 done and saved** (commit `bb4db37`): W1 signed-in browser and sign-in button, W3 IndiaMART signed in and Maps straight to the browser, W9 labelled estimates and finishing nearly-ready types first, W12 spend watch with the paid-credits money guard. Each package was reviewed independently and its findings fixed; the whole local Python suite ran (5,451 tests): the only failures are the ten already-failing modules listed in W14, all failing identically on `main`. **Wave 2 under way:** W2 Trends, W4 platforms, W6 crowding readers, plus integration fixes. **Waiting on the owner:** the new ranking design (weighted opportunity score; W6 selection and W7 wait for it) and permission for the GitHub author rewrite (the computer's safety check blocked it).

Next actions, in order: (1) owner approves Plan R10 and answers its questions; (2) build it package by package (W1 first, so the owner can sign in early); (3) cut over and prove each new source live; (4) finish the two report sets, deploy the portal update (approved) and verify private delivery.

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
