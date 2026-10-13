"""Exact local backup and isolated restore, without external execution.

Canonical research events and the financial journal are preserved byte for
byte. Only explicitly selected evidence/report files are included; indexes,
private API settings and browser profiles are not automatically copied. The
essential v2 scope adds only reviewed public settings and application files.
Research and finance checkpoints have separate dates: this is not a cross-store
atomic snapshot or proof that a permanent independent destination exists.
"""

from copy import deepcopy
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

from .models import SafetyError, canonical, decode_json, digest, identifier, timestamp, utc_now
from .research_memory import EVENT_LIMIT, ResearchMemory, TABLES, load_event
from .runtime_storage import runtime_path
from .storage import Journal, MAX_JOURNAL, atomic_bytes, atomic_json, load_json, safe_path


BACKUP_VERSION = "operational-record-backup-v1"
ESSENTIAL_BACKUP_VERSION = "essential-operational-record-backup-v2"
MAX_BACKUP_FILES = 100000
MAX_BACKUP_BYTES = 512 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
ATTACHMENT_KINDS = {"source_capture", "model_receipt", "report_package", "public_image"}
ATTACHMENT_EXTENSIONS = {".json", ".jsonl", ".csv", ".html", ".txt", ".pdf", ".xlsx", ".png", ".jpg", ".jpeg", ".webp"}
_PRIVATE_COMPONENTS = {"config", ".git", ".codex", ".agents", ".aws", "profiles", "browser-profile", "native-profile"}
_PRIVATE_FILENAMES = {"auth.json", "credentials.json", "tokens.json", "apify_accounts.json", "providers.env", "quickcommerce.env"}
_SELECTION_FIELDS = {"schema_version", "kind", "files", "unresolved_required", "absent_optional", "unsafe",
                     "limitations", "selected_bytes", "selection_fingerprint", "research_state_hash", "financial_state_hash"}
_ESSENTIAL_FIELDS = _SELECTION_FIELDS | {"project_root", "project_root_hash", "copy_failures", "coverage_state",
                                       "essential_files_complete", "operational_recovery_proved"}


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise SafetyError("BACKUP_FILE_PATH_INVALID")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise SafetyError("BACKUP_FILE_PATH_INVALID")
    if path.parts[0] not in ("research", "finance", "attachments"):
        raise SafetyError("BACKUP_FILE_PATH_INVALID")
    return path


def _file(root, relative):
    path = Path(root).joinpath(*_relative(relative).parts)
    safe_path(path.parent)
    if path.is_symlink():
        raise SafetyError("SYMLINK_PATH_REFUSED")
    return path


def _read(path, limit, *, checked_parent=False):
    path = Path(path).absolute()
    if not checked_parent:
        safe_path(path.parent)
    descriptor = None
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise SafetyError("SYMLINK_PATH_REFUSED")
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise SafetyError("BACKUP_FILE_SIZE_LIMIT")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SafetyError("SYMLINK_PATH_REFUSED")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(limit + 1)
            ended = os.fstat(stream.fileno())
        after = path.lstat()
    except OSError:
        raise SafetyError("BACKUP_FILE_UNAVAILABLE") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if len(raw) > limit or any(len({getattr(row, field) for row in (before, opened, ended, after)}) != 1 for field in fields):
        raise SafetyError("BACKUP_SOURCE_CHANGED")
    return raw


def _payload_paths(folder, manifest):
    """Check the small owned directory set once, not every full ancestry.

    Final files remain descriptor-checked and hash-verified. This is an owned
    local backup, not a defence against a malicious OS administrator.
    """
    folder = safe_path(folder)
    paths = {row["path"]: folder.joinpath(*_relative(row["path"]).parts) for row in manifest["files"]}
    directories = {parent for path in paths.values() for parent in path.parents if parent == folder or folder in parent.parents}
    for directory in sorted(directories, key=lambda path: (len(path.parts), str(path))):
        try:
            mode = directory.lstat().st_mode
        except OSError:
            raise SafetyError("BACKUP_FILE_UNAVAILABLE") from None
        if stat.S_ISLNK(mode):
            raise SafetyError("SYMLINK_PATH_REFUSED")
        if not stat.S_ISDIR(mode):
            raise SafetyError("BACKUP_FILE_UNAVAILABLE")
    return paths


def _attachment(value):
    if (not isinstance(value, dict) or set(value) != {"path", "kind"}
            or not isinstance(value["kind"], str) or value["kind"] not in ATTACHMENT_KINDS
            or not isinstance(value["path"], (str, Path))):
        raise SafetyError("BACKUP_ATTACHMENT_INVALID")
    path = Path(value["path"]).absolute()
    lowered = {part.casefold() for part in path.parts}
    if (lowered & _PRIVATE_COMPONENTS or path.name.casefold() in _PRIVATE_FILENAMES
            or path.suffix.casefold() not in ATTACHMENT_EXTENSIONS or path.name.startswith(".")):
        raise SafetyError("BACKUP_PRIVATE_FILE_REFUSED")
    safe_path(path.parent)
    if path.is_symlink():
        raise SafetyError("SYMLINK_PATH_REFUSED")
    return path, value["kind"]


def _checkpoint(memory, *, include_state=False):
    # Atomic immutable events provide a verified research prefix without
    # holding the financial writer lock over a cold historical replay.
    state, head, sequence, _ = memory._state(readonly=True)
    research = {"sequence": sequence, "head": head, "state_hash": digest(state),
                "table_counts": {table: len(state[table]) for table in TABLES}, "captured_at": utc_now()}
    with memory.journal.locked(timeout=5):
        events = memory.journal.events()
        finances = memory.journal.state()
        financial_raw = memory.journal.export_bytes()
    finance = {"events": len(events), "head": events[-1]["hash"] if events else "0" * 64,
               "state_hash": digest(finances), "captured_at": utc_now()}
    if include_state:
        return research, finance, financial_raw, state, finances
    return research, finance, financial_raw


def create_operational_backup(memory, destination, *, backup_id, attachments=()):
    """Create one immutable checkpoint; never overwrite an earlier backup.

    Reusing the same ID returns its original verified checkpoint, not today's
    changing state. Newer state therefore needs a new explicit backup ID.
    """
    identifier(backup_id)
    destination = safe_path(destination)
    for source in (memory.root, memory.journal.root):
        if destination == source or source in destination.parents:
            raise SafetyError("BACKUP_DESTINATION_INSIDE_SOURCE")
    folder = safe_path(destination, backup_id)
    if folder.exists():
        manifest = verify_operational_backup(folder)
        if manifest["kind"] != BACKUP_VERSION or manifest["backup_id"] != backup_id:
            raise SafetyError("BACKUP_ID_SCOPE_CONFLICT")
        if attachments:
            selected = {_attachment(value) for value in attachments}
            original = {(Path(value["original_path"]), value["kind"]) for value in manifest["attachments"]}
            if selected != original:
                raise SafetyError("BACKUP_ID_SCOPE_CONFLICT")
        return {"folder": str(folder), "manifest": manifest, "replayed": True}
    if not isinstance(attachments, (tuple, list)):
        raise SafetyError("BACKUP_ATTACHMENT_INVALID")
    selected = [_attachment(value) for value in attachments]
    if len({str(path) for path, _ in selected}) != len(selected):
        raise SafetyError("BACKUP_ATTACHMENT_DUPLICATED")
    research, finance, financial_raw = _checkpoint(memory)
    return _write_operational_backup(memory, folder, backup_id=backup_id, selected=selected,
        research=research, finance=finance, financial_raw=financial_raw)


def _essential_result(folder, manifest, *, replayed):
    selection = manifest["essential_selection"]
    return {"folder": str(folder), "manifest": manifest, "replayed": replayed,
            "state": selection["coverage_state"], "essential_files_complete": selection["essential_files_complete"],
            "operational_recovery_proved": False, "independent_backup_proved": False,
            "provider_calls": 0, "model_calls": 0}


def create_essential_operational_backup(memory, destination, *, backup_id, root, pending_controls=()):
    """Copy a bounded essential scope selected from the exact saved prefix.

    The v2 scope is a local second copy, not automatic relocated recovery or
    an independent drive. A repeated ID verifies and returns its original
    manifest without selecting from today's different research state.
    """
    from .operational_backup_selection import select_essential_backup_files, validate_essential_backup_selection
    identifier(backup_id); root = safe_path(root); destination = safe_path(destination)
    if (Path(memory.root).absolute() != runtime_path(root, "data/research/captured")
            or Path(memory.journal.root).absolute() != runtime_path(root, "data/benchmarks/state/apify")):
        raise SafetyError("BACKUP_ESSENTIAL_PROJECT_BINDING_INVALID")
    for source in (memory.root, memory.journal.root):
        source = Path(source).absolute()
        if destination == source or source in destination.parents:
            raise SafetyError("BACKUP_DESTINATION_INSIDE_SOURCE")
    folder = safe_path(destination, backup_id)
    if folder.exists():
        manifest = verify_operational_backup(folder)
        if (manifest["kind"] != ESSENTIAL_BACKUP_VERSION or manifest["backup_id"] != backup_id
                or manifest["essential_selection"]["project_root"] != str(root)):
            raise SafetyError("BACKUP_ID_SCOPE_CONFLICT")
        return _essential_result(folder, manifest, replayed=True)
    research, finance, financial_raw, state, finances = _checkpoint(memory, include_state=True)
    selection = select_essential_backup_files(state, finances, root=root, pending_controls=pending_controls)
    validate_essential_backup_selection(selection, root=root)
    if (selection["research_state_hash"] != research["state_hash"]
            or selection["financial_state_hash"] != finance["state_hash"]):
        raise SafetyError("BACKUP_ESSENTIAL_CHECKPOINT_CONFLICT")
    essential = {**deepcopy(selection), "project_root": str(root), "project_root_hash": digest(str(root)),
                 "copy_failures": [], "coverage_state": "records_verified_with_limitations",
                 "essential_files_complete": False, "operational_recovery_proved": False}
    selected = [(Path(row["path"]), row["kind"]) for row in selection["files"]]
    return _write_operational_backup(memory, folder, backup_id=backup_id, selected=selected,
        research=research, finance=finance, financial_raw=financial_raw, essential=essential)


def _essential_bytes(raw, reference):
    if len(raw) != reference["bytes"] or _hash(raw) != reference["expected_sha256"]:
        raise SafetyError("BACKUP_ATTACHMENT_SOURCE_HASH_MISMATCH")
    if reference["expected_json_digest"] is not None:
        try:
            actual = digest(decode_json(raw))
        except (SafetyError, TypeError, ValueError, RecursionError):
            raise SafetyError("BACKUP_ATTACHMENT_JSON_DIGEST_MISMATCH") from None
        if actual != reference["expected_json_digest"]:
            raise SafetyError("BACKUP_ATTACHMENT_JSON_DIGEST_MISMATCH")


def _write_operational_backup(memory, folder, *, backup_id, selected, research, finance, financial_raw, essential=None):
    """Shared exact-byte copier; selection and checkpoints are already frozen."""
    if research["sequence"] + len(selected) + 1 > MAX_BACKUP_FILES:
        raise SafetyError("BACKUP_FILE_COUNT_LIMIT")
    folder.mkdir(parents=True, exist_ok=False)
    files, total = [], 0

    def save(relative, raw, original):
        nonlocal total
        total += len(raw)
        if total > MAX_BACKUP_BYTES:
            raise SafetyError("BACKUP_TOTAL_SIZE_LIMIT")
        atomic_bytes(_file(folder, relative), raw)
        files.append({"path": relative, "bytes": len(raw), "sha256": _hash(raw), "original_path": str(original)})

    safe_path(memory.root / "events")
    for sequence in range(1, research["sequence"] + 1):
        relative = f"research/events/{sequence:08d}.json"
        original = memory.root / "events" / f"{sequence:08d}.json"
        save(relative, _read(original, EVENT_LIMIT, checked_parent=True), original)
    save("finance/events.jsonl", financial_raw, memory.journal.path)
    attached = []
    references = {row["path"]: row for row in essential["files"]} if essential is not None else {}
    for number, (path, kind) in enumerate(selected):
        try:
            raw = _read(path, MAX_ATTACHMENT_BYTES)
            if essential is not None:
                _essential_bytes(raw, references[str(path)])
        except SafetyError as error:
            if essential is None:
                raise
            reference = references[str(path)]
            essential["copy_failures"].append({"original_path": str(path), "kind": kind,
                "required": reference["required"], "reason": str(error)})
            continue
        relative = f"attachments/{number:06d}/{path.name}"
        save(relative, raw, path)
        attached.append({"original_path": str(path), "path": relative, "kind": kind})
    manifest = {"schema_version": 1, "kind": BACKUP_VERSION, "backup_id": backup_id,
                "created_at": utc_now(), "evidence_type": memory.evidence_type,
                "research_checkpoint": research, "finance_checkpoint": finance,
                "files": sorted(files, key=lambda row: row["path"]), "attachments": attached,
                "independent_backup_proved": False,
                "limitations": ["A durable independent backup destination is not configured by this operation.",
                                "Research and financial checkpoints have separate original dates.",
                                "Only explicitly selected extra files are included; original reference strings are not rewritten."]}
    if essential is not None:
        complete = not (essential["unresolved_required"] or essential["unsafe"] or essential["copy_failures"])
        essential.update(essential_files_complete=complete,
            coverage_state="essential_files_verified" if complete else "records_verified_with_limitations")
        manifest.update(schema_version=2, kind=ESSENTIAL_BACKUP_VERSION, essential_selection=essential)
        manifest["limitations"] = ["This is a local second copy, not proof of an independent device or cloud backup.",
            "Research and financial checkpoints have separate original dates.",
            "Original reference strings are preserved; automatic relocated application resumption is not proved.",
            "Passwords, API keys, browser profiles and installed dependencies are excluded; credentials need reconnection."]
        manifest["limitations"].extend(essential["limitations"])
        if not complete:
            manifest["limitations"].append("Required or unsafe references or copy failures remain; operational recovery is not complete.")
    manifest["hash"] = digest(manifest)
    if essential is not None and len(canonical(manifest)) + 1 > MAX_MANIFEST_BYTES:
        raise SafetyError("BACKUP_MANIFEST_SIZE_LIMIT")
    # Verify the copied chain and attachment bytes before publishing readiness.
    _verify_manifest_payload(folder, manifest)
    atomic_json(folder / "manifest.json", manifest)
    if essential is not None:
        return _essential_result(folder, manifest, replayed=False)
    return {"folder": str(folder), "manifest": manifest, "replayed": False}


def _validate_manifest(manifest):
    essential = isinstance(manifest, dict) and manifest.get("kind") == ESSENTIAL_BACKUP_VERSION
    fields = {"schema_version", "kind", "backup_id", "created_at", "evidence_type", "research_checkpoint",
              "finance_checkpoint", "files", "attachments", "independent_backup_proved", "limitations", "hash"}
    if essential:
        fields.add("essential_selection")
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest["kind"] != (ESSENTIAL_BACKUP_VERSION if essential else BACKUP_VERSION)
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != (2 if essential else 1)
            or manifest["evidence_type"] not in ("captured", "synthetic") or manifest["independent_backup_proved"] is not False
            or manifest["hash"] != digest({key: value for key, value in manifest.items() if key != "hash"})):
        raise SafetyError("BACKUP_MANIFEST_INVALID")
    identifier(manifest["backup_id"]); timestamp(manifest["created_at"])
    if (not isinstance(manifest["files"], list) or not 1 <= len(manifest["files"]) <= MAX_BACKUP_FILES
            or not isinstance(manifest["attachments"], list) or not isinstance(manifest["limitations"], list)):
        raise SafetyError("BACKUP_MANIFEST_INVALID")
    paths, total = set(), 0
    for row in manifest["files"]:
        if (not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256", "original_path"}
                or type(row["bytes"]) is not int or not 0 <= row["bytes"] <= MAX_ATTACHMENT_BYTES
                or not isinstance(row["original_path"], str) or not isinstance(row["sha256"], str)
                or len(row["sha256"]) != 64 or any(character not in "0123456789abcdef" for character in row["sha256"])):
            raise SafetyError("BACKUP_MANIFEST_INVALID")
        _relative(row["path"])
        if row["path"] in paths:
            raise SafetyError("BACKUP_FILE_DUPLICATED")
        paths.add(row["path"]); total += row["bytes"]
    if total > MAX_BACKUP_BYTES:
        raise SafetyError("BACKUP_TOTAL_SIZE_LIMIT")
    research, finance = manifest["research_checkpoint"], manifest["finance_checkpoint"]
    if (not isinstance(research, dict) or set(research) != {"sequence", "head", "state_hash", "table_counts", "captured_at"}
            or type(research["sequence"]) is not int or not 0 <= research["sequence"] < MAX_BACKUP_FILES
            or not isinstance(research["table_counts"], dict) or set(research["table_counts"]) != set(TABLES)
            or any(type(value) is not int or value < 0 for value in research["table_counts"].values())
            or not isinstance(finance, dict) or set(finance) != {"events", "head", "state_hash", "captured_at"}
            or type(finance["events"]) is not int or finance["events"] < 0):
        raise SafetyError("BACKUP_CHECKPOINT_INVALID")
    timestamp(research["captured_at"]); timestamp(finance["captured_at"])
    expected = {f"research/events/{number:08d}.json" for number in range(1, research["sequence"] + 1)} | {"finance/events.jsonl"}
    originals = set()
    attachment_kinds = ATTACHMENT_KINDS
    if essential:
        from .operational_backup_selection import ESSENTIAL_FILE_KINDS
        attachment_kinds = ATTACHMENT_KINDS | set(ESSENTIAL_FILE_KINDS)
    for attached in manifest["attachments"]:
        if (not isinstance(attached, dict) or set(attached) != {"original_path", "path", "kind"}
                or not isinstance(attached["kind"], str) or attached["kind"] not in attachment_kinds
                or not isinstance(attached["original_path"], str) or not isinstance(attached["path"], str)
                or not attached["path"].startswith("attachments/") or attached["original_path"] in originals
                or attached["path"] in expected):
            raise SafetyError("BACKUP_ATTACHMENT_INVALID")
        expected.add(attached["path"]); originals.add(attached["original_path"])
    if paths != expected:
        raise SafetyError("BACKUP_SCOPE_INVALID")
    file_originals = {row["path"]: row["original_path"] for row in manifest["files"]}
    if any(file_originals[value["path"]] != value["original_path"] for value in manifest["attachments"]):
        raise SafetyError("BACKUP_ATTACHMENT_INVALID")
    if essential:
        _validate_essential_manifest(manifest)
    return manifest


def _validate_essential_manifest(manifest):
    """Validate v2 scope from manifest metadata, never current source files."""
    from .operational_backup_selection import validate_essential_backup_selection
    value = manifest["essential_selection"]
    if (not isinstance(value, dict) or set(value) != _ESSENTIAL_FIELDS
            or not isinstance(value["project_root"], str) or not Path(value["project_root"]).is_absolute()
            or value["project_root_hash"] != digest(value["project_root"])
            or not isinstance(value["copy_failures"], list) or type(value["essential_files_complete"]) is not bool
            or value["operational_recovery_proved"] is not False):
        raise SafetyError("BACKUP_ESSENTIAL_SELECTION_INVALID")
    original = {name: value[name] for name in _SELECTION_FIELDS}
    validate_essential_backup_selection(original, root=Path(value["project_root"]))
    if (value["research_state_hash"] != manifest["research_checkpoint"]["state_hash"]
            or value["financial_state_hash"] != manifest["finance_checkpoint"]["state_hash"]):
        raise SafetyError("BACKUP_ESSENTIAL_CHECKPOINT_CONFLICT")
    root = Path(value["project_root"])
    original_paths = {row["path"]: row["original_path"] for row in manifest["files"]}
    # Lexical: validating a saved copy must not require the original store.
    if (original_paths["finance/events.jsonl"] != str(runtime_path(root, "data/benchmarks/state/apify/events.jsonl", lexical=True))
            or any(original_paths[f"research/events/{number:08d}.json"] !=
                   str(runtime_path(root, "data/research/captured/events", lexical=True) / f"{number:08d}.json")
                   for number in range(1, manifest["research_checkpoint"]["sequence"] + 1))):
        raise SafetyError("BACKUP_ESSENTIAL_PROJECT_BINDING_INVALID")
    selected = {row["path"]: row for row in value["files"]}
    failed = set()
    for row in value["copy_failures"]:
        if (not isinstance(row, dict) or set(row) != {"original_path", "kind", "required", "reason"}
                or not isinstance(row["original_path"], str) or row["original_path"] not in selected
                or row["original_path"] in failed or type(row["required"]) is not bool
                or row["kind"] != selected[row["original_path"]]["kind"]
                or row["required"] != selected[row["original_path"]]["required"]
                or not isinstance(row["reason"], str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,159}", row["reason"]) is None):
            raise SafetyError("BACKUP_ESSENTIAL_COPY_FAILURE_INVALID")
        failed.add(row["original_path"])
    attached = {row["original_path"]: row for row in manifest["attachments"]}
    if set(attached) != set(selected) - failed:
        raise SafetyError("BACKUP_ESSENTIAL_SCOPE_INVALID")
    files = {row["path"]: row for row in manifest["files"]}
    for path, row in attached.items():
        reference = selected[path]
        copied = files[row["path"]]
        if (row["kind"] != reference["kind"] or copied["bytes"] != reference["bytes"]
                or copied["sha256"] != reference["expected_sha256"]):
            raise SafetyError("BACKUP_ESSENTIAL_FILE_BINDING_INVALID")
    complete = not (value["unresolved_required"] or value["unsafe"] or failed)
    if (value["essential_files_complete"] != complete or value["coverage_state"] !=
            ("essential_files_verified" if complete else "records_verified_with_limitations")):
        raise SafetyError("BACKUP_ESSENTIAL_READINESS_INVALID")


def _verify_manifest_payload(folder, manifest):
    _validate_manifest(manifest)
    rows = {row["path"]: row for row in manifest["files"]}
    paths = _payload_paths(folder, manifest)
    essential = ({row["path"]: row for row in manifest["essential_selection"]["files"]}
                 if manifest["kind"] == ESSENTIAL_BACKUP_VERSION else {})
    for row in rows.values():
        raw = _read(paths[row["path"]], MAX_ATTACHMENT_BYTES, checked_parent=True)
        if len(raw) != row["bytes"] or _hash(raw) != row["sha256"]:
            raise SafetyError("BACKUP_FILE_HASH_MISMATCH")
        if row["path"].startswith("attachments/") and row["original_path"] in essential:
            _essential_bytes(raw, essential[row["original_path"]])
    state = {table: {} for table in TABLES}; head = "0" * 64; ids = set()
    for number in range(1, manifest["research_checkpoint"]["sequence"] + 1):
        event = load_event(paths[f"research/events/{number:08d}.json"], EVENT_LIMIT)
        try:
            if (type(event["seq"]) is not int or event["seq"] != number or event["previous"] != head
                    or event["hash"] != digest({key: value for key, value in event.items() if key != "hash"})
                    or event["evidence_type"] != manifest["evidence_type"] or event["id"] in ids):
                raise SafetyError("BACKUP_RESEARCH_CHAIN_INVALID")
            identifier(event["id"]); timestamp(event["at"])
            ResearchMemory._apply(state, event["changes"])
            head = event["hash"]; ids.add(event["id"])
        except (KeyError, TypeError, ValueError):
            raise SafetyError("BACKUP_RESEARCH_CHAIN_INVALID") from None
    research = manifest["research_checkpoint"]
    if (head != research["head"] or digest(state) != research["state_hash"]
            or {table: len(state[table]) for table in TABLES} != research["table_counts"]):
        raise SafetyError("BACKUP_RESEARCH_CHECKPOINT_MISMATCH")
    # Use the existing financial reader in an owned temporary directory, so
    # verification does not mutate a stored or read-only backup at all.
    financial_raw = _read(paths["finance/events.jsonl"], MAX_JOURNAL, checked_parent=True)
    with tempfile.TemporaryDirectory(prefix="neuraflow-backup-ledger-check-") as directory:
        journal = Journal(Path(directory))
        atomic_bytes(journal.path, financial_raw)
        with journal.locked():
            events = journal.events(); financial_state = journal.state()
    finance = manifest["finance_checkpoint"]
    if (len(events) != finance["events"] or (events[-1]["hash"] if events else "0" * 64) != finance["head"]
            or digest(financial_state) != finance["state_hash"]):
        raise SafetyError("BACKUP_FINANCIAL_CHECKPOINT_MISMATCH")
    return state


def verify_operational_backup(folder):
    folder = safe_path(folder)
    manifest = load_json(folder / "manifest.json", MAX_MANIFEST_BYTES)
    if isinstance(manifest, dict) and manifest.get("kind") == "essential-operational-record-backup-v3":
        from .operational_backup_parts import verify_multipart_backup
        return verify_multipart_backup(folder)
    _verify_manifest_payload(folder, manifest)
    return deepcopy(manifest)


def restore_operational_backup(folder, destination):
    """Restore into an empty isolated directory and verify canonical replay.

    This does not replace working research, rewrite original absolute file
    references, or reconcile/spend against any external account. Attachment
    mapping makes each original evidence file recoverable without changing it.
    """
    folder = safe_path(folder); destination = safe_path(destination)
    manifest = verify_operational_backup(folder)
    if manifest["kind"] == "essential-operational-record-backup-v3":
        from .operational_backup_parts import restore_multipart_backup
        return restore_multipart_backup(folder, destination)
    if (destination == folder or folder in destination.parents or destination in folder.parents
            or destination.exists() and not destination.is_dir()
            or destination.exists() and any(destination.iterdir())):
        raise SafetyError("RESTORE_DESTINATION_NOT_EMPTY")
    destination.mkdir(parents=True, exist_ok=True)
    paths = _payload_paths(folder, manifest)
    for row in manifest["files"]:
        raw = _read(paths[row["path"]], MAX_ATTACHMENT_BYTES, checked_parent=True)
        if len(raw) != row["bytes"] or _hash(raw) != row["sha256"]:
            raise SafetyError("BACKUP_FILE_HASH_MISMATCH")
        atomic_bytes(_file(destination, row["path"]), raw)
    restored = ResearchMemory(destination / "research", Journal(destination / "finance"),
                              evidence_type=manifest["evidence_type"])
    state, head, sequence, _ = restored._state(readonly=True)
    with restored.journal.locked():
        financial = restored.journal.state()
    if (head != manifest["research_checkpoint"]["head"] or sequence != manifest["research_checkpoint"]["sequence"]
            or digest(state) != manifest["research_checkpoint"]["state_hash"]
            or digest(financial) != manifest["finance_checkpoint"]["state_hash"]):
        raise SafetyError("RESTORE_CHECKPOINT_MISMATCH")
    receipt = {"schema_version": 1, "kind": "operational_restore_proof", "backup_id": manifest["backup_id"],
               "backup_hash": manifest["hash"], "verified_at": utc_now(), "evidence_type": manifest["evidence_type"],
               "research_events": sequence, "financial_events": manifest["finance_checkpoint"]["events"],
               "research_state_hash": digest(state), "financial_state_hash": digest(financial),
               "table_counts": {table: len(state[table]) for table in TABLES},
               "files_verified": len(manifest["files"]), "bytes_verified": sum(row["bytes"] for row in manifest["files"]),
               "attachment_mapping": [{"original_path": value["original_path"],
                                       "restored_path": str(_file(destination, value["path"])), "kind": value["kind"]}
                                      for value in manifest["attachments"]],
               "independent_backup_proved": False, "provider_calls": 0, "model_calls": 0,
               "verified_scope": ["research_canonical_records", "financial_journal", "selected_attachment_bytes"],
               "not_verified": ["durable_independent_destination", "automatic_reference_path_relocation", "hosted_restore"]}
    if manifest["kind"] == ESSENTIAL_BACKUP_VERSION:
        selection = manifest["essential_selection"]
        receipt.update(backup_kind=ESSENTIAL_BACKUP_VERSION,
            essential_files_complete=selection["essential_files_complete"],
            coverage_state=selection["coverage_state"], operational_recovery_proved=False,
            essential_limitations={"unresolved_required": len(selection["unresolved_required"]),
                "unsafe": len(selection["unsafe"]), "copy_failures": len(selection["copy_failures"])})
        receipt["verified_scope"].append("selected_essential_file_bytes")
        receipt["not_verified"].extend(["automatic_application_resumption", "credential_reconnection"])
    atomic_json(destination / "restore-proof.json", receipt)
    return receipt
