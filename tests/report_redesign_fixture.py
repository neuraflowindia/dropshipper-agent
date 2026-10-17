"""A rich, fully synthetic schema-3 report issue for visual checks of the report pages.

``rich_issue(root)`` builds invented product types through the application's own
writers (the same producers ``test_functional_source_market`` and
``test_functional_report_packet`` use) and returns a validated ten-product issue
whose products cover the states the report draws: all three opportunity labels,
Low / Medium / High crowding, rising-strongly / rising / steady / missing Google
Trends charts, zero to seven Meta advertisers with recent and older first ads,
dropshipping-platform presence with list roles and prices, quick-commerce stock
by place, product photos, supplier leads, one "Updated" product (published in an
earlier synthetic issue, then given a useful update) and both report languages.

Every name, page, photo, price and answer below is invented for testing; no
real research data, supplier or marketplace photo is used, and nothing here is
a recommendation. Pages use example.org hosts; the Meta ad-library links and
the Amazon bestseller links are the mappers' own URL forms for invented ad
numbers and ASINs. No network, provider, model or publication call is made.
"""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from product_radar.crowding import refresh_crowding
from product_radar.fit_description import FUNCTIONAL_CONTRACT
from product_radar.fit_review import prepare_practical_fit_reviews, run_fit_reviews
from product_radar.functional_types import (accept_functional_definition, effective_functional_definition,
    prepare_example_memberships)
from product_radar.issue_allocation import allocate_issues
from product_radar.live_outputs import build_issue_dataset, validate_dataset
from product_radar.models import digest
from product_radar.product_queries import ensure_query_profiles
from product_radar.programme import _functional_report_revision
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_narration import generate_report_narratives
from product_radar.report_selection import build_report_pool
from product_radar.research_flow import run_flow
from product_radar.research_memory import ResearchMemory, change, import_capture, key
from product_radar.research_scope import accept_scoped_evidence
from product_radar.research_sources import map_research_rows
from product_radar.source_interpretation import review_source_results
from product_radar.service import policy_for_fit
from product_radar.storage import Journal
from product_radar.supplier_effort import record_supplier_effort
from test_functional_fit import FunctionalFitRunner
from test_functional_queries import Runner as QueryRunner
from test_functional_source_interpretation import Runner as SourceRunner
from test_research_flow import FakeRunner, NOW, capture
from test_research_sources import amazon_row, request as amazon_request

ISSUE_ID = "redesign-visual-issue"
ISSUE_DATE = "2026-10-07"
EARLIER_ISSUE_ID = "redesign-visual-earlier-issue"
EARLIER_DATE = "2026-10-06"                 # released 9:00 India time (03:30 UTC)
PUBLISHED_AT = "2026-10-06T04:00:00+00:00"
CHART_LATER = "2026-10-06T08:00:00+00:00"   # a newer chart for the published ("Updated") type
LATER = "2026-10-06T10:00:00+00:00"         # the returned issue is selected at this time
OBSERVED = "2026-10-04T10:00:00+00:00"   # quick-commerce reads, a day before NOW
DAY = 86400
SEPT_20 = 1789862400   # 15 days before NOW: a "recent" first ad (within 30 days)
SEPT_1 = 1788220800
AUG_10 = 1786320000
JULY_20 = 1784505600
JUNE_15 = 1781481600
MEDIA_FOLDER = "synthetic-product-photos"
SOURCE_RUN = "redesign-visual-sources"


# The ten invented product types ---------------------------------------------------
#
# crowding: "high" = one relevant Amazon bestseller listing; "low"/"medium" = both
# marketplace searches read, with the listed (ratings) cards on synthetic pages.
# ads: each advertiser's first-ad start (unix seconds); the first is the discovery ad
#   ("meta": False leaves the discovery advert unmatched, so no Meta advertiser is counted).
# trends: None or a 54-week series name; presence: {route: (state, [(roles, price, new_arrival)])}.
# qc: [(app, place, available or None for an answered question with no result)].

def _series(name):
    if name == "strong":      # last four weeks about 1.6x the four before
        base = [22, 24, 21, 25, 23, 26, 24, 22, 27, 25, 23, 26, 28, 24, 25, 27, 26, 29, 27, 25, 28, 30, 27,
                26, 29, 31, 28, 27, 30, 29, 31, 28, 30, 32, 29, 31, 33, 30, 32, 31, 34, 33, 35, 32, 34, 36, 35, 33, 34]
        return base + [55, 58, 61, 57, 63]
    if name == "rising":      # about 1.25x
        base = [40, 42, 39, 44, 41, 43, 45, 42, 40, 44, 46, 43, 41, 45, 47, 44, 42, 46, 48, 45, 43, 47, 44, 46,
                49, 45, 47, 50, 46, 48, 51, 47, 49, 52, 48, 50, 53, 49, 51, 50, 52, 48, 51, 49, 50, 52, 49, 51, 50]
        return base + [62, 64, 61, 66, 60]
    if name == "steady":      # flat: recent mean within a few points of the previous
        base = [61, 58, 63, 60, 57, 62, 64, 59, 61, 60, 63, 58, 62, 65, 60, 59, 61, 63, 60, 62, 58, 61, 64, 60,
                59, 62, 61, 63, 60, 58, 62, 61, 59, 63, 60, 62, 61, 64, 59, 60, 62, 61, 63, 60, 59, 62, 61, 60, 62]
        return base + [61, 60, 62, 61, 58]
    if name == "strong_later":  # the "rising" search re-exported a day later: the last weeks climb steeply
        return _series("rising")[:49] + [80, 84, 86, 88, 90]
    raise ValueError(name)


TYPES = (
    {"slug": "spikes", "title": "Self-watering plant spike set", "category": "Home and Decor",
     "function": "Water potted plants slowly while owners are away", "mechanism": "Terracotta wicking spike",
     "use_context": ["Indoor potted plants", "Balcony planters"], "feature": "porous clay tip",
     "ad": "Self watering spikes for indoor plants, set of four", "crowding": "low",
     "ads": [SEPT_20, AUG_10, JULY_20, JUNE_15], "trends": "strong", "term": "self watering spikes",
     "presence": {"deodap": ("relevant", [(["bestseller"], "119", False)]),
                  "baapstore": ("relevant", [(["trending"], "129", False)]), "dropdash": ("no_relevant_items", [])},
     "qc": [], "photo": (46, 125, 84), "suppliers": 2, "unclear": ()},
    {"slug": "feeding", "title": "Silicone suction feeding set", "category": "Baby Products",
     "function": "Keep a toddler's bowl and plate fixed to the table at mealtimes", "mechanism": "Suction base silicone tableware",
     "use_context": ["Toddler mealtimes"], "feature": "suction base",
     "ad": "Silicone suction bowl and spoon set for toddlers", "crowding": "medium",
     "ads": [SEPT_1, AUG_10, JULY_20, JUNE_15, JUNE_15 + 9 * DAY], "trends": "rising", "term": "suction bowl for baby",
     "presence": {"deodap": ("relevant", [(["high_demand"], "249", False)]), "baapstore": ("not_checked", []),
                  "dropdash": ("no_relevant_items", [])},
     "qc": [("blinkit", "Mumbai", True), ("zepto", "Mumbai", False), ("instamart", "Mumbai", None)],
     "photo": (232, 140, 92), "suppliers": 0, "unclear": (9,)},
    {"slug": "cabinet", "title": "Rechargeable LED motion sensor cabinet light", "category": "Home and Kitchen",
     "function": "Light dark cupboards and wardrobes when a door opens", "mechanism": "Motion sensor light strip",
     "use_context": ["Kitchen cabinets", "Wardrobes"], "feature": "magnetic mounting strip",
     "ad": "Motion sensor cabinet light, USB rechargeable", "crowding": "low",
     "ads": [SEPT_20, SEPT_20 + 4 * DAY], "trends": "strong", "term": "motion sensor cabinet light",
     "presence": {"deodap": ("no_relevant_items", []), "baapstore": ("not_checked", []),
                  "dropdash": ("relevant", [(["new_arrival"], "329", True)])},
     "qc": [], "photo": None, "suppliers": 0, "unclear": (13,)},
    {"slug": "trimmer", "title": "Electric baby nail trimmer kit", "category": "Baby Products",
     "function": "File a baby's nails gently without clippers", "mechanism": "Low speed rotating filing pads",
     "use_context": ["Baby nail care"], "feature": "interchangeable soft pads",
     "ad": "Gentle electric nail filer for babies with six pads", "crowding": "high",
     "ads": [SEPT_20, SEPT_1, AUG_10, AUG_10 + 3 * DAY, JULY_20, JUNE_15, JUNE_15 + 5 * DAY], "trends": "steady",
     "term": "baby nail trimmer",
     "presence": {"deodap": ("relevant", [(["bestseller"], "349", False)]),
                  "baapstore": ("relevant", [(["high_demand"], "379", False)]),
                  "dropdash": ("relevant", [(["trending", "new_arrival"], "339", True)])},
     "qc": [("blinkit", "Delhi NCR", True), ("zepto", "Bengaluru", True), ("instamart", "Mumbai", None)],
     "photo": (120, 160, 220), "suppliers": 3, "unclear": ()},
    {"slug": "cubes", "title": "Compression packing cube set", "category": "Travel and Outdoor",
     "function": "Keep clothes compact and sorted inside a suitcase", "mechanism": "Zip compression fabric cubes",
     "use_context": ["Suitcases", "Cabin bags"], "feature": "double zip compression",
     "ad": "Compression packing cubes for travel, set of six", "crowding": "medium",
     "ads": [AUG_10], "meta": False, "trends": "steady", "term": "packing cubes",
     "presence": {"deodap": ("no_relevant_items", []), "baapstore": ("no_relevant_items", []),
                  "dropdash": ("not_checked", [])},
     "qc": [], "photo": None, "suppliers": 0, "unclear": (5, 13)},
    {"slug": "organiser", "title": "Under-sink pull-out organiser", "category": "Home and Kitchen",
     "function": "Make the cupboard under a sink easy to reach", "mechanism": "Two tier sliding drawer rack",
     "use_context": ["Kitchen sink cupboard", "Bathroom vanity"], "feature": "sliding basket",
     "ad": "Two tier pull out organiser for under the sink", "crowding": "high",
     "ads": [SEPT_1, AUG_10, JULY_20, JULY_20 + 6 * DAY, JUNE_15, JUNE_15 + 2 * DAY], "trends": "rising",
     "term": "under sink organiser",
     "presence": {"deodap": ("relevant", [(["trending"], "449", False)]), "baapstore": ("not_checked", []),
                  "dropdash": ("not_checked", [])},
     "qc": [], "photo": None, "suppliers": 2, "unclear": ()},
    {"slug": "macrame", "title": "Macrame hanging wall planter", "category": "Home and Decor",
     "function": "Hang small plants on a wall as decor", "mechanism": "Knotted cotton cord hanger",
     "use_context": ["Living room walls", "Balconies"], "feature": "wooden mounting dowel",
     "ad": "Handmade macrame wall hanging planter", "crowding": "low",
     "ads": [AUG_10, JULY_20, JUNE_15], "trends": None, "term": None,
     "presence": {"deodap": ("relevant", [(["trending"], "199", False)]), "baapstore": ("not_checked", []),
                  "dropdash": ("not_checked", [])},
     "qc": [], "photo": (196, 170, 128), "suppliers": 0, "unclear": (9,)},
    {"slug": "kettle", "title": "Collapsible silicone travel kettle", "category": "Travel and Outdoor",
     "function": "Boil water on trips and fold flat for packing", "mechanism": "Folding silicone body kettle",
     "use_context": ["Hotel stays", "Road trips"], "feature": "folding silicone walls",
     "ad": "Foldable travel kettle that packs flat", "crowding": "high",
     "ads": [AUG_10, JULY_20, JUNE_15], "trends": None, "term": None, "presence": None,
     "qc": [], "photo": None, "suppliers": 0, "unclear": ()},
    {"slug": "bathtub", "title": "Foldable baby bath tub with cushion", "category": "Baby Products",
     "function": "Bathe a baby safely in a small bathroom", "mechanism": "Folding tub with support cushion",
     "use_context": ["Baby bath time"], "feature": "non slip support cushion",
     "ad": "Foldable baby bathtub with soft support cushion", "crowding": "medium",
     "ads": [SEPT_20, AUG_10, JULY_20, JUNE_15], "trends": "strong", "term": "foldable baby bath tub",
     "presence": None, "qc": [("blinkit", "Bengaluru", True), ("zepto", "Bengaluru", None)],
     "photo": None, "suppliers": 0, "unclear": ()},
    {"slug": "lantern", "title": "Solar LED camping lantern", "category": "Travel and Outdoor",
     "function": "Light a tent or campsite without mains power", "mechanism": "Solar charged lantern",
     "use_context": ["Camping", "Power cuts"], "feature": "fold out solar panel",
     "ad": "Solar camping lantern with hanging hook", "crowding": "high",
     "ads": [JULY_20], "trends": "steady", "term": "solar camping lantern", "presence": None,
     "qc": [], "photo": None, "suppliers": 1, "unclear": (5,)},
)


# Nine further invented types fill the earlier, already published issue, so the
# "Updated" type has a real publication to be compared with. They are not in
# the returned issue (published, with no useful update).
_FILLER_ROWS = (
    ("spicerack", "Magnetic spice jar rack", "Home and Kitchen", "Keep spice jars on the side of a fridge"),
    ("shoerack", "Over-door shoe organiser", "Home and Decor", "Store shoes on the back of a door"),
    ("covers", "Reusable silicone food covers", "Home and Kitchen", "Cover bowls and cut fruit in the fridge"),
    ("dividers", "Bamboo drawer dividers", "Home and Decor", "Separate items neatly inside drawers"),
    ("gapfiller", "Car seat gap filler", "Travel and Outdoor", "Stop small items falling beside car seats"),
    ("toiletry", "Travel toiletry bottle set", "Travel and Outdoor", "Carry small amounts of liquids on trips"),
    ("dryingrack", "Baby bottle drying rack", "Baby Products", "Dry feeding bottles upright after washing"),
    ("keyholder", "Wall-mounted key holder", "Home and Decor", "Keep keys by the door"),
    ("fridgebins", "Stackable fridge storage bins", "Home and Kitchen", "Sort food inside a fridge"),
)
FILLERS = tuple({"slug": slug, "title": title, "category": category, "function": use, "mechanism": "Simple fixed holder",
                 "use_context": ["Everyday home use"], "feature": "simple fixed holder", "ad": title + ", pack of two",
                 "crowding": "high", "ads": [AUG_10, JULY_20, JUNE_15], "trends": None, "term": None, "presence": None,
                 "qc": [], "photo": None, "suppliers": 0, "unclear": ()}
                for slug, title, category, use in _FILLER_ROWS)
ALL_TYPES = TYPES + FILLERS
UPDATED_SLUG = "organiser"   # published earlier; a newer, steeper search chart is its useful update


# Discovery and accepted meaning -------------------------------------------------------

class _DiscoveryRunner(FakeRunner):
    """The real A/B flow with each invented type's own description."""
    identity = "redesign-visual-discovery-v1"

    def __call__(self, role, payload, folder):
        response, usage = super().__call__(role, payload, folder)
        if role == "A":
            for item, answer in zip(payload["items"], response["results"]):
                spec = _by_slug(item["source_text"]["title"].rsplit(" ", 1)[-1])
                for product in answer["products"]:
                    product.update(label=spec["title"], category=spec["category"], function=spec["function"],
                                   mechanism="manual", features=[spec["feature"]],
                                   audience="parents" if spec["category"] == "Baby Products" else "adult")
                answer["kind"] = "product"
        return response, usage


def _by_slug(slug):
    return next(spec for spec in ALL_TYPES if spec["slug"] == slug)


def _definition(spec):
    return {"report_title": spec["title"], "primary_use": spec["function"], "mechanism": spec["mechanism"],
            "use_context": list(spec["use_context"]), "compatibility_constraints": [],
            "included_distinctions": [spec["feature"].capitalize()], "excluded_distinctions": ["Unrelated accessories"]}


class _QueryRunner(QueryRunner):
    """Per-type search phrases (the C profile) instead of the sink caddy wording."""
    identity = "redesign-visual-queries-v1"

    def __init__(self, terms):
        super().__init__()
        self.terms = terms

    def __call__(self, role, payload, folder):
        response, usage = super().__call__(role, payload, folder)
        for item, row in zip(payload["items"], response["results"]):
            phrase = self.terms[item["id"]]
            row.update(core_type=phrase, subtype=phrase, aliases=[phrase + " set"], mechanism_anchors=[phrase],
                       broad_context=[phrase.split(" ")[-1]])
            for query in row["queries"]:
                query["text"] = phrase if query["scope"] == "subtype" else phrase.split(" ")[-1]
        return response, usage


class _FitRunner(FunctionalFitRunner):
    """The real v4 fit answers, with a few criteria not met for a varied checklist."""
    identity = "redesign-visual-fit-v1"

    def __init__(self, unclear):
        super().__init__()
        self.unclear = unclear   # meaning hash -> criterion ids judged "Does not meet"

    def __call__(self, role, payload, folder):
        response, usage = super().__call__(role, payload, folder)
        for item, row in zip(payload["items"], response["results"]):
            for criterion in row["criteria"]:
                if criterion["id"] in self.unclear.get(item["meaning_hash"], ()):
                    criterion.update(state="Does not meet",
                                     reason="Estimated: The described use does not clearly support this judgment.")
        return response, usage


# Market evidence ----------------------------------------------------------------------

def _accept(memory, cid, route, table, rid, path, limitation, at=NOW):
    row = memory.snapshot()[table][rid]
    accept_scoped_evidence(memory, cid, route=route, scope="narrow_type", relationship="relevant_family",
        support="insufficient", references=[{"table": table, "id": rid, "hash": digest(row)}],
        usable_fields=[{"reference": {"table": table, "id": rid}, "path": path}],
        limitations=[limitation], reason="Explicit synthetic fixture type comparison.", reviewer="synthetic", as_of=at,
        expected_meaning_hash=effective_functional_definition(memory.snapshot(), cid)["meaning_hash"])


def _meta(memory, n, spec):
    """Extra advertisers' ads (one each) beyond the discovery ad; return their observation ids."""
    if len(spec["ads"]) < 2:
        return []
    extra = [{"ad_archive_id": str(5000 + 10 * n + k), "page_id": str(7000 + 10 * n + k), "is_active": k % 3 != 2,
              "start_date": start, "page_name": f"Invented store {n}{chr(64 + k)}",
              "snapshot": {"title": spec["ad"] + f", offer {chr(64 + k)}"}}
             for k, start in enumerate(spec["ads"][1:], 1)]
    imported = capture(memory, values=extra, acquisition=f"visual-meta-{n}", observed=NOW)
    return sorted(ref for ref, row in memory.snapshot()["observations"].items() if row["capture_id"] == imported["capture_id"])


def _bestseller(memory, n, spec):
    cards = [dict(amazon_row(), title=spec["ad"], asin=f"B0VIS{n:05d}", link=f"https://www.amazon.in/dp/B0VIS{n:05d}",
                  price=f"₹{299 + 40 * n}.00")]
    imported = import_capture(memory, source="amazon", acquisition_id=f"visual-amazon-{n}", observed_at=NOW,
        mapped=map_research_rows(cards, amazon_request()), capture_hash=digest(cards), reference={"fixture": "synthetic"})
    return [ref for ref, row in memory.snapshot()["observations"].items() if row["capture_id"] == imported["capture_id"]]


_LISTINGS = {"low": [("amazon-search", 48), ("flipkart-search", 12)],
             "medium": [("amazon-search", 340), ("amazon-search", 95), ("flipkart-search", 61)]}


def _search_page(folder, route, term, cards, total):
    """A synthetic saved first results page that the real card parser reads."""
    folder.mkdir(parents=True, exist_ok=True)
    if route == "amazon-search":
        body = "".join(f'<div class="s-result-item" data-asin="{identity}"><h2><span>Invented {term} listing</span></h2>'
                       f'<span aria-label="4.1 out of 5 stars"></span><a aria-label="{ratings} ratings">'
                       f'<span>({ratings})</span></a></div>' for identity, ratings in cards)
        heading = f'1-48 of over {total:,} results for "{term}"'
    else:
        body = "".join(f'<div data-id="{identity}"><a>Invented {term} listing</a><span><div>4.0</div></span>'
                       f'<span>({ratings})</span></div>' for identity, ratings in cards)
        heading = f'Showing 1 – 40 of {total:,} results for "{term}"'
    (folder / "rendered.html").write_text(f"<!doctype html><html><body><h1>{heading}</h1>{body}</body></html>",
                                          encoding="utf-8")
    (folder / "snapshot.json").write_text('{"text": "Synthetic saved results page.\\n' + heading.replace('"', '\\"')
                                          + '"}', encoding="utf-8")


def _marketplace_searches(questions, root, n, cid, spec):
    """Both marketplace searches read, each saving its synthetic first page and relevant listings."""
    term = spec["term"] or spec["title"].lower()
    by_route = {}
    for index, (route, ratings) in enumerate(_LISTINGS[spec["crowding"]]):
        identity = (f"B0SR{n:02d}{index:04d}" if route == "amazon-search" else f"SYNFK{n:02d}{index:06d}")
        by_route.setdefault(route, []).append((identity, ratings))
    for route in ("amazon-search", "flipkart-search"):
        cards = by_route.get(route, [])

        def save(memory, acquisition_id, route=route, cards=cards):
            source = "amazon" if route == "amazon-search" else "flipkart"
            name = f"{route}-{spec['slug']}"
            folder = Path(root) / "saved-search-pages" / name
            _search_page(folder, route, term, cards, 640 + 175 * n)
            capture_id = "capture-" + name
            changes = [change("captures", capture_id, {
                "source": source, "acquisition_id": acquisition_id, "observed_at": NOW, "capture_hash": "0" * 64,
                "mapping_hash": "0" * 64, "mapping_status": "partial",
                "reference": {"route": route, "folder": str(folder), "file": "receipt.json", "request": {},
                              "receipt_hash": "0" * 64}})]
            for identity, _ in cards:
                semantic = {"title": f"Invented {term} listing {identity[-2:]}",
                            "product_url": f"https://listings.example.org/{source}/{identity}"}
                changes.append(change("observations", "obs-" + identity, {
                    "source": source, "acquisition_id": acquisition_id, "source_item": identity + ":visual",
                    "capture_id": capture_id, "observed_at": NOW, "semantic_id": key("item", semantic), "semantic": semantic,
                    "facts": {"asin" if source == "amazon" else "product_id": identity, "source_route": route,
                              "list_type": "product_search", "query": term, "direct_capture": {"capture_id": capture_id}}}))
            memory.commit(key("visual-search-page", capture_id), changes)
            return [capture_id], ["obs-" + identity for identity, _ in cards]

        _question(questions, cid, route, {"route": route, "term": term}, save=save, state="usable" if cards else "empty")


def _question(questions, cid, route, request, *, save=None, state="usable", at=NOW):
    """One source question for the real task writer; ``save`` stores what its synthetic answer read."""
    questions.append({"route": route, "request": request, "concept_ids": [cid], "save": save, "state": state,
                      "observed_at": at})


def _answer_questions(memory, questions):
    """Freeze the questions as one finite source run and answer each through
    the application's own task and receipt writers (no network), then accept
    each saved listing for its type through the real source-scope review."""
    from product_radar.source_coverage import execute_source_run, prepare_source_tasks
    by_request = {(row["route"], digest(row["request"])): row for row in questions}
    prepare_source_tasks(memory, [{field: row[field] for field in ("route", "request", "concept_ids")} for row in questions],
                         run_id=SOURCE_RUN, as_of=NOW)
    def answer(context):
        context["record_dispatch"]()
        row = by_request[context["route"], digest(context["request"])]
        captures, observations = row["save"](memory, context["acquisition_id"]) if row["save"] else ([], [])
        return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": row["observed_at"], "state": row["state"], "dispatch_started": True,
                "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": captures,
                "observation_ids": observations, "target_results": [],
                "reason": "Synthetic saved source answer.", "retryable": False}

    result = execute_source_run(memory, SOURCE_RUN, handlers={route: answer for route in {row["route"] for row in questions}},
                                as_of=NOW, bounded_call=lambda callback, context, _timeout: callback(context))
    tasks = memory.snapshot()["query_tasks"]
    if len(result["results"]) != len(questions) or any(
            tasks[row["task_id"]]["status"] not in ("acquired", "completed_no_results") for row in result["results"]):
        raise AssertionError(result)
    # The real functional source-scope review (E, synthetic runner) matches each saved listing to its type.
    runner = SourceRunner()
    for _ in range(len(questions)):
        reviewed = review_source_results(memory, runner, run_id=SOURCE_RUN, limit=20, as_of=NOW)
        if not reviewed["remaining_questions"]:
            break
    else:
        raise AssertionError(reviewed)


def _trends(memory, n, cid, spec):
    from product_radar.research_operations import import_trends_evidence
    from test_trends_browser import metadata, weekly
    chart = import_trends_evidence(memory, concept_id=cid, acquisition_id=f"visual-chart-{n}",
        csv_bytes=weekly(_series(spec["trends"]), label=spec["term"] + ": (India)"),
        metadata=metadata(term=spec["term"], collected_at=NOW, source_reference=f"visual-chart-{n}.csv"))
    _accept(memory, cid, "google-trends", "trends_evidence", chart["id"], ["parsed", "series"],
            "Relative search interest, not product sales.")


def _presence(memory, n, cid, spec):
    """A saved platform presence fact in the weekly writer's saved shape."""
    from product_radar.dropship_platforms import ROUTES
    from product_radar.platform_roles import PRESENCE_KIND, PRESENCE_VERSION, _change_hash, presence_record_id
    meaning = effective_functional_definition(memory.snapshot(), cid)["meaning_hash"]
    platforms = {}
    for route in ROUTES:
        state, rows = spec["presence"].get(route, ("not_checked", []))
        items = []
        for index, (roles, price, new_arrival) in enumerate(rows):
            item_id = f"{route}-{spec['slug']}-{index}"
            items.append({"item_id": item_id, "title": spec["ad"], "url": f"https://{route}.example.org/item/{item_id}",
                "listed": True, "observation_id": f"{route}-observation-{spec['slug']}-{index}",
                "relationship": "relevant_family", "review_id": f"{route}-review-{spec['slug']}-{index}",
                "roles": {role: {"first_seen_at": NOW, "last_seen_at": NOW} for role in roles},
                "on_list": any(role != "new_arrival" for role in roles), "new_arrival": new_arrival,
                "price": price, "mrp": str(int(price) * 2 - 1), "price_basis": "dropship_price", "in_stock": True,
                "terms": "Not stated", "first_seen_at": NOW, "last_seen_at": NOW})
        platforms[route] = {"label": route, "state": state, "items": items}
    carrying = [route for route in ROUTES if platforms[route]["items"]]
    value = {"kind": PRESENCE_KIND, "version": PRESENCE_VERSION, "concept_id": cid, "meaning_hash": meaning, "as_of": NOW,
             "platform_count": len(carrying), "platforms_carrying": carrying,
             "on_list_platforms": [route for route in carrying if any(row["on_list"] for row in platforms[route]["items"])],
             "new_arrival_platforms": [route for route in carrying
                                       if any(row["new_arrival"] for row in platforms[route]["items"])],
             "platforms": platforms}
    value["change_hash"] = _change_hash(value)
    record = dict(value, at=NOW)
    memory.put("source_checks", presence_record_id(record), record)


def _quick_commerce(questions, n, cid, spec):
    """Quick-commerce questions answered through the real capture writer (or with no result)."""
    from product_radar.quickcommerce import VERSION, map_response, prepare_query, save_capture
    from product_radar.quickcommerce_plan import load_settings
    from test_quickcommerce import payload
    places = {place["name"]: place for place in load_settings()["places"]}
    for index, (app, place, available) in enumerate(spec["qc"]):
        request = prepare_query(app, spec["term"] or spec["title"].lower(), deepcopy(places[place]))
        if available is None:
            _question(questions, cid, app, request, state="empty", at=OBSERVED)
            continue

        def save(memory, acquisition_id, request=request, index=index, available=available):
            raw = payload(request, products=[{"id": f"qc-{spec['slug']}-{index}", "name": spec["ad"],
                "brand": "Invented maker", "available": available, "offer_price": 279 + 30 * index, "mrp": 499,
                "quantity": "1 set", "rating": 4.2, "rating_count": 18}])
            receipt = {"version": VERSION, "request": request, "observed_at": OBSERVED, "dispatch_started": True,
                       "purchase_made": False, "apify_usd": "0", "raw_response": raw, "public_response_hash": digest(raw),
                       "body_complete": True, "http_status": 200, **map_response(raw, request)}
            imported = save_capture(memory, acquisition_id, receipt)
            return [imported["capture_id"]], list(imported["observation_ids"])

        _question(questions, cid, app, request, save=save, at=OBSERVED)


def _empty_supplier_effort(memory, n, cid):
    url = f"https://supplier-search.example.org/visual/{n}"
    record_supplier_effort(memory, concept_id=cid, description=memory.snapshot()["concepts"][cid]["description"],
        pass_id=f"visual-suppliers-{n}", receipts=[{"schema_version": 2, "id": f"visual-supplier-{n}",
            "requested_url": url, "final_url": url, "checked_at": NOW, "http_status": 200,
            "execution_stage": "response_received", "attempt_started_at": NOW,
            "execution_ref": "public-fetch-" + f"{n + 4096:032x}", "body_complete": True,
            "state": "no_results", "reason": "Synthetic readable empty supplier result."}],
        receipt_reference=f"synthetic/visual-{n}/receipt.json", outcome="completed_empty")


# Supplier leads ------------------------------------------------------------------------

# Invented supplier leads: (host, company page text, GSTIN, dispatch, subscription, offers).
# Each offer is (label, purchase unit, minimum, price, offer page text).
SUPPLIER_LEADS = (
    ("greenleaf-homeware", "Greenleaf Homeware Traders, Pune. GSTIN 27AAGCG4821K1Z3. We dispatch directly to customers. "
     "No subscription required.", "27AAGCG4821K1Z3", "Yes", "Not required",
     (("{product}", "1 piece", "5 pieces", "95", "{product}, 1 piece. Minimum 5 pieces. INR 95 per piece."),
      ("{product} pack", "pack of 4", "20 packs", "340", "{product} pack, pack of 4. Minimum 20 packs. INR 340 per pack."))),
    ("sunrise-craft-supply", "Sunrise Craft Supply, Jaipur. GSTIN 08AAJFS7310M1Z6. We dispatch directly to customers. "
     "Monthly subscription required for dropshipping.", "08AAJFS7310M1Z6", "Yes", "Required",
     (("{product}", "1 piece", "10 pieces", "120", "{product}, 1 piece. Minimum 10 pieces. INR 120 per piece."),)),
    ("coastal-bulk-goods", "Coastal Bulk Goods, Surat. GSTIN 24AADCC5539P1Z1. Orders ship to the reseller's warehouse only. "
     "No subscription required.", "24AADCC5539P1Z1", "No", "Not required",
     (("{product} carton", "carton of 24", "2 cartons", "2150",
       "{product} carton, carton of 24. Minimum 2 cartons. INR 2150 per carton."),)),
)


def _supplier_pages(lead, product):
    host, company, _, _, _, offers = lead
    base = f"https://{host}.example.org"
    pages = {base + "/about": company}
    for index, offer in enumerate(offers):
        pages[f"{base}/offers/{index}"] = offer[4].format(product=product)
    return base, pages


class _SupplierReader:
    """Saved synthetic supplier pages (the capture writer's public-page fetcher contract)."""

    def __init__(self, pages, title):
        self.pages, self.title = pages, title

    def __call__(self, request):
        return {"schema_version": 1, "id": request["id"], "requested_url": request["url"], "final_url": request["url"],
                "checked_at": NOW, "state": "usable", "reason": "Synthetic saved supplier page only.", "http_status": 200,
                "body_complete": True, "title": self.title, "text": self.pages[request["url"]], "discovered_links": [],
                "text_truncated": False, "html": "<p>Synthetic</p>"}


def _supplier_term(field, value, page, quote):
    return {"field": field, "value": value, "basis": "observed", "references": [page],
            "quotes": [{"page_id": page, "excerpt": quote}], "reason": "Observed synthetic scoped statement.",
            "applicability": "applies", "exceptions": []}


class _SupplierRunner:
    """Synthetic scoped F answers: quoted company terms and each offer's minimum and price."""
    identity = "redesign-visual-scoped-supplier-v4"

    def __init__(self, lead, product):
        self.lead, self.product = lead, product

    def __call__(self, role, payload, folder):
        _, company, _, dispatch, subscription, offers = self.lead
        values = []
        for item in payload["items"]:
            page = next(p for p in item["pages"] if p["id"] in item["scope_page_ids"])
            scope = item["scope"]
            value = {"id": item["id"], "scope": deepcopy(scope), "terms": [], "prices": [],
                     "relevance": {"relationship": "relevant_company" if scope["scope_kind"] == "company_workflow"
                                   else "exact_product", "references": [page["id"]],
                                   "reason": "Synthetic scoped applicability, not a verified supplier."}}
            if scope["scope_kind"] == "company_workflow":
                sentences = company.split(". ")
                value["terms"] = [_supplier_term("direct_customer_dispatch", dispatch, page["id"], sentences[2].rstrip(".") + "."),
                                  _supplier_term("subscription_requirement", subscription, page["id"],
                                                 sentences[3].rstrip(".") + ".")]
            else:
                offer = offers[int(page["url"].rsplit("/", 1)[-1])]
                value["terms"] = [_supplier_term("purchase_moq", offer[2], page["id"], page["text"])]
                value["prices"] = [{"amount": offer[3], "currency": "INR", "unit": offer[1].replace("1 piece", "piece"),
                    "quantity_condition": "minimum " + offer[2], "price_kind": "product",
                    "qualifier": "Invented offer conditions.", "references": [page["id"]],
                    "quotes": [{"page_id": page["id"], "excerpt": page["text"]}], "reason": "Synthetic offer-local quoted price."}]
            values.append(value)
        return {"results": values}, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}


def _supplier_leads(memory, cid, spec):
    """Save each invented lead's pages, then read its terms through the real scoped F path."""
    from product_radar.supplier_capture_store import capture_saved_supplier_bundle, load_saved_supplier_capture
    from product_radar.supplier_checks import extract_scoped_supplier_terms, prepare_supplier_terms
    product = spec["title"]
    for lead in SUPPLIER_LEADS[:spec["suppliers"]]:
        base, pages = _supplier_pages(lead, product)
        saved = capture_saved_supplier_bundle(memory, {"concept_id": cid, "lead": {"id": f"{lead[0]}-{spec['slug']}",
            "urls": list(pages)}}, fetcher=_SupplierReader(pages, lead[1].split(",")[0]))
        captured = {p["url"]: p for p in load_saved_supplier_capture(memory, saved["path"])["bundle"]["item"]["pages"]}
        company = captured[base + "/about"]
        identity = {"kind": "registered_identifier", "scheme": "GSTIN", "value": lead[2],
                    "quotes": [{"page_id": company["id"], "excerpt": "GSTIN " + lead[2] + "."}]}
        workflow = ("Direct customer dispatch" if lead[3] == "Yes" else "Reseller warehouse delivery") + (
            " without a subscription" if lead[4] == "Not required" else " with a subscription")
        choices = [{"scope_kind": "company_workflow", "page_ids": [company["id"]], "company_identity": identity,
                    "workflow": workflow, "offer": None}]
        for index, offer in enumerate(lead[5]):
            page = captured[f"{base}/offers/{index}"]
            choices.append({"scope_kind": "product_offer", "page_ids": [page["id"]], "company_identity": deepcopy(identity),
                            "workflow": workflow, "offer": {"page_id": page["id"], "label": offer[0].format(product=product),
                                "purchase_unit": offer[1], "quotes": [{"page_id": page["id"], "excerpt": page["text"]}]}})
        prepared = prepare_supplier_terms(memory, saved["path"], choices)
        with patch("product_radar.supplier_checks.utc_now", return_value=NOW):
            result = extract_scoped_supplier_terms(memory, prepared, _SupplierRunner(lead, product))
        if result["failures"]:
            raise AssertionError(result)


# Product photos ------------------------------------------------------------------------

def _photo_bytes(colour, slug):
    """A plain synthetic product silhouette (simple shapes only, never a real photo)."""
    image = Image.new("RGB", (640, 640), (246, 243, 238))
    draw = ImageDraw.Draw(image)
    dark = tuple(max(0, value - 60) for value in colour)
    draw.ellipse((150, 520, 490, 580), fill=(222, 216, 206))
    if slug in ("spikes", "macrame"):
        draw.rounded_rectangle((220, 250, 420, 520), radius=40, fill=colour)
        draw.polygon([(270, 250), (320, 90), (370, 250)], fill=dark)
    elif slug == "feeding":
        draw.pieslice((140, 230, 500, 560), 0, 180, fill=colour)
        draw.rounded_rectangle((440, 170, 470, 420), radius=14, fill=dark)
    else:
        draw.rounded_rectangle((200, 170, 440, 520), radius=60, fill=colour)
        draw.rounded_rectangle((260, 120, 380, 180), radius=20, fill=dark)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _photos(memory, ids, media_root):
    """Capture one owned photo for each type that has one, through the real image writer."""
    from product_radar.market_validation import functional_market_context
    from product_radar.product_images import capture_product_image, inspect_image
    from product_radar.report_examples import functional_image_candidates
    for cid, spec in zip(ids, ALL_TYPES):
        if not spec["photo"]:
            continue
        state = memory.snapshot()
        candidate = functional_image_candidates(state, functional_market_context(state, cid, as_of=NOW))[0]
        raw = _photo_bytes(spec["photo"], spec["slug"])

        def fetch(url, raw=raw):
            return {"state": "usable", "reason": "Synthetic product silhouette.", "checked_at": NOW,
                    "requested_url": url, "metadata": inspect_image(raw)}, raw

        capture_product_image(memory, cid, candidate, media_root=media_root, fetcher=fetch)


# The pool ---------------------------------------------------------------------------

def _build_memory(root):
    root = Path(root)
    memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
    discovery = [{"ad_archive_id": str(100 + n), "page_id": str(200 + n), "is_active": True,
                  "start_date": spec["ads"][0], "page_name": f"Invented store {n}",
                  "snapshot": {"title": spec["ad"] + " " + spec["slug"],
                               **({"original_image_url": f"https://images.example.org/visual/{spec['slug']}.png"}
                                  if spec["photo"] else {})}}
                 for n, spec in enumerate(ALL_TYPES)]
    capture(memory, values=discovery, acquisition="visual-discovery", observed=NOW)
    run_flow(memory, _DiscoveryRunner(), limit=len(ALL_TYPES), as_of=NOW)
    state = memory.snapshot()
    by_title = {concept["description"]["label"]: cid for cid, concept in state["concepts"].items()}
    ids = [by_title[spec["title"]] for spec in ALL_TYPES]
    unclear, terms, questions = {}, {}, []
    for n, (cid, spec) in enumerate(zip(ids, ALL_TYPES)):
        state = memory.snapshot()
        children = {child: row for child, row in state["associations"].items() if row["concept_id"] == cid}
        semantics = {row["semantic_id"] for row in children.values()}
        refs = sorted(ref for ref, row in state["observations"].items() if row["semantic_id"] in semantics)
        accept_functional_definition(memory, cid, _definition(spec), basis_refs=refs, reviewer="synthetic", reviewed_at=NOW,
            expected_meaning_hash=effective_functional_definition(state, cid)["meaning_hash"],
            example_decisions={child: {"decision": "same", "reason": "Explicit synthetic example."} for child in children})
        prepare_example_memberships(memory, cid, as_of=NOW)
        meaning = effective_functional_definition(memory.snapshot(), cid)["meaning_hash"]
        unclear[meaning], terms[cid] = spec["unclear"], spec["term"] or spec["title"].lower()
        # A type with "meta": False keeps its discovery advert as an example only (no Meta advertisers counted).
        for ref in (refs + _meta(memory, n, spec)) if spec.get("meta", True) else ():
            _accept(memory, cid, "meta-ads", "observations", ref, ["semantic"], "Synthetic public advert, not sales proof.")
        if spec["crowding"] == "high":
            for ref in _bestseller(memory, n, spec):
                _accept(memory, cid, "amazon-bestsellers", "observations", ref, ["semantic"],
                        "Synthetic public listing, not sales proof.")
        else:
            _marketplace_searches(questions, root, n, cid, spec)
        if spec["trends"]:
            _trends(memory, n, cid, spec)
        if spec["presence"]:
            _presence(memory, n, cid, spec)
        if spec["qc"]:
            _quick_commerce(questions, n, cid, spec)
        _empty_supplier_effort(memory, n, cid)
        if spec["suppliers"]:
            _supplier_leads(memory, cid, spec)
    _answer_questions(memory, questions)
    ensure_query_profiles(memory, _QueryRunner(terms), concept_ids=ids, as_of=NOW)
    reviews = prepare_practical_fit_reviews(memory, concept_ids=ids, as_of=NOW, contract_version=FUNCTIONAL_CONTRACT)
    results = run_fit_reviews(memory, _FitRunner(unclear), [row["review_id"] for row in reviews], limit=len(reviews))
    if any(row["status"] != "accepted" for row in results):
        raise AssertionError(results)
    refresh_crowding(memory, ids, as_of=NOW, limit=len(ids))
    return memory, ids


def _pool(memory, media_root, as_of):
    with patch("product_radar.product_images.MEDIA_ROOT", media_root):
        return build_report_pool(memory, as_of=as_of, recommendation_contract=RECOMMENDATION_CONTRACT)


def _frozen_issue(memory, root, media_root, pool, issue_id, date, history=(), reservations=()):
    """Allocate one complete issue and freeze its owned report assets and packet."""
    state = memory.snapshot()
    allocation = allocate_issues(pool["eligible"], [{"issue_id": issue_id, "client_id": "default", "date": date,
        "revision": 1}], state["published_history"] if history else [], state["issue_reservations"] if reservations else [],
        policy=policy_for_fit("fit-10-v1"))
    issue = allocation["issues"][0]
    if not issue["ready_for_review"]:
        raise AssertionError((issue["counts"], pool["decisions"], allocation["decisions"]))
    facts = [pool["facts_by_concept"][row["concept_id"]] for row in issue["products"]]
    return _functional_report_revision(root, issue, facts, "synthetic", media_root=media_root)


def _publish_earlier_issue(memory, root, media_root, ids):
    """The earlier client issue (the "Updated" type plus the nine fillers),
    held and recorded as published through the real local writers. The
    publication receipt is an explicitly synthetic input; nothing is sent."""
    from product_radar.issue_allocation import allocation_snapshot_hash, record_publication_receipt, reserve_issue
    from product_radar.report_workflow import freeze_review_input
    chosen = {cid for cid, spec in zip(ids, ALL_TYPES) if spec in FILLERS or spec["slug"] == UPDATED_SLUG}
    pool = _pool(memory, media_root, NOW)
    pool = dict(pool, eligible=[row for row in pool["eligible"] if row["concept_id"] in chosen])
    issue, facts = _frozen_issue(memory, root, media_root, pool, EARLIER_ISSUE_ID, EARLIER_DATE)
    freeze_review_input(memory, issue, facts, root=root)
    reserve_issue(memory, issue, report_root=root, as_of=NOW, expected_input_hash=issue["input_hash"],
                  expected_snapshot_hash=allocation_snapshot_hash(memory.snapshot()))
    receipt = {"issue_id": issue["issue_id"], "revision": issue["revision"], "selection_hash": issue["selection_hash"],
        "receipt_id": "synthetic-visual-receipt", "verified": True, "authority": "release_index", "remote_state": "active",
        "package_hash": "a" * 64, "facts_hash": "b" * 64, "server_activated_at": "2026-10-06T03:30:00+00:00",
        "scheduled_at": "2026-10-06T03:30:00+00:00", "server_observed_at": PUBLISHED_AT, "server_deactivated_at": None}
    record_publication_receipt(memory, issue["issue_id"], receipt, as_of=PUBLISHED_AT)


class _UpdateRunner:
    """Synthetic E material-update answer: the newer chart is a useful update."""
    identity = "redesign-visual-material-update-v1"

    def __call__(self, role, payload, folder):
        return {"results": [{"id": row["id"], "decision": "useful_update",
            "reason": "Search interest in the same Google Trends search has risen faster than when it was last shared.",
            "practical_consequence": "Recheck stock and advert plans for this type, as interest is climbing again.",
            "changed_fields": ["market"], "references": ["old:market", "new:market"]}
            for row in payload["items"]]}, {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 0}


def _useful_update(memory, media_root, ids):
    """A newer search chart for the published type (same search, later week), then the real E review."""
    from product_radar.material_updates import run_material_updates
    from product_radar.research_operations import import_trends_evidence
    from test_trends_browser import metadata, weekly
    n = next(index for index, spec in enumerate(ALL_TYPES) if spec["slug"] == UPDATED_SLUG)
    cid, spec = ids[n], ALL_TYPES[n]
    chart = import_trends_evidence(memory, concept_id=cid, acquisition_id=f"visual-chart-{n}-later",
        csv_bytes=weekly(_series("strong_later"), label=spec["term"] + ": (India)"),
        metadata=metadata(term=spec["term"], collected_at=CHART_LATER, source_reference=f"visual-chart-{n}-later.csv"))
    _accept(memory, cid, "google-trends", "trends_evidence", chart["id"], ["parsed", "series"],
            "Relative search interest, not product sales.", at=CHART_LATER)
    work = [row for row in _pool(memory, media_root, LATER)["needed_material_update_work"]
            if row["binding"]["concept_id"] == cid]
    with patch("product_radar.research_ai.utc_now", return_value=LATER):
        outcomes = run_material_updates(memory, work, _UpdateRunner(), as_of=LATER)
    if [row.get("decision") for row in outcomes] != ["useful_update"]:
        raise AssertionError(outcomes)


def rich_issue(root, *, count=10):
    """-> (memory, issue, dataset): a validated schema-3 functional issue with
    narratives for "en" and "hi-Latn", built only through the real writers."""
    if count != len(TYPES):
        raise ValueError("The visual fixture has exactly %d invented types." % len(TYPES))
    root = Path(root)
    memory, ids = _build_memory(root / "inputs")
    media_root = root / MEDIA_FOLDER
    _photos(memory, ids, media_root)
    _publish_earlier_issue(memory, root, media_root, ids)
    _useful_update(memory, media_root, ids)
    issue, facts = _frozen_issue(memory, root, media_root, _pool(memory, media_root, LATER), ISSUE_ID, ISSUE_DATE,
                                 history=True, reservations=True)
    dataset = build_issue_dataset(issue, facts, evidence_type="synthetic")
    validate_dataset(issue, dataset, root=root)
    result = generate_report_narratives(memory, issue, dataset, _Writer())
    if result["failures"] or result["complete_languages"] != ["en", "hi-Latn"]:
        raise AssertionError(result["failures"])
    dataset = result["dataset"]
    validate_dataset(issue, dataset, root=root)
    return memory, issue, dataset


# Narratives -------------------------------------------------------------------------

class _Writer:
    """Synthetic G writer: realistic invented prose in both languages."""
    identity = "redesign-visual-writer-v1"

    def __init__(self):
        self.calls = 0

    def __call__(self, role, payload, folder):
        self.calls += 1
        rows = []
        for item in payload["items"]:
            row = deepcopy(item["output_template"])
            hindi = item["language"] == "hi-Latn"
            if item["kind"] == "report_title":
                row["title"] = "Is hafte ke product sujhaav" if hindi else "This week's product picks"
            else:
                title = next(fact["text"] for fact in item["facts"] if fact["id"] == "title")
                prose = PROSE[title]["hi" if hindi else "en"]
                row["reason"], row["points"] = prose["reason"], list(prose["points"])
                row["checklist_phrases"] = [{"id": criterion["id"], "text": CHECKLIST["hi" if hindi else "en"][criterion["id"]]}
                                            for criterion in item["criteria"]]
                if item["update_fact"]:
                    row["update_sentence"] = UPDATE_SENTENCE["hi" if hindi else "en"]
            rows.append(row)
        return {"results": rows}, {"input_tokens": 0, "output_tokens": 0}


UPDATE_SENTENCE = {"en": "Shared before: search interest has climbed faster since then, so stock and advert plans are worth a recheck.",
                   "hi": "Pehle bhi bataya tha: tab se search interest aur tezi se badha hai, isliye stock aur ad plan dobara dekhein."}


CHECKLIST = {
    "en": {1: "Small enough to pack in a standard courier pouch or box",
           2: "Light and sturdy, so ordinary courier handling is unlikely to damage it",
           5: "Helps buyers feel more sure of getting a routine task right",
           6: "Removes a small daily hassle that buyers notice right away",
           7: "Cuts a few minutes from a routine that people repeat every week",
           8: "Fixes a clear, everyday problem that buyers can name themselves",
           9: "May reduce small repeat purchases, though savings were not measured",
           11: "Makes an ordinary part of home or travel life a little easier",
           12: "Looks and feels like more than its likely selling price",
           13: "Mostly appeals to people who run the household day to day"},
    "hi": {1: "Itna chhota ki aam courier pouch ya box mein aaraam se pack ho jaaye",
           2: "Halka aur mazboot, isliye courier handling mein tootne ka khatra kam",
           5: "Kharidne waale ko lagta hai ki roz ka kaam sahi tareeke se hoga",
           6: "Roz ki ek chhoti pareshani door karta hai jo turant dikhti hai",
           7: "Har hafte dohraaye jaane waale kaam mein kuch minute bachata hai",
           8: "Ek saaf, roz ki problem hal karta hai jise log khud pehchaante hain",
           9: "Chhoti baar baar ki khareed kam ho sakti hai, par bachat naapi nahi gayi",
           11: "Ghar ya safar ki roz ki zindagi ko thoda aasaan banata hai",
           12: "Dekhne aur chhoone mein apni sambhavit keemat se zyada lagta hai",
           13: "Zyaadatar ghar chalane waale logon ko pasand aata hai"},
}


def _prose(en_reason, en_points, hi_reason, hi_points):
    return {"en": {"reason": en_reason, "points": en_points}, "hi": {"reason": hi_reason, "points": hi_points}}


PROSE = {
    "Self-watering plant spike set": _prose(
        "{{title}} suits shoppers who keep indoor plants but travel or forget to water. Search interest has climbed "
        "clearly over the last month, several small sellers advertise it, and two dropshipping catalogues list it. "
        "Few marketplace listings show strong ratings yet, so a careful new seller has room.",
        ["Easy to explain in a short video: fill the bottle, push the spike in, done",
         "Light, unbreakable packing keeps courier damage and returns handling simple",
         "Works as a gift add-on for plant lovers during festive season sales"],
        "{{title}} un logon ke liye theek hai jo ghar mein paudhe rakhte hain par safar karte hain ya paani dena "
        "bhool jaate hain. Pichhle mahine search interest saaf badha hai, kai chhote seller iska ad chala rahe hain, "
        "aur do dropshipping catalogue mein yeh listed hai. Marketplace par abhi kam listings ki ratings mazboot hain.",
        ["Chhote video mein samjhana aasaan: bottle bharo, spike lagao, ho gaya",
         "Halki aur na tootne waali packing se courier ka nuksaan kam rehta hai",
         "Tyohaar ke sale mein paudhon ke shaukeen logon ke liye gift add-on ban sakta hai"]),
    "Silicone suction feeding set": _prose(
        "{{title}} answers a daily worry for parents of toddlers: bowls that slide or tip over at meals. Five "
        "advertisers are running ads, searches are rising steadily, and it is already in stock on one quick "
        "commerce app in Mumbai. Baby products need extra care with safety claims and material proof.",
        ["Parents understand the benefit from one photo of the bowl staying put",
         "Bright colour options make it easy to show in reels and carousel ads",
         "Check food grade material papers before listing any baby feeding item"],
        "{{title}} toddlers ke maa baap ki roz ki pareshani ka jawab hai: khaane ke waqt bowl khisakna ya ulatna. "
        "Paanch advertiser iske ad chala rahe hain, search dheere dheere badh rahi hai, aur Mumbai mein ek quick "
        "commerce app par yeh stock mein hai. Baby products mein safety ke daavon aur material proof ka khaas dhyan "
        "rakhein.",
        ["Ek photo mein bowl ko tika hua dekh kar maa baap fayda samajh jaate hain",
         "Chamkeele rang reels aur carousel ads mein dikhaane mein aasaan hain",
         "Baby feeding item list karne se pehle food grade material ke kaagaz dekh lein"]),
    "Rechargeable LED motion sensor cabinet light": _prose(
        "{{title}} is an early idea: search interest jumped sharply in recent weeks and two new advertisers started "
        "ads within the last month, while marketplaces still show only a couple of quiet listings. Demand is not "
        "proven yet, so treat it as a small test rather than a main product.",
        ["The before and after of a dark cupboard lighting up makes a strong short video",
         "One dropshipping catalogue added it recently as a new arrival",
         "Electrical items need clear charging and battery safety information"],
        "{{title}} ek shuruaati idea hai: haal ke hafton mein search interest tezi se badha aur do naye advertiser ne "
        "pichhle mahine ad shuru kiye, jabki marketplace par abhi sirf do shaant listings hain. Demand abhi pakki nahi "
        "hai, isliye ise main product ki jagah chhote test ki tarah dekhein.",
        ["Andhere cupboard ka roshan hona chhote video mein bahut asar daalta hai",
         "Ek dropshipping catalogue ne ise haal hi mein new arrival mein joda hai",
         "Electrical saamaan ke liye charging aur battery safety ki saaf jankari zaroori hai"]),
    "Electric baby nail trimmer kit": _prose(
        "{{title}} is clearly wanted: seven advertisers, all three dropshipping catalogues and quick commerce stock "
        "in two cities. The catch is competition, because a bestseller listing already leads on Amazon. It suits "
        "a seller who can stand out with content, bundles or service, not price alone.",
        ["Parents worry about cutting tiny nails, so the gentle filing pads sell the idea",
         "Bundle extra pads or a travel pouch to look different from existing sellers",
         "Baby and battery products both need careful safety and compliance checks"],
        "{{title}} ki maang saaf dikhti hai: saat advertiser, teenon dropshipping catalogue aur do shehron mein quick "
        "commerce stock. Dikkat competition ki hai, kyunki Amazon par ek bestseller listing pehle se aage hai. Yeh un "
        "sellers ke liye theek hai jo sirf keemat nahi, behtar content, bundle ya service se alag dikh sakte hain.",
        ["Maa baap chhote nakhoon kaatne se darte hain, naram filing pads yahi dar door karte hain",
         "Extra pads ya travel pouch ka bundle bana kar baaki sellers se alag dikhein",
         "Baby aur battery waale products dono mein safety aur compliance jaanch zaroori hai"]),
    "Compression packing cube set": _prose(
        "{{title}} has steady, year round search interest from travellers, though no matching Meta adverts were "
        "found this week. Marketplace competition looks moderate rather than crowded, and none of the dropshipping "
        "catalogues checked carry it, which leaves space for a seller who sources a well made set.",
        ["Packing demos are easy to film and show the space saved in one shot",
         "Interest holds through the year, with extra attention before holidays",
         "Choose sturdy zips, since weak zips are the main complaint for this type"],
        "{{title}} mein yatriyon ki saal bhar ek jaisi search dilchaspi rehti hai, haalaanki is hafte koi milta "
        "julta Meta ad nahi mila. Marketplace par competition bheed jaisa nahi, theek thaak hai, aur jaanche gaye "
        "dropshipping catalogue mein yeh nahi mila, isliye achha set laane waale seller ke liye jagah hai.",
        ["Packing ka demo film karna aasaan hai aur ek shot mein bachi jagah dikh jaati hai",
         "Dilchaspi saal bhar bani rehti hai, chhuttiyon se pehle thodi zyada",
         "Mazboot zip chunein, kyunki kamzor zip is type ki sabse badi shikayat hai"]),
    "Under-sink pull-out organiser": _prose(
        "{{title}} has proven demand: six advertisers, rising searches and a trending listing on one dropshipping "
        "catalogue. A bestseller already leads on Amazon, so expect strong competition. It can still work for a "
        "seller with good installation videos and a clear size guide for common cupboards.",
        ["A clear size guide avoids the most common reason buyers return storage racks",
         "Before and after cupboard photos explain the benefit without any words",
         "Larger and heavier parcels raise shipping cost, so check courier slabs first"],
        "{{title}} ki maang saabit hai: chhe advertiser, badhti search aur ek dropshipping catalogue par trending "
        "listing. Amazon par ek bestseller pehle se aage hai, isliye kadi takkar ki ummeed rakhein. Achhe installation "
        "video aur aam cupboard ke liye saaf size guide ke saath yeh phir bhi chal sakta hai.",
        ["Saaf size guide se storage rack lautaane ki sabse aam wajah se bacha ja sakta hai",
         "Cupboard ki pehle aur baad ki photo bina shabdon ke fayda samjha deti hai",
         "Bade aur bhaari parcel ka shipping kharch zyada hota hai, courier slab pehle dekhein"]),
    "Macrame hanging wall planter": _prose(
        "{{title}} is a decor piece with a loyal audience among people styling small homes and balconies. Three "
        "advertisers run ads and one dropshipping catalogue lists it as trending, while marketplace listings are "
        "few and lightly rated. No search chart was available, so demand rests on ads and listings.",
        ["Styled photos on a bare wall do most of the selling for decor items",
         "Soft cotton cord packs flat, which keeps shipping simple and cheap",
         "Offer two or three sizes so buyers can build a small wall arrangement"],
        "{{title}} ek decor item hai jise chhote ghar aur balcony sajaane waale log pasand karte hain. Teen advertiser "
        "ad chala rahe hain aur ek dropshipping catalogue mein yeh trending hai, jabki marketplace par listings kam "
        "aur halki rating waali hain. Is type ka search chart nahi mila, isliye maang ads aur listings par tiki hai.",
        ["Khaali deewar par sajaayi photo hi decor item ko bechne ka kaam karti hai",
         "Naram cotton ki rassi flat pack hoti hai, isliye shipping aasaan aur sasti rehti hai",
         "Do ya teen size rakhein taaki log deewar par chhota set bana sakein"]),
    "Collapsible silicone travel kettle": _prose(
        "{{title}} has visible demand from travellers, with three advertisers running ads, but a bestseller listing "
        "already leads on Amazon and no search chart or dropshipping listing was found. It is a crowded, proven "
        "idea that needs a clear reason for buyers to choose a new seller.",
        ["Folding flat is the hook, so show it packed inside a small bag",
         "Hotel and road trip use cases make simple, relatable short videos",
         "Heating products need clear safety instructions in the listing and box"],
        "{{title}} mein yatriyon ki maang dikhti hai, teen advertiser ad chala rahe hain, par Amazon par ek bestseller "
        "listing pehle se aage hai aur koi search chart ya dropshipping listing nahi mili. Yeh bheed waala, saabit "
        "idea hai jismein naye seller ko chunne ki saaf wajah deni hogi.",
        ["Flat fold hona hi khaasiyat hai, ise chhote bag ke andar pack karke dikhayein",
         "Hotel aur road trip waale istemaal se seedhe, samajh aane waale video bante hain",
         "Garam karne waale products ke liye listing aur box mein saaf safety nirdesh zaroori hain"]),
    "Foldable baby bath tub with cushion": _prose(
        "{{title}} helps parents with small bathrooms who want a safer bath routine. Searches have risen sharply "
        "this month, four advertisers run ads and it is in stock on a quick commerce app in Bengaluru. Marketplace "
        "competition is moderate. As a baby product it needs careful safety wording.",
        ["Folding flat after use is a strong point for families in small flats",
         "The support cushion is the detail that reassures first time parents",
         "Bulky box size means checking courier dimensions before pricing it"],
        "{{title}} chhote bathroom waale maa baap ki madad karta hai jo surakshit nahaane ka tareeka chahte hain. Is "
        "mahine search tezi se badhi hai, chaar advertiser ad chala rahe hain aur Bengaluru mein ek quick commerce app "
        "par yeh stock mein hai. Marketplace competition theek thaak hai. Baby product hai, safety ke shabd dhyan se "
        "likhein.",
        ["Istemaal ke baad flat fold hona chhote flat waale parivaron ke liye bada fayda hai",
         "Support cushion woh cheez hai jo pehli baar maa baap bane logon ko bharosa deti hai",
         "Box bada hai, isliye keemat tay karne se pehle courier ke naap dekh lein"]),
    "Solar LED camping lantern": _prose(
        "{{title}} sits in a crowded space: a bestseller listing already leads on Amazon and only one advertiser is "
        "running ads, while searches stay flat through the year. It may suit sellers already serving camping or power "
        "cut buyers who can add it to an existing range rather than lead with it.",
        ["Useful during power cuts as well as trips, which widens the audience",
         "Solar charging is easy to show outdoors in a short daylight video",
         "Battery and solar claims must match the supplier's tested figures exactly"],
        "{{title}} bheed waali jagah mein hai: Amazon par ek bestseller listing pehle se aage hai aur sirf ek "
        "advertiser ad chala raha hai, jabki search saal bhar ek jaisi rehti hai. Yeh un sellers ke liye theek ho "
        "sakta hai jo pehle se camping ya bijli jaane waale kharidaaron ko bechte hain aur ise apni range mein jodte hain.",
        ["Safar ke saath bijli jaane par bhi kaam aata hai, isliye kharidaar zyada hain",
         "Solar charging ko din ki roshni mein chhote video mein dikhana aasaan hai",
         "Battery aur solar ke daave supplier ke jaanche gaye aankdon se hi milne chahiye"]),
}


# Stress editions (layout checks only) ------------------------------------------------------

STRESS_TITLES = {"en": "Kitchen, decor, baby and travel picks to test this week: ten narrow product types, each with a proof",
                 "hi-Latn": "Is hafte test karne ke liye kitchen, decor, baby aur travel ke das narrow product type, saboot sahit"}
_FILLER = {"en": ("steady", "interest", "from", "buyers", "who", "compare", "listings", "before", "they", "order", "online",
                  "and", "expect", "clear", "photos", "with", "honest", "sizes"),
           "hi-Latn": ("kharidaaron", "ki", "lagataar", "dilchaspi", "jo", "order", "se", "pehle", "listing", "milate",
                       "hain", "aur", "saaf", "tasveer", "sahi", "naap", "chahte")}


def _filled(start, language, length, title):
    """Plain words after ``start`` up to ``length`` characters as printed ({{title}} counted as the name)."""
    words, text, index = _FILLER[language], start, 0
    while True:
        word = words[index % len(words)]
        candidate = (text + " " + word) if text else word.capitalize()
        if len(candidate.replace("{{title}}", title)) + 1 > length:
            return text + "."
        text, index = candidate, index + 1


def stress_edition(dataset, language, *, share=1.0):
    """(dataset copy, narrative) for a layout stress print: the title at its 100-character limit and
    every writing slot filled to ``share`` of its budget (reason, three points, the update sentence,
    the ten checklist phrases), with the updated type also given a compliance class, so the
    hardest page one (compliance bar + "What changed") is exercised. Not a valid packet: it is
    printed with ``report_view.render_document`` only, never written or delivered."""
    from product_radar.report_narration import WRITING_LIMITS
    limits = WRITING_LIMITS[language]
    stressed = deepcopy(dataset)
    for product in stressed["products"]:
        if product["status"] == "Updated":
            definition = product["functional_definition"]
            definition["primary_use"] = definition["primary_use"] + " with a battery night light"
    narrative = deepcopy(dataset["narratives"][language])
    narrative["title"] = STRESS_TITLES[language]
    for product, row in zip(stressed["products"], narrative["products"]):
        title = product["title"]
        row["reason"] = _filled("{{title}}", language, int(limits["reason"] * share), title)
        row["points"] = [_filled("", language, int(limits["point"] * share), title) for _ in range(3)]
        if product["status"] == "Updated":
            row["update_sentence"] = _filled("", language, int(limits["update_sentence"] * share), title)
        row["checklist_phrases"] = [dict(phrase, text=_filled("", language, int(limits["checklist"] * share), title))
                                    for phrase in row["checklist_phrases"]]
    return stressed, narrative
