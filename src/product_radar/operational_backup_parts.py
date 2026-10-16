"""Opt-in, resumable local backups with bounded immutable parts.

Each selected file's checked bytes are frozen in the existing object store as
it is read. The complete original selection and canonical heads are bound before
parts start. Each call finishes at most the supplied number of parts. Unchanged
bytes share content-addressed objects across sets; a restart adopts the exact
committed part rather than selecting today's different files. There are no
external calls, credential copies, deletions or independent-drive claims.
"""

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re
import stat
import tempfile
from time import monotonic

from .models import SafetyError, canonical, digest, identifier, timestamp, utc_now
from .operations import record_error, record_event
from .runtime_storage import runtime_path
from .storage import Journal, atomic_bytes, atomic_json, load_json, safe_path, MAX_JOURNAL
from .research_memory import ResearchMemory, TABLES, EVENT_LIMIT, load_event
from .operational_backup import _read, _essential_bytes, _relative, _file, MAX_ATTACHMENT_BYTES


VERSION = "essential-operational-record-backup-v3"
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_PAGE_ROWS = 256
MAX_PART_BYTES = 512 * 1024 * 1024
MAX_PART_FILES = 100000
MAX_FINANCE_CHUNK = 8 * 1024 * 1024
MAX_SAFE_INTEGER = 9007199254740991
_HASH = re.compile(r"[a-f0-9]{64}\Z")


@contextmanager
def _backup_phase(phase):
    """Time a finite local phase through the existing optional run recorder."""
    started = monotonic()
    record_event("phase_started", stage="backup", phase=phase)
    state = "completed"
    try:
        yield
    except BaseException as error:
        state = "failed" if isinstance(error, Exception) else "interrupted"
        record_error("backup_phase_failed" if state == "failed" else "backup_phase_interrupted",
                     error, stage="backup", phase=phase, state=state)
        raise
    finally:
        record_event("phase_finished", stage="backup", phase=phase, phase_state=state,
                     timings={phase: monotonic() - started})


def _sealed(value):
    return dict(value, hash=digest(value))


def _valid_hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _validate_seal(value, *, fields, contract):
    if (not isinstance(value, dict) or set(value) != set(fields) | {"hash"}
            or value.get("contract") != contract
            or value.get("hash") != digest({key: row for key, row in value.items() if key != "hash"})):
        raise SafetyError("BACKUP_PART_CONTROL_INVALID")
    return value


def _object_path(destination, reference, *, json=False):
    if (not isinstance(reference, dict) or set(reference) != {"sha256", "bytes"}
            or not _valid_hash(reference["sha256"]) or type(reference["bytes"]) is not int
            or not 0 <= reference["bytes"] <= (MAX_PAGE_BYTES if json else MAX_ATTACHMENT_BYTES)):
        raise SafetyError("BACKUP_PART_REFERENCE_INVALID")
    return safe_path(destination, "objects", "v3") / (reference["sha256"] + (".json" if json else ".bin"))


def _object_read(destination, reference, *, json=False):
    raw = _read(_object_path(destination, reference, json=json), MAX_PAGE_BYTES if json else MAX_ATTACHMENT_BYTES)
    if len(raw) != reference["bytes"] or sha256(raw).hexdigest() != reference["sha256"]:
        raise SafetyError("BACKUP_PART_OBJECT_CHANGED")
    if json:
        from .models import decode_json
        return decode_json(raw)
    return raw


def _object_save(destination, raw, *, json=False):
    reference = {"sha256": sha256(raw).hexdigest(), "bytes": len(raw)}
    path = _object_path(destination, reference, json=json)
    if path.exists():
        _object_read(destination, reference, json=json)
    else:
        atomic_bytes(path, raw)
        _object_read(destination, reference, json=json)
    return reference


def _save_pages(destination, rows, *, contract):
    previous, page, count = None, [], 0
    for row in rows:
        trial = {"contract": contract, "previous": previous, "rows": page + [row]}
        if page and (len(page) >= MAX_PAGE_ROWS or len(canonical(trial)) + 1 > MAX_PAGE_BYTES):
            previous = _object_save(destination, canonical({**trial, "rows": page}) + b"\n", json=True)
            page = []
        page.append(row); count += 1
    if page:
        previous = _object_save(destination, canonical({"contract": contract, "previous": previous, "rows": page}) + b"\n", json=True)
    return {"last": previous, "rows": count}


def _pages(destination, root, *, contract):
    if (not isinstance(root, dict) or set(root) != {"last", "rows"}
            or type(root["rows"]) is not int or not 0 <= root["rows"] <= MAX_SAFE_INTEGER):
        raise SafetyError("BACKUP_PART_PAGE_ROOT_INVALID")
    chain, seen, reference, count = [], set(), root["last"], 0
    while reference is not None:
        _object_path(destination, reference, json=True)
        if reference["sha256"] in seen:
            raise SafetyError("BACKUP_PART_PAGE_CYCLE")
        seen.add(reference["sha256"])
        page = _object_read(destination, reference, json=True)
        if (not isinstance(page, dict) or set(page) != {"contract", "previous", "rows"}
                or page["contract"] != contract or not isinstance(page["rows"], list)
                or not 1 <= len(page["rows"]) <= MAX_PAGE_ROWS):
            raise SafetyError("BACKUP_PART_PAGE_INVALID")
        count += len(page["rows"])
        if count > root["rows"]:
            raise SafetyError("BACKUP_PART_PAGE_COUNT_CHANGED")
        chain.append(page); reference = page["previous"]
    if count != root["rows"]:
        raise SafetyError("BACKUP_PART_PAGE_COUNT_CHANGED")
    for page in reversed(chain):
        yield from page["rows"]


def _descriptor(role, path, original, raw, *, kind=None, required=True, selection=None):
    _relative(path)
    return {"role": role, "path": path, "original_path": str(original), "bytes": len(raw),
        "sha256": sha256(raw).hexdigest(), "kind": kind, "required": required, "selection": selection}


def _validate_descriptor(row):
    if (not isinstance(row, dict) or set(row) != {"role", "path", "original_path", "bytes", "sha256", "kind", "required", "selection"}
            or row["role"] not in {"research", "finance", "attachment"}
            or not isinstance(row["original_path"], str) or not Path(row["original_path"]).is_absolute()
            or type(row["bytes"]) is not int or not 0 <= row["bytes"] <= MAX_ATTACHMENT_BYTES
            or not _valid_hash(row["sha256"]) or type(row["required"]) is not bool):
        raise SafetyError("BACKUP_PART_DESCRIPTOR_INVALID")
    _relative(row["path"])
    if row["role"] == "attachment":
        if not row["path"].startswith("attachments/") or not isinstance(row["selection"], dict):
            raise SafetyError("BACKUP_PART_DESCRIPTOR_INVALID")
    elif row["kind"] is not None or row["selection"] is not None or row["required"] is not True:
        raise SafetyError("BACKUP_PART_DESCRIPTOR_INVALID")
    return row


_PLAN_FIELDS = {"contract", "backup_id", "created_at", "project_root", "project_root_hash", "evidence_type",
    "research_checkpoint", "finance_checkpoint", "selection", "descriptors", "finance_chunks", "files", "bytes"}


def _validate_plan(value, *, root=None, backup_id=None):
    _validate_seal(value, fields=_PLAN_FIELDS, contract="backup-frozen-plan-v3")
    identifier(value["backup_id"]); timestamp(value["created_at"])
    if (not isinstance(value["project_root"], str) or not Path(value["project_root"]).is_absolute()
            or value["project_root_hash"] != digest(value["project_root"])
            or value["evidence_type"] not in {"synthetic", "captured"}
            or root is not None and value["project_root"] != str(root)
            or backup_id is not None and value["backup_id"] != backup_id
            or any(type(value[field]) is not int or not 0 <= value[field] <= MAX_SAFE_INTEGER
                   for field in ("finance_chunks", "files", "bytes"))):
        raise SafetyError("BACKUP_PART_PLAN_INVALID")
    return value


def _prepare(memory, folder, *, root, backup_id, pending_controls):
    from .operational_backup import _checkpoint
    from .operational_backup_selection import select_essential_backup_files
    destination = folder.parent
    research, finance, raw, state, finances = _checkpoint(memory, include_state=True)
    selection = select_essential_backup_files(state, finances, root=root, pending_controls=pending_controls, multipart=True,
        freeze_bytes=lambda selected_raw: _object_save(destination, selected_raw))
    if selection["research_state_hash"] != research["state_hash"] or selection["financial_state_hash"] != finance["state_hash"]:
        raise SafetyError("BACKUP_ESSENTIAL_CHECKPOINT_CONFLICT")
    descriptors = []
    for sequence in range(1, research["sequence"] + 1):
        path = memory.root / "events" / f"{sequence:08d}.json"
        descriptors.append(_descriptor("research", f"research/events/{sequence:08d}.json", path, _read(path, EVENT_LIMIT)))
    chunks = max(1, (len(raw) + MAX_FINANCE_CHUNK - 1) // MAX_FINANCE_CHUNK)
    for number in range(chunks):
        chunk = raw[number * MAX_FINANCE_CHUNK:(number + 1) * MAX_FINANCE_CHUNK]
        row = _descriptor("finance", f"finance/chunks/{number:08d}.bin", memory.journal.path, chunk)
        _object_save(destination, chunk)
        descriptors.append(row)
    for number, ref in enumerate(selection["files"]):
        descriptors.append({"role": "attachment", "path": f"attachments/{number:08d}/{Path(ref['path']).name}",
            "original_path": ref["path"], "bytes": ref["bytes"], "sha256": ref["expected_sha256"],
            "kind": ref["kind"], "required": ref["required"], "selection": ref})
    # The selected metadata is paged as well; neither the active root nor a
    # single part contains a lifetime-sized file or missing-reference list.
    selection_rows = [{"field": name, "value": item} for name in ("files", "unresolved_required", "absent_optional", "unsafe")
                      for item in selection[name]]
    metadata = {name: value for name, value in selection.items() if name not in {"files", "unresolved_required", "absent_optional", "unsafe"}}
    selection_root = {"metadata": metadata, "pages": _save_pages(destination, selection_rows, contract="backup-selection-page-v3")}
    plan = _sealed({"contract": "backup-frozen-plan-v3", "backup_id": backup_id, "created_at": utc_now(),
        "project_root": str(root), "project_root_hash": digest(str(root)), "evidence_type": memory.evidence_type,
        "research_checkpoint": research, "finance_checkpoint": finance, "selection": selection_root,
        "descriptors": _save_pages(destination, descriptors, contract="backup-descriptor-page-v3"),
        "finance_chunks": chunks, "files": len(descriptors), "bytes": sum(row["bytes"] for row in descriptors)})
    _validate_plan(plan, root=root, backup_id=backup_id)
    # No ready claim exists yet. Objects written before this atomic intent are
    # inert; after it, every continuation must use exactly this frozen plan.
    atomic_json(folder / "pending.json", plan)
    return plan


def _selection(destination, plan):
    from .operational_backup_selection import validate_essential_backup_selection
    root = plan["selection"]
    if not isinstance(root, dict) or set(root) != {"metadata", "pages"} or not isinstance(root["metadata"], dict):
        raise SafetyError("BACKUP_PART_SELECTION_INVALID")
    value = {**root["metadata"], "files": [], "unresolved_required": [], "absent_optional": [], "unsafe": []}
    for row in _pages(destination, root["pages"], contract="backup-selection-page-v3"):
        if not isinstance(row, dict) or set(row) != {"field", "value"} or row["field"] not in {"files", "unresolved_required", "absent_optional", "unsafe"}:
            raise SafetyError("BACKUP_PART_SELECTION_INVALID")
        value[row["field"]].append(row["value"])
    return validate_essential_backup_selection(value, root=Path(plan["project_root"]))


def _descriptors(destination, plan):
    rows = list(_pages(destination, plan["descriptors"], contract="backup-descriptor-page-v3"))
    for row in rows:
        _validate_descriptor(row)
    research = plan["research_checkpoint"]
    expected_prefix = [f"research/events/{number:08d}.json" for number in range(1, research["sequence"] + 1)]
    expected_prefix += [f"finance/chunks/{number:08d}.bin" for number in range(plan["finance_chunks"])]
    if (len(rows) != plan["files"] or len({row["path"] for row in rows}) != len(rows)
            or sum(row["bytes"] for row in rows) != plan["bytes"]
            or [row["path"] for row in rows[:len(expected_prefix)]] != expected_prefix
            or any(row["role"] != "research" for row in rows[:research["sequence"]])
            or any(row["role"] != "finance" for row in rows[research["sequence"]:len(expected_prefix)])
            or any(row["role"] != "attachment" for row in rows[len(expected_prefix):])):
        raise SafetyError("BACKUP_PART_SCOPE_CHANGED")
    selection = _selection(destination, plan)
    attached = rows[len(expected_prefix):]
    if ([row["selection"] for row in attached] != selection["files"]
            or any(row["original_path"] != row["selection"]["path"] or row["kind"] != row["selection"]["kind"]
                   or row["sha256"] != row["selection"]["expected_sha256"] or row["bytes"] != row["selection"]["bytes"]
                   or row["required"] != row["selection"]["required"] for row in attached)):
        raise SafetyError("BACKUP_PART_SELECTION_CHANGED")
    return rows, selection


_CURSOR_FIELDS = {"contract", "plan_hash", "next_index", "next_part", "last_part"}
_PART_FIELDS = {"contract", "plan_hash", "number", "start", "end", "previous", "outcomes"}


def _cursor(folder, plan):
    path = folder / "cursor.json"
    if not path.exists():
        return _sealed({"contract": "backup-part-cursor-v3", "plan_hash": plan["hash"], "next_index": 0,
            "next_part": 0, "last_part": None})
    value = load_json(path, 16384)
    _validate_seal(value, fields=_CURSOR_FIELDS, contract="backup-part-cursor-v3")
    if (value["plan_hash"] != plan["hash"] or any(type(value[field]) is not int or not 0 <= value[field] <= MAX_SAFE_INTEGER
            for field in ("next_index", "next_part")) or value["next_index"] > plan["files"]):
        raise SafetyError("BACKUP_PART_CURSOR_INVALID")
    return value


def _part(destination, reference, plan, rows):
    value = _object_read(destination, reference, json=True)
    _validate_seal(value, fields=_PART_FIELDS, contract="backup-part-v3")
    outcomes = list(_pages(destination, value["outcomes"], contract="backup-outcome-page-v3"))
    if (value["plan_hash"] != plan["hash"] or type(value["number"]) is not int or value["number"] < 0
            or type(value["start"]) is not int or type(value["end"]) is not int
            or not 0 <= value["start"] < value["end"] <= len(rows)
            or len(outcomes) != value["end"] - value["start"]
            or len(outcomes) > MAX_PART_FILES
            or sum(row["bytes"] for row in rows[value["start"]:value["end"]]) > MAX_PART_BYTES):
        raise SafetyError("BACKUP_PART_INVALID")
    for row, outcome in zip(rows[value["start"]:value["end"]], outcomes):
        if (not isinstance(outcome, dict) or set(outcome) != {"path", "stored", "reason"}
                or outcome["path"] != row["path"] or type(outcome["stored"]) is not bool
                or outcome["stored"] and outcome["reason"] is not None
                or not outcome["stored"] and (row["role"] != "attachment" or not isinstance(outcome["reason"], str)
                    or re.fullmatch(r"[A-Z][A-Z0-9_]{0,159}", outcome["reason"]) is None)):
            raise SafetyError("BACKUP_PART_OUTCOME_INVALID")
    # Expanded outcomes are a local reader view only. The original immutable
    # part/root binds the bounded page reference, not a lifetime-sized array.
    return dict(value, outcomes=outcomes)


def _part_chain(destination, last, plan, rows):
    chain, seen, reference = [], set(), last
    while reference is not None:
        _object_path(destination, reference, json=True)
        if reference["sha256"] in seen:
            raise SafetyError("BACKUP_PART_CYCLE")
        seen.add(reference["sha256"])
        part = _part(destination, reference, plan, rows)
        chain.append(part); reference = part["previous"]
    count = 0
    for number, part in enumerate(reversed(chain)):
        if part["number"] != number or part["start"] != count:
            raise SafetyError("BACKUP_PART_PREFIX_CHANGED")
        count = part["end"]
        yield part


def _existing_object(path, reference):
    """A stored file saved by an earlier copy, checked by kind and size only.

    Its name is the hash of its content, and the final verification of every
    copy reads and hashes every stored file before that copy is published.
    Reading each one in full here as well read the whole history twice a day
    on the slow project drive and added no protection.
    """
    try:
        status = path.lstat()
    except OSError:
        raise SafetyError("BACKUP_FILE_UNAVAILABLE") from None
    if stat.S_ISLNK(status.st_mode):
        raise SafetyError("SYMLINK_PATH_REFUSED")
    if not stat.S_ISREG(status.st_mode) or status.st_size != reference["bytes"]:
        raise SafetyError("BACKUP_PART_OBJECT_CHANGED")


def _copy_outcome(destination, row):
    reference = {"sha256": row["sha256"], "bytes": row["bytes"]}
    try:
        path = _object_path(destination, reference)
        if path.exists() or path.is_symlink():
            _existing_object(path, reference)
        elif row["role"] == "finance":
            raise SafetyError("BACKUP_FINANCIAL_CHUNK_MISSING")
        else:
            raw = _read(Path(row["original_path"]), MAX_ATTACHMENT_BYTES)
            if len(raw) != row["bytes"] or sha256(raw).hexdigest() != row["sha256"]:
                raise SafetyError("BACKUP_ATTACHMENT_SOURCE_HASH_MISMATCH")
            if row["selection"] is not None:
                _essential_bytes(raw, row["selection"])
            _object_save(destination, raw)
        return {"path": row["path"], "stored": True, "reason": None}
    except SafetyError as error:
        if row["role"] != "attachment":
            raise
        return {"path": row["path"], "stored": False, "reason": str(error)}


def _result(folder, plan, *, manifest=None, replayed=False):
    value = manifest or {}
    return {"folder": str(folder), "manifest": deepcopy(manifest), "state": value.get("coverage_state", "in_progress"),
        "files": value.get("files", plan["files"]), "bytes": value.get("bytes", plan["bytes"]),
        "essential_files_complete": value.get("essential_files_complete", False), "replayed": replayed,
        "operational_recovery_proved": False, "independent_backup_proved": False, "provider_calls": 0, "model_calls": 0}


def create_multipart_operational_backup(memory, destination, *, backup_id, root, pending_controls=(), max_parts=1):
    identifier(backup_id); root = safe_path(root); destination = safe_path(destination)
    if type(max_parts) is not int or not 1 <= max_parts <= 16:
        raise SafetyError("BACKUP_PART_WORK_BOUND_INVALID")
    if (Path(memory.root).absolute() != runtime_path(root, "data/research/captured")
            or Path(memory.journal.root).absolute() != runtime_path(root, "data/benchmarks/state/apify")
            or any(destination == source or source in destination.parents for source in (memory.root, memory.journal.root))):
        raise SafetyError("BACKUP_ESSENTIAL_PROJECT_BINDING_INVALID")
    folder = safe_path(destination, backup_id)
    with Journal(destination / "multipart-control").locked():
        ready = folder / "manifest.json"
        if ready.exists():
            manifest = verify_multipart_backup(folder)
            if manifest["backup_id"] != backup_id or manifest["project_root"] != str(root):
                raise SafetyError("BACKUP_ID_SCOPE_CONFLICT")
            plan = _validate_plan(load_json(folder / "pending.json", 65536), root=root, backup_id=backup_id)
            return _result(folder, plan, manifest=manifest, replayed=True)
        pending = folder / "pending.json"
        with _backup_phase("backup_preparation"):
            plan = (_validate_plan(load_json(pending, 65536), root=root, backup_id=backup_id) if pending.exists()
                    else _prepare(memory, folder, root=root, backup_id=backup_id, pending_controls=pending_controls))
            rows, selection = _descriptors(destination, plan)
            cursor = _cursor(folder, plan)
            prior = list(_part_chain(destination, cursor["last_part"], plan, rows))
            if len(prior) != cursor["next_part"] or (prior[-1]["end"] if prior else 0) != cursor["next_index"]:
                raise SafetyError("BACKUP_PART_CURSOR_PREFIX_CHANGED")
        with _backup_phase("backup_copy"):
            for _ in range(max_parts):
                start = cursor["next_index"]
                if start == len(rows):
                    break
                part_path = folder / "parts" / f"{cursor['next_part']:08d}.json"
                if part_path.exists():
                    reference = load_json(part_path, 4096)
                    part = _part(destination, reference, plan, rows)
                    if (part["start"] != start or part["number"] != cursor["next_part"] or part["previous"] != cursor["last_part"]):
                        raise SafetyError("BACKUP_PART_ORPHAN_CONFLICT")
                else:
                    end, size = start, 0
                    while end < len(rows) and end - start < MAX_PART_FILES:
                        if size + rows[end]["bytes"] > MAX_PART_BYTES:
                            break
                        size += rows[end]["bytes"]; end += 1
                    if end == start:
                        raise SafetyError("BACKUP_PART_FILE_TOO_LARGE")
                    outcomes = [_copy_outcome(destination, row) for row in rows[start:end]]
                    part = _sealed({"contract": "backup-part-v3", "plan_hash": plan["hash"], "number": cursor["next_part"],
                        "start": start, "end": end, "previous": cursor["last_part"],
                        "outcomes": _save_pages(destination, outcomes, contract="backup-outcome-page-v3")})
                    reference = _object_save(destination, canonical(part) + b"\n", json=True)
                    part = _part(destination, reference, plan, rows)
                    atomic_json(part_path, reference)
                cursor = _sealed({"contract": "backup-part-cursor-v3", "plan_hash": plan["hash"], "next_index": part["end"],
                    "next_part": cursor["next_part"] + 1, "last_part": reference})
                atomic_json(folder / "cursor.json", cursor)
        if cursor["next_index"] != len(rows):
            return _result(folder, plan)
        with _backup_phase("backup_final_verification"):
            chain = list(_part_chain(destination, cursor["last_part"], plan, rows))
            outcomes = [row for part in chain for row in part["outcomes"]]
            complete = not (selection["unresolved_required"] or selection["unsafe"] or any(not row["stored"] for row in outcomes))
            manifest = _sealed({"contract": VERSION, "kind": VERSION, "schema_version": 3, "backup_id": backup_id,
                "created_at": plan["created_at"], "completed_at": utc_now(), "project_root": str(root),
                "plan_hash": plan["hash"], "last_part": cursor["last_part"], "parts": len(chain), "files": len(rows),
                "bytes": sum(row["bytes"] for row, outcome in zip(rows, outcomes) if outcome["stored"]),
                "copy_failures": sum(not row["stored"] for row in outcomes), "essential_files_complete": complete,
                "coverage_state": "essential_files_verified" if complete else "records_verified_with_limitations",
                "independent_backup_proved": False, "operational_recovery_proved": False})
            # Read and canonically replay every original record before
            # publishing the ready root. Include that final local save in this
            # phase so a failed publication retains its failure location too.
            _verify(folder, manifest)
            atomic_json(ready, manifest)
        return _result(folder, plan, manifest=manifest)


_MANIFEST_FIELDS = {"contract", "kind", "schema_version", "backup_id", "created_at", "completed_at", "project_root",
    "plan_hash", "last_part", "parts", "files", "bytes", "copy_failures", "essential_files_complete", "coverage_state",
    "independent_backup_proved", "operational_recovery_proved"}


def _verify(folder, manifest, *, restored=None):
    folder = safe_path(folder); destination = folder.parent
    _validate_seal(manifest, fields=_MANIFEST_FIELDS, contract=VERSION)
    if (manifest["kind"] != VERSION or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 3
            or manifest["independent_backup_proved"] is not False or manifest["operational_recovery_proved"] is not False
            or type(manifest["essential_files_complete"]) is not bool):
        raise SafetyError("BACKUP_PART_MANIFEST_INVALID")
    timestamp(manifest["created_at"]); timestamp(manifest["completed_at"])
    plan = _validate_plan(load_json(folder / "pending.json", 65536), backup_id=manifest["backup_id"])
    if manifest["plan_hash"] != plan["hash"] or manifest["project_root"] != plan["project_root"] or manifest["created_at"] != plan["created_at"]:
        raise SafetyError("BACKUP_PART_MANIFEST_PLAN_CHANGED")
    rows, selection = _descriptors(destination, plan)
    chain = list(_part_chain(destination, manifest["last_part"], plan, rows))
    outcomes = [row for part in chain for row in part["outcomes"]]
    if (not chain or chain[-1]["end"] != len(rows) or len(chain) != manifest["parts"] or len(rows) != manifest["files"]
            or manifest["copy_failures"] != sum(not row["stored"] for row in outcomes)
            or manifest["bytes"] != sum(row["bytes"] for row, outcome in zip(rows, outcomes) if outcome["stored"])):
        raise SafetyError("BACKUP_PART_MANIFEST_COUNTS_CHANGED")
    complete = not (selection["unresolved_required"] or selection["unsafe"] or any(not row["stored"] for row in outcomes))
    if (manifest["essential_files_complete"] != complete or manifest["coverage_state"] !=
            ("essential_files_verified" if complete else "records_verified_with_limitations")):
        raise SafetyError("BACKUP_PART_READINESS_INVALID")
    state, head, ids, count = {table: {} for table in TABLES}, "0" * 64, set(), 0
    with tempfile.TemporaryDirectory(prefix="neuraflow-parts-verify-") as temporary:
        checking = Path(temporary)
        journal = Journal(checking / "finance")
        journal.path.parent.mkdir(parents=True, exist_ok=True)
        with journal.path.open("wb") as financial:
            for row, outcome in zip(rows, outcomes):
                if not outcome["stored"]:
                    continue
                raw = _object_read(destination, {"sha256": row["sha256"], "bytes": row["bytes"]})
                if row["role"] == "research":
                    path = checking / "event.json"; atomic_bytes(path, raw)
                    event = load_event(path, EVENT_LIMIT); count += 1
                    if (event["seq"] != count or event["previous"] != head or event["id"] in ids
                            or event["evidence_type"] != plan["evidence_type"]
                            or event["hash"] != digest({key: value for key, value in event.items() if key != "hash"})):
                        raise SafetyError("BACKUP_RESEARCH_CHAIN_INVALID")
                    identifier(event["id"]); timestamp(event["at"])
                    ResearchMemory._apply(state, event["changes"]); head = event["hash"]; ids.add(event["id"])
                elif row["role"] == "finance":
                    financial.write(raw)
                else:
                    _essential_bytes(raw, row["selection"])
                if restored is not None and row["role"] != "finance":
                    atomic_bytes(_file(restored, row["path"]), raw)
        research = plan["research_checkpoint"]
        if (count != research["sequence"] or head != research["head"] or digest(state) != research["state_hash"]
                or {table: len(state[table]) for table in TABLES} != research["table_counts"]):
            raise SafetyError("BACKUP_RESEARCH_CHECKPOINT_MISMATCH")
        with journal.locked():
            # The same segmented Journal reader verifies large legacy exports
            # without increasing the old eight-MiB loader's lifetime limit.
            journal.migrate_segments()
            events, finances = journal.events(), journal.state()
            finance = plan["finance_checkpoint"]
            if (len(events) != finance["events"] or (events[-1]["hash"] if events else "0" * 64) != finance["head"]
                    or digest(finances) != finance["state_hash"]):
                raise SafetyError("BACKUP_FINANCIAL_CHECKPOINT_MISMATCH")
            if restored is not None:
                restored_journal = Journal(safe_path(restored, "finance"))
                atomic_bytes(restored_journal.path, journal.export_bytes())
                if restored_journal.path.stat().st_size > MAX_JOURNAL:
                    with restored_journal.locked():
                        restored_journal.migrate_segments()
    return plan, rows, outcomes


def verify_multipart_backup(folder):
    with _backup_phase("backup_saved_verification"):
        manifest = load_json(safe_path(folder) / "manifest.json", 65536)
        _verify(folder, manifest)
        return deepcopy(manifest)


def restore_multipart_backup(folder, destination):
    folder = safe_path(folder); destination = safe_path(destination)
    # The guard is outside the empty target. Two distinct saved sets cannot
    # both pass admission and interleave attachments into one restore folder.
    guard = safe_path(destination.parent / ("restore-guard-" + digest(str(destination))[:32]))
    with Journal(guard).locked():
        return _restore_multipart_locked(folder, destination)


def _restore_multipart_locked(folder, destination):
    if (destination == folder or folder in destination.parents or destination in folder.parents
            or destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        raise SafetyError("RESTORE_DESTINATION_NOT_EMPTY")
    manifest = verify_multipart_backup(folder)
    destination.mkdir(parents=True, exist_ok=True)
    with _backup_phase("backup_restore_verification"):
        plan, rows, outcomes = _verify(folder, manifest, restored=destination)
        restored = ResearchMemory(destination / "research", Journal(destination / "finance"), evidence_type=plan["evidence_type"])
        state, head, sequence, _ = restored._state(readonly=True)
        with restored.journal.locked():
            financial = restored.journal.state()
        if (head != plan["research_checkpoint"]["head"] or sequence != plan["research_checkpoint"]["sequence"]
                or digest(state) != plan["research_checkpoint"]["state_hash"] or digest(financial) != plan["finance_checkpoint"]["state_hash"]):
            raise SafetyError("RESTORE_CHECKPOINT_MISMATCH")
        receipt = {"schema_version": 3, "kind": "operational_restore_proof", "backup_kind": VERSION,
            "backup_id": manifest["backup_id"], "backup_hash": manifest["hash"], "verified_at": utc_now(),
            "evidence_type": plan["evidence_type"], "research_events": sequence, "financial_events": plan["finance_checkpoint"]["events"],
            "research_state_hash": digest(state), "financial_state_hash": digest(financial),
            "files_verified": manifest["files"] - manifest["copy_failures"], "bytes_verified": manifest["bytes"],
            "essential_files_complete": manifest["essential_files_complete"], "coverage_state": manifest["coverage_state"],
            "attachment_mapping": [{"original_path": row["original_path"], "restored_path": str(_file(destination, row["path"])),
                "kind": row["kind"]} for row, outcome in zip(rows, outcomes) if row["role"] == "attachment" and outcome["stored"]],
            "independent_backup_proved": False, "operational_recovery_proved": False, "provider_calls": 0, "model_calls": 0,
            "not_verified": ["durable_independent_destination", "automatic_reference_path_relocation", "automatic_application_resumption", "credential_reconnection"]}
        atomic_json(destination / "restore-proof.json", receipt)
        return receipt
