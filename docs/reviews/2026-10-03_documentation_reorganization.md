# Local-operation decision and documentation reorganization

Date: 3 October 2026.
Status: Complete; post-change verification passed.

## Request and boundaries

The owner selected local weekly operation, accepting even double or triple the discussed 90–120-minute availability window. They requested a durable hosting-research record, all project documents under categorized `docs/` folders, a clean root, and explicit next-step instructions including how to perform each step.

This work records decisions and reorganizes documentation; it does not build the live research application, configure credentials, make paid calls, change client-facing content or provision hosting.

## Changes

- Moved seven root planning documents under `docs/product/`, `docs/operations/` and `docs/client-preview/`.
- Moved the two generated internal presentation guides from `outputs/client-preview/` to `docs/client-preview/guides/`, preserving their content.
- Moved the written image-prompt record to `docs/client-preview/design/`. All image assets and the supplied brand-colour reference remain in `media/`.
- Added the [index and filing rules](../README.md), [hosting research](../research/hosting/2026-10-03_local_vs_hosted.md) and [local-operation and provider micro-steps](../operations/local_execution_and_provider_setup.md).
- Updated the current brief and activation record with the local decision, unmeasured runtime caveat, backup boundary and no-hosting cost assumption.
- Updated generator, renderer and validator defaults for relocated internal guides; all three accept the same `--docs-dir` override.
- Updated manifest paths and the dependent image-manifest checksum. No image bytes, report layout or sample data needed to change.
- Retained approved client PDF and Excel locations under `outputs/client-preview/`. Requirements and tool entry files remain at root; documentation is under `docs/`.

## Review findings and corrections

1. Moving only root documents would have left internal guides outside `docs/`, and regeneration would have recreated them there. Corrected both existing locations and all three relevant tool defaults.
2. The image prompt's path is embedded in an asset manifest whose checksum is embedded in the report manifest. Corrected both references without rerendering approved artifacts.
3. The brief's proposed application layout would have encouraged new root documentation and benchmark notes beside raw data. Updated the proposed hierarchy and split written benchmark reviews from response files.
4. Earlier runtime discussion could be mistaken for a measured limit or a requirement for daily activity. Explicitly recorded weekly manual operation, unmeasured runtime, and the owner's acceptance of longer runs.
5. A free-hosting comparison could be mistaken for provider selection or a promise of zero cost. Recorded the rejected/deferred state, account-wide allowances, supporting charges and migration prerequisites.
6. Local storage could be mistaken for an independent backup. Recorded the need to select a separate destination and test restoration before first live delivery.
7. Preview documents contain historical pre-sale gates. Preserved that history and added current-status precedence instead of silently rewriting already-approved sales content.

## Verification

Before changes, the existing checks passed: P6 package 1,027 checks; report reconciliation 1,529 checks; dataset/workbook 835 checks. SHA-256 fingerprints of all four current PDFs and the workbook were recorded before edits.

Post-change results:

| Check | Result |
|---|---|
| `python -m unittest discover -s tests -v` | Four regression tests passed: document placement, relative links, staged regeneration, and consistent document-directory options. |
| `scripts/validate_client_preview_package.py` | 1,027 checks passed, including relocated guides and package manifest fingerprints. |
| `scripts/validate_sample_pdf.py` | 1,529 checks passed, including dataset/workbook/image/report reconciliation. |
| `scripts/validate_preview_dataset.py --workbook outputs/client-preview/NeuraFlow_Product_Opportunity_Evidence_Illustrative_2026-08-15.xlsx` | 835 checks passed. |
| SHA-256 before/after comparison | All four current PDFs, including the stitched preview, and the workbook are byte-for-byte unchanged. |
| Staged P6 generation in a temporary directory | HTML, CSS, offer source and both internal guides exactly match the approved existing content; actual client outputs were not regenerated. |
| Root and obsolete-path inspection | No narrative Markdown files remain at root, in `outputs/` or in `media/`; no active references remain to the relocated guides' or prompt record's old paths. |

Commands used the existing `.venv/bin/python` from the repository root. These are offline checks: no provider calls, hosting account setup, charges or client sending occurred. No production-readiness claim follows from passing preview/documentation tests.

## Remaining work

Apify account access is the next user-only step. Follow the [local setup plan](../operations/local_execution_and_provider_setup.md) one provider at a time, then perform capped evidence/cost benchmarks before the full application build. No deployment or paid hosting action is required.
