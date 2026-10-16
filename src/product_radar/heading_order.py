"""Which headings the discovery waves serve first (Plan R10 W10).

Replaces the alphabetical order. A wave's candidate searches are sorted by
their heading (catalogue family):

1. Headings whose types all came out High crowding (``crowding.crowding_for``;
   at least one type with a known level, every known level High) go last.
2. Never-served headings first (new headings are served as soon as their
   branch is admitted to the map).
3. Then headings with fresh signals since they were last served: new
   bestseller/trending/new-arrival items on the dropshipping platforms, rising
   Trends queries and relevant advertisers' new ads whose words include the
   heading's (``map_materials.load_signals``), more signals first.
4. Then never-served headings the product map's AI top-up suggested
   (``map_topup``): ideas without observed material wait behind the observed
   and freshly signalled ones, but go before plain re-searches.
5. Then the heading served longest ago.

Within each step the categories take turns: the first heading of each
category (by the steps above, then the sub-area with the fewest served
headings, then a fixed spread of the heading ids: a hash, not the alphabet),
then the second of each, and so on, starting from the wave's own rotation.
So a wave preparation reaches every category after a few headings and can
stop looking (``catalogue_discovery.prepare_next_discovery_wave``). Everything is worked out from
saved records and one small local file; nothing is read or sent.
"""

from .discovery import normalise
from .map_materials import load_signals, word_set
from .map_topup import AUTHOR as TOPUP_AUTHOR
from .models import SafetyError, digest, timestamp


def served_times(state):
    """When each heading was last planned into a discovery wave or cycle, and
    which heading each of their acquisitions belongs to."""
    from .catalogue_discovery import VERSION, WAVE_VERSION
    served, owners = {}, {}
    for record in state["batch_runs"].values():
        if record.get("kind") not in (VERSION, WAVE_VERSION):
            continue
        questions = {}
        for planned in record.get("planned_actions", []):
            family_id = planned.get("family_id")
            if not isinstance(family_id, str):
                continue
            prepared = record["prepared_at"]
            if family_id not in served or timestamp(prepared) > timestamp(served[family_id]):
                served[family_id] = prepared
            questions.update({question["id"]: family_id for question in planned.get("questions", [])})
        for question_id, action in record.get("actions", {}).items():
            if question_id in questions and isinstance(action, dict) and action.get("acquisition_id"):
                owners[action["acquisition_id"]] = questions[question_id]
    for outcome in state["query_outcomes"].values():
        family_id, at = outcome.get("family_id"), outcome.get("observed_at")
        if isinstance(family_id, str) and at and (family_id not in served or timestamp(at) > timestamp(served[family_id])):
            served[family_id] = at
    return served, owners


def first_served_times(state):
    """When each heading was first served (planned into a discovery wave or
    cycle, or searched): the same saved facts as ``served_times``, earliest
    instead of latest. A heading missing here has never been searched."""
    from .catalogue_discovery import VERSION, WAVE_VERSION
    first = {}

    def note(family_id, at):
        if isinstance(family_id, str) and at and (family_id not in first or timestamp(at) < timestamp(first[family_id])):
            first[family_id] = at
    for record in state["batch_runs"].values():
        if record.get("kind") in (VERSION, WAVE_VERSION):
            for planned in record.get("planned_actions", []):
                note(planned.get("family_id"), record["prepared_at"])
    for outcome in state["query_outcomes"].values():
        note(outcome.get("family_id"), outcome.get("observed_at"))
    return first


def crowded_headings(state, owners, thresholds=None):
    """Headings whose found types all came out High crowding."""
    from .crowding import crowding_for, load_thresholds
    from .catalogue_discovery import _raise_shared_integrity
    semantics = {}
    for row in state["observations"].values():
        family_id = owners.get(row.get("acquisition_id"))
        if family_id is not None:
            semantics.setdefault(row["semantic_id"], set()).add(family_id)
    types = {}
    for row in state["associations"].values():
        if row.get("status") == "resolved" and row.get("semantic_id") in semantics and row.get("concept_id") in state["concepts"]:
            for family_id in semantics[row["semantic_id"]]:
                types.setdefault(family_id, set()).add(row["concept_id"])
    # Only a type with a saved High fact can be High now (``crowding_for`` returns
    # the saved fact or Not checked), so only those headings need the full read.
    from .crowding import fact_id
    types = {family_id: concept_ids for family_id, concept_ids in types.items()
             if any((state["batch_runs"].get(fact_id(cid)) or {}).get("level") == "high" for cid in concept_ids)}
    if not types:
        return set()
    try:
        thresholds = thresholds or load_thresholds()
    except (SafetyError, OSError) as error:
        _raise_shared_integrity(error)
        return set()  # unreadable crowding settings: crowding unknown, nothing moves back
    levels, crowded = {}, set()
    for family_id, concept_ids in types.items():
        known = []
        for concept_id in sorted(concept_ids):
            if concept_id not in levels:
                try:
                    levels[concept_id] = crowding_for(state, concept_id, thresholds=thresholds)["level"]
                except SafetyError as error:
                    _raise_shared_integrity(error)
                    levels[concept_id] = None  # this type's unreadable fact never moves the heading
            if levels[concept_id] in ("low", "medium", "high"):
                known.append(levels[concept_id])
        if known and all(level == "high" for level in known):
            crowded.add(family_id)
    return crowded


def fresh_counts(catalogue, signals, served):
    """Signals newer than each heading's last serving whose words include the heading's."""
    index = {}
    for family in catalogue["families"]:
        texts = [family["label"], *family["aliases"], *(phrase["text"] for phrase in family["phrases"])]
        for found in {word_set(text) for text in texts}:
            if found:
                index.setdefault(min(found), []).append((family["id"], found))
    counts = {}
    for words, at in signals:
        matched = {family_id for word in words for family_id, found in index.get(word, ()) if found <= words}
        for family_id in matched:
            last = served.get(family_id)
            if last is None or timestamp(at) > timestamp(last):
                counts[family_id] = counts.get(family_id, 0) + 1
    return counts


def heading_facts(state, catalogue, *, root, thresholds=None):
    """Per heading: last served time, fresh-signal count and High-crowding flag;
    per sub-area: how many of its headings were served."""
    served, owners = served_times(state)
    crowded = crowded_headings(state, owners, thresholds)
    fresh = fresh_counts(catalogue, load_signals(root), served)
    branch_served = {}
    for family in catalogue["families"]:
        if family["id"] in served:
            branch_served[family["parent_id"]] = branch_served.get(family["parent_id"], 0) + 1
    facts = {"served": served, "crowded": crowded, "fresh": fresh, "branch_served": branch_served}
    facts["turns"] = heading_turns(catalogue, facts)
    return facts


def _place(family, facts):
    """(High-crowding, tier, fresh, served time): the first steps of the order."""
    served = facts["served"].get(family["id"])
    fresh = facts["fresh"].get(family["id"], 0) if served else 0
    tier = (2 if family.get("author") == TOPUP_AUTHOR else 0) if served is None else 1 if fresh else 3
    return family["id"] in facts["crowded"], tier, -fresh, timestamp(served).timestamp() if served else 0.0


def heading_turns(catalogue, facts):
    """Each heading's turn within its category and step (crowding, tier): the
    categories take turns, the first heading of each, then the second ..."""
    groups = {}
    for family in catalogue["families"]:
        crowded, tier, fresh, served = _place(family, facts)
        groups.setdefault((crowded, tier, family["category"]), []).append(
            ((fresh, served, facts["branch_served"].get(family["parent_id"], 0), digest(normalise(family["id"]))), family["id"]))
    return {family_id: turn for rows in groups.values() for turn, (_, family_id) in enumerate(sorted(rows))}


def heading_key(family, facts, categories):
    """The sort key of one heading (see the module order)."""
    crowded, tier, fresh, served = _place(family, facts)
    return (crowded, tier, facts["turns"].get(family["id"], 0), fresh, served, categories.index(family["category"]),
            facts["branch_served"].get(family["parent_id"], 0), digest(normalise(family["id"])))
