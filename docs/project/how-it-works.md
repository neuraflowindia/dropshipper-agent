# How the product finder works, in plain English

This page explains what the program does, step by step, without the technical detail. The exact rules live in the documents linked at the end. This page doesn't replace them. It just tells the story.

## What it's for

The program is a research assistant for one client: an Indian business that sells online by dropshipping (selling products that a supplier ships straight to the customer) and private label (selling a generic product under its own brand name).

Its job is to find products worth *testing* in India, gather proof that people are interested in them, and suggest a few possible suppliers. It turns all of that into short reports. The client gets a new report every **Tuesday, Thursday and Saturday at 9:00 am India time**.

A recommendation means "this looks worth trying". It doesn't promise sales or profit.

## Where it looks

Status, 15 October 2026: the new set-up below (agreed on 14 October in [Plan R10](plan-2026-10-14-speed-sources-and-selection.md)) is **built but not switched on yet**. It goes live at the planned switch-over, after which each new site has to prove itself with real reads. Until then the research still runs on the older set-up.

**To find new product ideas**
1. **Meta Ad Library**: the public list of ads running on Facebook and Instagram. It shows who is advertising a product and for how long. The program also looks at the other ads of advertisers it has already found useful.
2. **Four dropshipping platforms**: **DeoDap**, **BaapStore**, **Dropship India** and **Dropdash**. Their new arrivals, bestseller and trending lists suggest products. All four gave permission.
3. **Google Trends** "rising" searches, and new listings on **IndiaMART** and **TradeIndia** in our categories.

**To check each product**
4. **Meta Ad Library** again, for that exact product. When the free look leaves gaps (who is advertising, since when), a deeper look uses the free monthly Apify allowance.
5. **Google Trends**: whether people in India are searching for it more or less over time. It shows ups and downs, not actual numbers of people.
6. **Amazon India and Flipkart**, turned around: they no longer suggest new products, and their main job now is to show **how crowded** a product already is (Low, Medium or High), from how many similar listings there are, how many ratings they have and whether they're on a bestseller list. A product isn't ready for a report until this check is done. (Until the owner settles the new way of ranking products, they still also count as a supporting site in the old ranking described below.)
7. **Blinkit**, **Zepto** and **Swiggy Instamart** (the quick-delivery apps), in Mumbai first, then Delhi NCR and Bengaluru only if Mumbai shows something relevant. Each check costs a prepaid credit, so only products that already pass the checklist and aren't too crowded get checked, and the same city isn't asked again for 30 days.
8. **The four dropshipping platforms** again: whether the product is listed, at what price, and whether it's on their bestseller or trending lists.

**To find possible suppliers**
9. **IndiaMART** (read while signed in), 10. **TradeIndia**, 11. **Google Maps** (read straight in the program's own browser), 12. **supplier and company websites**, and 13. the **dropshipping platforms**, whose listings count as supplier leads.

Google search for suppliers was switched off on 14 October (Google refuses automated searches), and Amazon's "Movers & Shakers" list was dropped (it was always empty for our categories).

Separately, it saves real product photos where it can find suitable ones.

**Signing in once.** Google Trends, IndiaMART, Dropship India and Dropdash are read while signed in with a separate research Gmail, in the program's own Chrome window (never the owner's everyday Chrome). The owner clicks **Sign in to research sites** in the NeuraFlow Dropshipper window, signs in on each tab and closes it. If a sign-in runs out, the window says so and only that site waits. The program never stores passwords, never pays and never clicks "contact" or "get price" buttons.

**When a site says no.** It writes that down and waits longer each time (5 minutes, then 1 hour, then 6 hours, then a day) instead of knocking again and again. Google Trends must first prove it works: after signing in, its first three reads must save complete results. If Google still refuses, research carries on without Trends.

**What it deliberately does *not* do**
- It doesn't buy paid upgrades, extra credits, paid add-on packs or supplier memberships on its own.
- It doesn't contact suppliers, ask for quotes, create accounts or buy anything.
- It doesn't get around CAPTCHAs or other blocks. If a site says no, it writes that down and moves on.
- It doesn't predict profit margin, RTO (orders that get sent back) or RTO costs.
- It doesn't cover pets. The four categories are fixed (see below).
- It doesn't recommend products from well-known brands, such as Lakme, Hasbro or Meyer.
- It doesn't certify that a product is safe or legal. For baby products, things you eat or put on your skin, products that make medical claims, and risky electrical items, it adds a **"Client Compliance Check Required"** note so the client can check for themselves.

## How it comes up with product ideas

**A map of product areas.** The program keeps a map of product areas under each category, such as "baby feeding" or "camping and sleeping", with headings and search phrases under each. There are 48 areas (12 per category) and 224 headings so far; the aim is about 1,200 headings. The map grows by itself, a little each round: the AI looks at new things the program has seen (platform categories and lists, rising Trends searches, new ads, supplier listings), and adds a new heading only when it's really a different kind of product. The old way of building it from Amazon pages has been switched off.

**Discovery rounds.** The program works in short rounds. Each round it searches Meta for a few headings: headings never searched first, then ones with fresh signs of interest, and very crowded ones last, taking the four categories in turn. In the same round it also looks at the platforms' lists and the other new-idea sources above. It mixes two kinds of work so neither gets forgotten: finishing products that are nearly ready for a report, and exploring something new. If a search would just repeat one it has already done, it reuses the saved result.

**The AI's role.** The AI (the owner's existing signed-in AI subscription) does the reading and judging that needs common sense. It reads messy product listings and says what the product actually is. It decides whether two listings are really the same thing. It suggests new areas worth exploring, and it fills in the checklist below. The regular code handles everything that needs to be exact: keeping history, counting, doing the maths, picking which searches to run and staying within limits.

**What a "narrow product type" is.** The program doesn't recommend one seller's exact listing, and it doesn't recommend something as vague as "fitness gear". It recommends a clearly defined *kind* of product. For example, **"Adjustable doorway pull-up bar"** is the recommendation. The real listings and supplier offers that match it are shown underneath as examples. A different colour, pack size or seller doesn't count as a new product. A real difference in how it works does, such as manual vs. powered, or made for a child vs. an adult.

## How it sorts products

- **Four categories:** Home and Decor; Baby Products; Home and Kitchen; Travel and Outdoor.
- Each product gets **one main category**, plus optional extra tags. That way a product that fits both "Home and Decor" and "Home and Kitchen" isn't listed twice.
- **Duplicates:** exact copies are merged. Close matches are compared by what they do, and are kept separate only if they work in a meaningfully different way.
- **Brands, judged sensibly:** well-known brand products are left out. An unfamiliar small label, a shop's name or a maker's name doesn't rule a product out by itself. The reason is written down each time, including for borderline cases.

## How it judges products

**The ten-point checklist.** Each product is checked against ten questions. Is it small? Is it easy to ship? Does it boost confidence? Is it convenient? Does it save time? Does it solve a problem? Does it save the buyer money? Does it improve everyday life? Does it look valuable for the price? Is it aimed mainly at women? For each question the answer is *Meets*, *Does not meet*, *Unclear* or *Not checked*, with a short reason. Sensible, labelled guesses are fine. For example, the program can guess "small and light" from what the product is, without needing exact measurements. When it knows what a product is and how it's used, it gives a labelled estimate instead of answering "Unclear".

- **6–10 "Meets" = Good fit.**
- **5 "Meets" = Decent fit.** It can still go in a report.
- **Unknown isn't failure.** "Unclear" simply doesn't earn a point. If enough questions are still open, the product is marked "more information needed", not rejected.

**Market checks across sites.** These are kept separate from the checklist. Today the program counts how many of **seven sites** show relevant interest: Meta, Amazon (all Amazon routes count as one site), Flipkart, Google Trends, Blinkit, Zepto and Instamart. Supplier sites never count as a vote. Stock on a quick-delivery app is only background information, not proof of sales. Products are ranked in this order:
1. 6+ Meets and 2+ supporting sites
2. 5 Meets and 2+ sites
3. 6+ Meets and 1 genuinely positive site
4. 5 Meets and 1 genuinely positive site

**A new ranking is being designed with the owner.** Instead of these four groups, products will get a weighted score that combines how many places show interest, whether they're on bestseller or trending lists, and how crowded they are on Amazon and Flipkart. Until that's agreed, the four groups above stay, and the crowding level is recorded but doesn't change the order yet.

## Suppliers and photos

Every product chosen for a report gets a real, quick supplier search. The aim is 1–3 useful leads, or up to five when they're easy to find. The search covers Indian manufacturers, wholesalers and importers. For each lead the program records whatever the supplier states: whether they ship single pieces directly to customers, the minimum order, cash on delivery, plain packaging, private-label help, and any subscription or fees. Suppliers that don't need a paid subscription are preferred. If a real search finds nothing usable, the product can still appear, with the note **"Supplier search did not produce any usable results."** All leads are just *leads*. The client checks them.

Photos: the report uses at least one real product photo, with its source noted, whenever a suitable one is found. A missing photo never rules a product out.

## How a report is built

- Each report has **at least 10 products**, normally 10–15. It only goes up to 20 when there's enough good evidence.
- **At least 80% must be new**, meaning never sent to this client before. At most 20% can be meaningful updates, and each update says what changed. For example, a 10-product report has at least 8 new products.
- One product goes into only one report per week.
- From one checked set of facts the program makes a **portrait PDF**, an **Excel workbook** (shortlist, evidence, supplier leads, checklist, and example products) and a short phone-friendly summary.
- The first two reports come in **both English and Roman-script Hinglish** (Hindi written in English letters), with identical facts. After that, the client picks one language.
- Reports go to a **private website** (dropshipper.neuraflow.co.in). Only approved people can open it, using Google sign-in.
- **The owner approves every release.** Drafts stay private. An approved report appears at its release time, even if the owner's computer is switched off.
- If there aren't 10 good products, the program doesn't fill the gap with weak ones and doesn't publish a short report. It saves the draft and the exact shortfall for the owner to discuss.

## How it runs day to day

- **Weekly batch.** Research normally happens once a week, usually Monday, and prepares all three of that week's reports. So Saturday's report may contain Monday's research. The report shows both dates.
- **Rounds with fair shares.** The work is split into small rounds. Each round allows up to about **16 source checks and 10 AI uses** (16 may go up to 20 once the switch-over shows rounds stay quick). Reads in the signed-in browser share about 5 minutes a round. When report writing is due, one AI use is saved for it. When product-definition and market-relevance reviews are due, two more are saved for those. While the next reports are short of ready products, products that already pass the checklist are finished first, but two AI uses a round stay kept for finding new ideas. Busy work can't crowd out the jobs that actually produce reports.
- **The Start/Stop app.** The owner uses the **NeuraFlow Dropshipper** desktop shortcut. Start turns research on and Stop turns it off. Closing the window hides it next to the clock; Exit stops research first and then closes. The on/off choice is remembered after a restart.
- **Automatic check every 15 minutes.** A Windows task checks every 15 minutes, and again at sign-in. If research is switched on, it carries on from where it left off. If it's switched off, it does nothing.
- **Where the work is kept.** Since the 13 October 2026 fresh start, the program's working records live on the fast Linux side of the computer (`/home/anan/neuraflow-dropshipper-data/`). Everything from before that date is kept untouched in the project's `archive/` folder.
- **Backups.** One automatic copy per day goes into the project's `data/backups` folder on the D: drive. It includes research, reports and important files, but not passwords or sign-in files. It sits on the same drive, so it won't help if the drive itself fails. A failed backup never stops research.
- **Cost limits and free allowances.** The aim is to keep running costs at or below ₹750 a month (the estimate is about ₹500: one quick-commerce credit pack, which the owner buys when the app says credits are low). The deeper Meta look uses only the free US$5 a month on one Apify account, keeping US$0.50 back. The AI runs on the owner's existing ChatGPT subscription and never spends paid extra credits. A **spending watch** shows each allowance in the desktop window and warns at 50%, 75%, 90% and 100%; at 100% a paid route waits while free work carries on. Nothing is upgraded or bought automatically, and AI answers that are already saved are reused.
- **When something fails.** One failure doesn't stop everything. The program keeps everything that already worked, notes exactly what failed and when to try again, and moves on to other ready work. If a site is blocked, it moves on to other work. If nothing is ready, it waits quietly and picks up later. It doesn't pretend the work is finished.

## One product, start to finish

This is a made-up example to show the steps. It isn't a real result.

1. During Monday's research, a discovery round finds a pull-up bar that fits in a doorway on a dropshipping platform's new-arrivals list.
2. The AI reads the listing and defines the product as **"Adjustable doorway pull-up bar"**. It checks that this isn't a copy of something already found and that it isn't a big-brand product, and puts it in its main category.
3. The checklist gives, say, 6 "Meets" (small, easy to ship, convenient, solves a problem, saves money on gym fees, good value) = **Good fit**.
4. Market checks find several advertisers on Meta and rising searches on Google Trends: **two supporting sites**, so it lands in the top priority group. Amazon and Flipkart show only a few similar listings with few ratings: crowding **Low**. A quick-delivery check in Mumbai finds it in stock (background only).
5. A supplier search on IndiaMART, TradeIndia and Google Maps finds two wholesale leads, and DeoDap lists a similar item at a wholesale price. One says it ships single pieces to customers and has no subscription. A real photo is saved.
6. The product has never been sent to this client, so it's marked **New** and placed in Thursday's report.
7. The facts are frozen. The PDF(s) and Excel file are made, the owner checks and approves them, and on Thursday at 9:00 am the client sees it on the private website.

## Want more detail?

- [Project brief](pj-brief.md): purpose, stage and where everything lives.
- [Project tracker](pj-tracker.md): what's finished and what's still being worked on.
- [Plan R10](plan-2026-10-14-speed-sources-and-selection.md): the 14 October plan (new sites, crowding check, bigger map, spending watch) and what's built.
- [Operator guide, section 8](../operations/local_execution_and_provider_setup.md#8-signed-in-research-sites-allowances-and-the-r10-cutover): signing in, credits and the spending watch.
