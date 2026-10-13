# QuickCommerce API connection diagnostics

Updated: 4 October 2026. Purpose: existing access checker, credential route and remaining product tests.
Current limits: [cost plan](../build_activation_and_costs_2026-10-03.md).
Source trial: [benchmark plan](../benchmarks/2026-10-03/benchmark_plan.md).

## Current access

Authenticated credit check passed at 08:54:25 UTC on 3 October: 100 available, zero used, one active pack. This is a dated observation, not guaranteed current balance. No product-data coverage or charged-call behavior is accepted from that check.

Key saving is complete. No signup/replacement step remains and no commerce pack is purchased merely to prove access. Verify useful source behavior and actual expiry/need before the owner buys credits.

## Implemented checker

`.venv/bin/python scripts/check_quickcommerce_connection.py --live`

One authenticated GET to `https://api.quickcommerceapi.com/v1/credits`, documented as free. X-API-Key is in the header, never URL. Default invocation does not load credentials or request data. The checker refuses redirects, bounds input/response/network wait, does not auto-retry, and emits fixed messages/validated aggregate integers only.

The public supported-platforms endpoint is not authentication proof. Product searches can consume credits even though they use GET; do not use them as free connection probes or blindly retry lost responses. [Provider documentation](https://quickcommerceapi.com/docs).

Offline tests use fictional keys and cover line endings, malformed/duplicate keys, headers, balances, invalid responses, redirects, network/HTTP failures and secret-safe errors. Successful credit access does not prove each platform/location/product works.

## Private configuration

Existing file: `config/local/quickcommerce.env`, QUICKCOMMERCE_API_KEY line. Empty public example: `config/quickcommerce.env.example`. Do not read/edit the Apify secret as part of a commerce check.

Only for a genuine authorised replacement, guide the owner to the signed-in dashboard and existing private file. Never request key text or screenshots in chat, invent permission checkboxes, or save an unprotected .txt copy. The owner reports completion only; Codex checks privately without printing raw diagnostics.

Ignore rules are tested in a temporary repository; they are not encryption or exclusions from whole-project sharing. Keep secrets and credit-pack identifiers out of reports and logs.
