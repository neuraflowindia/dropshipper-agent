# Project brief — India Product Opportunity Radar

**Project stage: Development and testing** (confirmed by the owner on 13 October 2026). Only the owner and the agent use the application; no client has received a report. Precautions that exist only to protect real users (staged rollouts, careful migration of old states, backward compatibility) are not planned.

**Version control:** GitHub repository `github.com/neuraflowindia/dropshipper-agent`, branch `main` (attached 13 October 2026). Commits use the private no-reply author address set in the repository's local Git settings. Research data, reports, credentials, `.env`, `dropdash.txt` and `docs/project/handoff.md` are never committed.

## What the product is

NeuraFlow runs an India-focused product research service for one dropshipping client. A local Python program finds product ideas (mainly from the dropshipping stores DeoDap, BaapStore and Dropdash, and from Facebook ads), checks each idea on a few other places (Google Trends, Amazon, Flipkart; quick-delivery apps and suppliers for the picked ones), ranks everything by points, and produces three report sets a week (English PDF, Roman-Hinglish PDF, one Excel workbook) of 10 main picks plus up to 5 "other suggestions". The owner approves each report; a small private website (Cloudflare Worker in `portal/`) then publishes it for owner and client download.

**Where things run:** research, AI calls and report rendering run on the owner's computer (WSL plus Windows Chrome), so research stops while the computer is off. The private website runs on Cloudflare at `dropshipper.neuraflow.co.in` with report files in a private R2 bucket, Cloudflare Access (Google sign-in) with separate owner and client lists; once an approved report is uploaded, it is released on schedule (Tuesday, Thursday, Saturday, 09:00 India time) even if the computer is off. The client's access e-mail is deferred.

**Direction:** on 17 October 2026 the owner agreed a new design: suggest likely winners and rank them, never block for missing information, two-year horizon, automatic Dropdash sign-in, AI on the plan allowance and then paid credits within a soft budget per report. The build plan is [Plan R19](plan-2026-10-17-r19-product-flow-engine.md); it waits for the owner's approval.

## Reading order for a new agent

1. This brief.
2. [Project tracker](pj-tracker.md): where things stand and what is next.
3. [Plan R19](plan-2026-10-17-r19-product-flow-engine.md): the agreed design (section 3) and the build plan (sections 4–5). Read it all before building.
4. [Documentation index](../README.md): which document owns which topic.
5. As the task needs: the [product contract](../product/india_dropshipping_product_opportunity_radar_product_brief.md) (checklist criteria and report languages; superseded where Plan R19 differs), the [operator guide](../operations/local_execution_and_provider_setup.md), the [Windows launcher](../operations/windows_desktop_launcher.md), the provider notes under `docs/operations/providers/`.

## Code map (today; Plan R19 replaces most of the research engine)

| Area | Location |
|---|---|
| Research, AI, report and backup code | `src/product_radar/` (Plan R19 adds `src/product_radar/flow/` and deletes the old round-and-desk engine) |
| Entry points | `scripts/` — main loop `scripts/run_programme.py`; read-only trace `scripts/research_trace.py`; sign-in window `scripts/research_sign_in.py`; PDF printing `scripts/render_live_pdf.py`; private draft staging `scripts/stage_private_draft.py` |
| Windows desktop window and installer | `scripts/windows/` (C# tray app, Build/Install/Test scripts) and `src/product_radar/desktop_control.py` |
| Tests | `tests/` (Python `unittest`); portal tests `node --test portal/*.test.mjs` from the repository root (set `PRODUCT_RADAR_PYTHON` when the folder has no `.venv`, e.g. a build worktree) |
| Private website | `portal/` |
| Settings | `config/research/`, `config/ai/`, `config/service.json` (Plan R19 adds `config/flow/`) |
| Runtime data (never committed) | `/home/anan/neuraflow-dropshipper-data/` (research records, store catalogues, run logs, reports); daily backups in the project's `data/backups/`; older research in `archive/` and `/home/anan/neuraflow-dropshipper-archive/` |
| Credentials | ignored `config/local/` only; the owner's own `.env` and `dropdash.txt` in the project root are never opened by agents |

## Standing boundaries

Approved and still in force: commits and pushes; focused tests and the whole local suites; clean stop/start of research; Windows app rebuild/reinstall; ordinary research; reading DeoDap, BaapStore, Dropdash, IndiaMART and Google Trends (the owner has the platforms' permission — never ask for proof); spending the existing quick-delivery credits and the free Apify allowance; deploying the private website update when the first report set is ready. The owner's 17 October answers also approve the program signing in to Dropdash automatically, research continuing on paid Codex credits within the per-report budget, and the owner buying more quick-delivery packs when the program asks. Needs the owner: approval of Plan R19 (asked in the handoff), client publication or access changes, supplier contact, any purchase made by the agent or the program. No security-only work is wanted (owner, 17 October).
