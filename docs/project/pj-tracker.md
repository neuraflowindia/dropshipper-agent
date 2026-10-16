# Project tracker

Latest state: **17 October 2026, about 22:45 UTC on 16 October (4:15 am India time on 17 October) — regroup.** The owner **stopped research at 20:50:41 UTC on 16 October** and asked for a full, granular explanation of how the program works and for a new direction. The explanation, with every step, website, AI use and gate and the day's numbers, is [How the research program really works](research-wiring-explained-2026-10-16.md). The proposed direction is [Plan R18](plan-2026-10-17-ranking-and-big-universe.md): rank by likelihood instead of pass/fail gates, a product universe for 10+ months, and full tracing. Plan R18 **waits for the owner's answers** (15 questions, also in the handoff). Built for the owner's tracing request and now on `main`: the read-only tracing tool and per-AI-use accounting (R18a). This is the sole current progress summary. Requirements belong to the [product contract](../product/india_dropshipping_product_opportunity_radar_product_brief.md); technical changes belong to the [implementation record](../operations/implementation_2026-10-11.md).

## Current position — research day of 16 October (06:58:54–20:50:41 UTC)

| Measure | Count |
|---|---:|
| Product-map headings used / total | 32 / 1,706 |
| Raw items collected for ideas / read by the AI | 347 / 128 |
| Candidates / defined product types / passing the checklist | 34 / 30 / 30 of 34 |
| **Report-ready types** (need 20 across the two first sets) | **1** |
| Why the others are held (one type can be held by several) | crowding not decided 23; no supplier search yet 9; "too quiet" 6; checklist under 5 Meets 4 |
| Report sets produced | 0 / 2 |
| AI | 244 uses, 5.25 M input + 0.33 M output tokens (hidden reasoning not recorded), 81 AI minutes |
| Rounds | 30; median about 17 minutes, growing from about 8 to about 47 |
| Money since the fresh start | ₹0 cash; Apify US$0; quick-commerce pack 66 of 100 credits used (34 left, expire 2 November) |

**State of things now:**
- **Research is stopped** (owner's switch off). Plan R18 recommends keeping it stopped until its phase 2 loads.
- **AI allowance:** the program's last reading (18:10 UTC) shows the weekly allowance at 100%. The owner reset it that night; the program will see the reset at its next AI use.
- **Dropdash is signed out** (since between 08:18 and 17:26 UTC on 16 October; most likely the session expired). It has given 0 products so far. Owner action: sign in once.
- **Google Trends** (check passed at 17:56 UTC) and **IndiaMART** (from 17:45 UTC) are signed in.

**Built, and where it is:**

| Work | Where | State |
|---|---|---|
| Wiring audit + Plan R18 | `docs/project/` on `main` | Done; reviewed once by a separate checker and corrected |
| Read-only tracing tool (`scripts/research_trace.py`: funnel, routes, ai, product, summary) | `main` (`84012ec`), pushed | **Live**. 60/60 focused tests, 98% branch coverage; whole suite on its branch 6,102 OK (2 skipped). It runs in 5–40 s on the live records |
| R18a per-AI-use accounting + honest run records (accounting file per AI use with allowance before/after, allowance history, AI and platform run-log rows, Dropdash detection fixes, honest failure/block counters, product id on rows) | `main` (`ff2ca3e`), pushed | **Live** (takes effect when research restarts). A separate review found no must-fix items; its 6 should-fix items were fixed. Whole suite 6,142 OK (2 skipped). The meter now reports 75 failures instead of 105 (one per incident) and 0 access blocks instead of 57 (no website really refused on 16 October) |
| R14 report redesign + review fixes | `redesign` / `r10` | Done, not loaded; Plan R18 keeps its engine and simplifies its product page |
| R16 rounds 7–9× faster | `speed` (on `r10`) | Done, whole suite not run on the final commit; Plan R18 phase 0 loads it |
| R17 market checks first | `evidence` (on `r10`) | Plan R18 drops it |

Next actions, in order, once the owner answers: Plan R18 section 8 (phase 0: load the speed fix, accounting and Dropdash fixes; owner signs in to Dropdash; then phases 2–6). Until then, nothing new is built.

## Earlier fresh start — 13 October 2026 (history)

The first fresh start moved everything before 13 October 18:38:54 UTC to `archive/2026-10-13-before-fresh-start/` (91,219 files) and moved working records to `/home/anan/neuraflow-dropshipper-data/`. Its research (13–16 October) is now archived again in `/home/anan/neuraflow-dropshipper-archive/2026-10-16-before-fresh-start/`. Sections below titled "before the fresh start" are archived history; their evidence files are in the archive.

## Scope and remaining work

Complete the connected research programme and **two actual updated private report sets**. Each set contains an English PDF, a Roman-Hinglish PDF and one shared spreadsheet: **six owner-download files total**.

| Next action | Required result |
|---|---|
| Finish research | Current practical assessments, relevant source checks, supplier effort and photo attempts; preserve exact reasons and next actions for unresolved items. A filled report does not end research. |
| Resolve older-draft handling | Closed by the fresh start: the older drafts and their reservations are archived; the first two produced sets are the bilingual ones. |
| Finish both updated sets | At least ten distinct eligible narrow product types per set; at least twenty across the two sets. Inspect every PDF page, figures, photos, supplier limitations, hyperlinks and both five-sheet workbooks. |
| Verify private delivery | Ten private objects across the two sets and six byte-matched owner downloads through the existing connection. Renewing the existing owner sign-in is already approved if required. |
| Retain final files | Include the actual finished reports in the separate final implementation backup. Earlier backup proof cannot cover files that did not yet exist. |

Focused checks, the whole local suites (Plan R10 approval A1) and ordinary approved research are allowed. Hosted release gates, client publication/access changes, supplier contact, paid upgrades and credit purchases still need the owner. Last whole local run: `main` at `ff2ca3e` (16 October, 23:50 UTC), Python 6,142 OK (2 skipped). Earlier the same day: R14 review-fix branch 6,104 of 6,107 (1 known clock-dependent test, fixed on `main`); portal 303/303. The deferred client email does not prevent owner-only draft work.

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
