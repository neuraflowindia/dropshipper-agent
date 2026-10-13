"""Private account binding helpers; reuse existing owner-authorized credentials."""

import hashlib
import importlib.util
import json
from pathlib import Path
from datetime import datetime, timezone
import uuid
from dataclasses import replace
from decimal import Decimal

from .models import AccountBinding, SafetyError, identifier, timestamp, usd
from .storage import Journal, atomic_json, load_json, renewed_read_operations, safe_path

REGISTRY_VERSION = 1
REGISTRY_NAME = "apify_accounts.json"


def _private_reference(root, reference):
    """Registry entries name private files, never contain the secrets."""
    if not isinstance(reference, str) or not reference or len(reference) > 500:
        raise SafetyError("ACCOUNT_REGISTRY_REFERENCE_INVALID")
    path = Path(reference)
    private = safe_path(Path(root), "config", "local")
    destination = safe_path(path if path.is_absolute() else Path(root) / path)
    if not destination.resolve().is_relative_to(private.resolve()):
        raise SafetyError("ACCOUNT_REGISTRY_REFERENCE_INVALID")
    return destination


def load_account_registry(root, path=None):
    """Read an optional private pool; absent means the established one account.

    No provider access, credential read, new balance or allocation is created.
    Entries with credentials not supplied can remain explicitly disabled.
    """
    root = Path(root)
    path = _private_reference(root, path or f"config/local/{REGISTRY_NAME}")
    if not path.exists():
        return {"schema_version": REGISTRY_VERSION, "pool_enabled": False, "accounts": [{
            "id": "legacy", "enabled": True, "priority": 0,
            "credential": "config/local/providers.env", "binding": "config/local/apify_binding.json",
            "profile": "config/local/apify_profile.json", "handover": "config/local/apify_handover.json"}]}
    value = load_json(path, 32768)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "pool_enabled", "accounts"}
            or type(value["schema_version"]) is not int or value["schema_version"] != REGISTRY_VERSION
            or value["pool_enabled"] is not True or not isinstance(value["accounts"], list)
            or not 1 <= len(value["accounts"]) <= 9):
        raise SafetyError("ACCOUNT_REGISTRY_INVALID")
    ids, identities = set(), set()
    for entry in value["accounts"]:
        if (not isinstance(entry, dict) or set(entry) != {"id", "enabled", "priority", "credential", "binding", "profile", "handover"}
                or type(entry["enabled"]) is not bool or type(entry["priority"]) is not int or not 0 <= entry["priority"] <= 100):
            raise SafetyError("ACCOUNT_REGISTRY_INVALID")
        identifier(entry["id"])
        if entry["id"] in ids:
            raise SafetyError("ACCOUNT_REGISTRY_DUPLICATE")
        ids.add(entry["id"])
        for field in ("credential", "binding", "profile", "handover"):
            _private_reference(root, entry[field])
        bound_path = _private_reference(root, entry["binding"])
        if bound_path.exists():
            try:
                identities.add(load_binding(bound_path).account_key)
            except SafetyError:
                # The router reports an unusable profile individually. A
                # malformed optional file does not stop a healthy sibling.
                pass
    if len(identities) > 3:
        raise SafetyError("ACCOUNT_POOL_THREE_ACCOUNTS_ONLY")
    return value


def context_for_account(root, registry, account_id, *, launch=False, now=None,
                        expected_namespace=None, expected_generation=None):
    """Resolve one genuine saved identity; keys never substitute financial IDs."""
    entry = next((entry for entry in registry["accounts"] if entry["id"] == account_id), None)
    if entry is None or launch and not entry["enabled"]:
        raise SafetyError("ACCOUNT_PROFILE_NOT_CONFIGURED")
    paths = {field: _private_reference(root, entry[field]) for field in ("credential", "binding", "profile", "handover")}
    binding = load_binding(paths["binding"])
    profile = load_json(paths["profile"], 8192)
    if (not isinstance(profile, dict) or profile.get("schema_version") != 1
            or profile.get("namespace") != binding.namespace or profile.get("generation") != binding.generation
            or binding.role != "benchmark" or not binding.owner_account_confirmed or not binding.access_confirmed):
        raise SafetyError("ACCOUNT_PROFILE_MISMATCH")
    identifier(profile.get("provider_user_id")); timestamp(profile.get("period_ends_at"))
    # Real bindings use a stable hash of the authenticated provider ID.
    if binding.account_key != "apify-" + hashlib.sha256(profile["provider_user_id"].encode()).hexdigest()[:24]:
        raise SafetyError("ACCOUNT_IDENTITY_MISMATCH")
    if expected_namespace is not None and binding.namespace != expected_namespace:
        raise SafetyError("RECOVERY_ACCOUNT_MISMATCH")
    if expected_generation is not None and binding.generation != expected_generation:
        if launch:
            raise SafetyError("RECOVERY_ACCOUNT_MISMATCH")
        recovery_path = safe_path(Path(root), "config", "local", "apify_credential_recovery") / (
            "recovery-" + hashlib.sha256(f"{binding.namespace}:{expected_generation}:{binding.generation}".encode()).hexdigest()[:24] + ".json")
        proof = load_json(recovery_path, 8192) if recovery_path.exists() else None
        expected = {"schema_version": 1, "namespace": binding.namespace, "original_generation": expected_generation,
                    "replacement_generation": binding.generation, "replacement_account_id": entry["id"],
                    "provider_user_id": profile["provider_user_id"], "verified_at": proof.get("verified_at") if isinstance(proof, dict) else None,
                    "identity_verified": True, "recovery_only": True}
        if proof != expected or timestamp(proof["verified_at"]) > timestamp(binding.checked_at):
            raise SafetyError("CREDENTIAL_RECOVERY_PROOF_REQUIRED")
        # Original receipt and ledger generation stays intact. Only this safe
        # read context uses the replacement secret, under explicit proof.
        binding = replace(binding, generation=expected_generation)
        profile = {**profile, "generation": expected_generation}
    instant = now or datetime.now(timezone.utc)
    if launch and timestamp(profile["period_ends_at"]) <= instant:
        raise SafetyError("PROVIDER_PERIOD_EXPIRED")
    token = load_apify_token(paths["credential"])
    replace(binding, checked_at=instant.isoformat()).validate_key(token, "benchmark", instant)
    return {"id": entry["id"], "binding": binding, "profile": profile, "token": token, "paths": paths}


def context_for_origin(root, registry, origin, *, launch=False, now=None):
    """Use the original profile or a proved same-account read replacement."""
    candidates = [origin["account_id"]]
    if not launch:
        candidates += [entry["id"] for entry in registry["accounts"] if entry["id"] != origin["account_id"]]
    original_error = None
    for account_id in candidates:
        try:
            return context_for_account(root, registry, account_id, launch=launch, now=now,
                expected_namespace=origin["namespace"], expected_generation=origin["generation"])
        except SafetyError as error:
            original_error = original_error or error
    # A new billing period may legitimately update a slot's current profile.
    # Safe old-run reads keep the original private binding/profile snapshot,
    # rather than silently borrowing the new period's storage allowance.
    if not launch and origin.get("attempt_id"):
        saved_path = safe_path(Path(root), "config", "local", "apify_origins", origin["attempt_id"]) / "context.json"
        if saved_path.exists():
            saved = load_json(saved_path, 16384)
            if (saved.get("schema_version") != 1 or saved.get("origin") != origin
                    or set(saved.get("paths", {})) != {"credential", "binding", "profile", "handover"}):
                raise SafetyError("ACQUISITION_ACCOUNT_ORIGIN_CHANGED")
            binding = AccountBinding(**saved["binding"])
            profile = saved["profile"]
            if (binding.namespace != origin["namespace"] or binding.generation != origin["generation"]
                    or profile.get("namespace") != binding.namespace or profile.get("generation") != binding.generation
                    or binding.account_key != "apify-" + hashlib.sha256(profile["provider_user_id"].encode()).hexdigest()[:24]):
                raise SafetyError("RECOVERY_ACCOUNT_MISMATCH")
            paths = {field: _private_reference(root, value) for field, value in saved["paths"].items()}
            original = {"id": origin["account_id"], "binding": binding, "profile": profile, "paths": paths}
            try:
                token = load_apify_token(paths["credential"])
                replace(binding, checked_at=(now or datetime.now(timezone.utc)).isoformat()).validate_key(token, "benchmark", now)
                return dict(original, token=token)
            except SafetyError:
                # An actually authenticated same-account new-period binding
                # may supply its current key for old-run reads. This is not a
                # new collection identity, budget or original-period credit.
                replacement = current_read_funding_context(root, registry, original, now=now)
                proof_id = "read-identity-" + hashlib.sha256(
                    f"{origin['attempt_id']}:{binding.namespace}:{binding.generation}:{replacement['binding'].namespace}:{replacement['binding'].generation}:{replacement['binding'].checked_at}".encode()
                ).hexdigest()[:32]
                proof_path = safe_path(Path(root), "config", "local", "apify_credential_recovery", proof_id) / "proof.json"
                proof = {"schema_version": 1, "origin": origin, "replacement_namespace": replacement["binding"].namespace,
                         "replacement_generation": replacement["binding"].generation, "replacement_account_id": replacement["id"],
                         "provider_user_id": profile["provider_user_id"], "verified_at": replacement["binding"].checked_at,
                         "identity_source": "authenticated_user_read", "recovery_only": True}
                if proof_path.exists() and load_json(proof_path, 8192) != proof:
                    raise SafetyError("CREDENTIAL_RECOVERY_PROOF_CONFLICT")
                if not proof_path.exists():
                    atomic_json(proof_path, proof)
                recovered_binding = replace(binding, token_digest=replacement["binding"].token_digest,
                                             checked_at=replacement["binding"].checked_at)
                return dict(original, binding=recovered_binding, token=replacement["token"], paths=replacement["paths"],
                            credential_recovery_reference=str(proof_path))
    raise original_error or SafetyError("ORIGINAL_ACCOUNT_RECOVERY_REQUIRED")


def current_read_funding_context(root, registry, original, *, now=None):
    """Real current funds of the original account; never another pool account."""
    if not registry["pool_enabled"]:
        raise SafetyError("STORAGE_PERIOD_FUNDING_REVIEW_REQUIRED")
    instant = now or datetime.now(timezone.utc)
    wanted = original["binding"].account_key
    for entry in sorted(registry["accounts"], key=lambda value: (value["priority"], value["id"])):
        if not entry["enabled"]:
            continue
        try:
            current = context_for_account(root, registry, entry["id"], now=instant)
            binding, profile = current["binding"], current["profile"]
            if (binding.account_key != wanted or binding.namespace == original["binding"].namespace
                    or profile["provider_user_id"] != original["profile"]["provider_user_id"]
                    or profile.get("identity_source") != "authenticated_user_read"):
                continue
            facts = load_handover(current["paths"]["handover"], instant)
            if (facts["period"] != binding.period or facts["period_ends_at"] != profile["period_ends_at"]
                    or facts["provider_user_id"] not in (None, profile["provider_user_id"])):
                continue
            return dict(current, funding_facts=facts)
        except SafetyError:
            continue
    raise SafetyError("ORIGINAL_ACCOUNT_CURRENT_READ_FUNDS_REQUIRED")


def authorise_credential_recovery(root, journal, registry, account_id, original_generation):
    """Record verified same-account replacement for old reads, not relaunches.

    Bind the replacement through provider identity/access first. This local
    record requires an actual old ledger generation of that same account.
    """
    identifier(original_generation)
    context = context_for_account(root, registry, account_id)
    binding, profile = context["binding"], context["profile"]
    attempts = journal.state()[1]
    if (binding.generation == original_generation or
            not any(attempt["namespace"] == binding.namespace and attempt["generation"] == original_generation for attempt in attempts.values())
            or profile.get("identity_source") != "authenticated_user_read"):
        raise SafetyError("CREDENTIAL_RECOVERY_PROOF_REQUIRED")
    value = {"schema_version": 1, "namespace": binding.namespace, "original_generation": original_generation,
             "replacement_generation": binding.generation, "replacement_account_id": account_id,
             "provider_user_id": profile["provider_user_id"], "verified_at": binding.checked_at,
             "identity_verified": True, "recovery_only": True}
    name = "recovery-" + hashlib.sha256(f"{binding.namespace}:{original_generation}:{binding.generation}".encode()).hexdigest()[:24]
    path = safe_path(Path(root), "config", "local", "apify_credential_recovery") / (name + ".json")
    if path.exists() and load_json(path, 8192) != value:
        raise SafetyError("CREDENTIAL_RECOVERY_PROOF_CONFLICT")
    if not path.exists():
        atomic_json(path, value)
    return value


def choose_account(root, journal, registry, prepared, *, now=None, read_reserve="0.02"):
    """Only a new acquisition may choose; holds remain on their real account.

    This checks existing allocations, not merely a notional three-account cap.
    Missing/busy/exhausted profiles are reported independently and skipped.
    Caller holds the short financial lock; reservation rechecks at submission.
    """
    _, attempts = journal.state()
    if prepared.attempt_id in attempts:
        attempt = attempts[prepared.attempt_id]
        if prepared.config_hash != attempt["config_hash"]:
            raise SafetyError("RECOVERY_ACCOUNT_MISMATCH")
        origin_path = safe_path(Path(root), "config", "local", "apify_origins", prepared.attempt_id) / "origin.json"
        if origin_path.exists():
            origin = load_json(origin_path, 8192)
            if (origin.get("attempt_id") != prepared.attempt_id
                    or any(origin.get(field) != attempt[field] for field in ("namespace", "generation", "config_hash"))):
                raise SafetyError("RECOVERY_ACCOUNT_MISMATCH")
            return context_for_origin(root, registry, origin, now=now)
        for entry in registry["accounts"]:
            try:
                context = context_for_account(root, registry, entry["id"], now=now,
                    expected_namespace=attempt["namespace"], expected_generation=attempt["generation"])
            except SafetyError:
                continue
            return context
        raise SafetyError("ORIGINAL_ACCOUNT_RECOVERY_REQUIRED")
    from .models import request_stage
    reasons = []
    seen = set()
    for entry in sorted(registry["accounts"], key=lambda entry: (entry["priority"], entry["id"])):
        if not entry["enabled"]:
            continue
        try:
            context = context_for_account(root, registry, entry["id"], launch=True, now=now)
            account = context["binding"]
            if account.namespace in seen:
                continue
            facts = load_handover(context["paths"]["handover"], now)
            if (facts["period"] != account.period or facts["provider_user_id"] not in (None, context["profile"]["provider_user_id"])
                    or facts["period_ends_at"] != context["profile"]["period_ends_at"]):
                raise SafetyError("HANDOVER_ACCOUNT_MISMATCH")
            seen.add(account.namespace)
            journal.check_reservation(prepared, account, stage=request_stage(prepared),
                current_available=facts["available_usd"], read_reserve=read_reserve, account_scope=True)
            return context
        except SafetyError as error:
            reasons.append({"account_id": entry["id"], "reason": str(error)})
    # Opaque profile names and reason codes only; no keys, provider IDs or digest.
    error = SafetyError("NO_FUNDED_ACCOUNT_READY")
    error.account_reasons = reasons
    raise error


def freeze_account_origin(root, prepared, context):
    """Freeze once before any dispatch; changing a key cannot move this work."""
    binding = context["binding"]
    path = safe_path(Path(root), "config", "local", "apify_origins", prepared.attempt_id) / "origin.json"
    value = {"schema_version": 1, "attempt_id": prepared.attempt_id, "config_hash": prepared.config_hash,
             "account_id": context["id"], "namespace": binding.namespace, "generation": binding.generation}
    if path.exists():
        if load_json(path, 8192) != value:
            raise SafetyError("ACQUISITION_ACCOUNT_ORIGIN_CHANGED")
    else:
        from dataclasses import asdict
        # Secret bytes are not stored. Retain the original dated private
        # context for status recovery after the registry enters a new period.
        snapshot = {"schema_version": 1, "origin": value, "binding": asdict(binding), "profile": context["profile"],
                    "paths": {field: str(reference) for field, reference in context["paths"].items()}}
        atomic_json(path.parent / "context.json", snapshot)
        atomic_json(path, value)
    return value


def account_pool_status(root, journal, registry=None, *, now=None):
    """Offline numbers from real saved grants/holds; disabled keys add zero."""
    registry = registry or load_account_registry(root)
    budgets, attempts = journal.state()
    rows, seen = [], set()
    for entry in sorted(registry["accounts"], key=lambda entry: (entry["priority"], entry["id"])):
        row = {"account_id": entry["id"], "enabled": entry["enabled"], "state": "not_configured",
               "opening_usd": "0", "incurred_usd": "0", "unsettled_hold_usd": "0", "spendable_usd": "0"}
        try:
            binding = load_binding(_private_reference(root, entry["binding"]))
            budget = budgets.get(binding.namespace)
            if binding.namespace in seen:
                row["state"] = "same_account_alias"
            elif budget is not None:
                seen.add(binding.namespace)
                selected = [attempt for attempt in attempts.values() if attempt["namespace"] == binding.namespace]
                known = sum((usd(attempt["charge"] or "0") + usd(attempt["read_charge"] or "0") for attempt in selected), Decimal(0))
                holds = sum((usd(attempt["actor_cap"]) if attempt["charge"] is None else Decimal(0)) +
                            (usd(attempt["read_hold"]) if attempt["read_charge"] is None else Decimal(0)) for attempt in selected)
                renewed = renewed_read_operations(attempts, binding.namespace)
                known += sum((usd(read["charge"] or "0") for read in renewed), Decimal(0))
                holds += sum((usd(read["cap"]) for read in renewed if read["charge"] is None), Decimal(0))
                row.update(state="saved_account" if entry["enabled"] else "disabled", opening_usd=budget["allowance"],
                           incurred_usd=str(known), unsettled_hold_usd=str(holds),
                           spendable_usd=str(max(Decimal(0), journal.spendable(binding.namespace))))
        except SafetyError as error:
            row["reason"] = str(error)
        rows.append(row)
    renewed = renewed_read_operations(attempts)
    return {"schema_version": 1, "pool_enabled": registry["pool_enabled"], "accounts": rows,
            "configured_financial_accounts": len(seen),
            "incurred_usd": str(sum((usd(attempt["charge"] or "0") + usd(attempt["read_charge"] or "0")
                                     for attempt in attempts.values()), Decimal(0))
                                + sum((usd(read["charge"] or "0") for read in renewed), Decimal(0))),
            "unsettled_hold_usd": str(sum(((usd(attempt["actor_cap"]) if attempt["charge"] is None else Decimal(0)) +
                (usd(attempt["read_hold"]) if attempt["read_charge"] is None else Decimal(0)) for attempt in attempts.values()), Decimal(0))
                + sum((usd(read["cap"]) for read in renewed if read["charge"] is None), Decimal(0))),
            "renewed_read_funding_operations": len(renewed),
            "unlisted_attempt_namespaces": len({attempt["namespace"] for attempt in attempts.values()} - seen),
            "note": "Rows show configured saved account periods; totals retain every original journal period, including inactive profiles. Opening balances are dated inputs, not current provider balances. Holds are not bills."}


def load_binding(path):
    path = Path(path)
    safe_path(path.parent)
    if path.is_symlink():
        raise SafetyError("SYMLINK_PATH_REFUSED")
    try:
        with path.open("rb") as handle:
            raw = handle.read(8193)
        if len(raw) > 8192:
            raise ValueError
        from .models import decode_json
        data = decode_json(raw)
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data.pop("schema_version") != 1:
            raise ValueError
        return AccountBinding(**data)
    except (OSError, ValueError, TypeError, KeyError, SafetyError):
        raise SafetyError("PRIVATE_BINDING_UNAVAILABLE") from None


def save_binding(path, binding, journal, *, account_scope=False, renewal_origin_proof=None):
    # Caller owns one project lock and has confirmed account identity/access.
    # The actual provider period/balance is recorded in the ledger separately.
    path = Path(path)
    old = load_binding(path) if path.exists() else None
    same_identity = old is not None and all(getattr(old, field) == getattr(binding, field) for field in
                    ("account_key", "period", "role", "generation", "token_digest"))
    renewal = False
    if account_scope and old is not None and renewal_origin_proof is not None:
        proof = load_json(renewal_origin_proof, 65536)
        renewal = (proof.get("schema_version") == 1 and proof.get("identity_source") == "authenticated_user_read"
            and proof.get("prior_namespace") == old.namespace and proof.get("prior_generation") == old.generation
            and proof.get("new_namespace") == binding.namespace and proof.get("new_generation") == binding.generation
            and proof.get("account_key") == old.account_key == binding.account_key
            and old.period != binding.period and old.role == binding.role == "benchmark"
            and timestamp(proof.get("prior_period_ends_at")) <= timestamp(binding.checked_at)
            and timestamp(proof.get("new_period_ends_at")) > timestamp(binding.checked_at)
            and proof.get("provider_user_id") is not None
            and binding.account_key == "apify-" + hashlib.sha256(proof["provider_user_id"].encode()).hexdigest()[:24]
            and isinstance(proof.get("origins"), list))
        if not renewal:
            raise SafetyError("ACCOUNT_RENEWAL_ORIGIN_PROOF_INVALID")
    if not same_identity and not renewal and not (account_scope and old is None) and not journal.can_switch_key(namespace=(old or binding).namespace if account_scope else None):
        raise SafetyError("OLD_ACCOUNT_RECOVERY_REQUIRED")
    if path.exists():
        if old.token_digest != binding.token_digest and old.generation == binding.generation:
            raise SafetyError("NEW_GENERATION_REQUIRED")
    from dataclasses import asdict
    atomic_json(path, {"schema_version": 1, **asdict(binding)})


def _preserve_period_origins(root, journal, entry, paths, prior, current, provider_id, identity_source, end):
    """Snapshot old work before an authenticated expired-period renewal.

    This permits ordinary monthly continuation. It does not settle old money,
    change any attempt identity, or grant funds by renaming a credential.
    """
    prior_profile = load_json(paths["profile"], 8192)
    if (identity_source != "authenticated_user_read" or prior.account_key != current.account_key
            or prior.role != current.role or prior.period == current.period
            or prior_profile.get("namespace") != prior.namespace
            or prior_profile.get("generation") != prior.generation
            or prior_profile.get("provider_user_id") != provider_id
            or timestamp(prior_profile.get("period_ends_at")) > timestamp(current.checked_at)
            or timestamp(end) <= timestamp(current.checked_at)):
        raise SafetyError("ACCOUNT_RENEWAL_ORIGIN_PROOF_REQUIRED")
    from types import SimpleNamespace
    attempts = journal.state()[1]
    refs = []
    for attempt_id, attempt in attempts.items():
        if attempt["namespace"] != prior.namespace:
            continue
        folder = safe_path(Path(root), "config", "local", "apify_origins", attempt_id)
        with Journal(folder).locked(timeout=5):
            origin_path = folder / "origin.json"
            if origin_path.exists():
                origin = load_json(origin_path, 8192)
                saved = load_json(folder / "context.json", 16384)
                saved_binding = AccountBinding(**saved["binding"])
                if (origin.get("attempt_id") != attempt_id or saved.get("origin") != origin
                        or any(origin.get(field) != attempt[field] for field in ("namespace", "generation", "config_hash"))
                        or saved_binding.namespace != attempt["namespace"] or saved_binding.generation != attempt["generation"]
                        or saved["profile"].get("provider_user_id") != provider_id):
                    raise SafetyError("ACQUISITION_ACCOUNT_ORIGIN_CHANGED")
            else:
                if attempt["generation"] != prior.generation:
                    # A lost older key cannot be reconstructed from a current
                    # binding. Resolve that exact historical context first.
                    raise SafetyError("ORIGINAL_ACCOUNT_RECOVERY_REQUIRED")
                freeze_account_origin(root, SimpleNamespace(attempt_id=attempt_id, config_hash=attempt["config_hash"]),
                    {"id": entry["id"], "binding": prior, "profile": prior_profile, "paths": paths})
            refs.append(str(origin_path))
    proof = {"schema_version": 1, "prior_namespace": prior.namespace, "prior_generation": prior.generation,
             "new_namespace": current.namespace, "new_generation": current.generation, "account_key": current.account_key,
             "provider_user_id": provider_id, "identity_source": identity_source, "verified_at": current.checked_at,
             "prior_period_ends_at": prior_profile["period_ends_at"], "new_period_ends_at": end, "origins": refs}
    proof_id = "renewal-" + hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()[:32]
    destination = safe_path(Path(root), "config", "local", "apify_period_renewals", proof_id) / "proof.json"
    if not destination.exists():
        atomic_json(destination, proof)
    elif load_json(destination, 65536) != proof:
        raise SafetyError("ACCOUNT_RENEWAL_ORIGIN_PROOF_INVALID")
    return destination


def token_digest(token):
    # Private binding only. Never print, log or export this value.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_apify_token(path):
    # Reuse the established diagnostic's bounded parser without invoking its
    # main/check. No new secret-selection or environment fallback mechanism.
    path = Path(path)
    safe_path(path.parent)
    if path.is_symlink():
        raise SafetyError("SYMLINK_PATH_REFUSED")
    script = Path(__file__).resolve().parents[2] / "scripts/check_apify_connection.py"
    spec = importlib.util.spec_from_file_location("_radar_apify_key_parser", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.load_token(path)
    except (OSError, UnicodeError, module.ConfigurationError):
        raise SafetyError("PRIVATE_KEY_INVALID") from None


def load_handover(path, now=None):
    """Read saved authority and dated planning funds, not a live balance check.

Console funds retain their original date. Provider access/identity, refreshed
known charges and local reservations are checked by the live preflight path.
"""
    if not Path(path).exists():
        raise SafetyError("ACCOUNT_HANDOVER_REQUIRED")
    data = load_json(path, 8192)
    required = {"schema_version", "account_key", "period", "period_ends_at", "plan", "available_usd",
                "balance_checked_at", "independently_funded", "owner_account_confirmed", "owner_credential_confirmed",
                "confirmed_at", "provider_user_id", "output_route_reviewed", "production_provision_usd", "emergency_usd"}
    # Old records acknowledged a replacement. Preserve those genuine approvals,
    # but new records acknowledge use of a credential, not a required change.
    if isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 1:
        legacy = (required - {"owner_credential_confirmed"}) | {"owner_key_change_confirmed"}
        if set(data) != legacy:
            raise SafetyError("HANDOVER_INVALID")
        data = {**data, "schema_version": 2, "owner_credential_confirmed": data["owner_key_change_confirmed"]}
        del data["owner_key_change_confirmed"]
    if not isinstance(data, dict) or set(data) != required or type(data["schema_version"]) is not int or data["schema_version"] != 2:
        raise SafetyError("HANDOVER_INVALID")
    if data["plan"] != "Free" or any(data[k] is not True for k in ("owner_account_confirmed", "owner_credential_confirmed", "output_route_reviewed")):
        raise SafetyError("ACCOUNT_HANDOVER_REQUIRED")
    if type(data["independently_funded"]) is not bool:
        raise SafetyError("HANDOVER_INVALID")
    identifier(data["account_key"]); identifier(data["period"])
    if data["provider_user_id"] is not None:
        identifier(data["provider_user_id"])
    instant = now or datetime.now(timezone.utc)
    if timestamp(data["period_ends_at"]) <= instant:
        raise SafetyError("PROVIDER_PERIOD_EXPIRED")
    # Saved account/credential authority does not expire every day. A dated
    # owner balance remains a planning input within this provider period; do
    # not restamp it or present it as independently verified current funds.
    # Future timestamps are invalid, and actual period expiry still blocks.
    if timestamp(data["balance_checked_at"]) > instant or timestamp(data["confirmed_at"]) > instant:
        raise SafetyError("HANDOVER_TIMESTAMP_FUTURE")
    for key in ("available_usd", "production_provision_usd", "emergency_usd"):
        usd(data[key])
    # Funding role never changes the period cap/buffer. These legacy fields
    # describe the original binding; active allocations live in the journal.
    if usd(data["production_provision_usd"]) > usd("4.50"):
        raise SafetyError("FUTURE_ALLOCATION_INVALID")
    return data


def bind_account(root, journal, transport_factory, now=None, *, account_entry=None):
    """Only called after explicit live intent by the CLI; never change a key."""
    root = Path(root)
    paths = ({field: _private_reference(root, account_entry[field]) for field in ("credential", "binding", "profile", "handover")}
             if account_entry else {"handover": root / "config/local/apify_handover.json", "credential": root / "config/local/providers.env",
                                    "binding": root / "config/local/apify_binding.json", "profile": root / "config/local/apify_profile.json"})
    facts = load_handover(paths["handover"], now)
    token = load_apify_token(paths["credential"])
    path = paths["binding"]
    previous = load_binding(path) if path.exists() else None
    same_credential = (previous is not None and previous.owner_account_confirmed and previous.access_confirmed
                       and previous.token_digest == token_digest(token))
    if not same_credential and account_entry is None and not journal.can_switch_key():
        raise SafetyError("OLD_ACCOUNT_RECOVERY_REQUIRED")
    # A prior acknowledgement is not permission to silently bind a later key.
    # Editing another provider's entry is not an Apify credential change.
    if not same_credential and timestamp(facts["confirmed_at"]).timestamp() + 1 < paths["credential"].stat().st_mtime:
        raise SafetyError("CREDENTIAL_CONFIRMATION_REQUIRED")
    transport = transport_factory(token)
    transport.deadline = transport.clock() + 90
    response = transport.run_list()
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or len(data["items"]) > 50:
        raise SafetyError("ACCOUNT_ACCESS_RESPONSE_INVALID")
    provider_id, identity_source = facts["provider_user_id"], "owner_console"
    from .http_io import HttpFailure
    try:
        identity = transport.identity()
        observed = identity.get("data", {}).get("id")
        identifier(observed)
        if provider_id is not None and observed != provider_id:
            raise SafetyError("ACCOUNT_IDENTITY_MISMATCH")
        provider_id, identity_source = observed, "authenticated_user_read"
    except HttpFailure as error:
        if error.status != 403:
            raise
        if provider_id is None:
            raise SafetyError("CONSOLE_ACCOUNT_ID_REQUIRED") from None
    # The same provider user ID always yields the same financial identity;
    # a renamed owner label or a replacement token cannot create fresh funds.
    account_key = "apify-" + hashlib.sha256(provider_id.encode()).hexdigest()[:24]
    if account_entry:
        registry = load_account_registry(root)
        identities = set()
        for entry in registry["accounts"]:
            bound_path = _private_reference(root, entry["binding"])
            if bound_path.exists():
                identities.add(load_binding(bound_path).account_key)
        if account_key not in identities and len(identities) >= 3:
            raise SafetyError("ACCOUNT_POOL_THREE_ACCOUNTS_ONLY")
    if previous and previous.token_digest == token_digest(token) and previous.account_key != account_key:
        raise SafetyError("ACCOUNT_IDENTITY_MISMATCH")
    generation = previous.generation if previous and previous.token_digest == token_digest(token) else "key-" + uuid.uuid4().hex
    binding = AccountBinding(account_key, facts["period"], "benchmark", generation, token_digest(token),
                             (now or datetime.now(timezone.utc)).isoformat(), True, True)
    renewal_proof = None
    if account_entry and previous is not None and previous.account_key == binding.account_key and previous.period != binding.period:
        # Validate and retain every prior run before replacing its private slot
        # or recording this genuinely dated next period's opening balance.
        renewal_proof = _preserve_period_origins(root, journal, account_entry, paths, previous, binding,
            provider_id, identity_source, facts["period_ends_at"])
    budgets, _ = journal.state()
    if binding.namespace not in budgets:
        # Legacy provision fields must not invalidate a legitimate low opening
        # balance. Preserve the balance and represent current allocation in the
        # policy; insufficient headroom blocks spending, not account recording.
        journal.setup_budget(binding.namespace, facts["available_usd"])
        journal.apply_policy(binding.namespace, facts["production_provision_usd"], "initial-owner-allocation")
    else:
        budget = budgets[binding.namespace]
        if "policy" not in budget:
            raise SafetyError("BUDGET_POLICY_MIGRATION_REQUIRED")
    save_binding(path, binding, journal, account_scope=account_entry is not None, renewal_origin_proof=renewal_proof)
    atomic_json(paths["profile"], {"schema_version": 1, "namespace": binding.namespace,
                "generation": binding.generation, "provider_user_id": provider_id, "identity_source": identity_source,
                "owner_account_key": facts["account_key"], "period_ends_at": facts["period_ends_at"], "plan": "Free"})
    return binding


def bind_registered_account(root, journal, account_id, transport_factory, *, now=None, registry=None):
    """Explicit safe provider reads bind one supplied profile, never launch it."""
    registry = registry or load_account_registry(root)
    entry = next((entry for entry in registry["accounts"] if entry["id"] == account_id and entry["enabled"]), None)
    if entry is None or not registry["pool_enabled"]:
        raise SafetyError("ACCOUNT_PROFILE_NOT_CONFIGURED")
    from .storage import ShortJournal
    if journal._locked:
        raise SafetyError("NETWORK_FINANCIAL_LOCK_REFUSED")
    with Journal(safe_path(Path(root), "config", "local", "apify_profile_guards", account_id)).locked():
        return bind_account(root, ShortJournal(journal, account_scope=True), transport_factory, now, account_entry=entry)
