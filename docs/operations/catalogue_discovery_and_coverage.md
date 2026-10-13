# Product research breadth, fresh discovery and exact search accounting

Updated: 9 October2026. This is the operating contract for existing N19/P1a research and its planned strengthening. The
[execution specification](benchmarks/2026-10-03/execution_specification.md)
owns the complete programme; this document describes its source-grounded
catalogue preparation, current finite discovery and coverage measurements. [Section26](benchmarks/2026-10-03/execution_specification.md#26-narrow-type-recommendations-and-sustainable-research-for-2000-reports) specifies the not-yet-implemented narrow-type/renewable-free-research design. This document does not
approve a different provider, paid collection or visual redesign.

## 1. What the numbers mean

Keep five different counts separate. A public page can contain repeated sellers,
irrelevant services and known products; downloaded cards are not fresh products.

| Count | Meaning | What it does not mean |
|---|---|---|
| Public reads | Actual bounded page visits with saved navigation and source receipts. | Every result was a useful product. |
| Raw cards | Rows extracted from those pages within the saved limit. | Distinct types, qualifying products or fresh report inventory. |
| Grounded research examples | Exact original product/category excerpts staged for C planning. | Accepted new families, confirmed demand or suppliers. |
| Accepted family/phrase choices | Admitted functional directions and source-specific wording. | Each wording produces a different product, or has never been visited. |
| New admitted products | Existing A description and B identity work resolved a genuinely new concept. | Checklist/market/supplier/photo work is complete or a report is ready. |

## 2. Actual independent upfront research on 8 October 2026

The new side study completed **22/22 usable public Amazon India reads**, with
HTTP 200 and 30 cards per page: **660 raw cards**. It launched no Apify Actor,
made no model call, imported no canonical acquisition and changed no product
assessment. The original free page, owned-browser execution proof, parser
result and request are retained under `data/research/catalogue-public/`.

The pages cover kitchen tools, specialist preparation tools, baking, food
storage, home decoration, storage, furnishing, cleaning, savings boxes, indoor
fountains, baby care/feeding/toileting/safety/nursery, luggage, travel
accessories, outdoor recreation, camping and sleeping equipment. General
Sports navigation was inspected as exploratory outdoor context; this does not
silently change the installed Travel and Outdoor discovery department to
Sports. The production mapping remains `luggage` until a new scope is
explicitly connected and proved. A separate 23rd mapping check found that the
old guessed Home and Decor path, `home`, returned an undefined category and no
cards despite HTTP 200. Future discovery now uses the actually observed
`kitchen/1380374031` Home & Décor node; old frozen requests/receipts retain their
original meaning. This failed-scope check is separate from the 22 useful page
reads and is not treated as weak decor demand. Across all 23 reads the usable
page ratio is therefore **22/23 = 95.7%**, not 100%; all 23 returned HTTP 200.

Actual child-category links were read only from source navigation containers,
not inferred from arbitrary header/footer links. Examples include the kitchen
page's 20 children, kitchen-tools page's 37, home-decoration page's 26,
baby-products page's 15, camping/hiking page's 22 and travel-accessories page's
15. These are page-local directions with possible overlap, not an added family
total.

The exact staging plan is
`config/research/catalogue_branch_inventory_2026-10-08.json`. Its completed
first preparation has **28/28 branches, 145 examples and zero failures**:

| Category | Existing branches covered | Staged examples | Nature of examples |
|---|---:|---:|---|
| Home and Decor | 7 | 37 | Display/reminders, damage-free mounting, drafts, fragrance, water features, household organisation and lighting. |
| Baby Products | 7 | 36 | Hands-free nursing, first feeding, cleaning, toileting, nursery protection and childproofing tasks. |
| Home and Kitchen | 7 | 35 | Distinct preparation mechanisms, overflow/drying, dispensing, draining, carrying, storage and maintenance. |
| Travel and Outdoor | 7 | 37 | Packing, power/security, comfort, weather, sleeping, campsite food/water and equipment care. |
| Total | 28 | 145 | Source-grounded planning inputs, not 145 fresh product types. |

Some examples are actual category labels rather than individual product cards.
Some are already covered by existing families. C must label the former as
research directions and merge the latter without resetting history. It may
return no useful addition. New families require a practical difference in
function, mechanism or intended use, not another seller, colour, pack, price or
marketing adjective.

Source quality matters even when access succeeds. The actual retained Amazon
HTML included an LPG-booking service on home/nursery lists and a credit-card
service on luggage. These were genuine source rows, not a parser-generated
join. They remain in raw captures, but are omitted from staged product
examples. A/B product/relevance work must still precede use of a rank as a
product signal.

## 3. Preparation, interpretation and admission

`catalogue_seed.py` adds one isolated C mode, `catalogue_branch_seed`, with
prompt version `research-c-catalogue-seed-v1`. It does not reinterpret original
C-v1 or product-query-profile jobs.

| Layer | Exact work | Cost and continuation |
|---|---|---|
| Stage original material | `stage_marketplace_materials` replays the original request, receipt, snapshot, parser and hashes; requires actual navigation, HTTP 200 and complete retained text. `stage_page_materials` requires exact source excerpts and saves immutable page/material files. | Local work only. Altered/unreadable material is deferred, not supplied as invented evidence. |
| Prepare a branch | `prepare_catalogue_branch_inputs` supplies at most ten examples with branch/category, actual excerpts, optional surrounding source line and at most eight nearby accepted families. One actual canonical snapshot serves the whole inventory. | No Jobs, canonical admission, browser or model call. Content-named preparation files cannot overwrite original payloads. |
| Interpret a branch | Existing chosen C runner chooses `same_family`, `new_family`, `unclear` or `no_useful_addition`, with supplied IDs, functional reasoning and at most three short source/purpose phrases. | One bounded existing model request for needed examples; no second checker, provider choice or automatic paid fallback. |
| Admit the decision | `build_catalogue_branch` validates category/parent/IDs/phrases. Exact accepted labels and aliases merge locally. A distinct accepted family enters existing `catalogue_additions`; source-free adjacent ideas remain labelled plausible. | An original saved answer is reapplied after local commit failure. Completed unclear/no-addition decisions are cached, not infinite idea loops. |
| Measure again | `effective_catalogue` and `catalogue_matrix` calculate the actual admitted view and source choices. | Prepared examples are not added to the accepted-family total. |

Materials live at `catalogue_materials/pages/<page-id>/receipt.json` and
`catalogue_materials/items/<material-id>/material.json` inside the owned
research root. Original marketplace paths/hashes stay linked. The richer
surrounding line is context for interpretation, not a whole-title search term.
Saved older seed jobs without this optional context are replayed under their
original inputs rather than sent to the model again.

The independent script can run while the programme owns the writer. Actual C
admission now runs automatically through that programme's finite discovery
callback, using its normal ownership and original Jobs lifecycle. It does not
require a separate admission script or another simultaneous writer. The
original 156-product review run is not enlarged by changing its frozen IDs.

```python
from product_radar.catalogue_discovery import create_catalogue_discovery_step
from product_radar.catalogue_seed import load_seed_inventory

discovery_step = create_catalogue_discovery_step(
    catalogue,
    seed_inventory=load_seed_inventory(),  # config/research/catalogue_branch_inventory_2026-10-08.json
    seed_source_root=project_root,         # its source pages live in this project's store
    seed_branches_per_round=1,
)
# The existing programme invokes this callback under its normal writer.
# Construction makes no browser or model call and changes no research state.
```

The default driver (`scripts/run_programme.py`) supplies the repository
inventory, not a saved manifest. The first time the seed lane runs in a store
without its own seed cycle, it stages that inventory through the same route as
`catalogue_research.py --stage-inventory` (`prepare_seed_inventory_manifest`:
replay the declared public pages, stage materials and branch payloads; no page
fetch, model, Actor or canonical write) and saves
`catalogue_materials/preparations/seed-manifest-<inventory id>-<plan hash>.json`.
The seed cycle then starts from that manifest as below. A missing declared
source page is a named wait (`CATALOGUE_SEED_SOURCES_UNAVAILABLE`) whose wake
hint stays not-ready until those exact files change; an unusable preparation
waits for changed inputs and a local read failure retries after five minutes,
so neither repeats every turn. Since the 13 October fresh start the store holds
byte-identical copies of the 21 declared 8 October pages: 113 of the 145
excerpts are bestseller card titles that a new capture would not reproduce.

Each seed cycle freezes its original branch IDs, preparation-file references,
payload hashes and concept baseline in an additive
`catalogue-discovery-seed-cycle-v1` record. Its branch files remain the exact
source of each C input. New neighbours are not substituted into an old
prepared request. The loader checks the original preparation hash, branch and
material partition; admission checks each material's actual retained source,
surrounding context and supplied family references. A broken material can be
deferred while healthy material in the same branch is considered.

| Branch result | What is saved | What happens next |
|---|---|---|
| An exact accepted label or alias is already known. | A local merge and its original identity, with no new C call. | The next prepared branch receives a turn. |
| C identifies a useful new function or alias. | The validated addition and original C job/reference. | Only admitted choices enter the actual family/phrase matrix. |
| C finds no useful addition or cannot resolve the example. | The complete `no_useful_addition` or `unclear` decision. | The same unchanged example is not sent repeatedly to invent more breadth. |
| A material, preparation or result cannot be used. | Its exact failure, eligible/pending IDs and original request bindings. | Healthy branches and all original product-review lanes continue. |
| The answer was saved, but local admission failed. | The original saved job still owns the material. | After the bounded retry interval, reapply that answer; do not buy a second interpretation. |

One branch, containing at most ten originally prepared items, is considered
per programme round by default. Pending work waits five minutes between local
continuation attempts, and unvisited branches get their first turn before a
failed sibling is retried. This is a continuation delay, not a new provider
request or a reason to stop the original product lanes. Missing or malformed
whole manifests are reported explicitly while connected discovery continues
from the actual accepted catalogue.

A genuinely new discovery cycle freezes its search choices after one finite
initial pass through the supplied branches. It does not require every branch
to succeed. An already frozen discovery cycle keeps all its original requests
and baseline; newly admitted breadth is available to later fresh cycles,
without silently rewriting earlier acquisitions. The programme's progress
signature includes productive branch/admission changes, so an exact local
merge or failed first branch does not incorrectly end work before a healthy
later branch. Changes to diagnostic times alone do not keep the loop running.

The callback returns separate counts for prepared, eligible, completed,
actually judged, exact local merges, cache reuse, accepted, merged,
no-useful-addition, unclear, contract-rejected and pending examples. They are
not interchangeable. Its content-named accepted-matrix file contains the
actual category/family/phrase/source/purpose rows and counts after admission;
prepared examples do not increase those totals. Actual live admission counts
must be read from these saved records, not inferred from the 145-item manifest.

## 4. How actual fresh discovery proceeds

The **current installed behavior** of `create_catalogue_discovery_step` is the N22 callback
`(memory, frozen_programme_record, context) -> result`. A cycle freezes accepted
compact family choices and real inspected department list requests once. The
default is four total selected actions in one cycle per programme, not four newly renewed actions every round. It advances at most two external handler calls per round. The planned numbered waves remove that lifetime discovery cap without resetting original requests or paid/model authority.

New Amazon/Flipkart questions use the complete installed
`build_marketplace_request` result, including actual route, URL, department,
term, country and scope fields. The verified future category mapping is Home and
Decor `kitchen/1380374031`, Baby Products `baby`, Home and Kitchen `kitchen`, Travel and
Outdoor `luggage`. Already frozen sparse requests retain their original
meaning on replay.

The current family scheduler uses alias groups and prior results; its seven-day recent-wording suppression is family-scoped and its sibling sources are fallback-only. That is a limitation, not the desired long-term rule. Under section26, recent reuse keys include the actual source/request scope, pending interpretation parks only dependent work, and independent healthy sources can still run. Useful free synonyms retain the same identity/history but remain eligible for exploration. Exact unchanged fresh requests normally reuse saved captures. A paid cooldown cannot prohibit a free sibling; provider access waits still apply. Alias-label grouping is a scheduling aid, not proof of semantic equivalence across categories/functions.

| Situation | Actual continuation | Honest result |
|---|---|---|
| Meta route fails | Retrieve the same compact type through connected free Amazon/Flipkart alternatives. | A source fallback, not a newly invented family or bestseller ranking. |
| Cards are captured | Apply existing A/B description and identity work to those acquisition IDs. | Raw, described, known, new and deferred counts remain separate. |
| Navigation may already have started | Reconcile the original saved acquisition/receipt only. | No replacement browser launch disguised as a retry. |
| Local browser startup is positively known to have failed | Retain the no-navigation error and permit the same bounded local retry. | No fabricated site attempt or negative demand. |
| One route is blocked or one product remains ambiguous | Preserve its exact receipt/pending reason and give healthy sibling actions their turn. | No global claim that every product was checked on that route. |
| New identity is admitted | Return immutable new concept IDs to the programme's separate follow-on full-source review scope. | The original 156 review remains frozen; discovery is not report readiness. |

The existing finite cycle, automatic prepared-branch C phase and real
collector/import/A/B seam are implemented and fixture-tested. Live admission
of the 145 staged examples and subsequent fresh cycle execution are distinct
acceptance steps; side captures or fixture results alone do not prove the
recurring programme has performed them.

The current fallback table describes installed behavior, not full independent-source discovery. Planned source obligations must continue useful Amazon/Flipkart/Trends/supplier research even after Facebook succeeds or returns empty. Generic type/direction capture must precede actual-concept projection for adapters that currently require a found product; do not pass invented IDs. The source-by-source implementation, model admission, fair renewed waves and recovery are specified once in section26, not duplicated as another driver here.

## 5. Exact search pool and six-month workload

The retained snapshot at **09:49:46 UTC on 8 October 2026**, before admission of
the new side-study examples, has the following actual sums. This is not
category count multiplied by an invented universal phrase count.

| Category | Accepted families | Family/phrase pairs | Source/purpose options | Meta discovery choices | Meta actually tried | Meta not dispatched |
|---|---:|---:|---:|---:|---:|---:|
| Home and Decor | 56 | 110 | 170 | 58 | 6 | 52 |
| Baby Products | 56 | 109 | 169 | 57 | 8 | 49 |
| Home and Kitchen | 56 | 109 | 169 | 57 | 9 | 48 |
| Travel and Outdoor | 56 | 110 | 169 | 57 | 7 | 50 |
| Total | 224 | 438 | 677 | 229 | 30 | 199 |

`catalogue_coverage.catalogue_matrix` keys every actual usable choice by family,
normalized phrase, source, purpose, language and country. A different reason
does not create another choice. Bound original captures or actual submitted
Actor receipts establish attempts; preparation does not. Uncertain attempts
remain uncertain. Department lists, alternate-source retrieval and unmapped
product checks are listed separately, not used to claim fresh unused pool.

At three issues per week for 26 weeks, the planning horizon is **78 issues and
780 entries** at ten entries per issue. At most two useful updates per issue
means at least **624 fresh entries**, if useful updates actually exist. Without
them it means 780 fresh entries. The small existing pool does not on its own
establish six months of supply.

| Explicit assumption | Fresh entries per issue | New candidates needed per issue | Queries per issue at five genuinely new candidates per query | Queries for 78 issues |
|---|---:|---:|---:|---:|
| 5% useful yield, maximum useful updates available | 8 | 160 | 32 | 2,496 |
| 5% useful yield, no useful updates available | 10 | 200 | 40 | 3,120 |
| 3% stress yield, maximum useful updates available | 8 | 267 | 54 | 4,212 |
| 3% stress yield, no useful updates available | 10 | 334 | 67 | 5,226 |

Five genuinely new candidates per query is an assumption, not measured from
the 660 raw cards. The new relaxed checklist changes useful yield, so measure
it rather than carrying a legacy rejection percentage forward as truth. The
system needs continuing functional breadth, real list renewal, source-mix
learning and meaningful revisits. Useful similar free wording is allowed but must not be counted as thousands of new types or guaranteed fresh runway. The
[cost plan](build_activation_and_costs_2026-10-03.md#5-multi-account-financial-implementation-and-remaining-activation)
owns failed/duplicate collection, AI effort, exact charges, pooled-account
funding and lower-yield cost scenarios. Zero Apify charges for these page reads
does not mean zero computer time or zero later model cost.

## 6. Independent commands and verification

```text
.venv/bin/python scripts/catalogue_research.py --list-links <original-capture-folder> --output <side-file>
.venv/bin/python scripts/catalogue_research.py --stage-inventory config/research/catalogue_branch_inventory_2026-10-08.json --output <side-manifest>
.venv/bin/python scripts/catalogue_research.py --prepare-branch <branch-id> --material-id <material-id>
.venv/bin/python scripts/catalogue_research.py --matrix --new-candidates-per-query 5 --output <side-matrix>
.venv/bin/python -m unittest discover -s tests -p 'test_catalogue_*.py' -v
```

Only explicit `--capture-list` opens a public page. All other actions above
reuse verified files/read canonical state; they create no model job or paid
request. The current focused catalogue suite passes **96/96** checks, covering
exact-choice arithmetic, lower-yield scenarios, source integrity, navigation
scope, side preparation, alias/history retention, unhealthy sibling
continuation, original saved-job recovery, real installed marketplace
capture/import/A/B admission, exact prepared-file C inputs, automatic branch
turns, whole-programme continuation after local merges or input failures,
accepted-only matrix counts and zero-request replay. They are local proofs,
not a claim that live recurring discovery, six-month capacity or client release
has already been accepted.

## 7. Long-lived research and one joined counting system

The client recommendation is a narrow functional type using the existing concept identity; concrete listings/offers are its examples. Catalogue headings and words help find and investigate those types, but do not themselves count as client recommendations. The planned scope bindings join heading/direction research, actual concepts and original captures. Trends normally uses common type wording; suppliers/marketplaces also need actual offer-specific facts. One broad chart does not prove every narrow subtype.

The dated233-heading/470-wording-pair/710-instruction view is explained, together with the44 attempts that produced the original156 concepts, in [section26.11](benchmarks/2026-10-03/execution_specification.md#2611-counts-provenance-yield-and-expense-that-the-owner-can-follow). Those710 instructions are an existing partial source assignment, not every possible application search.470 ×14 =6,580 possible wording/route cells is a coverage view, not a mandatory bill or product inventory. Aliases share a subject; actual compatible requests share one acquisition; every target retains scoped relevance and route outcome. The153 ×14 product audit remains a different joined view, not an unrelated competing search system.

Target2,000 logical issues through renewed researched directions and actual market supply. Similar free searches are permitted; exact pointless repeats reuse saved work, and unchanged AI answers are not purchased again. At ten entries/issue,2,000 issues require20,000 appearances and at least16,000 genuinely New types if useful updates fill the entire allowance. This is a longevity workload, not evidence that the present seed guarantees that supply. Measure distinct functional discovery, completed practical screening, eligible unused inventory, meaningful updates and real expense separately. Do not claim a number of months from unused wording counts alone.
