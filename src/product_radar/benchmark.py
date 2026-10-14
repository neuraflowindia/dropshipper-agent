"""Bounded controller; optional origin-bound pool, never implicit delivery."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
from pathlib import Path
import re
import time
from urllib.parse import urlencode
import uuid

from .accounts import (load_apify_token, load_binding, load_handover, save_binding,
                       load_account_registry, context_for_origin, current_read_funding_context, choose_account, freeze_account_origin)
from .http_io import HttpFailure
from .models import (PreparedMeta, SafetyError, decode_json, digest, identifier, json_safe, timestamp, usd,
                     prepared_request, request_stage)
from .providers.apify import ApifyTransport, capture_mapping, map_prepared_rows, submit_once
from .runtime_storage import runtime_path
from .storage import Journal, ShortJournal, atomic_bytes, atomic_json, load_json, safe_path


ACTOR_ID = "XtaWFhbtfxyzqrFmd"
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
TRANSITIONAL = {"READY", "RUNNING", "TIMING-OUT", "ABORTING", "MIGRATING"}
PAGE_BYTES = 5 * 1024 * 1024
TOTAL_BYTES = 50 * 1024 * 1024
BILLING_REUSE_SECONDS = 300
FINAL_CHARGE_AFTER = timedelta(hours=1)


def renewed_read_summary(attempt):
    """Add current read windows without relabelling the old period's fees."""
    rows = (attempt or {}).get("renewed_reads", {})
    if not rows:
        return {}
    return {"renewed_read_actual_usd": str(sum((usd(row["charge"] or "0") for row in rows.values()), Decimal(0))),
            "renewed_read_reserved_usd": str(sum((usd(row["cap"]) for row in rows.values() if row["charge"] is None), Decimal(0))),
            "renewed_read_accounting_complete": all(row["charge"] is not None for row in rows.values()),
            "renewed_read_ids": list(rows)}


def review_allows_next_sample(attempt):
    # Data acceptance and accounting closure are separate. Unknown reads retain
    # their full hold in check_reservation; later control defects still block.
    return (attempt["review"] in ("FunctionalPass", "QualityPass") and attempt["capture"] is not None
            and attempt["mapping"] == "usable" and attempt["remote_status"] == "SUCCEEDED"
            and attempt["controls_ok"] is True and attempt["charge"] is not None
            and usd(attempt["charge"]) <= usd(attempt["actor_cap"])
            and all(read["charge"] is None or usd(read["charge"]) <= usd(read["cap"])
                    for read in attempt.get("renewed_reads", {}).values())
            and (attempt["review"] != "FunctionalPass"
                 or all(read["charge"] is not None for read in attempt.get("renewed_reads", {}).values()))
            and ((attempt["read_charge"] is None and attempt["review"] == "QualityPass" and usd(attempt["read_hold"]) > 0)
                 or (attempt["read_charge"] is not None and usd(attempt["read_charge"]) <= usd(attempt["read_hold"]))))


def load_preparation(root, attempt_id):
    identifier(attempt_id)
    base = safe_path(runtime_path(root, "data/benchmarks"))
    paths = [p for p in base.glob("*/" + attempt_id + "/prepared.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.parent.parent.name)]
    if len(paths) != 1:
        raise SafetyError("PREPARATION_MISSING_OR_AMBIGUOUS")
    envelope = load_json(paths[0])
    try:
        prepared = prepared_request(envelope["request"])
    except (KeyError, TypeError):
        raise SafetyError("PREPARATION_INVALID") from None
    if prepared.attempt_id != attempt_id or envelope.get("config_hash") != prepared.config_hash or envelope.get("execution_mode") != "offline_preparation":
        raise SafetyError("PREPARATION_INVALID")
    return prepared


def prepare_meta(config, attempt_id):
    """The reviewed Meta request as an offline preparation envelope; no provider call."""
    required = {"schema_version", "stage", "purpose", "term", "actor", "build", "expected_build_id",
                "reserved_usd", "stage_cap_usd", "trial_cap_usd", "reviewed_at", "metadata"}
    if set(config) != required or config["schema_version"] != 1 or config["stage"] != "T1-meta" or config["purpose"] not in ("discovery", "validation") or config["stage_cap_usd"] != "0.20" or config["trial_cap_usd"] != "0.75":
        raise SafetyError("UNREVIEWED_CONFIG")
    query = {"active_status": "active", "ad_type": "all", "country": "IN", "q": config["term"],
             "search_type": "keyword_unordered", "media_type": "all"}
    request = PreparedMeta(
        attempt_id=identifier(attempt_id), actor=config["actor"], build=config["build"],
        expected_build_id=config["expected_build_id"], purpose=config["purpose"],
        input={"urls": [{"url": "https://www.facebook.com/ads/library/?" + urlencode(query)}],
               "limitPerSource": 5 if config["purpose"] == "discovery" else 10,
               "scrapeAdDetails": False, "runTag": attempt_id},
        options={"build": config["build"], "timeout": 300, "maxTotalChargeUsd": config["reserved_usd"], "restartOnError": "false"},
        reserved_usd=config["reserved_usd"], reviewed_at=config["reviewed_at"], public_metadata_hash=digest(config["metadata"]))
    return {"schema_version": 1, "execution_mode": "offline_preparation", "request": request.to_dict(),
            "config_hash": request.config_hash, "remote_attempt_created": False, "credits_used": False,
            "ready_for_live": False, "blocking_checks": ["owner_credential_confirmation", "account_identity_period_balance",
                "fresh_account_bound_schema_pricing_build_review", "runtime_charge_and_output_access_preflight"],
            "local_capture_bounds": {"page_bytes": 5242880, "pages": 10, "records": 1000},
            "bounded_meta_sample": True, "metadata": config["metadata"]}


def checked_pricing(actor, build, prepared, now=None):
    if request_stage(prepared) != "meta":
        from .research_sources import checked_source_pricing
        return checked_source_pricing(actor, build, prepared, now)
    instant = now or datetime.now(timezone.utc)
    try:
        data, definition = actor["data"], build["data"]
        if data["id"] != ACTOR_ID or data.get("isDeprecated") is True or data["actorPermissionLevel"] != "LIMITED_PERMISSIONS":
            raise ValueError
        if definition["id"] != prepared.expected_build_id or definition["buildNumber"] != prepared.build or definition["status"] != "SUCCEEDED":
            raise ValueError
        schema = definition["actorDefinition"]["input"]
        properties = schema["properties"]
        if "urls" not in schema["required"] or properties["urls"]["editor"] != "requestListSources" or properties["urls"]["type"] != "array":
            raise ValueError
        example = properties["urls"].get("prefill")
        if example is not None and (not isinstance(example, list) or not example or
                any(not isinstance(item, dict) or not isinstance(item.get("url"), str) for item in example)):
            raise ValueError
        for name, expected in (("limitPerSource", "integer"), ("scrapeAdDetails", "boolean"), ("runTag", "string")):
            if properties[name]["type"] != expected:
                raise ValueError
        active = [entry for entry in data["pricingInfos"] if timestamp(entry["startedAt"]) <= instant]
        price = max(active, key=lambda entry: timestamp(entry["startedAt"]))
        if price["pricingModel"] != "PAY_PER_EVENT" or usd(str(price.get("minimalMaxTotalChargeUsd", "0"))) > usd(prepared.reserved_usd):
            raise ValueError
        events = price["pricingPerEvent"]["actorChargeEvents"]
        expected = {"apify-default-dataset-item": "0.00075", "apify-actor-start": "0.00005"}
        if set(events) != set(expected) or any(usd(str(events[key]["eventPriceUsd"])) != usd(value) for key, value in expected.items()):
            raise ValueError
        if data.get("isPPEPlatformUsagePaidByUser") is True:
            raise ValueError
        defaults = {key: value["default"] for key, value in properties.items() if isinstance(value, dict) and "default" in value}
        return {"pricing_model": "PAY_PER_EVENT", "event_prices": expected, "input_defaults": defaults,
                "build_id": prepared.expected_build_id, "build": prepared.build}
    except (KeyError, TypeError, ValueError, SafetyError):
        raise SafetyError("PUBLIC_METADATA_CHANGED_REVIEW_REQUIRED") from None


def actor_id(prepared):
    if request_stage(prepared) == "meta":
        return ACTOR_ID
    from .research_sources import source_spec
    return source_spec(prepared.actor)[1]["actor_id"]


def run_data(payload, prepared, *, run_id=None, provider_user_id=None):
    try:
        data = payload["data"]
        identifier(data["id"])
        if run_id is not None and data["id"] != run_id:
            raise ValueError
        if data["actId"] != actor_id(prepared) or data["buildId"] != prepared.expected_build_id:
            raise ValueError
        if provider_user_id is not None and data["userId"] != provider_user_id:
            raise ValueError
        if data["status"] not in TERMINAL | TRANSITIONAL:
            raise ValueError
        stats = data.get("stats", {})
        if not isinstance(stats, dict) or any(type(stats.get(key, 0)) is not int or stats.get(key, 0) != 0 for key in ("restartCount", "resurrectCount")):
            raise ValueError
        return data
    except (KeyError, TypeError, ValueError, SafetyError):
        raise SafetyError("REMOTE_IDENTITY_OR_STATUS_INVALID") from None


def actual_charge(data):
    if data.get("status") not in TERMINAL:
        return None
    value = data.get("usageTotalUsd")
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= 10000:
            return None
        counts = data.get("chargedEventCounts", {})
        if not isinstance(counts, dict) or any(type(count) is not int or count < 0 for count in counts.values()):
            return None
        if number == 0 and any(count > 0 for count in counts.values()):
            return None
        return format(number.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING), "f")
    except (ValueError, ArithmeticError):
        return None


class MetaTrial:
    def __init__(self, root, journal, *, transport_factory=ApifyTransport, clock=time.monotonic,
                 sleep=time.sleep, now=lambda: datetime.now(timezone.utc), announce=lambda message: None,
                 account_registry=None, short_locks=None):
        self.root = Path(root)
        self.account_registry = account_registry or load_account_registry(root)
        self.pool_enabled = self.account_registry["pool_enabled"]
        self.short_locks = self.pool_enabled if short_locks is None else short_locks
        if type(self.short_locks) is not bool or self.pool_enabled and not self.short_locks:
            raise SafetyError("ACCOUNT_POOL_SHORT_LOCKS_REQUIRED")
        self.journal = ShortJournal(journal, account_scope=self.pool_enabled) if self.short_locks else journal
        self.account_paths = None
        self.credential_recovery_reference = None
        self.transport_factory, self.clock, self.sleep, self.now, self.announce = transport_factory, clock, sleep, now, announce
        self.binding = self.profile = self.transport = self.prepared = self.folder = None
        # Process-local only: a restart or explicit refresh checks the provider
        # again. Unknown bills never create a reusable observation.
        self._billing_observations = {}

    def _billing_fingerprint(self, attempt_id, attempt):
        if (self.binding is None or attempt["namespace"] != self.binding.namespace
                or attempt["generation"] != self.binding.generation
                or attempt["submission"] != "submitted" or attempt["remote_status"] not in TERMINAL
                or attempt["charge"] is None or attempt["controls_ok"] is not True
                or usd(attempt["charge"]) > usd(attempt["actor_cap"])):
            return None
        return (attempt_id, attempt["namespace"], attempt["generation"], attempt["run_id"],
                attempt["remote_status"], attempt["charge"], attempt["controls_ok"], attempt["config_hash"])

    def _recent_billing(self, attempt_id, attempt):
        saved = self._billing_observations.get(attempt_id)
        fingerprint = self._billing_fingerprint(attempt_id, attempt)
        return bool(saved and fingerprint is not None and saved[0] == fingerprint
                    and 0 <= self.clock() - saved[1] < BILLING_REUSE_SECONDS)

    def _remember_billing(self, data):
        attempt_id = self.prepared.attempt_id
        self._billing_observations.pop(attempt_id, None)
        if actual_charge(data) is None:
            return
        fingerprint = self._billing_fingerprint(attempt_id, self.journal.state()[1][attempt_id])
        if fingerprint is not None:
            self._billing_observations[attempt_id] = (fingerprint, self.clock())

    def context(self, attempt_id, *, launch=False):
        self.folder = self.binding = self.profile = self.transport = None
        self.credential_recovery_reference = None
        self.prepared = load_preparation(self.root, attempt_id)
        self.account_paths = {"binding": self.root / "config/local/apify_binding.json",
                              "profile": self.root / "config/local/apify_profile.json",
                              "handover": self.root / "config/local/apify_handover.json",
                              "credential": self.root / "config/local/providers.env"}
        account_context = None
        if self.pool_enabled:
            origin_path = safe_path(self.root, "config", "local", "apify_origins", attempt_id) / "origin.json"
            with Journal(origin_path.parent).locked():
                origin = load_json(origin_path, 8192) if origin_path.exists() else None
                if origin:
                    if origin.get("attempt_id") != attempt_id or origin.get("config_hash") != self.prepared.config_hash:
                        raise SafetyError("ACQUISITION_ACCOUNT_ORIGIN_CHANGED")
                    account_context = context_for_origin(self.root, self.account_registry, origin,
                        launch=launch, now=self.now())
                    attempt = self.journal.state()[1].get(attempt_id)
                    if attempt and any(attempt[field] != origin[field] for field in ("namespace", "generation", "config_hash")):
                        raise SafetyError("RECOVERY_ACCOUNT_MISMATCH")
                else:
                    with self.journal.locked(timeout=5):
                        account_context = choose_account(self.root, self.journal, self.account_registry, self.prepared, now=self.now())
                    freeze_account_origin(self.root, self.prepared, account_context)
            self.account_paths = account_context["paths"]
            self.credential_recovery_reference = account_context.get("credential_recovery_reference")
        binding_path = self.account_paths["binding"]
        if not binding_path.exists():
            raise SafetyError("ACCOUNT_HANDOVER_REQUIRED")
        self.binding = account_context["binding"] if account_context else load_binding(binding_path)
        self.profile = account_context["profile"] if account_context else load_json(self.account_paths["profile"], 8192)
        if (not isinstance(self.profile, dict) or type(self.profile.get("schema_version")) is not int or self.profile.get("schema_version") != 1
                or self.profile.get("namespace") != self.binding.namespace or self.profile.get("generation") != self.binding.generation or self.binding.role != "benchmark"):
            raise SafetyError("ACCOUNT_PROFILE_MISMATCH")
        if launch and timestamp(self.profile["period_ends_at"]) <= self.now():
            raise SafetyError("PROVIDER_PERIOD_EXPIRED")
        token = account_context["token"] if account_context else load_apify_token(self.account_paths["credential"])
        # Safe reads refresh access without rebinding a key or resetting money.
        fresh = replace(self.binding, checked_at=self.now().isoformat())
        fresh.validate_key(token, "benchmark", self.now())
        self.folder = safe_path(self.journal.root, attempt_id)
        self.folder.mkdir(parents=True, exist_ok=True)
        saved = self.folder / "prepared.json"
        if saved.exists() and load_json(saved) != self.prepared.to_dict():
            raise SafetyError("ATTEMPT_ARTIFACT_CONFLICT")
        if not saved.exists():
            atomic_json(saved, self.prepared.to_dict())
        self.transport = self.transport_factory(token)
        self.transport.clock = self.clock
        self.transport.response_sink, self.transport.activity_sink = self.save_response, self.before_request
        self.transport.deadline = self.clock() + 120
        return token

    def before_request(self, spec, purpose):
        path = self.folder / "http_activity.json"
        activity = load_json(path, 262144) if path.exists() else []
        if len(activity) >= 256:
            raise SafetyError("HTTP_CALL_LIMIT_REVIEW_REQUIRED")
        is_storage = spec["path"].startswith(("/v2/datasets/", "/v2/key-value-stores/"))
        read_funding = None
        if is_storage:
            attempt = self.journal.state()[1].get(self.prepared.attempt_id)
            expired = timestamp(self.profile["period_ends_at"]) <= self.now()
            if expired and not self.pool_enabled:
                raise SafetyError("STORAGE_PERIOD_FUNDING_REVIEW_REQUIRED")
            if not attempt or not expired and (usd(attempt["read_hold"]) == 0 or attempt["read_charge"] is not None):
                raise SafetyError("READ_PROVISION_REQUIRED")
            previous = [a for a in activity if a["storage"]]
            reserved_bytes = sum(a["received_bytes"] if a["received_bytes"] is not None else a["max_bytes"] for a in previous)
            if len(previous) >= 40 or reserved_bytes + spec["byte_limit"] > TOTAL_BYTES:
                raise SafetyError("READ_ACTIVITY_LIMIT_REVIEW_REQUIRED")
            if expired:
                funding = current_read_funding_context(self.root, self.account_registry,
                    {"binding": self.binding, "profile": self.profile}, now=self.now())
                evidence_id = "read-funding-" + digest([self.prepared.attempt_id, funding["binding"].namespace])[:32]
                read_funding = self.journal.reserve_renewed_reads(self.prepared.attempt_id, funding["binding"],
                    period_ends_at=funding["profile"]["period_ends_at"], funded_at=self.now().isoformat(),
                    current_available=funding["funding_facts"]["available_usd"], evidence=evidence_id)
                funding_path = safe_path(self.folder, "funded_reads", read_funding["read_id"]) / "funding.json"
                proof = {"schema_version": 1, "attempt": self.prepared.attempt_id,
                         "origin_namespace": attempt["namespace"], "origin_generation": attempt["generation"],
                         "read_id": read_funding["read_id"], "funding_namespace": read_funding["namespace"],
                         "funding_generation": read_funding["generation"], "evidence_id": evidence_id,
                         "same_real_account": True, "read_cap_usd": read_funding["cap"],
                         "period_ends_at": read_funding["period_ends_at"]}
                if funding_path.exists() and load_json(funding_path) != proof:
                    raise SafetyError("READ_FUNDING_RECEIPT_CONFLICT")
                if not funding_path.exists():
                    atomic_json(funding_path, proof)
        reference = "http-" + uuid.uuid4().hex
        entry = {"id": reference, "method": spec["method"], "path": spec["path"], "purpose": purpose,
                         "started_at": self.now().isoformat(), "storage": is_storage,
                         "max_bytes": spec["byte_limit"], "received_bytes": None}
        if read_funding is not None:
            entry.update(funding_id=read_funding["read_id"], funding_namespace=read_funding["namespace"],
                         funding_generation=read_funding["generation"])
            if self.credential_recovery_reference:
                entry["credential_recovery_reference"] = self.credential_recovery_reference
        activity.append(entry)
        atomic_json(path, activity)
        if read_funding is not None:
            # Durable funding and bounded operation intent precede a GET.
            # A missing reply retains the allowance and conservative byte cap.
            self.journal.append("dispatch-" + reference, "renewed_read_dispatched", {
                "attempt": self.prepared.attempt_id, "read_id": read_funding["read_id"], "operation_id": reference,
                "started_at": entry["started_at"], "max_bytes": spec["byte_limit"]})
        return reference

    def save_response(self, spec, wire, purpose, reference):
        identifier(reference)
        folder = safe_path(self.folder, "http")
        atomic_bytes(folder / (reference + ".body.json"), wire.body)
        metadata = {"id": reference, "method": spec["method"], "path": spec["path"], "query": spec["query"],
                    "purpose": purpose, "status": wire.status, "headers": wire.headers,
                    "received_at": self.now().isoformat(), "elapsed": wire.elapsed,
                    "bytes": len(wire.body), "hash": hashlib.sha256(wire.body).hexdigest()}
        atomic_json(folder / (reference + ".json"), metadata)
        activity = load_json(self.folder / "http_activity.json", 262144)
        for entry in activity:
            if entry["id"] == reference:
                entry["received_bytes"] = len(wire.body)
                if entry.get("funding_id"):
                    metadata.update(funding_id=entry["funding_id"], funding_namespace=entry["funding_namespace"],
                                    funding_generation=entry["funding_generation"])
                    atomic_json(folder / (reference + ".json"), metadata)
                    self.journal.append("response-" + reference, "renewed_read_response", {
                        "attempt": self.prepared.attempt_id, "read_id": entry["funding_id"],
                        "operation_id": reference, "received_bytes": len(wire.body)})
        atomic_json(self.folder / "http_activity.json", activity)
        return reference

    def read(self, operation, purpose):
        self.transport.purpose = purpose
        for retry in range(3):
            try:
                return operation()
            except HttpFailure as error:
                if not error.retryable or retry == 2:
                    raise
                delay = error.retry_after if error.retry_after is not None else retry + 1
                if self.clock() + delay >= self.transport.deadline:
                    raise SafetyError("READ_DEADLINE_EXCEEDED") from None
                self.sleep(delay)

    def preflight(self, attempt_id):
        if self.short_locks:
            if self.journal._locked:
                raise SafetyError("NETWORK_FINANCIAL_LOCK_REFUSED")
            with Journal(safe_path(self.journal.root, "attempt_guards", attempt_id)).locked():
                return self._preflight(attempt_id)
        return self._preflight(attempt_id)

    def _preflight(self, attempt_id):
        token = self.context(attempt_id, launch=True)
        facts = load_handover(self.account_paths["handover"], self.now())
        if (facts["period"] != self.binding.period or facts["period_ends_at"] != self.profile["period_ends_at"]
                or facts["account_key"] != self.profile["owner_account_key"]
                or facts["provider_user_id"] not in (None, self.profile.get("provider_user_id"))):
            raise SafetyError("HANDOVER_ACCOUNT_MISMATCH")
        # Terminal usage can be posted late. Refresh existing terminal runs
        # before using their apparent low initial costs to fund another job.
        self.refresh_costs(reuse_recent=True, namespace=self.binding.namespace if self.pool_enabled else None)
        self.transport.deadline = self.clock() + 120
        attempts = self.journal.state()[1]
        self.check_previous_reviews(attempts)
        self.journal.check_reservation(self.prepared, self.binding, stage=request_stage(self.prepared), current_available=facts["available_usd"], read_reserve="0.02")
        access = self.read(lambda: self.transport.run_list(), "account_access")
        if not isinstance(access.get("data", {}).get("items"), list):
            raise SafetyError("ACCOUNT_ACCESS_RESPONSE_INVALID")
        self.binding = replace(self.binding, checked_at=self.now().isoformat())
        save_binding(self.account_paths["binding"], self.binding, self.journal)
        actor = self.read(lambda: self.transport.actor_metadata(self.prepared.actor), "actor_metadata")
        build = self.read(lambda: self.transport.build_metadata(self.prepared.expected_build_id), "build_metadata")
        policy = checked_pricing(actor, build, self.prepared, self.now())
        preflight = {"config_hash": self.prepared.config_hash, "generation": self.binding.generation,
                     "namespace": self.binding.namespace, "checked_at": self.now().isoformat(),
                     "available_usd": facts["available_usd"], "balance_confirmed": True,
                     "balance_source": "owner_console_dated", "balance_checked_at": facts["balance_checked_at"],
                     "build_schema_pricing_confirmed": True, "output_route_reviewed": True,
                     "runtime_charge_route_reviewed": True, "read_reserve_usd": "0.02"}
        atomic_json(self.folder / "policy.json", policy)
        atomic_json(self.root / "config/local" / ("apify_preflight-" + attempt_id + ".json"), preflight)
        return {"ready_for_live": True, "actor_cap_usd": self.prepared.reserved_usd, "read_reserved_usd": "0.02"}

    def refresh_costs(self, *, reuse_recent=False, namespace=None):
        count = reused = 0
        failures = []
        deadline = self.clock() + 120
        # A bill confirmed final by a later re-read is never read again, so the
        # refresh does not grow with every finished run.
        terminal = [(aid, a) for aid, a in self.journal.state()[1].items()
                    if a["submission"] == "submitted" and a["remote_status"] and not a.get("charge_final")
                    and (namespace is None or a["namespace"] == namespace)]
        for attempt_id, attempt in terminal:
            if reuse_recent and self._recent_billing(attempt_id, attempt):
                reused += 1
                continue
            if self.clock() >= deadline:
                raise SafetyError("BILLING_REFRESH_DEADLINE_EXCEEDED")
            self._billing_observations.pop(attempt_id, None)
            self.announce("Checking saved job billing " + str(count + 1) + "/" + str(len(terminal)) + "; no new research.")
            previous = MetaTrial(self.root, self.journal, transport_factory=self.transport_factory,
                                 clock=self.clock, sleep=self.sleep, now=self.now,
                                 account_registry=self.account_registry, short_locks=self.short_locks)
            previous._billing_observations = self._billing_observations
            try:
                previous.context(attempt_id)
                previous.transport.deadline = deadline
                if attempt["namespace"] != previous.binding.namespace or attempt["generation"] != previous.binding.generation:
                    raise SafetyError("RECOVERY_ACCOUNT_MISMATCH")
                if self.short_locks:
                    with Journal(safe_path(self.journal.root, "attempt_guards", attempt_id)).locked():
                        previous.wait_terminal(attempt["run_id"])
                else:
                    previous.wait_terminal(attempt["run_id"])
                count += 1
            except SafetyError as error:
                if not self.pool_enabled or namespace is not None:
                    raise
                failures.append({"attempt_id": attempt_id, "reason": str(error)})
        if reused:
            self.announce("Reused recent billing observations for " + str(reused) + "; unknown charges were not reused.")
        return {"verdict": "Billing observations refreshed", "checked_attempts": count,
                "reused_attempts": reused,
                "account_failures": failures,
                "code": "TERMINAL_COST_OBSERVATIONS_NOT_READ_SETTLEMENT"}

    def check_previous_reviews(self, attempts):
        for attempt_id, attempt in attempts.items():
            if self.pool_enabled and self.binding is not None and attempt["namespace"] != self.binding.namespace:
                continue
            if attempt["submission"] == "not_submitted":
                continue
            production = attempt.get("work_scope", "trial") == "production"
            if production and attempt["submission"] == "reserved":
                continue
            if production:
                if (attempt["remote_status"] not in TERMINAL or attempt["controls_ok"] is not True
                        or attempt["charge"] is None or usd(attempt["charge"]) > usd(attempt["actor_cap"])
                        or (attempt["read_charge"] is not None and usd(attempt["read_charge"]) > usd(attempt["read_hold"]))):
                    raise SafetyError("PRIOR_PRODUCTION_CONTROLS_UNRESOLVED")
                if attempt["capture_status"] == "provider_confirmed_no_output":
                    continue
                if attempt["capture_status"] == "not_captured" and attempt["capture"] is None:
                    # A reconciled production run can fail to yield usable
                    # source bytes. Retain its failure and unknown read-cost
                    # hold, but do not make a missing content file a global
                    # stop for unrelated research. Committed captures below
                    # still require their full original byte verification.
                    if (attempt["submission"] != "submitted"
                            or (attempt["read_charge"] is None and usd(attempt["read_hold"]) <= 0)):
                        raise SafetyError("PRIOR_PRODUCTION_CONTROLS_UNRESOLVED")
                    prepared = load_preparation(self.root, attempt_id)
                    if prepared.work_scope != "production" or prepared.config_hash != attempt["config_hash"]:
                        raise SafetyError("PRIOR_PRODUCTION_CAPTURE_INVALID")
                    continue
            accepted = review_allows_next_sample(attempt)
            isolated = (attempt["review"] in ("Partial", "Failed") and attempt["capture"] is not None
                        and attempt["remote_status"] in TERMINAL and attempt["controls_ok"] is True
                        and attempt["charge"] is not None and usd(attempt["charge"]) <= usd(attempt["actor_cap"])
                        and ((attempt["read_charge"] is None and usd(attempt["read_hold"]) > 0)
                             or (attempt["read_charge"] is not None and usd(attempt["read_charge"]) <= usd(attempt["read_hold"]))))
            if not production and not accepted and not isolated:
                raise SafetyError("PRIOR_SAMPLE_REVIEW_REQUIRED")
            # A ledger verdict must still correspond to intact reviewed bytes.
            previous = MetaTrial(self.root, self.journal, now=self.now,
                                 account_registry=self.account_registry, short_locks=self.short_locks)
            previous.prepared = load_preparation(self.root, attempt_id)
            previous.folder = safe_path(self.journal.root, attempt_id)
            manifest = load_json(previous.folder / "capture_manifest.json")
            rows = load_json(previous.folder / "captured_rows.json", TOTAL_BYTES)
            previous.verify_saved_capture(manifest, rows)
            if load_json(previous.folder / "mapped_rows.json", TOTAL_BYTES) != map_prepared_rows(rows, previous.prepared):
                raise SafetyError("REVIEW_MAPPING_MISMATCH")
            if production:
                if previous.prepared.work_scope != "production" or not manifest["bounded_capture_complete"] or manifest["truncated"]:
                    raise SafetyError("PRIOR_PRODUCTION_CAPTURE_INVALID")
                continue
            review = load_json(previous.folder / "operator_review.json")
            events = [e for e in self.journal.events() if e["kind"] == "review" and e["data"]["attempt"] == attempt_id]
            if (not events or events[-1]["event_id"] != "review-" + digest(review)[:20]
                    or (isolated and (not review.get("defects") or review.get("controls_checked") is not True
                                    or review.get("schema_checked") is not True))
                    or not manifest["bounded_capture_complete"] or manifest["truncated"]):
                raise SafetyError("PRIOR_SAMPLE_REVIEW_REQUIRED")

    def issue(self, code, phase, *, http_status=None):
        attempt = self.journal.state()[1].get(self.prepared.attempt_id)
        result = {"schema_version": 1, "attempt": self.prepared.attempt_id, "verdict": "Blocked",
                  "code": code, "phase": phase, "at": self.now().isoformat(),
                  "remote_may_run": bool(attempt and attempt["submission"] in ("uncertain", "submitted") and not attempt["remote_status"]),
                  "http_status": http_status,
                  "next_action": "resume_same_attempt_or_resolve_recorded_issue_no_new_launch",
                  **renewed_read_summary(attempt)}
        atomic_json(self.folder / "result.json", result)
        return result

    def run(self, attempt_id, *, resume_only=False):
        if self.short_locks:
            if self.journal._locked:
                raise SafetyError("NETWORK_FINANCIAL_LOCK_REFUSED")
            with Journal(safe_path(self.journal.root, "attempt_guards", identifier(attempt_id))).locked():
                return self._run(attempt_id, resume_only=resume_only)
        return self._run(attempt_id, resume_only=resume_only)

    def _run(self, attempt_id, *, resume_only=False):
        phase = "account"
        try:
            attempts = self.journal.state()[1]
            attempt = attempts.get(attempt_id)
            # A repeated run command is recovery, not a new launch. Expired
            # launch preflight/period does not hide an existing remote receipt.
            token = self.context(attempt_id, launch=attempt is None and not resume_only)
            whole_deadline = self.clock() + 480
            status_deadline = self.clock() + 360
            if attempt:
                if attempt["namespace"] != self.binding.namespace or attempt["generation"] != self.binding.generation or attempt["config_hash"] != self.prepared.config_hash:
                    raise SafetyError("RECOVERY_ACCOUNT_MISMATCH")
                if attempt["submission"] in ("reserved", "not_submitted"):
                    raise SafetyError("UNSUBMITTED_ATTEMPT_REVIEW_REQUIRED")
                if attempt["read_charge"] is not None and (self.folder / "result.json").exists() and attempt["capture"]:
                    manifest = load_json(self.folder / "capture_manifest.json")
                    rows = load_json(self.folder / "captured_rows.json", TOTAL_BYTES)
                    self.verify_saved_capture(manifest, rows)
                    result = load_json(self.folder / "result.json")
                    result.update(actor_actual_usd=attempt["charge"], read_actual_usd=attempt["read_charge"], read_reserved_usd="0")
                    result.update(renewed_read_summary(attempt))
                    return result
            elif resume_only:
                raise SafetyError("NO_SUBMITTED_ATTEMPT_TO_RESUME")
            else:
                self.check_previous_reviews(attempts)
                proof = load_json(self.root / "config/local" / ("apify_preflight-" + attempt_id + ".json"))
                if proof.get("read_reserve_usd") != "0.02":
                    raise SafetyError("PREFLIGHT_INCOMPLETE")
                phase = "submission"
                self.transport.deadline = self.clock() + 30
                self.transport.purpose = "launch"
                self.announce("Starting one " + request_stage(self.prepared) + " request. Collection limit US$" + self.prepared.reserved_usd + "; US$0.02 reserved for bounded reads.")
                try:
                    submit_once(self.prepared, self.binding, token, self.journal, self.transport,
                                explicit_live=True, preflight=proof, now=self.now(), short_locks=self.short_locks)
                except SafetyError as error:
                    # Receipt was persisted first. Inspect the same job and
                    # its bill/output even when echoed controls are wrong.
                    if str(error) != "RETURNED_CONTROLS_REQUIRE_REVIEW":
                        raise
                    self.journal.append("controls-" + attempt_id, "controls", {"attempt": attempt_id, "ok": False,
                                        "evidence": self.transport.last_reference})
            phase = "recovery"
            self.transport.deadline = status_deadline
            attempt = self.journal.state()[1][attempt_id]
            if attempt["submission"] == "uncertain":
                self.recover()
                attempt = self.journal.state()[1][attempt_id]
            phase = "status"
            data = self.wait_terminal(attempt["run_id"])
            phase = "capture"
            self.transport.deadline = min(self.clock() + 120, whole_deadline)
            result = self.capture(data)
            atomic_json(self.folder / "result.json", result)
            return result
        except SafetyError as error:
            if self.folder is not None:
                return self.issue(str(error), phase, http_status=getattr(error, "status", None))
            raise
        except Exception:
            if self.folder is not None:
                return self.issue("LOCAL_OPERATION_FAILED", phase)
            raise SafetyError("LOCAL_OPERATION_FAILED") from None

    def check_run(self, payload, run_id=None):
        return run_data(payload, self.prepared, run_id=run_id, provider_user_id=self.profile.get("provider_user_id"))

    def recover(self):
        attempt = self.journal.state()[1][self.prepared.attempt_id]
        local = []
        for metadata_path in (self.folder / "http").glob("*.json"):
            if metadata_path.name.endswith(".body.json"):
                continue
            metadata = load_json(metadata_path)
            if metadata.get("method") == "POST" and metadata.get("purpose") == "launch":
                try:
                    data = self.check_run(self.saved_response(metadata))
                    local.append(data)
                except SafetyError:
                    continue
        matches = {data["id"]: data for data in local}
        if not matches:
            intent = [event for event in self.journal.events() if event["kind"] == "submitting" and event["data"]["attempt"] == self.prepared.attempt_id][-1]
            after = (timestamp(intent["at"]) - timedelta(seconds=60)).isoformat()
            candidates, complete = [], False
            for offset in (0, 50):
                payload = self.read(lambda: self.transport.run_list(offset=offset, started_after=after), "recovery_run_list")
                data = payload.get("data", {})
                items = data.get("items")
                if not isinstance(items, list) or data.get("offset") != offset or type(data.get("total")) is not int or len(items) > 50:
                    raise SafetyError("RECOVERY_RUN_LIST_INVALID")
                candidates.extend(item for item in items if isinstance(item, dict) and item.get("actId") == actor_id(self.prepared))
                if offset + len(items) >= data["total"]:
                    complete = True
                    break
            if not complete or len(candidates) > 10:
                raise SafetyError("RECOVERY_SEARCH_INCOMPLETE")
            policy = load_json(self.folder / "policy.json")
            for candidate in candidates:
                identifier(candidate.get("id"))
                payload = self.read(lambda: self.transport.status(candidate["id"]), "recovery_run_detail")
                try:
                    detail = self.check_run(payload, candidate["id"])
                except SafetyError:
                    continue
                if request_stage(self.prepared) != "meta":
                    # No actor runTag exists. A unique identical input alone
                    # must not adopt an unrelated much-later manual launch.
                    try:
                        delta = (timestamp(detail.get("startedAt")) - timestamp(intent["at"])).total_seconds()
                    except SafetyError:
                        continue
                    if not -60 <= delta <= 120:
                        continue
                store = identifier(detail.get("defaultKeyValueStoreId"))
                supplied = self.read(lambda: self.transport.run_input(store), "recovery_input")
                if not isinstance(supplied, dict) or any(supplied.get(key) != value for key, value in self.prepared.input.items()):
                    continue
                extras = {key: value for key, value in supplied.items() if key not in self.prepared.input}
                if any(key not in policy["input_defaults"] or value != policy["input_defaults"][key] for key, value in extras.items()):
                    continue
                matches[detail["id"]] = detail
        if len(matches) != 1:
            raise SafetyError("RECOVERY_UNIQUE_MATCH_NOT_ESTABLISHED")
        data = next(iter(matches.values()))
        evidence = {"evidence_type": "adapter_verified_unique_remote_match", "attempt": self.prepared.attempt_id,
                    "namespace": attempt["namespace"], "generation": attempt["generation"],
                    "config_hash": attempt["config_hash"], "run_id": data["id"], "dataset_id": data.get("defaultDatasetId")}
        atomic_json(self.folder / "recovery.json", evidence)
        self.journal.recover_receipt(self.prepared.attempt_id, data["id"], data.get("defaultDatasetId"), "recovery", self.binding)

    def wait_terminal(self, run_id):
        # A failed or incomplete new observation must not leave an older cache
        # entry looking reusable after this attempted check.
        self._billing_observations.pop(self.prepared.attempt_id, None)
        self.announce("Remote receipt saved. Checking the same job; no new launch.")
        previous, last_notice = None, self.clock()
        while self.clock() < self.transport.deadline:
            payload = self.read(lambda: self.transport.status(run_id), "run_status")
            data = self.check_run(payload, run_id)
            options = data.get("options", {})
            try:
                good = (isinstance(options, dict) and type(options.get("timeoutSecs")) is int and options.get("timeoutSecs") == 300 and
                        usd(str(options.get("maxTotalChargeUsd"))) == usd(self.prepared.reserved_usd) and options.get("restartOnError", False) is False)
            except SafetyError:
                good = False
            state = self.journal.state()[1][self.prepared.attempt_id]
            if state["controls_ok"] is None:
                self.journal.append("controls-" + self.prepared.attempt_id, "controls", {"attempt": self.prepared.attempt_id,
                                    "ok": good, "evidence": self.transport.last_reference})
            elif state["controls_ok"] is True and not good:
                self.journal.append("controls-defect-" + self.prepared.attempt_id, "controls_defect", {
                    "attempt": self.prepared.attempt_id, "evidence": self.transport.last_reference})
            if data["status"] != previous or self.clock() - last_notice >= 30:
                self.announce("Remote status: " + data["status"])
                previous = data["status"]
                last_notice = self.clock()
            dataset = data.get("defaultDatasetId")
            if state["dataset_id"] is None and dataset is not None:
                self.journal.append("dataset-" + self.prepared.attempt_id, "dataset", {"attempt": self.prepared.attempt_id, "dataset_id": dataset})
            elif dataset is not None and state["dataset_id"] != dataset:
                raise SafetyError("REMOTE_DATASET_CHANGED")
            if data["status"] in TERMINAL:
                self.journal.append("terminal-" + self.prepared.attempt_id, "terminal", {"attempt": self.prepared.attempt_id, "status": data["status"]})
                charge = actual_charge(data)
                if charge is not None and state["charge"] is None:
                    self.journal.append("charge-" + self.prepared.attempt_id, "charge", {"attempt": self.prepared.attempt_id,
                                        "usd": charge, "evidence": self.transport.last_reference})
                elif charge is not None and usd(state["charge"] or "0") != usd(charge):
                    if usd(charge) < usd(state["charge"]):
                        raise SafetyError("TERMINAL_COST_DECREASE_REVIEW_REQUIRED")
                    self.journal.append("charge-update-" + digest([self.prepared.attempt_id, charge])[:24], "charge_update", {
                        "attempt": self.prepared.attempt_id, "previous_usd": state["charge"], "usd": charge,
                        "evidence": self.transport.last_reference})
                self.journal.settle_production_attempt(self.prepared.attempt_id)
                self._confirm_final_charge(state, data, charge)
                atomic_json(self.folder / "terminal.json", json_safe(data))
                result_path = self.folder / "result.json"
                if result_path.exists() and charge is not None:
                    result = load_json(result_path)
                    result["actor_actual_usd"] = charge
                    if usd(charge) > usd(self.prepared.reserved_usd):
                        result.update(verdict="Blocked", code="PROVIDER_CAP_OVERRUN_REVIEW_REQUIRED", phase="accounting")
                    atomic_json(result_path, result)
                self._remember_billing(data)
                return data
            remaining = self.transport.deadline - self.clock()
            if remaining < 5:
                break
            self.sleep(5)
        raise SafetyError("REMOTE_WAIT_LIMIT_REACHED")

    def _confirm_final_charge(self, before, data, charge):
        """Mark a bill final when this re-read, an hour or more after the run
        ended, shows the same amount an earlier read recorded."""
        if charge is None or before["charge"] is None or usd(before["charge"]) != usd(charge) or before.get("charge_final"):
            return
        try:
            ended = timestamp(data.get("finishedAt"))
        except SafetyError:
            return
        if self.now() - ended >= FINAL_CHARGE_AFTER:
            self.journal.append("charge-final-" + self.prepared.attempt_id, "charge_final", {
                "attempt": self.prepared.attempt_id, "evidence": self.transport.last_reference})

    def saved_response(self, metadata):
        identifier(metadata["id"])
        path = self.folder / "http" / (metadata["id"] + ".body.json")
        safe_path(path.parent)
        if path.is_symlink():
            raise SafetyError("SYMLINK_PATH_REFUSED")
        with path.open("rb") as handle:
            raw = handle.read(PAGE_BYTES + 1)
        if len(raw) > PAGE_BYTES or metadata.get("bytes") != len(raw) or metadata.get("hash") != hashlib.sha256(raw).hexdigest():
            raise SafetyError("SAVED_RESPONSE_HASH_MISMATCH")
        return decode_json(raw)

    def capture(self, data):
        dataset = self.journal.state()[1][self.prepared.attempt_id]["dataset_id"]
        if dataset is None:
            if self.journal.state()[1][self.prepared.attempt_id]["capture_status"] == "not_captured":
                self.journal.append("no-output-" + self.prepared.attempt_id, "no_output", {
                    "attempt": self.prepared.attempt_id, "evidence": self.transport.last_reference})
            raise SafetyError("TERMINAL_DATASET_ABSENT")
        progress_path = self.folder / "capture_progress.json"
        progress = load_json(progress_path) if progress_path.exists() else {"dataset": dataset, "pages": [], "complete": False, "truncated": False}
        if not isinstance(progress, dict) or set(progress) != {"dataset", "pages", "complete", "truncated"} or progress["dataset"] != dataset or not isinstance(progress["pages"], list) or len(progress["pages"]) > 10:
            raise SafetyError("CAPTURE_DATASET_MISMATCH")
        if type(progress["complete"]) is not bool or type(progress["truncated"]) is not bool or (not progress["pages"] and (progress["complete"] or progress["truncated"])):
            raise SafetyError("SAVED_PAGE_CONFLICT")
        rows, offset, total = [], 0, None
        for page in progress["pages"]:
            metadata = load_json(self.folder / "http" / (page["response"] + ".json"))
            loaded, received = self.checked_page(metadata, dataset, offset, min(100, 1000 - offset), total)
            if page != {"offset": offset, "count": len(loaded), "total": received["total"], "response": metadata["id"]}:
                raise SafetyError("SAVED_PAGE_CONFLICT")
            rows.extend(loaded)
            offset += len(loaded)
            total = received["total"]
        if progress["pages"]:
            complete = offset >= total or offset >= 1000 or len(progress["pages"]) >= 10
            if progress["complete"] is not complete or progress["truncated"] is not (complete and offset < total):
                raise SafetyError("SAVED_PAGE_CONFLICT")
        while not progress["complete"] and len(progress["pages"]) < 10 and offset < 1000:
            if self.clock() >= self.transport.deadline:
                raise SafetyError("CAPTURE_DEADLINE_EXCEEDED")
            limit = min(100, 1000 - offset)
            # A crash after raw+metadata save but before progress must not
            # trigger another billed dataset read. Reuse a unique saved page.
            candidates = []
            for path in (self.folder / "http").glob("*.json"):
                if path.name.endswith(".body.json"):
                    continue
                meta = load_json(path)
                if (meta.get("method") == "GET" and meta.get("path") == "/v2/datasets/" + dataset + "/items"
                        and meta.get("query") == {"format": "json", "offset": offset, "limit": limit}):
                    candidates.append(meta)
            if len({meta["hash"] for meta in candidates}) > 1:
                raise SafetyError("SAVED_PAGE_CONFLICT")
            if candidates:
                valid = []
                for candidate in candidates:
                    try:
                        self.checked_page(candidate, dataset, offset, limit, total)
                        valid.append(candidate)
                    except SafetyError as error:
                        if str(error) != "DATASET_PAGINATION_INCONSISTENT":
                            raise
                if valid:
                    metadata = valid[0]
                else:
                    first = candidates[0]
                    initial = self.saved_response(first)
                    # Observed eventual-count lag: terminal datasets can have
                    # product bytes before pagination counters finish updating.
                    # One corroborating read is allowed, not another collection.
                    # Exact original body hash must agree; all failures remain.
                    refresh = self.folder / "pagination_refresh.json"
                    headers = first["headers"]
                    old_count = headers.get("x-apify-pagination-count", "")
                    old_total = headers.get("x-apify-pagination-total", "")
                    zero_amazon_lag = (request_stage(self.prepared) == "amazon" and old_count == old_total == "0")
                    bounded_meta_lag = (request_stage(self.prepared) == "meta" and old_count == old_total
                                        and re.fullmatch(r"[0-9]{1,10}", old_count)
                                        and isinstance(initial, list) and 0 <= int(old_count) < len(initial) <= limit)
                    if (offset != 0 or total is not None or not initial or refresh.exists()
                            or not (zero_amazon_lag or bounded_meta_lag)
                            or headers.get("x-apify-pagination-offset") != "0"
                            or headers.get("x-apify-pagination-limit") != str(limit)):
                        raise SafetyError("DATASET_PAGINATION_INCONSISTENT")
                    atomic_json(refresh, {"original_response": first["id"], "original_hash": first["hash"],
                                          "offset": offset, "reason": "zero_counter_nonempty_body" if zero_amazon_lag else "lagging_counter_same_body",
                                          "at": self.now().isoformat()})
                    self.read(lambda: self.transport.dataset_page(dataset, offset=offset, limit=limit), "dataset_count_corroboration")
                    metadata = load_json(self.folder / "http" / (self.transport.last_reference + ".json"))
                    if metadata["hash"] != first["hash"]:
                        raise SafetyError("SAVED_PAGE_CONFLICT")
            else:
                self.read(lambda: self.transport.dataset_page(dataset, offset=offset, limit=limit), "dataset_capture")
                metadata = load_json(self.folder / "http" / (self.transport.last_reference + ".json"))
            loaded, received = self.checked_page(metadata, dataset, offset, limit, total)
            reference = metadata["id"]
            total = received["total"]
            rows.extend(loaded)
            progress["pages"].append({"offset": offset, "count": len(loaded), "total": total, "response": reference})
            offset += len(loaded)
            progress["complete"] = offset >= total or offset >= 1000 or len(progress["pages"]) >= 10
            progress["truncated"] = offset < total and progress["complete"]
            atomic_json(progress_path, progress)
        mapped = capture_mapping(self.journal, self.prepared.attempt_id, rows, purpose=self.prepared.purpose, query_reference=self.prepared.attempt_id, prepared=self.prepared)
        snapshot = {"schema_version": 1, "dataset_id": dataset, "collection_status": data["status"],
                    "bounded_capture_complete": progress["complete"], "truncated": progress["truncated"],
                    "returned_total": total, "captured_count": len(rows), "captured_hash": digest(rows),
                    "mapping_status": mapped["status"], "requested_country": "IN", "purpose": self.prepared.purpose,
                    "pages": progress["pages"], "observed_at": self.now().isoformat()}
        if total == 0 and rows:
            snapshot["capture_contents_kind"] = "source_error_diagnostic_not_dataset_items"
        atomic_json(self.folder / "capture_manifest.json", snapshot)
        state = self.journal.state()[1][self.prepared.attempt_id]
        return {"schema_version": 1, "attempt": self.prepared.attempt_id, "verdict": "Partial",
                "code": "CAPTURED_REVIEW_AND_READ_COST_PENDING", "phase": "review", "remote_may_run": False,
                "captured_count": len(rows), "mapped_count": len(mapped["records"]), "truncated": progress["truncated"],
                "actor_actual_usd": state["charge"], "read_reserved_usd": state["read_hold"] if state["read_charge"] is None else "0",
                "read_actual_usd": state["read_charge"], "next_action": "inspect_saved_sample_and_settle_read_costs",
                **renewed_read_summary(state)}

    def checked_page(self, metadata, dataset, offset, limit, total):
        if (metadata.get("method") != "GET" or metadata.get("path") != "/v2/datasets/" + dataset + "/items"
                or metadata.get("status") != 200 or metadata.get("query") != {"format": "json", "offset": offset, "limit": limit}):
            raise SafetyError("SAVED_PAGE_CONFLICT")
        loaded = self.saved_response(metadata)
        try:
            headers = metadata["headers"]
            values = [headers["x-apify-pagination-" + key] for key in ("offset", "limit", "count", "total")]
            if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value) for value in values):
                raise ValueError
            received = dict(zip(("offset", "limit", "count", "total"), map(int, values)))
        except (KeyError, TypeError, ValueError):
            raise SafetyError("DATASET_PAGINATION_UNVERIFIED") from None
        # Observed empty-source shapes use zero item counters while returning
        # one diagnostic object. Preserve only these exact request-bound shapes;
        # arbitrary mismatches and unknown errors still fail pagination checks.
        zero_diagnostic = (offset == 0 and total is None
                           and received == {"offset": 0, "limit": limit, "count": 0, "total": 0}
                           and isinstance(loaded, list) and len(loaded) == 1 and isinstance(loaded[0], dict))
        diagnostic = False
        if zero_diagnostic:
            row = loaded[0]
            if request_stage(self.prepared) == "amazon":
                diagnostic = (set(row) == {"categoryUrl", "status", "errorMessage"}
                              and row["categoryUrl"] == self.prepared.input["urls"][0]
                              and type(row["status"]) is int and row["status"] == 200
                              and row["errorMessage"] == "Empty page structure")
            elif request_stage(self.prepared) == "meta":
                diagnostic = (set(row) == {"error", "errorCode", "url"}
                              and row["error"] == "Ads not found" and row["errorCode"] == "ADS_NOT_FOUND"
                              and row["url"] == self.prepared.input["urls"][0]["url"])
        if diagnostic:
            return loaded, received
        if (not isinstance(loaded, list) or received["offset"] != offset or received["limit"] != limit
                or received["count"] != len(loaded) or len(loaded) > limit or received["total"] < offset + len(loaded)
                or (total is not None and received["total"] != total)
                or (len(loaded) < limit and offset + len(loaded) != received["total"])):
            raise SafetyError("DATASET_PAGINATION_INCONSISTENT")
        return loaded, received

    def verify_saved_capture(self, manifest, rows):
        state = self.journal.state()[1].get(self.prepared.attempt_id)
        if (not state or manifest.get("captured_hash") != state["capture"] or digest(rows) != state["capture"]
                or manifest.get("dataset_id") != state["dataset_id"] or manifest.get("purpose") != self.prepared.purpose
                or manifest.get("collection_status") != state["remote_status"]):
            raise SafetyError("REVIEW_SNAPSHOT_MISMATCH")
        pages, rebuilt, offset, total = manifest.get("pages"), [], 0, None
        if not isinstance(pages, list) or not 1 <= len(pages) <= 10:
            raise SafetyError("REVIEW_SNAPSHOT_MISMATCH")
        for page in pages:
            metadata = load_json(self.folder / "http" / (identifier(page["response"]) + ".json"))
            loaded, received = self.checked_page(metadata, state["dataset_id"], offset, min(100, 1000-offset), total)
            if page != {"offset": offset, "count": len(loaded), "total": received["total"], "response": metadata["id"]}:
                raise SafetyError("SAVED_PAGE_CONFLICT")
            rebuilt.extend(loaded)
            offset += len(loaded)
            total = received["total"]
        complete = offset >= total or offset >= 1000 or len(pages) >= 10
        if (digest(rebuilt) != state["capture"] or manifest.get("captured_count") != offset or manifest.get("returned_total") != total
                or manifest.get("bounded_capture_complete") is not complete or manifest.get("truncated") is not (complete and offset < total)):
            raise SafetyError("REVIEW_SNAPSHOT_MISMATCH")
        if total == 0 and rebuilt and manifest.get("capture_contents_kind") != "source_error_diagnostic_not_dataset_items":
            raise SafetyError("REVIEW_SNAPSHOT_MISMATCH")

    def review(self, attempt_id):
        self.prepared = load_preparation(self.root, attempt_id)
        self.folder = safe_path(self.journal.root, attempt_id)
        manifest = load_json(self.folder / "capture_manifest.json")
        rows = load_json(self.folder / "captured_rows.json", TOTAL_BYTES)
        self.verify_saved_capture(manifest, rows)
        state = self.journal.state()[1].get(attempt_id)
        if not state or state["capture"] != manifest["captured_hash"]:
            raise SafetyError("REVIEW_SNAPSHOT_MISMATCH")
        expected = map_prepared_rows(rows, self.prepared)
        if load_json(self.folder / "mapped_rows.json", TOTAL_BYTES) != expected:
            raise SafetyError("REVIEW_MAPPING_MISMATCH")
        review = load_json(self.folder / "operator_review.json")
        required = {"schema_version", "attempt", "snapshot_hash", "inspected_at", "controls_checked", "schema_checked",
                    "scope_checked", "commercial_relevance_checked", "defects", "verdict", "read_cost_usd", "read_cost_evidence"}
        if not isinstance(review, dict) or set(review) != required or type(review["schema_version"]) is not int or review["schema_version"] != 1 or review["attempt"] != attempt_id or review["snapshot_hash"] != manifest["captured_hash"]:
            raise SafetyError("REVIEW_RECORD_INVALID")
        if timestamp(review["inspected_at"]) > self.now():
            raise SafetyError("REVIEW_RECORD_INVALID")
        if (not isinstance(review["defects"], list) or any(not isinstance(item, str) or not item.strip() for item in review["defects"])
                or any(type(review[key]) is not bool for key in ("controls_checked", "schema_checked", "scope_checked", "commercial_relevance_checked"))
                or review["verdict"] not in ("FunctionalPass", "QualityPass", "Partial", "Failed", "Blocked")):
            raise SafetyError("REVIEW_RECORD_INVALID")
        if review["read_cost_usd"] is not None and state["read_charge"] is None:
            self.settle(attempt_id, review["read_cost_evidence"], expected_amount=review["read_cost_usd"])
        if state["read_charge"] is not None and review["read_cost_usd"] is not None and usd(review["read_cost_usd"]) != usd(state["read_charge"]):
            raise SafetyError("READ_COST_EVIDENCE_CONFLICT")
        if review["verdict"] in ("FunctionalPass", "QualityPass"):
            state = self.journal.state()[1][attempt_id]
            mapped = load_json(self.folder / "mapped_rows.json", TOTAL_BYTES)
            if (any(review[key] is not True for key in ("controls_checked", "schema_checked", "scope_checked", "commercial_relevance_checked"))
                    or review["defects"] or not mapped["records"] or manifest["truncated"] or not manifest["bounded_capture_complete"]
                    or mapped["status"] != "usable" or state["remote_status"] != "SUCCEEDED"
                    or state["controls_ok"] is not True or state["charge"] is None
                    or usd(state["charge"]) > usd(state["actor_cap"])
                    or any(read["charge"] is not None and usd(read["charge"]) > usd(read["cap"])
                           for read in state.get("renewed_reads", {}).values())
                    or (state["read_charge"] is not None and usd(state["read_charge"]) > usd(state["read_hold"]))
                    or (review["verdict"] == "FunctionalPass" and (state["read_charge"] is None
                        or any(read["charge"] is None for read in state.get("renewed_reads", {}).values())))
                    or (review["verdict"] == "QualityPass" and (state["read_charge"] is not None or usd(state["read_hold"]) <= 0))):
                raise SafetyError("FUNCTIONAL_ACCEPTANCE_INCOMPLETE")
        evidence = "review-" + digest(review)[:20]
        self.journal.append(evidence, "review", {"attempt": attempt_id, "verdict": review["verdict"],
                            "snapshot_hash": review["snapshot_hash"], "evidence": "operator_review"})
        state = self.journal.state()[1][attempt_id]
        result = {"schema_version": 1, "attempt": attempt_id, "verdict": review["verdict"],
                  "code": request_stage(self.prepared).upper() + "_SAMPLE_REVIEWED_NOT_PRODUCTION_ACCEPTANCE", "phase": "review", "remote_may_run": False,
                  "actor_actual_usd": state["charge"], "read_actual_usd": state["read_charge"],
                  "read_reserved_usd": state["read_hold"] if state["read_charge"] is None else "0",
                  "next_action": ("consult_current_benchmark_record" if review["verdict"] in ("FunctionalPass", "QualityPass")
                                  else "review_source_limits_and_independent_trials" if review["verdict"] in ("Partial", "Failed")
                                  else "resolve_recorded_sample_defects")}
        atomic_json(self.folder / "result.json", result)
        return result

    def settle(self, attempt_id, evidence_id="read_cost", *, expected_amount=None):
        # Financial closure is independent of a good sample or a dataset.
        # No credential/network use. Console/provider proof is supplied by
        # the operator, never generated from our estimate or a rounded balance.
        self.prepared = load_preparation(self.root, attempt_id)
        self.folder = safe_path(self.journal.root, attempt_id)
        state = self.journal.state()[1].get(attempt_id)
        if not state or not state["remote_status"] or state["capture_status"] == "not_captured":
            raise SafetyError("READ_SETTLEMENT_NOT_READY")
        proof = load_json(self.folder / (identifier(evidence_id) + ".json"))
        if (not isinstance(proof, dict) or proof.get("evidence_type") != "operator_confirmed_account_usage"
                or proof.get("attempt") != attempt_id or proof.get("namespace") != state["namespace"]
                or proof.get("generation") != state["generation"] or not isinstance(proof.get("reason"), str) or not proof["reason"].strip()):
            raise SafetyError("READ_COST_EVIDENCE_REQUIRED")
        amount = usd(proof.get("usd"))
        if expected_amount is not None and amount != usd(expected_amount):
            raise SafetyError("READ_COST_EVIDENCE_CONFLICT")
        checked = timestamp(proof.get("checked_at"))
        activity_path = self.folder / "http_activity.json"
        activity = load_json(activity_path, 262144) if activity_path.exists() else []
        if checked > self.now() or any(checked < timestamp(call["started_at"]) for call in activity if call["storage"] and not call.get("funding_id")):
            raise SafetyError("READ_COST_EVIDENCE_REQUIRED")
        if state["read_charge"] is not None:
            if amount != usd(state["read_charge"]):
                raise SafetyError("READ_COST_EVIDENCE_CONFLICT")
        else:
            self.journal.append("read-cost-" + attempt_id, "read_cost", {"attempt": attempt_id, "usd": str(amount), "evidence": evidence_id})
        self.journal.settle_production_attempt(attempt_id)
        if (self.folder / "result.json").exists():
            result = load_json(self.folder / "result.json")
            result.update(read_reserved_usd="0", read_actual_usd=str(amount))
            atomic_json(self.folder / "result.json", result)
        return {"verdict": "Partial", "code": "READ_COST_RECORDED_NOT_QUALITY_ACCEPTANCE", "phase": "accounting",
                "remote_may_run": False, "next_action": "resolve_recorded_sample_defects"}

    def settle_renewed_reads(self, attempt_id, read_id, evidence_id="read_cost"):
        """Exact new-period usage proof; no key, provider or old-cost rewrite."""
        identifier(attempt_id); identifier(read_id); identifier(evidence_id)
        attempt = self.journal.state()[1].get(attempt_id)
        read = attempt.get("renewed_reads", {}).get(read_id) if attempt else None
        if read is None or not read["operations"]:
            raise SafetyError("READ_SETTLEMENT_NOT_READY")
        folder = safe_path(self.journal.root, attempt_id, "funded_reads", read_id)
        proof = load_json(folder / (evidence_id + ".json"))
        fields = {"evidence_type", "attempt", "read_id", "namespace", "generation", "operation_ids", "usd", "checked_at", "reason"}
        if (not isinstance(proof, dict) or set(proof) != fields
                or proof["evidence_type"] != "operator_confirmed_read_usage" or proof["attempt"] != attempt_id
                or proof["read_id"] != read_id or proof["namespace"] != read["namespace"] or proof["generation"] != read["generation"]
                or not isinstance(proof["reason"], str) or not proof["reason"].strip()
                or not isinstance(proof["operation_ids"], list) or any(not isinstance(value, str) for value in proof["operation_ids"])
                or len(set(proof["operation_ids"])) != len(proof["operation_ids"])
                or set(proof["operation_ids"]) != set(read["operations"])):
            raise SafetyError("READ_COST_EVIDENCE_REQUIRED")
        amount, checked = usd(proof["usd"]), timestamp(proof["checked_at"])
        if checked > self.now() or any(checked < timestamp(row["started_at"]) for row in read["operations"].values()):
            raise SafetyError("READ_COST_EVIDENCE_REQUIRED")
        data = {"attempt": attempt_id, "read_id": read_id, "usd": str(amount), "evidence": evidence_id}
        if read["charge"] is None:
            self.journal.append("cost-" + read_id, "renewed_read_cost", data)
        elif amount < usd(read["charge"]):
            raise SafetyError("READ_COST_DECREASE_REVIEW_REQUIRED")
        elif amount > usd(read["charge"]):
            data["previous_usd"] = read["charge"]
            self.journal.append("cost-update-" + digest(data)[:32], "renewed_read_cost_update", data)
        self.journal.release_renewed_read(attempt_id, read_id)
        result_path = safe_path(self.journal.root, attempt_id) / "result.json"
        if result_path.exists():
            result = load_json(result_path)
            result.update(renewed_read_summary(self.journal.state()[1][attempt_id]))
            atomic_json(result_path, result)
        return {"verdict": "Partial", "code": "RENEWED_READ_COST_RECORDED_NOT_QUALITY_ACCEPTANCE", "phase": "accounting",
                "remote_may_run": False, "read_id": read_id, "read_actual_usd": str(amount),
                "next_action": "consult_current_benchmark_record"}

    def resolve_non_submission(self, attempt_id):
        self.prepared = load_preparation(self.root, attempt_id)
        self.folder = safe_path(self.journal.root, attempt_id)
        state = self.journal.state()[1].get(attempt_id)
        if state and state["submission"] == "reserved":
            # This code persists intent before every POST. A reservation with
            # no intent is locally provable non-submission; deliberate resolve
            # can cancel it without pretending a missing remote job is proof.
            self.journal.append("cancel-" + attempt_id, "cancelled", {"attempt": attempt_id})
            self.journal.settle_production_attempt(attempt_id)
            result = {"verdict": "Blocked", "code": "UNSUBMITTED_RESERVATION_CANCELLED", "phase": "accounting", "remote_may_run": False}
            atomic_json(self.folder / "result.json", result)
            return result
        if not state or state["submission"] != "uncertain":
            raise SafetyError("NON_SUBMISSION_RESOLUTION_NOT_READY")
        proof = load_json(self.folder / "non_submission.json")
        required = {"evidence_type", "attempt", "namespace", "generation", "config_hash", "checked_at", "reason", "console_verified", "read_cost_usd"}
        if (not isinstance(proof, dict) or set(proof) != required or proof["evidence_type"] != "operator_confirmed_non_submission"
                or proof["console_verified"] is not True or proof["attempt"] != attempt_id
                or any(proof[field] != state[field] for field in ("namespace", "generation", "config_hash"))
                or not isinstance(proof["reason"], str) or not proof["reason"].strip()):
            raise SafetyError("NON_SUBMISSION_EVIDENCE_REQUIRED")
        intent = next(event for event in self.journal.events() if event["kind"] == "submitting" and event["data"]["attempt"] == attempt_id)
        if not timestamp(intent["at"]) <= timestamp(proof["checked_at"]) <= self.now():
            raise SafetyError("NON_SUBMISSION_EVIDENCE_REQUIRED")
        self.journal.append("resolved-" + attempt_id, "non_submission", {"attempt": attempt_id, "evidence": "non_submission",
                            "read_usd": str(usd(proof["read_cost_usd"]))})
        self.journal.settle_production_attempt(attempt_id)
        result = {"verdict": "Blocked", "code": "VERIFIED_NOT_SUBMITTED", "phase": "accounting", "remote_may_run": False,
                  "next_action": "resolve_recorded_sample_defects"}
        atomic_json(self.folder / "result.json", result)
        return result
