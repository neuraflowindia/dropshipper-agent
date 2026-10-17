# Project documentation

Start with the [project brief](project/pj-brief.md) (stage, purpose, reading order, code map), then the [project tracker](project/pj-tracker.md) (where things stand). The tracker is a dated snapshot, not a live dashboard.

## Which document owns what

Each topic has one home. Other documents link to it instead of repeating it.

| Read this for… | Document |
|---|---|
| Project stage, purpose, reading order, code map, where things run | [Project brief](project/pj-brief.md) |
| Current position, live numbers, what is next | [Project tracker](project/pj-tracker.md) |
| **The agreed new design and the build plan** (ideas, removals, points, cross-checks, reports, never-block, AI cost and credits, sign-ins, phases and tasks) | [Plan R19, 17 October](project/plan-2026-10-17-r19-product-flow-engine.md) |
| Client service, categories, the ten-point checklist, report content and languages (rewritten in Plan R19 phase 7; where it differs, Plan R19 applies) | [Product and service contract](product/india_dropshipping_product_opportunity_radar_product_brief.md) |
| Owner steps: starting and stopping, signing in, buying quick-delivery credits, recovery | [Operator guide](operations/local_execution_and_provider_setup.md) |
| The desktop window: Start/Stop, sign-in buttons, notifications, installation | [Windows launcher](operations/windows_desktop_launcher.md) |
| AI through the owner's ChatGPT subscription (`codex exec`) | [Codex connection](operations/providers/codex_luna_connection.md) |
| Quick-delivery checks (Blinkit, Zepto, Instamart) and credit packs | [Quick-delivery connection](operations/providers/quickcommerce_connection.md) |
| Apify (kept only as the Facebook fallback under Plan R19) | [Apify connection](operations/providers/apify_connection.md), [Apify accounts](operations/providers/apify_environment_handover.md) |
| The client-report design (built on 16 October, branch `redesign`; merged in Plan R19 phase 0) | [Report redesign plan](client-preview/design/report-redesign-2026-10-16/plan.md) |
| The frozen August client-preview package (generated guides; a test keeps them byte-identical) | `client-preview/guides/` |

Older plans, audits, reviews, benchmark records and the long implementation record were removed on 17 October 2026 because the new design replaces what they describe. Git history keeps them (last commit with all of them: `02c8326`).

## Filing rules

| Content | Location |
|---|---|
| Research records, reports, run logs (never committed) | `/home/anan/neuraflow-dropshipper-data/` on the Linux disk (named in ignored `config/local/runtime_storage.json`); daily backups in the project's `data/backups/` |
| Earlier research and output | `archive/` in the project and `/home/anan/neuraflow-dropshipper-archive/` (moved, never deleted) |
| Credentials and private identities | Ignored `config/local/` only; never in documents or client files |
| The private agent handoff | `docs/project/handoff.md` (never committed) |

Update the current position in the tracker; technical behaviour in the implementation notes that Plan R19 starts in phase 1; requirements only in their owning document. Keep dates and denominators; do not append session narratives.

Documentation check: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_documentation_layout -v`.
