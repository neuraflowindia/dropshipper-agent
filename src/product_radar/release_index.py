"""Single-writer release contracts with an injected private storage adapter.

The injected storage must supply verified server observations and perform an
actual conditional write against the object's ETag. Its CAS operation applies
``apply_release_mutation`` in a trusted server context using server time, never
the laptop clock. R2 supports conditional put/ETags, not this whole adapter:
https://developers.cloudflare.com/r2/api/workers/workers-api-reference/

Storage interface (portal_storage.py implements the private Worker connection;
offline tests inject simulated storage, not hosted acceptance):
  read_index(key) -> {index, etag, server_observed_at, verified, authority}
  put_immutable(key, bytes, *, sha256, media_type) -> None
  read_object(key) -> bytes
  compare_and_swap_index(key, expected_etag, mutation) -> same snapshot | None
None means the conditional write failed; uploaded files stay unreferenced.
Uncertain writes require read/reconciliation, never a blind second mutation.
For the initial index, expected_etag=None means create-if-absent, not an
unconditional PUT (R2's If-None-Match: *); existing objects require their actual
ETag precondition. R2 metadata does not itself implement this whole adapter.
"""

from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .delivery_package import validate_delivery_manifest
from .live_outputs import digest_bytes
from .models import SafetyError, canonical, digest, identifier, timestamp
from .service import validate_issue
from .storage import safe_path

HASH = re.compile(r"[a-f0-9]{64}\Z")
TYPES = {"pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def _utc(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?(?:Z|\+00:00)", value):
        raise SafetyError("RELEASE_UTC_REQUIRED")
    instant = timestamp(value)
    if instant.utcoffset().total_seconds() != 0:
        raise SafetyError("RELEASE_UTC_REQUIRED")
    return instant


def scheduled_utc(issue_date, morning_time, *, zone="Asia/Kolkata"):
    """Convert the owner's configured HH:MM to UTC; no invented default."""
    if zone != "Asia/Kolkata" or not isinstance(morning_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", morning_time):
        raise SafetyError("RELEASE_TIME_CONFIGURATION_REQUIRED")
    try:
        day = date.fromisoformat(issue_date)
        if day.isoformat() != issue_date or day.weekday() not in (1, 3, 5):
            raise ValueError
        hour, minute = map(int, morning_time.split(":"))
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        raise SafetyError("RELEASE_DATE_INVALID") from None


def _available(entry, observed):
    if not entry["approved"] or not entry["complete"]:
        return None
    beginning = max(_utc(entry["scheduled_at"]), _utc(entry["server_activated_at"]))
    ending = _utc(entry["server_deactivated_at"]) if entry.get("server_deactivated_at") else None
    return beginning if beginning <= observed and (ending is None or ending > beginning) else None


def validate_release_index(index, client_id, *, server_at=None):
    identifier(client_id)
    if isinstance(index, dict) and index.get("schema_version") == 2:
        from .release_pages import validate_release_root
        return validate_release_root(index, client_id, server_at=server_at)
    if not isinstance(index, dict) or type(index.get("schema_version")) is not int or index["schema_version"] != 1 or index.get("client_id") != client_id:
        raise SafetyError("RELEASE_INDEX_INVALID")
    entries = index.get("issues")
    if not isinstance(entries, list) or len(entries) > 500 or len(canonical(index)) > 512 * 1024:
        raise SafetyError("RELEASE_INDEX_LIMIT")
    observed = _utc(server_at) if server_at else None
    active, revisions = set(), set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SafetyError("RELEASE_ENTRY_INVALID")
        issue_id = identifier(entry.get("issue_id"))
        revision = entry.get("revision")
        ordinal = entry.get("publication_ordinal")
        if (type(revision) is not int or revision < 1 or (issue_id, revision) in revisions
                or entry.get("remote_state") not in ("active", "withdrawn")
                or any(type(entry.get(field)) is not bool for field in ("approved", "complete"))
                or ordinal is not None and (type(ordinal) is not int or ordinal < 1)):
            raise SafetyError("RELEASE_ENTRY_INVALID")
        revisions.add((issue_id, revision))
        if entry["remote_state"] == "active":
            if issue_id in active or entry.get("server_deactivated_at") is not None:
                raise SafetyError("RELEASE_ACTIVE_REVISION_CONFLICT")
            active.add(issue_id)
        activation = _utc(entry.get("server_activated_at"))
        _utc(entry.get("scheduled_at"))
        if observed is not None and activation > observed:
            raise SafetyError("RELEASE_SERVER_TIME_INVALID")
        if entry["remote_state"] == "withdrawn":
            deactivated = _utc(entry.get("server_deactivated_at"))
            if deactivated < activation or observed is not None and deactivated > observed:
                raise SafetyError("RELEASE_SERVER_TIME_INVALID")
        for field in ("package_hash", "selection_hash"):
            if not isinstance(entry.get(field), str) or not HASH.fullmatch(entry[field]):
                raise SafetyError("RELEASE_HASH_INVALID")
        if (not isinstance(entry.get("title"), str) or not entry["title"].strip() or len(entry["title"]) > 160
                or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", entry["title"])
                or any(type(entry.get(field)) is not int for field in ("product_count", "new_count"))
                or not 0 <= entry["new_count"] <= entry["product_count"] <= 20):
            raise SafetyError("RELEASE_ENTRY_INVALID")
        languages, files = entry.get("languages"), entry.get("files")
        if (not isinstance(languages, list) or any(not isinstance(value, str) for value in languages) or len(languages) > 2 or len(set(languages)) != len(languages)
                or set(languages) - {"en", "hi-Latn"} or not isinstance(files, list) or len(files) > 3):
            raise SafetyError("RELEASE_FILES_INVALID")
        names, expected = set(), {"shortlist.xlsx", *("report-" + language + ".pdf" for language in languages)}
        for file in files:
            if not isinstance(file, dict):
                raise SafetyError("RELEASE_FILES_INVALID")
            name = file.get("name")
            extension = name.rsplit(".", 1)[-1] if isinstance(name, str) else None
            if (name not in expected or name in names or extension not in TYPES or file.get("media_type") != TYPES[extension]
                    or file.get("key") != f"clients/{client_id}/issues/{issue_id}/r{revision}/{name}"
                    or type(file.get("size")) is not int or not 1 <= file["size"] <= 32 * 1024 * 1024
                    or not isinstance(file.get("sha256"), str) or not HASH.fullmatch(file["sha256"])):
                raise SafetyError("RELEASE_FILES_INVALID")
            names.add(name)
        if entry["complete"] and (ordinal is None or names != expected or len(languages) != (2 if ordinal <= 2 else 1)
                                  or entry["product_count"] < 10 or 5 * entry["new_count"] < 4 * entry["product_count"]):
            raise SafetyError("RELEASE_INCOMPLETE_PACKAGE")
    return index


def project_issue_ordinal(index, issue_id, scheduled_at, *, server_at, read_object=None):
    """Count logical published IDs, then order the unreleased active queue."""
    validate_release_index(index, index["client_id"], server_at=server_at)
    identifier(issue_id)
    _utc(scheduled_at)
    observed = _utc(server_at)
    if index["schema_version"] == 2:
        tree = _release_tree(index, read_object)
        own = tree.get("issues", digest(issue_id))
        if own is not None and own.get("publication_ordinal") is not None:
            _v2_issue_index(tree, issue_id, server_at)
            return own["publication_ordinal"]
        pending, elapsed, after = {}, set(), None
        while True:
            page = tree.scan("queue", after=after, limit=50)
            for row in page["rows"]:
                q = row["value"]
                entry = _v2_entry(tree, q.get("sequence"), server_at)
                pointer = tree.get("issues", digest(entry["issue_id"]))
                if (q.get("issue_id") != entry["issue_id"] or pointer is None
                        or pointer.get("active_sequence") != entry["sequence"]
                        or entry["remote_state"] != "active" or type(q.get("is_new_issue")) is not bool
                        or q["is_new_issue"] != (pointer.get("first_exposed_at") is None)):
                    raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
                if not q["is_new_issue"]: continue
                if _available(entry, observed) is not None: elapsed.add(entry["issue_id"])
                elif entry["issue_id"] != issue_id: pending[entry["issue_id"]] = entry["scheduled_at"]
            if not page["more"]: break
            after = page["last_key"]
        if issue_id in elapsed:
            entry = _v2_issue_index(tree, issue_id, server_at)["issues"][-1]
            return entry["publication_ordinal"]
        pending[issue_id] = scheduled_at
        ordered = sorted(pending, key=lambda value: (_utc(pending[value]), value))
        return index["settled_publication_count"] + len(elapsed) + ordered.index(issue_id) + 1
    published = {}
    for entry in index["issues"]:
        if _available(entry, observed) is not None:
            ordinal = entry["publication_ordinal"]
            if entry["issue_id"] in published and published[entry["issue_id"]] != ordinal:
                raise SafetyError("RELEASE_PUBLISHED_ORDINAL_CONFLICT")
            published[entry["issue_id"]] = ordinal
    if len(set(published.values())) != len(published):
        raise SafetyError("RELEASE_PUBLISHED_ORDINAL_CONFLICT")
    if issue_id in published:
        return published[issue_id]
    queue = {entry["issue_id"]: entry["scheduled_at"] for entry in index["issues"]
             if entry["remote_state"] == "active" and entry["approved"] and entry["complete"] and entry["issue_id"] not in published}
    queue[issue_id] = scheduled_at
    ordered = sorted(queue, key=lambda value: (_utc(queue[value]), value))
    return len(published) + ordered.index(issue_id) + 1


def _approval(approval, values):
    if not isinstance(approval, dict) or approval.get("approved") is not True:
        raise SafetyError("RELEASE_OWNER_APPROVAL_REQUIRED")
    identifier(approval.get("owner_id"))
    if any(approval.get(field) != value or type(approval.get(field)) is not type(value) for field, value in values.items()):
        raise SafetyError("RELEASE_APPROVAL_BINDING_MISMATCH")


def plan_activation(index, manifest, approval, *, scheduled_at, server_at,
                    expected_previous=None, selected_language=None, allow_synthetic=False, read_object=None):
    """Pure serialisable mutation; trusted adapter supplies commit time later."""
    client_id = manifest.get("client_id")
    validate_release_index(index, client_id, server_at=server_at)
    ordinal = project_issue_ordinal(index, manifest["issue_id"], scheduled_at, server_at=server_at, read_object=read_object)
    if index["schema_version"] == 2:
        index = _v2_issue_index(_release_tree(index, read_object), manifest["issue_id"], server_at)
    _utc(scheduled_at)
    if type(allow_synthetic) is not bool:
        raise SafetyError("RELEASE_MUTATION_INVALID")
    if _utc(scheduled_at).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat() != manifest.get("release_date"):
        raise SafetyError("RELEASE_DATE_MISMATCH")
    if (manifest.get("complete") is not True or manifest.get("approved") is not False or manifest.get("release_ready") is not False
            or digest({key: value for key, value in manifest.items() if key != "package_hash"}) != manifest.get("package_hash")):
        raise SafetyError("RELEASE_DELIVERY_MANIFEST_INVALID")
    if manifest.get("evidence_type") != "captured" and not allow_synthetic:
        raise SafetyError("RELEASE_SYNTHETIC_PACKAGE_REFUSED")
    previous = [e for e in index["issues"] if e["issue_id"] == manifest["issue_id"]]
    active = next((e for e in previous if e["remote_state"] == "active"), None)
    replay = active and active["revision"] == manifest["revision"] and active["package_hash"] == manifest["package_hash"]
    if not replay:
        if (active and canonical(expected_previous) != canonical({"revision": active["revision"], "package_hash": active["package_hash"]})) or (not active and expected_previous is not None):
            raise SafetyError("RELEASE_PREVIOUS_REVISION_MISMATCH")
        if previous and manifest["revision"] <= max(e["revision"] for e in previous):
            raise SafetyError("RELEASE_REVISION_NOT_NEW")
        if (active and _available(active, _utc(server_at)) is not None and _utc(scheduled_at) > _utc(server_at)
                and approval.get("allow_visibility_gap") is not True):
            # The current portal selects one active revision. Replacing an
            # available report with a future revision would hide that issue
            # until its new schedule; do not imply old access is preserved.
            raise SafetyError("RELEASE_REPLACEMENT_VISIBILITY_REVIEW_REQUIRED")
    if manifest["publication_ordinal"] != ordinal:
        raise SafetyError("RELEASE_QUEUE_REPROJECTION_REQUIRED")
    if ordinal > 2 and (selected_language not in ("en", "hi-Latn") or manifest["languages"] != [selected_language]):
        raise SafetyError("RELEASE_LANGUAGE_CHOICE_REQUIRED")
    values = {field: manifest[field] for field in ("client_id", "issue_id", "revision", "package_hash", "selection_hash", "publication_ordinal")}
    values["scheduled_at"] = scheduled_at
    _approval(approval, values)
    if not replay and _utc(scheduled_at) <= _utc(server_at) and approval.get("allow_late") is not True:
        raise SafetyError("RELEASE_LATE_CONSEQUENCE_REVIEW_REQUIRED")
    return {"kind": "activate", "client_id": client_id, "manifest": deepcopy(manifest), "approval": deepcopy(approval),
            "scheduled_at": scheduled_at, "expected_previous": deepcopy(expected_previous), "selected_language": selected_language,
            "allow_synthetic": allow_synthetic}


def plan_withdrawal(index, issue_id, revision, package_hash, approval, *, server_at):
    validate_release_index(index, index["client_id"], server_at=server_at)
    identifier(issue_id)
    if type(revision) is not int or revision < 1 or not isinstance(package_hash, str) or not HASH.fullmatch(package_hash):
        raise SafetyError("RELEASE_PREVIOUS_REVISION_MISMATCH")
    _approval(approval, {"client_id": index["client_id"], "issue_id": issue_id, "revision": revision, "package_hash": package_hash, "action": "withdraw"})
    return {"kind": "withdraw", "client_id": index["client_id"], "issue_id": issue_id, "revision": revision,
            "package_hash": package_hash, "approval": deepcopy(approval)}


def _withdraw(entry, server_at, reason):
    entry.update(remote_state="withdrawn", server_deactivated_at=server_at, withdrawal_reason=reason)


def apply_release_mutation(index, mutation, *, server_at):
    """Trusted server-side transition inside real ETag CAS; pure offline here."""
    validate_release_index(index, mutation["client_id"], server_at=server_at)
    result = deepcopy(index)
    if mutation["kind"] == "activate":
        manifest = mutation["manifest"]
        plan_activation(index, manifest, mutation["approval"], scheduled_at=mutation["scheduled_at"], server_at=server_at,
                        expected_previous=mutation["expected_previous"], selected_language=mutation["selected_language"], allow_synthetic=mutation["allow_synthetic"])
        existing = [entry for entry in result["issues"] if entry["issue_id"] == manifest["issue_id"]]
        active = next((entry for entry in existing if entry["remote_state"] == "active"), None)
        if active and active["revision"] == manifest["revision"] and active["package_hash"] == manifest["package_hash"]:
            return result  # Lost receipt recovery, not another activation.
        prior = mutation["expected_previous"]
        if (active and prior != {"revision": active["revision"], "package_hash": active["package_hash"]}) or (not active and prior is not None):
            raise SafetyError("RELEASE_PREVIOUS_REVISION_MISMATCH")
        if existing and manifest["revision"] <= max(entry["revision"] for entry in existing):
            raise SafetyError("RELEASE_REVISION_NOT_NEW")
        if active:
            _withdraw(active, server_at, "superseded")
        title = "Product picks" if manifest["languages"][0] == "en" else "Product sujhaav"
        entry = {field: deepcopy(manifest[field]) for field in ("issue_id", "revision", "package_hash", "selection_hash", "publication_ordinal", "product_count", "new_count", "languages", "files")}
        entry.update(title=title, summary="", remote_state="active", approved=True, complete=True,
                     scheduled_at=mutation["scheduled_at"], server_activated_at=server_at,
                     owner_id=mutation["approval"]["owner_id"], evidence_type=manifest["evidence_type"],
                     facts_hash=manifest["facts_hash"], concept_ids=deepcopy(manifest["concept_ids"]))
        prefix = f'clients/{manifest["client_id"]}/issues/{manifest["issue_id"]}/r{manifest["revision"]}/'
        entry.update(manifest_key=prefix + "delivery-manifest.json", manifest_sha256=digest_bytes(canonical(manifest) + b"\n"),
                     facts_key=prefix + "final_report_data.json", facts_sha256=manifest["artifacts"]["facts"]["sha256"])
        result["issues"].append(entry)
    elif mutation["kind"] == "withdraw":
        plan_withdrawal(index, mutation["issue_id"], mutation["revision"], mutation["package_hash"], mutation["approval"], server_at=server_at)
        entry = next((e for e in result["issues"] if e["issue_id"] == mutation["issue_id"] and e["revision"] == mutation["revision"]), None)
        if entry is None or entry["package_hash"] != mutation["package_hash"]:
            raise SafetyError("RELEASE_PREVIOUS_REVISION_MISMATCH")
        if entry["remote_state"] != "withdrawn":
            _withdraw(entry, server_at, "owner_withdrawal")
    else:
        raise SafetyError("RELEASE_MUTATION_INVALID")
    # Do not block a cancellation or serve a now-wrong ordinal. Only future,
    # never-available affected queue entries are withdrawn; history survives.
    for _ in range(len(result["issues"]) + 1):
        queued = [e for e in result["issues"] if e["remote_state"] == "active" and _available(e, _utc(server_at)) is None and e["approved"] and e["complete"]]
        bad = [e for e in queued if project_issue_ordinal(result, e["issue_id"], e["scheduled_at"], server_at=server_at) != e["publication_ordinal"]]
        if not bad:
            break
        for entry in bad:
            _withdraw(entry, server_at, "queue_reprojection_required")
    return validate_release_index(result, mutation["client_id"], server_at=server_at)


def _snapshot(snapshot, client_id):
    if not isinstance(snapshot, dict) or snapshot.get("verified") is not True or snapshot.get("authority") != "release_index":
        raise SafetyError("RELEASE_TRUSTED_SNAPSHOT_REQUIRED")
    if snapshot.get("etag") is not None and (not isinstance(snapshot["etag"], str) or not snapshot["etag"]):
        raise SafetyError("RELEASE_ETAG_REQUIRED")
    _utc(snapshot.get("server_observed_at"))
    validate_release_index(snapshot.get("index"), client_id, server_at=snapshot.get("server_observed_at"))
    if snapshot.get("etag") is None and (snapshot["index"]["schema_version"] == 2 or snapshot["index"]["issues"]):
        raise SafetyError("RELEASE_ETAG_REQUIRED")
    return snapshot


def _release_tree(index, read_object):
    from .release_pages import ReleasePages
    if not callable(read_object): raise SafetyError("RELEASE_PAGE_READER_REQUIRED")
    return ReleasePages(index, read_object)


def _v2_entry(tree, sequence, server_at):
    from .release_pages import sequence_key
    entry = tree.get("archive", sequence_key(sequence))
    if entry is None or entry.get("sequence") != sequence:
        raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
    validate_release_index({"schema_version": 1, "client_id": tree.root["client_id"], "issues": [entry]},
        tree.root["client_id"], server_at=server_at)
    return entry


def _v2_issue_index(tree, issue_id, server_at):
    pointer = tree.get("issues", digest(issue_id))
    entries = []
    if pointer is not None:
        if pointer.get("issue_id") != issue_id: raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
        sequences = [pointer.get("latest_sequence")]
        if pointer.get("active_sequence") is not None and pointer["active_sequence"] not in sequences:
            sequences.append(pointer["active_sequence"])
        for sequence in sequences:
            entry = _v2_entry(tree, sequence, server_at)
            if entry["issue_id"] != issue_id: raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
            entries.append(entry)
    return {"schema_version": 1, "client_id": tree.root["client_id"], "issues": entries}


def release_receipt(snapshot, issue_id, revision, *, read_object=None):
    """Remote confirmation, including future/nonpublished withdrawal states."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("index"), dict):
        raise SafetyError("RELEASE_TRUSTED_SNAPSHOT_REQUIRED")
    _snapshot(snapshot, snapshot["index"].get("client_id"))
    identifier(issue_id)
    if type(revision) is not int or revision < 1:
        raise SafetyError("RELEASE_REVISION_MISSING")
    if snapshot["index"]["schema_version"] == 2:
        from .release_pages import sequence_key
        tree = _release_tree(snapshot["index"], read_object)
        ref = tree.get("revisions", digest(issue_id) + sequence_key(revision))
        entry = _v2_entry(tree, ref.get("sequence"), snapshot["server_observed_at"]) if ref else None
        if ref is not None and (ref.get("issue_id") != issue_id or ref.get("revision") != revision
                or entry["issue_id"] != issue_id or entry["revision"] != revision):
            raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
    else:
        entry = next((e for e in snapshot["index"]["issues"] if e["issue_id"] == issue_id and e["revision"] == revision), None)
    if entry is None:
        raise SafetyError("RELEASE_REVISION_MISSING")
    available = _available(entry, _utc(snapshot["server_observed_at"]))
    result = {field: entry[field] for field in ("issue_id", "revision", "selection_hash", "package_hash", "remote_state", "scheduled_at", "server_activated_at")}
    result.update(verified=True, authority="release_index", server_observed_at=snapshot["server_observed_at"],
                  server_deactivated_at=entry.get("server_deactivated_at"), publication_ordinal=entry["publication_ordinal"],
                  published=available is not None, available_at=available.isoformat() if available else None)
    if "facts_hash" in entry:
        # Genuine new entries retain the exact published facts identity. Old
        # receipts lacking it are not silently assigned an invented hash.
        if not isinstance(entry["facts_hash"], str) or not HASH.fullmatch(entry["facts_hash"]):
            raise SafetyError("RELEASE_FACTS_HASH_INVALID")
        result["facts_hash"] = entry["facts_hash"]
    result["receipt_id"] = "release-" + digest(result)[:40]
    return result


def publication_receipt(snapshot, issue_id, revision, *, read_object=None):
    receipt = release_receipt(snapshot, issue_id, revision, read_object=read_object)
    return receipt if receipt["published"] else None


def _deferred(before, after):
    if before["schema_version"] == 2 or after["schema_version"] == 2:
        # Version two never silently withdraws another issue. Its server
        # supplies an exact owner-action list instead of cancelling a queue.
        return []
    old_active = {(e["issue_id"], e["revision"]) for e in before["issues"] if e["remote_state"] == "active"}
    return sorted({e["issue_id"] for e in after["issues"] if (e["issue_id"], e["revision"]) in old_active
                   and e["remote_state"] == "withdrawn" and e.get("withdrawal_reason") == "queue_reprojection_required"})


def _v2_selected_history(index, products, own_issue, server_at, read_object):
    """Read only selected identities and their current holds, not all reports."""
    from .functional_lineage_portable import lineage_type_context
    tree = _release_tree(index, read_object)
    published, queued = set(), set()
    for product in products:
        cid = identifier(product["concept_id"])
        context = lineage_type_context(tree, cid)
        history_ids = set(context["member_ids"]) | set(context["ancestor_ids"])
        overlap_ids = history_ids | set(context["descendant_ids"])
        for seen_cid in sorted(history_ids):
            exposure = tree.get("types", digest(seen_cid))
            if exposure is not None:
                if exposure.get("concept_id") != seen_cid:
                    raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
                ref = exposure.get("first_publication")
                if exposure.get("first_exposed_at") is None:
                    if ref is not None: raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
                else:
                    if not isinstance(ref, dict) or _utc(exposure["first_exposed_at"]) > _utc(server_at):
                        raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
                    entry = _v2_entry(tree, ref.get("sequence"), server_at)
                    if (entry["issue_id"] != ref.get("issue_id") or seen_cid not in entry.get("concept_ids", [])
                            or _available(entry, _utc(server_at)) is None):
                        raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
                    if entry["issue_id"] != own_issue: published.add(cid)
        for held_cid in sorted(overlap_ids):
            after = None
            while True:
                page = tree.scan("holds", prefix=digest(held_cid), after=after, limit=50)
                for row in page["rows"]:
                    hold = row["value"]
                    entry = _v2_entry(tree, hold.get("sequence"), server_at)
                    pointer = tree.get("issues", digest(entry["issue_id"]))
                    if (hold.get("concept_id") != held_cid or hold.get("issue_id") != entry["issue_id"]
                            or held_cid not in entry.get("concept_ids", []) or pointer is None
                            or pointer.get("active_sequence") != entry["sequence"] or entry["remote_state"] != "active"):
                        raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
                    if entry["issue_id"] != own_issue:
                        if _available(entry, _utc(server_at)) is None:
                            queued.add(cid)
                        elif held_cid in history_ids:
                            published.add(cid)
                if not page["more"]: break
                after = page["last_key"]
    return published, queued


def advance_release_index(storage, client_id, *, expected_etag, migrate=False, finish=True):
    """One bounded owner operation; callers explicitly resume remaining work."""
    key = f"clients/{identifier(client_id)}/release-index.json"
    method = getattr(storage, "compare_and_swap_index_v2", None)
    if not callable(method): raise SafetyError("RELEASE_V2_STORAGE_REQUIRED")
    if type(migrate) is not bool or type(finish) is not bool: raise SafetyError("RELEASE_MUTATION_INVALID")
    before = _snapshot(storage.read_index(key), client_id)
    if before["etag"] != expected_etag and not (migrate and before["index"]["schema_version"] == 2
            and (before["index"].get("migration") or {}).get("source_etag") == expected_etag):
        raise SafetyError("RELEASE_INDEX_CONFLICT")
    mutation = {"kind": "migrate" if migrate else "reconcile", "client_id": client_id}
    if migrate: mutation["finish"] = finish
    after = method(key, expected_etag, mutation)
    if after is None: raise SafetyError("RELEASE_INDEX_CONFLICT")
    _snapshot(after, client_id)
    confirmed = _snapshot(storage.read_index(key), client_id)
    if confirmed["etag"] != after["etag"] or canonical(confirmed["index"]) != canonical(after["index"]):
        raise SafetyError("RELEASE_WRITE_RECONCILIATION_REQUIRED")
    return {"snapshot": confirmed, "mutation_applied": after.get("mutation_applied") is True,
        "migration": deepcopy(after.get("migration")), "settlement": deepcopy(after.get("settlement")),
        "next_action": after.get("next_action")}


def _valid_review_refs(refs):
    """Original references are ordered unique rows, never a lossy ID map."""
    if not isinstance(refs, list) or not refs:
        return False
    ids = []
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {"id", "hash"}
                or not isinstance(ref["hash"], str) or not HASH.fullmatch(ref["hash"])):
            return False
        try:
            ids.append(identifier(ref["id"]))
        except SafetyError:
            return False
    return ids == sorted(set(ids))


def apply_reviewed_release_aliases(storage, state, client_id, record_ids, *, expected_etag, approval,
                                 dependent_withdrawals=None):
    """Owner-only checked original proof, never an automatic draft publication."""
    from .functional_aliases import portable_alias_proof
    proof = portable_alias_proof(state, record_ids)
    _approval(approval, {"client_id": identifier(client_id), "action": "aliases", "proof_hash": digest(proof)})
    key = f"clients/{client_id}/release-index.json"
    before = _snapshot(storage.read_index(key), client_id)
    if before["index"]["schema_version"] != 2: raise SafetyError("RELEASE_V2_REQUIRED")
    method = getattr(storage, "compare_and_swap_index_v2", None)
    if not callable(method): raise SafetyError("RELEASE_V2_STORAGE_REQUIRED")
    mutation = {"kind": "aliases", "client_id": client_id, "proof": proof, "approval": deepcopy(approval)}
    if dependent_withdrawals is not None:
        if not isinstance(dependent_withdrawals, list) or len(dependent_withdrawals) > 20:
            raise SafetyError("RELEASE_MUTATION_INVALID")
        mutation["dependent_withdrawals"] = deepcopy(dependent_withdrawals)
    after = method(key, expected_etag, mutation)
    if after is None: raise SafetyError("RELEASE_INDEX_CONFLICT")
    _snapshot(after, client_id)
    confirmed = _snapshot(storage.read_index(key), client_id)
    if confirmed["etag"] != after["etag"] or canonical(confirmed["index"]) != canonical(after["index"]):
        raise SafetyError("RELEASE_WRITE_RECONCILIATION_REQUIRED")
    # An elapsed queue continuation does not assert that any edge was accepted.
    remaining, legacy = set(), set()
    if after.get("mutation_applied") is True:
        from .frozen_records import read_original_json
        from .functional_aliases import alias_projection
        from .release_pages import release_bytes
        tree = _release_tree(confirmed["index"], storage.read_object)
        aliases = alias_projection(state)
        for rid, wrapped in proof["records"].items():
            original = read_original_json(wrapped)
            members = original["input"]["member_ids"]
            remote = tree.get("aliases", digest(members[0]))
            if (not isinstance(remote, dict)
                    or set(remote) != {"member_id", "canonical_id", "member_ids", "record_refs", "ancestor_ids"}
                    or remote.get("member_id") != members[0] or remote.get("ancestor_ids") != []
                    or not isinstance(remote.get("member_ids"), list)
                    or not remote["member_ids"]
                    or any(not isinstance(cid, str) for cid in remote["member_ids"])
                    or remote["member_ids"] != sorted(set(remote["member_ids"]))
                    or remote.get("canonical_id") != remote["member_ids"][0]
                    or not set(members).issubset(remote["member_ids"])
                    or not _valid_review_refs(remote.get("record_refs"))
                    or {"id": rid, "hash": digest(original)} not in remote.get("record_refs", [])):
                raise SafetyError("RELEASE_ALIAS_READBACK_REQUIRED")
            for cid in members:
                other = tree.get("aliases", digest(cid))
                if (not isinstance(other, dict) or other.get("member_id") != cid
                        or canonical({k: v for k, v in other.items() if k != "member_id"}) != canonical({k: v for k, v in remote.items() if k != "member_id"})):
                    raise SafetyError("RELEASE_ALIAS_READBACK_REQUIRED")
            definitions = {}
            for binding in original["input"]["definition_bindings"].values():
                did = binding["definition_id"]
                while did is not None:
                    definitions[did] = proof["definitions"][did]
                    did = read_original_json(definitions[did])["previous_definition_id"]
            expected = {"contract": "functional-alias-portable-record-v1", "record_id": rid,
                        "record": wrapped, "definitions": definitions}
            key_original = f"clients/{client_id}/release-index-v2/alias-records/{rid}.json"
            if storage.read_object(key_original) != release_bytes(expected):
                raise SafetyError("RELEASE_ALIAS_READBACK_REQUIRED")
            known = {ref["id"]: ref["hash"] for ref in remote["record_refs"]}
            local = aliases[members[0]]
            remaining.update(ref["id"] for ref in local["record_refs"] if known.get(ref["id"]) != ref["hash"])
            covered = set()
            for ref in local["record_refs"]:
                edge = state["source_checks"].get(ref["id"], {})
                covered.update(edge.get("input", {}).get("member_ids", []))
            legacy.update(set(local["member_ids"]) - covered - set(remote["member_ids"]))
    return {"snapshot": confirmed, "mutation_applied": after.get("mutation_applied") is True,
            "settlement": deepcopy(after.get("settlement")), "replay": after.get("replay") is True,
            "remaining_record_ids": sorted(remaining), "remaining_legacy_member_ids": sorted(legacy),
            "component_complete": after.get("mutation_applied") is True and not remaining and not legacy}


def apply_reviewed_release_lineage(storage, state, client_id, record_ids, *, expected_etag, approval,
                                  dependent_withdrawals=None):
    """Apply exact reviewed narrowing originals, then verify actual stored proof."""
    from .functional_lineage_portable import (portable_lineage_proof, validate_lineage_proof,
        lineage_record_capsule, lineage_key)
    from .release_pages import release_bytes
    proof = portable_lineage_proof(state, record_ids)
    client_id = identifier(client_id)
    _approval(approval, {"client_id": client_id, "action": "lineage", "proof_hash": digest(proof)})
    index_key = f"clients/{client_id}/release-index.json"
    before = _snapshot(storage.read_index(index_key), client_id)
    if before["index"]["schema_version"] != 2: raise SafetyError("RELEASE_V2_REQUIRED")
    checked = validate_lineage_proof(proof, as_of=before["server_observed_at"])
    method = getattr(storage, "compare_and_swap_index_v2", None)
    if not callable(method): raise SafetyError("RELEASE_V2_STORAGE_REQUIRED")
    mutation = {"kind": "lineage", "client_id": client_id, "proof": proof, "approval": deepcopy(approval)}
    if dependent_withdrawals is not None:
        if not isinstance(dependent_withdrawals, list) or len(dependent_withdrawals) > 20:
            raise SafetyError("RELEASE_MUTATION_INVALID")
        mutation["dependent_withdrawals"] = deepcopy(dependent_withdrawals)
    after = method(index_key, expected_etag, mutation)
    if after is None: raise SafetyError("RELEASE_INDEX_CONFLICT")
    _snapshot(after, client_id)
    confirmed = _snapshot(storage.read_index(index_key), client_id)
    if confirmed["etag"] != after["etag"] or canonical(confirmed["index"]) != canonical(after["index"]):
        raise SafetyError("RELEASE_WRITE_RECONCILIATION_REQUIRED")
    applied = after.get("mutation_applied") is True
    verified = []
    if applied:
        tree = _release_tree(confirmed["index"], storage.read_object)
        for rid, original in checked["records"].items():
            row = original["row"]; parent, child = row["input"]["parent_id"], row["input"]["child_id"]
            forward = tree.get("lineage", lineage_key(parent, child))
            reverse = tree.get("lineage", lineage_key(parent, child, 1))
            if (not isinstance(forward, dict) or set(forward) != {"parent_id", "child_id", "record_refs"}
                    or forward != reverse or not _valid_review_refs(forward.get("record_refs"))
                    or forward.get("parent_id") != parent or forward.get("child_id") != child
                    or {"id": rid, "hash": original["hash"]} not in forward.get("record_refs", [])):
                raise SafetyError("RELEASE_LINEAGE_READBACK_REQUIRED")
            expected = lineage_record_capsule(proof, rid, as_of=confirmed["server_observed_at"])
            original_key = f"clients/{client_id}/release-index-v2/lineage-records/{rid}.json"
            if storage.read_object(original_key) != release_bytes(expected):
                raise SafetyError("RELEASE_LINEAGE_READBACK_REQUIRED")
            verified.append(rid)
    return {"snapshot": confirmed, "mutation_applied": applied, "selected_record_ids_verified": sorted(verified),
            "settlement": deepcopy(after.get("settlement")), "replay": after.get("replay") is True}


def activate_delivery_package(storage, issue, manifest, *, root, approval, scheduled_at, expected_etag,
                              expected_previous=None, selected_language=None, allow_synthetic=False):
    """Verify/upload immutable objects, then conditionally activate exact index.

    This function is exercised only with an injected fake storage. A real
    adapter must implement server-time stamping, actual CAS and read-back;
    merely returning this requested index/ETag is not verification.
    """
    validate_delivery_manifest(manifest, issue, root=root, selected_language=selected_language)
    key = f'clients/{manifest["client_id"]}/release-index.json'
    before = _snapshot(storage.read_index(key), manifest["client_id"])
    mutation = plan_activation(before["index"], manifest, approval, scheduled_at=scheduled_at,
                               server_at=before["server_observed_at"], expected_previous=expected_previous,
                               selected_language=selected_language, allow_synthetic=allow_synthetic,
                               read_object=storage.read_object)
    if before["etag"] != expected_etag:
        if before["index"]["schema_version"] == 2:
            try:
                receipt = release_receipt(before, issue["issue_id"], issue["revision"], read_object=storage.read_object)
            except SafetyError as error:
                if str(error) != "RELEASE_REVISION_MISSING": raise
                receipt = None
            if (receipt is not None and receipt["remote_state"] == "active" and receipt["package_hash"] == manifest["package_hash"]
                    and receipt["selection_hash"] == manifest["selection_hash"]
                    and receipt["scheduled_at"] == scheduled_at and receipt["publication_ordinal"] == manifest["publication_ordinal"]):
                return {"snapshot": before, "receipt": receipt, "published": receipt["published"],
                        "deferred_issue_ids": [], "replay": True}
        raise SafetyError("RELEASE_INDEX_CONFLICT")
    # Recheck actual remote history/other queued holds, not just draft flags.
    published, queued = set(), set()
    if before["index"]["schema_version"] == 2:
        published, queued = _v2_selected_history(before["index"], issue["products"], issue["issue_id"],
            before["server_observed_at"], storage.read_object)
    for entry in before["index"].get("issues", []):
        if entry["issue_id"] == issue["issue_id"]:
            continue  # A correction is not another logical New publication.
        available = _available(entry, _utc(before["server_observed_at"])) is not None
        held = entry["remote_state"] == "active" and entry["approved"] and entry["complete"]
        if not available and not held:
            continue
        concepts = entry.get("concept_ids")
        if (not isinstance(concepts, list) or len(concepts) != entry["product_count"]
                or any(not isinstance(c, str) for c in concepts) or len(set(concepts)) != len(concepts)):
            raise SafetyError("RELEASE_HISTORY_RECONCILIATION_REQUIRED")
        for concept in concepts:
            identifier(concept)
        if available:
            published.update(concepts)
        elif held:
            queued.update(concepts)
    validate_issue(issue["products"], published, queued)
    uploads = []
    for file in manifest["files"]:
        artifact = manifest["artifacts"]["workbook" if file["name"] == "shortlist.xlsx" else "pdf-" + file["name"][7:-4]]
        path = Path(artifact["path"])
        safe_path(path.parent)
        raw = path.read_bytes()
        if len(raw) != file["size"] or digest_bytes(raw) != file["sha256"]:
            raise SafetyError("RELEASE_LOCAL_FILE_CHANGED")
        uploads.append((file["key"], raw, file["sha256"], file["media_type"]))
    prefix = f'clients/{manifest["client_id"]}/issues/{manifest["issue_id"]}/r{manifest["revision"]}/'
    facts_path = Path(manifest["artifacts"]["facts"]["path"])
    safe_path(facts_path.parent)
    facts = facts_path.read_bytes()
    if digest_bytes(facts) != manifest["artifacts"]["facts"]["sha256"]:
        raise SafetyError("RELEASE_LOCAL_FILE_CHANGED")
    raw_manifest = canonical(manifest) + b"\n"
    uploads.extend([(prefix + "final_report_data.json", facts, digest_bytes(facts), "application/json"),
                    (prefix + "delivery-manifest.json", raw_manifest, digest_bytes(raw_manifest), "application/json")])
    for object_key, raw, sha256, media_type in uploads:
        storage.put_immutable(object_key, raw, sha256=sha256, media_type=media_type)
        checked = storage.read_object(object_key)
        if not isinstance(checked, bytes) or len(checked) != len(raw) or digest_bytes(checked) != sha256:
            raise SafetyError("RELEASE_UPLOAD_VERIFICATION_FAILED")
    write = storage.compare_and_swap_index_v2 if before["index"]["schema_version"] == 2 else storage.compare_and_swap_index
    after = write(key, expected_etag, mutation)
    if after is None:
        raise SafetyError("RELEASE_INDEX_CONFLICT")
    _snapshot(after, manifest["client_id"])
    if before["index"]["schema_version"] == 2 and after.get("mutation_applied") is not True:
        raise SafetyError("RELEASE_SETTLEMENT_CONTINUATION_REQUIRED")
    if after["etag"] == expected_etag and canonical(after["index"]) != canonical(before["index"]):
        raise SafetyError("RELEASE_WRITE_RECONCILIATION_REQUIRED")
    # Check remote read-back rather than trusting the response to a write.
    confirmed = _snapshot(storage.read_index(key), manifest["client_id"])
    if confirmed["etag"] != after["etag"] or canonical(confirmed["index"]) != canonical(after["index"]):
        raise SafetyError("RELEASE_WRITE_RECONCILIATION_REQUIRED")
    receipt = release_receipt(confirmed, issue["issue_id"], issue["revision"], read_object=storage.read_object)
    if receipt["package_hash"] != manifest["package_hash"] or receipt["remote_state"] != "active":
        raise SafetyError("RELEASE_WRITE_RECONCILIATION_REQUIRED")
    return {"snapshot": confirmed, "receipt": receipt, "published": receipt["published"],
            "deferred_issue_ids": _deferred(before["index"], confirmed["index"])}


def withdraw_release(storage, client_id, issue_id, revision, package_hash, *, approval, expected_etag):
    key = f"clients/{identifier(client_id)}/release-index.json"
    before = _snapshot(storage.read_index(key), client_id)
    mutation = plan_withdrawal(before["index"], issue_id, revision, package_hash, approval, server_at=before["server_observed_at"])
    if before["etag"] != expected_etag:
        if before["index"]["schema_version"] == 2:
            try:
                receipt = release_receipt(before, issue_id, revision, read_object=storage.read_object)
            except SafetyError as error:
                if str(error) != "RELEASE_REVISION_MISSING": raise
                receipt = None
            if receipt is not None and receipt["remote_state"] == "withdrawn" and receipt["package_hash"] == package_hash:
                return {"snapshot": before, "receipt": receipt, "published": receipt["published"],
                        "deferred_issue_ids": [], "replay": True}
        raise SafetyError("RELEASE_INDEX_CONFLICT")
    write = storage.compare_and_swap_index_v2 if before["index"]["schema_version"] == 2 else storage.compare_and_swap_index
    after = write(key, expected_etag, mutation)
    if after is None:
        raise SafetyError("RELEASE_INDEX_CONFLICT")
    if before["index"]["schema_version"] == 2 and after.get("mutation_applied") is not True:
        raise SafetyError("RELEASE_SETTLEMENT_CONTINUATION_REQUIRED")
    confirmed = _snapshot(storage.read_index(key), client_id)
    if confirmed["etag"] != after.get("etag") or canonical(confirmed["index"]) != canonical(after.get("index")):
        raise SafetyError("RELEASE_WRITE_RECONCILIATION_REQUIRED")
    receipt = release_receipt(confirmed, issue_id, revision, read_object=storage.read_object)
    if receipt["package_hash"] != package_hash or receipt["remote_state"] != "withdrawn":
        raise SafetyError("RELEASE_WRITE_RECONCILIATION_REQUIRED")
    return {"snapshot": confirmed, "receipt": receipt, "published": receipt["published"],
            "deferred_issue_ids": _deferred(before["index"], confirmed["index"])}
