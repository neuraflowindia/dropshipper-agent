# Project brief — India Product Opportunity Radar

**Project stage: Development and testing** (confirmed by the owner on 13 October 2026). Only the owner and the agent use the application; no client has received a live report. Precautions that exist only to protect real users (staged rollouts, careful migration of old states, backward compatibility) are not planned.

**Version control:** the project is not yet attached to a Git repository (the `.git` folder is empty). The owner will connect one later; until then no commits or pushes are possible.

## What the product is

NeuraFlow runs an India-focused product research service for one dropshipping/private-label client. A local Python programme searches agreed websites, judges narrowly defined product types against a ten-point checklist, looks for possible suppliers and photos, and produces private report sets (English PDF, Roman-Hinglish PDF and one five-sheet Excel workbook). A small online private portal (Cloudflare Worker in `portal/`) stores approved packages for owner and client download.

Requirements, categories, pricing, selection and report rules: [product and service contract](../product/india_dropshipping_product_opportunity_radar_product_brief.md).

## Reading order for a new agent

1. This brief.
2. [Project tracker](pj-tracker.md): current completion checks, research counts, remaining work and open choices.
3. [Documentation index](../README.md): which document owns which topic.
4. Then, as the task needs: [implementation record](../operations/implementation_2026-10-11.md), [execution specification](../operations/benchmarks/2026-10-03/execution_specification.md), [operator guide](../operations/local_execution_and_provider_setup.md), [command reference](../operations/benchmarks/2026-10-03/offline_runner_guide.md).

## Code map

| Area | Location |
|---|---|
| Research, judgment, supplier, report and backup logic | `src/product_radar/` |
| Entry points (programme, launcher, checks) | `scripts/` — main loop `scripts/run_programme.py`; read-only status `--activity`; owner meter since the fresh start (time, money, counts, reports, errors/waits) `--meter` |
| Tests (Python `unittest`) | `tests/`; portal tests are `portal/*.test.mjs` |
| Private portal | `portal/` |
| Research records, money ledger, receipts, reports (not for version control) | `data/`, `media/live/` inside `/home/anan/neuraflow-dropshipper-data/` on the Linux disk (named in `config/local/runtime_storage.json`); daily backups stay in the project's `data/backups/`; everything before the 13 October 2026 fresh start is in `archive/2026-10-13-before-fresh-start/` |
| Credentials | ignored `config/local/` only |

## Standing boundaries

Ordinary research, focused checks and owner sign-in renewal are approved. Client publication or access changes, supplier contact, paid upgrades, credit purchases and the broad local/hosted release suites need separate owner approval. Visual redesign is outside the present work.
