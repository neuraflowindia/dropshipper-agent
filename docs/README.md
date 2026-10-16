# Project documentation

Start with the [project brief](project/pj-brief.md) (stage, purpose, reading order), then the [project tracker](project/pj-tracker.md) for the latest verified position and remaining work. The tracker is a dated snapshot, not a live dashboard.

## Document ownership

Each topic has one main home. Other documents link to it rather than repeat its policy, numbers or history.

| Read this for… | Main document |
|---|---|
| Client service, categories, ten-point checklist, selection priorities, supplier requirements, report content and languages | [Product and service contract](product/india_dropshipping_product_opportunity_radar_product_brief.md) |
| Project stage, purpose, reading order and code map | [Project brief](project/pj-brief.md) |
| Current completion score, dated research counts, remaining work and unresolved choices | [Project tracker](project/pj-tracker.md) |
| Implementation requirements, dependencies, record contracts, migrations and acceptance criteria; current source routes and retired routes (section 30) | [Execution specification](operations/benchmarks/2026-10-03/execution_specification.md) |
| Implemented operating repairs, technical constraints, loading and verification evidence; what Plan R10 built, its wait codes and settings files (R10) | [Implementation record](operations/implementation_2026-10-11.md) |
| Commercial limits, account funding, monthly cost plan (Apify free allowance, quick-commerce credits, AI money guard), cost attribution and low-yield forecasts | [Cost and capacity plan](operations/build_activation_and_costs_2026-10-03.md) |
| Product map (catalogue), heading order, wider and platform discovery, search counts and coverage accounting | [Catalogue and search accounting](operations/catalogue_discovery_and_coverage.md) |
| Historical source trials, measured charges and older report/download checkpoints | [Benchmark evidence](operations/benchmarks/2026-10-03/benchmark_record.md) |
| Owner setup and operating journey; signing in to research sites, Apify month renewal, quick-commerce packs, the R10 cutover and the spend watch | [Operator guide](operations/local_execution_and_provider_setup.md) |
| Exact existing commands, collection and recovery options | [Command reference](operations/benchmarks/2026-10-03/offline_runner_guide.md) |
| Desktop shortcut, Start/Stop, sign-in button, sign-in/Trends/platform and spend lines, notification tray, installation and recovery | [Windows launcher](operations/windows_desktop_launcher.md) |
| Provider-specific credentials and setup | [Apify connection](operations/providers/apify_connection.md), [account handover](operations/providers/apify_environment_handover.md), [Luna connection](operations/providers/codex_luna_connection.md), [quick commerce](operations/providers/quickcommerce_connection.md) |
| Local versus hosted architecture and deferred alternatives | [Hosting decision](research/hosting/2026-10-03_local_vs_hosted.md) |
| Agreed programme after the owner's 14 October decisions (sources, crowding check, product map, spend watch): build state per package, the lead's build decisions and what waits for the ranking design | [Plan R10](project/plan-2026-10-14-speed-sources-and-selection.md) |
| Indian dropshipping platforms (DeoDap, BaapStore, Dropdash; Dropship India dropped 15 October): research behind the built platform routes; paid search services set aside | [Platform research](research/sources/2026-10-14_dropshipping_platforms_and_search_services.md) |
| Plain-language explanation of how the program works | [How it works](project/how-it-works.md) |
| Proposed client-report redesign (plan, high-fidelity mock-ups and renders, waiting for owner approval; not built) | [Report redesign, 16 October](client-preview/design/report-redesign-2026-10-16/plan.md) |
| Agreed programme of 16 October: second fresh start, year-long product map, long-run durability | [Plan R11](project/plan-2026-10-16-fresh-start-and-year-long-research.md) |

## Scope and precedence

The approved work covers the connected nonvisual application, including two actual updated bilingual private report sets. Visual redesign, client publication and local/hosted release gates remain outside the present work. The [project tracker](project/pj-tracker.md#scope-and-remaining-work) records the current boundary.

Use the product contract for requirements, the specification for mechanisms, and dated receipts for proof. A plan, passing code check, loaded change, completed research result, private download and client publication are different outcomes. Historical review/checkpoint documents do not override later accepted decisions or establish current running state.

## Design references

The approved manufactured preview is a design reference, not live product evidence. Preserve its source, assets and client outputs.

- [Preview blueprint](client-preview/client_preview_package_and_demo_data_blueprint.md)
- [Excel reference](client-preview/production/session_p4_sample_excel_production_plan.md)
- [Portrait PDF reference](client-preview/production/session_p5_portrait_pdf_production_plan.md)
- [Brochure reference](client-preview/production/session_p6_brochure_revision_plan.md)

## Filing and maintenance

| Content | Location |
|---|---|
| Requirements, instructions, implementation summaries and reviews | Categorized folders under `docs/` |
| Where `data/` and `media/live/` physically live | Since 13 October 2026: `/home/anan/neuraflow-dropshipper-data/` on the Linux disk, named in ignored `config/local/runtime_storage.json`. Paths below are logical project paths inside that folder. Daily backups stay in the project's own `data/backups/` on D:. |
| Output made before the 13 October 2026 fresh start | `archive/2026-10-13-before-fresh-start/` (moved unchanged, never deleted) |
| Original research, finance, execution logs and proof receipts | `data/`; link exact evidence rather than copying its full narrative |
| Frozen report facts, spreadsheets and manifests | `data/outputs/<issue>/revision-<n>/` |
| Live report PDFs, HTML and review media | `media/live/reports/<issue>/revision-<n>/` |
| Manufactured client-preview outputs | `outputs/client-preview/` |
| Credentials and private identities | Ignored `config/local/`; never in documentation or client files |

Update current status in the project tracker; update technical behavior in the implementation record; change requirements only in their owning contract. Keep dates, denominators, limitations and evidence links. Do not append repeated session narratives or duplicate status tables to the index/specification.

Documentation checks: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests .venv/bin/python -m unittest test_documentation_layout -v`. These are offline checks, not provider, billing or release proof.
