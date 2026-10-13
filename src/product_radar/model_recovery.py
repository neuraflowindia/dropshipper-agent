"""Explicit current-input recovery for existing model jobs, not a new runner.

Legacy callers retain Jobs behavior. Cooperative callers may prepare a proved
ended, due transient retry only while every original batch input remains current.
"""

from .models import SafetyError, canonical, timestamp
from .research_ai import (Jobs, MODEL, _item_bindings, closed_model_retry_hint,
                         prepare_closed_model_retry, prepared_closed_model_retry_hint, prompt_version)
from .research_memory import key
from .work_turns import _binding, active_work_turn


def _current_scope(memory, runner, role, payload, *, declared_version, current_items,
                   as_of, retry_seconds, retry_not_before, state=None):
    policy = getattr(runner, "closed_retry_policy", None)
    # One verified point-in-time view is enough for this advisory selection.
    # Actual retry preparation and Jobs admission still read freshly below.
    if state is None and policy is not None and _binding(memory) is not None:
        state = memory.snapshot()
    cooperative = (policy is not None and _binding(memory) is not None
                   and state is not None and active_work_turn(memory, state=state) is not None)
    if as_of is None and not cooperative:
        return None
    if cooperative and as_of is None:
        if not isinstance(policy, dict) or set(policy) != {"as_of", "retry_seconds", "retry_not_before"}:
            raise SafetyError("AI_RETRY_POLICY_INVALID")
        def clock():
            at = policy["as_of"]() if callable(policy["as_of"]) else policy["as_of"]
            deadline = policy["retry_not_before"]() if callable(policy["retry_not_before"]) else policy["retry_not_before"]
            timestamp(at)
            if deadline is not None: timestamp(deadline)
            return at, policy["retry_seconds"], deadline
    else:
        def clock():
            timestamp(as_of)
            return as_of, retry_seconds, retry_not_before

    version = prompt_version(role, payload, declared_version)
    identity = getattr(runner, "identity", "codex-chatgpt-high-v1")
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise SafetyError("AI_JOB_INPUT_INVALID")
    items = payload["items"] if current_items is None else current_items
    if (not isinstance(items, (list, tuple)) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
            for row in items) or len({row["id"] for row in items}) != len(items)):
        raise SafetyError("AI_CURRENT_MODEL_INPUT_INVALID")
    current = {row["id"]: canonical(row) for row in items}
    context = {name: value for name, value in payload.items() if name != "items"}

    def complete_current(request):
        return (isinstance(request, dict) and request.get("role") == role and request.get("model") == MODEL
            and request.get("runner") == identity and request.get("prompt_version") == version
            and isinstance(request.get("payload"), dict) and isinstance(request["payload"].get("items"), list)
            and canonical({name: value for name, value in request["payload"].items() if name != "items"}) == canonical(context)
            and all(isinstance(row, dict) and current.get(row.get("id")) == canonical(row)
                    for row in request["payload"]["items"]))

    def check_runner():
        guard = getattr(runner, "check_new_execution", None)
        if guard is not None:
            if not callable(guard): raise SafetyError("AI_EXECUTION_GUARD_INVALID")
            guard()

    def before_execution(request, folder):
        if not complete_current(request):
            raise SafetyError("AI_JOB_PENDING_RECONCILIATION")
        if folder.name.startswith("retry-"):
            at, delay, deadline = clock()
            prepared = prepared_closed_model_retry_hint(memory, key("job", request), as_of=at,
                retry_seconds=delay, not_before=deadline)
            if not prepared["ready"]:
                raise SafetyError(prepared["reason"])
        check_runner()

    state = memory.snapshot() if state is None else state
    bindings = _item_bindings(role, payload, MODEL, version, identity)
    owners = {binding: jid for jid, job in state["jobs"].items() for binding in job["binding_keys"].values()}
    selected_owners = sorted({owners[value] for value in bindings.values() if value in owners})
    return {"state": state, "bindings": bindings, "owners": owners, "selected_owners": selected_owners,
            "complete_current": complete_current, "clock": clock, "check_runner": check_runner,
            "before_execution": before_execution, "version": version}


def _due_owners(memory, runner, scope):
    for owner in scope["selected_owners"]:
        request = scope["state"]["jobs"][owner]["request"]
        if runner is None or not scope["complete_current"](request):
            continue
        at, delay, deadline = scope["clock"]()
        try:
            hint = closed_model_retry_hint(memory, owner, as_of=at, retry_seconds=delay, not_before=deadline,
                                           state=scope["state"])
            if not hint["ready"] and hint["attempt"].startswith("retry-") and hint["failure_code"] is None:
                prepared = prepared_closed_model_retry_hint(memory, owner, as_of=at,
                    retry_seconds=delay, not_before=deadline, state=scope["state"])
                if prepared["ready"]:
                    yield owner, dict(prepared, requires_prepare=False), at, delay, deadline
                    continue
        except SafetyError as error:
            if str(error) in ("AI_RETRY_RECONCILIATION_REQUIRED", "AI_RETRY_ORIGINAL_CHANGED"):
                continue  # Still saved-only. No label or missing proof authorizes execution.
            raise
        if hint["ready"]:
            yield owner, dict(hint, requires_prepare=True), at, delay, deadline


def current_model_retry_bindings(memory, runner, role, payload, *, declared_version=None, current_items=None,
                                  as_of=None, retry_seconds=300, retry_not_before=None, state=None):
    """Pure exact due bindings which a cooperative caller may stop deferring.

    It does not prepare a retry, invoke a runner guard, launch or spend an
    allowance. The eventual wrapper rechecks the same originals under lock.
    Unknown, partial and obsolete original batches remain blocked.
    Advisory callers may supply their already verified state; actual execution
    still obtains fresh state and rechecks its original files and admission.
    """
    scope = _current_scope(memory, runner, role, payload, declared_version=declared_version, current_items=current_items,
                           as_of=as_of, retry_seconds=retry_seconds, retry_not_before=retry_not_before, state=state)
    if scope is None:
        return set()
    due = {owner for owner, _, _, _, _ in _due_owners(memory, runner, scope)}
    return {value for value in scope["bindings"].values() if scope["owners"].get(value) in due}


def run_current_model_job(memory, runner, role, payload, *, declared_version=None, current_items=None,
                          as_of=None, retry_seconds=300, retry_not_before=None, before_new_execution=None):
    """Use ordinary Jobs, with explicit full-current-batch retry authority.

    ``current_items`` is the caller's complete already-validated current group,
    not an arbitrary list of saved historical items. Omission means only this
    supplied batch is current. Explicit ``as_of`` opts in for bounded callers;
    otherwise only an active cooperative runner's retry policy enables it.
    A caller's current-source check can additionally run before a new intent;
    it never replaces the original-batch retry check or gates saved recovery.
    """
    if before_new_execution is not None and not callable(before_new_execution):
        raise SafetyError("AI_EXECUTION_GUARD_INVALID")
    scope = _current_scope(memory, runner, role, payload, declared_version=declared_version, current_items=current_items,
                           as_of=as_of, retry_seconds=retry_seconds, retry_not_before=retry_not_before)
    if scope is None:
        jobs = Jobs(memory, runner, before_new_execution=before_new_execution)
        return jobs.run(role, payload) if declared_version is None else jobs.run(role, payload, declared_version=declared_version)
    for owner, hint, at, delay, deadline in _due_owners(memory, runner, scope):
        if hint["requires_prepare"]:
            scope["check_runner"]()
            prepare_closed_model_retry(memory, owner, as_of=at, retry_seconds=delay,
                                       not_before=deadline, expected_signal=hint["signal"])
    def before_execution(request, folder):
        scope["before_execution"](request, folder)
        if before_new_execution is not None:
            before_new_execution(request, folder)

    return Jobs(memory, runner, before_new_execution=before_execution).run(role, payload,
                                                                          declared_version=scope["version"])
