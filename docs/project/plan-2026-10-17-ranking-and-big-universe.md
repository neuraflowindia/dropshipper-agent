# Plan R18 — rank, don't gate; a product universe for 10+ months; see every step

**Project stage:** Development and testing (confirmed 13 October 2026). No client has received a report, so this plan changes things directly. No staged rollouts, and no keeping old and new side by side.

**Status:** proposal, written 16–17 October 2026 after the owner's regroup message, and reviewed once by a separate checker. **Not approved yet.** Only the parts the owner asked for directly are built, on separate copies (section 5.1): the read-only tracing tool and the AI accounting. The questions are in section 11 and in the handoff.

**Read first:** [How the research program really works](research-wiring-explained-2026-10-16.md). It is the step-by-step audit this plan is built on; every number below comes from it unless marked "estimate".

---

## 1. What the owner asked for (16 October, in short)

1. **Probability, not proof.** Look for products with a decent chance. No pass/fail, just "more likely" and "less likely". Rank everything; pick from the top first, then the next ranks.
2. **Cross-site presence is the main signal.** Out of about 10 sites, a product seen on 4 (sometimes 3) is good enough to go forward.
3. **Don't burn resources.** Light, cursory checks; never exhaust allowances quickly.
4. **A huge, ever-growing universe** of ideas (broad types, narrow types, products) that lasts **at least 10 months** and keeps fetching new things.
5. **Output:** 10–15 products per report, 3 reports a week (12+ reports and 120–180 products a month). Ideally a report's products are ready in a couple of hours.
6. **Report wording:** good product suggestions. No proof, guarantee or caveat language, in the report or in how the program thinks.
7. **Full tracing:** see how the program works at every step, how many items pass each point and why, and exactly how much AI each step uses (tokens and time). Build it where it is missing.
8. **Dropdash:** sign in with the details given, and stop being signed out (gentle pacing, maybe separate browsers).

## 2. What the audit found, in one paragraph

The machinery is complete and runs, but it is a chain of strict gates. **1 of 34 product types became report-ready on 16 October; no report was made.** The rest were held by:
- crowding checks waiting for the AI (23 types);
- a demand rule that counts unreviewed evidence as none (26 of 34 types had 0 "want" points);
- a checklist re-asked every time its wording changed.

The AI is used one small item at a time, so about 72% of its input is the same fixed instructions repeated. Supplier work takes 45% of website attempts, yet gives almost no leads, because the AI's supplier answers are rejected by a formatting rule. The best idea source (platform best-seller lists: 13 types from 15 items) is capped at 30 items a day, with 839 waiting. Rounds slow down as the history grows (1.7 s → 41 s of the program's own record-keeping per website read).

## 3. The new design

### 3.1 The universe: one big, growing idea list

The owner's three levels map like this:
- **broad type** = a heading in the product map;
- **narrow type** = a product type (what reports recommend);
- **product** = a real listing, shown as an example.

**Where ideas come from:**
- **Every in-scope item in the platform catalogues.** Today that is about **5,450 in-stock items**: DeoDap 4,521 and BaapStore 931, including the 839 queued best-seller items. Dropdash's 21 categories will add more once it is signed in. Catalogues are refreshed weekly and new arrivals daily.
- **The product map's 1,706 headings**, searched on Meta for **active** ads only. 1,674 have never been used.
- **Amazon and Flipkart best-seller and new-release pages** per category (question 4).
- **IndiaMART and TradeIndia** category listings.
- **Google Trends "rising" searches.**
- **The AI's map top-ups.**

**How items become product types, cheaply:**
1. **Code first (free):**
   - drop items outside the four categories, and the excluded kinds (pets, jewellery, clothing, cosmetics, phone accessories);
   - drop big brands where the catalogue says so;
   - fold together near-identical titles;
   - note each item's free signals: best-seller, trending, "most bought" or new list; number of state lists; DeoDap's "High Performing" tag; Dropdash "units sold"; in stock; price.
2. **AI in groups of 10 (the most the code allows per use today).** One AI use, for 10 items at once:
   - names each item's product type;
   - says which known type it belongs to, if any;
   - fills the 10-question checklist;
   - sets the category;
   - raises the compliance flag.

   This replaces today's five separate AI steps (read, new-or-known, define, checklist, duplicate check). Larger groups only after measuring that answers stay good and finish within the AI time limit (180 seconds).
3. **Best first.** Items with the strongest free signals are named first, so the top of the list fills up first.

**How much is needed (estimate):**
- 36 products a week × about 43 weeks ≈ 1,550 report places. "Only new products" at first (section 3.5) means about **1,550 distinct types**.
- Picking from the top third means ranking about **4,500 types** over 10 months (about 100 a week).
- **The platform catalogues alone probably hold only 1,500–2,500 distinct types**: many items are versions of the same thing, and DeoDap's own product-type labels give about 1,660 distinct labels.
- The rest must come from:
  - Meta heading searches (about 0.5 new type per search on 16 October → roughly 800 from the unused headings);
  - Amazon/Flipkart lists, Dropdash, IndiaMART/TradeIndia;
  - weekly new arrivals (DeoDap had 245 "just arrived");
  - AI map top-ups.
- **Week 1 measures the real rate.** The window will show "weeks of ideas left" from it. If supply falls short, more sources are added before it runs out.

### 3.2 Presence: "seen on N of 10 sites"

**The ten sites:**
1. Meta ads
2. Amazon
3. Flipkart
4. DeoDap
5. BaapStore
6. Dropdash
7. Quick-delivery apps (Blinkit, Zepto and Instamart as one)
8. Google Trends
9. IndiaMART
10. TradeIndia

**What "present" means for each site** (proposal; question 3). This needs a clear bar, because Amazon, Flipkart, IndiaMART and Trends show almost every ordinary product:

| Site | Present when… |
|---|---|
| Meta | At least 1 advertiser with an active ad for it that started in the last 30 days |
| Amazon / Flipkart | At least 3 matching listings |
| DeoDap / BaapStore / Dropdash | At least 1 matching item in stock |
| Quick-delivery | In stock in at least 1 city (only checked for top picks) |
| Google Trends | A "rising" result, or at least 4 weeks with searches in the last 12 months |
| IndiaMART / TradeIndia | At least 3 different sellers |

**How it is checked:**
- The three dropshipping platforms: by title words against their saved catalogues, so no website visit.
- The other sites: one search each, only for types near the top of the list (section 3.4).
- Code decides first, by word matching. The AI confirms in one use per 5 types, looking only at the best matches.
- Exact and "same family" matches count. Today the "example offer" ones are thrown away.

**Before the weights are fixed,** the rule is tested on the 34 types from 16 October, to see whether it actually separates them.

### 3.3 The likelihood rank (replaces gates, labels and the 7-day expiry)

**One number per type, from 0 to 100.** The weights are a proposal (question 2):

| Part | Weight | What feeds it |
|---|---|---|
| Presence | 35 | How many of the 10 sites show it. 3–5 sites is the sweet spot; 6+ with very heavy reviews counts slightly less, because it is already saturated |
| Buzz | 25 | Best-seller, trending or "most bought" lists; units sold; active advertisers; recent advertisers; rising searches |
| Product appeal | 25 | The checklist: Meets out of 10, with Unclear counted as half |
| Room | 15 | Fewer big sellers on Amazon/Flipkart means more room. A small adjustment, not a gate |

**Rules:**
- **Unknown is neither good nor bad.** It simply earns nothing yet. The window says "not looked at yet", never "too quiet".
- **Three bands**, for the program and the owner only: more likely, likely, less likely. **Nothing is ever rejected for a low number**; it just sits lower in the list.
- **Hard exclusions stay.** These are business rules, not quality judgments:
  - outside the four categories;
  - well-known brands;
  - excluded kinds, including pets;
  - duplicates;
  - already sent to the client.
- **Older facts fade slowly** instead of expiring after 7 days.
- **Duplicates across thousands of types:** code compares names, uses and mechanisms across the whole list. The AI checks a pair only when two top picks for the same week look alike.

### 3.4 Spend little: four levels of effort

| Level | Who gets it | Cost | What it does |
|---|---|---|---|
| 0 | Every item in the universe | Free (code only) | Category, exclusions, folding near-copies, catalogue signals, first rough rank |
| 1 | The best ~100 new items a day | 10 AI uses a day | Name, type, checklist, category, compliance flag; platform presence by title matching |
| 2 | The top ~30 types per report (~90 a week) | About 4–6 website searches each, plus 1 AI use per 5 types | Meta (active ads), Amazon, Flipkart, Trends (when signed in), IndiaMART and TradeIndia (browser only); AI confirms the best matches; rank updated |
| 3 | The 10–15 picked for a report | No extra searches | Supplier leads = the platform listing(s) plus the IndiaMART results already read (supplier name, city, price, minimum order, shown as they are); photo from the listing; AI writes the text |

**Stopped, and their code removed in the build:**
- company-page reading and AI supplier-page reading;
- Google Maps supplier search;
- the TradeIndia plain-read-first step;
- per-type Amazon best-seller list reviews;
- listing-by-listing AI review of every saved result;
- AI duplicate checks on every type;
- the deeper Meta look through Apify;
- automatic quick-delivery checks (optional for top picks; question 9).

### 3.5 Reports

- 10–15 products per issue: filled to 10 first, then up to 15 when plenty are ready (as the code does today). Issues go out Tuesday, Thursday and Saturday at 9:00 am India time. The first two are in English and Hinglish.
- **Only new products at first.** "Updates" (re-sending a product with something new) come back later with their own review step.
- **Wording:** "good product suggestions" in plain positive language. All the code-written proof and disclaimer phrases are removed:
  - "no sales guarantee";
  - "not sales proof";
  - "zero is not proof…";
  - "unit and price basis not verified";
  - the long supplier sentences;
  - the cover notice.

  The AI still never invents numbers and never promises profit or sales.
- **Per product:**
  - photo and name;
  - a short **"why it's a good pick" line written by code** from saved facts, for example "On DeoDap's best-seller list · 10 advertisers on Facebook · sold on Amazon and Flipkart" (code, because the AI's text checks refuse numbers);
  - price points;
  - where to source it;
  - checklist highlights;
  - the "Client Compliance Check Required" note on baby, food, skin, medical and mains-electrical products (question 5).
- **Look:** keep the 16 October redesign's engine (fonts, PDF and Excel styling) and simplify the product page to the suggestion layout. Two quick mock-ups come first (question 6).
- **Missing pieces to build:**
  - an owner **Approve** button (none exists today);
  - the language choice after issue 2;
  - putting each issue together the evening before (today one Monday plan reserves all three issues of the week).
- **The private website:** today it re-checks every AI step behind a report (definition, checklist wording, gates, the 7-day rule, the High rule, supplier, photo and website proofs, the new/update ratio). All of that breaks when the gates go. Recommendation (question 14): **simplify it to check the owner's approval and the report files themselves**, rather than rewrite every check.
- **Short week:** with ranking, an issue is short only if the whole list is empty.

### 3.6 Time and cost per report (estimates; measured in week 1)

- **Machine time per issue of 12:** about 30 types × 4–6 searches at about 10–20 s, plus pacing ≈ **1–2 hours**, plus 10–20 minutes for writing and files. Building the universe runs in the background between issues.
- **AI per week: about 3–7 million tokens.** That is 0.4–0.65 million a day building the list, plus 0.45–0.95 million per issue for confirmations and writing.
  - On 16 October about 4.7 million recorded tokens moved the weekly meter by at most 22 points, so this is roughly **15–35% of the weekly allowance**.
  - This is uncertain. The AI's hidden "thinking" tokens are not recorded today, and the jump from 75% to 100% on 16 October is unexplained.
  - The new AI accounting (section 5) measures the allowance before and after every AI use. The daily AI guard is set from week 1's real numbers.
- **Cash:** ₹0 per report. The AI is the existing subscription; Apify is not used; quick-delivery checks are optional (the 34 credits left in the pack bought earlier last until 2 November).
- **±10% accuracy:** only possible after timing the first 3 real issues. The window will then show measured minutes, tokens and allowance per issue.

## 4. Running and resources

- **Research runs all day** (the owner's Start/Stop stays). It builds and refreshes the universe at a gentle pace, and each issue is put together the evening before from the top of the list.
- **AI guard:** research spends at most a set share of the weekly allowance per day, set from week 1's measurements (question 10). It shows the day's use by step in the window and slows down before the limit, not at 100%.
- **Speed:** load the 16 October speed fix (rounds 7–9× faster), and switch the history checkpoints back on (a saved summary that keeps the program fast as records grow).
- **A runway line in the window:** "Ideas ready to rank: N (about W weeks)", from the measured rate.

## 5. Seeing every step (tracing, logging and AI accounting)

### 5.1 Built on 16–17 October, on separate copies (they go live in phase 0)

1. **Tracing tool** (`scripts/research_trace.py`), read-only, with five views:
   - funnel: every step, with counts and reasons;
   - websites: each site's visits, answers, failures, waits and times;
   - AI: each AI step's uses, tokens and minutes, per day;
   - product: one type's journey;
   - summary: all of the above, short.
2. **AI accounting.** Every AI use records:
   - its step name and how many items it carried;
   - tokens in and out, and the AI's own time;
   - the allowance reading before and after (both the 5-hour and the weekly limit).

   A history of every allowance reading is kept. The run records also gain:
   - a line per AI use and per AI wait;
   - a line per dropshipping-platform page visit (including Dropdash);
   - honest counters (one failure counted once; "blocked" only when a website really refused).

   Exact contents are in the handoff once the build finishes.

### 5.2 Still to build

1. A plain page the owner can open from the desktop window ("See research steps"), refreshed after every round. It shows the funnel, today's AI by step (for example "AI today: 6% of the weekly allowance — naming 2%, checks 3%, writing 1%"), each site's day, and the top 20 ranked products with why they rank there.
2. A line for each check that is planned but cannot be created (for example "Dropdash: 32 types waiting for its catalogue").
3. The product's id on every website-visit line, so a product's journey needs no detective work.

## 6. Dropdash and the signed-in sites

1. **Now:** the owner signs in to Dropdash once (5 minutes). Steps are in the handoff.
2. **Fix the two Dropdash faults** (phase 0):
   - a sign-in redirect is recognised as a sign-in wall at once;
   - "signed in" is recorded when the account greeting shows (the name itself is never saved).
3. **Keep the session alive** (phase 0): one light visit to Dropdash's home page every 2–4 hours. The likely cause on 16 October was 9 idle hours. A short test (one light visit an hour, noting when the sign-in page appears) shows how long a session lasts.
4. **Gentle pacing:** at most 1 Dropdash page per round, at least 5 minutes apart, at most 80 a day, with a little random spacing.
5. **Automatic sign-in** (phase 5; needs approval: question 7):
   - **Storing the details.** The owner types the Dropdash phone number and password once into a Windows box. They are kept in **Windows Credential Manager**. That protects them from other Windows users and from a copied disk, but not from other programs running under the owner's own login.
   - **Using them.** They are read only inside the Windows sign-in step, at the moment of typing, and typed through the browser's own typing command. They never enter the program's saved scripts, its requests, the command line, the run records or any file.
   - **What is saved.** Sign-in attempts save no page copy, and later page copies hide the phone number.
   - **Limits.** When Dropdash shows its sign-in page, the program signs itself back in at most once every 6 hours and 3 times a day. On a one-time code, a puzzle or "new device", it stops and shows the owner a line instead.
   - **Rule change.** This changes the rule that the program never types into a website.
   - **IndiaMART** signs in with a one-time code texted to a phone, so it cannot be automated; it stays an owner sign-in.
6. **Separate browser profiles** for Trends, IndiaMART and Dropdash, so one site's trouble never blocks the others. A per-site sign-in button, and a reminder if the sign-in window stays open over 10 minutes (on 16 October it blocked all signed-in sites for 5.5 hours).

## 7. Faults to fix in any case

These come from the audit, section 22. Several disappear with the new design rather than being patched:
- items silently marked "done" when the AI was briefly unavailable;
- Trends charts using up headings;
- old-phrase searches still running after a phrase change;
- alphabetical review order;
- "Too quiet" given before the evidence is looked at;
- "example offer" matches not counted;
- the checklist re-asked for a wording change alone;
- the photo desk rebuilding the whole list each time;
- the supplier AI format rule (removed with supplier page reading);
- the empty compliance flag;
- the dead "advertisers' other ads" source;
- three saved Dropdash pages that still contain the account holder's first name (deleted in phase 0 if the owner agrees: question 13).

## 8. Work order, effort and time (estimates, agent working time)

| Phase | What | Effort | Time |
|---|---|---|---|
| 0 | Load the speed fix, the tracing tool and the AI accounting; switch history checkpoints on; Dropdash fault fixes and keep-alive; owner signs in to Dropdash. **Research stays stopped** (question 12) | Small–medium | ½–1 day |
| 2 | Universe build: free signals, 10-item AI naming + checklist, presence by title matching, presence rules tested on the 34 types, the rank; remove the gates; fresh start of research records that keeps the downloaded catalogues (question 15); restart research | Large | 3–4 days |
| 3 | Level 2 light checks; supplier = platform listing + IndiaMART results; photos from listings; remove the heavy steps | Medium | 1–2 days |
| 4 | Report wording; simplified product page (mock-ups first); Approve button; language choice; evening-before issue assembly; private website simplified to approval + files | Large | 3–4 days + owner's mock-up choice |
| 5 | Dropdash automatic sign-in; separate browser profiles; sign-in window reminder; the owner's status page | Medium | 1–2 days |
| 6 | First two bilingual issues → owner review → private website live | Small | ½ day + owner time |

- **Total:** about **10–14 working days** of agent time (estimate). If the private website's checks were rewritten instead of simplified, it would be about 12–18.
- **First reports** come at the end of phase 4, about **8–11 working days** after approval (estimate). Phase 5 can run alongside phases 3–4.
- **Testing:** focused tests for every change; the whole local test suites before each load (already approved under the Plan R10 approval); at least 90% coverage on new code.
- **The three 16 October copies:**
  - the speed fix is loaded in phase 0;
  - the report design's engine is kept and its product page simplified in phase 4;
  - "market checks first" is dropped, because it makes the evidence gate stricter.
- **AI availability:** the owner reset the weekly AI allowance on the night of 16 October. The program's last saved reading (18:10 UTC) still says "used up until 22 October", and it will update at its first AI use after the restart.

## 9. Approvals

- **Already approved (still applies):**
  - commits and pushes;
  - focused tests and the whole local suites;
  - clean stop/start;
  - Windows app rebuild/reinstall;
  - ordinary research;
  - reading DeoDap, BaapStore, Dropdash (signed in), IndiaMART and Google Trends;
  - spending the existing quick-commerce credits and the free Apify allowance;
  - private website deploy when the first report set is ready.
- **Needed from the owner before building:** questions 1–15 below. Most important: 1 (direction), 2–3 (rank and presence rules), 7 (automatic Dropdash sign-in), 14 (private website) and 15 (fresh start).
- **Always separate:** client publication or access changes; supplier contact; any purchase, paid plan or credit pack; replacing the private-website upload key (question 13).

## 10. Risks

| Risk | What we would do about it |
|---|---|
| Not enough distinct types for 10 months (the platforms give maybe 1,500–2,500) | Measure in week 1; add Amazon/Flipkart lists, more Dropdash pages, IndiaMART/TradeIndia categories and AI top-ups before supply runs low |
| "Seen on N sites" may not separate good from ordinary products | Clear per-site bars (section 3.2), tested on the 34 existing types before the weights are fixed |
| AI use may be higher than estimated (hidden "thinking" tokens) | The accounting measures the allowance per use from phase 0; the daily guard is set from real numbers |
| Grouped AI answers may be less careful | Code checks on every answer; spot checks shown on the status page |
| Automatic Dropdash sign-in may clash with Dropdash's terms (unknown) or trigger a one-time code | Strict limits; it stops and asks the owner on anything unusual |
| Ranking without proof may surface items the client finds obvious | The room part and the "already saturated" dip; the owner reviews the first issues |

## 11. Questions for the owner (also in the handoff, with answer lines)

1. Approve this direction (rank instead of pass/fail, a big universe, four levels of effort)? **Recommendation: yes.**
2. The rank's weights: presence 35, buzz 25, product appeal 25, room 15? They replace your 15 October weights, and will be adjusted once after testing on the existing 34 types. **Recommendation: yes.**
3. The ten sites and what "present" means on each (section 3.2 table)? **Recommendation: yes.**
4. Let Amazon and Flipkart best-seller and new-release pages suggest new ideas again (this reverses the 14 October choice)? **Recommendation: yes.**
5. Report wording: plain suggestions; a short "why it's a good pick" line written by the program from saved facts; and the "Client Compliance Check Required" note on baby, food, skin, medical and mains-electrical products? **Recommendation: yes to all three.**
6. Report look: simplify the product page of the 16 October design into the suggestion layout, choosing from 2 quick mock-ups first? **Recommendation: yes.**
7. Automatic Dropdash sign-in, with the phone number and password kept in Windows Credential Manager (you type them once into a Windows box)? **Recommendation: yes.**
8. Supplier leads = the dropship platform listing plus IndiaMART results shown as they are; stop company-page reading, Google Maps and AI page reading? **Recommendation: yes.**
9. Quick-delivery app checks only for top picks, using the 34 credits left until 2 November, and no new pack for now? **Recommendation: yes.**
10. AI budget: research may use at most a set share of your weekly AI allowance each day, fixed from week 1's measurements (expected about 15–35% a week); the rest stays for your own Codex work? **Recommendation: yes.**
11. Work order as in section 8 (first reports in about 8–11 working days)? **Recommendation: yes.**
12. Keep research stopped until phase 2 is loaded (the old design would spend AI on steps that are being removed)? **Recommendation: yes.**
13. Security:
    - A helper accidentally displayed the private website's draft-upload key in its own working notes (not shared anywhere else). Replace it with a new one?
    - The Dropdash password was pasted into chat on 15 October. Change it when you set up the automatic sign-in?
    - Three saved Dropdash pages still contain your first name. Delete them now?

    **Recommendation: yes to all three.**
14. Private website: simplify its checks to "the owner approved it, and the files are the ones approved", instead of rewriting every check for the new design? **Recommendation: yes** (saves 2–4 days).
15. When phase 2 loads, start the research records fresh but keep the downloaded catalogues (the 16 October records follow the old rules)? **Recommendation: yes.**
