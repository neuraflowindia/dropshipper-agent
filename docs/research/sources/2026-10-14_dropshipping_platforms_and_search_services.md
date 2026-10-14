# Dropshipping platforms and search services — first look (14 October 2026)

Status: **first look; nothing built yet.** The client asked (13 October) that four Indian dropshipping platforms be considered as sources. On 14 October the owner confirmed permission from all four platforms and will create accounts on Dropship India and Dropdash; the build is package W4 of [Plan R10](../../project/plan-2026-10-14-speed-sources-and-selection.md). For this first look only public pages were read: no login, no forms, a handful of fetches per site.

## The four platforms

| | DeoDap | BaapStore | Dropship India | Dropdash |
|---|---|---|---|---|
| Official site | deodap.in (deodap.com redirects) | baapstore.com | dropshipindia.live | dropdash.co / app.dropdash.co |
| What it is | Shopify wholesale store, "No MOQ", plus dropship plans (prices unpublished) | Paid dropship membership (Self-Serve "from ₹9,999"; page also says ₹25,000) | Reseller platform and Shopify app; COD | Done-for-you dropship service and Shopify app |
| Public catalogue | Yes: 12,390 in "All Products", 250+ collections (claims 50,000+ in app) | Yes: 65 collections, mostly fashion (claims 5,000+ SKUs) | Not confirmed (opens on sign-in) | No (catalogue needs signup) |
| Fit with the four client categories | Strong in all four (Baby 135, Balcony & Gardening 380, Bags & Luggage, Bakeware…) | Home/Kitchen (437), Home Decor (290), Kids (604); weak Baby/Travel | Unknown | Claims home, baby, bags/luggage |
| Trending / bestseller lists | "Bestselling Products" (275), state-wise best sellers, "Newly Launched", "Trending searches" | "Trending Products" (15) | Unknown | "Trending"/"High Demand" labels, "winning products" page |
| Data visible publicly | Title, images, price vs MRP (e.g. ₹44 vs ₹349), stock flag, vendor, tags, date added | Reseller price vs MRP, images, SKU, weight, stock flag | — | — |
| Technical route | Shopify `/products.json`, `/collections.json`, 14 product sitemaps work; robots allow; Cloudflare present but plain requests not blocked | Same as DeoDap | JavaScript app with a separate backend; needs login | Login + browser; app robots.txt `Disallow: /` |
| Terms on automated access | Ban without written permission (owner confirmed permission, 14 October) | Copying ban without written permission (permission confirmed) | Ban on crawling/scraping (permission confirmed) | Not read (permission confirmed) |

Observations:
- **DeoDap** is the richest and easiest (official Shopify feeds). Its newly launched and bestseller items are mostly low-cost imported gadgets, and its founder states it supplies sellers on Amazon, Flipkart and Meesho, so many items are probably already on those marketplaces. It looks most useful as a **supplier price benchmark and lead**, and its "Newly Launched" list as an early-signal source.
- **BaapStore** is fashion-heavy; moderate use for Home/Kitchen and Kids.
- **Dropship India** and **Dropdash** need a login (and Dropdash blocks robots entirely). Only a partner feed/API or a logged-in browser session with written permission would work.

Questions the owner can take to contacts at these platforms:
1. Is there an official product feed (API, CSV or the Shopify feed) we may read daily, and at what rate?
2. Written permission to store and show titles, images and prices in a private client report, including trending/bestseller data?
3. Any real sales signals (units sold, reseller pick counts, "new this week") per product or category?
4. Are dropship prices and dispatch times the same as wholesale; are there quantity price tiers?
5. Which items are exclusive or not yet on Amazon/Flipkart, and can they be tagged?

Sources: deodap.in (home, robots.txt, terms of service, collections.json, sitemap.xml); baapstore.com (same pages); dropshipindia.live (tnc.html, robots.txt); dropdash.co; app.dropdash.co/robots.txt; apps.shopify.com/dropdash and /dropship-india.

## Paid search services (checked, then set aside)

On 14 October the owner confirmed that the program has permission to read Google Maps directly in a browser, and that no paid search service (Google Places API, Brave Search) is wanted. The program does this through its owned-browser Maps read. Recorded for reference only (prices from the providers' pages, updated 8 October 2026): Google Places Text Search with India billing allows 7,000 free Enterprise-field requests a month; Brave Search API gives about 1,000 free a month, then $5 per 1,000. Google's Custom Search JSON API is closed to new customers and ends 1 January 2027; Bing Search APIs retired in August 2025. Ordinary Google web search remains blocked for automated reading ("unusual traffic"), and the program records it as blocked rather than bypassing it.
