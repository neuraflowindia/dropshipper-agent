# Plan R19 — the product-by-product research engine (implementation plan)

> **For agentic workers:** build this plan task by task (superpowers:subagent-driven-development recommended: one fresh helper per task, a separate reviewer per phase). Every task follows test-first steps: write the failing test, see it fail, write the code, see it pass, commit. Steps are not spelled out per task; each task names its files, its interfaces, the tests that prove it and what "done" means.

**Project stage:** Development and testing (confirmed 13 October 2026). No client has received a report. Things are replaced directly: no migration of old research records, no keeping old and new side by side, no staged rollout.

**Status:** written 17 October 2026 from the owner's answers of the same day, reviewed by a separate checker and corrected (section 12). **Waiting for the owner's approval in the next chat.** Nothing in it is built yet. Research has been stopped since 16 October 20:50:41 UTC and stays stopped until phase 5 loads (owner answer 24).

**Goal:** turn the stopped research program into a steady flow of ranked product suggestions: ideas come from the dropshipping stores and Facebook ads, each product is finished in one go, ranked by points (nothing is blocked for missing information), and three reports a week are assembled from the ranking, for at least two years, with the owner's routine part limited to approving reports, IndiaMART's phone code and the occasional purchase the program asks for.

**Architecture (in one paragraph):** a new small package `src/product_radar/flow/` holds the whole research pipeline — one SQLite database (Python's built-in `sqlite3`) for the product pool, evidence, the work queue, the waiting list and the AI ledger; a conveyor that moves one product at a time through fixed stages; a points calculator; a report assembler. It reuses today's website readers, store-catalogue downloads, AI client, report renderer (the 16 October redesign), private portal, backups and Windows launcher, each adapted at its edge. Most of today's round-and-desk engine (about 50,000 lines in about 100 modules) and its tests are deleted.

**Tech stack:** Python 3 standard library (`sqlite3`, `json`, `unittest`), existing Windows Chrome + DevTools reading through PowerShell, the Codex CLI (`codex exec`) on the owner's ChatGPT subscription, the existing report renderer (HTML → PDF, Excel), Cloudflare Worker portal (`portal/`, Node tests).

**Spec (what this plan argues from):** the owner's answers of 17 October (section 2) and the design rules in section 3 of this plan. Background facts come from the 17 October discussion (now folded in here) and three code surveys of 17 October.

---

## 1. In plain words (for the owner)

**What changes.** Today's program walked around ten "desks" in rounds, touched every product a little each lap, and held products back whenever any fact was missing. On 16 October that gave 1 report-ready product in 14 hours. The new program works like a conveyor belt:

1. **Collect ideas** every day: new and best-selling products from DeoDap, BaapStore and Dropdash (free — their whole lists are already downloaded), plus Facebook ad searches from the 1,706-line product list, plus new supplier listings on TradeIndia and IndiaMART.
2. **Throw out only the obvious** for free: other categories, big brands, the excluded kinds (jewellery, clothing, pets…), copies.
3. **The AI names 10–20 items in one go**: what each product is, its category, and the 10-question checklist.
4. **Check each product on 4–6 places, one product at a time**: the three stores (free look-ups), Facebook ads, Google Trends and Amazon for everyone; Flipkart for products near the top; quick-delivery apps and a supplier look-up only for the products picked for a report. Places are chosen by fixed rules, never by the AI.
5. **Give it points.** Present in a few places = high; nowhere = low; everywhere with heavy sales = lower again. "Not checked yet" counts as zero, never as bad.
6. **The evening before each report** (Tuesday, Thursday and Saturday mornings): pick 10 main products (5 from the top third, 3 from the middle, 2 from the lower third) and up to 5 "other suggestions", write the report, and put it in the private website for you to approve.

**What you have to do.** Very little:
- **Keep the computer switched on, signed in to Windows and unlocked, with sleep and the automatic screen lock turned off** (click-by-click steps will be in the operator guide). Research and report assembly run on it; while the screen is locked, the checks that need a signed-in site (Google Trends, Dropdash, IndiaMART) wait, and everything else carries on. Phase 4 also tests whether those checks can run with the screen locked, which would remove this need.
- IndiaMART sometimes needs the code it texts to your phone. A desktop alert with a chime repeats every 20 minutes with "Sign in now", "Snooze 1 hour" and "Mute". Research carries on while it waits; only IndiaMART supplier look-ups wait (TradeIndia is used meanwhile).
- When the quick-delivery credit pack runs out, the window shows exactly which pack to buy and how. Work carries on meanwhile. The 34 credits left cover less than one report (one report needs about 45), so the first report will ask for the ₹500 pack (2,500 credits, about 55 reports).
- Approve each finished report with one click in the desktop window. A report you have not approved yet waits up to 7 days and goes out at the next free slot after you approve it.
- If your paid Codex credits fall below 3,000, or Codex ever asks you to sign in again, the window tells you once a day what to do.
- Dropdash is expected to sign itself in using the password Chrome has already saved in the research browser (or, failing that, the details in your `.env` file). This is tested live in phase 4; until it is proven, Dropdash's saved list of 5,189 products keeps serving the checks.

**AI cost.** At OpenAI's published price, the small model research uses (GPT-6 Luna) costs 2.5 credits per million tokens read and 12.5 per million written. All of 16 October's research (244 AI calls) would have cost about **17 credits**. The new design handles many more products, so expect **tens of credits per report** (a rough guess: 20–60 while the backlog of about 8,000 store items is being named in the first weeks, less after), measured from day 1 — far below your target of 500. Research uses your plan's allowance first; when that reaches 100% it should carry on with your paid credits automatically (OpenAI offers no way to use credits first). This is confirmed the first time the limit is reached; if Codex refuses instead, only the AI part waits for the reset and everything else carries on.

**Time.** About **20 working days** of agent work (an estimate; range 17–25), in 9 phases. Research restarts at the end of phase 5 (about day 12–13); the first two reports come at the end of phase 6 (about day 15–17, depending on the next Tuesday/Thursday/Saturday slot); the illustrated report look follows in phase 7, after you pick one of two mock-ups. Each stretch of work starts when you open a new chat with the handoff message, so calendar time is longer than working days.

**Percentile scores.** As you asked, the 10 main picks show a percentile ("Top 8%"). Because 2 of the 10 come from the lower third on purpose, those two would show something like "Top 72%" — see question 2 in the handoff.

---

## 2. The owner's decisions (17 October) and where each one lives in this plan

| # | Decision (owner's answer) | Where |
|---|---|---|
| 1 | Ideas mainly from stores + Facebook; cross-check on several places; rank by points; only hard rules remove. Choose places **by rule, never by AI**; checking 4–6 places with positive evidence on 2 is "good to go" | 3.1, 3.4 |
| 2 | Hard removals only: big brands; clearly outside the four categories; excluded kinds; clearly failing checklist; already sent; exact duplicates. Baby skin care stays | 3.2 |
| 3 | Checklist tiers: 5+ "yes" main; 3–4 "other suggestions"; removed only at ≤2 "yes" and ≥5 "no"; many "unclear" asked again first | 3.3, 3.6 |
| 4 | Report of 15: 10 main (5 top third, 3 middle, 2 lower third, never bottom fifth) + up to 5 other suggestions. **Show a percentile score for the 10 main picks only** | 3.6 |
| 5 | Facebook: 1 ad counts, 2+ advertisers more, recent ad a little more, stopped ads count half | 3.5 |
| 6 | Trends: rising > steady > falling = no chart = 0 | 3.5 |
| 7 | Amazon/Flipkart: a few listings small plus; heavy selling minus | 3.5 |
| 8 | Quick-delivery only for picks, swap if pushed down. **Buying more packs is approved**; the program tells the owner what to buy and keeps working | 3.6, 3.10 |
| 9 | First draft of points, tuned once after two weeks of real reports | 3.5, phase 8 |
| 10 | Finish one product at a time; AI groups whenever several are ready. **Never block**: blocked work waits in a list, everything else continues, fallbacks everywhere | 3.4, 3.7 |
| 11 | Suppliers a bonus; store listing + IndiaMART by kind of product | 3.6 |
| 12 | TradeIndia/IndiaMART "new listings" side source — **comprehensive, not a short word list**; Amazon/Flipkart best-seller lists only for cross-checks | 3.1 |
| 13 | No repeats in the first 12 months; review supply in month 9 | 3.6 |
| 14–15 | Record every kind of token; slim AI calls | 3.8 |
| 16 | **No pause at 50%**: plan allowance to 100% (5-hour and weekly), then paid credits. Soft budget ≈500 credits per report (first two up to ≈1,000, then ≈750, then ≈500) | 3.8 |
| 17 | Daily "who used the AI" line including the owner's other Codex projects | 3.8 |
| 18 | Other projects may use credits; auto-refill is off | 3.8 (wallet alert) |
| 19, 21 | **Dropdash signs in automatically**; the owner will not be available; account holds no payment, address or KYC | 3.9 |
| 20 | Owner signed in to Dropdash on 17 October (≈07:20 UTC); the full list was captured the same morning: 5,189 products, all 21 categories, signed in throughout | 3.1 |
| 22 | IndiaMART alert every 20 minutes with Sign in now / Snooze 1 h / Mute | 3.9, 3.10 |
| 23 | Separate browser profiles per signed-in site; reminder if a sign-in window stays open over 10 minutes. **One deviation:** Google/Trends and Dropdash share today's profile, because Dropdash's saved password and session live there (so nobody signs in again); IndiaMART, the one that needs you, gets its own | 3.9 |
| 24–25 | Research stays stopped until built; fresh research records, keep store catalogues and the product list | phase 5 |
| 26 | **No security-only work** (no key rotation, no deleting the saved Dropdash pages for privacy) | not planned |
| 27 | Portal checks simplified to "owner approved, these are the approved files" (explained in the handoff; silence = yes) | phase 6 |
| 28 | Plain wording, "why it's a good pick" line, compliance note; **reports illustrated, marketing-grade, layered, easy narrative** | 3.6, phase 7 |
| 29 | Report mock-ups later, after the engine | phase 7 |

---

## 3. Design rules (the build's source of truth)

### 3.1 Where ideas come from

| Source | How it is read | How often | Share of AI naming |
|---|---|---|---|
| **DeoDap, BaapStore** (public) | Whole catalogues from their Shopify feeds (already built); best-seller, "most bought", state best-seller and "just arrived" lists | Full refresh weekly; lists daily | Main |
| **Dropdash** (signed in) | Whole catalogue: 5,189 products in 19 categories plus 2 sales sections (captured 17 October). Each category page is scrolled to its end; the program keeps the products the page receives from Dropdash's own app (with the exact order count) and the visible cards. Its card "units sold" ranges overstate 5–10 times, so the exact order count is used. Sign-in: 3.9 | Full refresh weekly; "Profitable"/"Testing" sections daily | Main |
| **Facebook Ad Library** (public, India, all ads) | Searches from the product list's 1,706 headings (short phrase first); headings never searched go first, rotated so the four categories stay balanced; Trends "rising" phrases join this queue | About 30 searches a day to start (setting) | Main |
| **TradeIndia** (public) and **IndiaMART** (signed in) "new listings" | Comprehensive rotation: first the 502 sub-area words of the product list, then every heading's short phrase (1,706), least recently searched first with the categories taking turns — about 4 weeks per sweep at ≈80 searches a day (TradeIndia often needs two reads per search) — then repeat, since new listings keep appearing (these sites have no "newest first" order, so "new" means "not seen before"). IndiaMART joins when signed in; otherwise TradeIndia alone | Daily | At most 20% of naming capacity (setting), so stores and Facebook dominate |
| **Product-list top-up** (existing) | The AI adds headings when fewer than 300 remain unsearched | When needed | — |

Not idea sources: Amazon/Flipkart best-seller and new-release lists (crowded by nature; used only as cross-checks).

**Order inside the pool:** store items with the strongest free signals first (on a best-seller or state list, Dropdash 1K+ units sold, new arrival, sold by several stores), so the top of the ranking fills first.

### 3.2 Free filter and hard removals (code only, no AI, no website visit)

Before naming, code drops: out-of-stock store items; items whose store category or title words are clearly outside the four categories (Home & Kitchen, Home & Decor, Baby, Travel); the **excluded kinds** (jewellery, adult cosmetics and beauty, perfume, clothing, stationery, phone cases and screen guards, chargers and cables, earphones and headphones, smartwatch straps, pets and aquariums — `config/flow/rules.json`); known big brands (store vendor field + brand list); near-identical titles (normalised words); items already named.

After naming, a product type is **removed** only when: the AI says big brand; the AI says clearly outside the four categories ("loosely related" stays); its kind is on the excluded list; the checklist **clearly fails** (≤2 "yes" **and** ≥5 "no", after "unclear"-heavy answers were asked once more); it was **sent to the client in the last 12 months**; or it is an **exact duplicate** of a known type (then its evidence joins that type). Every removal is recorded with its reason and counted in the trace. Expected removal rate is measured in week 1 (16 October: 24% of named products, before "loosely related stays").

### 3.3 The naming call (AI, up to 20 items per call)

One AI call takes up to 20 waiting items (it never waits for a full group: it runs as soon as the AI is free and at least 1 item waits; priorities in 3.8). Code first gives each item a shortlist of up to 5 similar known product types (word overlap). For each item the AI returns:
- kind: one product / several products (each named) / service / unreadable;
- product type name (generic, no brand), a **short search phrase (2–3 words)** and a longer phrase;
- category (one of the four, "loosely related", or "outside"), big brand yes/no;
- known-type match: one shortlist id, or "new";
- the 10 checklist answers (yes / no / unclear; criteria unchanged from the product contract section 4; only for a new type — items that join a known type inherit its answers);
- compliance group: baby, food, skin, medical, mains-electrical, or none.

Code checks every answer. An item with a malformed answer is asked again once in a later call; if it fails again it waits in the waiting list with reason `AI_ANSWER_UNUSABLE` and is counted — **never marked done unread**. A product with 3 or more "unclear" checklist answers gets one re-ask (with its store description) before its tier is fixed.

**Checklist tiers:** 5+ "yes" = main; 3–4 "yes" = other suggestions; ≤2 "yes" **and** ≥5 "no" = removed; ≤2 "yes" with fewer than 5 "no" after the re-ask = **low** (kept and counted, ranked, never reported).

### 3.4 Cross-checks: which places, by fixed rules

Each new product type (not removed) is finished in one go, in this order:

| Stage | Who | Places | Rule |
|---|---|---|---|
| **A — every product** | All new product types | **Stores** (DeoDap, BaapStore, Dropdash: free look-ups in the saved catalogues); **Facebook Ad Library**; **Google Trends**; **Amazon** | Facebook is skipped when the idea came from a Facebook ad (the ads the naming call assigned to it are its evidence). Short phrase first; if more than 60% of the results are unrelated, the longer phrase once. Searches are case-insensitive and never repeated for the same phrase within 30 days (the saved result is reused) |
| **B — near the top** | Products in the top 40% of the ranking, and any product about to be picked | **Flipkart** | Confirms the plus or the crowding minus (only these products can collect a second crowding reading; a small, accepted tilt) |
| **C — picked** | The products picked for a report | **Quick-delivery apps** (Blinkit, Zepto, Instamart; one city, Mumbai, one paid credit per app); **supplier look-up** (IndiaMART by kind of product, TradeIndia when IndiaMART is signed out); **photo** from the best listing | Only for picks |

After a product's stage-A (or B) results arrive, **one confirmation call** shows the AI the top 10 results per place (title, price, ratings, "bought" badge, advertiser, ad dates; listings sharing no strong word with the product are dropped first by code) and asks "same product / related / unrelated" for each; up to 5 products that are ready at the same moment share one call; nothing waits for a group. Store matches with borderline title overlap ride the same call. Code then computes the signals (3.5).

**Order of products:** highest free score first; one in five starts goes to a lower-ranked product at random, so new things also get a chance. Up to 3 products are in flight at once (setting), so a 90-second Trends gap never idles the other sites.

**Rankable:** a product enters the ranking once every stage-A check has a result, or has been parked for more than 24 hours (that place then counts 0 until its result lands, and the product is re-scored when it does).

**Throughput:** Google Trends allows one search every 90 seconds and 200 a day, so about **150–200 products a day** can finish stage A (measured in week 1). That is far more than three reports a week need; the first weeks work through the store backlog.

**The owner's "2 positives is enough":** every product gets the same stage-A checks (a few minutes of website time each, including pacing), so scores stay comparable. A product positive on 2 or more places is shown as "seen in N places" in the trace and in the report's "why it's a good pick" line, and its points already lift it into the upper part of the ranking. No product waits for more places once stage A is done.

### 3.5 Points (first draft; tuned once after two weeks of real reports)

Unknown earns nothing and loses nothing. A fact counts once. Only the order matters.

| Group | Signal | Points | Exact meaning |
|---|---|---|---|
| Stores (free) | Sold by 1 store / 2+ stores | +8 / +12 | Distinct stores with a confirmed same-product item in stock, origin included |
| | On a store list | +10 | A confirmed item on a best-seller, trending, "most bought" or state best-seller list, carrying DeoDap's "High Performing" tag, or with 1,000+ Dropdash orders (its exact order count) |
| | New in a store | +5 | DeoDap/BaapStore: the store's own `created_at` within 60 days, or on a "just arrived" list. Dropdash (no dates): first seen after the 17 October baseline, or in its "Testing Products" section |
| Appeal | Checklist | +2.5 per "yes" (max +25) | |
| Facebook | 1 ad / 2+ advertisers | +8 / +12 | Running ads count 1, stopped ads 0.5; weighted advertisers ≥2 → +12; ≥1 → +8; 0.5 → +4 |
| | Recent ad | +4 | A confirmed ad started in the last 30 days |
| Trends | Rising / steady / falling / no chart | +15 / +8 / 0 / 0 | Rising: last 3 months ≥15% above the 3 before; steady: within ±15% |
| Amazon/Flipkart | A few matching listings, not heavily sold | +5 | ≥1 confirmed listing and not "heavy" |
| Push-downs | Crowding medium / high | −5 / −15 | High ("heavy"): a best-seller badge on a confirmed listing, or ≥3 confirmed listings with ≥1,000 ratings, or ≥2 with "1K+ bought last month"; medium: 2 confirmed listings with ≥1,000 ratings or 1 with "1K+ bought"; high replaces the +5. (A new ladder; today's `crowding.crowding_level` rules are not reused as they are) |
| | Quick-delivery 2 apps / 3 apps or ≥10 listings | −5 / −10 | Picks only |
| Bonus | Supplier outside the stores | +5 | An IndiaMART or TradeIndia lead for its kind of product |

Highest possible ≈ 93. Points and their basis lines (each sub-point with its evidence) are stored with the product, so the trace adds up.

**Percentile:** for the main tier only (rankable products with 5+ "yes", not sent): the share of that pool at or below the pick's points when its report is assembled, shown as "Top 8%". The bands in 3.6 use the same pool.

**Tuning (owner question 9, in plain words):** after about six reports, the agent checks whether the top of the list looks right — did picks later appear on store best-seller lists or gain Facebook advertisers, and what the owner and the client thought — then proposes one change to these numbers with examples. The owner replies only to disagree.

### 3.6 Report assembly (the evening before each report)

- Reports: Tuesday, Thursday, Saturday at 09:00 India time; assembly starts 18:00 India time the evening before, and finalises with what is known by 06:00 India time (settings).
- Pool: rankable products, minus anything sent or reserved in the last 12 months, minus near-duplicates of the last two reports.
- **Main picks (up to 10):** from the main tier, ranked by points, split into thirds: 5 from the top third, 3 from the middle third, 2 from the lower third but never from the bottom fifth (that is, ranked in the top 67–80%). Categories balanced: each of the four categories at least 1 when available, at most 4. No two picks that are near-duplicates (code word overlap; one AI "pair check" only when two picks look alike; if the AI is unavailable, a look-alike pair is treated as near-duplicates and the next product is taken).
- **When rules collide** (common while the pool is small): relax the category minimum first, then take from the next band up; never relax near-duplicates or the 12-month rule.
- **Other suggestions (up to 5):** the best-ranked products with 3–4 "yes", shown under "More ideas worth a look", no score.
- For the picks: stage B (Flipkart) if not done, then stage C (quick-delivery, supplier, photo). A pick pushed out of its band by a quick-delivery minus is swapped for the next product in that band.
- Suppliers: the store listing (name, wholesale price, link) is shown as the supplier; an IndiaMART/TradeIndia lead is added when found.
- Writing: one AI call per report and language writes the product texts from saved facts only; the "why it's a good pick" line is written by code from saved facts ("On DeoDap's best-seller list · seen in 3 places · rising on Google"); a "Client Compliance Check Required" note for baby, food, skin, medical and mains-electrical products. If the AI is unavailable, code-written text from saved facts is used.
- Output: English PDF, Roman-Hinglish PDF (first two reports, then the owner's language choice — handoff question), one Excel workbook; a private draft for the owner; uploaded and scheduled on the private website after the owner clicks **Approve**.
- **Never stuck:** a draft not yet approved stays approvable for 7 days and goes out at the next free slot after approval (the portal's late-release option); its picks stay reserved until approval or 7 days, then return to the pool. If staging or the private website fails, the window's "Open" shows the local PDFs and workbook and staging retries every hour. If PDF printing waits (Windows Chrome unavailable), the HTML and the workbook are still produced and the PDFs follow.
- **Shortage:** a report goes out with 5–15 products (fewer main picks if the main tier is small, plus the other suggestions), with a line to the owner saying why; with 1–4 products it is held and the owner is told.
- Month 9: the trace's supply line ("ideas ready to rank: N, about W weeks") is reviewed; older products may return later with a new angle only with the owner's agreement.

### 3.7 Never block

- **Health register:** each dependency (each website, each sign-in, the AI, the quick-delivery credits, the Windows desktop/Chrome, the private website) has a state: working / waiting (reason, next try time) / needs the owner.
- **Waiting list:** a check that cannot run is parked with its reason and next try time; the product carries on with its other checks and is scored with what is known. Parked work resumes automatically, picks first, then oldest first. Every parked item is visible in the trace with its reason.
- **Nothing is ever marked done without a result.**
- **Retry:** 5 minutes, 15, 60, then every 3 hours; a site that refuses (blocked, captcha) rests for 6 hours.

| If this stops | Everything else carries on, and… |
|---|---|
| Windows screen locked or computer asleep | Signed-in checks (Trends, Dropdash, IndiaMART) wait; public checks and store feeds continue; products still become rankable after 24 hours (3.4) |
| Google (Trends) signed out | Trends counts as unknown (0); the owner gets the alert (rare) |
| IndiaMART signed out | Supplier look-ups use TradeIndia; the 20-minute alert with Snooze/Mute |
| Dropdash signed out | Automatic sign-in (3.9); after two failures in a row it retries on the 6-hour rest within its daily cap; the saved catalogue keeps serving look-ups; the owner is needed only if Dropdash shows a one-time code or a puzzle |
| Facebook Ad Library refusing | Heading searches wait; store ideas continue; after 6 hours of refusals the Apify fallback searches Facebook within the month's free allowance (phase 8) |
| Amazon or Flipkart refusing | That check waits; scores use what is known |
| Quick-delivery credits used up | Picks go ahead without that check; the owner is told exactly what to buy |
| AI unavailable (Codex down; usage limit and no credits; Codex sign-in lapsed) | Collecting and website checks continue; naming and confirmation wait; report text falls back to code-written text; the owner is told if a sign-in is needed |
| A Codex update renames a setting | The program drops unknown settings at start-up; if Codex still refuses, it uses the plain (un-slimmed) command and tells the owner |
| Windows Chrome unavailable | Store feeds (plain web requests) continue; browser checks and PDF printing wait |
| Private website staging or deploy fails | Local report files open from the window; staging retries hourly |
| The database is busy | Each worker waits up to 30 seconds for its turn (short transactions), then retries |

### 3.8 AI use, cost and the credit budget

**Calls.** Five AI steps remain: naming (3.3), confirmation (3.4), report writing (3.6), the occasional near-duplicate pair check (3.6) and the product-list top-up (new headings when fewer than 300 remain unsearched). Today's other AI steps (separate definition, duplicate checks per type, listing-by-listing reviews, supplier page reading, best-seller row reviews) are deleted.

**Priority** when several kinds wait (one AI worker): report writing, then pair check, then confirmation, then naming, then top-up; while confirmations wait, naming takes at most every second call. Each call still carries up to 20 items and never waits for a group.

**Slim calls** (measured 17 October: a near-empty research call carried 15,000 tokens of Codex's own material):
- our own short instructions through Codex's `model_instructions_file` (−3,691 tokens);
- research runs with `HOME` pointed at an empty folder (keeping `CODEX_HOME`), so the owner's 50 coding "skills" are not attached (−4,226);
- the unused Codex features switched off (−760), except `code_mode_host`.
Re-measured three times in phase 3 before the savings are quoted.

**Thinking level and model per step** (settings; checked on saved examples in phase 3): naming GPT-6 Luna at medium; confirmation and pair check Luna at low; report writing Luna at high; top-up Luna at medium. GPT-6 Sol costs about 20 times as much as Luna (naming the backlog on Sol would cost about 1,000 credits) and uses the shared allowance much faster, so a step moves to Sol only if phase 3 shows a clear quality gain.

**Accounting for every call** (`codex exec --json` reports these on `turn.completed`): input, cached input, cache-write, output and reasoning-output tokens; model and thinking level; seconds; items carried; the step; the 5-hour and weekly allowance before and after and the credit balance (read with today's Codex app-server reader — research calls are `--ephemeral` and leave no session logs); and a **credit-equivalent cost** = (input − cached) × 2.5 + cached × 0.25 + output × 12.5 credits per million tokens for GPT-6 Luna (cached tokens are part of input and reasoning is part of output, so they are not charged twice; `config/ai/rate_card.json`). Using the rate card (not the wallet's change) keeps research's cost separate from the owner's other projects, which draw from the same wallet; a weekly check compares it with wallet drops at moments when nothing else was running.

**Allowance and credits (owner answer 16).** No pause at 50%. Research uses the plan allowance up to 100% of the 5-hour and the weekly limit; Codex should then draw on the paid credits by itself (OpenAI offers no way to choose credits first). The three refusals in today's code go, including the `paid_fallback` setting. The first time a window shows 100%, the program records whether the credit balance moved; if Codex answers "usage limit" instead, AI work waits for the reset and everything else carries on.

**Credit budget per report** (soft): reports 1–2: 1,000 credits; reports 3–4: 750; report 5 onwards: 500 (settings; "first couple" read as two reports at each step). Counted as the credit-equivalent cost of research's calls since the previous report's assembly began, whether the allowance or the wallet paid. At 100% of the budget the window shows an amber line and work continues. Only as a guard against a program fault: past 300% (for example 1,500 of 500) the AI keeps to report work (confirmations for picks and writing) until the next report and the owner is told. Expected use is tens of credits per report, so neither normally happens.

**Wallet alert:** when the paid balance falls below 3,000 credits (setting), the owner gets one desktop notice a day with the steps to add credits (Codex Settings → Usage → Credits). Research never buys anything.

**Daily lines in the window and the trace:** "Research AI today: N calls, X% of the weekly allowance, Y credits (credit-equivalent)"; "Your other Codex projects today: N calls, X% of the weekly allowance" (numbers only, from `~/.codex/sessions`; research's share of the whole-number allowance movement is split by tokens).

### 3.9 Sign-ins

**Dropdash — automatic.**
1. **Primary: Chrome's own saved password.** The research profile already autofills Dropdash's phone and password (owner, 17 October). When a read meets Dropdash's sign-in page, the program waits up to 10 seconds for Chrome's autofill (fields marked `:-webkit-autofill`; the check returns yes/no only), sends one real mouse click (DevTools `Input.dispatchMouseEvent`) on the phone field, waits 300 ms, then clicks "Sign in". Chrome releases the autofilled values on that real click. The program's code never reads, stores or prints the password.
2. **Second: the owner's `.env`.** If autofill did not appear, the program reads the phone number and password at that moment from the owner's `.env` in the project folder (accepted keys: `DROPDASH_PHONE` or `DROPDASH_MOBILE`, and `DROPDASH_PASSWORD`), types them through DevTools `Input.insertText`, and clicks "Sign in". The values exist only in memory for that step; never in a file, a log, a command line or a saved page. If the keys are not found, one quiet notice names the expected keys (handoff question 7).
3. **Limits:** at most one attempt every 30 minutes and 12 a day; after two failures in a row, retries follow the 6-hour rest within the daily cap; the owner is needed only when Dropdash shows a one-time code or a puzzle (one quiet notice).
4. **Keep-alive:** one light visit to Dropdash's home page every 2–3 hours; each visit records whether the session was still alive, so the real session length is known after a week (17 October: still signed in 1 h 40 min after the owner's sign-in).
5. **Sharing the profile with Trends:** Dropdash reads one category page per turn, interleaved with Trends searches; the weekly full refresh runs overnight; a program Chrome on Dropdash's sign-in page is closed after each attempt.
6. **Agent rule:** the building agent never opens `.env`, `dropdash.txt`, `config/local/*`, cookies or saved passwords, and never types the password itself; tests use a fake sign-in page. The live check is the program signing itself in (phase 4).

**Browser profiles (owner answer 23).** Profile A, today's `research-browser-profile`, keeps **Google/Trends and Dropdash** (it holds Dropdash's saved password and both sessions, so nothing has to be signed in again). Profile B, new, holds **IndiaMART** only: its frequent sign-ins (the phone code) then never stop Trends or Dropdash, and the two profiles can be read at the same time. Public sites keep their own temporary profiles. Each profile has its own sign-in button in the window; a sign-in window open for more than 10 minutes gets a reminder.

**IndiaMART — the owner.** It has never been signed in, so its alert (3.10) starts when research restarts; only IndiaMART look-ups wait.

**Google** stays signed in for months; if it signs out, the same alert (rare).

### 3.10 What the owner sees

| Notice | When | How |
|---|---|---|
| IndiaMART needs the phone code | IndiaMART signed out | A small popup at the bottom right with a chime, repeated every 20 minutes: "Sign in now" (opens profile B on IndiaMART), "Snooze 1 hour", "Mute for this site" (window shows "IndiaMART reminders muted — Unmute") |
| Google needs signing in | Rare | Same popup, profile A |
| Quick-delivery pack needed | Credits below what one report needs (15 picks × 3 apps = 45 credits) | Once a day: which pack (₹500 = 2,500 credits, about 55 reports), where to buy it, where the key goes (steps from the operator guide); work continues |
| AI wallet low / Codex sign-in needed | Below 3,000 credits / Codex asks to sign in | Once a day, with steps |
| Dropdash needs a code or puzzle | Rare | Once, quiet (no chime) |
| Report ready to approve | Each report | Once, with chime; "Open" shows the draft and the **Approve** button |
| Daily summary | Each evening | One line in the window: products added, checked, ranked; report status; AI and credits |

### 3.11 Seeing every step

- The trace tool (`scripts/research_trace.py`) is rebuilt on the new database: **funnel** (every stage with counts that add up, and every removal and wait reason), **websites** (visits, results, waits, times), **AI** (calls, items, every token kind, credit-equivalent cost per step and per report), **product** (one product's journey), **supply** ("ideas ready to rank: N, about W weeks").
- A plain status page ("See research steps" in the window), refreshed every few minutes: today's funnel, the waiting list by reason, the top 20 ranked products and why, AI and credits today, the next report's assembly time.

---

## 4. Architecture

### 4.1 Why the old engine is replaced, not repaired

Three surveys of the code (17 October) found that nothing in today's loop can carry product-by-product work:
- `programme.py` (5,338 lines) rotates 9 stages over "review groups" every round; `work_turns.py` caps each round at 16 reads and 10 AI calls; `source_coverage.py` (4,131) plans a question per type per site.
- All research state lives in a hash-chained event store (`research_memory.py`: one JSON file per event, 9,081 events and 105 MB on 16 October). Every read replays and deep-copies the whole state, which is why record-keeping grew from 1.7 s to 41 s per website read.
- The AI client (`research_ai.py`, 2,300 lines) carries about 25 prompt versions for steps that are going away, and refuses to spend credits in three places.

**Kept and adapted** (each at its edge): the website capture functions (Facebook Ad Library, Amazon, Flipkart, Google Trends, TradeIndia, IndiaMART, quick-delivery API), the store catalogue downloads (DeoDap/BaapStore feeds, Dropdash reader), the Windows Chrome engine, the allowance reader, the 16 October report redesign, delivery packaging, private drafts, the release index (v2 paged), the portal's identity/hash/schedule checks, backups' content-addressed parts and retention, the launcher.

**Deleted** (with their tests): the round/desk loop, the event store, the old AI steps and prompts, functional types/aliases/lineage, fit migrations, the opportunity score and its gates, scope splitting, supplier and company page reading, Google/Maps supplier search, the deeper-Meta Apify read, Trends extras, the wider-discovery lanes, material updates, the old meter and trace funnel, the `speed` and `evidence` branch work. Estimated: about 50,000–60,000 source lines and about 160 test files (about 60,000–75,000 test lines).

### 4.2 The new shape

```
src/product_radar/flow/          ← new package, ~25 small files, one job each
  db.py           SQLite store: open, schema, transactions
  config.py       loads and checks config/flow/*.json
  text_match.py   words, similarity, conflict veto (pure)
  search_key.py   site + case-folded phrase + country
  intake_stores.py  catalogue → listings, tiers, new arrivals
  intake_facebook.py  next heading → Facebook search → listings
  intake_new_listings.py  TradeIndia/IndiaMART rotation → listings
  free_filter.py  excluded kinds, category, brands, near-copies, sent
  ai_client.py    one slim `codex exec` call, 5 token fields
  ai_meter.py     allowance, credit-equivalent, budget, daily lines
  ai_queue.py     "send whatever is ready, up to 20"
  ai_naming.py    naming step: payload, schema, apply answers
  ai_topup.py     product-list top-up step (admission rules from map_topup)
  checklist_rules.py  tiers, removal, re-ask
  grouping.py     product types (groups): join / new / veto
  store_lookup.py  product ↔ store catalogues by title
  store_signals.py  stores, lists, new-in-60-days
  points.py       points table, basis lines, percentile
  site_gate.py    pacing, back-off, sign-in state per site
  parked.py       blockers and the waiting list
  reader_adapters.py  one check(site, phrase) per site
  search_phrases.py  short first, long if unrelated
  check_plan.py   which places, in which order (rule table)
  crosscheck.py   runs a product's places, in-flight window
  confirm_same.py  confirmation step: payload, schema, apply
  crowding_rules.py  none / few / medium / high from confirmed listings
  issue_plan.py   report calendar, assembly time, sent history
  report_picker.py  10 main (5/3/2) + 5 other, balance, near-copies, pair check
  pick_finalise.py  stage B/C for picks, swaps, supplier, photo
  report_reasons.py  code-written "why it's a good pick" (EN + Hinglish)
  report_text.py  AI product text, code fallback
  report_dataset.py  picks → the report renderer's dataset
  trace.py        counts that add up, per stage / site / AI / product
  runner.py       the main process: workers, owner switch, heartbeat
config/flow/  rules.json  points.json  check_plan.json  sites.json  schedule.json
config/ai/    codex_luna.json (effort per step, slim settings, budget list, wallet floor)  rate_card.json
scripts/run_programme.py   → flow.runner.main
scripts/research_trace.py  → flow.trace
scripts/fresh_start.py     (new) preview / execute the R19 fresh start
scripts/approve_report.py  (new) owner Approve → portal activation
```

**One database.** `research.sqlite3` in the runtime folder on the Linux disk (`/home/anan/neuraflow-dropshipper-data/data/flow/`; never on `/mnt/d`, where file locking is unreliable). WAL mode, `synchronous=FULL` (a power cut emptied files on 16 October), foreign keys on, `busy_timeout` 30 s, one connection per thread, short transactions. Raw pages and AI request/answer files stay on disk as gzipped files referenced by path and SHA-256, so the database stays small. Tables: `listings`, `groups` (product types, with their points, basis lines and scoring time), `checks`, `reads`, `ai_jobs`, `ai_queue`, `blockers`, `parked`, `issues`, `picks`, `sent_history`, `headings_served`, `terms_served`, `trace_events`.

**One process, a few workers.** `runner.py` runs a short main loop; one worker thread per site group (public browser reads may run two at a time; each signed-in profile one at a time) and one AI worker. Every state change is one SQLite transaction. Up to 3 products are in flight in cross-checks (setting; 1 = strictly one at a time), so a 90-second Trends gap never idles the other sites.

**Restart safety.** A read writes its file first, then marks `reads.status = saved` with the hash; on start, saved-but-unapplied reads are applied, free reads left `sent` go back to the queue, paid quick-delivery reads left `sent` become `unknown_paid` and are never retried blind (the balance is checked first). An AI answer file is applied on restart; otherwise its items are queued again.

**Fresh start (owner answer 25).** `scripts/fresh_start.py` (preview, then `--execute --expected-plan-hash`, with a receipt; moves by rename, deletes nothing): checks that the 12 headings the AI added on 16 October are already in `config/research/catalogue.json` (exported in phase 0, while the old event store still exists); moves the DeoDap and BaapStore catalogues and the converted Dropdash catalogue of 17 October to `data/stores/<store>/` (the store folder setting changes with it); keeps the owner switch, `spend_watch/`, `signed-in-browser/` state and the Apify money ledger (`data/benchmarks/`, for the Facebook fallback); archives everything else of the old research to `/home/anan/neuraflow-dropshipper-archive/<date>-before-r19/`. "New in 60 days" uses the stores' own `created_at` (all DeoDap/BaapStore items carry it) or the "just arrived" list, never `first_seen_at` (reset to 16 October by the last fresh start).

### 4.3 The AI client

- **Command:** today's `codex exec --ignore-user-config --ephemeral --skip-git-repo-check --sandbox read-only --json --output-schema … -` with its `--disable` list, plus: `--config model_instructions_file=<our step instructions>`, `--config include_permissions_instructions=false`, `--config include_environment_context=false`, the extra unused features switched off **except `code_mode_host`** (switching it off makes Codex emit an `error` item, which must stay a failure), `HOME` = an empty runtime folder and `CODEX_HOME` set explicitly to the real `~/.codex` (keeps the sign-in). Each step's fixed instructions live in the instructions file (a stable prefix that can be cached); only the data goes on standard input.
- **Thinking level per step** from `config/ai/codex_luna.json` (`naming: medium, confirm: low, writing: high, pair_check: low`); the level is part of the job's cache key and its accounting. The old fixed runner identity checks go.
- **Items per call:** up to 20 (today 10); deadline 420 s (today 180 s; measured in phase 3); 256 KiB input cap kept.
- **Checks:** exactly one answer; JSON schema per step; every item id exactly once; valid items are kept even when siblings fail; failed items are re-asked once, then parked (`AI_ANSWER_UNUSABLE`).
- **Sign-in check** (`codex login status`) once at start and then hourly, not before every call.
- **Codex updates:** at start-up the program runs `codex features list`, switches off only features that exist, and records the Codex version; if Codex still rejects the settings, it falls back to the plain (un-slimmed) command and tells the owner. The model per step is a setting passed on every call.
- **Allowance reading:** today's `spend_watch_ai.read_rate_limits` (Codex app-server `account/rateLimits/read`, numbers only) — fresh after every call; the "before" reading reuses the previous "after" when under 60 s old. Percentages are whole numbers; the credit balance has six decimals.
- **Credits:** the three refusals go (`paid_fallback` assertion, `AI_PLAN_LIMIT_CREDITS_PROTECTED`, `AI_PAID_CREDITS_USED`); the budget guard of 3.8 replaces them. A failed allowance reading no longer blocks: the call proceeds on the token-based estimate and the owner is told after 3 failures in a row. If Codex answers "usage limit" instead of drawing credits, AI work is parked until the window resets (everything else continues) and the case is reported.

### 4.4 Reports and the private website

- **The 16 October redesign is merged first** (branch `redesign`, phase 0; it conflicts only in documents already deleted). `r10` is not merged (it also carries R17, which is dropped).
- **Kept as they are:** `report_theme`, `report_charts`, `report_guard`, `report_workbook_style`, `scripts/render_live_pdf.py`, `private_drafts`, `draft_automation`, `release_pages` (the v2 paged index: the v1 index would overflow in about 14 months), `portal_storage`.
- **Adapted:** `live_outputs` (new dataset: main picks with percentile, other suggestions, reasons, compliance, store supplier, photo); `report_copy`/`report_pages`/`report_view`/`report_visuals` (score ring → "Top 8%" badge on main picks; the old 4-part bars → the points breakdown; an "other suggestions" page; supplier table → store listing; compliance bar for baby, food, skin, medical, mains-electrical); `report_workflow`, `report_assets`, `delivery_package`, `release_index` (aliases/lineage removed).
- **Deleted:** `report_narration` (old AI writing), `report_examples`, `report_source_facts`, `report_supplier_facts`, `supplier_scoped_display`, `portal_delivery_check` (after moving `ORIGIN` and `cached_owner_token`), and every proof/disclaimer phrase listed in the AI/report survey ("not a promise of sales", "not verified", "Leads to look into, not endorsements", "Not-positive", "Opportunity score … out of 100", and the rest).
- **Portal (owner answer 27):** keeps identity, method, header, size, upload-hash, magic-byte, immutability and read-back checks; manifest and per-file hashes re-checked on every download; the owner approval bound to owner id + package hash + scheduled time; ETag compare-and-swap; the Tuesday/Thursday/Saturday schedule; one active revision per issue and withdraw; product count 5–15; no product repeated within 12 months. Deleted: `validateFitPacket` (it would refuse every "other suggestion"), `validateFunctionalPacket`, `functional-packet.mjs`, `opportunity-score.mjs`, `supplier-scoped.mjs`, the four proof modules, alias/lineage, the 80%-new and "Updated" rules.
- **Python side:** `service.validate_issue` and `config/service.json` (today: at least 10 products, each with 5+ "yes") are rewritten to 5–15 products, no checklist or score re-checks, and the 12-month no-repeat by product type; it is called at activation and when the dataset is built.
- **Approve:** there is no usable Approve step today (nothing calls `release_index.activate_delivery_package`, and it has only been tested against fake storage). New: an **Approve** button in the desktop window (and `scripts/approve_report.py`) that shows the staged draft's files, records the owner's approval and posts the activation with the owner's Cloudflare Access session through the Windows `cloudflared.exe` (the owner's browser opens for Google sign-in only if that session has expired). Before report 1: read whether the live index is v1 or v2, and rehearse one activation of a synthetic issue, withdrawn straight after.
- **Deploy:** `npx wrangler deploy` from `portal/` with the existing Cloudflare sign-in (if it has expired, the owner signs in once — the only owner step here).

---

## 5. Build phases and tasks

### 5.0 Global constraints (every task)

- Stage: development and testing — replace directly; no migration of old records, no old/new side by side.
- Python standard library only for new code (no new packages); `sqlite3` for the store; tests in `unittest`.
- Work on branch `r19` in worktree `/home/anan/dropshipper-r19` (made from `main` after phase 0); each phase that loads is fast-forwarded into `main` (`git merge --ff-only r19`) after the whole local suites pass. Research stays stopped until phase 5.
- The research database and all runtime files live under `/home/anan/neuraflow-dropshipper-data/` (Linux disk), never under `/mnt/d`.
- Records use UTC; the report calendar uses India time (UTC+5:30), converted in one place (`issue_plan`).
- Settings live in `config/flow/*.json` and `config/ai/*.json`, each checked by `flow.config.load` at start (a bad setting stops start-up with a plain message).
- Nothing is ever marked done without a result; every wait and removal has a reason code and is counted in `trace_events`.
- The agent never opens `.env`, `dropdash.txt`, `config/local/*`, cookies or saved passwords, and never types a password; helpers are told the same.
- Report wording: plain, positive suggestions; none of the proof/disclaimer phrases listed in 4.4; AI text uses saved facts only and never promises sales or profit.
- Coverage ≥ 90% per new or changed module, no exclusions.
- Tests run with the main folder's interpreter (the worktree has no `.venv`): `cd /home/anan/dropshipper-r19 && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests "/mnt/d/Coding/Business/Ideas and Trials and Forked and Misc/dropshipping-product-and-supplier-finder/.venv/bin/python" -m unittest <module> -v`; portal tests with `PRODUCT_RADAR_PYTHON` set to the same interpreter.
- **Old-engine tests between phases 1 and 5:** a task that changes a module the old engine also uses deletes, in the same task, the old caller paths it breaks and the old-engine tests of exactly those paths (never weakens a test that still covers kept behaviour). New `flow` tests always pass; the whole suites must be green at every load (phases 0, 5, 6, 7, 8). Task 5.5 removes whatever is left.
- AI calls carry up to 20 items everywhere (settings and tests).

### 5.1 Review focus (the failure modes most likely to bite; each has a test in the owning task)

1. **Power cut in the middle of work** (WSL, 16 October): the database stays intact; reads saved but not applied are applied on restart; nothing is counted twice. → Task 1.1 (`test_reopen_after_interrupted_transaction`), Task 5.2 (`test_restart_applies_saved_reads_once`).
2. **A 20-item AI answer with a few bad items:** good items are kept, bad ones are re-asked once, then parked with `AI_ANSWER_UNUSABLE`; an AI outage leaves items "new", never "done". → Task 3.3 (`test_partial_answer_keeps_valid_siblings`, `test_ai_outage_leaves_items_new`).
3. **A website quietly changes its layout** (cards not recognised): the check is "unknown" with reason `LAYOUT_UNRECOGNISED`, parked and shown in the trace, never "nothing found". → Task 4.3 (`test_unrecognised_layout_is_unknown_not_nothing`).
4. **The owner's own Codex work fills the 5-hour window:** research continues on credits; if Codex refuses with "usage limit", AI work parks until the reset and every other stage keeps moving. → Task 3.2 (`test_usage_limit_parks_ai_only`), Task 5.2 (`test_runner_continues_when_ai_blocked`).
5. **The same product in two stores under slightly different titles:** it becomes one product type with both store listings; a report never carries two near-copies. → Task 2.3 (`test_cross_store_titles_join_one_group`), Task 6.1 (`test_no_near_duplicates_in_one_report`).
6. **The computer's screen is locked for hours while the owner is away:** signed-in checks wait, public checks continue, products still become rankable after 24 hours and are re-scored later. → Task 4.4 (`test_parked_trends_over_24h_still_rankable_and_rescored`).
7. **The owner has not approved a draft by report time:** the draft stays approvable for 7 days and goes out at the next free slot; its picks stay reserved meanwhile. → Task 6.4 (`test_unapproved_draft_released_late_and_picks_returned_after_7_days`).

### Phase 0 — Groundwork (about 1 day)

**Task 0.1 Merge the report redesign; open the build branch.**
- The 17 October document clean-up is already committed (leave the owner's uncommitted `CLAUDE.md` edit alone). Merge `redesign` into `main` (conflicts only in documents already deleted on 17 October: keep the deletions; `docs/README.md`: keep `main`'s version; delete the re-added `report-redesign-2026-10-16/production-renders/` images). Run the whole local suites. Create branch `r19` and worktree `/home/anan/dropshipper-r19`. Remove the worktrees of finished branches (`speed`, `evidence`, `sites`, `trace`, `obs`, `redesign`, `r10`); the branches themselves stay.
- Done when: `main` contains the eight R14 `report_*` modules; Python and portal suites pass; `git worktree list` shows `main` and `r19` only.

**Task 0.2 Export the 12 AI-added headings.** While the old event store still exists, export the 12 headings the AI added on 16 October (`catalogue_expansion.effective_catalogue` over the event store's `catalogue_additions`, encoded with `catalogue_rows.encode_catalogue`) into `config/research/catalogue.json` (1,694 → 1,706 headings) and commit it. Test: `test_catalogue_has_1706_headings` (in `tests/test_catalogue.py`).

**Task 0.3 Test fixtures from live data.** Copy trimmed saved pages (a few Facebook, Amazon, Flipkart, Trends, TradeIndia, IndiaMART and quick-delivery results) from the live `data/research/captured/direct_*` folders, and a trimmed slice of the 17 October Dropdash capture, into `tests/fixtures/r19/` (synthetic-safe: no account names or numbers), before the fresh start archives them.

### Phase 1 — Foundation (about 1½ days)

**Task 1.1 The database.** Files: create `src/product_radar/flow/__init__.py`, `flow/db.py`; test `tests/test_flow_db.py`.
- Interfaces: `open_db(path: Path) -> sqlite3.Connection` (WAL, `synchronous=FULL`, `foreign_keys=ON`, creates the schema of 4.2 when missing, `schema_version` row = 1); `transaction(conn)` context manager (BEGIN IMMEDIATE … COMMIT/ROLLBACK); `refuse_windows_mount(path)` raises `FlowError("DB_ON_WINDOWS_DRIVE")` for paths under `/mnt/`.
- Also: `PRAGMA busy_timeout=30000`; one connection per thread (`connect_for_thread()`); transactions kept short.
- Tests: `test_creates_schema_once`, `test_refuses_mnt_path`, `test_reopen_after_interrupted_transaction` (a child process killed mid-transaction leaves the previous state), `test_status_values_are_checked` (CHECK constraints reject an unknown status), `test_two_threads_write_without_locked_error`.

**Task 1.2 Settings.** Files: `flow/config.py`; `config/flow/rules.json` (categories, excluded kinds, big-brand list, removal thresholds 2 / 5, re-ask at ≥3 unclear), `points.json` (the 3.5 table), `check_plan.json` (3.4 stages), `sites.json` (gaps, daily caps, back-off ladder 300/900/3600/10800 s, refusal rest 21600 s, in-flight 3, browser parallel 2), `schedule.json` (Tue/Thu/Sat 09:00 IST, assembly 18:00 IST the day before, minimum 5 products); `config/ai/rate_card.json` (GPT-6 Luna 2.5 / 0.25 / 12.5 credits per million; GPT-6 Sol 50 / 5 / 250); test `tests/test_flow_config.py`.
- Interfaces: `load(name: str, root: Path) -> dict` (validated; unknown keys refused).
- Tests: `test_points_table_matches_plan` (each value of 3.5), `test_bad_setting_stops_with_plain_message`.

**Task 1.3 Words and search keys.** Files: `flow/text_match.py` (moved from `discovery.normalise/tokens/meaningful_tokens/find_possible_matches/explicit_conflict` and `market_validation._retrieval_tokens/_strong_tokens`), `flow/search_key.py`; tests `tests/test_flow_text_match.py`, `tests/test_flow_search_key.py`.
- Interfaces: `tokens(text) -> list[str]`; `similarity(a, b) -> float`; `shortlist(candidates, title, limit=5) -> list[str]` (group ids); `explicit_conflict(a, b) -> bool` (manual vs powered, adult vs baby); `strong_overlap(query, title) -> bool`; `search_key.make(site, phrase, country="IN") -> str`.
- Tests: `test_case_and_spacing_give_one_key` ("Baby Lotion" = "baby  lotion"), `test_conflict_vetoes_manual_vs_electric`.

**Task 1.4 Blockers and the waiting list.** Files: `flow/parked.py`; test `tests/test_flow_parked.py`.
- Interfaces: `open_blocker(conn, dependency, kind, *, owner_text, next_try_at) -> int`; `park(conn, blocker_id, work_kind, work_id)`; `release(conn, blocker_id) -> int` (requeues in original order); `due_probes(conn, now) -> list[Blocker]`.
- Tests: `test_release_requeues_in_original_order`, `test_parked_items_counted_in_trace`.

### Phase 2 — Store intake, free filter and free score, no AI, no websites (about 1½ days)

**Task 2.1 Catalogue intake.** Files: `flow/intake_stores.py` (uses `platform_roles._tier`, `item_category`, `excluded_kind` logic moved in); adapt `dropship_platforms.py` (remove research-event coupling `record_catalogue_states`); test `tests/test_flow_intake_stores.py` with frozen 200-item slices of the real DeoDap, BaapStore and Dropdash catalogues.
- Also: convert the 17 October Dropdash capture (a list of 5,189 rows) into the catalogue shape `platform_matching` expects (`items[id]` with `roles` from `sections`/`order_count`, `tags`, `last_seen_at`); update the store folder setting (`platform_folder`) for `data/stores/<store>/`; Dropdash newness = first seen after the 17 October baseline, or in its "Testing Products" section.
- Interfaces: `sync_catalogue(conn, store: str, catalogue: dict, now) -> SyncResult(new: int, updated: int, delisted: int)`; `next_for_naming(conn, limit: int) -> list[Listing]` (order: list items and Dropdash ≥1,000 orders, then 2+ state lists, then new arrivals, then the rest; stores take turns).
- Tests: `test_new_arrival_uses_created_at_not_first_seen`, `test_dropdash_uses_order_count_not_card_range`, `test_out_of_stock_skipped`, `test_order_puts_list_items_first`.

**Task 2.2 Free filter.** Files: `flow/free_filter.py`; test `tests/test_flow_free_filter.py`.
- Interfaces: `screen(listing, rules) -> Verdict(keep: bool, reason: str | None, maybe_bin: bool)`. Items with no category match go to the "maybe" bin (`maybe_bin=True`), read only when the main backlog is empty.
- Tests: `test_excluded_kinds_dropped` (each kind of 3.2), `test_unclear_category_goes_to_maybe_bin`, `test_near_identical_titles_dropped`, `test_sent_in_last_12_months_dropped`.

**Task 2.3 Product types, store look-ups and points.** Files: `flow/grouping.py`, `flow/store_lookup.py` (from `platform_matching.words/_stem/match_terms/local_matches`: ≥0.67 title coverage and ≥2 words), `flow/store_signals.py`, `flow/points.py`; tests `tests/test_flow_grouping.py`, `test_flow_store_lookup.py`, `test_flow_points.py` (fixture: the 34 product types of 16 October with their known facts).
- Interfaces: `grouping.join_or_create(conn, listing_id, match: str | None, name, phrases, category, brand) -> group_id` (refuses a join on `explicit_conflict`); `store_lookup.matches(catalogues, group) -> list[StoreMatch(store, item_id, coverage, roles, order_count, created_at)]` (coverage 0.67–0.8 marked `borderline` for confirmation); `store_signals.signals(matches, now) -> StoreSignals(stores: int, on_list: bool, new60: bool)`; `points.score(facts: GroupFacts, table) -> Score(points: float, basis: list[BasisLine])`; `points.percentile(conn, group_id) -> int`.
- Tests: `test_unknown_scores_zero`, `test_water_bottle_ranks_above_duster` (40.5 vs 36.5 on the 16 October facts), `test_baby_lotion_ranked_not_blocked` (10.5), `test_stopped_ads_count_half`, `test_high_crowding_replaces_plus_five`, `test_cross_store_titles_join_one_group`, `test_basis_lines_add_up_to_points`.
- Done when: a dry run over the three kept catalogues prints, with numbers that add up: items in, kept, maybe bin, dropped by reason, and the naming backlog by tier.

### Phase 3 — The AI client and the naming step (about 2½ days)

**Task 3.1 The slim client.** Files: `flow/ai_client.py`; adapt `scripts/check_codex_luna_connection.py` to use it; `config/ai/codex_luna.json` (effort per step, deadline 420, item limit 20, slim settings, `paid_fallback` removed); instruction files `config/ai/instructions/{naming,confirm,writing,pair_check}.md`; test `tests/test_flow_ai_client.py` (fake `codex` executable).
- Also: at start-up run `codex features list`, switch off only names that exist, record the Codex version; on `AI_CLI_CONFIGURATION_REJECTED` fall back to the plain command and raise an owner notice.
- Interfaces: `run(step: str, payload: dict, schema: dict, *, model: str, effort: str, deadline_s: int) -> AiResult(answer: dict | None, usage: Usage(input, cached_input, cache_write_input, output, reasoning_output), seconds: float, model: str, error: str | None)`; `build_command(step, effort, tmp) -> list[str]`; `child_environment(runtime_root) -> dict` (`HOME` = empty folder, `CODEX_HOME` = real).
- Tests: `test_command_has_slim_settings_and_no_code_mode_host_disable`, `test_parses_all_five_token_fields`, `test_error_item_is_failure`, `test_missing_usage_is_none_not_zero`, `test_login_check_hourly_not_per_call`, `test_unknown_feature_names_skipped`, `test_configuration_rejected_falls_back_to_plain_command`.
- Live (phase-end): three probe runs each of today's and the slim command on "return ok"; record tokens; check whether `~/.codex/AGENTS.md` is attached (by token difference only; the file is not opened).

**Task 3.2 The meter and the budget.** Files: `flow/ai_meter.py` (wraps `spend_watch_ai.read_rate_limits` and its history; reads the owner's other projects from `~/.codex/sessions/**/rollout-*.jsonl` `token_count` rows, numbers only); adapt `spend_watch_ai.py` (delete the three refusals and the fail-closed read); adapt `ai_accounting.py`; test `tests/test_flow_ai_meter.py`.
- Interfaces: `credit_equivalent(usage, model, rate_card) -> float` (= (input − cached) × in + cached × cached_rate + output × out, per million); `budget_state(conn, now) -> BudgetState(period_id, used: float, target: float, level: "ok" | "amber" | "fault_guard")` (targets 1000, 1000, 750, 750, 500…; amber at 100%, fault guard past 300%; report periods derived from `config/flow/schedule.json`); `wallet_alert_due(reading, floor=3000) -> bool`; `daily_lines(conn, day) -> list[str]`.
- Tests: `test_luna_rate_card_cost` (1M fresh input + 100k output = 3.75 credits), `test_cached_and_reasoning_not_double_counted`, `test_budget_amber_at_100_percent_fault_guard_past_300`, `test_usage_limit_parks_ai_only`, `test_read_failure_does_not_block`, `test_other_projects_line_reads_numbers_only`, `test_first_full_window_records_whether_credits_moved`.

**Task 3.3 The AI queue and the naming step.** Files: `flow/ai_queue.py`, `flow/ai_naming.py`, `flow/checklist_rules.py` (from `fit_policy.ACTIVE_CRITERION_IDS`), JSON schema `config/ai/schemas/naming.json`; tests `tests/test_flow_ai_queue.py`, `test_flow_ai_naming.py`, `test_flow_checklist_rules.py`.
- Interfaces: `ai_queue.next_batch(conn, max_items=20) -> Batch | None` (priority: writing > pair check > confirmation > naming > top-up; while confirmations wait, naming takes at most every second call; never waits); `ai_naming.build_payload(listings, shortlists) -> dict`; `ai_naming.apply(conn, job_id, answer) -> NamingOutcome(named, joined, removed_by_reason: dict, reasked, parked)`; `checklist_rules.tier(answers: dict, reasked: bool) -> "main" | "other" | "low" | "removed" | "recheck"`.
- Tests: `test_partial_answer_keeps_valid_siblings`, `test_bad_item_reasked_once_then_parked`, `test_ai_outage_leaves_items_new`, `test_removed_only_at_two_yes_and_five_no`, `test_two_yes_three_no_after_reask_is_low_not_removed`, `test_three_unclear_triggers_one_reask`, `test_brand_and_outside_removed_loosely_related_kept`, `test_confirmation_jumps_naming_backlog`.
- Live (phase-end, in a throwaway database): name 60 real store items (3 calls of 20): record tokens, credit-equivalent, seconds per call; the agent spot-checks 20 answers against the listings and writes the result into the implementation notes. If more than 3 of 20 are wrong on category or checklist, test GPT-6 Sol at medium on the same 60; switch only for a clear gain (Sol costs about 20 times Luna: about 1,000 credits to name the backlog).

### Phase 4 — Websites, sign-ins and cross-checks (about 4½ days)

**Task 4.1 Browser engine additions.** Files: adapt `native_public_browser.py` (trusted input `Input.dispatchMouseEvent`/`Input.insertText`; a scroll loop for a named scroll container with a longer per-read cap up to 600 s for catalogue pages; a profile name per site group), `signed_in_browser.py` (profiles: A = existing `research-browser-profile` for Google/Trends and Dropdash, B = new `research-browser-profile-indiamart`; per-profile lock, leftover clean-up and start-failure rest; `open_sign_in_window(site)` opens only that site; `sign_in_window_age()`); tests in `tests/test_native_public_browser.py`, `tests/test_signed_in_browser.py`.
- Also: keep the response bodies of a named site's own product-list requests while a page loads (product data only; never headers, cookies, storage or tokens), as the 17 October capture did (`scripts/reference/dropdash-capture-2026-10-17/cdp.ps1`).
- Experiment (removes the "keep the screen unlocked" need if it works): run Trends and Dropdash reads on profile A with `--headless=new` while the Windows screen is locked; if charts draw and sessions hold, signed-in reads stop waiting for an unlocked desktop.
- Tests: `test_trusted_click_uses_input_domain_not_js`, `test_scroll_loop_stops_after_two_quiet_loads`, `test_indiamart_window_does_not_block_profile_a`, `test_response_capture_keeps_bodies_only_for_allowed_paths`.

**Task 4.2 Dropdash: full catalogue and automatic sign-in.** Files: adapt `platform_signed_in.py` (full read: home page categories, then each category page scrolled to its end in its own scroll box at a 1280×640 view with a small up-then-down nudge; keeps the visible cards and the app's own product-list replies the page receives — product data only, never headers, cookies or tokens; `order_count` exact; a category is complete when its stated total is reached), new `flow/dropdash_sign_in.py`; tests `tests/test_dropdash_full_read.py` (fixtures from the 17 October capture), `tests/test_dropdash_sign_in.py` (fake sign-in page and fake CDP).
- Interfaces: `dropdash_sign_in.sign_in(cdp, *, now, limits) -> SignInOutcome("signed_in" | "autofill_absent" | "still_on_sign_in" | "code_or_puzzle" | "rate_limited" | "keys_missing")`: waits ≤10 s for `:-webkit-autofill` on both fields (the expression returns booleans only), trusted click on the phone field, 300 ms, trusted click on "Sign in", waits to leave `/signin`, confirms the signed-in marker. If autofill is absent: reads `DROPDASH_PHONE` or `DROPDASH_MOBILE` and `DROPDASH_PASSWORD` from the owner's `.env` at that moment, types them with `Input.insertText`, and clicks; values are never returned to callers, logged or saved; missing keys → quiet notice naming the expected keys. Limits: 1 attempt per 30 min, 12 a day; after two failures in a row, retries follow the 6-hour rest within the daily cap; a one-time code or puzzle → blocker `DROPDASH_NEEDS_OWNER` (quiet notice). The program Chrome on the sign-in page is closed after each attempt; Dropdash reads one category per turn, interleaved with Trends; the weekly full refresh runs overnight.
- Tests: `test_autofill_path_never_reads_values`, `test_env_fallback_values_never_logged_or_saved`, `test_missing_env_keys_raise_quiet_notice`, `test_two_failures_follow_six_hour_rest_not_owner`, `test_attempt_limits`, `test_full_read_reaches_at_least_stated_totals` (on the trimmed fixture), `test_greeting_name_never_saved`, `test_dropdash_full_read_yields_to_trends_between_categories`.
- Live (phase-end): the program itself runs the keep-alive and, when the session lapses, the automatic sign-in; the agent records the outcome only. If Claude Code's safety rule stops the agent from running this live step, the step is listed for the owner with the exact message and phase 4 carries on.

**Task 4.3 Site gate and reader adapters.** Files: `flow/site_gate.py` (back-off ladder moved from `source_coverage.ACCESS_BACKOFF_SECONDS`; pacing from `signed_in_pacing`), `flow/reader_adapters.py` (wraps `meta_browser.capture_meta_search`, `marketplace_browser.capture_marketplace` + `marketplace_cards.read_capture_cards`, `trends_controller.capture_direct_trends` + the CSV parser, `supplier_route_acquisition` TradeIndia parsing, `indiamart_signed_in` + `indiamart_results`, `QuickCommerceClient`); adapt `trends.py` (rising / steady / falling at ±15%) and keep a small parser for the "rising" related searches that `trends_controller` already returns; delete the reader-side handler layers (`meta_direct_source_handlers`, Trends shared/Playwright/rising/proof-gate modules, the Amazon best-seller route and trial); tests `tests/test_flow_site_gate.py`, `test_flow_reader_adapters.py` (fixtures from Task 0.3).
- Interfaces: `site_gate.ask(conn, site, now) -> Go | Wait(reason, retry_at, blocker_id)`; `site_gate.record(conn, site, outcome, now)`; uses `parked` (Task 1.4); `reader_adapters.check(site, phrase, *, folder, context) -> CheckResult(state: "found" | "nothing" | "unknown" | "waiting", records: list[dict], facts: dict, seconds: float, retry_at: datetime | None, reason: str | None)`.
- Tests: `test_unrecognised_layout_is_unknown_not_nothing`, `test_refusal_rests_six_hours`, `test_trends_three_way_signal`, `test_rising_phrases_parsed`, `test_quickcommerce_paid_read_left_sent_becomes_unknown_paid`, `test_locked_desktop_waits_signed_in_only`.

**Task 4.4 Cross-checks and confirmation.** Files: `flow/search_phrases.py`, `flow/check_plan.py`, `flow/crosscheck.py`, `flow/confirm_same.py`, `flow/crowding_rules.py` (from `crowding.crowding_level/threshold_ladder/_collapse_variants`, "example offer" split dropped, thresholds per 3.5), schema `config/ai/schemas/confirm.json`; tests `tests/test_flow_check_plan.py`, `test_flow_crosscheck.py`, `test_flow_confirm_same.py`, `test_flow_crowding_rules.py`.
- Interfaces: `check_plan.places(group, origin, known, stage) -> list[Place]`; `crosscheck.advance(conn, now) -> Progress` (keeps ≤3 products in flight; 1 in 5 starts goes to a random lower-ranked product); `search_phrases.next_phrase(group, site, last: CheckResult | None) -> str | None`; `confirm_same.build_payload(conn, group_ids) -> dict`; `confirm_same.apply(conn, job_id, answer) -> None`; `crowding_rules.level(confirmed: list[Listing]) -> "none" | "few" | "medium" | "high"`.
- Tests: `test_facebook_skipped_for_facebook_origin`, `test_short_phrase_first_long_if_mostly_unrelated`, `test_same_search_key_reused_within_30_days`, `test_confirmation_covers_all_places_of_a_product`, `test_free_prefilter_drops_listings_without_strong_words`, `test_stage_b_only_for_top_40_percent`, `test_parked_trends_over_24h_still_rankable_and_rescored`, `test_crowding_ladder_matches_points_table`.

**Task 4.5 Owner alerts and sign-in buttons.** Files: adapt `src/product_radar/desktop_control.py`, `scripts/desktop_research_control.py` (`snooze <site>`, `mute <site>`, `unmute <site>`), `scripts/windows/DropshipperDesktop.cs` (a small always-on-top popup at the bottom right with "Sign in now / Snooze 1 hour / Mute for this site" and a `SystemSounds` chime, repeated every 20 minutes while the need lasts; one Sign-in button per profile; a reminder when a sign-in window has been open for 10 minutes; quiet notices without chime per 3.10; the "Report ready to approve" notice and the Approve button arrive in Task 6.4); tests `tests/test_desktop_control.py`, `scripts/windows/Test-Dropshipper.ps1`.
- Tests: `test_indiamart_alert_repeats_every_20_minutes_until_signed_in_or_muted`, `test_snooze_one_hour`, `test_mute_shows_unmute_line`, `test_window_open_10_minutes_reminder`.
- Done when (phase 4): each adapter has run once live on a real product and returned found/nothing/unknown correctly; the Windows app is rebuilt and reinstalled.

### Phase 5 — The runner, idea intake, fresh start and restart (about 3 days + 1 day of measuring)

**Task 5.1 Facebook and new-listings intake.** Files: `flow/intake_facebook.py` (adapt `heading_order` to Facebook only; Trends "rising" phrases join the queue after unsearched headings), `flow/intake_new_listings.py` (rotation from `wider_candidates.supplier_targets`: first the 502 sub-area words of `product_map.json`, then every heading's marketplace phrase; least recently read first; categories take turns; TradeIndia always, IndiaMART when signed in; at most 20% of naming capacity); tests `tests/test_flow_intake_facebook.py`, `test_flow_intake_new_listings.py`.
- Also: `flow/ai_topup.py` — the product-list top-up as an AI step through `ai_queue` (admission rules ported from `map_topup.admit/known_word_sets/_same_or_variant`; schema `config/ai/schemas/topup.json`), when fewer than 300 headings remain unsearched.
- Tests: `test_only_facebook_search_uses_a_heading`, `test_rotation_reaches_every_heading`, `test_new_listings_share_capped_at_20_percent`, `test_topup_runs_below_300_unsearched`.

**Task 5.2 The runner.** Files: `flow/runner.py` (owner switch kept from `programme.programme_service_state`; heartbeat and status for the launcher via `operations.py`; workers per site group and one AI worker; store refresh on its own timer), `scripts/run_programme.py` → `flow.runner.main`; adapt `operations.py`, `store_housekeeping.py` (daily database backup trigger), `desktop_control.py` status lines (products added/checked/ranked today, waiting list by reason, AI and credits, next report); tests `tests/test_flow_runner.py`.
- Tests: `test_restart_applies_saved_reads_once`, `test_runner_continues_when_ai_blocked`, `test_owner_stop_finishes_current_step_only`, `test_every_stage_count_adds_up` (one simulated day on fixtures).

**Task 5.3 Trace and status page (first version).** Files: `flow/trace.py`; `scripts/research_trace.py` → `flow.trace`; a plain HTML status page written every 5 minutes to the runtime folder and opened by the launcher's "See research steps"; delete `trace_funnel.py`, `trace_records.py`, `trace_product.py`, `programme_summary.py`; adapt `trace_ai.py`, `trace_labels.py`, `trace_text.py`, `trace_routes.py`; tests `tests/test_flow_trace.py`.
- Tests: `test_funnel_adds_up_or_fails_loudly`, `test_waiting_list_shows_reason_and_age`, `test_ai_view_shows_all_token_kinds_and_credits`.

**Task 5.4 Backups for the new store.** Files: replace `operational_backup_selection.py` with a short selection (SQLite online backup copy + store catalogues + `config/` + read files of the last 30 days); adapt `operational_backup_schedule.py`, `operational_backup_parts.py`; keep `backup_retention.py`; tests rewritten in `tests/test_operational_backup_*.py`.
- Tests: `test_backup_uses_sqlite_backup_api`, `test_restore_rehearsal_opens_copy`.

**Task 5.5 Delete the old engine.** Delete the modules marked delete or replaced in the three surveys (listed in the phase's commit message), their tests, `config/research/round_limits.json`, `wider_discovery.json`, `opportunity_score.json`, and the `research_memory` event store code. Cut the imports of deleted modules from kept ones (each moves to `flow` or to the new read saving): `meta_browser` (`research_memory.import_capture`); `platform_signed_in`, `dropship_platforms`, `platform_feeds`, `quickcommerce`, `supplier_route_acquisition`, `product_images` (`research_memory.key/change`); `trends_controller` (`trends_capture`, `research_operations`); `signed_in_browser` (`signed_in_round`); `spend_watch_ai` (`research_ai`); `desktop_control` (`programme`, `opportunity_score`, `trends_proof_gate`); `heading_order` (`catalogue_discovery`, `map_materials`, `map_topup`); `release_index` (`functional_aliases`, `functional_lineage_portable`); `delivery_package`/`live_outputs` (`opportunity_score`, `report_examples`, `report_source_facts`, `supplier_effort`); and the 12 scripts in `scripts/` that import `research_memory` (deleted unless still needed). Run the whole local suites.
- Done when: no module imports `research_memory`, `programme`, `work_turns` or `source_coverage`; Python and portal suites pass; coverage ≥ 90% on `flow/`.

**Task 5.6 Fresh start and restart (load).** Files: `scripts/fresh_start.py`; test `tests/test_fresh_start.py` (preview, plan hash, rename-only, receipt, 1,706 headings present, Apify ledger kept).
- Steps: run the preview; execute; fast-forward `main`; rebuild/reinstall the Windows app; start research (owner's Start stays the switch).
- Done when (first 24 hours, measured — section 6): store items named, product types created and removed by reason, products through stage A, the waiting list, calls and credit-equivalent per day, allowance points moved, Dropdash session length. Results go into the tracker.

### Phase 6 — Reports and the private website (about 3½ days + the wait for a report slot)

**Task 6.1 Calendar and picker.** Files: `flow/issue_plan.py` (from `programme.research_week/release_slots`), `flow/report_picker.py`; tests `tests/test_flow_issue_plan.py`, `test_flow_report_picker.py`.
- Interfaces: `issue_plan.assembly_due(conn, now) -> Slot | None`; `issue_plan.finalise_by(slot) -> datetime` (06:00 IST); `report_picker.pick(conn, slot, rules) -> Picks(main: list[Pick(group_id, band, percentile)], other: list[Pick], notes: list[str])`; `report_picker.pair_check(conn, pairs) -> dict` (AI, schema `config/ai/schemas/pair_check.json`; if the AI is unavailable, every look-alike pair counts as near-duplicates).
- Tests: `test_five_three_two_from_thirds`, `test_never_from_bottom_fifth` (lower picks ranked in the top 67–80% of the main tier), `test_categories_at_least_one_when_available_at_most_four`, `test_relax_order_category_then_band_never_duplicates`, `test_no_near_duplicates_in_one_report`, `test_pair_check_fallback_treats_lookalikes_as_duplicates`, `test_no_repeat_within_12_months`, `test_percentile_for_main_only_from_main_tier_pool`, `test_short_pool_gives_five_to_fourteen_with_reason`, `test_one_to_four_products_held_and_owner_told`.

**Task 6.2 Finalising picks.** Files: `flow/pick_finalise.py` (stage B/C; swap a pick pushed out of its band; supplier = store listing + IndiaMART/TradeIndia lead; photo via `product_images.fetch_product_image`/`inspect_image` from the store listing), adapt `product_images.py` (cut the acquisition machinery), replace `quickcommerce_plan.py`/`quickcommerce_handlers.py` (picks only, one city, credits check first, owner pack message from `spend_watch_quickcommerce`); tests `tests/test_flow_pick_finalise.py`.
- Tests: `test_pick_pushed_down_by_quick_delivery_is_swapped`, `test_store_listing_is_supplier`, `test_no_credits_pick_goes_ahead_and_owner_told_what_to_buy`.

**Task 6.3 Report text and dataset.** Files: `flow/report_reasons.py` (code-written "why" line, English and Roman-Hinglish templates), `flow/report_text.py` (one AI call per report and language, from saved facts; code fallback), `flow/report_dataset.py`; schema `config/ai/schemas/writing.json`; adapt `live_outputs.py` (new dataset; delete the fit-13 legacy, price v1 and functional re-proofs), R14 `report_copy.py`, `report_pages.py`, `report_view.py`, `report_visuals.py` (percentile badge, points breakdown, "More ideas worth a look" page, store supplier table, compliance bar), `report_workflow.py`, `report_assets.py`, `delivery_package.py` (workbook check reduced to sheet names and rows); delete `report_narration.py`, `report_examples.py`, `report_source_facts.py`, `report_supplier_facts.py`, `supplier_scoped_display.py`, and every phrase listed in 4.4; tests rewritten: `tests/test_live_outputs.py`, `test_report_redesign_*`, new `tests/test_flow_report_text.py`, `test_flow_report_dataset.py`.
- Tests: `test_no_banned_proof_phrases_anywhere_in_output`, `test_ai_unavailable_uses_code_text`, `test_compliance_note_for_baby_food_skin_medical_electrical`, `test_other_suggestions_have_no_score`, `test_hinglish_for_first_two_reports`.

**Task 6.4 Portal simplification and Approve.** Files: `portal/release-admin.mjs`, `portal/worker.mjs` (keep the checks listed in 4.4; product count 5–15; 12-month no-repeat; delete `functional-packet.mjs`, `opportunity-score.mjs`, `supplier-scoped.mjs`, the proof modules, alias/lineage and the 80%-new/Updated rules), `service.py` + `config/service.json` (`validate_issue`: 5–15 products, no checklist or score re-checks, 12-month no-repeat by product type), `release_index.py` (delete alias/lineage apply), `portal_storage.py` (trim routes; call the Windows `cloudflared.exe` explicitly), move `ORIGIN`/`cached_owner_token` out of `portal_delivery_check.py` and delete it; new `scripts/approve_report.py` and an **Approve** button plus the "Report ready to approve" notice in the launcher (`DropshipperDesktop.cs`, rebuilt and reinstalled); late release within 7 days through the portal's late-release option; picks returned to the pool after 7 days unapproved; staging retried hourly on failure; portal tests adapted (`node --test portal/*.test.mjs`).
- Before report 1: read whether the live index is v1 or v2; rehearse one activation of a synthetic issue on the live portal and withdraw it straight after.
- Tests: `test_other_suggestion_with_three_yes_accepted`, `test_validate_issue_accepts_other_suggestion_and_five_products`, `test_approval_bound_to_package_hash_and_schedule`, `test_repeat_within_12_months_refused`, `test_download_rehashes_every_file`, `test_unapproved_draft_released_late_and_picks_returned_after_7_days`, `test_staging_failure_retries_hourly_and_local_files_open`.
- Deploy: `npx wrangler deploy` from `portal/` with the existing Cloudflare sign-in (approved: "deploy the private website update when the first report set is ready"); if that sign-in has expired, the owner signs in once (the window says how).

**Task 6.5 First two reports (load).** Run the whole suites; fast-forward `main`; restart; let the first two reports assemble on schedule (English + Roman-Hinglish PDFs, one workbook each); inspect every page as rendered; stage the private drafts; the owner gets the "Report ready to approve" notice.
- Done when: both drafts are staged and byte-matched on owner download; every PDF page has been looked at; the tracker records assembly time, credits per report and the 15 products of each.

### Phase 7 — The illustrated report look and the product contract (about 1½ days + the owner's choice)

**Task 7.1 Two report mock-ups.** Following the project's design rules (design intent + implementation specs with exact type sizes, colours, spacing, states; desktop and phone width) and the R14 engine, make two illustrated, marketing-grade versions of the product page and the cover, using real products from the first reports; render and inspect both; recommend one.
- Owner choice: at the regroup after phase 6 (or straight away if the owner has said "go with your pick").

**Task 7.2 Build the chosen look.** Adapt the R14 view modules and theme; re-render the latest report; inspect every page at both widths.

**Task 7.3 Rewrite the product contract.** Rewrite `docs/product/india_dropshipping_product_opportunity_radar_product_brief.md` to match section 3 (lean: categories, checklist, removals, points, report make-up, sources, suppliers, wording).

### Phase 8 — Polish, documents and tuning (about 1 day, plus the tuning after two weeks)

**Task 8.1 Status page and daily summary polish; trace product view.** Files: `flow/trace.py`, `desktop_control.py`.
**Task 8.2 Documents.** Write a new plain `docs/project/how-it-works.md`; update the operator guide, the launcher guide, the Codex note (short), the quick-delivery note, the Apify note (fallback only, one short file), and a lean implementation note; update the brief's code map and the index.
**Task 8.3 Apify fallback for Facebook.** Files: `flow/reader_adapters.py` (Facebook through `providers/apify.py` with the `curious_coder~facebook-ads-library-scraper` actor, only after 6 hours of refusals and only within the month's free allowance, using the kept money ledger), `docs/operations/providers/apify.md` (one short note replacing the two old ones). Test: `test_apify_fallback_only_after_six_hours_and_within_allowance`.

**Task 8.4 Points tuning (after about six reports).** Compare the top of the ranking with what reached reports; propose one adjustment with examples; apply after the owner agrees (silence = yes).

### Effort and time (estimates; agent working time)

| Phase | What | Estimate |
|---|---|---|
| 0 | Merge the redesign, build branch, export the 12 headings, test fixtures | 1 day |
| 1 | Database, settings, words, waiting list | 1½ days |
| 2 | Store intake, free filter, free score | 1½ days |
| 3 | AI client, meter and budget, naming | 2½ days |
| 4 | Browser additions, Dropdash full read + automatic sign-in, readers, cross-checks, alerts | 4½ days |
| 5 | Runner, idea intake, trace, backups, delete the old engine, fresh start, **research restarts** | 3 days + 1 calendar day of measuring |
| 6 | Calendar, picker, finalising, report text, portal + Approve, **first two reports** | 3½ days + the wait for a report slot |
| 7 | Mock-ups, chosen look, product contract | 1½ days + owner's choice |
| 8 | Polish, documents, Apify fallback (tuning after two weeks) | 1 day |
| **Total** | | **about 20 working days** (range 17–25) |

Research restarts at the end of phase 5 (about day 12–13); the first two reports come at the end of phase 6 (about day 15–17). If the restart date matters more than completeness, the Apify fallback and the IndiaMART half of the new-listings rotation can wait for phase 8 without changing anything else.

---

## 6. Testing and proof

- **Test first, every task** (`unittest`, the project's existing runner): `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest <module> -v`. Portal: `node --test portal/*.test.mjs` from the repository root.
- **Coverage ≥ 90%** on every new or changed module, no exclusions (`coverage run -m unittest` then `coverage report --include='src/product_radar/flow/*,…'`).
- **Fixtures, not live sites, in tests:** trimmed saved real pages (copied from the live runtime folders in Task 0.3, before the fresh start archives them) become fixtures for the reader adapters; the 34 product types of 16 October become the scoring and assembly fixture (expected: the water bottle ranks above the duster; the baby lotion is ranked, not blocked; nothing is held for missing facts).
- **Whole local suites** (Python and portal) before each load into `main` (phases 0, 5, 6, 7, 8). Already approved under Plan R10 approval A1.
- **Live proof at phase 5 (first 24 hours after restart), measured, not guessed:** products named per AI call; the AI's answer quality on 20 spot-checked items; minutes per product through stage A; calls, every token kind and credit-equivalent cost per report; allowance points moved; removal rate; the waiting list by reason; Dropdash session length; the first two reports' assembly time.
- **Visual proof at phase 7:** every PDF page and both workbooks inspected as rendered (desktop and phone width for the HTML), per the project's design rules.

---

## 7. Documents during the build

| Document | Updated in |
|---|---|
| `docs/project/pj-tracker.md` | After every phase (what is built, live numbers) |
| `docs/operations/implementation-r19.md` (new, lean: what is built, settings files, wait and reason codes, live measurements) | Started in phase 1, updated every phase |
| `docs/project/pj-brief.md` (code map) and `docs/README.md` | Phase 5 (old engine gone) and phase 8 |
| `docs/operations/providers/codex_luna_connection.md` (short, current) | Phase 3 |
| `docs/operations/providers/apify_*.md` → one short `apify.md` (Facebook fallback only) | Phase 4 |
| `docs/operations/local_execution_and_provider_setup.md`, `docs/operations/windows_desktop_launcher.md` | Phases 4–5 (sign-ins, alerts, Approve) and 8 |
| `docs/operations/providers/quickcommerce_connection.md` | Phase 6 |
| `docs/product/india_dropshipping_product_opportunity_radar_product_brief.md` | Phase 7 (rewritten to match section 3) |
| `docs/project/how-it-works.md` (new, plain words) | Phase 8 |

---

## 8. Approvals

**Already approved (still applies):** commits and pushes; focused tests and the whole local suites; clean stop/start of research; Windows app rebuild/reinstall; ordinary research; reading DeoDap, BaapStore, Dropdash, IndiaMART and Google Trends; spending the existing quick-delivery credits and the free Apify allowance; deploying the private website update when the first report set is ready; the report redesign.

**Approved by the owner's 17 October answers:** the whole design in section 3; the program signing in to Dropdash automatically (3.9); research continuing on paid Codex credits after the plan limit, within the soft per-report budget (3.8); buying further quick-delivery credit packs — the owner buys them, the program only says what to buy (3.10); a fresh start of research records keeping the store catalogues and the product list; simplifying the private website's checks (phase 6).

**Asked for with this plan (one approval covers the whole programme, phases 0–8):** build and load Plan R19 as written, including deleting the old engine's code and tests, restarting research at phase 5, and deploying the private website change in phase 6.

**Still separate (not covered):** client publication or client access changes; supplier contact; any purchase made by the agent or the program; changes to the owner's Codex or ChatGPT account settings.

---

## 9. Risks

| Risk | What we do about it |
|---|---|
| Chrome's saved-password autofill does not appear in the program's automated Chrome | The second route (the owner's `.env`, read only by the program) signs in; tested on a fake page first and live in phase 4 |
| Claude Code's safety rule stops the agent from building or testing the sign-in step | The agent never handles the password (autofill click; values read only inside the program); if a step is still stopped, the rest of the plan goes on and the step is listed for the owner with the exact message |
| Dropdash notices automation and signs the session out often | Gentle pacing (30 s between pages, 300 a day), keep-alive visits, automatic sign-in limits; its saved catalogue keeps serving look-ups |
| Grouped AI answers are less careful than one-by-one | Code checks every answer; 20 spot checks in phase 3 and on day 1; group size is a setting (20, lowered if answers suffer) |
| Points put odd products at the top | The two-week tuning (3.5); the owner reviews the first reports before approving |
| Not enough distinct products for two years (3,100–4,700 needed) | Probably enough (measured in week 1): about 8,000 in-category store items today (DeoDap ≈4,500, BaapStore ≈930, Dropdash ≈2,500–3,000 on a rough title count), about 350 new in-category DeoDap items a month, plus Facebook searches and the TradeIndia/IndiaMART rotation. Many items are versions of one product, so week 1 measures the real number of distinct types; the trace shows the runway; month-9 review |
| OpenAI changes limits or prices again (it did on 16 October) | Every call records allowance and credit-equivalent cost; the rate card is a settings file; the budget guard and wallet alert |
| A Codex update renames settings (0.160 → 0.163 in 10 days) | Start-up check of `codex features list`; fall back to the plain command and tell the owner |
| The computer's screen locks or the computer sleeps while the owner is away | Owner keeps it on and unlocked (section 1); phase 4 tests headless signed-in reads; parked checks never stop ranking (24-hour rule) |
| Research runs around the clock on the owner's main ChatGPT/Codex account, which is under OpenAI's fair-use rules; an account action would also hit the owner's own work | Few, grouped calls; no parallel AI calls; daily AI lines visible to the owner |
| Trends allows 200 searches a day and Amazon/Facebook may refuse at volume | About 150–200 products a day can finish stage A; refusals park and rest 6 hours; scores use what is known |
| The new database grows large over two years | SQLite handles millions of rows; old raw pages are pruned after 30 days (facts stay); daily backup copies the database with SQLite's own backup command |

---

## 10. Faults found on 16 October that the new design must not repeat

| Fault | How R19 prevents it |
|---|---|
| "Not looked at yet" treated as "nothing found" (baby lotion, storage container, 26 "zero demand" products) | Unknown = 0 points, never a block; the trace shows "not checked yet" |
| Items silently marked "done" after an AI pause (25 listings, 4 products) | Nothing is done without a result; failures go to the waiting list with a reason |
| Facebook and Trends reviewed last (alphabetical order of internal site names) | Fixed stage order; confirmation covers all places of a product in one call |
| One long Facebook phrase and no shorter spare (spice rack) | Short phrase first, longer phrase only if results are mostly unrelated |
| Trends charts "using up" product-list headings | Only a Facebook search uses a heading |
| "Baby Lotion" and "baby lotion" searched twice | Case-insensitive search key; no repeat within 30 days |
| Supplier answers rejected over a quoting format; store leads never shown | No AI supplier reading; the store listing is the supplier shown |
| Record-keeping ate 43 of 47 minutes in a round | One indexed database instead of rebuilding history files each round |
| One open sign-in window stopped every signed-in site for 5.5 hours | Separate profiles; reminder after 10 minutes |
| A run counted saved look-ups as website visits and stopped itself | Store look-ups are local queries, never counted as visits |

---

## 11. Choices the agent made in this plan (and why)

1. **One SQLite database replaces the event store.** The event store is the root of the slowness and of the silent drops; SQLite is part of Python, survives power cuts with the settings above, and makes "numbers that add up" a simple count.
2. **A new `flow/` package; the old engine is deleted, not patched.** All three code surveys found nothing in the old loop that can carry product-by-product work.
3. **Every product gets the same stage-A checks** (no stopping after 2 positives). The checks are cheap (a few minutes per product), and the same checks for everyone keep the points comparable; "seen in N places" still shows the owner's "2 positives".
4. **Up to 3 products in flight**, so a 90-second Trends gap never idles the other sites.
5. **Dropdash stays in today's browser profile** (with Google), so its saved password and session carry over and nobody has to sign in again; IndiaMART moves to its own profile.
6. **Dropdash is read through the products its own app sends to the page** while the category is scrolled (plus the visible cards), because that is complete and carries the exact order count; the card's "units sold" range overstates 5–10 times.
7. **"On a store list" includes DeoDap's "High Performing" tag and 1,000+ Dropdash orders.** Both are the stores' own sales signals; checked in the two-week tuning.
8. **Apify stays only as the Facebook fallback** (free monthly allowance; only after 6 hours of refusals; built in phase 8), because the owner asked for fallbacks everywhere; the paid deeper-Meta read is deleted.
9. **Research's AI cost is counted with OpenAI's published rate card**, not the wallet's change, because the owner's other projects draw from the same wallet at the same time.
10. **Budget behaviour:** amber at 100% of the per-report target and work continues; only past 300% (a program fault) does the AI keep to report work until the next report; wallet notice below 3,000 credits. "First couple 1,000, then 750, then 500" is read as reports 1–2, 3–4, then 5 onwards.
11. **Reports:** each category at least 1 and at most 4 products when available; lower-third picks ranked in the top 67–80% of the main tier; when rules collide, relax the category minimum, then the band, never near-duplicates or the 12-month rule; a short pool still gives a report of 5–14 products with a reason line; an unapproved draft waits up to 7 days.
12. **Report text:** one AI call per report and language writes the product texts from saved facts; the "why it's a good pick" line is written by code; code text is used if the AI is unavailable.
13. **Approve lives in the desktop window** (and a script), because the private website's pages cannot safely carry a button.
14. **Only the `redesign` branch is merged**; the `speed` (R16) and `evidence`/R17 work is dropped because it speeds up or tightens the engine being removed.
15. **TradeIndia/IndiaMART rotation:** the 502 sub-area words first, then every heading's phrase, about 4 weeks per sweep, repeating.
16. **Dropdash's second sign-in route reads the owner's `.env`** (the owner already put the details there); the Windows Credential Manager route was dropped because nobody would set it up and it is security-only work.
17. **The 12 AI-added headings are exported in phase 0**, while the old event store that holds them still exists.

## 12. Review record

A separate checker (a fresh helper on the same model at extra-high effort, which had not written the plan) reviewed it on 17 October against the owner's answers, the three code surveys, the code on `main` and `redesign`, and the Dropdash capture. It found 3 critical, 24 important and 21 minor points; all were accepted and fixed in this version. The main ones:

- **Critical:** (1) a locked or sleeping computer stops the signed-in checks — now in section 1 (owner keeps it on and unlocked), the never-block table, the 24-hour "rankable" rule and a phase-4 headless experiment; (2) report delivery could get stuck — now: unapproved drafts wait up to 7 days and go out late, local files open if the private website fails, PDFs follow if printing waits, 5–14 products still go out; (3) the time estimate was too optimistic and its own milestones did not add up — re-based to about 20 working days (17–25), research restart about day 12–13, first reports about day 15–17.
- **Important (selection):** the 12 AI headings are exported in phase 0 (the old store is deleted in phase 5); `service.validate_issue` added to the portal task; AI queue priorities so confirmations are never starved; credit drawing at 100% is checked live instead of promised; cost wording corrected (tens of credits per report; Sol about 20× Luna); credit formula no longer double-counts cached and reasoning tokens; the shared Google/Dropdash profile is stated as a deviation with an interleaving rule; Trends' 200-a-day cap is stated; the pair check and the product-list top-up have tasks; the Dropdash capture scripts are kept in the repository; an explicit rule for old-engine tests between phases 1 and 5; the import clean-up listed; the waiting list moved to phase 1; category rule settled (at least 1, at most 4) with a relax order, the sub-area rule dropped; one pool for the percentile; a "low" checklist tier; the Credential Manager route dropped and `.env` keys named (no agent ever opens the file); Dropdash retries without the owner; SQLite locking rules; Codex update safety; Dropdash catalogue conversion and newness; plain-language gaps in section 1; live approval rehearsal and deploy route.
- **Checked and found correct:** every reused function named in the plan exists; `native_public_browser`'s 15–45 s limit; the eight R14 modules on `redesign` and a docs-only merge conflict; the capture's 5,189 products in 21 complete entries with an exact order count on every row; the arithmetic (93 points maximum, about 17 credits for 16 October, about 55 reports per ₹500 pack); consistent budget list, retry ladder, thinking levels and AI deadline; no 50% pause and no security-only work.
