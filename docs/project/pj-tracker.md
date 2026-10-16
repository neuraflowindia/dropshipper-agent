# Project tracker

Latest state: **16 October 2026, about 20:00 UTC — the owner paused new building to regroup.** Research keeps running on the live R15 code (`main` `7294369`), started from scratch at **06:58:54 UTC on 16 October**. Work built during the day (R14 report redesign, R16 speed, R17 market checks first) sits on separate branches and is **not loaded**. The owner judged the research process too strict and over-engineered for a service that offers *prospects worth testing*; the step-by-step explanation, the measured reasons it is slow, and a proposed lighter "quick pick" process are in the [research process review](research-process-review-2026-10-16.md). Nothing new is built until the owner answers its questions (they are also in the handoff). This is the sole current progress summary. Requirements belong to the [product contract](../product/india_dropshipping_product_opportunity_radar_product_brief.md); technical changes and receipts belong to the [implementation record](../operations/implementation_2026-10-11.md) (R13 same-day fixes and R15 signed-in sites on `main`; R14, R16 and R17 sections live on their branches until loaded).

## Current position — 16 October 2026, 19:52 UTC

| Measure (owner meter) | Count |
|---|---:|
| Candidates / accepted narrow types / assessed / passing | 34 / 30 / 30 / 27 (in 13 hours since 06:58:54 UTC) |
| **Report-eligible types** (need 20 across the two sets) | **1** |
| Why the others are not ready (34 types; one type can wait on several things) | 23 waiting for the Amazon/Flipkart crowding check; 11 still need a supplier search; 6 rejected as "Too quiet" (no demand signal); 4 need more checklist facts or fall below 5 Meets. Only 4 types have any relevant Meta ad and 1 has usable Trends data |
| Report sets produced | 0 / 2 |
| Product map | 1,706 headings, 1,674 never searched (about 7 weeks at today's pace; tops itself up) |
| Money since the fresh start | ₹0 cash; Apify US$0 (US$3.07 earmarked free allowance left this month); 42 free quick-commerce credits (expire 2 November) |

**Blockers right now:**
- **AI weekly allowance at 100%** since 18:10 UTC (owner's ChatGPT Plus/Codex, shared with the owner's other Codex projects; research used under 2% of it). Every AI step waits until **22 October, 06:13 UTC**. Paid extra credits are never used without the owner.
- **Dropdash signed out** at 18:37 UTC (its page showed the sign-in form). Owner action: **Sign in to research sites** → Dropdash tab → close the Chrome window.
- **The demand gate** (opportunity "want" at least 5 points) and the requirement that every product pass every market check before it counts. This is a design choice, not a bug; the review proposes relaxing it.

**Signed-in sites:** Google Trends passed its three-read check at 17:56 UTC (owner signed in). IndiaMART signed in and read since 17:45 UTC. Dropdash signed out (above).

**Built on 16 October, not loaded** (all branches in `/home/anan/`, none pushed except `main`):

| Work | Branch / folder | State |
|---|---|---|
| R14 report redesign (owner approved all plan section 14 options) + review fixes | `redesign` in `dropshipper-redesign` (`f37f8a2`); merged into `r10` | Done. Whole suite 6,107: 6,104 passed, 2 skipped, 1 known clock test; Chrome print 5/5; portal 303/303. EN and Hinglish fixture issues 25 pages each |
| R15 signed-in sites (Dropdash plain Profitable list, weekly 21 categories, units-sold hint, first name removed, lost-read sweep, probe fixes) | `main` | **Live** since 16 October |
| R16 rounds stay fast (stop copying the whole history on every read) | `speed` in `dropshipper-speed` (`76049fa`, on top of `r10` `ba7b849`) | Rounds 7–9× faster on copies of real data. Whole suite not yet run on the final commit |
| R17 market checks first for passing types (+ review fixes M1–M5, m1–m9) | `evidence` in `dropshipper-evidence` (`69cb68c`, on top of `r10` `204c2b9`) | Fixes done, focused tests 386 OK. Whole suite not yet run. **Recommended to park** under the quick-pick direction |
| Build copy | `r10` in `dropshipper-r10` (`204c2b9`) = R15 + R14 + R17 before its fixes | Integration copy only; do not fast-forward `main` to it as it is |

Next actions, in order, once the owner answers: (1) decide the quick-pick direction (review questions 1–5); (2) integrate what is kept (R14 and R16 on `main`; one known one-line conflict in `source_coverage.py` if R17 is also kept), run the whole suite, load, restart; (3) build quick pick if approved; (4) first two bilingual report sets, portal deploy and private-delivery checks.

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

Focused checks, the whole local suites (Plan R10 approval A1) and ordinary approved research are allowed. Hosted release gates, client publication/access changes, supplier contact, paid upgrades and credit purchases still need the owner. Last whole local runs (16 October): the R14+R15 merge in the build copy, Python 6,097 OK (2 skipped); the R14 review-fix branch 6,104 of 6,107 (1 known clock-dependent test, already fixed on `r10`); portal 303/303. The deferred client email does not prevent owner-only draft work.

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
