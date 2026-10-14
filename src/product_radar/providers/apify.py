"""Explicit Apify transport, receipt validation and saved-response mapping."""

from datetime import datetime, timezone
import json
import re
import shutil
import time
from urllib.parse import urlsplit

from ..models import SafetyError, canonical, decode_json, identifier, timestamp, usd, prepared_request, request_stage
from ..storage import atomic_json, safe_path
from ..http_io import HttpFailure, NoRedirects, perform_raw, worker_call


class ApifyTransport:
    def __init__(self, token, opener=None):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", token):
            raise SafetyError("INVALID_TOKEN")
        self._token = token
        self._opener = opener
        self.deadline = None
        self.clock = time.monotonic
        self.response_sink = None
        self.activity_sink = None
        self.last_response = None
        self.last_reference = None
        self.purpose = "unspecified"

    def request(self, method, path, *, query=None, body=None, byte_limit=65536, decimal_numbers=True):
        spec = {"method": method, "path": path, "query": query, "body": body,
                "byte_limit": byte_limit, "token": self._token}
        from ..http_io import validate_spec
        validate_spec(spec)
        self.last_response, self.last_reference = None, None
        timeout = min(30, self.deadline - self.clock()) if self.deadline is not None else 30
        if timeout <= 0:
            raise HttpFailure("HTTP_DEADLINE_EXCEEDED", retryable=True)
        safe_spec = {key: value for key, value in spec.items() if key != "token"}
        activity = self.activity_sink(safe_spec, self.purpose) if self.activity_sink else None
        try:
            wire = perform_raw(spec, self._opener) if self._opener is not None else worker_call(spec, timeout=timeout)
            self.last_response = wire
            if self.response_sink:
                self.last_reference = self.response_sink(safe_spec, wire, self.purpose, activity)
            return decode_json(wire.body, decimals=decimal_numbers)
        except SafetyError as error:
            if str(error) == "JSON_RESPONSE_INVALID":
                raise SafetyError("PROVIDER_RESPONSE_INVALID") from None
            raise
        except (ValueError, UnicodeError):
            raise SafetyError("PROVIDER_RESPONSE_INVALID") from None

    def verify_token(self, token):
        if token != self._token:
            raise SafetyError("TRANSPORT_KEY_MISMATCH")

    def launch(self, prepared):
        return self.request("POST", "/v2/acts/" + prepared.actor + "/runs",
                            query=prepared.options, body=prepared.input)

    def status(self, run_id):
        return self.request("GET", "/v2/actor-runs/" + identifier(run_id))

    def run_list(self, offset=0, limit=50, started_after=None):
        if type(offset) is not int or offset not in (0, 50) or limit != 50:
            raise SafetyError("RUN_LIST_RANGE_REFUSED")
        query = {"limit": limit, "offset": offset, "desc": "true"}
        if started_after:
            timestamp(started_after)
            query["startedAfter"] = started_after
        return self.request("GET", "/v2/actor-runs", query=query)

    def run_input(self, store_id):
        return self.request("GET", "/v2/key-value-stores/" + identifier(store_id) + "/records/INPUT")

    def actor_metadata(self, actor="curious_coder~facebook-ads-library-scraper"):
        return self.request("GET", "/v2/acts/" + actor, byte_limit=524288)

    def build_metadata(self, build_id):
        return self.request("GET", "/v2/actor-builds/" + identifier(build_id), byte_limit=524288)

    def identity(self):
        return self.request("GET", "/v2/users/me")

    def monthly_usage(self):
        # Optional current-period read only. Denial is not invalid-key proof;
        # the operator can supply Console facts without widening token scope.
        return self.request("GET", "/v2/users/me/usage/monthly", byte_limit=524288)

    def limits(self):
        # Free account read: the current monthly usage cycle, its usage limit
        # and the usage so far (GET /v2/users/me/limits). A narrow key may be refused.
        return self.request("GET", "/v2/users/me/limits", byte_limit=65536)

    def dataset_page(self, dataset_id, offset=0, limit=100):
        if type(offset) is not int or type(limit) is not int or not 0 <= offset < 1000 or not 1 <= limit <= min(100, 1000 - offset):
            raise SafetyError("DATASET_RANGE_REFUSED")
        result = self.request("GET", "/v2/datasets/" + identifier(dataset_id) + "/items",
                              query={"format": "json", "offset": offset, "limit": limit},
                              byte_limit=5 * 1024 * 1024, decimal_numbers=False)
        if not isinstance(result, list) or len(result) > limit:
            raise SafetyError("DATASET_RESPONSE_INVALID")
        return result


def submit_once(prepared, binding, token, journal, transport, *, explicit_live=False,
                preflight=None, now=None, short_locks=False):
    # Internal primitive behind the explicitly gated live CLI/controller.
    # A copied/validated contract freezes nested mutable inputs before hashing.
    prepared = prepared_request(json.loads(canonical(prepared.to_dict())))
    if not explicit_live:
        raise SafetyError("EXPLICIT_LIVE_REQUIRED")
    if short_locks and journal._locked:
        raise SafetyError("NETWORK_FINANCIAL_LOCK_REFUSED")
    binding.validate_key(token, "benchmark", now)
    transport.verify_token(token)
    required = {"config_hash", "generation", "namespace", "checked_at", "available_usd", "balance_confirmed", "build_schema_pricing_confirmed",
                "output_route_reviewed", "runtime_charge_route_reviewed"}
    provenance = {"balance_source", "balance_checked_at"}
    if not isinstance(preflight, dict) or set(preflight) not in (required, required | {"read_reserve_usd"}, required | provenance, required | provenance | {"read_reserve_usd"}):
        raise SafetyError("PREFLIGHT_INCOMPLETE")
    if provenance <= set(preflight):
        if preflight["balance_source"] != "owner_console_dated" or timestamp(preflight["balance_checked_at"]) > timestamp(preflight["checked_at"]):
            raise SafetyError("PREFLIGHT_INCOMPLETE")
    age = ((now or datetime.now(timezone.utc)) - timestamp(preflight["checked_at"])).total_seconds()
    if (preflight["config_hash"] != prepared.config_hash or preflight["generation"] != binding.generation
            or preflight["namespace"] != binding.namespace or not 0 <= age <= 3600
            or any(preflight[k] is not True for k in ("balance_confirmed", "build_schema_pricing_confirmed", "output_route_reviewed", "runtime_charge_route_reviewed"))):
        raise SafetyError("PREFLIGHT_INCOMPLETE")
    usd(preflight["available_usd"])
    # Legacy callers hold the lock. Pool callers use a short compound intent
    # transaction, release it for network, then commit the receipt separately.
    # If any disk or transport failure occurs after submitting, the journal
    # remains uncertain. No exception path releases money or reposts the job.
    journal.state()  # includes a lock check before touching attempt files
    if shutil.disk_usage(journal.root).free < 64 * 1024 * 1024:
        raise SafetyError("INSUFFICIENT_LOCAL_SPACE")
    folder = safe_path(journal.root, prepared.attempt_id)
    saved_request = folder / "prepared.json"
    if saved_request.exists():
        try:
            if saved_request.is_symlink() or saved_request.stat().st_size > 32768 or json.loads(saved_request.read_bytes()) != prepared.to_dict():
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise SafetyError("ATTEMPT_ARTIFACT_CONFLICT") from None
    else:
        atomic_json(saved_request, prepared.to_dict())
    from contextlib import nullcontext
    with journal.locked(timeout=5) if short_locks else nullcontext():
        journal.reserve(prepared, binding, stage=request_stage(prepared), current_available=preflight["available_usd"], read_reserve=preflight.get("read_reserve_usd", "0"))
        journal.append("submit-" + prepared.attempt_id, "submitting", {"attempt": prepared.attempt_id})
    result = transport.launch(prepared)
    try:
        data = result["data"]
        run_id = identifier(data["id"])
        dataset_id = data.get("defaultDatasetId")
        if dataset_id is not None:
            identifier(dataset_id)
    except (KeyError, TypeError):
        raise SafetyError("RECEIPT_INVALID_RECONCILE_REQUIRED") from None
    with journal.locked(timeout=5) if short_locks else nullcontext():
        journal.append("receipt-" + prepared.attempt_id, "receipt", {
            "attempt": prepared.attempt_id, "run_id": run_id, "dataset_id": dataset_id})
    # IDs are saved even if provider options/build/storage fail verification.
    # Missing/rejected mandatory controls do not justify another launch. The
    # official response does not promise a restartOnError echo; absence is OK.
    options = data.get("options", {})
    if not isinstance(options, dict):
        raise SafetyError("RETURNED_CONTROLS_REQUIRE_REVIEW")
    try:
        controls_ok = (data.get("buildId") == prepared.expected_build_id
                       and type(options.get("timeoutSecs")) is int and options["timeoutSecs"] == 300
                       and usd(str(options.get("maxTotalChargeUsd"))) == usd(prepared.reserved_usd)
                       and options.get("restartOnError", False) is False and dataset_id is not None)
    except SafetyError:
        controls_ok = False
    if not controls_ok:
        raise SafetyError("RETURNED_CONTROLS_REQUIRE_REVIEW")
    return {"run_id": run_id, "dataset_id": dataset_id}


def _creative_display(source):
    """Plain, bounded source text/links only; never fetch or render creative."""
    display, templates = {}, []
    for key in ("title", "body", "link_description"):
        value = source.get(key)
        if key == "body" and isinstance(value, dict):
            value = value.get("text")
        if isinstance(value, str) and value.strip():
            display[key] = value.strip()[:2000]
            if re.search(r"\{\{[^{}]{1,200}\}\}", display[key]):
                templates.append(key)
    for key in ("link_url", "original_image_url"):
        value = source.get(key)
        if isinstance(value, str) and len(value) <= 2048 and not any(ord(c) < 32 for c in value):
            try:
                parsed = urlsplit(value)
                if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password:
                    display[key] = value
            except ValueError:
                pass
    if templates:
        display["unresolved_template_fields"] = templates
    return display


def map_meta_rows(rows, *, purpose, query_reference):
    """Provisional saved-response mapping; never a product demand assessment.

    Field spelling must be checked against the first live response. Synthetic
    cases verify our decisions, not the current provider's response schema.
    """
    if purpose not in ("discovery", "validation") or not isinstance(rows, list) or len(rows) > 1000:
        raise SafetyError("INVALID_MAPPING_CONTEXT")
    identifier(query_reference)
    mapped, rejected, conflicts = {}, [], set()
    for index, row in enumerate(rows):
        reference = {"query": query_reference, "row": index}
        if not isinstance(row, dict):
            rejected.append({"reference": reference, "reason": "not_an_object"})
            continue
        ad_id = row.get("ad_archive_id")
        if not isinstance(ad_id, str) or not re.fullmatch(r"[0-9]{1,40}", ad_id):
            rejected.append({"reference": reference, "reason": "missing_or_invalid_ad_identity"})
            continue
        page_id = row.get("page_id")
        page_id = page_id if isinstance(page_id, str) and re.fullmatch(r"[0-9]{1,40}", page_id) else None
        active = row.get("is_active") if type(row.get("is_active")) is bool else None
        start = row.get("start_date")
        started_at = None
        if type(start) is int and 0 <= start <= 4102444800:
            started_at = datetime.fromtimestamp(start, timezone.utc).isoformat()
        reasons = []
        for key, value in (("advertiser", page_id), ("active_status", active), ("start_date", started_at)):
            if value is None:
                reasons.append("missing_or_invalid_" + key)
        item = {"ad_id": ad_id, "advertiser_id": page_id, "active": active, "started_at": started_at,
                "library_url": "https://www.facebook.com/ads/library/?id=" + ad_id,
                "purpose": purpose, "raw_references": [reference], "limitations": reasons}
        name = row.get("page_name")
        if isinstance(name, str) and name.strip():
            item["advertiser_name"] = name.strip()[:200]
        # Optional, provisional display fields only. Original nested creative
        # stays in captured rows; no URL is fetched and no HTML is rendered.
        snapshot = row.get("snapshot")
        if isinstance(snapshot, dict):
            display = _creative_display(snapshot)
            cards = snapshot.get("cards")
            if isinstance(cards, list) and cards:
                display["cards_total"] = len(cards)
                display["cards_truncated"] = len(cards) > 20
                display["cards"] = []
                invalid = []
                for index, card in enumerate(cards[:20]):
                    if not isinstance(card, dict):
                        invalid.append(index)
                        continue
                    fields = _creative_display(card)
                    if fields:
                        display["cards"].append({"card_index": index, **fields})
                if invalid:
                    display["invalid_card_indices"] = invalid
            elif cards is not None and not isinstance(cards, list):
                reasons.append("invalid_creative_cards")
            if display:
                item["presentation"] = display
        if ad_id in mapped:
            old = mapped[ad_id]
            if any(old[field] != item[field] for field in ("advertiser_id", "active", "started_at")):
                conflicts.add(ad_id)
            old["raw_references"].append(reference)
        else:
            mapped[ad_id] = item
    for ad_id in sorted(conflicts):
        for reference in mapped[ad_id]["raw_references"]:
            rejected.append({"reference": reference, "reason": "conflicting_duplicate_identity"})
        del mapped[ad_id]
    return {"schema_version": 1, "schema_status": "provisional_pending_live_inspection",
            "purpose": purpose, "records": list(mapped.values()), "rejected": rejected,
            "status": "partial" if rejected or any(i["limitations"] for i in mapped.values()) else "usable",
            "empty_scope_observation": len(rows) == 0}


def map_prepared_rows(rows, prepared):
    if request_stage(prepared) == "meta":
        return map_meta_rows(rows, purpose=prepared.purpose, query_reference=prepared.attempt_id)
    from ..research_sources import map_research_rows
    return map_research_rows(rows, prepared)


def capture_mapping(journal, attempt_id, rows, *, purpose, query_reference, prepared=None):
    # Captures only already-loaded, bounded data; no hidden recollection.
    _, attempts = journal.state()
    attempt = attempts.get(identifier(attempt_id))
    if not attempt or not attempt["run_id"]:
        raise SafetyError("RECEIPT_REQUIRED")
    if prepared is not None and (prepared.attempt_id != attempt_id or prepared.config_hash != attempt["config_hash"]
                                 or prepared.purpose != purpose or query_reference != attempt_id):
        raise SafetyError("MAPPING_REQUEST_MISMATCH")
    result = (map_prepared_rows(rows, prepared) if prepared is not None else
              map_meta_rows(rows, purpose=purpose, query_reference=query_reference))
    from ..models import digest
    folder = safe_path(journal.root, attempt_id)
    # Refuse to replace previously committed capture with different bytes.
    if attempt["capture"] is not None and attempt["capture"] != digest(rows):
        raise SafetyError("CAPTURE_CONFLICT")
    atomic_json(folder / "captured_rows.json", rows)
    journal.append("capture-" + attempt_id, "capture", {"attempt": attempt_id, "hash": digest(rows)})
    atomic_json(folder / "mapped_rows.json", result)
    journal.append("mapping-" + attempt_id, "mapping", {"attempt": attempt_id, "status": result["status"]})
    return result
