# Hosting architecture and future compute options

Updated: 7 October 2026. This document owns where work runs and private delivery/storage choices. The [operator guide](../../operations/local_execution_and_provider_setup.md) owns user steps; the [implementation plan](../../operations/benchmarks/2026-10-03/execution_specification.md) owns exact changes; the [cost plan](../../operations/build_activation_and_costs_2026-10-03.md) owns expenditure.

## 1. Current selected architecture

| Part | Location and role | Actual boundary |
|---|---|---|
| Research/browser/AI/rendering | Owner's local computer; existing saved plans/events and chosen signed-in Luna route. | Finite operations exist; persistent scheduled complete weekly service is planned. Research stops while the computer is off. |
| Private report website | Deployed Cloudflare Worker at `dropshipper.neuraflow.co.in`. | Serves approved uploaded files independently; does not start paid research on client clicks. |
| Private report storage | Existing selected R2 bucket `neuraflow-dropshipping-mr-mu-ad-product-reports`. | Worker binding/private access work; actual report-file transport and scheduled-release acceptance remain open. |
| Sign-in | Whole-hostname Access with Google identity and separate operator/client policies. | Owner access exists; client email is deferred. Local uploader needs its own supported named-owner session. |
| Cost warning | Native enabled US$1 Cloudflare Budget Alert. | Account-wide notification, not project-only spend cap; provider test confirmed, real inbox/threshold unproved. |

Do not rebuild working Access/R2 setup, move unrelated projects, create public bucket access or add device enrolment/Tunnel. The existing NeuraFlow main website remains its own project. A subdomain routes the report service independently; it does not require changing Stock Planner. No www prefix is needed for the canonical report hostname.

## 2. Data, output and private release

Research/history/financial records stay local and append-only; derived indexes are rebuildable. Frozen live facts/workbooks/manifests live under `data/outputs/<issue>/revision-<n>/`; PDFs/HTML/review media under `media/live/reports/<issue>/revision-<n>/`. Private hosted storage contains checked packages and a small release index, not provider secrets or the whole research environment.

The direct Worker trusts verified Access context/audience and explicit allowed identities. Protect every report/download/admin route, not just the homepage. Operator writes are separate from client reads; clients cannot supply bucket keys, external URLs, new indexes or research credentials.

Existing immutable object creation/readback and real R2 ETag conditional index updates are preserved. Stage files, verify bytes/hash, then activate the exact approved revision with server time. A stale writer leaves the prior index/release untouched; a lost response is reconciled, not blindly retried. Keep revisions/history and current withdrawal semantics. [R2 conditional API reference](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/), [trusted Worker Access integration](https://developers.cloudflare.com/workers/configuration/cloudflare-access/).

Scheduled release Tuesday/Thursday/Saturday 09:00 India uses server time after complete approval/activation. The laptop can be off after confirmed upload. Availability is not a client download and must be reconciled into local publication/novelty history. Exact source-valid-until at the intended release instant is checked by the planned new package/approval contract; the server never refreshes source data itself.

Owner browser/dashboard/management-plugin credentials are not the local uploader's supported Access session. The existing finite login/helper/transport checker should prove actual private files when the owner is available; no permanent browser DNS alteration, cookie extraction or HTTPS bypass.

## 3. Footprint, monitoring and backup

Two language editions of a report are still one logical issue and one shared workbook. The current PDFs are roughly 0.32 MB each, but genuine photos and useful 2–3 pages/product will change footprint. Measure new typical package sizes, upload/read operations and retained archive before claiming free-tier fit; do not extrapolate the basic draft's size as the finished design.

Use the existing native billing warning, not another polling service or database. Free-plan settings and the alert do not establish that every future storage operation is free. No automatic paid upgrade, feature activation or deliberate spend test.

Proposed portal archive retention is 12 months, subject to owner/storage review. No automatic deletion is authorised. Keep essential lifetime concept/publication/history/financial evidence and an independent owner-controlled backup; prove restoration. R2 plus the working computer is not automatically an independent complete research backup.

## 4. Fully hosted research is deferred

The selected service does not require hosted research. Moving it later needs actual requirements/permissions and tested alternatives, not a disposable second system.

| Possible future route | Practical tradeoff |
|---|---|
| Keep local research plus small scheduled driver | Lowest new infrastructure; uses current browser/model sign-in. Depends on computer availability; uploaded releases remain independent. Recommended now. |
| Owner-controlled always-on machine | Better research continuity but new running/security/backup cost and browser/account handling. Consider only after measured local limits. |
| Hosted browser/worker/VM | Could run independently, but browser access, long model processes, signed-in route, private credentials and job recovery need explicit provider-supported proof. Not automatically covered by the existing Worker. |

Do not add a paid database/vector store, workflow engine, sandbox framework, custom cost meter or new domain merely to operate one client. Persistent local progression plus simple private hosted files is the current scope.

## 5. Acceptance and future migration prerequisites

Prove current private-file upload/readback, exact approval/conditional activation, owner/client allow-and-deny, server scheduling while the computer is off, retained prior releases, cancellation/revision reconciliation and backup restore. These are separate from an empty-index read or offline website checks.

Before any future compute migration: measure actual work duration, source/block rates, model allowance, package/storage size and operating effort; define funded limits, official credential/session route, restart/uncertain-job recovery and rollback. Preserve original accounts/periods/source dates/history and existing client URL. Regroup on material new scope and expense.
