"""Durable, finite local work turns shared by source callbacks and model jobs.

Reservations are effort/fairness counters, never money, model usage, or proof of
dispatch. A restart resumes canonical counters. A closed turn cannot launch;
saved answers remain recoverable without any new execution reservation.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import re

from .models import SafetyError, canonical, digest, identifier, timestamp
from .research_memory import change, key


TURN_KIND = "work-turn-v1"
DEFAULT_POLICY = {"source_callbacks": 12, "model_executions": 10,
                  "purpose_items": {"C:product_query_profile": 20, "E:fit": 10, "E:source_support": 5, "F": 5,
                                    "photos": 10},
                  "exploration_slots": 2, "report_slots": 1, "review_slots": 2}
_MODEL_PURPOSES = frozenset(("A", "B", "C", "C:product_query_profile", "D", "E", "E:fit", "E:source_support", "F", "G"))
# Turns saved before the review share existed keep their exact policy bytes.
_OPTIONAL_POLICY_FIELDS = frozenset(("review_slots",))


def _reservations(row, policy):
    """Reject malformed counters/owners before they can authorize a launch."""
    identifier(row.get("programme_id"))
    if type(row.get("programme_turn_sequence")) is not int or row["programme_turn_sequence"] < 1:
        raise SafetyError("WORK_TURN_RECORD_INVALID")
    for token, source in row["reservations"]["source"].items():
        if (not isinstance(source, dict) or set(source) != {"token", "owner_key", "lane", "at", "joined_changes_hash"}
                or source.get("lane") not in ("exploration", "backlog") or source.get("token") != token):
            raise SafetyError("WORK_TURN_RECORD_INVALID")
        identifier(source["owner_key"]); _time(source["at"])
        if not isinstance(source["joined_changes_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", source["joined_changes_hash"]):
            raise SafetyError("WORK_TURN_RECORD_INVALID")
        if token != key("source-callback", [row["id"], source["owner_key"]]):
            raise SafetyError("WORK_TURN_RECORD_INVALID")
    required = {"origin_token", "origin_turn_id", "job_id", "attempt", "request_hash", "binding_hash", "purpose", "item_count",
                "token", "turn_id", "at", "carried_purpose_overage"}
    for token, model in row["reservations"]["model"].items():
        if (not isinstance(model, dict) or set(model) != required or model.get("token") != token or model.get("turn_id") != row["id"]
                or model.get("purpose") not in _MODEL_PURPOSES or type(model.get("item_count")) is not int
                or not 1 <= model["item_count"] <= 10 or type(model.get("carried_purpose_overage")) is not bool):
            raise SafetyError("WORK_TURN_RECORD_INVALID")
        for name in ("origin_token", "origin_turn_id", "job_id", "attempt"):
            identifier(model[name])
        for name in ("request_hash", "binding_hash"):
            if not isinstance(model[name], str) or not re.fullmatch(r"[0-9a-f]{64}", model[name]):
                raise SafetyError("WORK_TURN_RECORD_INVALID")
        _time(model["at"])
        if token != key("model-service", [model["origin_token"], row["id"]]):
            raise SafetyError("WORK_TURN_RECORD_INVALID")
        if model["carried_purpose_overage"] and (model["origin_turn_id"] == row["id"] or model["purpose"] not in policy["purpose_items"]):
            raise SafetyError("WORK_TURN_RECORD_INVALID")
    for token, item in row["reservations"]["items"].items():
        if (not isinstance(item, dict) or set(item) != {"token", "owner_key", "purpose", "item_count", "at", "joined_changes_hash"}
                or item.get("purpose") != "photos" or type(item.get("item_count")) is not int or item["item_count"] < 1
                or item.get("token") != token):
            raise SafetyError("WORK_TURN_RECORD_INVALID")
        identifier(item["owner_key"]); _time(item["at"])
        if not isinstance(item["joined_changes_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["joined_changes_hash"]):
            raise SafetyError("WORK_TURN_RECORD_INVALID")
        if token != key("stage-items", [row["id"], item["purpose"], item["owner_key"]]):
            raise SafetyError("WORK_TURN_RECORD_INVALID")


def _time(value):
    timestamp(value)
    return value


def _retry_local_conflict(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        for attempt in range(3):
            try:
                return function(*args, **kwargs)
            except SafetyError as error:
                if str(error) != "RESEARCH_DEPENDENCY_CHANGED" or attempt == 2:
                    raise
        raise SafetyError("RESEARCH_DEPENDENCY_CHANGED")
    return bounded


def _policy(value):
    result = deepcopy(DEFAULT_POLICY if value is None else value)
    if (not isinstance(result, dict) or not set(DEFAULT_POLICY) - _OPTIONAL_POLICY_FIELDS <= set(result) <= set(DEFAULT_POLICY)
            or not isinstance(result["purpose_items"], dict) or set(result["purpose_items"]) != set(DEFAULT_POLICY["purpose_items"])):
        raise SafetyError("WORK_TURN_POLICY_INVALID")
    for name in ("source_callbacks", "model_executions"):
        if type(result[name]) is not int or not 1 <= result[name] <= 1000:
            raise SafetyError("WORK_TURN_POLICY_INVALID")
    for name in ("exploration_slots", "report_slots"):
        if type(result[name]) is not int or not 0 <= result[name] <= result["source_callbacks" if name == "exploration_slots" else "model_executions"]:
            raise SafetyError("WORK_TURN_POLICY_INVALID")
    # A requested share, not extra executions; review_share() clamps it so the
    # turn total never grows and one ordinary execution always remains.
    if "review_slots" in result and (type(result["review_slots"]) is not int or not 0 <= result["review_slots"] <= 1000):
        raise SafetyError("WORK_TURN_POLICY_INVALID")
    if any(type(number) is not int or not 1 <= number <= 10000 for number in result["purpose_items"].values()):
        raise SafetyError("WORK_TURN_POLICY_INVALID")
    return result


def _lanes(value):
    if value is None:
        return {"exploration": False, "backlog": False}
    if not isinstance(value, dict) or set(value) != {"exploration", "backlog"} or any(type(item) is not bool for item in value.values()):
        raise SafetyError("WORK_TURN_READINESS_INVALID")
    return deepcopy(value)


def _record(state, turn_id):
    identifier(turn_id)
    row = state.get("batch_runs", {}).get(turn_id)
    if not isinstance(row, dict) or row.get("kind") != TURN_KIND or row.get("id") != turn_id:
        raise SafetyError("WORK_TURN_NOT_FOUND")
    policy = _policy(row.get("policy"))
    if row.get("policy_hash") != digest(policy) or type(row.get("turn_sequence")) is not int or row["turn_sequence"] < 1 or row.get("state") not in ("open", "closed"):
        raise SafetyError("WORK_TURN_RECORD_INVALID")
    _time(row.get("opened_at"))
    if row["state"] == "open":
        if row.get("closed_at") is not None or row.get("close_reason") is not None:
            raise SafetyError("WORK_TURN_RECORD_INVALID")
    else:
        _time(row.get("closed_at"))
        reason = row.get("close_reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise SafetyError("WORK_TURN_RECORD_INVALID")
    try:
        if not isinstance(row.get("cursors"), dict) or len(canonical(row["cursors"])) > 16384:
            raise SafetyError("WORK_TURN_RECORD_INVALID")
    except (TypeError, ValueError, RecursionError):
        raise SafetyError("WORK_TURN_RECORD_INVALID") from None
    _lanes(row.get("ready_lanes"))
    if type(row.get("report_ready")) is not bool or type(row.get("review_ready", False)) is not bool:
        raise SafetyError("WORK_TURN_RECORD_INVALID")
    reservations = row.get("reservations")
    if not isinstance(reservations, dict) or set(reservations) != {"source", "model", "items"} or any(not isinstance(item, dict) for item in reservations.values()):
        raise SafetyError("WORK_TURN_RECORD_INVALID")
    _reservations(row, policy)
    counters = {"source_callbacks": len(reservations["source"]), "model_executions": len(reservations["model"]),
                "origin_admissions": sum(item["origin_turn_id"] == turn_id for item in reservations["model"].values()),
                "purpose_items": {name: 0 for name in policy["purpose_items"]}}
    for item in list(reservations["model"].values()) + list(reservations["items"].values()):
        if item["purpose"] in counters["purpose_items"]:
            counters["purpose_items"][item["purpose"]] += item["item_count"]
    if row.get("counters") != counters:
        raise SafetyError("WORK_TURN_COUNTER_CONFLICT")
    if counters["source_callbacks"] > policy["source_callbacks"] or counters["model_executions"] > policy["model_executions"]:
        raise SafetyError("WORK_TURN_COUNTER_CONFLICT")
    for purpose, count in counters["purpose_items"].items():
        if count > policy["purpose_items"][purpose]:
            owners = [item for item in reservations["model"].values() if item["purpose"] == purpose]
            if len(owners) != 1 or not owners[0]["carried_purpose_overage"] or owners[0]["item_count"] != count:
                raise SafetyError("WORK_TURN_COUNTER_CONFLICT")
    return deepcopy(row)


def _context_var(memory):
    # Per-memory context-local binding: independent threads/tasks do not share
    # an unsafe process-global active programme, and nested bindings restore.
    with memory._cache_lock:
        variable = getattr(memory, "_work_turn_binding", None)
        if variable is None:
            variable = ContextVar("product_radar_work_turn", default=None)
            memory._work_turn_binding = variable
        return variable


def _binding(memory):
    return _context_var(memory).get()


# While the coming report sets are short of eligible types, two of a round's
# model executions are held back from the stages that run before discovery,
# until discovery has run (Plan R10 W9). In-process scheduling of one bound
# round only: nothing new is saved, the total allowance is unchanged and
# discovery's own questions are never held back.
DISCOVERY_RESERVED_EXECUTIONS = 2


def _discovery_var(memory):
    with memory._cache_lock:
        variable = getattr(memory, "_work_turn_discovery_reserve", None)
        if variable is None:
            variable = ContextVar("product_radar_discovery_reserve", default=None)
            memory._work_turn_discovery_reserve = variable
        return variable


@contextmanager
def discovery_reserve_scope(memory):
    """Bind one round's discovery hold; it always ends with the round."""
    token = _discovery_var(memory).set({"count": 0})
    try:
        yield
    finally:
        _discovery_var(memory).reset(token)


def hold_for_discovery(memory, count):
    """Set the round's hold. Outside a bound round there is nothing to hold."""
    if type(count) is not int or not 0 <= count <= DISCOVERY_RESERVED_EXECUTIONS:
        raise SafetyError("WORK_TURN_DISCOVERY_RESERVE_INVALID")
    holder = _discovery_var(memory).get()
    if holder is not None:
        holder["count"] = count


def discovery_reserved_slots(memory, turn):
    """Held executions, clamped so writing, review and one ordinary call remain."""
    holder = _discovery_var(memory).get()
    count = holder["count"] if holder is not None else 0
    policy = turn["policy"]
    return max(0, min(count, policy["model_executions"] - policy["report_slots"] - review_share(policy) - 1))


def active_work_turn(memory, *, state=None):
    binding = _binding(memory)
    return None if binding is None else _record(memory.snapshot() if state is None else state, binding["turn_id"])


def review_model_request(request):
    """Accepted-type definition and functional market-check (source) questions.

    These two reviews unlock current-format report stock. They share the
    turn's protected review executions; every other question, including other
    B/E work, uses the ordinary remainder.
    """
    payload = request.get("payload") if isinstance(request, dict) else None
    if not isinstance(payload, dict):
        return False
    if request.get("role") == "B":
        from .functional_definition_review import MODE
        return payload.get("mode") == MODE
    if request.get("role") == "E" and payload.get("mode") == "marketplace_relevance_only":
        from .research_ai import FUNCTIONAL_SOURCE_STYLE
        return payload.get("relevance_style") == FUNCTIONAL_SOURCE_STYLE
    return False


def review_share(policy):
    """Clamp the requested share; it never grows the total or takes the last slot."""
    return max(0, min(policy.get("review_slots", 0), policy["model_executions"] - policy["report_slots"] - 1))


def protected_model_slots(turn, purpose, *, review=False, jobs=None):
    """Unused writing and review shares this purpose may not consume.

    The writing share is withheld from non-G work while frozen writing is due
    (report_ready). The review share is withheld from non-review work while a
    definition or functional market-check question is due (review_ready).
    ``jobs`` identifies which existing reservations already served a review.
    """
    policy, reserved = turn["policy"], list(turn["reservations"]["model"].values())
    used_g = sum(item["purpose"] == "G" for item in reserved)
    withheld = max(0, policy["report_slots"] - used_g) if turn["report_ready"] and purpose != "G" else 0
    share = review_share(policy)
    if turn.get("review_ready") is True and not review and share:
        jobs = jobs or {}
        used = sum(review_model_request((jobs.get(item["job_id"]) or {}).get("request")) for item in reserved)
        withheld += max(0, share - used)
    return withheld


def new_model_work_wait(memory, purpose, *, item_count=1, state=None, review=False):
    """Pure allowance hint for proved-fresh work, never launch authority.

    Original saved jobs and carried reservations must use their existing
    recovery/admission path. In particular, this hint does not implement or
    replace their indivisible-batch exceptions. A supplied verified state is
    useful while selecting one finite group; actual admission remains fresh.
    ``review`` marks a definition/functional market-check question.
    """
    if (not isinstance(purpose, str) or purpose not in _MODEL_PURPOSES or type(item_count) is not int
            or not 1 <= item_count <= 10 or type(review) is not bool):
        raise SafetyError("WORK_TURN_MODEL_BINDING_INVALID")
    if _binding(memory) is None:
        return None
    state = memory.snapshot() if state is None else state
    turn = active_work_turn(memory, state=state)
    if turn["state"] != "open":
        return "WORK_TURN_CLOSED"
    counter, policy = turn["counters"], turn["policy"]
    withheld = protected_model_slots(turn, purpose, review=review, jobs=state.get("jobs"))
    withheld += discovery_reserved_slots(memory, turn)
    if counter["model_executions"] >= policy["model_executions"] - withheld:
        return "WORK_TURN_MODEL_LIMIT"
    if purpose in policy["purpose_items"] and counter["purpose_items"][purpose] + item_count > policy["purpose_items"][purpose]:
        return "WORK_TURN_PURPOSE_LIMIT"
    return None


@contextmanager
def bind_work_turn(memory, turn_id, *, before_model=None, before_source=None):
    row = _record(memory.snapshot(), turn_id)
    if row["state"] != "open":
        raise SafetyError("WORK_TURN_CLOSED")
    if any(callback is not None and not callable(callback) for callback in (before_model, before_source)):
        raise SafetyError("WORK_TURN_GUARD_INVALID")
    variable = _context_var(memory)
    token = variable.set({"turn_id": turn_id, "before_model": before_model, "before_source": before_source})
    try:
        yield row
    finally:
        variable.reset(token)


def check_turn_launch(memory, kind):
    binding = _binding(memory)
    if binding is None:
        return None
    row = _record(memory.snapshot(), binding["turn_id"])
    if row["state"] != "open":
        raise SafetyError("WORK_TURN_CLOSED")
    if kind not in ("model", "source"):
        raise SafetyError("WORK_TURN_GUARD_INVALID")
    callback = binding["before_" + kind]
    if callback is not None:
        callback()
    return row


def open_work_turn(memory, programme_id, *, at, policy=None, ready_lanes=None, report_ready=False,
                   preserve_open_policy=False, review_ready=False):
    """Open a new allowance, or resume the exact validated original allowance.

    Direct callers remain strict about a changed policy. The guarded programme
    dispatcher explicitly preserves an unfinished turn across changed CLI
    limits; those new limits take effect only after the original turn closes.
    ``review_ready`` (due definition/functional market-check work) protects
    the policy's review share, like ``report_ready`` protects writing.
    """
    identifier(programme_id); _time(at)
    if type(preserve_open_policy) is not bool:
        raise SafetyError("WORK_TURN_POLICY_INVALID")
    configured = _policy(policy)
    lanes = _lanes(ready_lanes)
    if type(report_ready) is not bool or type(review_ready) is not bool:
        raise SafetyError("WORK_TURN_READINESS_INVALID")
    for retry in range(3):
        state = memory.snapshot()
        turns = [row for row in state["batch_runs"].values() if row.get("kind") == TURN_KIND]
        for candidate in turns:
            _record(state, candidate["id"])
        opened = [row for row in turns if row["programme_id"] == programme_id and row["state"] == "open"]
        if len(opened) > 1:
            raise SafetyError("WORK_TURN_OWNER_CONFLICT")
        if opened:
            if (not preserve_open_policy and policy is not None
                    and opened[0]["policy_hash"] != digest(configured)):
                raise SafetyError("WORK_TURN_POLICY_CHANGED")
            return deepcopy(opened[0])
        sequence = max((row["turn_sequence"] for row in turns), default=0) + 1
        turn_id = key("work-turn", [programme_id, sequence])
        row = {"id": turn_id, "kind": TURN_KIND, "programme_id": programme_id, "turn_sequence": sequence,
               "programme_turn_sequence": 1 + sum(item["programme_id"] == programme_id for item in turns),
               "policy": configured, "policy_hash": digest(configured), "state": "open", "opened_at": at,
               "closed_at": None, "close_reason": None, "ready_lanes": lanes, "report_ready": report_ready,
               "review_ready": review_ready, "cursors": {}, "reservations": {"source": {}, "model": {}, "items": {}},
               "counters": {"source_callbacks": 0, "model_executions": 0, "origin_admissions": 0,
                            "purpose_items": {name: 0 for name in configured["purpose_items"]}}}
        def unchanged(current):
            old = [item for item in current["batch_runs"].values() if item.get("kind") == TURN_KIND]
            if (any(item["programme_id"] == programme_id and item["state"] == "open" for item in old)
                    or max((item["turn_sequence"] for item in old), default=0) + 1 != sequence):
                raise SafetyError("RESEARCH_DEPENDENCY_CHANGED")
        try:
            memory.commit(key("open-work-turn", [turn_id, digest(row)]), [change("batch_runs", turn_id, row)], validate=unchanged)
        except SafetyError as error:
            if str(error) != "RESEARCH_DEPENDENCY_CHANGED" or retry == 2:
                raise
            continue
        return row
    raise SafetyError("RESEARCH_DEPENDENCY_CHANGED")


@_retry_local_conflict
def close_work_turn(memory, turn_id, *, at, reason, cursors=None):
    _time(at)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise SafetyError("WORK_TURN_CLOSE_REASON_INVALID")
    state = memory.snapshot(); original = _record(state, turn_id)
    if original["state"] == "closed":
        return original
    if cursors is not None and (not isinstance(cursors, dict) or len(canonical(cursors)) > 16384):
        raise SafetyError("WORK_TURN_CURSOR_INVALID")
    row = {**original, "state": "closed", "closed_at": at, "close_reason": reason,
           "cursors": deepcopy(original["cursors"] if cursors is None else cursors)}
    memory.commit(key("close-work-turn", [turn_id, digest(row)]), [change("batch_runs", turn_id, row, original)])
    return row


def _commit_reservation(memory, original, row, changes, *, validate=None):
    if not isinstance(changes, (list, tuple)):
        raise SafetyError("WORK_TURN_JOINED_CHANGES_INVALID")
    if any(not isinstance(item, dict) or (item.get("table") == "batch_runs" and item.get("id") == row["id"]) for item in changes):
        raise SafetyError("WORK_TURN_JOINED_CHANGES_INVALID")
    joined = [change("batch_runs", row["id"], row, original), *deepcopy(changes)]
    def unchanged(current):
        actual = _record(current, row["id"])
        if actual != original or actual["state"] != "open":
            raise SafetyError("RESEARCH_DEPENDENCY_CHANGED")
        if validate is not None:
            validate(current)
    memory.commit(key("reserve-work-turn", joined), joined, validate=unchanged)


def _joined_hash(changes):
    if not isinstance(changes, (list, tuple)) or any(not isinstance(item, dict) for item in changes):
        raise SafetyError("WORK_TURN_JOINED_CHANGES_INVALID")
    return digest(list(changes))


def _verify_joined_replay(memory, saved, changes):
    if saved["joined_changes_hash"] != _joined_hash(changes):
        raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")
    state = memory.snapshot()
    for item in changes:
        if (not {"table", "id", "value", "previous_hash"} == set(item)
                or item["table"] not in state or not isinstance(item["id"], str)
                or state[item["table"]].get(item["id"]) != item["value"]):
            raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")


@_retry_local_conflict
def reserve_source_callback(memory, owner_key, *, at, changes=(), lane="backlog", ready_lanes=None):
    """Reserve once per invocation owner; repeating a handler needs a NEW owner.

    Saved-only reconciliation is still a handler invocation. Pure local apply
    is not. A crash consumes the callback already reserved. Joined intent
    changes and the slot are saved in the same canonical event.
    """
    identifier(owner_key); _time(at)
    original = check_turn_launch(memory, "source")
    if original is None:
        raise SafetyError("WORK_TURN_NOT_BOUND")
    if lane not in ("backlog", "exploration"):
        raise SafetyError("WORK_TURN_SOURCE_LANE_INVALID")
    token = key("source-callback", [original["id"], owner_key])
    saved = original["reservations"]["source"].get(token)
    if saved is not None:
        if saved["lane"] != lane:
            raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")
        _verify_joined_replay(memory, saved, changes)
        return deepcopy(saved)
    row = deepcopy(original)
    lanes = _lanes(ready_lanes) if ready_lanes is not None else row["ready_lanes"]
    consumed = row["counters"]["source_callbacks"]
    policy = row["policy"]
    if all(lanes.values()) and policy["source_callbacks"] == 1:
        # With only one available slot, alternate the programme's lane across
        # turns. A global inter-programme sequence could starve one programme's
        # lane when several programmes happen to take alternating global turns.
        preferred = "exploration" if row["programme_turn_sequence"] % 2 else "backlog"
        if lane != preferred:
            raise SafetyError("WORK_TURN_SOURCE_LIMIT")
    other = "exploration" if lane == "backlog" else "backlog"
    used_other = sum(item["lane"] == other for item in row["reservations"]["source"].values())
    protected = policy["exploration_slots"] if other == "exploration" else 1
    if all(lanes.values()):
        protected = min(max(1, protected), policy["source_callbacks"] - 1)
    withheld = max(0, protected - used_other) if lanes[other] else 0
    if consumed >= policy["source_callbacks"] - withheld:
        raise SafetyError("WORK_TURN_SOURCE_LIMIT")
    saved = {"token": token, "owner_key": owner_key, "lane": lane, "at": at, "joined_changes_hash": _joined_hash(changes)}
    row["ready_lanes"] = lanes
    row["reservations"]["source"][token] = saved
    row["counters"]["source_callbacks"] += 1
    _commit_reservation(memory, original, row, changes)
    return deepcopy(saved)


@_retry_local_conflict
def reserve_stage_items(memory, purpose, owner_key, count, *, at, changes=(), validate=None):
    """Shared non-model item bound (currently photos), not source admission."""
    identifier(owner_key); _time(at)
    original = active_work_turn(memory)
    if original is None:
        raise SafetyError("WORK_TURN_NOT_BOUND")
    if original["state"] != "open":
        raise SafetyError("WORK_TURN_CLOSED")
    if purpose != "photos" or type(count) is not int or count < 1:
        raise SafetyError("WORK_TURN_PURPOSE_INVALID")
    if validate is not None and not callable(validate):
        raise SafetyError("WORK_TURN_JOINED_VALIDATOR_INVALID")
    token = key("stage-items", [original["id"], purpose, owner_key])
    saved = original["reservations"]["items"].get(token)
    if saved is not None:
        if saved["item_count"] != count:
            raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")
        _verify_joined_replay(memory, saved, changes)
        if validate is not None:
            validate(memory.snapshot())
        return deepcopy(saved)
    if original["counters"]["purpose_items"][purpose] + count > original["policy"]["purpose_items"][purpose]:
        raise SafetyError("WORK_TURN_PURPOSE_LIMIT")
    row = deepcopy(original)
    saved = {"token": token, "owner_key": owner_key, "purpose": purpose, "item_count": count, "at": at,
             "joined_changes_hash": _joined_hash(changes)}
    row["reservations"]["items"][token] = saved
    row["counters"]["purpose_items"][purpose] += count
    _commit_reservation(memory, original, row, changes, validate=validate)
    return deepcopy(saved)


def stage_item_wait(memory, purpose, *, item_count=1, state=None):
    """Pure hint before preparing a fresh stage item; never a reservation.

    A spent photo allowance should not first claim owners and save intents
    only for reserve_stage_items to refuse. Saved originals need no item.
    """
    if purpose != "photos" or type(item_count) is not int or item_count < 1:
        raise SafetyError("WORK_TURN_PURPOSE_INVALID")
    turn = active_work_turn(memory, state=state)
    if turn is None:
        return None
    if turn["state"] != "open":
        return "WORK_TURN_CLOSED"
    if turn["counters"]["purpose_items"][purpose] + item_count > turn["policy"]["purpose_items"][purpose]:
        return "WORK_TURN_PURPOSE_LIMIT"
    return None


def model_purpose(request):
    role = request["role"]
    payload = request["payload"]
    mode = payload.get("mode")
    if role == "C" and mode == "product_query_profile":
        return "C:product_query_profile"
    if role == "E" and mode in ("fit_assessment", "fit_partial_review"):
        return "E:fit"
    if role == "E" and mode == "marketplace_relevance_only":
        return "E:source_support"
    return role


def _verify_origin(state, job, attempt, admission):
    required = {"origin_token", "origin_turn_id", "job_id", "attempt", "request_hash", "binding_hash", "purpose", "item_count", "at", "services"}
    if (not isinstance(job, dict) or not isinstance(job.get("request"), dict) or not isinstance(job.get("binding_keys"), dict)
            or not isinstance(admission, dict) or set(admission) != required or not isinstance(admission.get("services"), dict)):
        raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")
    origin = _record(state, admission["origin_turn_id"])
    expected_token = key("model-origin", [origin["id"], admission["job_id"], attempt, digest(job["request"]), digest(job["binding_keys"])])
    service_token = key("model-service", [expected_token, origin["id"]])
    service = origin["reservations"]["model"].get(service_token)
    if (admission["job_id"] != key("job", job["request"]) or admission["origin_token"] != expected_token or admission["attempt"] != attempt
            or admission["request_hash"] != digest(job["request"]) or admission["binding_hash"] != digest(job["binding_keys"])
            or admission["purpose"] != model_purpose(job["request"]) or admission["item_count"] != len(job["binding_keys"])
            or admission["services"].get(origin["id"]) != service_token or service is None
            or any(service[field] != admission[field] for field in ("origin_token", "origin_turn_id", "job_id", "attempt", "request_hash", "binding_hash", "purpose", "item_count"))):
        raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")


@_retry_local_conflict
def admit_model_job(memory, job_id, request, binding_keys, *, attempt, at):
    """Join original job metadata/admission and current execution service once.

    The caller must have proved the actual attempt has NO intent/result before
    calling. Intent presence/absence is rechecked by Jobs under its short lock.
    A carried admission keeps its origin and reserves this turn's full payload.
    """
    identifier(job_id); identifier(attempt); _time(at)
    original = check_turn_launch(memory, "model")
    if original is None:
        return None
    from .research_ai import MODEL, _item_bindings, current_attempt, prompt_version
    from .storage import safe_path
    try:
        if (not isinstance(request, dict) or set(request) != {"role", "payload", "model", "prompt_version", "runner"}
                or not isinstance(request["payload"], dict) or not isinstance(request["payload"].get("items"), list)
                or not 1 <= len(request["payload"]["items"]) <= 10 or len(canonical(request["payload"])) > 256 * 1024
                or request["model"] != MODEL or not isinstance(request["runner"], str) or not request["runner"]
                or len(request["runner"]) > 160 or not isinstance(binding_keys, dict) or job_id != key("job", request)
                or prompt_version(request["role"], request["payload"], request["prompt_version"]) != request["prompt_version"]):
            raise SafetyError("WORK_TURN_MODEL_BINDING_INVALID")
        ids = [identifier(item["id"]) for item in request["payload"]["items"]]
        if (len(ids) != len(set(ids)) or binding_keys != _item_bindings(request["role"], request["payload"], MODEL,
                                                                     request["prompt_version"], request["runner"])):
            raise SafetyError("WORK_TURN_MODEL_BINDING_INVALID")
    except (TypeError, KeyError, ValueError, RecursionError):
        raise SafetyError("WORK_TURN_MODEL_BINDING_INVALID") from None
    purpose = model_purpose(request)
    if purpose not in _MODEL_PURPOSES:
        raise SafetyError("WORK_TURN_MODEL_BINDING_INVALID")
    state = memory.snapshot()
    previous = state["jobs"].get(job_id)
    if previous is not None and (previous.get("request") != request or previous.get("binding_keys") != binding_keys):
        raise SafetyError("WORK_TURN_MODEL_BINDING_INVALID")
    job = deepcopy(previous if previous is not None else {"request": request, "binding_keys": binding_keys})
    admissions = job.setdefault("turn_admissions", {})
    if not isinstance(admissions, dict):
        raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")
    admission = admissions.get(attempt)
    if admission is not None:
        _verify_origin(state, job, attempt, admission)
    origin_token = key("model-origin", [original["id"], job_id, attempt, digest(request), digest(binding_keys)]) if admission is None else admission["origin_token"]
    service_token = key("model-service", [origin_token, original["id"]])
    if service_token in original["reservations"]["model"]:
        saved = original["reservations"]["model"][service_token]
        if admission is None or admission.get("services", {}).get(original["id"]) != service_token:
            raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")
        return deepcopy(saved)
    if admission is not None and (admission["request_hash"] != digest(request) or admission["binding_hash"] != digest(binding_keys)
                                  or admission["purpose"] != purpose or admission["item_count"] != len(binding_keys)):
        raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")
    row = deepcopy(original)
    base = safe_path(memory.root, "ai_jobs", job_id)
    folder = current_attempt(base)
    if ("original" if folder == base else folder.name) != attempt:
        raise SafetyError("WORK_TURN_MODEL_ATTEMPT_CHANGED")
    if any((folder / name).exists() for name in ("intent.json", "runner_execution.json", "execution_failure.json", "failure.json", "result.json", "completion.json")):
        raise SafetyError("AI_JOB_PENDING_RECONCILIATION")
    counter, policy = row["counters"], row["policy"]
    withheld = protected_model_slots(row, purpose, review=review_model_request(request), jobs=state["jobs"])
    withheld += discovery_reserved_slots(memory, row)
    if counter["model_executions"] >= policy["model_executions"] - withheld:
        raise SafetyError("WORK_TURN_MODEL_LIMIT")
    overage = False
    if purpose in policy["purpose_items"] and counter["purpose_items"][purpose] + len(binding_keys) > policy["purpose_items"][purpose]:
        # An older indivisible, proved-never-intented batch must not wait
        # forever merely because today's fair item size is smaller. Admit one
        # dedicated full old owner only; its full count prevents more work of
        # this purpose in this turn. New jobs cannot use this exception.
        overage = (admission is not None and _record(state, admission["origin_turn_id"])["turn_sequence"] < row["turn_sequence"]
                   and counter["purpose_items"][purpose] == 0 and len(binding_keys) <= 10)
        if not overage:
            if previous is not None and admission is None and counter["purpose_items"][purpose] == 0 and len(binding_keys) > policy["purpose_items"][purpose]:
                raise SafetyError("WORK_TURN_LEGACY_PAYLOAD_POLICY_WAIT")
            raise SafetyError("WORK_TURN_PURPOSE_LIMIT")
    if admission is None:
        admission = {"origin_token": origin_token, "origin_turn_id": row["id"], "job_id": job_id, "attempt": attempt,
                     "request_hash": digest(request), "binding_hash": digest(binding_keys), "purpose": purpose,
                     "item_count": len(binding_keys), "at": at, "services": {}}
        counter["origin_admissions"] += 1
    else:
        admission = deepcopy(admission)
    saved = {field: deepcopy(admission[field]) for field in ("origin_token", "origin_turn_id", "job_id", "attempt", "request_hash", "binding_hash", "purpose", "item_count")}
    saved.update({"token": service_token, "turn_id": row["id"], "at": at, "carried_purpose_overage": overage})
    admission["services"][row["id"]] = service_token
    job["turn_admissions"][attempt] = admission
    row["reservations"]["model"][service_token] = saved
    counter["model_executions"] += 1
    if purpose in counter["purpose_items"]:
        counter["purpose_items"][purpose] += len(binding_keys)
    _commit_reservation(memory, original, row, [change("jobs", job_id, job, previous)])
    return deepcopy(saved)


def verify_model_admission(memory, admission, *, check_guard=True):
    """Verify same live service/origin after admission and before intent write."""
    row = check_turn_launch(memory, "model") if check_guard else active_work_turn(memory)
    if (row is None or row["state"] != "open" or row["id"] != admission["turn_id"]
            or row["reservations"]["model"].get(admission["token"]) != admission):
        raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")
    state = memory.snapshot()
    job = state["jobs"].get(admission["job_id"])
    admissions = None if not isinstance(job, dict) else job.get("turn_admissions")
    origin = None if not isinstance(admissions, dict) else admissions.get(admission["attempt"])
    if (not isinstance(origin, dict) or origin.get("origin_token") != admission["origin_token"]
            or not isinstance(origin.get("services"), dict) or origin["services"].get(row["id"]) != admission["token"]):
        raise SafetyError("WORK_TURN_RESERVATION_CONFLICT")
    _verify_origin(state, job, admission["attempt"], origin)
    return deepcopy(admission)
