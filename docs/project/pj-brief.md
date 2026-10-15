# Project brief — India Product Opportunity Radar

**Project stage: Development and testing** (confirmed by the owner on 13 October 2026). Only the owner and the agent use the application; no client has received a live report. Precautions that exist only to protect real users (staged rollouts, careful migration of old states, backward compatibility) are not planned.

**Version control:** GitHub repository `github.com/neuraflowindia/dropshipper-agent`, branch `main` (attached 13 October 2026). Commits use the private no-reply author address set in the repository's local Git settings. Research data, reports, credentials and `docs/project/handoff.md` are never committed.

## What the product is

NeuraFlow runs an India-focused product research service for one dropshipping/private-label client. A local Python programme searches agreed websites, judges narrowly defined product types against a ten-point checklist, looks for possible suppliers and photos, and produces private report sets (English PDF, Roman-Hinglish PDF and one five-sheet Excel workbook). A small online private portal (Cloudflare Worker in `portal/`) stores approved packages for owner and client download.

Requirements, categories, pricing, selection and report rules: [product and service contract](../product/india_dropshipping_product_opportunity_radar_product_brief.md).

## Reading order for a new agent

1. This brief.
2. [Project tracker](pj-tracker.md): current completion checks, research counts, remaining work and open choices.
3. [Plan R10, 14 October](plan-2026-10-14-speed-sources-and-selection.md): the owner's decisions of 14 October and the agreed programme (signed-in research browser, dropshipping platforms, quick commerce, Amazon/Flipkart crowding check, larger product map, spend watch). Read before building.
4. [Documentation index](../README.md): which document owns which topic.
5. Then, as the task needs: [implementation record](../operations/implementation_2026-10-11.md), [execution specification](../operations/benchmarks/2026-10-03/execution_specification.md), [operator guide](../operations/local_execution_and_provider_setup.md), [command reference](../operations/benchmarks/2026-10-03/offline_runner_guide.md).

## Code map

| Area | Location |
|---|---|
| Research, judgment, supplier, report and backup logic | `src/product_radar/` |
| Entry points (programme, launcher, checks) | `scripts/` — main loop `scripts/run_programme.py`; read-only status `--activity`; owner meter since the fresh start (time, money, counts, reports, errors/waits) `--meter` |
| Tests (Python `unittest`) | `tests/`; portal tests are `portal/*.test.mjs`, run with `node --test portal/*.test.mjs` from the repository root. Portal tests that build their fixtures with the Python code start the interpreter named by `PRODUCT_RADAR_PYTHON`, else `.venv/bin/python` at the repository root (`portal/python.test-helper.mjs`); set it when the folder has no `.venv` of its own (for example a build copy) |
| Private portal | `portal/` |
| Research records, money ledger, receipts, reports (not for version control) | `data/`, `media/live/` inside `/home/anan/neuraflow-dropshipper-data/` on the Linux disk (named in `config/local/runtime_storage.json`); daily backups stay in the project's `data/backups/`; everything before the 13 October 2026 fresh start is in `archive/2026-10-13-before-fresh-start/` |
| Credentials | ignored `config/local/` only |

## Standing boundaries

Ordinary research, focused checks and owner sign-in renewal are approved. On 14 October the owner also approved reading the dropshipping platforms (now three: DeoDap and BaapStore from their public catalogues, Dropdash signed in; Dropship India was dropped on 15 October; their paid plans are never bought), IndiaMART (signed in) and Google Trends, spending the existing quick-commerce credits and the free monthly Apify allowance automatically, and deploying the portal update when the first report set is ready ([Plan R10](plan-2026-10-14-speed-sources-and-selection.md)). The Plan R10 switch-over was approved and done on 15 October; the whole local test suites are covered by the Plan R10 approval (A1). Client publication or access changes, supplier contact, paid upgrades and credit purchases need separate owner approval. Visual redesign is outside the present work.
