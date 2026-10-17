# How the research program works, where the AI really went, and your new direction (discussion paper, 17 October 2026)

**What this is.** The owner's reply of 17 October 2026 asked for a full, toddler-level explanation of the program with every number adding up, a look at AI use, and a discussion of a new direction. This paper answers all of it. **It changes nothing in the program and does not change Plan R18.** Plan R18 will be rewritten only after the owner has agreed the direction below.

**Project stage:** Development and testing (confirmed 13 October 2026). Research has been stopped since 16 October 20:50:41 UTC.

**Times** are UTC; India time is UTC + 5 hours 30 minutes.

**Sources.** The program's own saved records for the research day (16 October 06:58:54 to 20:50:41 UTC), the program's code, and, for AI use, the usage numbers that Codex itself writes into its log files on this computer (only the numbers and settings were read: time, tokens, allowance percentages and model names, never the content).

**On one screen:**
1. **Dropdash** is still empty: the safety rule stopped the agent from typing your password. Easiest fix: you sign in once (5 minutes), then the next agent reads the whole list (Part A, question 20).
2. **The 16% after your reset was used by your own Codex projects, not by research.** Research's last AI call was at 18:08 UTC, before the reset. The allowance also got much more expensive at 18:09 on 16 October; research's real cost today must be measured (Part B).
3. **Every number now adds up:** 354 listings collected → 128 read by the AI → 78 products → 19 removed (outside categories or big brands) → 34 new product types. After that, the checks run **side by side** on the same 34, which is why "9 decided" and "26 of 34" are not a chain (Part C).
4. **The faults** (duster suppliers, spice rack, baby lotion, unread items) all come from one habit: "not looked at yet" was treated as "nothing found", plus a few ordering and format bugs (Part D).
5. **Your direction is sound.** It needs a few decisions: what exactly removes a product, the report mix, the first draft of points, and who comes first on the AI allowance (Part E, questions 1–4, 9, 16).
6. **Sign-ins:** after a one-time Dropdash setup, only IndiaMART needs you routinely, with a repeating alert and a mute (Part F).

---

## Contents

- Part A. Dropdash today
- Part B. Where the AI allowance really went
- Part C. How the program works today, step by step, with every number adding up
- Part D. Why the faults happened
- Part E. Your new direction: what I understood, where I agree, where a decision is needed
- Part F. Sign-ins: keeping your part as small as possible
- Part G. Questions for the owner

---

## Part A. Dropdash today

- **Not populated.** On 17 October the agent tried to sign in to Dropdash in the program's own Chrome with the phone number and password you gave in your message. The very first step (only looking at the sign-in page) was stopped by Claude Code's safety rule for automatic mode ("credential exploration"), and the rule then also stopped edits to that helper script. The agent deleted the script. Nothing was typed into Dropdash, and the agent did not write the password into any file.
- **A file called `dropdash.txt` appeared in the project folder** at 06:04 UTC (11:34 am India time) on 17 October. The agent did not create it and did not open it. If it holds the Dropdash password, please delete it: a password in a plain file in the project folder could end up in the project's online copy. It has been added to the project's "never upload" list so that cannot happen by accident.
- **The rule stops the agent itself from handling a password, even one you give directly.** Reading Dropdash after someone has signed in needs no password, so it should not be blocked. A program feature that reads the password from Windows' own password safe (Windows Credential Manager) at the moment of signing in means the agent never sees it; whether the safety rule also lets the agent *build* that feature cannot be known until it is tried.
- **Three ways forward** (question 20):
  1. *Recommended for today:* you sign in once yourself (about 5 minutes; steps in question 20). The next agent then reads the full Dropdash product list, with no password involved.
  2. You add a Claude Code permission rule that lets the agent sign in. Not recommended: the password would pass through the agent again.
  3. Wait for the automatic sign-in feature (question 19).
- **Note:** today's program reads only a *sample* of Dropdash (its home page, its "Profitable Products" list, and the first 18 products of each of its 21 categories once a week). Reading the whole list needs a small change to the reader; that is part of the new design.

---

## Part B. Where the AI allowance really went

### B.1 The short answer

**The research program did not use the 16% after your reset.** Its last AI call started at **18:08:39** and finished at 18:09:01 on 16 October, more than two and a half hours before your reset at 20:41:55. The 16% was used in 19 minutes by **your other Codex projects** on this computer ("task management" and "neuraflow-website-factory"). Every research AI call goes through one gate in the code and is recorded: 244 calls were started that day (243 finished; 1 was cut off by the 07:30 power cut), and none after 18:08:39.

(A note on words: "AI call" and "AI use" mean the same thing here: one request to the AI.)

### B.2 The timeline, minute by minute

Counts of your projects' calls are approximate (they come from Codex's log lines).

| Time (UTC) | What the allowance did | Research program | Your other Codex projects |
|---|---|---|---|
| 16 Oct 07:02 | Weekly allowance 65% used (35% left) | Research had started at 06:58 | Running |
| 07:02 → 18:08 | Weekly 65% → 77% used (**+12 points in 11 hours**) | 244 AI calls: 5.25 million tokens read (5.0 million fresh), 0.33 million written | About 2,040 AI calls: about 387 million tokens read, of which about 14 million fresh and the rest re-used ("cached", much cheaper), 1.35 million written |
| 18:09:01 | — | **Last research AI call finishes** | — |
| 18:09:17 → 18:09:32 | A new **5-hour limit** appears in OpenAI's replies, and the weekly figure jumps **77% → 100% used** | 0 calls | Normal pace |
| 18:10 → 20:41 | Weekly stays at 100% used | 0 calls (waiting) | About 820 AI calls (168 million tokens), which ran on your **paid Codex credits** because the plan was used up |
| 20:41:55 | **Your reset**: weekly back to 0% used | 0 | — |
| 20:42 → 21:01 | Weekly **0% → 16% used**; 5-hour limit **0% → 100% used** (full at 21:00:54) | 0 calls (research was stopped at 20:50:41) | About 140 AI calls: 29.2 million tokens read (0.6 million fresh) |
| 21:01 → 22:34 | 5-hour limit full, so the weekly figure stayed at 16% used (84% left) | 0 | About 140 AI calls, again on paid credits |
| 22:34 → 17 Oct 06:00 | Nothing on this computer | 0 | No Codex work on this computer (the Windows side has had none since 2 October) |
| 17 Oct 06:00 | 5-hour 0%, weekly 16% used | 0 | — |
| 17 Oct 06:08 → 06:42 | 5-hour 0% → 29%, weekly 16% → 20% used | 0 (research is stopped) | Your "task management" project started working again |

What this means:
1. **The 77% → 100% jump at 18:09 was not usage.** Nothing used 23% of a week's allowance in 15 seconds, and research made no call then. It happened at exactly the moment OpenAI's replies started showing a new 5-hour limit, so it looks like a change on OpenAI's side. The program's last reading (18:10) still showed your plan as "Plus". It is worth checking in your ChatGPT account whether your plan or a special offer changed at that moment (question 18).
2. **Why the weekly figure stayed at 16% used (84% left) overnight:** at 21:01 your 5-hour limit was full. From then on your projects could only continue on paid credits, which do not move the weekly figure. After 22:34 no Codex work ran on this computer.
3. **Paid Codex credits** (not the quick-delivery credits in Part E): 59,904.14 at 18:10 on 16 October, 58,522.08 on the morning of 17 October, so **1,382 credits used**. About 1,340 of them match your projects' work on this computer (about 1,100 between 18:10 and 20:42, about 240 between 21:01 and 22:34). About 44 were used after the last logged call here; credits are account-wide, so another device, a cloud task or a GitHub review would also show up. The research program never uses credits: it is built to stop and wait.
4. **What research cost, honestly.** On 16 October the weekly figure moved 12 points during the research day, while research and your projects both ran. Research read about 5.0 million *fresh* tokens against your projects' about 14 million fresh (plus their 373 million cheap re-used ones). At the rate of the 16 October morning, research's share was roughly **1 to 3 points**.
5. **But the allowance now buys much less than it did that morning.** On 14 and 15 October, 17–19 weekly points covered about 4–5 million fresh tokens in two hours; on the morning of 16 October about 5.5 million fresh tokens moved it only 3 points; after the 18:09 change, 0.6 million fresh tokens (plus 28.6 million re-used) took 16 points and the whole 5-hour window. Research uses a smaller AI model ("gpt-6-luna" at high thinking) than your projects ("gpt-6.1-sol" at maximum thinking, as Codex's own session settings show), and is probably charged less per token, but **how much one research call costs today is unknown until it is measured**. One 5-hour window now equals about 16% of the week.

### B.3 What is recorded today, and what is not

| Number | Recorded? |
|---|---|
| Every research AI call (step, items, time) | Yes: 244 started on 16 October, all accounted for |
| Tokens read ("input") and re-used tokens ("cached input") | Yes |
| Tokens written ("output") | Yes. **Correction:** on 16 October I said the AI's hidden "thinking" was not recorded. It is: Codex counts thinking inside the "written" number. What is missing is the split |
| Thinking tokens on their own ("reasoning output") | **No.** Codex reports this number, but the program throws it away |
| Tokens written into the re-use store ("cache write") | **No.** Reported by Codex, thrown away by the program |
| Allowance before and after each AI call (5-hour and weekly) | Yes, from the next run (the accounting built on the night of 16 October) |
| Your other Codex projects' use | No. The program cannot see it. Codex's own log files on this computer show it (question 17) |

### B.4 Why each AI call costs so much: measured on 17 October

Five tiny test calls were made with the research program's exact settings and a near-empty question ("return ok"). The 5-hour meter, read right after tests 1, 2 and 5, still showed 0%, so their cost was too small to see.

| Test | Tokens read for a near-empty question |
|---|---|
| 1. Today's research setup | **15,000** |
| 2. Setup 1, but with our own one-line instructions instead of Codex's built-in "coding assistant" instructions | 11,309 (−3,691) |
| 3. Setup 2, plus 21 Codex features that research never uses switched off | 10,549 (−760) |
| 4. Setup 2, plus hiding the list of **50 add-on "skills" installed for your own coding work**, which Codex attaches to every call | **7,083** (−4,226) |
| 5. Setup 2 repeated straight away | 11,309 again, none of it re-used |

- So about **15,000 tokens of every research call are Codex's own material**, before our question. On top of that, most research calls on 16 October added only 1,000–3,000 tokens of our own instructions and data; the biggest (reviews of 20 listings at once) added up to about 58,000.
- **On 16 October, 144 of the 243 finished calls carried just one item**, which is why that fixed 15,000 dominated.
- **Re-use ("caching") between separate calls happens, but not reliably.** Test 5 re-used nothing; test 4 re-used 4,224 tokens; on 16 October, 17 research calls each re-used about 13,000 tokens. So tests 3 and 4 must be repeated a few times before their savings are quoted as firm.

### B.5 How to spend much less (proposal; questions 14–17)

1. **Record everything** for every research AI call: tokens read, re-used, cache-written, written and thinking; the AI model and thinking level; seconds; and the 5-hour and weekly allowance before and after. Show a daily line: "Research used X% of your weekly allowance today."
2. **Slim every call:** our own short instructions, the skills list hidden, unused features off. That is **about 8,000 fewer tokens per call**: roughly half of a tiny call and about a third of a typical 16 October call (to be re-tested, B.4). No new sign-in is needed (the test kept the same Codex sign-in).
3. **Group the work:** 10–20 items per AI call instead of one, whenever several are ready (never waiting for a group to fill).
4. **Fewer AI steps:** one combined "name it, group it, fill the checklist" step instead of five separate steps; no listing-by-listing reviews; no reading of supplier web pages.
5. **The right thinking level for each step:** low for simple "is this the same product?" checks, medium for the checklist, high only for defining a product type. Tested on saved examples first.
6. **A guard, with your work first** (question 16): research pauses its AI whenever your own Codex work is filling the 5-hour window (for example above 50%), keeps to a daily share of the weekly allowance that is set **after measuring** the first hours of the new design, and never uses paid credits.
7. **Expected effect:** the work of 16 October would need about 25–40 AI calls instead of 244, and several times fewer tokens. What that means in allowance points will be measured in the first hours, because of the change at 18:09 (B.2 point 5).

---

## Part C. How the program works today, step by step, with every number adding up

### C.1 Words used here

| Word | Meaning | Example |
|---|---|---|
| **Listing** | One thing seen on a website: one Facebook ad, one store product, one supplier listing | One Facebook ad for a kitchen mat |
| **Product type** | A kind of product with no seller attached. This is what a report recommends | "Memory Foam Kitchen Standing Mat" |
| **Heading** | One line of the program's product list, with ready-made search phrases | "Musical crib mobile", searched on Facebook as "baby crib mobile" |
| **Website read** | One real visit to a website (a page load) | One Amazon search for "spice rack" |
| **AI use** (also "AI call") | One request to the AI (your ChatGPT/Codex subscription). One request can carry several items | "Read these 10 ads and say what each one sells" |
| **Token** | The AI's unit of reading and writing, about ¾ of a word | A 1,000-word page is about 1,300 tokens |
| **Question** | One planned check of one website for one product type | "Search Amazon for the spice rack" |
| **Desk** | One kind of work inside the program | The "checklist desk", the "website reading desk" |
| **Round** | One pass of the program over all its desks (C.3) | Round 9: 08:00 to 08:09 |

### C.2 Where it starts: the lists

There are **two main lists** and **two small side sources**.

1. **Main list 1: the product list ("product map"), 1,706 headings** (1,694 written on the morning of 16 October, plus 12 the AI added during the day). Each heading is one kind of product with ready search phrases. Example: heading "Musical crib mobile" (Baby Products) has the Facebook phrase "baby crib mobile", and the Amazon/Trends phrase "musical crib mobile". A heading's phrase is typed into the **Facebook Ad Library** search (`facebook.com/ads/library`, India, all ads) to find ads, or into **Google Trends**. On 16 October 32 headings were used (25 on Facebook, 7 on Trends); 1,674 were never used.
2. **Main list 2: the dropshipping stores' full product lists.** DeoDap 13,527 products, BaapStore 6,053 products, Dropdash 0 (signed out). These are downloaded whole, so looking something up in them needs no website visit.
3. **Side source: 15 fixed words on TradeIndia and IndiaMART.** You asked why this sits next to the big lists. **It should not have.** It is not part of the universe and not an exhaustive list. It is a small, hand-written seed list in a settings file: 4 words for Home and Kitchen ("kitchen organiser", "kitchen utensil", "storage container", "cleaning mop"), 4 for Home and Decor ("home decor item", "wall hanging", "led fairy light", "artificial plant"), 3 for Baby ("baby care product", "baby feeding bottle", "kids toy") and 4 for Travel ("travel accessory", "travel bag", "camping tent", "car accessory"). Each word is searched on TradeIndia and IndiaMART to catch newly listed products, at most once a round per site, and again after 7 days. Nobody recorded why it is 15; it was a first small set. On 16 October only 5 words were fully read (TradeIndia), giving 22 listings, which became 9 of the 34 product types, including the spice rack, the baby lotion and the storage container.
4. **Side source: Google Trends "rising" searches.** When a Trends chart is read, Google also shows "rising" related searches; these get searched on Facebook. Only 3 were saved all day; 1 of them was searched on Facebook, and its 10 ads were lost (C.5).

### C.3 What a "round" is, with two real rounds

**The idea in one sentence.** A round is one lap of the program around all its desks. On each lap, each desk does a little work for **many** products, within fixed limits, and whatever does not fit waits for the next lap.

**The limits of one lap** (settings file `config/research/round_limits.json` and others):

| Limit per round | Value | What it means |
|---|---|---|
| Website reads | **16** | At most 16 real visits to websites in one lap, shared by all products and all websites. A look-up in a saved store list or a saved page is free and not counted. 2 of the 16 are kept for finding new ideas |
| AI uses | **10** | At most 10 requests to the AI in one lap. While reports were short of products (all day on 16 October), 2 of the 10 were kept for finding ideas, 2 for definitions and market reviews, and 1 for report writing when a report is due |
| Signed-in website time | 300 seconds | Google Trends, IndiaMART and Dropdash share this; Trends visits must be 90 seconds apart |
| Store list picks | 5 a round, 30 a day | New items taken from DeoDap/BaapStore best-seller lists |

**Why "a small pile of about 60 listings" per round.** That was a rough *ceiling*, and the exact ceiling is about 70: up to 2 heading searches × up to 30 Facebook ads each = up to 60 ads, plus up to 5 store best-seller items, plus up to 5 TradeIndia listings. In practice it was far less, because only about one heading search ran per round (32 headings in 30 rounds).

**"The AI reads about 10 at a time": 10 what?** There are two different tens:
- **Up to 10 AI uses per round** (the round's allowance above).
- **Up to 10 items per AI use.** An "item" is whatever that step works on: for the ad-reading step it is one ad or listing; for the checklist it is one product type; for "is this the same product?" it is one product type together with one website's results (up to 20 listings).
- In practice only the ad-reading step came close to 10 (8.1 items per use on average). Across all steps the average was 2.1, and **144 of the 243 finished AI uses carried a single item**.

**The desks, in the order they ran on 16 October:**
1. **Search phrases**: the AI writes search phrases for new product types.
2. **Checklist**: the AI answers the 10 questions.
3. **Website reading**: the program visits websites for each product type (this is where the 16 reads go).
4. **Matching**: the AI defines product types, checks for duplicates, and judges "is this listing the same product?".
5. **Suppliers**: the AI reads saved supplier web pages.
6. **Finding ideas**: heading searches on Facebook, TradeIndia words, and the AI reading new ads.
7. **Stores**: picks new items from the DeoDap/BaapStore lists.
8. **Crowding**: code (no AI, no visit) works out how crowded Amazon/Flipkart are.
9. **Photos**, 10. **Reports**, 11. **Private drafts**.

**Real round 1 — round 9, 08:00:02 to 08:08:50 UTC (1:30 to 1:39 pm India time), 8 minutes 48 seconds:**
- AI use 1: search phrases for 2 types (a baby-care kit and baby lotion).
- AI use 2: the checklist for those same 2 types.
- Website reading: 5 reads here (supplier pages for 3 types, a Google Maps search, and a Trends try that failed because of our own window fault). The desk then stopped itself because of a counting fault (Part D, fault 6). With the Facebook search and the TradeIndia read below, the round used **7 of its 16 reads**.
- AI uses 3–6: "is this the same product?" for the duster's Amazon page (20 listings); an older-style review of 30 results for the vague "decor item"; one duplicate check; Amazon best-seller rows for the baby-care kit and the baby lotion.
- AI uses 7–8: one supplier web page each for the duster (both answers later rejected by the code).
- Finding ideas: 1 Facebook heading search ("baby crib mobile", 20 ads, 19 of them already stopped) and 1 TradeIndia word ("travel accessory"). AI use 9: the AI read 10 ads (5 from an earlier "duvet cover" search, 5 from this one): 3 products (2 were big-brand bedding, removed; 1 was a cot mobile, a new idea), 1 service, 5 unreadable, 1 bad answer. AI use 10: the checklist for the new cot mobile.
- The 10 AI uses were now gone, so 13 collected ads waited for the next round.
- Totals: 10 AI uses carrying 22 items, 220,189 tokens read and 16,761 written, about 3.7 minutes of AI time; about 1 minute of waiting for websites; the rest was the program's own work.

**Real round 2 — round 25, 14:22:45 to 15:10:13 UTC (7:52 to 8:40 pm India time), 47 minutes 28 seconds:**
- The reading desk looked at **204 open questions for 32 product types** and sent only **14 real reads**. Of the rest, 35 waited because the sign-in window was open, 6 were pushed to the next round when the 16 reads ran out, and the others were either answered from saved copies or not reached that round (the records do not split those two cleanly).
- 10 AI uses carrying 22 items, 221,978 tokens read.
- The photo desk spent 5¾ minutes and saved no photo.
- **About 43 of the 47 minutes were the program's own record-keeping**, not websites and not AI.

### C.4 One product or many? Why many? Why not product by product?

- **Many.** Every round works on many products at once. Each website search *is* about one product (for example "Amazon search for the spice rack"), but the program never takes one product from start to finish. Each product sits in a queue at each desk and gets a turn when that desk reaches it in some round.
- **Why it was built this way** (from the design notes in the code):
  1. Fair shares: busy work must not crowd out the rest.
  2. Restart safety: a round is a small unit that can resume after a power cut or a Stop click.
  3. Website pacing: Trends must be 90 seconds apart and IndiaMART 30; spreading many products across sites keeps every site busy while one waits.
  4. Shared work: one Amazon best-seller page or one store list serves many products, and one AI use can carry several items.
- **What it cost:** every product had to pass every desk, each desk had its own queue and caps, and the AI grouping never crossed "review groups" (batches of products that arrived together; there were 17 by the evening, most with 1–2 products). So most AI uses carried one item, and most products ended the day half-checked.
- **My view:** you are right to question it. In the new design each product is finished in one go (Part E.5). The AI still takes several items in one call whenever several are ready, because that is where grouping saves money, but nothing waits for a group to fill.

### C.5 The idea funnel, with every number (16 October)

**How this matches the steps in my earlier message.** That message listed 13 steps. Here is where each one is explained:

| Earlier step | What it was | Where now | Kind |
|---|---|---|---|
| 1 | Collect listings | C.5 step 1 | Funnel (each step feeds the next) |
| 2 | AI reads them | C.5 step 2–3 | Funnel |
| 3 | Remove (outside categories, big brands) | C.5 step 4 | Funnel |
| 4 | New or known? | C.5 step 4 | Funnel |
| 5 | AI defines the product type | C.6 "Definition" | Side by side on the 34 types |
| 6 | 10-question checklist | C.6 "Checklist" | Side by side |
| 7 | Check other websites | C.3 (website-reading desk) and C.7 | Side by side |
| 8 | AI checks every result | C.6 notes and C.8 | Side by side |
| 9 | How crowded (Amazon/Flipkart) | C.6 "Crowding" and C.7 | Side by side |
| 10 | Demand ("want") score | C.6 "Want points" | Side by side |
| 11 | Supplier search | C.6 "Supplier search" | Side by side |
| 12 | Report-ready | C.6 "Report-ready" | Result of all of them |
| 13 | Report (needs 10 ready) | Part D | 0 made |

**Correction first:** my earlier figures "347 collected, 128 read, 219 never read" were wrong in two ways. The true figures are **357 saved, 354 different listings** (3 Facebook ads were saved twice), **128 read and 226 not read**. The earlier 347 left out 10 ads that its own table included, and counted the 3 duplicates.

**Step 1. Listings collected to find ideas: 354.**

| Where from | Listings |
|---|---|
| Facebook ads from 25 heading searches (15 returned ads, 3 said "no ads", 7 pages were not recognised) | 292 (295 saved; 3 duplicates) |
| Facebook ads from one Trends "rising" search | 10 |
| DeoDap best-seller and new lists | 20 |
| BaapStore trending list | 10 |
| TradeIndia new listings (the 15-word source) | 22 |
| **Total** | **354** |

**Step 2. The AI reads them: 354 − 226 not read = 128 read.**

| Why not read | Listings |
|---|---|
| Facebook ads still waiting in line (the idea desk gets only 2 of each round's 10 AI uses, and each use reads at most 10) | 156 |
| Facebook ads that hit the program's 5-minute "AI pause" at 19:38, which the program set because the allowance showed "used up" | 23 |
| Facebook ads turned back directly by the "used up" allowance between 18:28 and 20:35 | 20 |
| Facebook ads whose AI answer was in the wrong shape | 2 |
| Store items (10 BaapStore, 5 DeoDap) hit by the AI pause and then **silently marked "done"** (a fault, Part D) | 15 |
| Trends-rising ads turned back after 18:10 and then **silently marked "done"** | 10 |
| **Total not read** | **156 + 23 + 20 + 2 + 15 + 10 = 226** |

Read: Facebook 91 + DeoDap 15 + TradeIndia 22 = **128**.

**Step 3. What the AI found in the 128 listings: 78 products.** The AI says, for each listing, whether it shows one product, several products, a service, or nothing readable.
- 66 listings showed one product → 66 products.
- 3 listings showed several products (3, 7 and 2) → 12 products.
- 45 listings were unreadable (mostly ads whose text held only "Inactive, 6 May–28 May" and similar).
- 14 listings were services (clinics, events, apps).
- Check: 66 + 3 + 45 + 14 = 128 listings; 66 + 12 = **78 products**.

**Step 4. What happened to the 78 products.** This is all one step ("new or already known?"). Inside it, the program first throws out two kinds, using the AI's own answers from step 3 (the AI says the category and whether it is a big brand; code then removes):

| Outcome | Products | Running count |
|---|---|---|
| Start | | 78 |
| Outside the four categories (removed) | 14 | 78 − 14 = 64 |
| A big, well-known brand (removed) | 5 | 64 − 5 = 59 |
| New: code found no similar known product type, so no AI was needed | 23 | |
| New: the AI compared it with similar known types and said it is different | 11 | |
| Same as a product type already known (joined it; not a new idea) | 6 | |
| The AI could not tell (parked) | 3 | |
| Not finished: stopped by the AI pause or the limit, still waiting | 8 | |
| Not finished: stopped, then silently marked "done" (fault) | 4 | |
| Not finished: not reached yet | 4 | |
| **Check** | 23 + 11 + 6 + 3 + 8 + 4 + 4 = **59** | ✔ |

"Code finds similar ones, then the AI compares" means: the program first compares words (free). If no known product type shares enough words, the product is new straight away (23). If some do, the AI is shown the product and the similar ones and asked "same or different?" (11 different, 6 same, 3 unsure).

**Step 5. New product types: 23 + 11 = 34.** By source: DeoDap 13, Facebook 12, TradeIndia 9.

### C.6 Earlier steps 5 to 12 are checks on the same 34 types, side by side, not one after another

This is why the earlier table looked impossible. After step 5, **the 34 product types do not go through a funnel**. All the remaining checks work on the same 34 at the same time, each at its own speed. So every line below adds up to 34 on its own; you cannot subtract one line from another.

| Check | The 34 types | Adds up |
|---|---|---|
| **Definition** (the AI writes a clear name and description) | 30 accepted + 1 "too vague" (the "home decor item") + 3 whose AI answer failed a format check and was never retried (2 travel kits, a doorbell). Correction: I said "3 being retried"; they are stuck | 30 + 1 + 3 = 34 |
| **Checklist** (10 questions, 5 "yes" needed) | 30 pass + 2 below (4 "yes": bird wall hanging, butterfly wing pillow) + 2 "not enough information" (3 and 2 "yes" with many "unclear") | 30 + 2 + 2 = 34 |
| **Crowding** (Amazon/Flipkart) | 7 High + 1 Medium + 1 Low + **25 not yet worked out** (10: both sites read, AI review waiting; 6: neither site read yet; 3: one read and waiting, the other not read; 2: one reviewed, the other waiting; 4: no accepted definition, so crowding cannot run) | 7 + 1 + 1 + 25 = 34 |
| **"Want" points** (demand, 5 needed) | 26 types with 0 points, 3 with 1, 1 with 2, 2 with 4, 1 with 12, 1 with 13 | 26 + 3 + 1 + 2 + 1 + 1 = 34 |
| **Supplier search** | 24 attempted (a supplier site was read, no accepted lead) + 1 with accepted leads (the home decor item, 7 leads) + 9 not started | 24 + 1 + 9 = 34 |
| **Report-ready** (every check passed) | 1 ready (the duster) + 33 held | 1 + 33 = 34 |

**So "only 9 decided" and "26 of 34" are two different checks on the same 34 types:**
- Crowding: 9 of the 34 have a crowding answer (7 High, 1 Medium, 1 Low); 25 are still waiting.
- Want points: 26 of the same 34 have zero demand points; 8 have some.
- A type can be in both: for example the memory foam kitchen mat has no crowding answer yet **and** zero demand points.

**Every reason each of the 33 held types was held** (all conditions checked separately):

| Held because | Types |
|---|---|
| Crowding not worked out + too quiet | 14 |
| Crowding not worked out + too quiet + no supplier search yet | 9 |
| Too quiet only | 6 |
| Checklist below 5 + too quiet | 1 |
| Checklist below 5 + crowding not worked out + too quiet | 1 |
| Checklist information missing + too quiet | 1 |
| Checklist information missing + crowding not worked out | 1 |
| **Ready** | **1** |
| **Total** | **34** |

Under today's rules 32 of the 34 had fewer than 5 "want" points. So even with every crowding answer in, only the duster could have been report-ready; the home decor item had 13 "want" points, but its checklist was still open (7 "unclear" answers) and it had no definition.

### C.7 Earlier step 7 versus step 9: is Amazon/Flipkart checked twice?

**No.** Amazon and Flipkart are visited once per product type, in the website-reading desk (earlier step 7). The crowding step (earlier step 9) visits nothing and uses no AI: it re-reads the saved Amazon/Flipkart pages and counts the listings the AI already accepted as the same product (ratings, "1K+ bought", best-seller badges). That is why crowding has to wait for the AI's listing-by-listing review, and why 25 types were still waiting.

### C.8 The website results numbers

- **2,870** different listings were saved all day by every desk (Amazon 1,049, Flipkart 850, Facebook 573, DeoDap 143, Instamart 98, BaapStore 62, Blinkit 53, TradeIndia 22, Zepto 20).
- The earlier "1,087 judged" and "1,993 waiting" are not listings but **(product type, listing) pairs**: one Amazon best-seller row can be checked against several product types.
- Every one of the 2,870 listings in exactly one place: 912 judged for at least one type + 116 judged by an older method + 1,507 waiting for review + 324 idea-finding listings never used as evidence + 11 store matches not queued = **2,870**.
- Of the 1,087 judgments: 607 "same product" + 401 unrelated + 79 unclear. Of the 607, only 264 counted; 283 were marked "only this seller's offer" and were not counted as demand or crowding evidence, and 60 were "broader".

### C.9 All 34 product types at the stop

"Score" is today's score out of 100: "want" (up to 20) + "early" (up to 30) + "room" (Amazon/Flipkart crowding: Low 25, Medium 12, High 0) + "product" (checklist: 10 for 5 "yes", plus 3 per extra "yes", up to 25). The spice rack's 44 is mostly room (25, from its Low crowding) plus product (19).

| # | Product type | Idea from | Checklist "yes" | Crowding | Want points | Facebook ads: saved / AI-judged / confirmed | Score | Label |
|---|---|---|---|---|---|---|---|---|
| 1 | Revolving airtight spice rack | TradeIndia | 8 | Low | 0 | 0 (no ads for its long phrase) | 44 | Too quiet |
| 2 | Extendable microfiber duster | Facebook | 7 | High | 12 | 18 / 18 / 12 | 37 | Proven but crowded (**ready**) |
| 3 | Compartmented travel organiser | TradeIndia | 7 | Medium | 1 | 0 | 29 | Too quiet |
| 4 | Expandable adjustable storage rack | Facebook | 10 | not yet | 0 | 2 / 0 / 0 | 25 | Too quiet |
| 5 | Microfiber kitchen towel with loop | DeoDap | 8 | not yet | 4 | 0 | 23 | Too quiet |
| 6 | Adhesive magnetic door stopper | Facebook | 9 | not yet | 0 | 1 / 0 / 0 | 22 | Too quiet |
| 7 | Car interior cleaning brush set | Facebook | 9 | not yet | 0 | 21 / 0 / 0 | 22 | Too quiet |
| 8 | Wireless doorbell with plug-in chime | Facebook | 9 | not yet | 0 | 1 / 0 / 0 | 22 | Too quiet |
| 9 | Water bottle with temperature display | DeoDap | 9 | not yet | 0 | 6 / 0 / 0 | 22 | Too quiet |
| 10 | Hospital and travel dental kit | TradeIndia | 8 | not yet | 0 | 0 | 19 | Too quiet |
| 11 | Portable LCD writing tablet | DeoDap | 8 | not yet | 0 | 0 | 19 | Too quiet |
| 12 | Hand-push food mixer | DeoDap | 8 | not yet | 0 | 0 | 19 | Too quiet |
| 13 | Double-sided dishwashing sponge brush | DeoDap | 8 | not yet | 0 | 0 | 19 | Too quiet |
| 14 | Wire combination lock | DeoDap | 8 | not yet | 0 | 0 | 19 | Too quiet |
| 15 | Hooked clothesline | DeoDap | 8 | not yet | 0 | 0 | 19 | Too quiet |
| 16 | Peel-and-stick marble-look wallpaper | DeoDap | 6 | not yet | 4 | 0 | 17 | Too quiet |
| 17 | Memory foam kitchen standing mat | Facebook | 7 | not yet | 0 | 15 / 0 / 0 | 16 | Too quiet |
| 18 | Round travel kit with bag | TradeIndia | 7 | not yet | 0 | 0 | 16 | Too quiet |
| 19 | Switch icon label stickers | DeoDap | 7 | not yet | 0 | 0 | 16 | Too quiet |
| 20 | Birthday pattern projector night light | DeoDap | 7 | not yet | 0 | 0 | 16 | Too quiet |
| 21 | Transparent table protector | Facebook | 7 | High | 0 | 44 / 0 / 0 | 16 | Too quiet |
| 22 | Home decor item (too vague) | TradeIndia | 2 | not yet | 13 | 6 confirmed by an older method | 16 | none |
| 23 | Split AC dust cover | DeoDap | 6 | not yet | 0 | 0 | 13 | Too quiet |
| 24 | Liquid soap dispenser | DeoDap | 6 | not yet | 0 | 12 / 0 / 0 | 13 | Too quiet |
| 25 | Duvet cover bedding set | Facebook | 6 | not yet | 0 | 3 / 0 / 0 | 13 | Too quiet |
| 26 | Dining table runner | Facebook | 6 | not yet | 0 | 1 / 0 / 0 | 13 | Too quiet |
| 27 | Soft hanging cot mobile | Facebook | 5 | High | 2 | 4 / 4 / 3 | 12 | Too quiet |
| 28 | Storage container | TradeIndia | 5 | High | 1 | 26 / 0 / 0 | 11 | Too quiet |
| 29 | Baby lotion | TradeIndia | 5 | High | 1 | 30 / 0 / 0 | 11 | Too quiet |
| 30 | Freestanding three-shelf kitchen organiser | TradeIndia | 5 | not yet | 0 | 0 | 10 | Too quiet |
| 31 | Baby Buddha decorative figure set | DeoDap | 5 | not yet | 0 | 0 | 10 | Too quiet |
| 32 | Bird wall hanging | Facebook | 4 | High | 0 | 27 / 0 / 0 | 0 | Too quiet |
| 33 | Baby-care kit | TradeIndia | 3 | High | 0 | 29 / 0 / 0 | 0 | Too quiet |
| 34 | Butterfly wing pillow | Facebook | 4 | not yet | 0 | 30 / 0 / 0 | 0 | Too quiet |

What the table shows: 18 of the 34 had Facebook ads saved, but only 3 had any of them reviewed by the AI (the duster, the cot mobile, and the home decor item by an older method); 29 of the 34 had matching DeoDap products saved; many with 8–10 "yes" answers sat at "too quiet" with nothing reviewed. Under your direction, all of these would have been ranked, and several would have reached a report.

---

## Part D. Why the faults happened

### D.1 Why there are faults like these at all (the honest answer)

1. **The program was built to never claim anything it had not proven.** So whenever information was missing, the "safe" choice in the code was to hold the product back. "Not looked at yet" was treated exactly like "looked and found nothing". That one habit sits behind most of the faults below.
2. **It was built as a long chain of separate desks, each with its own queue and its own caps.** Each desk was tested on its own with made-up data. The whole chain was only watched end to end on real data on 16 October, and that is when the knock-on effects showed.
3. **New rules were stacked on old ones over several building rounds** without stepping back to ask whether the whole still made sense. That is my responsibility: I optimised for proof and safety instead of for a steady flow of suggestions.

### D.2 The faults one by one

**1. The extendable duster's report would say "Supplier search did not produce any usable results".**
- DeoDap sells 5 matching dusters. All 5 are in stock, all 5 have a wholesale price, and all 5 are on DeoDap's best-seller list. The program saved them as "platform leads" at 08:46.
- But the report's supplier section reads only one drawer: the AI's readings of supplier web pages. The platform leads sit in a different drawer that the report never opens.
- The AI's supplier readings all failed the code's format checks: 23 of 24 that day, including all 5 of the duster's. The AI had quoted a company's name and address as two quotes, and the code wants one quote holding both; or it filled in a field the code does not allow in that case.
- With the AI drawer empty, a fixed sentence in the code prints "Supplier search did not produce any usable results".
- *New design:* the store listing itself is shown as the supplier (name, wholesale price, link); supplier web pages are no longer read by the AI.

**2. The revolving spice rack was called "too quiet" (correction: its Facebook phrase had 6 words, not 5).**
- The AI step that writes search phrases gave exactly one Facebook phrase: "revolving airtight spice rack wide mouth". Facebook said "No ads match your search".
- The same AI answer also held the shorter names "revolving spice rack" and "spice rack", but the code only uses those as spare phrases for profiles made the older way. The rule "try the next phrase" needs a next phrase to exist, and there was none.
- No Facebook ads meant 0 "want" points, so it was blocked as "too quiet", although its score of 44 was the day's highest (mostly from its Low crowding; C.9). Its Low crowding was itself shaky: 44 of its 60 Amazon/Flipkart listings were judged "same family" but "only this seller's offer" and not counted; counting them would have made it High.
- *New design:* every product gets a short phrase (2–3 words) first and a longer one only if the short one brings unrelated ads; and no product is ever blocked for having no ads.

**3. The baby lotion and the black storage container were called "too quiet" without anyone looking.**
- The baby lotion had 30 Facebook ads and a Google Trends chart saved; the storage container had 26 ads and a chart. The AI reviewed none of them.
- Why: the AI's "is this the same product?" review goes through each product's saved results **in alphabetical order of the website's internal name**. Amazon best-sellers, Amazon, BaapStore, Blinkit, DeoDap and Flipkart come first; Google Trends is 7th and Facebook ("meta-ads") 9th of 10. Each pass reviews only one website per product, and only 5 per round. The lotion's 140 Amazon/Flipkart/BaapStore rows were reviewed first; its Facebook ads were still waiting when research stopped. On the whole day, only 2 of 89 reviews were Facebook and 0 were Trends.
- Unreviewed results count as zero "want" points, and the "too quiet" label is checked before anything else. So both were labelled "too quiet".
- Both were also "High" crowded (an Amazon best-seller match). If a review had confirmed enough of their ads to reach 5 "want" points (3 or more advertisers, or 5 or more running ads), they would have shown as "Proven but crowded"; "too quiet" hid that.
- The baby lotion was also searched twice on 7 websites, because "Baby Lotion" and "baby lotion" (capital letters only) were treated as two different searches, and old searches are never withdrawn.
- *New design:* unknown counts as unknown; Facebook and Trends are checked first, not last; one search per phrase regardless of capital letters.

**4. 226 of the 354 collected listings were never read by the AI** (the full table is in C.5, step 2):
- 156 Facebook ads were still waiting in line: the idea-finding desk gets only 2 of each round's 10 AI uses, each use reads at most 10 items, and a single Facebook search brings in up to 30 ads.
- 23 Facebook ads hit the program's 5-minute "AI pause" at 19:38, and 20 more were turned back directly between 18:28 and 20:35, because the allowance showed "used up" after the 18:09 jump (Part B).
- 2 got an AI answer in the wrong shape.
- 15 store items (10 BaapStore, 5 DeoDap) were **silently marked "done"**: when the AI was briefly unavailable (the allowance meter could not be read at 07:39 and 13:27), the code wrote a "problem" note instead of a reading, and then treated "has a problem note" as "decided". They were never tried again. The same happened to the 10 ads from the Trends "rising" search, and to 4 products at the "new or known?" step.
- Check: 156 + 23 + 20 + 2 + 15 + 10 = 226.
- The label the records use for most of these stops, "AI was switched off for this run", is misleading: nobody switched the AI off. It really means "the AI is paused for 5 minutes because the allowance reading failed or the limit was reached".
- *New design:* the AI work is grouped 10–20 items per call without the "2 of 10" squeeze, and nothing is ever marked done unread.

**5. Google Trends "used up" headings.** Each heading can be checked on Facebook or on Trends. When both are free, a tie-break sorts by website name, and "google-trends" comes before "meta-ads". So the heading went to a Trends chart, which can never create a product idea, and was then counted as "searched".

**6. The reading desk stopped itself early in 13 morning rounds** (07:48–11:04). Its own counting check counted DeoDap/BaapStore look-ups (which are answered from our saved copy, with no website visit) as fresh website visits, saw "too many", and stopped. Fixed by the 11:07 update on 16 October.

**7. The program's own record-keeping ate most of the time.** In round 25 (evening), about 43 of 47 minutes were the program's own bookkeeping; the photo desk alone took 5¾ minutes and saved 0 photos.

---

## Part E. Your new direction: what I understood, where I agree, where a decision is needed

### E.1 Your direction, restated as rules (please correct anything wrong)

1. **We suggest possible winners; we do not prove winners.** Ranking, not pass/fail. Reports keep coming every week for at least **two years**.
2. **Where ideas come from ("discovery"):** mainly the dropshipping stores (DeoDap, BaapStore, Dropdash) and Facebook ads.
3. **How an idea is cross-checked ("validation"):** on 2, 3 or 4 other places, mixed and matched: Google Trends, Facebook ads (also a discovery source), Amazon, Flipkart. Being on 2–3 of the places is good enough. Missing a place never removes a product.
4. **Google Trends:** a rising chart ranks highest, a steady chart lower, a falling chart lowest; no chart at all ranks about the same as a falling one. Trends confirms; it does not decide.
5. **Things that push a product down, but never remove it:** heavy selling on Amazon/Flipkart ("crowded"); several quick-delivery apps (Blinkit, Zepto, Instamart) already selling it. One quick-delivery app is not a big deal; several are a minus.
6. **Things that remove a product:** a big brand; clearly outside the four categories (loosely related is fine); clearly failing the 10-question checklist; not present anywhere at all. You expect only about 10% to be removed.
7. **The checklist score is kept for every product** (1 to 10 "yes" answers). Products with 5+ are the main picks; products with 4 or fewer can still appear as "other suggestions", without saying they scored lower. Example: of 15, 10 with 5+ and 5 with 4 or fewer.
8. **Reports mix ranks:** some from the top, some from the middle, a few from lower down, above a cut-off.
9. **Suppliers are a bonus, not a requirement.** Products with a supplier get some priority. Supplier search can be loose: by the kind of product, not the exact item.
10. **The demand gate goes** (earlier step 10: "at least 5 'want' points or blocked"). Facebook and Trends still add points; they just never block.
11. **Facebook ads:** one or two ads are enough; no "3 advertisers / 5 running ads" rule.
12. **The faults found** (duster suppliers, spice-rack phrase, baby lotion and storage container) must be fixed.
13. **AI:** watch every token (read, written, thinking), find what is not watched, and spend less.
14. **Sign-ins:** nothing should depend on you except IndiaMART's one-time code, with a desktop alert and a mute button.

### E.2 Where I fully agree

- **The contradiction you spotted is real.** Today the program marks products down for being crowded *and* holds back products that are not yet visibly popular. A product cannot be both. The new score makes a hill instead of a wall: **nothing anywhere → low; some presence in 2–4 places → high; everywhere with heavy sales → lower again.**
- **Unknown is not "bad".** "Not looked at yet" must never count as "looked and found nothing". That single mistake caused the baby lotion, storage container and most of the 26 "zero demand" products.
- **Dropshipping stores are the best starting point.** They are free to read (no website visit per product, no AI) and they carry the stores' own sales hints: best-seller lists, "most bought this month", state best-seller lists, DeoDap's "High Performing" tag and Dropdash's "units sold". On 16 October, DeoDap's lists turned 13 of 15 items read into product types, the best rate of any source.
- **Finish one product before starting the next.** Today every product waits in a queue at every desk, so many products end the day half-checked. Each product should go through its 2–4 checks in one go (a few minutes), then get its score. The AI still takes several items in one call **whenever several are ready**, because that is where grouping saves money, but nothing ever waits for a group to fill.

### E.3 Where a decision or a small correction is needed

1. **"Not present anywhere" as a removal rule.** Every idea starts from a real listing (a store item or an ad), so it is always present at least where it was found. My suggestion: no removal; "found only where it started" simply ranks low (question 2).
2. **What "clearly failing" the checklist means.** Suggestion: removed only with 2 or fewer "yes" **and** 5 or more "no"; 3–4 "yes" = "other suggestions" pool; 5+ = main pool. If many answers are "unclear", the questions are asked again first, because unclear is unknown, not bad (question 3). On 16 October this would have removed none of the 34: the two lowest (2 and 3 "yes") both had 7 "unclear" answers. Three (two with 4 "yes", one with 3) would have gone to the "other suggestions" pool.
3. **Your "about 10% removed" against 16 October.** Brand and category alone removed **19 of 78 products (24%)**: 14 outside the four categories (all of them from Facebook ads) and 5 big brands. "Loosely related stays" should bring this down. There is also a fixed list of **excluded kinds** in the settings, which you have not been asked about yet: jewellery, adult cosmetics and beauty, perfume, clothing, stationery, phone cases and screen guards, chargers and cables, earphones and headphones, smartwatch straps, and pets and aquariums. Baby skin care (lotions, creams, oils) is not on it; the suggestion is to keep it, with the safety note (questions 2 and 28).
4. **Amazon "performance" and Amazon "crowding" are the same evidence read two ways.** The only performance signs Amazon shows are ratings, stars, "1K+ bought last month" and best-seller badges, and those are exactly what "crowded" is measured with. Suggestion: a few matching listings = a small plus ("people already buy it online"); heavy selling = a minus; nothing = no points (question 7).
5. **Quick-delivery checks cost money.** Each check costs 1 *quick-delivery credit* per app per city (not the same thing as the Codex credits in Part B), and now only ever gives a minus. The 34 quick-delivery credits left (they expire on 2 November) cover about 11 products in one city across the 3 apps, less than one report. Suggestion: check only the products about to go into a report; if a check pushes a pick down, swap it for the next-ranked product; buy no more for now (question 8).
6. **Stopped Facebook ads.** About 3 in 4 of the ads found while looking for ideas on 16 October (226 of 295) had already stopped running; the cot mobile itself came from a search where 19 of 20 ads had stopped. "Running ads only" would throw away most ideas. Suggestion: search all ads; running ads count fully, stopped ads count half (question 5).
7. **"No Trends chart".** Many small or new products have too few searches for Google to draw a chart, so "no chart = falling" will hold some genuinely new products back. That matches your instruction, and because it is 0 points rather than a minus, it only withholds a lift; it never pushes down (question 6).
8. **Two years needs about 3,100–4,700 different products** (3 reports a week × 10–15 products × 104 weeks), each used once. The supply, line by line (estimates):

   | Source | Different product types over two years |
   |---|---|
   | Today's store stock: DeoDap's about 4,521 in-stock, in-category items (many are versions of the same thing; DeoDap uses 2,372 different labels on its in-stock items) plus BaapStore's about 931 in-category items | about 1,500–2,500 |
   | New store arrivals: DeoDap adds about 600 in-stock items a month (913, 731 and 962 in July–September), about 350 of them in the four categories; if 20–30% are new kinds, that is 70–100 a month for 24 months | about 1,700–2,400 |
   | Facebook ad searches over the 1,706 headings (16 October gave about 0.5 new type per search, measured when only 91 of 292 ads were read, so this is a floor) | about 850 or more |
   | Dropdash | unknown until it is read |
   | **Total** | **about 4,000–5,750** |

   Enough to use each once, but tight if we want to pick mostly from the better half. The first week measures the real rate. Reserves: Dropdash, Amazon/Flipkart new-release lists and, much later, re-suggesting older products with a new angle (question 13).
9. **The 15-word TradeIndia/IndiaMART "new listings" search**, fairly: it gave 9 of the 34 types on 16 October, including the spice rack (the day's top score), the baby lotion and the storage container. But 4 of its 9 were weak or stuck (a too-vague "decor item", a baby-care kit with 3 "yes", and two travel kits whose definitions failed), and only 1 of the 9 had an accepted supplier lead that day. Suggestion: keep it as a small side source, with one word per sub-area of the product list (55) instead of 15 hand-picked words (question 12).

### E.4 A first draft of the points (for ranking only; question 9)

Every product that is not removed gets points. Only the order matters, so the exact numbers will be tuned after two weeks of real results. Rules: **unknown earns nothing and loses nothing**; a fact is counted once.

| Group | Signal | Points |
|---|---|---|
| **Free signals** (no website visit, no AI) | Sold by 1 dropshipping store / by 2 or more | +8 / +12 |
| | On a store's best-seller, trending, "most bought" or state best-seller list, or Dropdash "1K+ units sold" | +10 |
| | New in a store in the last 60 days | +5 |
| **Product appeal** | Checklist: each "yes" | +2.5 (up to +25) |
| **Cross-checks** (2–4 website searches) | Facebook: 1 matching ad / 2 or more advertisers (stopped ads count half) | +8 / +12 |
| | Facebook: an ad that started in the last 30 days | +4 |
| | Google Trends: rising / steady / falling / no chart | +15 / +8 / 0 / 0 |
| | Amazon or Flipkart: a few matching listings, not heavily sold | +5 |
| **Push-downs** (never removal) | Amazon/Flipkart crowding: medium / high | −5 / −15 |
| | Quick-delivery: 1 app / 2 apps / 3 apps or many listings | 0 / −5 / −10 |
| **Bonus** | A supplier found *outside* the dropshipping stores (IndiaMART or TradeIndia), since a store listing already counts above | +5 |

Balance to be aware of: the free store signals plus the checklist can give up to about 52 of the roughly 93 possible points, so the order will lean on store lists and the AI's checklist until the cross-checks come in.

**Three worked examples, using only what was confirmed on 16 October** (unknown = 0), with what is still to check:

| Signal | Extendable duster | Water bottle with temperature display | Baby lotion |
|---|---|---|---|
| Stores | DeoDap (5 confirmed matches): +8 | DeoDap (its own source listing): +8 | BaapStore (1 confirmed match): +8 |
| Store best-seller list | Yes: +10 | Yes (it came from DeoDap's lists): +10 | No: 0 |
| Checklist | 7 "yes": +17.5 | 9 "yes": +22.5 | 5 "yes": +12.5 |
| Facebook | 10 advertisers: +12; 7 started in the last 30 days: +4 | 6 ads saved, not reviewed: 0 | 30 ads saved, not reviewed: 0 |
| Google Trends | Not read: 0 | Not read: 0 | Chart saved, not reviewed: 0 |
| Amazon/Flipkart | High (4 matching listings with 1,000+ ratings, the largest 22,607): −15 | Saved, not reviewed: 0 | High (a best-seller list match): −15 |
| Supplier outside the stores | None accepted: 0 | None: 0 | Its own TradeIndia supplier listing: +5 |
| **Known now** | **36.5** | **40.5** | **10.5** |
| Still to check | Trends (0 to +15) | Second store (+4), Facebook (0 to +16), Trends (0 to +15), Amazon/Flipkart (−15 to +5) | DeoDap (+4), Facebook (0 to +16), Trends (0 to +15) |
| Today's rules said | Report-ready (the only one) | "Too quiet", blocked | "Too quiet", blocked |

So even on what is known today, the water bottle (whose evidence nobody looked at on 16 October) ranks above the duster, and the baby lotion is in the list rather than blocked; its Facebook ads and Trends chart could still lift it a long way.

### E.5 How a product would travel in the new design

1. **Collect.** Every day, read the stores' new arrivals and lists (free), and run a few Facebook ad searches from the 1,706 headings (all ads; stopped ones count half).
2. **Sort out the obvious for free.** Code drops the excluded kinds (E.3 point 3), clear out-of-category items and near-identical titles.
3. **Name, group and checklist in one AI call per 10–20 items.** The AI says what each item is, which known product type it belongs to (or that it is new), the category, the 10 checklist answers, and whether it is a big brand.
4. **Remove only by the hard rules** (question 2).
5. **Free score** for every product type: store signals + checklist. This orders the queue.
6. **Cross-check the next product in the queue, start to finish:** 2–4 searches (Facebook if it did not come from Facebook, Google Trends, Amazon, Flipkart). One AI call then confirms which results are the same product; if other products are ready at the same moment they share the call, but nothing waits. About 1 in 5 checks goes to a lower-ranked product, so new things also get a chance.
7. **Final score and rank**, shown to you as "more likely / likely / less likely".
8. **The evening before each report:** pick 10 main products (5 from the top third, 3 from the middle third, 2 from the lower third, never from the bottom fifth) and up to 5 "other suggestions"; balance the categories; avoid near-duplicates; check quick-delivery for the picked ones only and swap any that it pushes down; look up IndiaMART suppliers by product type for the picked ones; take the photo from a listing; the AI writes the text.

### E.6 Today versus the new design

| | Today | New design |
|---|---|---|
| Decision | A chain of pass/fail rules (in one category and not a brand; a definition; 5 checklist "yes"; a crowding answer; 5 "want" points; a supplier search; and more) | Points and a rank; only the hard rules remove |
| Order of work | Every desk touches every product a little each round | One product is finished, then the next |
| Websites per product | One search on every website in the list (Facebook, Trends, Amazon, Flipkart, Amazon best-sellers, three quick-delivery apps, four supplier sites), plus the store look-ups | 2–4 searches, more only for picks |
| AI per product | Many single-item calls (five separate naming steps, listing-by-listing reviews) | Grouped whenever several are ready: 10–20 items per naming call |
| Facebook | Needs 3+ advertisers or 5+ running ads for full marks | 1 ad counts |
| Demand | At least 5 "want" points, or blocked | No gate; Facebook and Trends just add points |
| Quick-delivery | Counted as demand (a plus) | Only a minus, only for picks |
| Supplier | Required before a report | A small bonus |
| Checklist | Under 5 "yes" = rejected | 5+ main, 3–4 "other suggestions", removed only if clearly failing |
| "Not looked at yet" | Counted as "nothing found" | Counted as unknown: no plus, no minus |

---

## Part F. Sign-ins: keeping your part as small as possible

| Site | What happens in the new design | Your part |
|---|---|---|
| **Dropdash** | The program signs itself in, using the phone number and password kept in **Windows Credential Manager** (Windows' own encrypted password safe). It visits lightly every 2–4 hours so the session does not expire, and signs back in if it ever does (at most once every 6 hours). The agent never sees the password | Once: type the phone number and password into a Windows box (question 19), then change the Dropdash password (question 21). Only if Dropdash ever asks for a one-time code: the alert below |
| **Google (for Trends)** | Stays signed in for months. Google's own sign-in cannot be safely automated (it asks for phone confirmation and blocks robots) | Only if it ever signs out: the alert below |
| **IndiaMART** | Needs a one-time code sent to your phone, so it cannot be automated | Respond to the alert |
| **DeoDap, BaapStore** | Public, no sign-in | Nothing |

So, honestly: after a one-time Dropdash setup, **only IndiaMART needs you as a matter of routine**; Dropdash and Google need you only in the rare case that they ask for a one-time code or sign out.

**The alert** (question 22):
1. When IndiaMART (or, rarely, Google or Dropdash) needs you, a Windows notification pops up from the NeuraFlow Dropshipper icon near the clock, with a short chime: "IndiaMART needs you to sign in (one-time code to your phone). Research continues with the other sites."
2. It repeats every **20 minutes** (you can pick 15) until you sign in or mute it.
3. Its choices: **Sign in now** (opens the sign-in window on that site's tab only), **Snooze 1 hour**, **Mute reminders for this site** (until you unmute). While muted, the NeuraFlow window shows "IndiaMART reminders are muted — Unmute".
4. While IndiaMART waits, only IndiaMART supplier look-ups wait; everything else carries on.

**One more fix** (question 23): each signed-in site gets its own browser profile, so an open sign-in window for one site no longer stops the others (on 16 October an open sign-in window stopped every signed-in site for 5.5 hours), plus a reminder if the sign-in window stays open for more than 10 minutes.

---

## Part G. Questions for the owner

The same list, with answer lines, is in `docs/project/handoff.md`. Questions marked **(needs your answer)** decide the design; any other question left unanswered takes the recommendation.

**A. How products are found, checked and ranked**

1. **(needs your answer) The overall flow:** ideas come mainly from the dropshipping stores and Facebook ads; each is cross-checked on 2–4 places (Facebook ads, Google Trends, Amazon, Flipkart); everything is ranked by points; only the hard rules in question 2 remove anything. *Recommendation: yes.*
2. **(needs your answer) Hard removals only:** big brands; clearly outside the four categories (loosely related stays); the excluded kinds in the settings (jewellery, adult cosmetics and beauty, perfume, clothing, stationery, phone cases and screen guards, chargers and cables, earphones and headphones, smartwatch straps, pets and aquariums); clearly failing the checklist (question 3); already sent to the client; exact duplicates. Baby skin care stays, with the safety note. "Found only where it started" ranks low but is not removed. *Recommendation: yes.*
3. **(needs your answer) Checklist tiers:** 5+ "yes" = main picks; 3–4 = "other suggestions"; removed only with 2 or fewer "yes" and 5 or more "no"; many "unclear" answers are asked again first. *Recommendation: yes.*
4. **(needs your answer) Report make-up (15 products):** 10 main picks (5 from the top third of the ranking, 3 from the middle third, 2 from the lower third, never from the bottom fifth) plus up to 5 "other suggestions", shown under a heading such as "More ideas worth a look", with no scores; categories balanced; no near-duplicates in one report. *Recommendation: yes.*
5. **Facebook ads:** search all ads; 1 matching ad counts; 2 or more advertisers count more; an ad that started in the last 30 days adds a little; stopped ads count half. *Recommendation: yes.*
6. **Google Trends:** rising (the last 3 months at least 15% above the 3 months before) scores most; steady (within 15%) scores less; falling and "no chart" score nothing. *Recommendation: yes.*
7. **Amazon/Flipkart:** a few matching listings = a small plus; heavy selling (a best-seller badge, or 3+ matching listings with 1,000+ ratings, or 2+ listings with "1K+ bought last month") = a minus; nothing found = no points. *Recommendation: yes.*
8. **Quick-delivery apps:** checked only for products about to go into a report; 1 app = no change, 2 apps = small minus, 3 apps or many listings = bigger minus; a pick that gets pushed down is swapped for the next-ranked product; use the 34 quick-delivery credits left (about 11 products in one city; they expire on 2 November) and buy no more for now. *Recommendation: yes.*
9. **(needs your answer) The first draft of points** in Part E.4, tuned after two weeks of real results. *Recommendation: yes.*
10. **Order of work:** finish each product's checks in one go instead of rounds that touch every product a little; the AI groups items whenever several are ready, never waiting. *Recommendation: yes.*
11. **Suppliers:** a small bonus, never a requirement; for the picked products, IndiaMART look-up by kind of product plus the dropshipping store's own listing. *Recommendation: yes.*
12. **Side idea sources:** keep the TradeIndia/IndiaMART "new listings" search as a small side source, with one word per sub-area of the product list (55) instead of 15 hand-picked words; do not use Amazon/Flipkart best-seller lists as idea sources (they are crowded by nature), only for cross-checks. *Recommendation: yes to both.*
13. **Two-year supply:** never repeat a product in the first 12 months; in month 9, check the remaining supply and decide whether older products may return with a new angle. *Recommendation: yes.*

**B. AI use**

14. **Record every kind of token** (read, re-used, cache-written, written, thinking), the model and thinking level, and the allowance before and after, for every research AI call; show a daily "research used X% of your weekly allowance" line. *Recommendation: yes.*
15. **Slim AI calls** (own short instructions, your skills list hidden from research calls, unused features off): about 8,000 fewer tokens per call, to be re-tested before relying on it. *Recommendation: yes.*
16. **(needs your answer) Who comes first when your own Codex work and research compete for the shared allowance:** your work. Research pauses its AI whenever your own work is filling the 5-hour window (above 50%), keeps to a daily share of the weekly allowance that is set after measuring the first hours of the new design, and never uses paid credits. *Recommendation: yes, your work first.*
17. **A daily "who used the AI" line** that also counts your other Codex projects, by reading only the numbers in Codex's own log files on this computer. *Recommendation: yes.*
18. **Your paid Codex credits and your plan** (your decision; nothing for the agent to do):
    - Your other projects used about 1,340 paid Codex credits after the plan limit was reached on 16 October (about 44 more are not visible in this computer's logs). If you do not want that, turn off "use credits when the limit is reached": in ChatGPT, click your profile picture, open **Settings**, and look for the Codex usage or credits section (the exact names on that page may differ).
    - On the same page, check whether your plan or a special offer changed at about 11:39 pm India time on 16 October, when the new 5-hour limit appeared and the weekly figure jumped to 100%.

**C. Sign-ins**

19. **(needs your answer) Automatic Dropdash sign-in** with Windows Credential Manager (you type the phone number and password once into a Windows box; the program signs back in when needed; an alert only if Dropdash asks for a one-time code). *Recommendation: yes.*
20. **(needs your answer) Today's Dropdash list.** Choose one:
    - (a) You sign in once now, and the next agent reads the full list (no password involved). Steps: 1. Open the **NeuraFlow Dropshipper** window. 2. Click **Sign in to research sites**. 3. In the Chrome window that opens, click the **Dropdash** tab. 4. Type your 10-digit mobile number and password and click **Sign in**. 5. Check that products appear. 6. Close that whole Chrome window (while it is open, the other signed-in sites wait). 7. Tell the next agent "Dropdash is signed in". A Dropdash sign-in seems to last about a day, so do it shortly before starting the new chat.
    - (b) You add a Claude Code permission rule so the agent can sign in.
    - (c) Wait for question 19's build.

    *Recommendation: (a).*
21. **Change the Dropdash password** after question 19 is set up (it has now been pasted into chat twice), and delete `dropdash.txt` from the project folder if it holds the password. *Recommendation: yes.*
22. **The alert:** a notification with a chime every 20 minutes while IndiaMART (or, rarely, Google or Dropdash) needs you; "Sign in now", "Snooze 1 hour", "Mute reminders for this site"; research carries on meanwhile. *Recommendation: yes, every 20 minutes.*
23. **Separate browser profiles per signed-in site**, plus a reminder if the sign-in window stays open more than 10 minutes. *Recommendation: yes.*

**D. Carried over from 16 October**

24. **Keep research stopped** until the new design is built and loaded. *Recommendation: yes.*
25. **Start research records fresh** when the new design starts, keeping the downloaded store catalogues. *Recommendation: yes.*
26. **Security:** replace the private website's draft-upload key (a helper displayed it in its own notes on 16 October); delete the 3 saved Dropdash pages that contain your first name. *Recommendation: yes to both.*
27. **Private website checks:** simplify to "you approved it, and these are the approved files". *Recommendation: yes.*
28. **Report wording:** plain suggestions; a short "why it's a good pick" line written from saved facts; a "Client Compliance Check Required" note on baby, food, skin, medical and mains-electrical products. *Recommendation: yes.*
29. **Report look:** two quick mock-ups of a simplified product page to choose from. *Recommendation: yes.*

The 16 October questions not listed here are replaced by the questions above.
