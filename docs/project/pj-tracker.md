# Project tracker

Latest state: **17 October 2026, about 09:30 UTC (3:00 pm India time) — planning round finished.** The owner answered the 29 design questions; [Plan R19](plan-2026-10-17-r19-product-flow-engine.md) (the agreed design and the build plan) is written, reviewed by a separate checker, and **waits for the owner's approval in the next chat**. Research has been **stopped since 16 October 20:50:41 UTC** and stays stopped until Plan R19 phase 5 loads. This is the only progress summary; the design lives in Plan R19, requirements in the [product contract](../product/india_dropshipping_product_opportunity_radar_product_brief.md).

## Where things stand

| Item | State |
|---|---|
| Plan R19 | Written and reviewed 17 October; 0 of 9 phases built; needs the owner's approval |
| Research | Stopped (owner's switch) since 16 October 20:50:41 UTC |
| Code on `main` | Unchanged since `02c8326` (17 October); only documents changed in this round |
| Report sets produced | 0 (the first two come at the end of Plan R19 phase 6) |
| Documents | Cut from 44 documents to 14 on 17 October (plus about 9 MB of old mock-up images and scripts); everything removed is in git history (`02c8326`) |

## Store catalogues (kept for the new design)

| Store | Products | Saved where (runtime folder `data/research/captured/dropship_platforms/`) | Last read |
|---|---:|---|---|
| DeoDap (public) | 13,527 (about 4,521 in stock and in the four categories) | `deodap/catalogue.json` | 16 October |
| BaapStore (public) | 6,053 (about 931 in the four categories) | `baapstore/catalogue.json` | 16 October |
| Dropdash (signed in) | **5,189 in 21 categories, all complete** (4,991 in stock; about 2,500–3,000 look in-category by a rough title count) | `dropdash-full-list-2026-10-17/` (`products.json`, `categories.json`) | **17 October 08:00–08:58 UTC**, read-only, signed in throughout |

Dropdash findings (17 October): category pages load 18 products per scroll from Dropdash's own app; each product carries an exact order count. The card's "units sold" range overstates it 5–10 times (4,678 cards say "100-500", yet 2,915 of those products have zero orders), so Plan R19 uses the exact count. The owner's sign-in of about 07:20 UTC was still valid at 08:58 UTC.

## Accounts and allowances

| Item | State |
|---|---|
| ChatGPT/Codex allowance | Last read 17 October 06:42 UTC: 5-hour window 29%, weekly 20% used (weekly resets 23 October 20:41:55 UTC). Usage is shared with the owner's own Codex projects |
| Paid Codex credits | 58,522 (17 October morning); auto-refill off. Published rate for the research model (GPT-6 Luna): 2.5 credits per million tokens read, 0.25 cached, 12.5 written |
| Quick-delivery credits | 34 of 100 left in the pack, expiring 2 November (one report's picks need about 45; the ₹500 pack gives 2,500) |
| Apify | Free allowance US$1.38 of US$4.50 used this month |
| Sign-ins | Google (Trends): signed in at the stop. Dropdash: signed in (17 October). IndiaMART: never signed in (needs the owner's phone code) |

## Last research day, for reference (16 October, old engine)

354 listings collected, 128 read by the AI, 34 new product types, **1 report-ready**, 0 reports; 244 AI calls (5.25 M tokens read, 0.33 M written; about 17 credits at today's published rate). The faults found that day and how Plan R19 prevents them are in Plan R19 section 10.

## Branches

`main` (live code, docs updated 17 October). `redesign` (16 October report redesign, built and reviewed): merged into `main` in Plan R19 phase 0. Not carried forward: `speed` (R16), `evidence` and the R17 part of `r10`; `trace`, `obs` and `sites` are already in `main`. Last whole local run: `main` at `ff2ca3e` (16 October, 23:50 UTC), Python 6,142 OK (2 skipped); portal 303/303.

## What is next

1. The owner approves Plan R19 (or asks for changes) in the next chat.
2. Build phases 0–5 (about 12–13 working days), then restart research with the new engine and measure the first 24 hours.
3. Phase 6: the first two reports (about day 15–17), owner approval, private website update.
4. Phases 7–8: the illustrated report look (owner picks one of two mock-ups), documents, and the points tuning after about six reports.
