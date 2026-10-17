# QuickCommerce API connection diagnostics

**17 October 2026:** [Plan R19](../../project/plan-2026-10-17-r19-product-flow-engine.md) uses these checks only for the products picked for a report (section 3.4, stage C), and buying more credit packs is approved: the program tells the owner which pack to buy and keeps working. This note is updated when Plan R19's report assembly is built.

Updated: 16 October 2026. Purpose: the access checker, the private key file and how the running research uses the provider.
Source rules (who is checked, where, and what a result is worth): [product brief](../../product/india_dropshipping_product_opportunity_radar_product_brief.md). Owner steps for buying credits: [operator guide, section 8.3](../local_execution_and_provider_setup.md).

## How research uses it (Plan R10, live since 15 October 2026)

Blinkit, Zepto and Swiggy Instamart are read **only** through this paid provider API (one credit per search on one app in one place, charged only on success). The free browser route was deleted (it never returned verified local rows), and quick commerce is never a discovery source.

- **Who is checked:** only types that pass the checklist and whose Amazon/Flipkart crowding is Low or Medium, checked at planning and again just before sending. One phrase per type (its main reviewed quick-commerce phrase).
- **Where:** `config/research/quickcommerce_locations.json`: Mumbai first on all three apps (3 credits); Delhi NCR and Bengaluru (6 more) only after a relevant Mumbai result. A place answered for a type is not asked again within 30 days, except a type allocated to an upcoming issue, re-checked once its support is older than seven days.
- **What it is worth:** a relevant in-stock listing earns at most 2 of the opportunity score's want points (one city 1, two or more 2). Stock or a price is never sales proof.
- **Held checks** are recorded as "reserved for later" (`QUICKCOMMERCE_RESERVED_*`), never as failures.
- **Credits:** before each batch the free balance check runs once through the spend watch. A zero or unknown balance makes the route wait (`QUICKCOMMERCE_CREDITS_EXHAUSTED`) while all other research continues; a later positive balance clears it (after a provider "no credits" refusal, only a risen balance or a new pack). New packs are read from the provider's pack list with their own expiry; expiry is flagged three days ahead and usage at 50/75/90/100%. An interrupted paid search is never repeated. The program never buys a pack.
- **Running it:** the research programme uses this route only with `--funded-quickcommerce-fallback` and `--cooperative-turns`, both already in the installed Windows task's allowed flags. Places come from the settings file; there are no location options.

Provider prices (checked 14 October 2026, `config/research/spend_watch.json`): one-off packs that expire 30 days after purchase and do not refill: ₹500 = 2,500 credits, ₹1,800 = 10,000, ₹7,500 = 50,000. On 14 October 44 of the 100 free sign-up credits were left; that is a dated observation, not the current balance. The owner meter (`scripts/run_programme.py --meter`) and the desktop app show the current balance and percent used.

## Implemented checker

`.venv/bin/python scripts/check_quickcommerce_connection.py --live`

One authenticated GET to `https://api.quickcommerceapi.com/v1/credits`, documented as free. The check itself is the library's `QuickCommerceClient.credits` (`src/product_radar/quickcommerce.py`), the same call the research makes before each batch; the script only prints the validated totals (available, used, active packs) and records nothing. X-API-Key is in the header, never the URL. Without `--live` it loads no credentials and makes no request. The client refuses redirects, bounds response size and network wait, does not auto-retry, and emits fixed messages and validated aggregate integers only.

The public supported-platforms endpoint is not authentication proof. Product searches (`/v1/search`) consume credits even though they use GET; never use them as free connection probes or blindly retry lost responses. [Provider documentation](https://quickcommerceapi.com/docs).

Offline tests (`tests/test_quickcommerce*.py`, `tests/test_spend_watch_quickcommerce.py`) use fictional keys and cover key-file parsing, headers, balances and packs, invalid responses, redirects, network/HTTP failures, secret-safe errors, the admission and location rules, credit accounting and the exhausted-credit wait. Successful credit access does not prove each platform, place or product works.

## Private configuration

Existing file: `config/local/quickcommerce.env`, one `QUICKCOMMERCE_API_KEY` line. Empty public example: `config/quickcommerce.env.example`. Do not read or edit the Apify secret as part of a commerce check.

Only for a genuine authorised replacement, guide the owner to the signed-in provider dashboard and the existing private file. Never request key text or screenshots in chat, invent permission checkboxes, or save an unprotected .txt copy. The owner reports completion only; the agent checks privately without printing raw diagnostics.

Ignore rules are tested in a temporary repository; they are not encryption or exclusions from whole-project sharing. Keep secrets and credit-pack identifiers out of reports and logs.
