# Plan R10 — sources, crowding check, larger product map and spend watch (14 October 2026)

**Project stage: Development and testing** (no real users; precautions that only protect real users are not planned).
**Status: approved by the owner on 14 October 2026 (A1), building wave by wave.** One part is being redesigned with the owner: how products are ranked (a weighted opportunity score instead of the four priority tiers; see "Owner answers" in section 6). W6's selection step and W7's market rules wait for that design; everything else proceeds.

This document replaces the 14 October morning proposal. The owner answered its eleven questions on 14 October; the answers are recorded below as decisions. Facts come from read-only inspection of the code and the live store on 14 October (about 10:40 UTC) and from providers' own pages checked the same day. Estimates are labelled.

## 1. Owner decisions (14 October 2026)

| # | Decision |
|---|---|
| D1 | **Dropshipping platforms:** the owner confirmed permission from all four platforms (DeoDap, BaapStore, Dropship India, Dropdash). Use them fully: catalogue, new arrivals, bestseller/trending lists, prices, supplier details. |
| D2 | **Logins:** the owner signs in once with a separate research Gmail (chosen 14 October; the address is kept out of the public repository) to Google, Trends, IndiaMART and the four platforms. The program reads these sites in a browser while signed in. |
| D3 | **Apify** is not a paid subscription: the free plan gives USD 5 of usage every month (renews monthly, no card). Use that free monthly allowance automatically for deeper Meta reads. No paid Apify plan. |
| D4 | **Amazon India and Flipkart are turned around:** they stop being discovery sources and stop being positive market votes. They become a **crowding check** ("how crowded is this product already?"). Low presence is good; crowded products go later in the queue. |
| D5 | **Google Trends:** the owner reports permission from Google (Google India's Trends team, approved up their chain) to use Trends; Google will not build a special API for us. Read Trends in the program's browser. |
| D6 | **IndiaMART:** permission confirmed; read it signed in (D2). |
| D7 | **Blinkit, Zepto, Instamart:** switch them on and spend the prepaid quick-commerce credits automatically. |
| D8 | **Product map:** build a much bigger map of product areas and places to search; switch on the AI heading expansion. |
| D9 | **Spend watch:** track every expense and allowance; flag usage at 50% and above; everything production-grade, nothing left switched off. |
| D10 | **AI:** the owner's existing ChatGPT subscription through the Codex program already installed on this computer (it is already signed in; no new login needed). |
| D11 | **Google Maps:** go straight to the browser read (skip the plain read that always returns an empty shell). |
| D12 | **Private download website:** deploy the portal update when the first report set is ready. |
| D13 | **GitHub author address:** replace the Gmail address on the one affected commit with the private GitHub address (see question A5 for the exact effect). |

## 2. Facts that shape the plan

**Running research (10:37 UTC, commit `0a71c9c`):** 30 candidates, 24 accepted narrow types, 24 assessed, 18 passing, **6 report-eligible** (need 20), 0/2 report sets, USD 0 provider money, 227 AI jobs. Rounds start every 1–8 minutes; the research process still uses about 90% of one processor core (local recalculation).

**Browser:** the program drives the Windows copy of Google Chrome from Linux (WSL) over Chrome's remote-control connection, always **hidden** (headless) and with a **brand-new empty profile for every read** (`native_public_browser.py:254`). It therefore can never be signed in to anything, and Google Trends refuses its hidden browser every time (HTTP 429, 44 of 44 attempts). A visible window is supported by the code (`headed=True`) but no automatic route uses it. No sign-in command exists.

**Quick commerce (quickcommerceapi.com):** the program already runs with `--funded-quickcommerce-fallback`; checks never ran only because no location is passed (`SOURCE_QUICKCOMMERCE_LOCATION_PENDING`), and the Windows launcher's allowed-flags list cannot pass one. Balance at 10:40 UTC: **44 credits left of the 100 free sign-up credits (56 used in early trials)**. Provider pricing (checked 14 October): credit packs are one-off purchases that **expire 30 days after purchase and do not refill monthly**: ₹500 = 2,500 credits; ₹1,800 = 10,000; ₹7,500 = 50,000. One search on one platform in one place = 1 credit, charged only on success; the program makes one call per app (Blinkit, Zepto, Instamart), so one product in one city costs 3 credits; the balance check is free; limit 100 requests a minute.

**Apify:** free plan USD 5/month, renews monthly, unused credit lapses. The Meta Ad Library reader the program already prepared (`curious_coder` scraper) costs about USD 0.75 per 1,000 ads plus a tiny start fee; the program reserves USD 0.03 per run. Current period (to 2 November): USD 5.00 opening, about USD 3.07 earmarked for sources. Apify is wired only to the manual benchmark command today; the automatic programme reads Meta directly (free).

**Meta:** the Ad Library **website** shows all active ads in India free; Meta's official Ad Library **API** returns only political/social-issue ads for India, so it cannot replace the website. Apify adds depth (advertiser counts, start dates) that the free direct read sometimes misses.

**Google Trends official API:** still an application-only alpha (announced July 2025); no general release. Applying is free and optional (question A9).

**AI allowance:** the subscription is **ChatGPT Plus** (shared with the owner's other Codex work on this computer). GPT-6 Luna is the cheapest Codex model (OpenAI estimates about 350–3,000 messages per 5-hour window on Plus). The weekly window was **24% used** at 10:46 UTC, resetting 20 October 05:04 UTC. The program's AI calls run with `--ephemeral`, which hides usage; a test call without it saved a session file whose last `token_count` event carries `rate_limits` (window, used percent, reset time, plan type) and a `credits` balance. The installed Codex (0.162.1) also has an `account/rateLimits/read` request in its app-server, which reads the same figures without any AI call. The account also shows a Codex **credits balance** (about 59,915); the program must never spend it (its setting already says no paid fallback).

**Amazon/Flipkart readers** capture only identity, title, link, position, price and image. Ratings count, stars, seller/brand, "bought in past month", sponsored label and the total result count are **not** captured, but the full rendered page (`rendered.html`) and page text are saved, so new fields can be read from saved pages with a new parser version.

**Product map:** 224 headings in 28 sub-areas (`config/research/catalogue.json`), all from Amazon-led seed pages; headings are served alphabetically within a category rotation, four per round. `catalogue_expansion.run_expansion` (AI role C) exists but nothing in the programme calls it. The AI's phrase schema only allows Meta, marketplace and Trends phrases (`research_ai.py:233`).

**Where today's market evidence comes from:** the live store holds 724 Amazon, 385 Flipkart, 171 Meta, 0 Trends and 0 quick-commerce observations, so today's report-eligible types rest mainly on Amazon and Flipkart search.

**Selection today:** seven market sites (Meta, Amazon, Flipkart, Trends, Blinkit, Zepto, Instamart; `opportunity_policy.py:19`), four priority tiers (`opportunity_policy.py:25-32`). AI prompts allow estimates but never tell the AI to *prefer* a labelled estimate over "Unclear". Supplier search is queued only when it is a type's last missing piece (`report_selection.py:1248`). No stage promotes nearly-finished types.

**Repository visibility:** the GitHub repository `neuraflowindia/dropshipper-agent` is **public** (question A6).

## 3. The programme (work packages)

An independent review (14 October, read-only, extra-high effort) checked this plan against the code; its findings are built into the packages below.

**Order (waves).** Each package ends with its focused tests passing and an independent review by a separate helper. At the end of each wave the tracker and `handoff.md` are updated so a new conversation can continue.

1. **Wave 1:** W1 (sign-in browser, so the owner can sign in early), W12 (spend watch), W9 (AI judgment), W3 (IndiaMART, Maps).
2. **Wave 2:** W2 (Trends), W4 (platforms), W6 (crowding check), W11 (Apify Meta depth).
3. **Wave 3:** W5 (quick commerce, needs W6), W7 (market sites, includes a dry run), W8 (wider discovery).
4. **Wave 4:** W10 (product map).
5. **Wave 5:** W13 (cutover and live proof), W14 (docs, suites, outside steps).

**Rules that apply to every package.**
- Development stage (CLAUDE.md 1.6): new versions replace old ones in place. No compatibility layers for old records; saved raw pages are re-read with new parser versions; changed policies get new version numbers and types are simply re-judged.
- Replaced paths are deleted in the same programme (CLAUDE.md 8.4): the hidden-browser and Playwright Trends routes and their manual scripts (`trends_controller.py:157-217` fallback, `trends_capture.py`, `scripts/capture_trends_browser.py`, `scripts/save_connected_trends_diagnostic.py` once the signed-in route is proven), the Maps plain-read-first branch, Amazon Movers & Shakers, the Amazon/Flipkart discovery specs, the single-location quick-commerce options.
- Every route change is mirrored in the private portal's packet checker (`portal/functional-packet.mjs:14,24,142,391-393,613-691` and `portal/release-admin.mjs:33,42-46,300-302`) and its tests, or new report packets would be refused at upload. The portal deploy (D12) carries these changes.
- Every new route id is registered in all route tables: `source_coverage.ROUTES`/`PLANNED_ROUTES` (61-78), `source_acquisitions.CAPTURE_SOURCES` (37-40), `research_cohort._SOURCES` (20), `operations.py` labels and `_SITES`, `report_source_facts.py:34`, `research_scope.py:63-64,118-122`, `operational_backup_selection.py:561`, `product_queries.py:26-28`, `catalogue_discovery.py:34,173-174,347-383`, `source_interpretation.py:32,381-385`, `material_updates.py:23`, `research_ai.py:167,1054`, and supplier routes also in `supplier_effort.record_supplier_effort`.
- Raw feeds and pages are kept as files; only matched, relevant items become research events (the event log is already about 300 MB and the store about 600 MB).
- Signed-in sites: never pay, never accept a paid plan or membership prompt (the route waits with a reason instead), never click contact, "View number", "Get best price", "Send enquiry" or similar. Reading only.
- Coverage of at least 90% on all working code touched by the programme (CLAUDE.md 7.2); no exclusions.

### W1. Signed-in research browser and owner sign-in

- **Objective:** one lasting, separate Chrome profile for the program (never the owner's everyday profile) so Trends, IndiaMART, Dropship India and Dropdash can be read while signed in; the owner signs in once.
- **Profile:** Windows folder `%LOCALAPPDATA%\NeuraFlowDropshipper\research-browser-profile` (path resolved through PowerShell, not hard-coded). `native_public_browser.native_snapshot` gains `profile="signed-in"`; all other routes keep the throwaway profile.
- **Changes the lasting profile needs** (`native_public_browser.py:72-75,123,254`): delete any old `DevToolsActivePort` file before each start; close Chrome with the browser's own close command first and force-kill only as a fallback (protects saved sign-ins); one signed-in Chrome at a time through a lock that also covers the owner's sign-in window.
- **Visible but out of the way:** Google refuses hidden browsers, so signed-in reads use a real visible window, started minimised **without taking focus** (SW_SHOWMINNOACTIVE) and with `--disable-renderer-backgrounding --disable-backgrounding-occluded-windows --disable-background-timer-throttling` so pages render fully while minimised. No disguise and no fake browser identity. Reads need the owner's Windows session to be signed in (the scheduled task already runs in it); while Windows is locked or signed out, signed-in routes wait with `OWNER_DESKTOP_UNAVAILABLE`.
- **Sign-in window:** new `scripts/research_sign_in.py open|status`. `open` starts **plain Chrome without the remote-control flag** (Google refuses sign-in in automation-controlled browsers) and with `--disable-sync`, on the same profile, with tabs for Google sign-in, Google Trends, IndiaMART, DeoDap, BaapStore, Dropship India and Dropdash (DeoDap and BaapStore are still read through their public feeds; signing in there only helps if signed-in pages show more, such as dropshipper prices). While it is open, signed-in reads wait with `OWNER_SIGN_IN_WINDOW_OPEN` and never close it. `status` reports each site as signed in / sign-in needed / expired, from the markers seen on the latest reads (no extra visits).
- **Without sign-in:** affected routes wait with `OWNER_SIGN_IN_REQUIRED` (a calm wait, not repeated failures). Everything else keeps running.
- **Desktop app:** `desktop_research_control.py` gains a `sign-in` action (its allowed actions list, line 18). `DropshipperDesktop.cs` gains a "Sign in to research sites" button, one text line for sign-in needs and the spend lines (W12), and a Windows notification (`NotifyIcon.ShowBalloonTip`) per new need. Text-only additions inside the existing window style, so no mock-up is needed (CLAUDE.md 3.8). Rebuild with `Build-Dropshipper.ps1`, reinstall with `Install-Dropshipper.ps1`, run `Test-Dropshipper.ps1`, restart the tray app.
- **Security:** the program never stores, reads or prints passwords or cookies; Chrome keeps them encrypted in the profile under the owner's Windows account. The profile is excluded from daily backups and from Git. Access waits use a separate identity (`owner-signed-in`) so old refusals of the hidden browser do not hold the signed-in route. While a signed-in Chrome is being driven, other programs on the computer could in principle reach it through its local control port; the port is random, local-only and closed after each read (see question A3 on which Google account to use).
- **Live check (W13):** ten signed-in reads while the owner is typing elsewhere: no focus taken, complete Trends and IndiaMART pages.
- **Tests:** profile path, stale-port removal, graceful close, lock (including sign-in window open), wait codes, status parsing, backup exclusion, desktop status fields and actions.

### W2. Google Trends through the signed-in browser

- **Build:** the `google-trends` route uses W1's profile with the existing chart/CSV export logic (`trends_controller.capture_direct_trends`; `headed`/`profile` passed through at line 214, at `create_trends_handlers` line 504, and through the cooperative path at `trends_shared.py:126`). Pacing: at most one Trends read every 90 seconds, daily ceiling 200 (config). Escalating waits stay (5 min → 1 h → 6 h → 24 h) under the new access identity. Rising related queries feed discovery (W8).
- **Proof gate:** after the owner signs in, the first three Trends reads must save complete exports. If Google still refuses, the route stays blocked with the exact reason, the desktop app says so, and the program carries on without Trends (fail closed; no workaround). The contract's "connected browser chart/export route" wording is replaced by this route.
- **Removed:** hidden-browser and Playwright Trends routes and their manual scripts (see the rules above), with their tests.
- **Tests:** pacing, daily ceiling, separate wait identity, export parsing, refusal handling.

### W3. IndiaMART signed in, and Google Maps straight to the browser

- **IndiaMART:** supplier route reads search results in the signed-in profile; pacing one read every 30 seconds, daily ceiling 150. The login-wall detector stays (`supplier_route_acquisition.py:220-243`): a wall while signed in means the sign-in expired → `OWNER_SIGN_IN_REQUIRED`. Parser v6 reads supplier name, location, product title, price/MOQ text and listing link. IndiaMART category pages also feed the product map (W10).
- **Google Maps (D11):** `supplier_search.supplier_request_for_state` (101-117) returns the browser request directly for `google-maps`; the proof gates at 726-730, 811-812 and 854 accept a Maps browser read without a plain read; the plain-read-first branch for Maps is deleted.
- **Tests:** wall while signed in, parser v6 on saved fixtures, pacing, Maps browser-first planning and gate.

### W4. Four dropshipping platforms

- **Route ids:** four separate routes (`deodap`, `baapstore`, `dropship-india`, `dropdash`) in a new source group `dropship-platforms`, counted as **one** market site.
- **DeoDap and BaapStore (public Shopify feeds):** `collections.json`, collection product lists and `products.json`, one request every 5 seconds, daily ceiling 1,500 per platform. Fields: title, images, price and MRP, stock flag, vendor, tags, created date, collection membership (new arrivals, bestsellers, trending, state-wise bestsellers). A full catalogue refresh at most weekly; new-arrival and bestseller collections daily.
- **Dropship India and Dropdash (signed in, W1):** browser reads of the catalogue, trending / "high demand" / "winning products" pages and product pages; one read every 30 seconds, daily ceiling 300 each.
- **Per-type check:** Shopify feeds cannot be searched, so each type is checked by a **local match against the saved catalogue** (title and tag words from the type's definition and phrases), then AI relevance review of the few best matches (batched with W7's combined review).
- **Roles:** (a) **discovery:** new arrivals, bestsellers and trending items become candidates; (b) **market site:** only a relevant item on a bestseller / trending / high-demand list counts, as "reasonably positive"; a plain listing does **not** count (the contract says suppliers never vote); (c) **supplier lead:** a relevant listing is recorded as supplier effort and a lead with price and stated dropship terms (`supplier_effort.record_supplier_effort`); (d) **product map** input (W10).
- **Tests:** feed parsing on saved fixtures, pacing and ceilings, local matching, roles, supplier-lead mapping, market-site mapping, replay, portal checker rows.

### W5. Quick commerce switched on (after W6)

- **Locations:** the contract's three reference points: Mumbai (19.0760, 72.8777), Delhi NCR (28.6139, 77.2090), Bengaluru (12.9716, 77.5946), in `config/research/quickcommerce_locations.json`. The request builder reads the list from config; location leaves the programme's frozen settings (`programme.py:104,194,204-212,252,4780`) and the `--location-*` options are removed (`run_programme.py:89,157-160`), so the Windows launcher needs no new flags.
- **No discovery use:** the quick-commerce discovery specs (`catalogue_discovery.py:377-383`) are deleted; credits are spent only on product checks.
- **Admission rule:** paid checks only for types that pass the checklist and whose crowding is Low or Medium (not High, not "Not checked"); one phrase only (the type's main phrase, no sparse-result extra phrases from `source_coverage.py:1316-1385`); Mumbai first on all three apps (3 credits), Delhi NCR and Bengaluru (6 more) only when Mumbai shows a relevant result. Held-back checks are recorded as "reserved for later", not as failures.
- **Estimate:** 3–9 credits per type; about 300–900 credits a month at present research speed; up to about 2,000 if research speed doubles.
- **Credits:** the free balance check moves from the script (`check_quickcommerce_connection.py:117`) into the library, runs before each batch, and a positive balance clears a route-wide "credits exhausted" wait (`quickcommerce.py:189-191,301`). A pack purchase is detected from a jump in the balance; its expiry is recorded as detection date + 30 days.
- **Tests:** location list, admission rule, one-phrase rule, credit accounting, exhausted-credit wait and clearing, no repeat of an uncertain charged call.

### W6. Amazon and Flipkart as a crowding check (D4)

- **Readers:** parser version 2 adds, per result card on the **first results page only**: ratings count, star rating, brand/seller text where shown, "bought in past month" (Amazon), sponsored label; per page: total result count. Saved pages are re-read with v2 (development stage; no old-parser compatibility).
- **Crowding level (code, from AI-reviewed relevant listings only):**
  - **High:** a relevant item on an Amazon bestseller list; or at least 3 relevant listings with 1,000+ ratings; or at least 8 relevant listings from at least 5 different brands/sellers; or at least 2 relevant listings showing "1K+ bought in past month".
  - **Low:** at most 2 relevant listings across both sites and none with 200+ ratings.
  - **Medium:** everything else.
  - **Not checked:** reads failed or not done; never treated as Low. The check is retried; a type stays not report-eligible until its crowding is known, because the check is free.
  - **Calibration:** after the first 50 checked types, if more than 70% or fewer than 10% land in High, the agent adjusts the rating thresholds (only those) once, records the before/after counts in the implementation record, and reports it to the owner.
- **Selection:** in each issue, Low and Medium crowding types fill the issue first under the existing priority tiers; High crowding types are used only when fewer than ten Low/Medium types are ready for that issue, and are labelled. Plugged into `report_selection` sort, `issue_allocation.allocate_issues`, `live_outputs`, PDF, workbook and portal summary ("Amazon/Flipkart crowding: Low / Medium / High" with the counts behind it).
- **Discovery:** Amazon search, Flipkart search and the Amazon bestseller lists stop producing candidates (they keep running as crowding reads). Amazon Movers & Shakers is retired.
- **Tests:** parser v2 on fixtures, every boundary, "not checked", calibration rule, selection order per issue, report fields, retired routes, portal checker. About 34 test files mention the Amazon/Flipkart routes; they are updated to the new rules, never deleted to get a pass.

### W7. New market-site set and one market review per type

- **Market sites become four:** Meta; Google Trends; Quick commerce (Blinkit, Zepto and Instamart together count as one site, like Amazon's lists did); Dropship platforms (bestseller/trending lists only). Amazon and Flipkart leave the list (`opportunity_policy.SOURCE_PLATFORMS` and `_evaluate_platforms` 85-89, 113-117; `market_validation.py:44,80,257`; `discovery.py:179-188`; `research_ai.py:167,1054`; wording in `report_narration.py:296-300` and `live_outputs.py:839-845`). New opportunity policy version (v3). Tier rules keep their shape: two usable sites, or one reasonably positive site; **at least one site is still required.** Quick-commerce stock stays neutral context (usable, not positive), as the contract says.
- **One review per type (P4):** a type's saved results waiting for AI relevance review are reviewed together in one AI call, with a new support-prompt version; every type is re-reviewed once under it (extra AI load counted in W12's measurements).
- **Dry run first:** before cutover, run the new policy on a copy of the store and record how many types stay eligible. Expect a big drop (today's eligible types rest on Amazon and Flipkart search: the store holds 724 Amazon, 385 Flipkart, 171 Meta, 0 Trends and 0 quick-commerce observations). The count recovers as Trends, quick commerce and the platforms come online. This is accepted in advance (question A2).
- **Tests:** tier rules with the four sites, quick commerce counted once, dropship plain listings not counted, combined review keeps per-site states, portal checker parity.

### W8. Wider discovery

- **Meta:** heading searches stay; each relevant advertiser's other active ads (free, same direct read) become candidates.
- **Dropship platforms:** new arrivals, bestsellers, trending (W4). **Google Trends:** rising related queries (W2). **IndiaMART and TradeIndia:** newest listings in mapped categories.
- **Round limits:** source reads per round 12 → 20 (config). AI calls stay at 10 per round until W12 has measured the weekly-allowance share per AI job; then raised only if the weekly projection stays under the pause level.

### W9. AI judgment and finishing nearly-ready products

- **P2:** the checklist prompt gains one rule: when the product's form and use are known, give a labelled practical estimate ("Estimated: …") instead of "Unclear"; keep "Unclear" only when the form or use itself is not known. New prompt version; every type is re-assessed once under it, then reused.
- **P3:** stage planning puts nearly-ready types first: any type that passes the checklist gets its market review, crowding check and supplier search queued at once (`report_selection.py:1245-1250` no longer waits for supplier search to be the last gap), ahead of new discovery while the report sets are short of eligible types.
- **Tests:** prompt version and reuse, priority order, supplier queueing for every passing type.

### W10. Bigger product map and AI expansion switched on (D8)

- **Target:** about 1,200 headings (4 categories × about 12 sub-areas × about 25 headings), built mainly from non-Amazon material: DeoDap/BaapStore collection trees, Dropship India/Dropdash catalogues, IndiaMART/TradeIndia category trees, Meta advertisers and the existing headings.
- **Builder:** the existing seed path (`catalogue_seed.py`, AI role C, one branch per call) is extended to take these new materials as branch inputs; this builds the map. `catalogue_expansion.run_expansion` (one concept at a time, at most five) is then switched on in the discovery step for **renewal**: one AI slot per round when new material exists (new arrivals, rising queries, new advertisers). About 200–300 AI jobs for the first build (estimate, measured by W12).
- **AI schema:** phrase sources add `dropship` (`research_ai.py:233-235`; `catalogue.py:103` already accepts `supplier` and `quickcommerce`).
- **Heading order:** never-served first, then headings with fresh signals, rotating categories; headings whose types came out High crowding move back. Replaces alphabetical order (`catalogue_discovery.py:350,484-490`).
- **Tests:** branch inputs from the new materials, admission/merge, ordering, weekly renewal.

### W11. Apify free allowance for deeper Meta reads (D3)

- **Separate follow-up route** `meta-ads-depth` (not the `meta_handler=` slot, which would replace the free read): it runs only for types whose free Meta read lacks advertiser counts or ad start dates, through the existing account registry, ledger and reserve-before-spend rules (`accounts.choose_account`, the Apify transport, `map_meta_rows`), charge recorded as unknown until settled. Its results count toward the single Meta site.
- **Account and money:** the one funded account (period 3 October – 2 November). The "USD 3.07 earmarked" is the part of this period's USD 5.00 set aside for source reads after earlier trial charges and holds; the route may use it down to the protected USD 0.50 buffer and then waits for the next monthly allowance. The other two approved accounts in the contract are not funded and are not used.
- **Tests:** admission rule, reserve and settle, stop at the buffer, period rollover, no retry of an uncertain paid call, Meta site counted once.

### W12. Spend and allowance watch (D9)

| Item | Allowance | How it is measured |
|---|---|---|
| Apify | USD 5/month free (4.50 usable) per account period | existing ledger |
| Quick-commerce credits | active packs (credits used ÷ credits held) and expiry | library balance check before each batch, plus each response's remaining count |
| AI (ChatGPT Plus via Codex) | 5-hour and weekly windows | `codex app-server` request `account/rateLimits/read` (present in the installed Codex 0.162.1; no model call, no files). Fallback if it fails: run AI calls without `--ephemeral`, find the program's own session file by the thread id in the `--json` output, read its last usage record, delete only that file, with a clean-up sweep after crashes |
| Codex paid credits | must stay unused by research | credits balance from the same read; a drop during a research AI call pauses the AI route (`AI_PAID_CREDITS_USED`) until the next window reset, then resumes if the balance is unchanged since |
| Hosting (Cloudflare) | free plan; existing USD 1 budget email | recorded plan facts; no extra polling |
| Money in rupees | ₹750/month target, review above ₹1,000 | Apify charges plus quick-commerce packs (pack price recorded by the agent from the owner's purchase message or the detected balance jump at the published price) |

- **Flags:** 50%, 75% and 90% of each allowance, once per crossing per period; at 100% a money route (Apify, quick-commerce credits) pauses cleanly while free work continues. **AI never pauses for allowance use** (owner, A4): it only flags at 50/75/90/100% of whatever usage windows Codex reports (the owner notes Codex has no 5-hour limit), with a desktop notification. The paid-credits guard (`AI_PAID_CREDITS_USED`) still pauses, because that would be spending money.
- **Where the owner sees it:** `--meter` gains an "allowances" section read from the saved records (no provider calls); the desktop app shows one line per item with percent used and a Windows notification for each new flag; a dated alert log keeps every flag. Spend-watch records are added to daily backups (`operational_backup_selection.py`).
- **Tests:** thresholds, rollover, once-only flags, pause and resume, rate-limit reading (both methods), paid-credit guard, desktop fields, secret-safe output. Tests that pin `--ephemeral` (`tests/test_codex_executable.py`, `tests/test_visual_runner.py`, `scripts/check_codex_luna_connection.py:155`) change only if the fallback is needed.

### W13. Cutover and live proof

- Clean stop (`desktop_research_control.py stop`), copy the store to a dated backup folder (new records cannot be read by older code), load the new code, start.
- **Live proof gates**, each recorded with dates: Trends 3/3 exports after sign-in; IndiaMART, Dropship India, Dropdash signed-in reads; ten signed-in reads with no focus taken; DeoDap and BaapStore feeds; quick-commerce results in three cities; one Apify depth read; crowding level on at least 10 types; AI usage percent visible; one flag shown in the desktop app.
- **Speed:** measure round time and new types per hour for at least 3 hours. If the median round takes longer than 15 minutes, or the research process stays above 80% of a processor core with rounds slowing, profile it and fix the cause inside this programme (as with the report-pool fix on 14 October).
- Watch items carried over: the IndiaMART task waiting with `SUPPLIER_BROWSER_FALLBACK_PROOF_REQUIRED` (resolved by W3) and the Trends wait that sat at attempt 1 (replaced by W2).

### W14. Documents, test suites and outside steps

- Update the product contract (§2 Apify and longevity rows, §5 sources and the "fourteen routes" audit line, §6 market sites and crowding, §7 suppliers with dropship platforms and IndiaMART), execution specification, implementation record (R10), cost plan, operator guide, Windows launcher guide, how-it-works, tracker, brief, index and platform research.
- Fix the known failing tests (`test_supplier_unchanged_original…`, `test_company_reconciliation_readiness…`, `test_quickcommerce_access_controller…uncertain_due_recovery…`, the timing-sensitive deadline test, the two order-dependent `test_suppliers` tests) and update `test_documentation_layout` to accept the owner's `CLAUDE.md`/`AGENTS.md` at the project root.
- Whole local Python suite and portal suite once at the end (question A1).
- Commits and pushes (approved). Git author rewrite and force push (approved, A5; blocked once by the computer's safety check on 14 October, so it needs the owner's one-time permission click). The repository stays **public** (owner, A6).

### When the programme is done, and what follows

**Done** = W1–W14 built, reviewed, focused tests and both whole suites passing, and every W13 live proof gate recorded (or, for a site that still refuses after sign-in, recorded as blocked with the exact reason).

**Then, without a new approval:** research runs until 20 report-eligible types exist; the agent builds the two bilingual report sets, deploys the portal update (D12) and verifies private delivery as the tracker's scope table says. If 20 eligible types are not reached within 7 days of cutover, the agent stops and reports the exact shortfall and causes.

## 4. Effort and time (estimates)

| Package | Size | Rough agent time |
|---|---|---|
| W1 sign-in browser + desktop app | Medium–large | 1–1.5 days |
| W2 Trends | Small–medium | 0.5 day |
| W3 IndiaMART + Maps | Medium | 0.5–1 day |
| W4 four platforms | Large | 2–3 days |
| W5 quick commerce | Medium | 0.5–1 day |
| W6 crowding check | Large | 2–3 days |
| W7 market sites + combined review + dry run | Medium | 1–1.5 days |
| W8 wider discovery | Medium | 1 day |
| W9 AI judgment + finishing first | Small–medium | 0.5–1 day |
| W10 product map + expansion | Large | 2–3 days build, 1 day first generation |
| W11 Apify Meta depth route | Medium | 0.5–1 day |
| W12 spend watch | Medium | 1–1.5 days |
| W13 cutover + live proof | Medium | 0.5 day plus at least 3 hours of watching |
| W14 docs, suites, outside steps | Medium | 1–1.5 days |
| **Total** | | **about 16–22 agent days, over several conversations** |

These are estimates. Owner time: about 10–15 minutes to sign in once (after wave 1), a few minutes to buy a quick-commerce pack if approved, and nothing else unless a stop condition appears.

## 5. Risks and how they are handled

- **Eligible products drop at cutover** (W7): accepted in advance; recovers as new sites come online; 7-day shortfall report.
- **Google may still refuse Trends in a visible signed-in window:** fail closed with a clear notice; the optional official Trends API application is the long-term route.
- **Signed-in reads use real accounts:** pacing, daily ceilings, reading only; a site asking to sign in again pauses only its own route. See A3 for the account choice.
- **A visible minimised window on the owner's desktop:** no focus taken (checked live); reads pause while Windows is locked.
- **Crowding thresholds are drafts:** one automatic calibration rule, recorded.
- **More AI use per hour:** pause levels protect the owner's own Codex use; Luna is the cheapest Codex model.
- **Quick-commerce credits expire after 30 days:** smallest pack that covers a month; 50/75/90% and expiry flags.
- **Big change set:** wave by wave, focused tests and an independent review per package, store copied before cutover.

## 6. Approvals and questions for the owner

Already approved: ordinary research; focused tests; commits and pushes; clean stop/start of research; owner sign-in renewal; Maps in the program's browser; portal deploy when the first report set is ready; spending the existing quick-commerce credits and the free Apify allowance automatically; reading the four platforms, IndiaMART (signed in) and Google Trends.

| # | Question | Recommendation |
|---|---|---|
| A1 | Approve this whole programme (W1–W14), including running the whole local test suite and the portal test suite once at the end, so the next agent can build it without stopping? | Yes |
| A2 | Market rules after the turn-around: four market sites (Meta; Google Trends; quick commerce counted as one site; dropship platforms' bestseller/trending lists only); every product still needs at least one site; crowded (High) products only fill an issue when fewer than ten others are ready; the report shows the crowding level; Meesho is not added. You also accept that the number of report-ready products will drop at first and climb back as the new sites come online. Agree? | Yes to all |
| A3 | Which Google account should the research browser sign in with? A separate new Gmail just for research keeps your business Gmail (which also opens the private download website) safe if Google ever objects to automated use. | A new research-only Gmail (you create it; 5 minutes) |
| A4 | AI: research pauses its AI work at 80% of your weekly ChatGPT Plus allowance (and 85% of the 5-hour allowance) so you keep room for your own Codex use. Right levels? | 80% / 85% |
| A5 | GitHub author fix: rewriting the one commit also changes every commit made after it (three today) on GitHub (a "force push"). The old commit can still be opened by its exact code until the repository is private. Go ahead? | Yes |
| A6 | The GitHub repository is **public**: anyone can read the code and documents (including the client price and business rules). Make it private (after A5)? | Yes |
| A7 | Quick-commerce credits: 44 free credits are left (enough for about 5–15 products). Buy the ₹500 pack (2,500 credits, valid 30 days) when the desktop app says they are low, and again each month if needed? You make the purchase; the agent cannot pay. | Yes, ₹500 pack |

### Owner answers (14 October 2026, afternoon)

| # | Answer |
|---|---|
| A1 | **Yes**: the whole programme, including both whole test suites once at the end. |
| A2 | Crowding rules **yes as written** (High crowding fills an issue only when fewer than ten others are ready; reports show crowding; temporary drop accepted). The proof-of-demand rule is **reopened**: the owner does not want a simple "bestseller list or nothing" rule (bestsellers may already be crowded; plain listings may have no demand) and asked for a weighted score across platforms, bestseller presence and crowding. Design under discussion; W6 selection and W7 wait for it. |
| A3 | A separate research Gmail (the owner offered two; the chosen one is recorded only in the private handoff). |
| A4 | **No AI pause.** Flag usage only (see W12). |
| A5 | **Yes.** |
| A6 | **No**: the repository stays public. Consequence: personal details (such as Gmail addresses) are never written into committed files. |
| A7 | Yes, when the credits run low (the owner buys). Everything must behave exactly as it will for the client: full production behaviour, no shortcuts. |

Defaults the agent will use without asking: spend alerts in the desktop app, meter and alert log only (no email); the official Google Trends API application is an optional owner to-do (section 7), not a dependency.

## 7. What the owner does (step by step, when the time comes)

1. **Accounts:** if not done yet, create accounts on Dropship India (dropshipindia.live) and Dropdash (dropdash.co). If you agree with A3, first create a new Gmail for research and use it for these accounts and for IndiaMART.
2. **Sign in once (after wave 1 is built):** the NeuraFlow Dropshipper window shows "Sign-in needed". Click **Sign in to research sites**. A Chrome window opens with five tabs:
   1. Google: sign in with the research Gmail (or the business Gmail if you prefer).
   2. Google Trends: it should already show you as signed in; if not, click **Sign in** at the top right.
   3. IndiaMART: click **Sign In**; use "Continue with Google" if shown, otherwise type your mobile number and the one-time code IndiaMART texts you.
   4. Dropship India: sign in with the account you created.
   5. Dropdash: sign in with the account you created.
   Close the Chrome window. The program picks up the sign-ins on its next reads and the "Sign-in needed" line disappears.
3. **Quick-commerce pack (if A7 is yes):** when the window says the credits are low: open quickcommerceapi.com, sign in, click **Pricing**, choose **Starter – ₹500**, pay. The program notices the new credits by itself.
4. **Optional, any time:** apply for Google's official Trends API at developers.google.com/search/apis/trends ("Apply for the alpha"), describing this product research use; your Google contact may be able to speed it up.
