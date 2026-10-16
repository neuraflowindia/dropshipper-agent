# How the program finds products today, why it is slow, and a simpler way

Written 16 October 2026, about 20:00 UTC (1:30 am India time on 17 October), at the owner's request to stop and regroup. Plain language. Numbers come from the live program's own records (the owner meter and the run logs), not guesses, unless a line says "estimate".

**In one line:** the program is a careful machine that wants *proof of demand* for every product before it will put it in a report. In 13 hours it found 34 product types but only **1** passed every gate. It will never reach 10–15 products every other day like this. The cause is how strict the gates are and how the work is queued, not one bug. A much lighter "quick pick" process (section 6) could plausibly produce 10–15 prospects in 1–3 hours (estimate). That needs the owner's yes before it is built.

---

## 1. The whole universe of what we are building

| Part | What it is | State |
|---|---|---|
| A. Research engine | Runs on the owner's computer, looks at websites, uses AI to judge products | Built and running all day; produces far too few finished products |
| B. Sources | Meta Ad Library, DeoDap, BaapStore, Dropdash, Google Trends, IndiaMART, TradeIndia, Google Maps, Amazon, Flipkart, Blinkit/Zepto/Instamart | All connected. Dropdash logged the research account out at 18:37 UTC on 16 Oct (owner must sign in again) |
| C. Judging | 10-point checklist, Amazon/Flipkart crowding check, 100-point opportunity score, labels | Built |
| D. Suppliers and photos | 1–3 supplier leads and a real photo per product | Built |
| E. Report making | English PDF, Hinglish PDF, Excel workbook | Built. New magazine-style design built and checked; it sits in the build copy and is not loaded into the live program yet |
| F. Private website | Client signs in with Google and downloads reports | Built and tested; goes online when the first reports exist |
| G. Running by itself | Start/Stop desktop app, auto-restart every 15 minutes, daily backup, money/allowance watch, power-cut recovery | Built and working |
| H. Actual reports delivered | The first two report sets (10+ products each, both languages) | **0 of 2. Not started, because there are not enough finished products** |

**How much is complete:** the *machinery* (A–G) is about 90% built. Over 6,100 automatic checks pass. The *output* the client pays for (H) is at 0%. Measured by what the business actually needs (reports), we are close to the start, not the end. That is the honest picture.

## 2. Exactly what the program does with one product today, step by step

Think of a factory line with many inspection desks. A product only reaches the report if it passes every desk.

1. **Pick a search heading.** The program has a "map" of 1,706 headings (e.g. "baby bottle drying rack"). Each round it takes a few headings it has never searched.
2. **Look on Meta (Facebook/Instagram ads).** It searches the Ad Library for that heading and saves the ads it sees. It also reads the new-arrival and bestseller lists of the three dropshipping platforms, and new supplier listings, for more ideas.
3. **AI names the product.** The AI reads the messy listings and decides on a clear "narrow product type" (e.g. "Adjustable doorway pull-up bar"). It checks this is not a duplicate and not a big brand. *(Desk 1: "accepted": 34 so far.)*
4. **AI fills the 10-point checklist** (small? easy to ship? solves a problem? …). It needs 5 or more "Meets". *(Desk 2: "assessed/passing": 27 of 30 pass. Easy.)*
5. **Supplier search.** It looks on IndiaMART, TradeIndia, Google Maps, company websites and the platforms for 1–3 supplier leads, and opens company pages to read their terms. *(11 types still wait for this.)*
6. **Photo matching.** It saves real photos and the AI checks they show the same product.
7. **Market checks, one site at a time.**
   - Meta again, for that exact product: how many advertisers, since when.
   - Google Trends: are searches rising?
   - Amazon and Flipkart: how crowded is it (Low/Medium/High)? *(23 types are waiting their turn for this check right now.)*
   - The platforms again: is it on their lists?
   - Quick-delivery apps (costs a credit), only for products that already look good.
8. **Score out of 100.** Want 20, Early 30, Room 25, Product 25.
9. **The big gate: "want" must be at least 5 points.** A product needs real signs of demand: several Meta advertisers, Trends searches, bestseller lists or quick-delivery stock. Without them it is labelled **"Too quiet"** and is *not allowed* into a report. *(6 types were checked and rejected here. Only 4 of 34 types have even one relevant Meta ad, and only 1 has usable Trends data so far.)*
10. **Report-ready ("eligible").** Every desk passed. *(**1 type** after 13 hours.)*
11. **Make the report** once 10 or more are ready: the AI writes short texts in both languages, and the program builds the PDFs and Excel file. The owner approves, and it goes on the private website.

**The limits on each round:** at most 16 website reads and 10 AI uses per round. The signed-in sites (Trends, IndiaMART, Dropdash) share about 5 minutes a round. Every site waits politely after a refusal (5 minutes, then 1 hour, 6 hours, a day).

## 3. Why it is so slow: the plain reasons

1. **It demands proof, and proof is hard to find for new products.** The "want ≥ 5" gate (step 9) throws out almost everything. New or niche products rarely have five Meta advertisers or measurable Google searches. That is exactly the kind of product a dropshipper wants to *test*, and the gate rejects it. Measured: about 7% of scored types (3 of 43 across both research runs) ever reached want ≥ 5.
2. **Every product must pass every desk before anything counts.** One product needs about **9–10 website reads and 7 AI uses** before it can be judged ready. With 16 reads and 10 AI uses per round, that is roughly **one finished product's worth of work per round**. Then the work is spread thinly across 30+ products at once, so all of them sit half-done (23 are waiting at the Amazon/Flipkart desk).
3. **Rounds get slower as the pile grows.** Rounds took 4–15 minutes in the morning and 35–48 minutes by the evening. In the earlier run they reached 100–147 minutes after a day. 66–88% of the time is the program re-reading and re-sorting its own records, not looking at websites. (A speed fix was being built; see section 5.)
4. **The signed-in sites kept stalling.** Trends only started working at 17:56 UTC on 16 October. IndiaMART was signed in at 17:45 UTC. Dropdash logged out at 18:37 UTC. Before that, demand facts simply could not be collected.
5. **The AI allowance is used up.** The program uses the owner's ChatGPT Plus (Codex) weekly allowance. It hit **100% at 18:10 UTC on 16 October** and resets on **22 October at 06:13 UTC**. Research itself used under 2% of it; the rest was the owner's other Codex projects. Until the reset, every AI step (naming products, checklists, report writing) waits.
6. **Engineering went into reliability, not output.** Over the last days most effort went into making the machine survive anything: power cuts, sign-in edge cases, a year-long product map, backups, spending watches, a magazine-quality report design. Each of these is sound. Together they are more than this business needs before the first report exists. The owner's criticism is fair.

## 4. Where the time goes (measured, 16 October, 7.3 running hours)

| Step | Minutes | Share |
|---|---:|---:|
| Fetching web pages (acquire) | 146 | 35% |
| Deciding if listings match a product (match) | 74 | 18% |
| Discovery (Meta searches, lists) | 53 | 13% |
| Photos | 33 | 8% |
| Checklist (fit) | 27 | 6% |
| Platform lists | 23 | 5% |
| Supplier search | 21 | 5% |
| Company profiles | 16 | 4% |
| Crowding, staging, reports | 20 | 5% |

Across all of it: 66% is the program's own processing, 18% is waiting for websites and 16% is the AI thinking.

## 5. What was underway when the owner asked to stop

| Work | State at the stop |
|---|---|
| R14: new report design + review fixes | Finished and tested (6,104 of 6,107 checks pass; 1 is a known clock-dependent test). Not loaded live |
| R15: signed-in site fixes (Dropdash weekly categories, no first name, lost-read sweep) | Live since 16 October |
| R16: rounds slowing down | The helper was told to wrap up; see the handoff for what it finished |
| R17: market checks first for every passing product | Built. A review found 5 important problems; the fix helper was told to wrap up; **do not load as it is** |

All of this is on separate working copies. The live program still runs the R15 version, unchanged, and is not half-updated.

## 6. Recommendation: a "quick pick" process

The owner's brief is clear: the job is to give the client **promising prospects worth testing**, not proven winners. The process should match that.

**Proposed quick pick (needs owner yes, then about 1–2 days of build and test; estimate):**

1. **Start from ready-made product lists, not from scratch.** The three dropshipping platforms already list thousands of products with prices, photos and a ready supplier (DeoDap and BaapStore catalogues, Dropdash lists). Meta ads found by the heading search are a second pool.
2. **AI judges products in batches.** One AI use names, de-duplicates and fills the checklist for, say, 10 products at a time, instead of 7 AI uses per product.
3. **One quick market glance per product:** a single Amazon search (crowding and price), plus whatever demand signals are already saved (ads, Trends, list badges). Signals are **shown when found, never required**. No "Too quiet" rejection. A product with no demand signal can still go in, worded as "early, unproven".
4. **Supplier = the platform listing** the product came from, plus one IndiaMART search if time allows. No company-page deep reading.
5. **One photo** from the listing itself.
6. **Pick 10–15** by checklist score and low crowding, keep the 80%-new rule, and build the report with the existing (redesigned) PDF and Excel.

**Rough time per report with quick pick (estimate, not measured):** gathering and judging about 30 candidates to pick 15 takes about 30 batch AI uses and about 30–45 web reads. That is roughly **1–3 hours per report**, run three times a week, or one longer run on Monday for all three. Building the report takes about 10–20 minutes more. The ±10% accuracy the owner asked for can only come from timing about 3 real quick-pick runs. Those numbers will exist after the first week of quick pick.

**Cost per report (estimate):** ₹0 extra for AI (owner's existing subscription, *if* it has room left that week); ₹0 for Apify (free allowance); quick-delivery checks become optional (₹0–₹500 a month). So about ₹0–₹40 cash per report, well under the ₹750 a month target.

**What we would keep:** the checklist, brand and duplicate rules, compliance notes, 80% new, both languages for the first two sets, the private website and owner approval of every release.

**What we would park (switched off, not deleted):** the "want ≥ 5" gate, the full multi-site market check before a product counts, deep supplier company reading, quick-delivery checks as a requirement, and the R17 "market checks first" work.

## 7. How long our resources last ("runway"), as of 16 October

| Resource | Left | Lasts |
|---|---|---|
| AI weekly allowance (owner's Codex) | 0% left until 22 Oct 06:13 UTC | Research AI is stopped for about 5.5 days. Research needs only a few % a week. The owner's other Codex work decides this |
| Apify free money (deeper Meta look) | US$3.07 earmarked this month | Renews monthly; quick pick needs almost none |
| Quick-delivery credits | 42 free credits, expire 2 Nov | Optional under quick pick |
| Product map | 1,674 of 1,706 headings never searched | About 7 weeks at today's pace, and it tops itself up |
| Disk for backups | 77 GB free on D: | About 3.5–6 months before backups stop at 10 GB (estimate) |
| Money | ₹0 cash spent since the 16 October restart | Target ₹750 a month at most |

## 8. Questions this raises (the full numbered list, with answer lines, is in the private handoff)

1. Approve the quick pick process in section 6? Recommendation: yes.
2. Under quick pick, should a product with no demand signal at all be allowed into a report (worded "early, unproven"), or must it have at least one signal of any kind? Recommendation: allow it, but rank products with any signal first.
3. The AI allowance: keep some of the weekly Codex allowance free for research (for example, stop other Codex work at about 85%), or wait each time it runs out? Paid extra credits stay off unless you say so. Recommendation: keep about 15% free for research.
4. Load the finished report redesign (R14) with quick pick? Recommendation: yes.
5. Park R17 and the slow-round fix (R16) unless quick pick needs them? Recommendation: park R17; keep only the part of R16 that is small and finished.
