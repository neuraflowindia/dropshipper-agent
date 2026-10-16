# Project tracker

Latest state: **16 October 2026, 12:31 UTC** — **research running on the new code since the second fresh start; the same-day live fixes (R13) are loaded.** At the owner's request research restarted from scratch at **06:58:54 UTC on 16 October** (the meter's new start). Code `main` = `r10` (Plan R10 from 15 October plus Plan R11/R12 and the R13 live fixes of 16 October). This is the sole current progress summary. Requirements belong to the [product contract](../product/india_dropshipping_product_opportunity_radar_product_brief.md); technical changes and receipts belong to the [implementation record](../operations/implementation_2026-10-11.md) (fresh starts: R08 and Plan R11; Plan R10: R10; durability: R11; product map and round speed: R12; same-day live fixes: R13); the 16 October programme is [Plan R11](plan-2026-10-16-fresh-start-and-year-long-research.md).

## Current position — 16 October 2026

| Measure (owner meter) | Count |
|---|---:|
| Candidates / accepted narrow types / assessed / passing (12:31 UTC) | 28 / 23 / 23 / 20 (rebuilding since 06:58:54 UTC) |
| **Report-eligible types** (need 20 across the two sets) | **1** |
| Report sets produced | 0 / 2 |
| Product map | **1,705 headings in 55 sub-areas** (was 224 in 48; 1,681 not searched yet); grows by itself (AI top-up below 300 unsearched headings) |
| Provider (Apify) money since the fresh start | USD 0 |

**What changed on 16 October** ([Plan R11](plan-2026-10-16-fresh-start-and-year-long-research.md)):
- **Exhaustive product map:** 1,470 new headings (research directions, one functional kind of product each) across all four categories, plus seven new sub-areas (party decoration, home office, home repair, arts and crafts, pregnancy and new-mother care, car exterior care, swimming). The map never stops growing: soft target 40 per sub-area, and an AI top-up adds up to 12 headings a call (at most 6 calls a day, at most 120 headings a sub-area) whenever fewer than 300 headings are still unsearched. The meter and the window show a "Product map" runway line.
- **Year-long running:** the weekly programme record is saved in small parts (store growth about −73%), daily backups keep 14 daily + 8 weekly copies and stop before the D: drive drops below 10 GB, unused lookup pages and 90-day-old run logs are tidied, the meter stays fast, cold start replays history once, and the window shows one plain line if any tidying gets stuck.
- **Round speed and clarity** (owner "yes" to 15 October question 3): "Review group N of M" in the status; a restarted round skips stages it already finished; match stage about 45% faster.
- **Second fresh start:** old research, reports, run logs and backups archived (nothing deleted); money ledger, the owner's switch and the allowance watch carried. Receipt: `data/operations/fresh-start-20261016/fresh-start.md` in the working-data folder.
- **Live fixes during the day** ([implementation record, R13](../operations/implementation_2026-10-11.md#r13-same-day-live-fixes-after-the-second-fresh-start-16-october); loaded on 16 October at 11:09 UTC as one update, commit `9059304`; the Windows app was rebuilt and reinstalled at the same time and its self-test passed 33/33):
  - **Google Trends works while the window stays minimised.** The first signed-in reads failed because Chrome draws nothing for a minimised window (`TRENDS_MINIMISED_NOT_RENDERED`). Each signed-in read now turns on Chrome's own "act as if focused" setting, so the page draws while the window stays minimised and the owner is not disturbed. Proven live: chart drawn, chart file of 54 rows saved. A chart file is used only when Chrome reports the download finished.
  - **Dropdash reads its real pages**, seen signed in for the first time: the home page (21 categories, 6 products each) as the catalogue and "Profitable Products" (first 18 products) as its winning list; prices, MRP and stock come from the page's own price and stock boxes. Dropdash prices include GST and shipping (the site says so). Dropdash first waits out a back-off from the morning's failed reads under the old guessed settings, until about 14:18 UTC.
  - **Saved answers no longer counted as fresh reads.** DeoDap/BaapStore answers served from the saved catalogue were counted as new reads, so the acquire stage failed (`PROGRAMME_ACQUISITION_COUNTS_INVALID`, 7 times) and later review groups lost their turn that round.
  - **Power cut proofing.** A power cut at about 07:30 UTC left two saved browser files empty, and one saved Amazon bestseller read then failed its check on every round. Every file a browser read saves is now forced to disk before the read counts; a saved read whose files are proven lost (empty or all zeros) is set aside (nothing deleted) and read once more under the normal limits.
  - **IndiaMART waits for the owner.** The research browser has never been signed in to IndiaMART (its read showed IndiaMART's own mobile-number sign-in page). A question that meets that page now waits calmly for the sign-in (it keeps its try limit) instead of failing; the one question already stopped that way is reopened.
  - **Sign-in window seen correctly.** When exactly one sign-in window was open, the program counted it as closed (a Windows PowerShell quirk with single results), so a Trends read at 11:48 UTC collided with the owner's open sign-in window and failed. Fixed and loaded the same day (second update): research now waits while the window is open.
  - **Clearer meter.** Its access-wait rows show the real reason (for example `OWNER_SIGN_IN_REQUIRED`, `TRENDS_MINIMISED_NOT_RENDERED`) instead of only "challenge: true".
  - Earlier the same morning: DeoDap/BaapStore feed reads crashed on every round (`platforms_failed`, a socket handling bug since 14 October), fixed (`85ccb0c`); a test-only leak that failed one test in single-process runs, fixed (`2791256`).
- **Report redesign:** plan and mock-ups ready for the owner ([plan](../client-preview/design/report-redesign-2026-10-16/plan.md)); not built until the owner approves.

**Live check after loading (12:31 UTC):** no `PROGRAMME_ACQUISITION_COUNTS_INVALID` and no failed stage on the new code; the power-cut-damaged Amazon read was set aside at 11:13 and read again at 11:14. The owner opened the sign-in window at 11:18:50 UTC and it was still open at 12:31; Trends and IndiaMART reads wait for it to close.

**Owner action needed (once, after the update is loaded):** click **Sign in to research sites** in the NeuraFlow Dropshipper window. In the **IndiaMART** tab, finish IndiaMART's sign-in with the mobile number and the one-time code IndiaMART texts to it; check the other tabs still show you signed in; then close the Chrome window. This is needed for IndiaMART, and the new sign-in also restarts the Trends check line (its first three reads after a sign-in must be complete).

**Live checks still open (W13):** Google Trends draws while minimised (one read proven on 16 October); its three-read check after the owner's next sign-in is still to pass. Dropdash's real pages are recognised on saved copies; its first live reads with the new settings come after its back-off (about 14:18 UTC). IndiaMART waits for the owner's sign-in. Still to watch: ten signed-in reads with no focus taken, Apify `map_meta_rows` fields, crowding calibration after 50 types, three-hour round time.

Next actions, in order: (1) owner's sign-in (above); (2) watch the first rounds on the R13 code: Trends check line 3/3, Dropdash's first real reads, IndiaMART after the sign-in, no `PROGRAMME_ACQUISITION_COUNTS_INVALID`, the set-aside Amazon bestseller read replaced once; (3) remaining W13 live checks; (4) adopt store checkpoints once events exist; (5) research to 20 eligible types, two bilingual report sets (redesigned if the owner approves), portal deploy and private-delivery checks.

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

Focused checks, the whole local suites (Plan R10 approval A1) and ordinary approved research are allowed. Hosted release gates, client publication/access changes, supplier contact, paid upgrades and credit purchases still need the owner. Last whole local run (16 October, on the R13 code): Python 6,026 tests, all passed (2 skipped); portal 303/303 (15 October; portal unchanged since). The deferred client email does not prevent owner-only draft work.

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
