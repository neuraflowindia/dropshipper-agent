"""Reuse equivalent saved work before binding a replacement collector task."""

from copy import deepcopy
from datetime import timedelta

from .models import SafetyError, digest, identifier, timestamp, utc_now
from .research_planner import semantic_key
from .research_records import is_paid_query_task
from .storage import load_json, safe_path


def find_existing_task(memory, action, as_of=None):
    """Return an active exact task or proven fresh complete capture, else None.

    This only chooses existing work. It never launches a provider, changes an
    attempt, refreshes dates, infers a cost, or makes an unknown submission safe
    to repeat. Source/build/options/coverage changes are different requests.
    Family and target describe why work is useful, not a different collection;
    exact work in this memory namespace is reusable without rewriting its owner.
    """
    if not isinstance(action, dict) or "family_id" not in action or "request" not in action:
        raise SafetyError("QUERY_ACTION_INVALID")
    identifier(action["family_id"])
    expected = semantic_key(action["request"])
    now = timestamp(as_of or utc_now())
    state = memory.snapshot()
    with memory.journal.locked():
        attempts = memory.journal.state()[1]
    active, completed = [], []
    for task_id, task in state["query_tasks"].items():
        if not is_paid_query_task(task):
            continue
        if semantic_key(task["request"]) != expected:
            continue
        attempt = attempts.get(task.get("attempt_id"))
        if attempt and attempt.get("config_hash") != task.get("config_hash"):
            raise SafetyError("QUERY_TASK_ATTEMPT_SCOPE_MISMATCH")
        # Unfinished durable work is resumed/reconciled even when old. Freshness
        # controls using a finished result, not duplicating an unknown POST.
        if (attempt and attempt["submission"] in ("reserved", "uncertain", "submitted")
                and (attempt["remote_status"] is None or attempt["charge"] is None)):
            active.append((task_id, task, "Resume or reconcile the existing provider attempt; do not submit a replacement."))
            continue
        if task.get("status") in ("prepared", "pending", "running") and attempt is None:
            active.append((task_id, task, "Reuse the saved preparation rather than bind the same search to another plan."))
            continue
        imported = any(isinstance(c.get("reference"), dict) and c["reference"].get("attempt_id") == task.get("attempt_id")
                       for c in state["captures"].values())
        if attempt and attempt.get("capture") and not imported:
            active.append((task_id, task, "Import and validate the existing saved capture before deciding whether another collection is needed."))
            continue
        if (attempt and attempt.get("submission") == "submitted" and attempt.get("remote_status") == "SUCCEEDED"
                and attempt.get("capture_status") == "not_captured"):
            active.append((task_id, task, "Resume capture of the existing successful run; its output has not been saved yet."))
            continue
        if not attempt or attempt.get("remote_status") != "SUCCEEDED" or attempt.get("controls_ok") is not True:
            continue
        if attempt.get("capture") is None or attempt.get("dataset_id") is None:
            continue
        capture = next((c for c in state["captures"].values()
                        if isinstance(c.get("reference"), dict) and c["reference"].get("attempt_id") == task.get("attempt_id")), None)
        if not capture or capture.get("mapping_status") not in ("usable", "partial"):
            continue
        expected_source = {"marketplace": "amazon"}.get(task["request"]["source"], task["request"]["source"])
        if capture.get("source") != expected_source:
            continue
        try:
            observed = timestamp(capture["observed_at"])
            if observed > now or now - observed > timedelta(days=7):
                continue
            folder = safe_path(memory.journal.root, identifier(task["attempt_id"]))
            manifest = load_json(folder / "capture_manifest.json")
            rows = load_json(folder / "captured_rows.json", 50 * 1024 * 1024)
            if (capture["capture_hash"] != attempt["capture"] or digest(rows) != attempt["capture"]
                    or manifest.get("captured_hash") != attempt["capture"]
                    or manifest.get("dataset_id") != attempt["dataset_id"]
                    or capture["acquisition_id"] != attempt["dataset_id"]
                    or manifest.get("observed_at") != capture["observed_at"]
                    or manifest.get("bounded_capture_complete") is not True or manifest.get("truncated") is not False):
                continue
        except (SafetyError, KeyError, TypeError):
            continue
        completed.append((observed, task_id, task))
    if active:
        task_id, task, reason = sorted(active, key=lambda value: value[0])[0]
        return dict(deepcopy(task), task_id=task_id, reuse_kind="active_task", reuse_reason=reason)
    if completed:
        observed, task_id, task = sorted(completed, key=lambda value: (value[0], value[1]), reverse=True)[0]
        return dict(deepcopy(task), task_id=task_id, reuse_kind="fresh_capture",
                    reuse_reason="Reuse the exact committed complete capture within its seven-day window; collection dates remain unchanged.")
    return None
