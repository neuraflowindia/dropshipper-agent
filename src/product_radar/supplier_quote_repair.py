"""One separately owned correction of a completed legacy supplier quotation.

Original replies are immutable. A correction is not an execution retry, a
more-positive supplier search, or permission to rerun an uncertain original.
"""

from copy import deepcopy
import stat

from .models import SafetyError, digest, timestamp
from .research_ai import Jobs, MODEL, _item_bindings, current_attempt, validate_results
from .research_memory import key
from .storage import Journal, atomic_json, load_json, safe_path


VERSION = "research-f-v3"
SCOPE = "one-item-supplier-quote-repair-v1"
FAILURE = "AI_SUPPLIER_QUOTE_INVALID"
RULES = (
    "Correct only the rejected item's quotations using the exact supplied saved pages. "
    "Copy complete literal source spans; do not paraphrase, translate or change words, "
    "currency, punctuation, numbers, restrictions, exceptions or quantity conditions. "
    "Do not remove an exception or qualifier merely to pass validation. Keep the same "
    "supplier question, every non-quotation field, quote count and page attribution. "
    "Only excerpt text may change. If a claim has no exact support, do not invent it; "
    "no quotation-only correction can then be applied. Do not seek a more "
    "positive result, add prices or capabilities, browse or contact anybody. Return "
    "exactly one ordinary F result with the supplied item id."
)


def _hint(state, *, payload=None, runner_identity=None, job_id=None, original_job_id=None,
          scope_id=None, accepted=None, failure=None, local_ready=False, completed_at=None):
    return {"state": state, "payload": payload, "runner_identity": runner_identity,
            "job_id": job_id, "original_job_id": original_job_id, "scope_id": scope_id,
            "accepted": accepted, "failure": failure, "local_ready": local_ready,
            "completed_at": completed_at}


def _request(payload, identity):
    return {"role": "F", "payload": payload, "model": MODEL,
            "prompt_version": VERSION, "runner": identity}


def _checked_request(job_id, job):
    request = job.get("request") if isinstance(job, dict) else None
    if (not isinstance(request, dict) or set(request) != {"role", "payload", "model", "prompt_version", "runner"}
            or not isinstance(request["payload"], dict) or not isinstance(request["runner"], str)
            or request.get("role") != "F"
            or request.get("model") != MODEL or request.get("prompt_version") != VERSION
            or key("job", request) != job_id
            or job.get("binding_keys") != _item_bindings("F", request["payload"], MODEL, VERSION, request["runner"])):
        raise SafetyError("SUPPLIER_QUOTE_REPAIR_JOB_CONFLICT")
    return request


def _load_original(path):
    # Read the one shared job-size limit at call time. This module is imported
    # lazily, so an import-time copy would keep whatever value was current then.
    from .research_ai import MAX_JOB_BYTES
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise SafetyError("SUPPLIER_QUOTE_REPAIR_JOB_CONFLICT")
    value = load_json(path, MAX_JOB_BYTES)
    after = path.lstat()
    fingerprint = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
    if fingerprint(before) != fingerprint(after):
        raise SafetyError("SUPPLIER_QUOTE_REPAIR_ORIGINAL_CHANGED")
    return value


def _quotation_shape(value):
    """Ignore only excerpt text, retaining quote count, page and every claim."""
    value = deepcopy(value)
    for row in value.get("terms", []) + value.get("prices", []):
        for quote in row.get("quotes", []):
            quote["excerpt"] = None
    return value


def _attempt(memory, job_id, request, *, original_only=False):
    base = safe_path(memory.root, "ai_jobs", job_id)
    folder = base if original_only else current_attempt(base)
    paths = {name: folder / name for name in ("intent.json", "result.json", "completion.json",
        "runner_execution.json", "execution_failure.json", "failure.json")}
    if any(path.is_symlink() for path in paths.values()):
        raise SafetyError("SYMLINK_PATH_REFUSED")
    intent = _load_original(paths["intent.json"]) if paths["intent.json"].exists() else None
    if intent is not None and (not isinstance(intent, dict) or intent.get("request") != request):
        raise SafetyError("SUPPLIER_QUOTE_REPAIR_JOB_CONFLICT")
    if paths["result.json"].exists():
        saved = _load_original(paths["result.json"])
        if (intent is None or not isinstance(saved, dict) or saved.get("request_hash") != digest(request)
                or "response" not in saved):
            raise SafetyError("SUPPLIER_QUOTE_REPAIR_JOB_CONFLICT")
        timestamp(saved.get("completed_at"))
        try:
            checked = validate_results("F", request["payload"], saved["response"], declared_version=VERSION)
        except SafetyError as error:
            if str(error) != "AI_ENVELOPE_INVALID":
                raise
            checked = {"accepted": {}, "failures": {row["id"]: str(error) for row in request["payload"]["items"]},
                       "diagnostics": []}
        repair = request["payload"].get("supplier_repair")
        if isinstance(repair, dict):
            item_id = repair["original_item_id"]
            rows = ([row for row in saved["response"]["results"]
                     if isinstance(row, dict) and row.get("id") == item_id]
                    if isinstance(saved["response"], dict) and isinstance(saved["response"].get("results"), list) else [])
            if (item_id in checked["accepted"] and
                    (len(rows) != 1 or _quotation_shape(rows[0]) != _quotation_shape(repair["rejected_result"]))):
                checked["accepted"].pop(item_id)
                checked["failures"][item_id] = "AI_SUPPLIER_QUOTE_REPAIR_SCOPE_INVALID"
        return {"state": "completed", "checked": checked, "saved": saved}
    if paths["completion.json"].exists():
        saved = _load_original(paths["completion.json"])
        if (intent is None or not isinstance(saved, dict) or "response" not in saved
                or "usage" not in saved or type(saved.get("exit_code")) is not int or saved["exit_code"] != 0):
            raise SafetyError("SUPPLIER_QUOTE_REPAIR_JOB_CONFLICT")
        return {"state": "completion"}
    return {"state": "uncertain" if any(path.exists() for path in paths.values())
            or original_only and (base / "retries.json").exists() else "unstarted"}


def _claim_path(memory, scope_id):
    return safe_path(memory.root, "ai_jobs", "quote-repair-" + scope_id) / "owner.json"


def _owner(memory, state, payload):
    descriptor = payload["supplier_repair"]
    owners = [(jid, job) for jid, job in state["jobs"].items()
              if job.get("request", {}).get("role") == "F"
              and isinstance(job["request"].get("payload", {}).get("supplier_repair"), dict)
              and job["request"]["payload"]["supplier_repair"].get("scope_id") == descriptor["scope_id"]]
    if len(owners) > 1:
        raise SafetyError("SUPPLIER_QUOTE_REPAIR_OWNER_CONFLICT")
    request = None
    if owners:
        jid, job = owners[0]
        request = _checked_request(jid, job)
        if request["payload"] != payload:
            raise SafetyError("SUPPLIER_QUOTE_REPAIR_OWNER_CONFLICT")
    path = _claim_path(memory, descriptor["scope_id"])
    if path.is_symlink():
        raise SafetyError("SYMLINK_PATH_REFUSED")
    if path.exists():
        claim = _load_original(path)
        if (not isinstance(claim, dict) or set(claim) != {"schema_version", "scope_id", "job_id", "request"}
                or type(claim["schema_version"]) is not int or claim["schema_version"] != 1
                or claim["scope_id"] != descriptor["scope_id"] or not isinstance(claim["request"], dict)
                or set(claim["request"]) != {"role", "payload", "model", "prompt_version", "runner"}
                or not isinstance(claim["request"].get("runner"), str)
                or claim["request"].get("payload") != payload
                or claim["request"].get("role") != "F" or claim["request"].get("model") != MODEL
                or claim["request"].get("prompt_version") != VERSION
                or key("job", claim["request"]) != claim["job_id"]
                or request is not None and request != claim["request"]):
            raise SafetyError("SUPPLIER_QUOTE_REPAIR_OWNER_CONFLICT")
        request = claim["request"]
    return request


def supplier_model_question(memory, item, *, state):
    """Pure exact F-v3 original/correction question, shared by run and wake.

    Source dates are deliberately absent: their exact application proof is
    checked by the capture owner, while identical page words reuse model work.
    """
    from .suppliers import _supplier_payload
    payload = _supplier_payload([item])
    supplied = payload["items"][0]
    context = {name: value for name, value in payload.items() if name != "items"}
    originals = []
    for jid, job in state["jobs"].items():
        request = job.get("request", {})
        original_payload = request.get("payload", {})
        if (request.get("role") != "F" or request.get("model") != MODEL
                or request.get("prompt_version") != VERSION or "supplier_repair" in original_payload
                or {name: value for name, value in original_payload.items() if name != "items"} != context
                or supplied not in original_payload.get("items", [])):
            continue
        _checked_request(jid, job)
        originals.append((jid, job))
    if not originals:
        return _hint("original", payload=payload)
    if len(originals) != 1:
        raise SafetyError("SUPPLIER_QUOTE_REPAIR_ORIGINAL_CONFLICT")
    original_id, original = originals[0]
    request = original["request"]
    found = _attempt(memory, original_id, request)
    base = {"payload": deepcopy(request["payload"]), "runner_identity": request["runner"],
            "job_id": original_id, "original_job_id": original_id}
    if found["state"] != "completed":
        if found["state"] == "unstarted":
            return _hint("original", **base)
        return _hint("waiting", **base, failure=("AI_SUPPLIER_RESULT_RECONCILIATION_REQUIRED"
            if found["state"] == "completion" else "AI_JOB_PENDING_RECONCILIATION"))
    checked, saved = found["checked"], found["saved"]
    accepted = checked["accepted"].get(supplied["id"])
    if accepted is not None:
        return _hint("original", **base, accepted=accepted, local_ready=True, completed_at=saved["completed_at"])
    failure = checked["failures"].get(supplied["id"])
    if failure != FAILURE:
        return _hint("waiting", **base, failure=failure, completed_at=saved["completed_at"])
    raw = [row for row in saved["response"]["results"] if isinstance(row, dict) and row.get("id") == supplied["id"]]
    if len(raw) != 1:
        raise SafetyError("SUPPLIER_QUOTE_REPAIR_ORIGINAL_CONFLICT")
    binding = original["binding_keys"][supplied["id"]]
    response_hash = digest(saved["response"])
    scope_id = digest([original_id, binding, response_hash])
    descriptor = {"scope": SCOPE, "scope_id": scope_id, "original_job_id": original_id,
        "original_item_id": supplied["id"], "original_binding_key": binding,
        "original_response_hash": response_hash, "failure_code": failure,
        "rejected_result": deepcopy(raw[0]), "rules": RULES}
    corrective = {name: deepcopy(value) for name, value in request["payload"].items() if name != "items"}
    corrective.update(items=[deepcopy(supplied)], supplier_repair=descriptor)
    owner = _owner(memory, state, corrective)
    repair_base = {"payload": corrective, "original_job_id": original_id, "scope_id": scope_id}
    if owner is None:
        return _hint("repair", **repair_base)
    repair_id = key("job", owner)
    repair_base.update(runner_identity=owner["runner"], job_id=repair_id)
    result = _attempt(memory, repair_id, owner, original_only=True)
    if repair_id not in state["jobs"] and result["state"] != "unstarted":
        raise SafetyError("SUPPLIER_QUOTE_REPAIR_JOB_CONFLICT")
    if result["state"] == "unstarted":
        return _hint("repair", **repair_base)
    if result["state"] != "completed":
        return _hint("waiting", **repair_base, failure=("AI_SUPPLIER_RESULT_RECONCILIATION_REQUIRED"
            if result["state"] == "completion" else "AI_JOB_PENDING_RECONCILIATION"))
    accepted = result["checked"]["accepted"].get(supplied["id"])
    if accepted is None:
        return _hint("terminal", **repair_base, failure=result["checked"]["failures"].get(supplied["id"]),
                     completed_at=result["saved"]["completed_at"])
    return _hint("repair", **repair_base, accepted=accepted, local_ready=True,
                 completed_at=result["saved"]["completed_at"])


def supplier_original_retry_question(memory, question, runner, *, current_items, as_of=None,
                                     retry_seconds=300, retry_not_before=None, state=None):
    """Opt in only to existing full-current ORIGINAL transient retry proof.

    ``current_items`` is the complete already-aliased current group. A pure
    readiness caller may supply its verified state and clock; execution still
    uses run_current_model_job's fresh checked retry and admission. The normal
    question remains saved-only, and a corrective job never enters this path.
    """
    if (question["state"] != "waiting" or question["failure"] != "AI_JOB_PENDING_RECONCILIATION"
            or question["scope_id"] is not None or not question["job_id"]
            or question["original_job_id"] != question["job_id"]
            or question["payload"] is None or "supplier_repair" in question["payload"]
            or runner is None or question["runner_identity"] != getattr(runner, "identity", "codex-chatgpt-high-v1")):
        return question
    state = memory.snapshot() if state is None else state
    job = state["jobs"].get(question["job_id"])
    if job is None:
        return question
    request = _checked_request(question["job_id"], job)
    if request["payload"] != question["payload"] or request["runner"] != question["runner_identity"]:
        return question
    owners = {binding: job_id for job_id, row in state["jobs"].items() for binding in row["binding_keys"].values()}
    bindings = set(job["binding_keys"].values())
    if not bindings or any(owners.get(binding) != question["job_id"] for binding in bindings):
        return question
    from .model_recovery import current_model_retry_bindings
    due = current_model_retry_bindings(memory, runner, "F", request["payload"], declared_version=VERSION,
        current_items=current_items, as_of=as_of, retry_seconds=retry_seconds,
        retry_not_before=retry_not_before, state=state)
    return dict(question, state="original", failure=None) if bindings <= due else question


class _SavedOnly:
    def __init__(self, identity):
        self.identity = identity

    def __call__(self, role, payload, folder):
        raise SafetyError("AI_JOB_PENDING_RECONCILIATION")


def _refuse_execution(request, folder):
    raise SafetyError("AI_JOB_PENDING_RECONCILIATION")


def resolve_supplier_question(memory, item, runner=None, *, current_guard=None):
    """Recover or execute at most the one exact corrective question.

    A tiny immutable claim pins its runner before Jobs admission. No research
    or financial lock, nor the scope claim lock, spans an AI wait.
    """
    hint = supplier_model_question(memory, item, state=memory.snapshot())
    if current_guard is not None:
        current_guard(item, memory.snapshot())
    if hint["failure"] == "AI_SUPPLIER_RESULT_RECONCILIATION_REQUIRED":
        try:
            Jobs(memory, _SavedOnly(hint["runner_identity"]), before_new_execution=_refuse_execution).run(
                "F", hint["payload"], declared_version=VERSION)
        except SafetyError as error:
            if str(error) != "AI_ENVELOPE_INVALID":
                raise
        hint = supplier_model_question(memory, item, state=memory.snapshot())
    if hint["state"] != "repair" or hint["accepted"] is not None:
        if (hint["state"] == "original" and hint["job_id"] and hint["accepted"] is None
                and hint["runner_identity"] != getattr(runner, "identity", "codex-chatgpt-high-v1")):
            return dict(hint, state="waiting", failure="AI_JOB_PENDING_RECONCILIATION")
        return hint
    if runner is None:
        return dict(hint, state="waiting", failure="AI_RUNNER_NOT_ENABLED")
    payload, scope_id = hint["payload"], hint["scope_id"]
    path = _claim_path(memory, scope_id)
    with Journal(path.parent).locked():
        current = supplier_model_question(memory, item, state=memory.snapshot())
        if current["scope_id"] != scope_id or current["payload"] != payload:
            raise SafetyError("SUPPLIER_QUOTE_REPAIR_ORIGINAL_CHANGED")
        if current["state"] != "repair" or current["accepted"] is not None:
            return current
        identity = current["runner_identity"] or getattr(runner, "identity", "codex-chatgpt-high-v1")
        request = _request(payload, identity)
        if not path.exists():
            atomic_json(path, {"schema_version": 1, "scope_id": scope_id,
                              "job_id": key("job", request), "request": request})
        if identity != getattr(runner, "identity", "codex-chatgpt-high-v1"):
            return dict(current, state="waiting", runner_identity=identity, job_id=key("job", request),
                        failure="AI_JOB_PENDING_RECONCILIATION")

    def before_execution(request, folder):
        if folder != safe_path(memory.root, "ai_jobs", key("job", request)):
            raise SafetyError("SUPPLIER_QUOTE_REPAIR_RETRY_REFUSED")
        fresh = memory.snapshot()
        current = supplier_model_question(memory, item, state=fresh)
        if (current["scope_id"] != scope_id or current["payload"] != payload
                or current["job_id"] != key("job", request) or current["runner_identity"] != request["runner"]):
            raise SafetyError("SUPPLIER_QUOTE_REPAIR_ORIGINAL_CHANGED")
        if current_guard is not None:
            current_guard(item, fresh)
        guard = getattr(runner, "check_new_execution", None)
        if guard is not None:
            if not callable(guard):
                raise SafetyError("AI_EXECUTION_GUARD_INVALID")
            guard()

    from .suppliers import _save_supplier_aliases, _supplier_aliases
    _, references = _supplier_aliases([item])
    _save_supplier_aliases(memory, runner, payload, references)
    Jobs(memory, runner, before_new_execution=before_execution).run("F", payload, declared_version=VERSION)
    return supplier_model_question(memory, item, state=memory.snapshot())
