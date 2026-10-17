# Apify connection diagnostics

**17 October 2026:** under [Plan R19](../../project/plan-2026-10-17-r19-product-flow-engine.md) Apify is kept only as the fallback for Facebook Ad Library searches when the free browser read is blocked, within the month's free allowance; the deeper-Meta read is deleted. This note is shortened to match when that fallback is built.

Updated: 7 October 2026. Purpose: existing checker behavior and intended token scope. Current work is planning only; three-account routing is specified but not implemented.
Current account/funding procedure: [account guide](apify_environment_handover.md).
Current service/budget: [operator plan](../local_execution_and_provider_setup.md), cost plan.

## Current access

Initial authenticated run-list check passed on 3 October at 08:35:56 UTC. Genuine private binding, first request preflight, run and output access passed on 4 October. The first sample's data quality is accepted; remaining source/production acceptance is pending. Profile/monthly-usage reads can return 403 under the working restricted token.

Initial setup is complete. Trends has a verified runtime request-queue permission error. The owner-supplied 5 October screenshot shows Limit token permissions enabled; Actors Read/Run/List runs; Tasks, Schedules and general Storages empty. It does not show Running Actors, default-run-storage access or full-permission-Actor controls. This is a displayed-settings observation, not a new authenticated runtime test or proof of the exact denied operation. No settings were changed. Measured results.

Browser Trends now works in bounded chart/export tests and remains the selected route. No new Apify Trends attempt or permission repair is authorised. Only after browser alternatives fail and a fallback is discussed would missing lower settings be inspected. Recovery navigation: Settings → API & Integrations → Edit beside the existing project token → Limit token permissions → lower Running Actors section. These are token-editor controls, not items on the main API page. Never reveal a token or save a speculative permission change.

## Implemented checker

From repository root:
`.venv/bin/python scripts/check_apify_connection.py --live`

One authenticated GET to `https://api.apify.com/v2/actor-runs?limit=1&desc=true`, Authorization header only. It reads existing metadata and never starts an Actor. Without --live it makes no request and does not load the key.

The checker refuses redirects, limits response bytes, applies a twenty-second network timeout and no automatic retries. It prints only a timestamp/fixed safe result, never token, raw response or secret-bearing exceptions. Offline tests use fictional keys/mocked responses. A networking failure is not proof of invalid credentials; HTTP 200 on this route is not proof of billing balance or every permission.

[Official run-list endpoint](https://docs.apify.com/api/v2/actor-runs-get).

## Intended restricted scope

| Control | Intended setting |
|---|---|
| Account-level Actors | Read, Run, List runs |
| Tasks, Schedules, general Storages | None |
| Running Actor access mode | Restricted |
| Default run storage access | Enabled; includes read/write to corresponding run storage |
| Full-permission Actors | Disabled |

This is an account-level scope, not a per-Actor allowlist. The upper settings match the supplied screenshot; lower runtime settings have not been visually confirmed in that screenshot or enumerated by the checker. Successful run/output access proves the exercised route only. Do not assume API abort/restart permission. Use provider runtime/monetary bounds and guide an owner Console stop for a specific job if needed. Resource-specific narrowing can be evaluated once chosen tools are proven, without breaking needed recovery access.

The intended runtime scope above is insufficient for the exercised Trends run or differs from the actual saved settings; the exact distinction is unverified. Its log confirms restricted mode and queue denial, not which endpoint failed. Default-storage access and creating additional storage are distinct permissions. If proceeding with this fallback, ask only for the missing lower section before proposing a minimal correction. Keep the intended Full-permission Actors setting disabled; do not grant general storage Write or Full access as a convenience fix.

[Token/Actor execution controls](https://docs.apify.com/integrations/api), [Actor permission levels](https://docs.apify.com/actors/development/permissions).

## Secret storage

Existing private key: `config/local/providers.env`, APIFY_TOKEN line. Empty public template: `config/providers.env.example`. Never paste a token into chat, command arguments, URLs, docs or screenshots; don't print/source the secret file.

Only if a genuine owner-approved credential replacement is needed, guide editing the existing private file without a .txt copy. Preserve unrelated provider entries and binding/history; do not reset money.

Ignore rules cover config/local/ and conventional secrets; tests check those rules in a temporary Git repository without real credentials. This checkout has no usable Git worktree, so tracked-file status is not certified. Ignore rules are not encryption or protection against whole-folder ZIP/cloud sharing.
