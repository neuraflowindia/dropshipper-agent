"""Positive record dispatch for additive research contracts.

These readers do not validate meaning, resolve aliases, select a current
revision, write records or authorise spending. Owning contract readers retain
those responsibilities. Unknown kinds must never become source children,
market votes or paid Actor tasks merely because they share a field name.
"""

from .models import SafetyError, identifier


FUNCTIONAL_DEFINITION_KIND = "functional-definition-v1"
EXAMPLE_MEMBERSHIP_KIND = "concept-example-v1"
CATALOGUE_SCOPE_KIND = "catalogue-concept-scope-v1"
SCOPED_EVIDENCE_KIND = "scoped-evidence-v1"
RESEARCH_QUESTION_KIND = "research-question-v2"

_PAID_TASK_STATUSES = frozenset(("prepared", "pending", "running", "completed", "partial", "failed"))


def _concept_id(record):
    subject = record.get("subject")
    if isinstance(subject, dict):
        return subject.get("concept_id") if subject.get("kind") == "concept" else None
    return record.get("concept_id")


def _iter_source_kind(state, kind, concept_id):
    for record_id, record in state.get("source_checks", {}).items():
        if (isinstance(record, dict) and record.get("kind") == kind
                and (concept_id is None or _concept_id(record) == concept_id)):
            yield record_id, record


def iter_functional_definitions(state, concept_id=None):
    """Yield only definition records; do not bless their scope or currentness."""
    yield from _iter_source_kind(state, FUNCTIONAL_DEFINITION_KIND, concept_id)


def iter_example_memberships(state, concept_id=None):
    """Yield only original example-membership overlays for the requested type."""
    yield from _iter_source_kind(state, EXAMPLE_MEMBERSHIP_KIND, concept_id)


def iter_scoped_evidence(state, concept_id=None):
    """Direction evidence remains visible unfiltered, never as a fake concept."""
    yield from _iter_source_kind(state, SCOPED_EVIDENCE_KIND, concept_id)


def iter_catalogue_scopes(state, concept_id=None):
    yield from _iter_source_kind(state, CATALOGUE_SCOPE_KIND, concept_id)


def is_paid_query_task(record):
    """Recognise the existing untyped prepared-Actor task contract positively.

    Recognition is not execution permission or ledger proof. Current direct
    controller tasks, v2 research questions and unknown explicit kinds cannot
    enter this path even if they copy legacy paid fields. Historical preparation
    and feedback statuses remain recognised so old uncertainty is recoverable.
    """
    if (not isinstance(record, dict) or "kind" in record
            or not isinstance(record.get("status"), str) or record["status"] not in _PAID_TASK_STATUSES
            or not isinstance(record.get("baseline_concepts"), list)
            or type(record.get("collector_dispatched")) is not bool):
        return False
    config_hash = record.get("config_hash")
    if (not isinstance(config_hash, str) or len(config_hash) != 64
            or any(character not in "0123456789abcdef" for character in config_hash)):
        return False
    try:
        for field in ("plan_id", "family_id", "attempt_id"):
            identifier(record.get(field))
        if "action_id" in record:
            identifier(record["action_id"])
        for concept_id in record["baseline_concepts"]:
            identifier(concept_id)
        # Import at the leaf to share the authoritative legacy request validator
        # without introducing an import cycle with the planner's task readers.
        from .research_planner import semantic_request
        request = semantic_request(record.get("request"))
    except (SafetyError, TypeError):
        return False
    return request["route"] == "apify"
