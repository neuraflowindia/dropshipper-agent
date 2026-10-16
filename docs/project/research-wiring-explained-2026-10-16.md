# How the research program really works, step by step (wiring audit of 16 October 2026)

**What this is.** A full walk through the live program, from the moment it starts to the moment a report could go out. It covers every list it starts from, every website it reads, every question the AI is asked, every rule that lets a product through or holds it back, and how many items made it past each point on 16 October. The owner asked for this on 16 October 2026 to understand why finding products takes so long.

**How it was made.** Five separate read-only checks of the code and the live records, each covering one part of the program, plus a sixth that built a tracing tool (section 19). Nobody changed the code or the live data to make this. The numbers cover the research day from the fresh start at **06:58:54 UTC (12:28 pm India time) to 20:50:41 UTC (2:20 am India time) on 16 October**: about 13 hours 50 minutes. Research was stopped at the end of that window. All times are UTC; add 5 hours 30 minutes for India time.

**Live code described:** the version running on 16 October. Work built on 16 October but not yet loaded (the new report design, a speed fix and a "market checks first" change) is mentioned only where it matters. (Inside the project these versions have short labels such as R15 or R16; you can ignore them.)

**Product names.** Examples use generic product-type names only. No supplier, seller or advertiser names appear.

---

## Contents

1. Words used in this document
2. The whole journey on one page, with the day's numbers
3. What starts the program, and what one "round" is
4. The starting lists
5. Finding ideas, source by source
6. From a raw ad or listing to a product type
7. The 10-question checklist
8. Market checks, website by website
9. The AI "is this the same product?" step
10. How crowded is it? (Amazon and Flipkart)
11. The score, the labels and the gates
12. Suppliers
13. Photos
14. Making and releasing a report
15. Where the time went
16. Where the AI went (tokens and time, step by step)
17. Each website's day
18. Dropdash: what happened and why
19. What we can see today, what we can't, and the new tracing tool
20. Real product journeys
21. "What happens if…": the common situations
22. Faults and waste found, ranked
23. Why it is slow, in one list

---

## 1. Words used in this document

| Word | Plain meaning | Example |
|---|---|---|
| **Category** | One of the client's four areas | Home and Kitchen |
| **Sub-area** | A group of related products inside a category | "Sink and dish organisation" |
| **Heading** | One kind of product the program plans to look for, with a ready search phrase | "Anti-fatigue standing mats", searched as "anti fatigue mat" |
| **Raw item** | One thing seen on a website: an ad, a platform listing or a supplier listing | One Facebook ad for a kitchen mat |
| **Product** | What the AI says a raw item is selling | "Memory foam kitchen mat" |
| **Candidate** | A new product idea that did not match anything already known | — |
| **Product type** | A clearly defined kind of product, independent of any one seller. This is what reports recommend | "Memory Foam Kitchen Standing Mat" |
| **Question** | One planned check of one website for one product type | "Search Amazon for the spice rack" |
| **Counted read** | One website check charged to a round's allowance of 16 (some are answered from a saved copy) | 307 on 16 October |
| **Real visit** | One actual page load or download from a website, including the platform catalogue downloads that are not charged to the 16 | About 410 on 16 October |
| **Round** | One pass of the program over all its work, with a fixed allowance of reads and AI uses | — |
| **AI use** | One request to the AI. The AI is the owner's ChatGPT subscription, used through OpenAI's "Codex" tool. One use can carry up to 10 items | — |
| **Token** | The AI's unit of reading and writing; roughly three-quarters of a word. The AI also "thinks" before answering, and that thinking is not recorded today, so real use is somewhat higher than the token numbers here | A 1,000-word page ≈ 1,300 tokens |
| **Cached tokens** | Input the AI service recognised as repeated and charged less for | 4.3% on 16 October |
| **Apify** | An outside service that can read Facebook's ad library more deeply; paid from a free US$5 monthly allowance | Not used on 16 October |
| **Gate** | A rule that stops a product type going further until something is true | "Needs at least 5 checklist Meets" |
| **Wait** | A planned pause: the work is kept and tried later | "This round's 16 reads are used up" |
| **"Narrow" / "example offer"** | The AI's verdict on a matching listing: "narrow" means it shows the product type itself; "example offer" means it shows only that one seller's offer. Only "narrow" ones count today | — |
| **Bookkeeping** | The program's own work of reading and updating its saved records (no website, no AI) | — |

**The owner's three levels, in the program's words.** "Broad product type" = a **heading** in the product map. "Narrow product type" = a **product type**. "Product" = a real **listing** (raw item) shown as an example.

**A product versus a product type.** The program never recommends one seller's listing. It recommends a *kind* of product (a "type"). Real listings are kept only as examples under the type. For example, three different sellers' extendable dusters are three *raw items*, the AI reads them as three *products*, and they all belong to one *product type*: "Extendable Microfiber Duster".

---

## 2. The whole journey on one page, with the day's numbers

**Does it pick one product or many?** Many. Each round it collects a small pile of raw items (up to about 60 ads from two heading searches, 5 platform list items and 5 supplier listings). The AI reads about 10 of them at a time. It never follows one product from start to finish; instead, every product type sits in a queue at each desk and moves on when that desk gets to it.

Think of a factory line with many desks. The desks work side by side (market checks and supplier checks for the same type happen at the same time), but every product type must have passed every desk before it can go into a report.

```
STARTING LISTS
  Product map: 1,706 headings (32 used, 1,674 never used)
  Platform catalogues: DeoDap 13,527 items, BaapStore 6,053 items, Dropdash 0 (never read)
  15 fixed supplier-site search words; Google Trends "rising" searches
        │
        ▼
FINDING IDEAS (discovery)                      347 raw items collected
  Meta ad searches: 25 searches → 295 ads
  Platform best-seller lists: 30 items (daily cap hit at 13:32; 839 more waiting)
  TradeIndia listings: 22
        │
        ▼
AI READS EACH RAW ITEM (step A)                128 of 347 read (219 never read)
  66 products, 3 mixed, 45 unreadable or removed ads, 14 services → 78 products
  Removed: 14 outside the four categories, 5 big brands
        │
        ▼
NEW OR ALREADY KNOWN? (step B)                 34 new candidates, 6 joined known ones, 3 unclear
        │
        ▼
AI DEFINES THE PRODUCT TYPE                    30 accepted, 1 unclear, 3 being retried
        │                                       (from here on: 34 = 30 defined + 4 not yet defined,
        ▼                                        which are checked the older way)
10-QUESTION CHECKLIST                          30 of 34 pass (5+ "Meets"), 2 rejected, 2 on hold
        │
        ▼
MARKET CHECKS PER TYPE (~16 websites each)     Meta, Trends, Amazon, Flipkart, Amazon best-sellers,
  + AI "same product?" review                   DeoDap, BaapStore, Dropdash, Blinkit, Zepto, Instamart
                                                1,087 listings judged; 1,993 saved items still waiting
        │
        ▼
HOW CROWDED (Amazon + Flipkart)                9 decided (7 High, 1 Medium, 1 Low); 25 not yet
        │
        ▼
SCORE OUT OF 100 + DEMAND GATE (want ≥ 5)      26 of 34 types have 0 "want" points
        │
        ▼
SUPPLIER SEARCH (Maps, IndiaMART, TradeIndia)  25 of 34 have had at least one supplier read
        │
        ▼
REPORT-READY ("eligible")                      1 type
        │
        ▼
REPORTS                                        0 (each needs at least 10 types)
```

**Why only 1 of 34 got through**, per type (a type can be held for several reasons at once):

| Held by | Types | What it means |
|---|---|---|
| Waiting for the Amazon/Flipkart crowding answer | 23 | 10 are waiting for the AI to review their saved listings, 6 have not been read yet, 4 are waiting for the crowding desk to run again, 3 a mix |
| No supplier search yet | 9 | All 9 are also waiting for crowding |
| "Too quiet" (fewer than 5 demand points) | 6 | 3 of these had Facebook ads and a Trends chart saved but never reviewed |
| Checklist: under 5 Meets | 4 | 2 rejected, 2 on hold |
| **Ready** | **1** | The extendable microfiber duster, score 37 of 100 |

---

## 3. What starts the program, and what one "round" is

### 3.1 Starting

1. The owner clicks **Start** in the NeuraFlow Dropshipper window. A Windows task also checks every 15 minutes, and at sign-in, whether research should be running.
2. If research is switched on and not already running, the program starts. On 16 October it started 7 times: 6 were owner Stop/Start clicks and 1 followed a power cut at about 07:30.
3. Each start first re-reads the whole research history (9,081 saved records by the evening). This took under a second in the morning and 1–2 minutes by the evening.

### 3.2 What one round is

The program works in rounds. A round is one pass over all its work. Each round gets a fixed allowance:

| Allowance per round | Amount | Notes |
|---|---|---|
| Website reads | 16 | Shared by all product types and all websites |
| AI uses | 10 | Of these, 2 are held back for finding new ideas while reports are short of products, 2 for definition and market reviews, and 1 for report writing when a report is due |
| Signed-in website time (Trends, IndiaMART, Dropdash) | 300 seconds | Before each signed-in read the program sets aside the worst case (123 seconds). With typical 15–40 second reads that allows about 5–8 signed-in reads per round, but only 2 if pages are very slow. Trends reads must be at least 90 seconds apart |
| IndiaMART reads | 4 | |
| Dropshipping platform time | 150 seconds | Separate from the 16 reads; Dropdash gets at most 1 page per round |
| AI items per round, by job | Checklist 10, "same product?" reviews 5, supplier page reading 5, search wording 20, photos 10 | |

### 3.3 What happens inside a round, in order

The round has nine desks ("stages"). The starting desk moves along every two rounds, so the order changes:

1. **Profiles**: the AI writes search phrases for each product type (for Meta, Amazon/Flipkart, Trends, suppliers, quick-delivery apps).
2. **Fit**: the AI answers the 10-question checklist.
3. **Acquire**: the program reads websites for each product type (the 16 reads are spent here).
4. **Match**: the AI defines new types, checks for duplicates, and judges whether saved listings are the same product.
5. **Crowding**: code (no AI) works out Low/Medium/High crowding.
6. **Suppliers**: the AI reads saved supplier pages.
7. **Photos**: downloads product photos (no AI).
8. **Reports**: builds a report when 10 or more types are ready.
9. **Private staging**: saves private report drafts.

Two things are slotted in as well:
- **Finding new ideas (discovery)** runs before the desks on odd rounds and after them on even rounds.
- **Platforms**: refreshes the three dropshipping platforms' catalogues.

**"Nearly ready first."** While the coming reports are short of products, types that already pass the checklist are pushed to the front of the reading, matching, crowding and supplier desks.

**Review groups.** Every discovery result that creates new candidates opens a new "review group". Every desk visits every group in turn. By the evening there were 17 groups, which is one reason rounds got longer.

**When a round ends.** After one pass. The next round starts at once if anything changed. Otherwise the program waits for the next saved retry time or the 15-minute check.

### 3.4 How long rounds took on 16 October

| Period | Typical round length |
|---|---|
| First 10 rounds (morning) | about 8 minutes |
| Middle of the day | 17–67 minutes |
| Last 10 rounds (evening) | about 47 minutes (longest 81) |
| All 30 complete rounds | median about 17 minutes |

The main reason rounds slowed down is the program's own bookkeeping, not the websites (section 15).

---

## 4. The starting lists

**Yes, it starts from lists.** There are two main lists and two small ones.

### 4.1 The product map (1,706 headings)

- **What it is:** a list of headings in 55 sub-areas across the 4 categories. Every heading has a ready Facebook/Instagram search phrase, a marketplace phrase and a Google Trends phrase. 1,694 headings were built on 16 October; the AI added 12 more during the day.
- **Per category (the 1,694 built that morning):** Home and Decor 455, Home and Kitchen 444, Travel and Outdoor 426, Baby Products 369; plus 12 added by the AI.
- **How a heading is chosen:** headings never used come first; then headings with fresh signs of interest (for example, words seen in new platform list items, rising Trends searches or new ads); then headings used longest ago. Headings whose types all turned out "High" crowding go last. The four categories take turns.
- **How many per round:** headings are taken in "waves" of 4, one per category. A wave must fully settle before the next one starts, and at most **2 heading searches run per round**.
- **What "searched" means:** a heading counts as searched the moment it is put into a wave, whatever happens. A heading served only by a Trends chart (which can never produce a product) still counts as searched.
- **How it grows:** each round the program looks at up to 400 saved items for new kinds of product (live: 9,703 looked at; 664 kept for the AI). The AI may add a heading if it is really a different kind of product (live: 8 attempts gave 1 heading; 7 more gave 11). An AI "top-up" of up to 12 headings a call, at most 6 calls a day, runs only when fewer than 300 headings are left unused.
- **Day's numbers:** 8 waves; **32 headings used** (8 per category), of which 7 were used up by a Trends chart only; 1,674 never used.

### 4.2 The dropshipping platform catalogues

- **DeoDap:** the program downloads its whole public catalogue every 7 days and its best-seller, "most bought this month", state best-seller and new-arrival lists every day. Live: **13,527 items**, 7,789 in stock, **2,732 on at least one list** (3,254 list places, because some items sit on several lists). Its own tags also mark 2,864 items "High Performing". About 4,521 in-stock items fit the four categories by the program's keyword rule.
- **BaapStore:** the same, **6,053 items**: 3,516 are kinds the program excludes (mostly clothing), 1,606 fit no category, and **931 fit the four categories**. Only 15 are on its "trending" list.
- **Dropdash:** read through the program's own signed-in Chrome (home page, the "Profitable Products" list, and once a week the first 18 products of each of its 21 categories). **No Dropdash product was saved on 16 October** (section 18).

### 4.3 Small lists

- **15 fixed supplier-site words** (for example "kitchen organiser", "wall hanging", "baby care product", "travel accessory"), searched on TradeIndia and IndiaMART for newly listed products. Each word is re-read after 7 days.
- **Google Trends "rising" searches** saved from any Trends chart read in the last 14 days.

---

## 5. Finding ideas, source by source

### 5.1 Facebook/Instagram ads (Meta Ad Library), by heading

| | |
|---|---|
| **What triggers it** | A heading in the current wave (at most 2 searches per round) |
| **What is sent** | A keyword search of the public Ad Library for India, all ads (active and inactive), sorted by Meta by impressions |
| **How it is read** | One page load in the program's own hidden Chrome (no account, no scrolling, no clicks), 45-second limit |
| **What is kept** | The first 30 ads: ad ID, advertiser page, active or not, start date, ad text, picture |
| **Outcomes** | Ads found → wait for the AI to read them; "no ads" → nothing; page not recognised → nothing; refused → back-off |
| **Day's numbers** | 25 searches: 15 found ads, 3 "no ads", 7 pages not recognised. **295 ads collected**, of which the AI read 91; **about 200 were never read** (156 still in line, 43 hit the AI limit, 2 bad answers; 3 more are not accounted for in the records). The 91 gave 44 products, 12 new candidates and 11 product types |
| **Quality** | Of all 573 Meta ads seen that day, **61% were inactive** (old ads). Of the ad items the AI read, 46% were removed ads, events, clinics or apps. Searches often bring unrelated products (the search "transparent table cover" also gave a bird wall hanging and a storage rack) |

### 5.2 The platforms' best-seller and new lists

| | |
|---|---|
| **What triggers it** | Every round, from the saved DeoDap and BaapStore catalogues (Dropdash had none) |
| **Which items qualify** | In stock, on a list (best-seller or two or more state best-seller lists first, then trending, then new arrivals; **items on no list, or on only one state list, are never picked**: 741 in-scope DeoDap items sit on just one state list), in one of the four categories by keyword, not seen before, not a near-copy of a known type |
| **Limits** | 5 items per round, **30 per day**, and no new picks while 2 earlier picks are still being read by the AI |
| **Day's numbers** | 30 items picked (DeoDap 20, BaapStore 10) by 13:32, when the daily cap was hit. DeoDap: 15 read by the AI → 15 products → **13 new candidates, all 13 became product types** (the best success rate of any source). 5 DeoDap and all 10 BaapStore items were never read, because the AI was unavailable at that moment, and a fault then marked them "done" (section 22). **839 DeoDap list items are waiting** (about 4 weeks at 30 a day) |

### 5.3 TradeIndia and IndiaMART new listings

| | |
|---|---|
| **What triggers it** | One of the 15 fixed words, least recently read first; 1 read per site per round |
| **What is kept** | Up to 5 listings not seen before |
| **Day's numbers** | TradeIndia: 5 successful reads → 22 listings → 9 candidates → 6 product types. Every TradeIndia search first tries a plain page read, which failed every time, so each costs two reads. IndiaMART: 1 read with an unclear result |

### 5.4 Google Trends "rising" searches

Only 3 rising searches were saved all day (Trends only started working at 17:56). One was searched on Meta at 18:27 → 10 ads → never read by the AI (the AI allowance had run out) → dropped.

### 5.5 Advertisers' other ads

This source can never run as built. It needs the advertiser's number from the Meta page, and none of the 573 ads seen showed it.

### 5.6 Summary of where the 34 candidates came from

| Source | Raw items | Read by AI | Products | New candidates | Product types |
|---|---|---|---|---|---|
| Meta heading searches | 295 | 91 | 44 | 12 | 11 |
| DeoDap lists | 20 | 15 | 15 | 13 | 13 |
| BaapStore trending | 10 | 0 | 0 | 0 | 0 |
| TradeIndia listings | 22 | 22 | 19 | 9 | 6 |
| Trends rising → Meta | 10 | 0 | 0 | 0 | 0 |
| Dropdash, advertisers, IndiaMART | 0 | — | — | 0 | 0 |

**Only 14% of the day's counted reads (42 of 307) and 39% of AI uses went to finding and defining new ideas.** The other 86% of counted reads went to checking types already found.

---

## 6. From a raw ad or listing to a product type

Four steps, three of them AI. (The step names below match the AI table in section 16.)

**Step 1 — "read the raw items" (AI).** Up to 10 raw items per AI use. For each one it says whether it shows a product, several products, a service, or nothing readable. For each product it gives a name, category (one of the four, or "outside scope"), what it does, how it works, who it is for, features, and whether it is a well-known brand.
- Day: 16 AI uses read 128 different items (130 including 2 re-reads) → 66 products, 3 mixed listings, 45 unreadable, 14 services → **78 products**.

**Gate: category and brand (code).** "Outside scope" is dropped (14). A well-known brand is dropped (5). "Unclear" brand passes.

**Step 2 — "new or already known?" (code, then AI).** Code first finds known candidates with similar words. If none, it is new without asking the AI (23 of the day's cases). Otherwise the AI compares it with up to 10 (then 20) similar candidates and answers same, different or unclear. A "same" is overruled if one is manual and the other powered, or one is for adults and the other for babies.
- Day: 20 AI uses (one product per use, about 15,700 tokens each); 34 new candidates, 6 joined existing ones, 3 left unclear.

**Step 3 — "define the product type" (AI).** In the match desk, up to 5 candidates per AI use. The AI is given up to 20 examples and asked for "the narrowest useful kind of product, independent of the maker": a report title, main use, how it works, where it is used, what it must fit, and what is and isn't included. There is no "reject" here.
- Day: **30 accepted**, 1 unclear, 3 being retried. Median time from candidate to accepted type: about 20 minutes.

**Afterwards (more AI):**
- **Duplicate check:** the AI compares types in the same category with similar words. Day: 17 AI uses, 34 pairs, **0 duplicates**.
- **Heading check:** is the type the same as its heading? Day: 7 uses (2 "unrelated", 2 "broader").
- **Example check:** do later listings belong to the type? Day: 2 uses.

**Step 4 — the checklist** (section 7).

The 30 defined types by category: Home and Kitchen 13, Home and Decor 10, Baby Products 4, Travel and Outdoor 3. The other 4 of the day's 34 ideas have no settled definition yet (1 unclear, 3 being retried); they still get checked, by an older, simpler path.

**What it costs to find and define one product type (16 October average):** about 3 AI uses and 55,000 input tokens. For comparison, per type: the checklist about 18,000 tokens; the "same product?" reviews about 143,000 for each type that had any; supplier work 120,000–190,000.

---

## 7. The 10-question checklist

### 7.1 The questions

| No. | Question | Day: Meets / Does not meet / Unclear (34 types) |
|---|---|---|
| 1 | Is it small? | 23 / 9 / 2 |
| 2 | Is it easy to ship? | 30 / 2 / 2 |
| 5 | Does it boost confidence? | 13 / **19** / 2 |
| 6 | Is it convenient? | 30 / 3 / 1 |
| 7 | Does it save time? | 25 / 8 / 1 |
| 8 | Does it solve a problem? | 32 / 0 / 2 |
| 9 | Does it save money? | 10 / **20** / 4 |
| 11 | Does it improve everyday life? | 33 / 0 / 1 |
| 12 | Does it look valuable for the price? | 21 / 11 / 2 |
| 13 | Is it aimed mainly at women? | 10 / **24** / 0 |

(Numbers 3, 4 and 10 were retired earlier: high margin, past winners and extremely unique.)

### 7.2 How it is decided

- The AI answers all ten for up to 5 types per AI use.
- **What the AI sees:** only the type's definition, plus features and audience copied from the one listing that created it. It never sees prices, photos, listing text or measurements. A connection meant to pass physical facts (weight, size, material) from matched listings into the checklist delivers nothing, because of a mismatch in the code. On 16 October **all 340 answers were estimates; none was based on an observed fact.**
- The rules tell the AI to give a labelled estimate rather than "Unclear" whenever it knows what the product is.
- Code counts the Meets: **6–10 = Good fit, 5 = Decent fit, under 5 = not eligible.** If enough answers are Unclear to still reach 5, the type is put on hold instead.
- The score gives 10 points for 5 Meets, plus 3 per extra Meet (up to 25).

### 7.3 Day's numbers

| Meets | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|
| Types | 1 | 1 | 2 | 5 | 5 | 7 | 8 | 4 | 1 |

- **30 of 34 pass.** 2 are rejected (a butterfly wing pillow and a bird wall hanging, both 4 Meets). 2 are on hold (a baby-care kit whose contents are unknown, and a vague, not-yet-defined "decor item").
- **58 checklists for 34 types.** Three versions of the checklist wording each re-asked all ten questions; 20 types were fully re-asked, 4 of them three times. The same product moved by up to 4 Meets between wording versions, and 5 types crossed the pass line at least once.
- **What it costs:** 35 AI uses, about 611,000 input tokens, 15 AI minutes.

### 7.4 What it tells us

- It is a **weak filter**: 88% pass, and the one type that reached a report has 7 Meets, the same as types rejected later as "too quiet".
- Three questions (women, money, confidence) do almost all the rejecting. Two (problem, everyday life) are almost always Meets.
- **The compliance note does not work.** The live checklist wording no longer asks for safety or legal notes, so the AI fills the slot with a disclaimer. Not one of 34 types has a real flag, including a baby lotion and a cot mobile, and the live report code does not print the note anyway.

---

## 8. Market checks, website by website

Once a type exists, the program plans **one question per website for every type**: 16 websites, for every type, every round, until each is answered. It uses the AI's search phrases (or, until those exist, the type's short name; when the phrases arrive, the old short-name questions are not withdrawn, so 12–13 types were searched twice).

| Website | What is sent | What is read | What is used | Day's numbers |
|---|---|---|---|---|
| **Meta ads (per type)** | Keyword search in India, all ads | First 30 ads | Number of advertisers and when their ads started | 46 questions, 32 read: 2 types with relevant ads reviewed, 14 types with ads saved but not reviewed, 6 "no ads", 4 page not recognised, 6 not read |
| **Meta deeper look (Apify, paid from the free monthly allowance)** | Only when the free look leaves gaps | 10 ads | Advertiser and start date | **Never used** (its conditions were never met) |
| **Google Trends** (signed in) | 12 months of searches in India | The weekly chart and "rising" searches | Whether searches exist and are rising | 46 questions, 11 read, 8 charts. **0 Trends charts were reviewed**, so 0 Trends points for every current type |
| **Amazon search** | First results page | First 30 listings: ratings, stars, "1K+ bought", best-seller badge | Crowding only (no demand points) | 47 questions, 34 read |
| **Flipkart search** | First results page | First 30 listings | Crowding only | 47 questions, 32 read |
| **Amazon best-seller list** | One department page per category, shared | 30 list items | Crowding (best-seller rule) | 32 checks on 5 real page loads. 320 list rows reviewed → **1** match; it changed no crowding answer |
| **DeoDap / BaapStore** | No website visit: the saved catalogue is searched by title words | Best 5 matching items | On a best-seller or new list? | DeoDap 35 checks, BaapStore 36 |
| **Dropdash** | Same, from its saved catalogue | — | — | **0 checks**: no catalogue saved |
| **Blinkit, Zepto, Instamart** | Search through a paid service, 1 prepaid credit each (the current pack was bought earlier and lasts until 2 November); Mumbai first, then Delhi NCR and Bengaluru only if Mumbai found it | Up to 50 results | In stock in a city? | Only for types that pass the checklist and are known **not** to be High crowding. 10 searches (10 credits) for 3 types → 1 demand point in total |

**How the 16 reads per round are shared.** All of these plus the supplier sites (section 12) share the round's 16 reads. On 16 October, questions were pushed to a later round **212 times** because the round's reads were used up.

**What counts as "found" on each website (the exact rules):**

| Website | Counts when… |
|---|---|
| Meta | The AI judged at least one ad as this product type ("narrow"). **"Positive"** = 3+ different advertisers, or 5+ active ads with at least one running 14+ days |
| Google Trends | The chart is **usable**: at least 8 weekly points covering 300–400 days, no more than 7 days old, and judged by the AI as this type. **"Rising"** = the last 4 weeks average at least 1.15× the 4 before **and** at least 5 points higher, or a related "rising" search for the same product |
| Amazon / Flipkart | The AI judged listings as this type ("narrow"); used only for crowding (section 10) |
| DeoDap / BaapStore / Dropdash | A catalogue item's title covers at least two-thirds of a search phrase's words (and at least 2 words); the best 5 are kept, then the AI judges them. "On a list" = a matched item on a best-seller, trending, high-demand or state best-seller list |
| Blinkit / Zepto / Instamart | A matched listing marked available in that city |

**Freshness.** Score facts count for 7 days. To stay report-ready, a type must be re-read every week.

---

## 9. The AI "is this the same product?" step

Before any website result can count, the AI must judge, listing by listing, whether it shows the same product type.

- **How:** up to 20 saved results per type and website per item, 5 items per AI use, at most **5 items per round**.
- **Answers:** exact product, same family, broader, "example offer", unrelated or unclear. Plus a "scope": does the result establish the *type* ("narrow") or only *this one offer*?
- **What counts:** only "exact" or "family" results marked "narrow". A result marked "example offer" counts **nowhere**: not for demand, crowding, platform presence or photos.
- **Order:** for each type, websites are reviewed in **alphabetical order of their internal names**. So the Amazon best-seller list ("amazon-bestsellers") is always reviewed first, and Meta ("meta-ads"), the main demand source, almost last.

**Day's numbers:**
- 67 AI uses, **2.14 million input tokens (41% of all research AI input)**, 30 AI minutes.
- 1,087 listings judged: 607 accepted, 401 unrelated, 79 unclear. Only 264 counted as "narrow"; 283 exact or family matches were marked "example offer" and so did not count.
- By website: Amazon 695 listings, Flipkart 238, Instamart 50, BaapStore 28, Blinkit 27, DeoDap 26, **Meta 22**, Zepto 1.
- **Still waiting at the stop: 1,993 saved listings and ads**, which make up about 180 review batches: about 36 AI uses, so at least 36 rounds at 5 batches per round.

---

## 10. How crowded is it? (Amazon and Flipkart)

Code only, no AI. Uses only listings the AI judged as the same type ("narrow"). The first rule that matches wins:

| Level | Rule |
|---|---|
| High | At least 1 best-seller hit (list or badge) |
| High | At least 3 relevant listings with 1,000+ ratings |
| High | At least 8 listings showing a brand and 5+ different brands (never fired: pages rarely show brands) |
| High | At least 2 relevant listings with "1K+ bought in past month" |
| Not checked | Any saved page unreadable |
| Low | At most 2 relevant listings across both sites, none with 200+ ratings, and every listing shows its rating count |
| Medium | Everything else |

- High can be decided from one site. Low and Medium need **both** sites read **and** reviewed.
- Score: Low 25 points, Medium 12, High 0, not checked 0 **and the type is held**.
- **Day:** 9 decided (7 High, 1 Medium, 1 Low), 25 not checked.
- **Watch out:** the one Low and the one Medium happened only because many matching listings were marked "example offer" and not counted. Counting them makes 9 of the 10 decided types High. Under these thresholds, almost anything that sells on Amazon is "High".

---

## 11. The score, the labels and the gates

### 11.1 The score (out of 100, the owner's weights)

| Part | Max | Earned by |
|---|---|---|
| **Want** | 20 | Meta ads (up to 8: 6 if "positive" = 3+ advertisers or 5+ active ads with one 14+ days old; 8 if also 5+ advertisers; 2 for any relevant ad), Google searches (up to 5), platform lists (up to 5), quick-delivery stock (up to 2) |
| **Early** | 30 | Rising searches (8 or 12), advertisers who started in the last 30 days (3/6/9), new-arrival lists (6 or 9) |
| **Room** | 25 | Crowding: Low 25, Medium 12, High 0 |
| **Product** | 25 | Checklist: 10 for 5 Meets, plus 3 per extra Meet |

Only reviewed evidence counts. **Anything not yet reviewed counts as zero.**

### 11.2 Labels (checked in this order)

1. Want under 5 → **Too quiet**. This is given even before crowding is known, so a type whose evidence simply hasn't been reviewed yet shows "Too quiet".
2. Crowding not checked → no label.
3. High crowding → **Proven but crowded**.
4. Early 15+ and want under 10 → **Early bet**.
5. Otherwise → **Sweet spot**.

### 11.3 Every gate a type must pass to be report-ready

1. In one of the four categories, not a big brand, identity settled.
2. A current checklist on the latest wording.
3. **At least 5 Meets.**
4. **A known crowding level** (any level, High included).
5. **At least 5 "want" points** from reviewed evidence no older than 7 days.
6. **At least one supplier search read** (even a refused one counts).
7. If already sent to the client before, a reviewed "useful update".
8. Saved facts pass their checks.

### 11.4 Day's numbers

| Part | Types with 0 points |
|---|---|
| Want | **26 of 34** |
| Early | **32 of 34** (no type had any rising-search or new-arrival points) |
| Room | 32 of 34 (7 High + 25 not checked) |

- Score range 0–44, median 16.
- **Labels:** 32 "Too quiet" (most only because nothing was reviewed yet), 1 "Proven but crowded", 1 none. **No Sweet spot or Early bet existed.**
- **How many websites show each type** (counting only reviewed, relevant results): 0 websites for 22 types, 1 for 6, 2 for 4, 3 for 1, 4 for 1.

---

## 12. Suppliers

### 12.1 What happens

1. **Every** type gets supplier questions for Google Maps, IndiaMART, TradeIndia and "company pages", at the same time as its market checks. Types that later fail the checklist or are called "too quiet" still get the full supplier work (about 39% of supplier reads went to such types).
2. **Searches:**
   - Google Maps: "<phrase> manufacturer supplier India" → place names only.
   - IndiaMART (signed in): up to 10 results with supplier, city, price and minimum order. **Only the link is used; the price and minimum order are thrown away.**
   - TradeIndia: a plain read (failed 26 of 26 times) and then a browser read.
3. **"Company pages":** up to 6 result pages per type are opened and saved. On 16 October, 138 of 183 were Google Maps place pages (about 580 characters: name, rating, address; each needed two reads). **No supplier's own website and no terms page was read all day.**
4. **AI supplier reading:** one AI use per saved page (about 18,000–21,000 tokens each) pulls out 17 terms (single-piece orders, direct dispatch, minimum order, COD, plain packaging, private label, fees and more), each with an exact quote.
5. **The supplier gate:** a type needs just **one** real supplier read on any route (or a matching platform listing) to pass.

### 12.2 What went wrong

- **The AI supplier reading is broken for every current type.** Code rejected 23 of 24 answers because the AI quoted the company name and the address separately (the code wants both in one quote) or added an extra field. So 435,000 tokens were thrown away. The same rejected answers are re-checked every round and keep taking the 5 supplier slots. **Result: 0 supplier leads for any current type.** Only one not-yet-defined type (the vague "decor item") has leads (7), read by an older, simpler path.
- **Platform leads are hidden.** 14 matching DeoDap and BaapStore listings (with wholesale or reseller prices) were saved as leads for 5 types, but the report's supplier section never shows them. The one report-ready type (the duster) would print "Supplier search did not produce any usable results." even though DeoDap lists 5 matching items at a wholesale price.
- **Old search wording.** When a type's phrase changed, the old searches still ran: 17 wasted reads, including 6 of the day's 9 signed-in IndiaMART reads.

### 12.3 Day's numbers

| Route | Questions | Read | With results | Not read yet |
|---|---|---|---|---|
| Google Maps | 47 | 30 | 28 | 17 |
| IndiaMART | 47 | 11 | 9 | 35 |
| TradeIndia plain | 47 | 27 | 0 (all failed) | 20 |
| TradeIndia browser | 19 | 15 | 14 | 4 |
| Company pages | 38 | 28 | 17 | 10 |

**Cost:** supplier work took **45% of all logged website attempts (193 of 427, counting saved pages reused) and 76% of website-reading time** that day. Per type: 10–20 reads, about 120,000–190,000 AI tokens and 5–8 minutes of machine time.

---

## 13. Photos

- **No AI.** An AI photo check exists in the code but is not switched on.
- Photos are downloaded only from listings the AI judged to be the same type ("narrow"). Minimum short edge is 128 pixels. A missing photo never blocks a type.
- **Day:** 15 downloads (14 saved, 1 failed). 11 types have a photo, mostly 320-pixel Amazon thumbnails. 23 types have no photo candidate yet.
- **Time:** the photo desk ran 310 times for about 70 minutes, mostly rebuilding the list of report-ready types before each pass, for those 15 downloads.

---

## 14. Making and releasing a report

1. **Schedule.** One research week (from Monday) owns three issues: Tuesday, Thursday and Saturday at 9:00 am India time.
2. **Filling.** Each issue is filled to 10 types, earliest issue first, then extras are added one at a time up to **15**. Nothing fills above 15.
3. **New vs updates.** At least 80% must never have been sent to the client. A type can be in only one live issue, ever; after that it can only return as an "update".
4. **Order inside an issue.** New first, then Low/Medium crowding before High, then score, then early points, then checklist, then category balance. High crowding only fills a gap while fewer than 10 Low/Medium types are ready.
5. **Short week.** If fewer than 10 are ready, **nothing** is published; the draft is saved as "short". Saturday 17 October is short by 9 (1 type ready).
6. **Writing.** The AI writes a title and, for each product, a reason (up to 450 characters), up to 3 points and 10 checklist phrases; English first, then Hinglish. That is about 6 AI uses for a 10-product bilingual issue. A code check refuses numbers, links, and words like "profit", "guarantee" and "will sell".
7. **Files.** A portrait A4 PDF per language and one English Excel workbook (5 sheets). **The phone summary is not built.**
8. **Approval and release.** Drafts go to the private website. **There is no button or tool today for the owner to approve a report**, so nothing could be released yet. Languages after issue 2 are not yet chosen.
9. **Wording written by code that sounds like proof or disclaimers** reaches the client today, for example "no sales guarantee", "not sales proof", "zero is not proof of absent demand", "unit and price basis not verified". The website re-checks this text, so changing it means changing the website too.

**Day:** 0 reports, 0 AI writing uses.

---

## 15. Where the time went

Measured from the run records. Totals include website and AI time inside each desk.

| Desk | Minutes | What it mostly was |
|---|---|---|
| Acquire (website checks) | 294 | Only 45 minutes was actually waiting for websites; 98 minutes was reading and applying saved records; 44 minutes were failed actions, mostly company pages |
| Match (definitions + "same product?") | 120 | About 30 AI minutes; the rest local processing |
| Discovery (finding ideas) | 91 | |
| Photos | 71 | Mostly rebuilding the ready-types list |
| Suppliers (AI page reading) | 42 | 9 AI minutes |
| Fit (checklist) | 38 | 15 AI minutes |
| Profiles (search phrases) | 30 | 5 AI minutes |
| Platforms | 22 | |
| Crowding | 12 | |
| Reports | 5 | |
| Also: readiness checks 15, choosing what to run 17 | | Grew from under a second to 1–2 minutes per round by evening |

**The program's own bookkeeping per website read grew from 1.7 seconds in the morning to 41 seconds by evening**, while the websites themselves answered in about 8 seconds all day. The research history grew to 9,081 records (105 MB). A feature that keeps this fast by saving a summary of the history from time to time (the "checkpoints") was never switched back on after the fresh start, so every start and many reads go through the whole history. (The speed fix built on 16 October and not yet loaded makes rounds 7–9 times faster on copies of real data.)

In 13.7 hours the program made about **410 real website visits, roughly 30 an hour**.

---

## 16. Where the AI went (tokens and time, step by step)

**Totals for the day:** 244 AI uses carrying 522 items; **5.25 million input tokens** (only 4.3% reused from cache) and **331,000 output tokens**; 81 minutes of AI time. First use 07:06, last 18:08. No photo or report-writing uses.

| AI step | Uses | Items | Input tokens | Output tokens | AI minutes |
|---|---|---|---|---|---|
| Read raw ads and listings (step 1) | 16 | 130 | 297,000 | 22,000 | 5.3 |
| New or already known? (step 2) | 20 | 20 | 315,000 | 3,800 | 3.2 |
| Define the product type (step 3) | 17 | 34 | 270,000 | 12,000 | 4.1 |
| Duplicate check | 17 | 34 | 271,000 | 2,900 | 2.5 |
| Heading check | 7 | 9 | 115,000 | 2,900 | 1.3 |
| Example check | 2 | 2 | 31,000 | 300 | 0.3 |
| Grow the product map | 15 | 69 | 341,000 | 28,000 | 5.7 |
| Write search phrases | 20 | 35 | 324,000 | 19,000 | 5.0 |
| 10-question checklist (step 4) | 35 | 61 | 611,000 | 80,000 | 14.8 |
| "Same product?" reviews | 67 | 95 | **2,142,000** | 122,000 | 30.4 |
| Read supplier pages | 28 | 33 | 533,000 | 39,000 | 8.7 |
| **Total** | **244** | **522** | **5,250,000** | **331,000** | **81** |

**These totals leave out the AI's hidden "thinking" tokens**, which the program does not record today. Real use of the allowance is therefore higher than shown.

**The biggest lesson:** every AI use costs about **15,500 input tokens before it reads anything** (the fixed instructions sent each time). 144 of the 243 uses that finished normally carried just one item (the 244th was cut off by the power cut). So about **72% of all input tokens were this fixed cost**. Putting 10–20 items in each use would cut AI use several times over.

**The weekly allowance (the owner's ChatGPT Plus Codex):** 53% used at 17:04 on 15 October, 75% at 15:38 on 16 October, 100% at 18:10 on 16 October. Research's own share cannot be split out exactly: the program records only the latest reading, and the owner's other Codex work uses the same allowance. The jump from 75% to 100% came while research recorded only 25 AI uses (520,000 tokens). That is either the owner's other Codex use or the unrecorded "thinking" tokens; the records cannot tell which. From 18:10 every research AI step waited; the program's last reading said the allowance would reset on 22 October at 06:13 UTC. The owner reset the allowance later that night; the program will see this at its next reading, when research is restarted. That wait does not appear in the run records at all.

---

## 17. Each website's day

| Website | Real visits | Answered | Failed | Typical website time |
|---|---|---|---|---|
| Meta ads (per type) | 32 | 18 with ads, 7 none, 7 unreadable | 0 | 8 s |
| Meta (finding ideas) | 25 | 15 with ads | 0 | 6 s |
| Amazon search | 34 | 32 | 0 | 9 s |
| Amazon best-sellers | 5 page loads, shared | 5 | 0 | 9 s |
| Flipkart | 32 | 29 | 0 | 6 s |
| Google Trends | 16 browser reads | 8 charts, 3 "no data" | 5 (4 were our own window-drawing fault, fixed at 11:09) | 15–22 s |
| IndiaMART | 13 browser starts | 9 | 2 (1 sign-in wall at 07:13) | 14–24 s |
| TradeIndia | 42 | 14 | 26 plain reads not recognised | 4–8 s |
| Google Maps | 30 | 28 | 1 | 15 s |
| Company pages | 28 reached the internet | 17 | 12 timeouts | 24–28 s |
| DeoDap / BaapStore feeds | 133 requests | all | 0 | — |
| Dropdash | 5 browser visits | 0 products | all | — |
| Blinkit / Zepto / Instamart | 10 searches | 9 | 0 | 5 s |

**No website refused us all day.** Every "block" in the records was caused by our own side: the sign-in window being open, a Trends window not drawing, a supplier page not yet saved, or the Dropdash sign-in running out.

**Why work waited** (number of times):

| Reason | Times | Plain meaning |
|---|---|---|
| Round's 16 reads used up | 212 | Moved to a later round |
| One question re-trying a site for everyone | 98 | After a wait, only one question tries the site first |
| Owner's sign-in window open | 80 | Opened 11:18, closed about 16:50: no signed-in site could be read for about 5.5 hours |
| Round's 10 AI uses used up | 41 | |
| Trends window not drawing | 38 | Our fault, fixed at 11:09 |
| IndiaMART not signed in | 36 | Until 17:45 |
| "Lookup file too big" | 14 | Harmless: nothing reads that file; it only looks alarming |

---

## 18. Dropdash: what happened and why

| Visit (UTC) | What happened |
|---|---|
| 07:00, 07:10, 08:18 | Signed in, but the old page reader found no product cards (it was fixed at 11:07) |
| 14:19 | Waited: the owner's sign-in window was open |
| 17:26 | Dropdash sent us to its sign-in page. Recorded wrongly as a plain "read failed" |
| 18:36 | Sign-in page recognised; the window now says "Dropdash needs you to sign in" |

- **The account was signed out between 08:18 and 17:26 UTC, not at 18:37.**
- **Most likely cause: the Dropdash session ran out.** The owner signed in at about 18:19–18:30 UTC on 15 October (about 23 hours earlier), and the program had not touched Dropdash for 9 hours (it was backing off after its own reader faults, then the sign-in window was open).
- **Too many visits is ruled out:** only 3 successful page loads all day, none in the 9 hours before. The daily allowance was 300; we used 5.
- **Possible, cannot be checked:** someone signing in to the same Dropdash account on a phone or another browser that afternoon, if Dropdash allows only one session.
- **Products from Dropdash so far: 0.** Because no Dropdash catalogue was saved, about 32 product types never got a Dropdash check. The program refused them silently while planning (579 times over the day).
- **Two program faults found:** a sign-in page that crashes the reader is logged as "read failed", and a successful signed-in visit is only recorded when product cards are seen, so the window can never say "last seen signed in at…".

**What the owner has given us:** on 15 October an email address and a password were shared in chat. Dropdash's sign-in form asks for a **10-digit phone number** and password. The program has never typed into any website: today it only reads, and signing in is the owner's job. The safe-handling rules of this session also stopped the agent from using a password copied out of chat. Section 6 of the plan proposes a safe automatic sign-in for Dropdash.

---

## 19. What we can see today, what we can't, and the new tracing tool

**Today we can see:** a meter with totals (candidates, types, assessed, passing, eligible, reports), waits and failures (several counted up to 3 times; "access blocks" that are not real refusals), allowance lines, and the desktop window.

**Today we cannot see:**
1. A funnel showing how many items entered, passed, failed or are waiting at each desk.
2. AI use per step in tokens and time (only a total count appears).
3. A product's full journey in time order.
4. Per-website visit counts and times.
5. Dropshipping platform visits (nothing is logged per visit, including Dropdash).
6. Checks that were never created (for example the Dropdash checks for about 32 types).
7. The AI allowance wait, and the allowance over time.
8. Per-AI-use allowance share.

**New tracing tool (built 16 October, read-only; see the handoff for whether it has been added to the live program yet):** `scripts/research_trace.py` gives:
- **funnel** — every step with counts and reasons;
- **routes** — every website's visits, answers, failures, waits and times;
- **ai** — every AI step's uses, tokens and minutes, per day;
- **product** — one product type's full journey;
- **summary** — all of these, short.

It reads the saved records without changing anything. The deeper fixes (logging what is missing above) are in the plan.

---

## 20. Real product journeys

**1. Extendable microfiber duster — the only report-ready type (score 37/100, "Proven but crowded")**
1. 07:27: picked up from a discovery result; 07:28: checklist 7 Meets (Good fit).
2. 07:34: defined as a type.
3. 07:34–07:54: the AI reviewed 30 Amazon best-seller list items for it; all unrelated.
4. 07:52: the first supplier search (Google Maps) clears the supplier gate. TradeIndia needed two reads. 9 supplier pages were saved; 5 AI supplier readings found 2 exact offers with prices, **but all 5 were rejected by the code**.
5. 08:03–09:13: Amazon (24 exact listings) and Flipkart (10) reviewed. DeoDap: 5 matching items **on its best-seller list**.
6. 09:19: crowding **High** (4 listings with 1,000+ ratings, the largest with 22,607).
7. 09:24: Meta reviewed: 12 relevant ads from **10 advertisers**, 7 of them started in the last 30 days. Now eligible.
8. Score: want 12 (Meta 8 + DeoDap list 4), early 9 (7 new advertisers), room 0 (High), product 16. Trends was never read (spacing). Quick-delivery skipped (High).
9. Report: placed in Saturday 17 October's issue, which is short by 9. Its supplier section would say "no usable results".

**2. Revolving airtight spice rack — "Too quiet" (score 44, 8 Meets)**
- Meta phrase "revolving airtight spice rack wide mouth" (5 words) → "no ads". No shorter phrase is ever tried.
- Trends phrase queued, never read.
- Amazon/Flipkart: 60 listings reviewed; 44 were "same family" but marked "example offer", so crowding counted 0 → **Low** (25 points). Counting them would make it **High**.
- 2 quick-delivery credits spent; nothing relevant.
- **Want 0 → blocked**, despite the highest score of the day.

**3. Multi-compartment travel organiser — "Too quiet" (7 Meets)**
- Meta page not recognised; Trends not drawn (our fault).
- Amazon 20 relevant → **Medium**; Flipkart 26 relevant but all "example offer", not counted.
- **8 quick-delivery credits** across 3 cities and 3 apps → 1 in-stock match → 1 point. 93 more results never reviewed. Want 1 → blocked.

**4. Baby lotion and a black storage container — "Too quiet" without anyone looking**
- Both had **Meta ads (30 and 26) and a Trends chart saved but never reviewed**. Unreviewed means 0 points, so both were labelled "Too quiet".
- The baby lotion was searched twice on every website ("Baby Lotion" and "baby lotion").

**5. Butterfly wing pillow — rejected by the checklist (4 Meets)**
- Earlier wording gave it 3 Meets and 6 Unclear; the newer wording turned the Unclears into "Does not meet". It will never get a market or supplier check again.

**6. Transparent table protector — supplier work on a type later rejected**
- Its phrase changed after 15 minutes; the old searches still ran later (Maps, TradeIndia and one of the day's few IndiaMART reads), all thrown away. 3 AI supplier readings were all rejected by the code. It was later labelled "Too quiet".

---

## 21. "What happens if…": the common situations

| Situation | What the program does today |
|---|---|
| A Meta search shows no ads | The type earns 0 Meta points. A second, broader phrase is tried only if the first answer was reviewed as empty and another phrase exists (almost every type has only one) |
| A website refuses us | That site waits 5 minutes, then 1 hour, 6 hours, 1 day, with one test question trying first each time. Other sites carry on |
| A sign-in runs out (Trends, IndiaMART, Dropdash) | Only that site waits, calmly, until the owner signs in again. The window shows a line |
| The owner's sign-in window is left open | Every signed-in site waits until it is closed (5.5 hours on 16 October). No reminder |
| The AI allowance runs out | Every AI step waits until the allowance resets. Website reads that need no AI carry on, but nothing new gets judged |
| The round's 16 reads are used | The rest wait for the next round |
| A product is found on 0, 1, 2 or 4 websites | Today this count is **not used at all**. Only the score parts count, and only reviewed "narrow" results |
| A type has 4 Meets | Rejected for good, unless its definition changes |
| A type has 5 Meets | Passes, the same as 10 Meets (only the score differs) |
| Crowding is High | Allowed in a report, but only to fill a gap when fewer than 10 Low/Medium types are ready. No quick-delivery check |
| Crowding cannot be decided | The type waits; it can never be reported |
| Want is under 5 | "Too quiet": cannot be reported, even if nothing was reviewed yet |
| Supplier search finds nothing | Fine: the report says "Supplier search did not produce any usable results" |
| No photo | Fine: reported without one |
| Fewer than 10 report-ready types | Nothing is published that day |
| A type's search phrase changes | New questions are made; old ones still run and are thrown away |
| The AI gives an answer in the wrong format | One repair is tried. For supplier pages there is no repair: the same wrong answer is re-checked every round |

---

## 22. Faults and waste found, ranked by effect

1. **"Too quiet" before anyone looked.** Unreviewed evidence counts as zero, and Meta (the main demand source) is reviewed almost last because of alphabetical order. 3 of the 6 "too quiet" types had unreviewed ads and charts.
2. **The demand gate itself.** Want ≥ 5 rejects almost everything new: 26 of 34 types had 0 want points.
3. **The AI's fixed cost per use.** About 15,500 tokens per use and mostly one item per use, so about 72% of AI input was fixed overhead.
4. **Listing-by-listing AI review.** 41% of all AI input, 44% of judgments "unrelated" or "unclear", and 1,993 items still waiting.
5. **"Example offer" results don't count.** 38–74% of relevant Amazon/Flipkart results are dropped. This flips crowding answers.
6. **Supplier AI reading rejects almost everything.** 23 of 24 answers rejected; 0 leads for current types. The rejected items block the supplier slots every round.
7. **Supplier work for every type.** 45% of reads and 76% of reading time, including types later rejected.
8. **Platform leads hidden.** Ready-made dropship sources (with prices) never reach the report.
9. **The best idea source is the most limited.** Platform lists gave 13 types from 15 items (87%) but are capped at 30 items a day, with 839 waiting.
10. **Silent drops.** When the AI was briefly unavailable, 15 platform items and 10 rising-search ads were marked "done" and never read.
11. **Trends charts use up headings.** A Trends chart can never create a product, but it marks its heading "searched", pushing that heading's Meta search to the back of 1,674 others. Now that Trends works, most new heading waves would be charts.
12. **Meta search brings mostly old ads.** 61% inactive; nearly half of what the AI reads is junk.
13. **Repeated checklists** after each wording change (58 for 34 types).
14. **Old-phrase searches still run** after a phrase changes (Meta, Amazon, Flipkart, supplier sites).
15. **The program's own bookkeeping grows** with the history (1.7 s → 41 s per read).
16. **The sign-in window blocks all signed-in sites** while open, with no reminder.
17. **Counts that mislead:** failures counted up to 3 times; "access blocks" that were not refusals; "lookup file too big" shown as a wait.
18. **Missing promised outputs:** compliance flags, extra category tags, the phone summary and an owner approval button do not exist in the live program.

---

## 23. Why it is slow, in one list

1. **Every type must pass every desk**, and each desk has its own queue, round cap and AI cap.
2. **The demand gate needs proof** that new products rarely have, and counts unreviewed evidence as no evidence.
3. **The AI is used one small item at a time**, and most of its cost is fixed overhead.
4. **Most work goes to checking, not finding**: 86% of reads and 61% of AI went to types already found, much of it on suppliers and Amazon/Flipkart listings.
5. **The best source of ideas (platform best-seller lists) is capped at 30 items a day.**
6. **Rounds slow down as the history grows**, because of the program's own bookkeeping.
7. **Outages on our side:** the sign-in window (5.5 hours), the Trends drawing fault (morning), Dropdash signed out, and the AI allowance used up at 18:10.

The plan for a faster, ranking-based approach is in [`plan-2026-10-17-ranking-and-big-universe.md`](plan-2026-10-17-ranking-and-big-universe.md).
