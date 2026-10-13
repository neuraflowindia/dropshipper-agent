"""Durable local journal. One filesystem lock protects validation and writes."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

from .models import SafetyError, canonical, decode_json, digest, identifier, timestamp, usd, utc_now
from .source_deadline import local_source_work


MAX_JOURNAL = 8 * 1024 * 1024
STAGES = {"meta": "0.20", "amazon": "0.42", "trends": "0.03", "correction": "0.10"}
POLICY_VERSION = "apify-free-450-ballpark-v1"
PRODUCTION_SCOPE_VERSION = "production-allocation-v1"
PERIOD_GRANT = Decimal("5")
PERIOD_CAP = Decimal("4.50")
PROTECTED_BUFFER = Decimal("0.50")
RENEWED_READ_VERSION = "apify-original-account-read-funding-v1"
# Owner permits a few cents either side; plan at 4.50, tolerate at most five
# cents of rounding/variation automatically. This is not a paid-plan opt-in.
PENNY_TOLERANCE = Decimal("0.05")


def renewed_read_operations(attempts, namespace=None):
    """Derived read funding, separate from original acquisition money."""
    return [dict(value, attempt_id=attempt_id) for attempt_id, attempt in attempts.items()
            for value in attempt.get("renewed_reads", {}).values()
            if namespace is None or value["namespace"] == namespace]


def _renewed_read_commitment(attempts, namespace):
    return sum((usd(row["charge"] if row["charge"] is not None else row["cap"])
                for row in renewed_read_operations(attempts, namespace)), Decimal(0))


def safe_path(root, *parts):
    root = Path(root).absolute()
    # Reject symlink components, including a linked data root. Local files are
    # owner-controlled; this is not a defence against a malicious OS account.
    for part in parts:
        identifier(part)
    destination = root.joinpath(*parts)
    for component in [destination, *destination.parents]:
        if component.is_symlink():
            raise SafetyError("SYMLINK_PATH_REFUSED")
    return destination


@local_source_work("response_save")
def atomic_bytes(path, encoded):
    path = Path(path)
    safe_path(path.parent)  # reject existing symlinks through the parent
    if path.is_symlink():
        raise SafetyError("SYMLINK_PATH_REFUSED")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(encoded, bytes):
        raise SafetyError("INVALID_RAW_SAVE")
    descriptor, temporary = tempfile.mkstemp(prefix=".save-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value):
    atomic_bytes(path, canonical(value) + b"\n")


def load_json(path, limit=32768):
    path = Path(path)
    safe_path(path.parent)
    if path.is_symlink():
        raise SafetyError("SYMLINK_PATH_REFUSED")
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise SafetyError("SAVED_FILE_SIZE_LIMIT")
        return decode_json(raw)
    except OSError:
        raise SafetyError("SAVED_FILE_UNAVAILABLE") from None


class Journal:
    def __init__(self, root):
        self.root = safe_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "events.jsonl"
        self._locked = False

    @contextmanager
    def locked(self, *, timeout=0):
        # Only short local record operations opt into waiting. Provider launch
        # guards remain immediate by default; this never steals a held lock.
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 <= timeout <= 5):
            raise SafetyError("LOCK_WAIT_INVALID")
        if self._locked:
            raise SafetyError("NESTED_LOCK_REFUSED")
        lock_path = self.root / "runner.lock"
        if lock_path.is_symlink():
            raise SafetyError("SYMLINK_PATH_REFUSED")
        with lock_path.open("a+b") as handle:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SafetyError("RUNNER_ALREADY_ACTIVE") from None
                    time.sleep(min(0.05, remaining))
            self._locked = True
            try:
                # Kernel lock ownership is authoritative, not PID/age. Linux
                # releases it on crash; stale diagnostic metadata is harmless.
                stat = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
                info = {"pid": os.getpid(), "process_start_ticks": stat[19], "started_at": utc_now()}
                handle.seek(0)
                handle.truncate()
                handle.write(canonical(info))
                handle.flush()
                os.fsync(handle.fileno())
                yield self
            finally:
                self._locked = False
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _require_lock(self):
        if not self._locked:
            raise SafetyError("LOCK_REQUIRED")

    def events(self):
        self._require_lock()
        from .financial_segments import enabled, read
        if enabled(self):
            return read(self)[0]
        if self.path.is_symlink():
            raise SafetyError("SYMLINK_PATH_REFUSED")
        if not self.path.exists():
            return []
        with self.path.open("rb") as handle:
            raw = handle.read(MAX_JOURNAL + 1)
        if len(raw) > MAX_JOURNAL or (raw and not raw.endswith(b"\n")):
            raise SafetyError("JOURNAL_CORRUPT_REVIEW_REQUIRED")
        events, previous, ids = [], "0" * 64, set()
        try:
            for line in raw.splitlines():
                event = json.loads(line)
                if set(event) != {"version", "seq", "event_id", "at", "kind", "data", "previous", "hash"}:
                    raise ValueError
                body = {k: v for k, v in event.items() if k != "hash"}
                if type(event["version"]) is not int or type(event["seq"]) is not int or event["version"] != 1 or event["seq"] != len(events) + 1 or event["previous"] != previous or digest(body) != event["hash"]:
                    raise ValueError
                identifier(event["event_id"])
                timestamp(event["at"])
                if event["event_id"] in ids:
                    raise ValueError
                previous = event["hash"]
                ids.add(event["event_id"])
                events.append(event)
        except (ValueError, TypeError, KeyError, UnicodeError, AttributeError, SafetyError):
            raise SafetyError("JOURNAL_CORRUPT_REVIEW_REQUIRED") from None
        return events

    @staticmethod
    def _reduce(events, *, initial=None):
        budgets, attempts = deepcopy(initial) if initial is not None else ({}, {})
        for event in events:
            data, kind = event["data"], event["kind"]
            try:
                if kind == "budget":
                    if set(data) != {"namespace", "allowance", "production_provision", "emergency"} or data["namespace"] in budgets:
                        raise ValueError
                    account, period = data["namespace"].split(":")
                    identifier(account); identifier(period)
                    for field in ("allowance", "production_provision", "emergency"):
                        usd(data[field])
                    if usd(data["production_provision"]) + usd(data["emergency"]) > usd(data["allowance"]):
                        raise ValueError
                    budgets[data["namespace"]] = data
                elif kind == "budget_policy":
                    if (set(data) != {"namespace", "version", "future_allocation", "decision"}
                            or data["namespace"] not in budgets or data["version"] != POLICY_VERSION):
                        raise ValueError
                    identifier(data["decision"])
                    if usd(data["future_allocation"]) > PERIOD_CAP:
                        raise ValueError
                    # Derived state only: original budget/event bytes never change.
                    budgets[data["namespace"]] = {**budgets[data["namespace"]], "policy": dict(data)}
                elif kind == "production_scope":
                    if (set(data) != {"namespace", "version", "source_allocations", "reason", "decision"}
                            or data["namespace"] not in budgets or data["version"] != PRODUCTION_SCOPE_VERSION
                            or not isinstance(data["reason"], str) or not 1 <= len(data["reason"].strip()) <= 2000
                            or not isinstance(data["source_allocations"], dict) or not data["source_allocations"]
                            or not set(data["source_allocations"]) <= set(STAGES)):
                        raise ValueError
                    identifier(data["decision"])
                    budget = budgets[data["namespace"]]
                    if sum((usd(v) for v in data["source_allocations"].values()), Decimal(0)) > usd(budget["policy"]["future_allocation"]):
                        raise ValueError
                    budget["production_scope"] = deepcopy(data)
                elif kind in ("reserved", "reserved_production"):
                    base = {"attempt", "namespace", "stage", "cap", "config_hash", "generation"}
                    extra = {"work_scope", "scope_decision", "allocation_transfer"} if kind == "reserved_production" else set()
                    if set(data) not in (base | extra, base | extra | {"actor_cap", "read_hold"}) or data["attempt"] in attempts or data["namespace"] not in budgets:
                        raise ValueError
                    identifier(data["attempt"]); identifier(data["generation"])
                    if data["stage"] not in STAGES or usd(data["cap"]) <= 0 or usd(data["cap"]) > usd(STAGES[data["stage"]]):
                        raise ValueError
                    if not isinstance(data["config_hash"], str) or len(data["config_hash"]) != 64:
                        raise ValueError
                    actor_cap, read_hold = data.get("actor_cap", data["cap"]), data.get("read_hold", "0")
                    if usd(actor_cap) + usd(read_hold) != usd(data["cap"]):
                        raise ValueError
                    if kind == "reserved_production":
                        budget = budgets[data["namespace"]]
                        scope = budget.get("production_scope", {})
                        transfer = usd(data["allocation_transfer"])
                        committed = sum((usd(a["cap"]) if a["charge"] is None else usd(a["charge"]) + usd(a["read_charge"] if a["read_charge"] is not None else a["read_hold"]))
                                        for a in attempts.values() if a["namespace"] == data["namespace"])
                        committed += _renewed_read_commitment(attempts, data["namespace"])
                        future = usd(budget["policy"]["future_allocation"])
                        maximum = min(PERIOD_CAP, min(usd(budget["allowance"]), PERIOD_GRANT) - PROTECTED_BUFFER) + PENNY_TOLERANCE
                        if (data["work_scope"] != "production" or data["scope_decision"] != scope.get("decision")
                                or transfer != usd(data["cap"]) or transfer > future
                                or transfer > usd(scope.get("source_allocations", {}).get(data["stage"], "0"))
                                or future + committed > maximum):
                            raise ValueError
                        budget["policy"]["future_allocation"] = str(future - transfer)
                        scope["source_allocations"][data["stage"]] = str(usd(scope["source_allocations"][data["stage"]]) - transfer)
                    attempts[data["attempt"]] = {**data, "submission": "reserved", "remote_status": None,
                                               "run_id": None, "dataset_id": None, "charge": None,
                                               "capture": None, "capture_status": "not_captured", "mapping": None,
                                               "actor_cap": actor_cap, "read_hold": read_hold,
                                               "read_charge": "0" if usd(read_hold) == 0 else None,
                                               "controls_ok": None, "review": None}
                    if kind == "reserved_production":
                        attempts[data["attempt"]]["allocation_released"] = "0"
                elif kind == "renewed_read_reserved":
                    fields = {"attempt", "read_id", "version", "namespace", "generation", "cap", "stage",
                              "scope_decision", "allocation_transfer", "period_ends_at", "funded_at", "evidence"}
                    if set(data) != fields or data["version"] != RENEWED_READ_VERSION:
                        raise ValueError
                    attempt = attempts[data["attempt"]]
                    account, period = data["namespace"].split(":")
                    identifier(account); identifier(period); identifier(data["generation"])
                    identifier(data["read_id"]); identifier(data["evidence"])
                    if (attempt["submission"] not in ("uncertain", "submitted")
                            or data["namespace"] == attempt["namespace"]
                            or account != attempt["namespace"].split(":")[0]
                            or data["stage"] != attempt["stage"]
                            or data["namespace"] not in budgets or not 0 < usd(data["cap"]) <= Decimal("0.02")
                            or timestamp(data["period_ends_at"]) <= timestamp(data["funded_at"])
                            or data["read_id"] in attempt.get("renewed_reads", {})):
                        raise ValueError
                    budget = budgets[data["namespace"]]
                    scope = budget.get("production_scope", {})
                    cap, transfer = usd(data["cap"]), usd(data["allocation_transfer"])
                    future = usd(budget["policy"]["future_allocation"])
                    committed = sum((usd(row["cap"]) if row["charge"] is None else usd(row["charge"])
                                     + usd(row["read_charge"] if row["read_charge"] is not None else row["read_hold"]))
                                    for row in attempts.values() if row["namespace"] == data["namespace"])
                    committed += _renewed_read_commitment(attempts, data["namespace"])
                    maximum = min(PERIOD_CAP, min(usd(budget["allowance"]), PERIOD_GRANT) - PROTECTED_BUFFER) + PENNY_TOLERANCE
                    if (data["scope_decision"] != scope.get("decision") or transfer != cap or cap > future
                            or cap > usd(scope.get("source_allocations", {}).get(data["stage"], "0"))
                            or future + committed > maximum):
                        raise ValueError
                    budget["policy"]["future_allocation"] = str(future - cap)
                    scope["source_allocations"][data["stage"]] = str(usd(scope["source_allocations"][data["stage"]]) - cap)
                    attempt.setdefault("renewed_reads", {})[data["read_id"]] = dict(data, charge=None,
                        allocation_released="0", operations={})
                elif kind in ("renewed_read_dispatched", "renewed_read_response", "renewed_read_cost", "renewed_read_cost_update", "renewed_read_release"):
                    attempt = attempts[data["attempt"]]
                    read = attempt["renewed_reads"][data["read_id"]]
                    if kind == "renewed_read_dispatched":
                        if set(data) != {"attempt", "read_id", "operation_id", "started_at", "max_bytes"}:
                            raise ValueError
                        identifier(data["operation_id"])
                        instant = timestamp(data["started_at"])
                        if (data["operation_id"] in read["operations"] or read["charge"] is not None
                                or instant < timestamp(read["funded_at"]) or instant >= timestamp(read["period_ends_at"])
                                or type(data["max_bytes"]) is not int or not 0 < data["max_bytes"] <= 5 * 1024 * 1024
                                or len(read["operations"]) >= 40
                                or sum(row["received_bytes"] if row["received_bytes"] is not None else row["max_bytes"]
                                       for row in read["operations"].values()) + data["max_bytes"] > 50 * 1024 * 1024):
                            raise ValueError
                        read["operations"][data["operation_id"]] = {"started_at": data["started_at"],
                            "max_bytes": data["max_bytes"], "received_bytes": None}
                    elif kind == "renewed_read_response":
                        if set(data) != {"attempt", "read_id", "operation_id", "received_bytes"}:
                            raise ValueError
                        operation = read["operations"][data["operation_id"]]
                        if (operation["received_bytes"] is not None or type(data["received_bytes"]) is not int
                                or not 0 <= data["received_bytes"] <= operation["max_bytes"]):
                            raise ValueError
                        operation["received_bytes"] = data["received_bytes"]
                    elif kind in ("renewed_read_cost", "renewed_read_cost_update"):
                        required = {"attempt", "read_id", "usd", "evidence"}
                        if kind == "renewed_read_cost_update":
                            required.add("previous_usd")
                        if set(data) != required or not read["operations"]:
                            raise ValueError
                        identifier(data["evidence"]); usd(data["usd"])
                        if kind == "renewed_read_cost" and read["charge"] is not None:
                            raise ValueError
                        if kind == "renewed_read_cost_update" and (read["charge"] is None
                                or usd(data["previous_usd"]) != usd(read["charge"]) or usd(data["usd"]) <= usd(read["charge"])):
                            raise ValueError
                        read["charge"] = data["usd"]
                    else:
                        if set(data) != {"attempt", "read_id", "usd", "previous_released", "commitment"} or read["charge"] is None:
                            raise ValueError
                        amount, old, charge = usd(data["usd"]), usd(read["allocation_released"]), usd(read["charge"])
                        if (amount <= 0 or usd(data["previous_released"]) != old or usd(data["commitment"]) != charge
                                or old + amount > max(Decimal(0), usd(read["cap"]) - charge)):
                            raise ValueError
                        budget = budgets[read["namespace"]]
                        budget["policy"]["future_allocation"] = str(usd(budget["policy"]["future_allocation"]) + amount)
                        scope = budget["production_scope"]["source_allocations"]
                        scope[read["stage"]] = str(usd(scope.get(read["stage"], "0")) + amount)
                        read["allocation_released"] = str(old + amount)
                elif kind == "production_release":
                    if set(data) != {"attempt", "usd", "previous_released", "commitment"}:
                        raise ValueError
                    attempt = attempts[data["attempt"]]
                    if attempt.get("work_scope", "trial") != "production" or attempt["charge"] is None:
                        raise ValueError
                    commitment = usd(attempt["charge"]) + usd(attempt["read_charge"] if attempt["read_charge"] is not None else attempt["read_hold"])
                    old = usd(attempt["allocation_released"])
                    amount = usd(data["usd"])
                    if (amount <= 0 or usd(data["previous_released"]) != old or usd(data["commitment"]) != commitment
                            or old + amount > max(Decimal(0), usd(attempt["cap"]) - commitment)):
                        raise ValueError
                    budget = budgets[attempt["namespace"]]
                    budget["policy"]["future_allocation"] = str(usd(budget["policy"]["future_allocation"]) + amount)
                    scope = budget["production_scope"]["source_allocations"]
                    scope[attempt["stage"]] = str(usd(scope.get(attempt["stage"], "0")) + amount)
                    attempt["allocation_released"] = str(old + amount)
                else:
                    attempt = attempts[data["attempt"]]
                    if kind == "submitting" and set(data) == {"attempt"} and attempt["submission"] == "reserved":
                        attempt["submission"] = "uncertain"
                    elif kind == "receipt" and set(data) == {"attempt", "run_id", "dataset_id"} and attempt["submission"] == "uncertain":
                        identifier(data["run_id"])
                        if data["dataset_id"] is not None:
                            identifier(data["dataset_id"])
                        attempt.update(submission="submitted", run_id=data["run_id"], dataset_id=data["dataset_id"])
                    elif kind == "terminal" and set(data) == {"attempt", "status"} and attempt["submission"] == "submitted" and attempt["remote_status"] is None and data["status"] in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                        attempt["remote_status"] = data["status"]
                    elif kind == "charge" and set(data) == {"attempt", "usd", "evidence"} and attempt["remote_status"] and attempt["charge"] is None:
                        usd(data["usd"]); identifier(data["evidence"])
                        attempt["charge"] = data["usd"]
                    elif kind == "charge_update" and set(data) == {"attempt", "previous_usd", "usd", "evidence"} and attempt["remote_status"] and attempt["charge"] is not None:
                        identifier(data["evidence"])
                        if usd(data["previous_usd"]) != usd(attempt["charge"]) or usd(data["usd"]) <= usd(attempt["charge"]):
                            raise ValueError
                        # Authenticated delayed billing, appended not overwritten.
                        # Record overruns too; reservation/quality gates stop spending.
                        attempt["charge"] = data["usd"]
                    elif kind == "read_cost" and set(data) == {"attempt", "usd", "evidence"} and attempt["remote_status"] and attempt["read_charge"] is None:
                        usd(data["usd"]); identifier(data["evidence"])
                        attempt["read_charge"] = data["usd"]
                    elif kind == "cancelled" and set(data) == {"attempt"} and attempt["submission"] == "reserved":
                        attempt.update(submission="not_submitted", charge="0", read_charge="0")
                    elif kind == "non_submission" and set(data) == {"attempt", "evidence", "read_usd"} and attempt["submission"] == "uncertain":
                        identifier(data["evidence"])
                        usd(data["read_usd"])
                        attempt.update(submission="not_submitted", charge="0", read_charge=data["read_usd"])
                    elif kind == "dataset" and set(data) == {"attempt", "dataset_id"} and attempt["run_id"] and attempt["dataset_id"] is None:
                        attempt["dataset_id"] = identifier(data["dataset_id"])
                    elif kind == "controls" and set(data) == {"attempt", "ok", "evidence"} and attempt["run_id"] and type(data["ok"]) is bool and attempt["controls_ok"] is None:
                        identifier(data["evidence"])
                        attempt["controls_ok"] = data["ok"]
                    elif kind == "controls_defect" and set(data) == {"attempt", "evidence"} and attempt["controls_ok"] is True:
                        identifier(data["evidence"])
                        attempt["controls_ok"] = False
                    elif kind == "capture" and set(data) == {"attempt", "hash"} and attempt["run_id"] and attempt["capture"] is None and isinstance(data["hash"], str) and len(data["hash"]) == 64:
                        attempt["capture"] = data["hash"]
                        attempt["capture_status"] = "captured"
                    elif kind == "no_output" and set(data) == {"attempt", "evidence"} and attempt["remote_status"] and attempt["dataset_id"] is None and attempt["capture_status"] == "not_captured":
                        identifier(data["evidence"])
                        attempt["capture_status"] = "provider_confirmed_no_output"
                    elif kind == "mapping" and set(data) == {"attempt", "status"} and attempt["capture"] and data["status"] in ("usable", "partial", "invalid"):
                        attempt["mapping"] = data["status"]
                    elif kind == "review" and set(data) == {"attempt", "verdict", "snapshot_hash", "evidence"} and attempt["capture"] == data["snapshot_hash"] and data["verdict"] in ("FunctionalPass", "QualityPass", "Partial", "Failed", "Blocked"):
                        identifier(data["evidence"])
                        if data["verdict"] in ("FunctionalPass", "QualityPass") and (attempt["remote_status"] != "SUCCEEDED" or attempt["controls_ok"] is not True or attempt["charge"] is None or attempt["mapping"] != "usable"):
                            raise ValueError
                        if data["verdict"] in ("FunctionalPass", "QualityPass") and (usd(attempt["charge"]) > usd(attempt["actor_cap"]) or
                                (attempt["read_charge"] is not None and usd(attempt["read_charge"]) > usd(attempt["read_hold"]))):
                            raise ValueError
                        if data["verdict"] == "FunctionalPass" and attempt["read_charge"] is None:
                            raise ValueError
                        if (data["verdict"] == "FunctionalPass"
                                and any(read["charge"] is None for read in attempt.get("renewed_reads", {}).values())):
                            raise ValueError
                        if (data["verdict"] in ("FunctionalPass", "QualityPass")
                                and any(read["charge"] is not None and usd(read["charge"]) > usd(read["cap"])
                                        for read in attempt.get("renewed_reads", {}).values())):
                            raise ValueError
                        if data["verdict"] == "QualityPass" and (attempt["read_charge"] is not None or usd(attempt["read_hold"]) <= 0):
                            raise ValueError
                        attempt["review"] = data["verdict"]
                    else:
                        raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError, SafetyError):
                raise SafetyError("JOURNAL_STATE_INVALID") from None
        return budgets, attempts

    def state(self):
        self._require_lock()
        from .financial_segments import enabled, read
        if enabled(self):
            return read(self, include_events=False)[1]
        return self._reduce(self.events())

    def migrate_segments(self):
        """Explicit reader-first financial rollover; retains old original bytes."""
        from .financial_segments import migrate
        return migrate(self)

    def export_bytes(self):
        """Canonical complete financial history, including migrated segments."""
        self._require_lock()
        from .financial_segments import enabled
        if enabled(self):
            return b"".join(canonical(event) + b"\n" for event in self.events())
        return self.path.read_bytes() if self.path.exists() else b""

    def append(self, event_id, kind, data):
        self._require_lock()
        from .financial_segments import enabled, append
        if enabled(self):
            return append(self, event_id, kind, data)
        identifier(event_id)
        events = self.events()
        for event in events:
            if event["event_id"] == event_id:
                if event["kind"] == kind and event["data"] == data:
                    return event
                raise SafetyError("EVENT_ID_CONFLICT")
        body = {"version": 1, "seq": len(events) + 1, "event_id": event_id, "at": utc_now(),
                "kind": kind, "data": data, "previous": events[-1]["hash"] if events else "0" * 64}
        event = {**body, "hash": digest(body)}
        self._reduce(events + [event])  # reject invalid transitions before write
        encoded = canonical(event) + b"\n"
        if sum(len(canonical(e)) + 1 for e in events) + len(encoded) > MAX_JOURNAL:
            raise SafetyError("JOURNAL_SIZE_REVIEW_REQUIRED")
        with self.path.open("ab") as handle:
            if handle.write(encoded) != len(encoded):
                raise SafetyError("JOURNAL_WRITE_FAILED")
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return event

    def setup_budget(self, namespace, allowance, production_provision="0", emergency="0"):
        # Rebinding a token/role must never replace this account-period baseline.
        result = self.append("budget-" + digest(namespace)[:20], "budget", {
            "namespace": namespace, "allowance": str(usd(allowance)),
            "production_provision": str(usd(production_provision)), "emergency": str(usd(emergency))})
        if "policy" not in self.state()[0][namespace]:
            self.apply_policy(namespace, production_provision, "initial-policy")
        return result

    def apply_policy(self, namespace, future_allocation, decision):
        """Explicit append-only policy adoption; no key, network or money reset."""
        identifier(decision)
        data = {"namespace": namespace, "version": POLICY_VERSION,
                "future_allocation": str(usd(future_allocation)), "decision": decision}
        return self.append("policy-" + digest([namespace, decision])[:24], "budget_policy", data)

    def adopt_production_scope(self, namespace, source_allocations, reason, decision):
        """Partition existing earmarked funds; no new credit or trial reset."""
        identifier(decision)
        if not isinstance(source_allocations, dict):
            raise SafetyError("PRODUCTION_ALLOCATION_INVALID")
        data = {"namespace": namespace, "version": PRODUCTION_SCOPE_VERSION,
                "source_allocations": {k: str(usd(v)) for k, v in source_allocations.items()},
                "reason": reason, "decision": decision}
        return self.append("production-scope-" + digest([namespace, decision])[:24], "production_scope", data)

    def settle_production_attempt(self, attempt_id):
        """Release only unused known commitment; unresolved reads keep a hold."""
        attempt = self.state()[1].get(identifier(attempt_id))
        if not attempt or attempt.get("work_scope", "trial") != "production" or attempt["charge"] is None:
            return None
        commitment = usd(attempt["charge"]) + usd(attempt["read_charge"] if attempt["read_charge"] is not None else attempt["read_hold"])
        old = usd(attempt["allocation_released"])
        amount = max(Decimal(0), usd(attempt["cap"]) - commitment - old)
        if amount == 0:
            return None
        return self.append("production-release-" + digest([attempt_id, str(old), str(commitment)])[:24], "production_release",
                           {"attempt": attempt_id, "usd": str(amount), "previous_released": str(old), "commitment": str(commitment)})

    def spendable(self, namespace, current_available=None):
        """Conservative headroom including holds; fresh balance is not a reset."""
        budgets, attempts = self.state()
        budget = budgets.get(namespace)
        if budget is None:
            raise SafetyError("BALANCE_NOT_CONFIRMED")
        policy = budget.get("policy")
        if not policy or policy["version"] != POLICY_VERSION:
            raise SafetyError("BUDGET_POLICY_MIGRATION_REQUIRED")
        allocation = usd(policy["future_allocation"])
        matching = [a for a in attempts.values() if a["namespace"] == namespace]
        committed = sum((usd(a["cap"]) if a["charge"] is None else usd(a["charge"]) +
                         usd(a["read_charge"] if a["read_charge"] is not None else a["read_hold"])
                         for a in matching), Decimal(0))
        committed += _renewed_read_commitment(attempts, namespace)
        # The opening balance already reflects pre-project/external spending.
        opening = min(usd(budget["allowance"]), PERIOD_GRANT)
        headroom = min(PERIOD_CAP, opening - PROTECTED_BUFFER) + PENNY_TOLERANCE - allocation - committed
        if current_available is not None:
            # Settled charges are already in a current provider balance. Hold
            # uncertain charges in full: availability is never inferred upward.
            pending = sum((usd(a["cap"]) if a["charge"] is None else
                           usd(a["read_hold"]) if a["read_charge"] is None else Decimal(0)
                           for a in matching), Decimal(0))
            pending += sum((usd(read["cap"]) for read in renewed_read_operations(attempts, namespace)
                            if read["charge"] is None), Decimal(0))
            headroom = min(headroom, min(usd(current_available), PERIOD_GRANT)
                           - PROTECTED_BUFFER + PENNY_TOLERANCE - allocation - pending)
        return headroom

    def reserve_renewed_reads(self, attempt_id, binding, *, period_ends_at, funded_at,
                             current_available, evidence, cap="0.02"):
        """One finite read envelope per old acquisition/current real period."""
        budgets, attempts = self.state()
        attempt = attempts.get(identifier(attempt_id))
        if (attempt is None or binding.role != "benchmark" or not binding.access_confirmed
                or not binding.owner_account_confirmed or binding.namespace == attempt["namespace"]
                or binding.account_key != attempt["namespace"].split(":")[0]):
            raise SafetyError("READ_FUNDING_ACCOUNT_MISMATCH")
        read_id = "funded-read-" + digest([attempt_id, binding.namespace])[:32]
        previous = attempt.get("renewed_reads", {}).get(read_id)
        if previous is not None:
            if previous["charge"] is not None:
                raise SafetyError("READ_FUNDING_ALREADY_SETTLED")
            return previous
        if timestamp(period_ends_at) <= timestamp(funded_at):
            raise SafetyError("PROVIDER_PERIOD_EXPIRED")
        budget = budgets.get(binding.namespace)
        scope = budget.get("production_scope") if budget else None
        amount = usd(cap)
        if (not scope or scope["version"] != PRODUCTION_SCOPE_VERSION or not 0 < amount <= Decimal("0.02")
                or amount > usd(scope["source_allocations"].get(attempt["stage"], "0"))
                or amount > usd(budget["policy"]["future_allocation"])
                or self.spendable(binding.namespace, current_available) < 0):
            raise SafetyError("READ_FUNDING_EXHAUSTED")
        if any(row["charge"] is not None and usd(row["charge"]) > usd(row["cap"])
               for row in renewed_read_operations(attempts, binding.namespace)):
            raise SafetyError("PROVIDER_CAP_OVERRUN_REVIEW_REQUIRED")
        self.append("reserve-" + read_id, "renewed_read_reserved", {"attempt": attempt_id, "read_id": read_id,
            "version": RENEWED_READ_VERSION, "namespace": binding.namespace, "generation": binding.generation,
            "cap": str(amount), "stage": attempt["stage"], "scope_decision": scope["decision"],
            "allocation_transfer": str(amount), "period_ends_at": period_ends_at, "funded_at": funded_at,
            "evidence": identifier(evidence)})
        return self.state()[1][attempt_id]["renewed_reads"][read_id]

    def release_renewed_read(self, attempt_id, read_id):
        read = self.state()[1][identifier(attempt_id)]["renewed_reads"][identifier(read_id)]
        if read["charge"] is None:
            return None
        commitment, old = usd(read["charge"]), usd(read["allocation_released"])
        amount = max(Decimal(0), usd(read["cap"]) - commitment - old)
        if amount <= 0:
            return None
        return self.append("release-" + digest([attempt_id, read_id, str(old), str(commitment)])[:32],
            "renewed_read_release", {"attempt": attempt_id, "read_id": read_id, "usd": str(amount),
             "previous_released": str(old), "commitment": str(commitment)})

    def check_reservation(self, request, binding, stage="meta", current_available=None, read_reserve="0", *, account_scope=False):
        budgets, attempts = self.state()
        if request.attempt_id in attempts:
            raise SafetyError("ATTEMPT_ALREADY_EXISTS")
        guarded = [a for a in attempts.values() if not account_scope or a["namespace"] == binding.namespace]
        if any(a["submission"] in ("uncertain", "submitted") and (not a["remote_status"] or a["charge"] is None) for a in guarded):
            raise SafetyError("UNRESOLVED_PROVIDER_ATTEMPT")
        if binding.role != "benchmark":
            raise SafetyError("BENCHMARK_ROLE_REQUIRED")
        budget = budgets.get(binding.namespace)
        if budget is None:
            raise SafetyError("BALANCE_NOT_CONFIRMED")
        if any(a["charge"] is not None and (usd(a["charge"]) > usd(a["actor_cap"]) or
               (a["read_charge"] is not None and usd(a["read_charge"]) > usd(a["read_hold"])) or
               usd(a["charge"]) + usd(a["read_charge"] if a["read_charge"] is not None else a["read_hold"]) > usd(a["cap"])) for a in guarded):
            raise SafetyError("PROVIDER_CAP_OVERRUN_REVIEW_REQUIRED")
        if any(read["charge"] is not None and usd(read["charge"]) > usd(read["cap"])
               for read in renewed_read_operations(attempts)
               if not account_scope or read["namespace"] == binding.namespace):
            raise SafetyError("PROVIDER_CAP_OVERRUN_REVIEW_REQUIRED")
        def committed(items):
            return sum((usd(a["cap"]) if a["charge"] is None else usd(a["charge"]) +
                        usd(a["read_charge"] if a["read_charge"] is not None else a["read_hold"]) for a in items), Decimal(0))
        # Trial/stage ceilings span this entire benchmark, not just an account
        # or billing month. A switch cannot replenish the approved trial cap.
        trials = [a for a in attempts.values() if a.get("work_scope", "trial") == "trial"]
        trial_spent = committed(trials)
        stage_spent = committed(a for a in trials if a["stage"] == stage)
        read_reserve = str(usd(read_reserve))
        cap = usd(request.reserved_usd) + usd(read_reserve)
        scope = getattr(request, "work_scope", "trial")
        transfer = cap if scope == "production" else Decimal(0)
        if scope == "production":
            policy = budget.get("production_scope")
            if not policy or policy["version"] != PRODUCTION_SCOPE_VERSION:
                raise SafetyError("PRODUCTION_SCOPE_NOT_ADOPTED")
            if cap > usd(policy["source_allocations"].get(stage, "0")) or cap > usd(budget["policy"]["future_allocation"]):
                raise SafetyError("PRODUCTION_ALLOCATION_EXHAUSTED")
        elif scope != "trial":
            raise SafetyError("INVALID_WORK_SCOPE")
        if (stage not in STAGES or cap > usd(STAGES.get(stage, "0")) or cap > self.spendable(binding.namespace) + transfer
                or (scope == "trial" and (trial_spent + cap > Decimal("0.75") or stage_spent + cap > usd(STAGES[stage])))):
            raise SafetyError("BUDGET_EXHAUSTED")
        if current_available is not None:
            if cap > self.spendable(binding.namespace, current_available) + transfer:
                raise SafetyError("CURRENT_BALANCE_INSUFFICIENT")
        data = {
            "attempt": request.attempt_id, "namespace": binding.namespace, "stage": stage,
            "cap": str(cap), "config_hash": request.config_hash, "generation": binding.generation}
        if usd(read_reserve) > 0:
            data.update(actor_cap=request.reserved_usd, read_hold=read_reserve)
        if scope == "production":
            data.update(work_scope="production", scope_decision=policy["decision"], allocation_transfer=str(cap))
        return data

    def reserve(self, request, binding, stage="meta", current_available=None, read_reserve="0", *, account_scope=False):
        data = self.check_reservation(request, binding, stage, current_available, read_reserve, account_scope=account_scope)
        return self.append("reserve-" + request.attempt_id, "reserved_production" if data.get("work_scope") == "production" else "reserved", data)

    def can_switch_key(self, *, namespace=None):
        _, attempts = self.state()
        return not any(a["submission"] in ("uncertain", "submitted") and
                       (not a["remote_status"] or a["charge"] is None or a["read_charge"] is None or a["capture_status"] == "not_captured")
                       for a in attempts.values() if namespace is None or a["namespace"] == namespace)

    def recover_receipt(self, attempt_id, run_id, dataset_id, evidence_id, binding):
        # Operator/adapter must verify the exact remote match before calling.
        # This never searches, infers a match from time alone or resubmits.
        _, attempts = self.state()
        attempt = attempts.get(identifier(attempt_id))
        if not attempt or attempt["namespace"] != binding.namespace or attempt["generation"] != binding.generation:
            raise SafetyError("RECOVERY_ACCOUNT_MISMATCH")
        identifier(evidence_id)
        folder = safe_path(self.root, identifier(attempt_id))
        path = folder / (evidence_id + ".json")
        try:
            if path.is_symlink():
                raise ValueError
            with path.open("rb") as handle:
                raw = handle.read(8193)
            if len(raw) > 8192:
                raise ValueError
            evidence = json.loads(raw)
            expected = {"evidence_type": evidence.get("evidence_type"), "attempt": attempt_id,
                        "namespace": attempt["namespace"], "generation": attempt["generation"],
                        "config_hash": attempt["config_hash"], "run_id": run_id, "dataset_id": dataset_id}
            if evidence != expected or evidence.get("evidence_type") not in ("operator_verified_unique_remote_match", "adapter_verified_unique_remote_match"):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise SafetyError("RECOVERY_EVIDENCE_REQUIRED") from None
        return self.append("receipt-" + attempt_id, "receipt", {
            "attempt": attempt_id, "run_id": run_id, "dataset_id": dataset_id})

    def summary(self):
        budgets, attempts = self.state()
        return {"schema_version": 1, "event_count": len(self.events()), "budgets": budgets, "attempts": attempts}


class ShortJournal:
    """The same ledger with short transactions for the opt-in account pool.

    No second ledger, state filter or balance reset exists. Explicit compound
    transitions use locked(); ordinary controller reads/commits acquire one
    short lock. Network code must not be called inside this wrapper's lock.
    """
    _METHODS = {"state", "events", "append", "setup_budget", "apply_policy", "adopt_production_scope",
                "settle_production_attempt", "spendable", "check_reservation", "reserve", "can_switch_key",
                "recover_receipt", "reserve_renewed_reads", "release_renewed_read", "summary"}

    def __init__(self, journal, *, account_scope=False):
        self.base = journal.base if isinstance(journal, ShortJournal) else journal
        self.account_scope = account_scope

    @property
    def root(self):
        return self.base.root

    @property
    def _locked(self):
        return self.base._locked

    @contextmanager
    def locked(self, *, timeout=0):
        with self.base.locked(timeout=timeout):
            yield self

    def __getattr__(self, name):
        if name not in self._METHODS:
            return getattr(self.base, name)
        operation = getattr(self.base, name)
        def call(*args, **kwargs):
            if self.account_scope and name in {"check_reservation", "reserve"}:
                kwargs.setdefault("account_scope", True)
            if self.base._locked:
                return operation(*args, **kwargs)
            with self.base.locked(timeout=5):
                return operation(*args, **kwargs)
        return call
