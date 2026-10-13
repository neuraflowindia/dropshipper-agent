"""Opt-in, immutable financial segments behind the existing Journal writer.

The old JSON-lines file is retained byte for byte. The new pointer is published
only after canonical replay, state/ID-page verification and exact readback.
Small hash-linked objects replace a lifetime-sized file; they do not create
credits or change a provider attempt's identity. No network calls occur here.

Compatibility callers may still request the full event list. A process-local
cache avoids parsing unchanged segments again; a cold reader verifies the
canonical event chain before accepting a stored checkpoint. This is not an
independent-device backup or protection against a malicious OS administrator.
"""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re

from .models import SafetyError, canonical, decode_json, digest, identifier, timestamp, utc_now
from .storage import atomic_json, load_json, safe_path


VERSION = "financial-segments-v2"
POINTER = "financial-ledger-v2.json"
MAX_OBJECT = 8 * 1024 * 1024
MAX_PAGE_ROWS = 128
MAX_SEGMENT_EVENTS = 512
CHECKPOINT_INTERVAL = 128
ZERO = "0" * 64
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def enabled(journal):
    return safe_path(journal.root / POINTER).exists()


def _fingerprint(path):
    import stat
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise SafetyError("JOURNAL_SEGMENT_FILE_INVALID")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _path(journal, reference):
    if (not isinstance(reference, dict) or set(reference) != {"sha256", "bytes"}
            or not isinstance(reference["sha256"], str) or not _HASH.fullmatch(reference["sha256"])
            or type(reference["bytes"]) is not int or not 0 < reference["bytes"] <= MAX_OBJECT):
        raise SafetyError("JOURNAL_SEGMENT_REFERENCE_INVALID")
    return safe_path(journal.root, "financial-v2", "objects") / (reference["sha256"] + ".json")


def _read(journal, reference, files=None):
    path = safe_path(_path(journal, reference))
    before = _fingerprint(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_OBJECT + 1)
    if (len(raw) != reference["bytes"] or sha256(raw).hexdigest() != reference["sha256"]
            or before != _fingerprint(path)):
        raise SafetyError("JOURNAL_SEGMENT_HASH_CHANGED")
    if files is not None:
        files[path] = before
    return decode_json(raw)


def _save(journal, value):
    raw = canonical(value) + b"\n"
    if len(raw) > MAX_OBJECT:
        raise SafetyError("JOURNAL_SEGMENT_OBJECT_SIZE_LIMIT")
    reference = {"sha256": sha256(raw).hexdigest(), "bytes": len(raw)}
    path = _path(journal, reference)
    if path.exists():
        if _read(journal, reference) != value:
            raise SafetyError("JOURNAL_SEGMENT_HASH_CHANGED")
    else:
        atomic_json(path, value)
        if _read(journal, reference) != value:
            raise SafetyError("JOURNAL_SEGMENT_HASH_CHANGED")
    return reference


def _event(event, *, sequence, head, ids):
    try:
        if (set(event) != {"version", "seq", "event_id", "at", "kind", "data", "previous", "hash"}
                or type(event["version"]) is not int or event["version"] != 1
                or type(event["seq"]) is not int or event["seq"] != sequence
                or event["previous"] != head or digest({k: v for k, v in event.items() if k != "hash"}) != event["hash"]
                or event["event_id"] in ids):
            raise ValueError
        identifier(event["event_id"]); timestamp(event["at"])
    except (KeyError, ValueError, TypeError, AttributeError, SafetyError):
        raise SafetyError("JOURNAL_CORRUPT_REVIEW_REQUIRED") from None
    return event["hash"]


def _pages(journal, table, rows):
    """A bounded linked page chain, not a lifetime-sized root reference list."""
    previous, chunk, total = None, [], 0
    for key, value in sorted(rows.items()):
        row = [key, value]
        trial = {"contract": "financial-state-page-v1", "table": table, "previous": previous, "rows": chunk + [row]}
        if chunk and (len(chunk) >= MAX_PAGE_ROWS or len(canonical(trial)) + 1 > MAX_OBJECT):
            previous = _save(journal, {**trial, "rows": chunk}); chunk = []
        chunk.append(row); total += 1
    if chunk:
        previous = _save(journal, {"contract": "financial-state-page-v1", "table": table, "previous": previous, "rows": chunk})
    return {"last": previous, "rows": total}


def _read_pages(journal, table, root, files):
    if (not isinstance(root, dict) or set(root) != {"last", "rows"}
            or type(root["rows"]) is not int or root["rows"] < 0):
        raise SafetyError("JOURNAL_CHECKPOINT_INVALID")
    rows, seen, previous = {}, set(), root["last"]
    while previous is not None:
        if previous["sha256"] in seen:
            raise SafetyError("JOURNAL_CHECKPOINT_INVALID")
        seen.add(previous["sha256"])
        page = _read(journal, previous, files)
        if (set(page) != {"contract", "table", "previous", "rows"}
                or page["contract"] != "financial-state-page-v1" or page["table"] != table
                or not isinstance(page["rows"], list) or not 1 <= len(page["rows"]) <= MAX_PAGE_ROWS):
            raise SafetyError("JOURNAL_CHECKPOINT_INVALID")
        for row in page["rows"]:
            if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str) or row[0] in rows:
                raise SafetyError("JOURNAL_CHECKPOINT_INVALID")
            rows[row[0]] = row[1]
        previous = page["previous"]
    if len(rows) != root["rows"]:
        raise SafetyError("JOURNAL_CHECKPOINT_INVALID")
    return rows


def _checkpoint(journal, events, state):
    ids = {event["event_id"]: {"seq": event["seq"], "hash": event["hash"]} for event in events}
    # Event-ID lookup has its own bounded pages, never an unbounded ID array
    # copied into the small active pointer or disposable research index.
    return _save(journal, {"contract": "financial-checkpoint-v1", "sequence": len(events),
        "head": events[-1]["hash"] if events else ZERO, "state_hash": digest(state),
        "tables": {name: _pages(journal, name, values) for name, values in
                   (("budgets", state[0]), ("attempts", state[1]), ("event_ids", ids))}})


def _pointer(journal, value=None):
    value = load_json(safe_path(journal.root / POINTER), 16384) if value is None else value
    if (not isinstance(value, dict) or set(value) != {"contract", "generation", "event_count", "head", "last_segment", "checkpoint", "legacy", "hash"}
            or value["contract"] != VERSION or type(value["generation"]) is not int or value["generation"] < 1
            or type(value["event_count"]) is not int or not 0 <= value["event_count"] <= 9007199254740991
            or not isinstance(value["head"], str) or not _HASH.fullmatch(value["head"])
            or value["hash"] != digest({k: v for k, v in value.items() if k != "hash"})
            or not isinstance(value["legacy"], dict) or set(value["legacy"]) != {"bytes", "sha256"}
            or type(value["legacy"]["bytes"]) is not int or value["legacy"]["bytes"] < 0
            or not isinstance(value["legacy"]["sha256"], str) or not _HASH.fullmatch(value["legacy"]["sha256"])):
        raise SafetyError("JOURNAL_SEGMENT_POINTER_INVALID")
    return value


def _publish(journal, value):
    value["hash"] = digest({k: v for k, v in value.items() if k != "hash"})
    atomic_json(safe_path(journal.root / POINTER), value)


def read(journal, *, _candidate=None, include_events=True):
    journal._require_lock()
    if type(include_events) is not bool:
        raise SafetyError("JOURNAL_READ_OPTION_INVALID")
    try:
        pointer = _pointer(journal, _candidate)
        previous = getattr(journal, "_financial_verified", None) if _candidate is None else None
        if previous is not None:
            if pointer["event_count"] < previous["pointer"]["event_count"] or pointer["generation"] < previous["pointer"]["generation"]:
                raise SafetyError("JOURNAL_SEGMENT_ROLLBACK")
            checked_files = dict(previous["segment_files"])
            if pointer["checkpoint"] == previous["pointer"]["checkpoint"]:
                checked_files.update(previous["checkpoint_files"])
            if journal.path in previous["files"]:
                checked_files[journal.path] = previous["files"][journal.path]
            try:
                unchanged = (journal.path.exists() == (journal.path in previous["files"])
                    and all(_fingerprint(path) == value for path, value in checked_files.items()))
            except OSError:
                unchanged = False
            if pointer == previous["pointer"] and unchanged:
                return (deepcopy(previous["events"]) if include_events else None), deepcopy(previous["state"])
            if not unchanged:
                previous = None
        files, segments, seen = dict(previous["segment_files"]) if previous else {}, [], set()
        reference = pointer["last_segment"]
        stop = previous["pointer"]["last_segment"] if previous else None
        while reference != stop:
            if reference is None:
                raise SafetyError("JOURNAL_SEGMENT_HISTORY_FORK")
            if reference["sha256"] in seen:
                raise SafetyError("JOURNAL_SEGMENT_CHAIN_INVALID")
            seen.add(reference["sha256"])
            segment = _read(journal, reference, files)
            if (not isinstance(segment, dict) or set(segment) != {"contract", "first", "last", "previous", "events"}
                    or segment["contract"] != VERSION or not isinstance(segment["events"], list)
                    or not 1 <= len(segment["events"]) <= MAX_SEGMENT_EVENTS):
                raise SafetyError("JOURNAL_SEGMENT_CHAIN_INVALID")
            segments.append(segment); reference = segment["previous"]
        events = list(previous["events"]) if previous else []
        ids = set(previous["event_ids"]) if previous else set()
        head = previous["pointer"]["head"] if previous else ZERO
        first_new = len(events)
        for segment in reversed(segments):
            if (type(segment["first"]) is not int or type(segment["last"]) is not int
                    or segment["first"] != len(events) + 1 or segment["last"] != len(events) + len(segment["events"])):
                raise SafetyError("JOURNAL_SEGMENT_CHAIN_INVALID")
            for event in segment["events"]:
                head = _event(event, sequence=len(events) + 1, head=head, ids=ids)
                events.append(event); ids.add(event["event_id"])
        if len(events) != pointer["event_count"] or head != pointer["head"]:
            raise SafetyError("JOURNAL_SEGMENT_CHAIN_INVALID")
        segment_files = dict(files)
        if previous and pointer["checkpoint"] == previous["pointer"]["checkpoint"]:
            # The already verified checkpoint is unchanged. Only newly linked
            # canonical tail events are reduced under the short writer.
            state = journal._reduce(events[first_new:], initial=previous["state"])
            checkpoint_state = previous["checkpoint_state"]
            checkpoint_files = previous["checkpoint_files"]
        else:
            checkpoint_files = {}
            checkpoint = _read(journal, pointer["checkpoint"], checkpoint_files)
            checkpoint_state = _verify_checkpoint(journal, checkpoint, events, previous, checkpoint_files)
            state = journal._reduce(events[checkpoint["sequence"]:], initial=checkpoint_state)
        files.update(checkpoint_files)
        # The retained legacy prefix is immutable provenance, not a second
        # writable ledger. An unchanged verified file needs no byte reread.
        if previous is None and _legacy(journal) != pointer["legacy"]:
            raise SafetyError("JOURNAL_LEGACY_PREFIX_CHANGED")
        if previous is not None and pointer["legacy"] != previous["pointer"]["legacy"]:
            raise SafetyError("JOURNAL_LEGACY_PREFIX_CHANGED")
        if journal.path.exists():
            files[journal.path] = _fingerprint(journal.path)
        event_ids = {event["event_id"]: event["seq"] for event in events}
        if _candidate is None:
            journal._financial_verified = {"pointer": pointer, "files": files, "events": events,
                "state": state, "event_ids": event_ids, "checkpoint_state": checkpoint_state,
                "segment_files": segment_files, "checkpoint_files": checkpoint_files}
        return (deepcopy(events) if include_events else None), deepcopy(state)
    except (KeyError, TypeError, ValueError, AttributeError, OSError):
        raise SafetyError("JOURNAL_SEGMENT_CHAIN_INVALID") from None


def _verify_checkpoint(journal, checkpoint, events, previous, files):
    try:
        if (not isinstance(checkpoint, dict) or set(checkpoint) != {"contract", "sequence", "head", "state_hash", "tables"}
                or checkpoint["contract"] != "financial-checkpoint-v1" or type(checkpoint["sequence"]) is not int
                or not 0 <= checkpoint["sequence"] <= len(events) or set(checkpoint["tables"]) != {"budgets", "attempts", "event_ids"}
                or checkpoint["head"] != (events[checkpoint["sequence"] - 1]["hash"] if checkpoint["sequence"] else ZERO)):
            raise SafetyError("JOURNAL_CHECKPOINT_INVALID")
        saved = {name: _read_pages(journal, name, root, files) for name, root in checkpoint["tables"].items()}
        if previous and checkpoint["sequence"] >= len(previous["events"]):
            actual = journal._reduce(events[len(previous["events"]):checkpoint["sequence"]], initial=previous["state"])
        else:
            actual = journal._reduce(events[:checkpoint["sequence"]])
        expected_ids = {event["event_id"]: {"seq": event["seq"], "hash": event["hash"]}
                        for event in events[:checkpoint["sequence"]]}
        if ((saved["budgets"], saved["attempts"]) != actual or digest(actual) != checkpoint["state_hash"]
                or saved["event_ids"] != expected_ids):
            raise SafetyError("JOURNAL_CHECKPOINT_CANONICAL_MISMATCH")
        return actual
    except (KeyError, TypeError, ValueError, AttributeError, OSError):
        raise SafetyError("JOURNAL_SEGMENT_CHAIN_INVALID") from None


def _legacy(journal, *, events=None):
    safe_path(journal.path)
    if not journal.path.exists():
        return {"bytes": 0, "sha256": sha256(b"").hexdigest()}
    before, hashed, total = _fingerprint(journal.path), sha256(), 0
    with journal.path.open("rb") as stream:
        while True:
            raw = stream.readline(MAX_OBJECT + 1) if events is not None else stream.read(1024 * 1024)
            if not raw:
                break
            if events is not None:
                if len(raw) > MAX_OBJECT or not raw.endswith(b"\n"):
                    raise SafetyError("JOURNAL_CORRUPT_REVIEW_REQUIRED")
                events.append(decode_json(raw))
            hashed.update(raw); total += len(raw)
    if before != _fingerprint(journal.path):
        raise SafetyError("JOURNAL_LEGACY_PREFIX_CHANGED")
    return {"bytes": total, "sha256": hashed.hexdigest()}


def migrate(journal):
    """Explicit local migration under the same writer; never automatic spend."""
    journal._require_lock()
    if enabled(journal):
        events, state = read(journal)
        return {"contract": VERSION, "events": len(events), "head": events[-1]["hash"] if events else ZERO,
                "state_hash": digest(state), "reused": True, "legacy_bytes_unchanged": True}
    events, ids, head = [], set(), ZERO
    legacy = _legacy(journal, events=events)
    for sequence, event in enumerate(events, 1):
        head = _event(event, sequence=sequence, head=head, ids=ids)
        ids.add(event["event_id"])
    state = journal._reduce(events)
    last, chunk = None, []
    for event in events:
        trial = {"contract": VERSION, "first": event["seq"] - len(chunk), "last": event["seq"], "previous": last, "events": chunk + [event]}
        if chunk and (len(chunk) >= MAX_SEGMENT_EVENTS or len(canonical(trial)) + 1 > MAX_OBJECT):
            last = _save(journal, {"contract": VERSION, "first": chunk[0]["seq"], "last": chunk[-1]["seq"], "previous": last, "events": chunk})
            chunk = []
        chunk.append(event)
    if chunk:
        last = _save(journal, {"contract": VERSION, "first": chunk[0]["seq"], "last": chunk[-1]["seq"], "previous": last, "events": chunk})
    pointer = {"contract": VERSION, "generation": 1, "event_count": len(events), "head": head,
        "last_segment": last, "checkpoint": _checkpoint(journal, events, state),
        "legacy": legacy}
    pointer["hash"] = digest(pointer)
    actual_events, actual_state = read(journal, _candidate=pointer)
    if actual_events != events or actual_state != state:
        raise SafetyError("JOURNAL_SEGMENT_MIGRATION_MISMATCH")
    _publish(journal, pointer)
    read(journal)
    return {"contract": VERSION, "events": len(events), "head": head, "state_hash": digest(state),
            "reused": False, "legacy_bytes_unchanged": True}


def append(journal, event_id, kind, data):
    journal._require_lock(); identifier(event_id)
    _, state = read(journal, include_events=False)
    verified = journal._financial_verified
    events = verified["events"]
    sequence = verified["event_ids"].get(event_id)
    if sequence is not None:
        event = events[sequence - 1]
        if event["kind"] == kind and event["data"] == data:
            return deepcopy(event)
        raise SafetyError("EVENT_ID_CONFLICT")
    pointer = _pointer(journal)
    if pointer["event_count"] >= 9007199254740991:
        raise SafetyError("JOURNAL_SEQUENCE_SIZE_LIMIT")
    body = {"version": 1, "seq": len(events) + 1, "event_id": event_id, "at": utc_now(),
            "kind": kind, "data": data, "previous": pointer["head"]}
    event = {**body, "hash": digest(body)}
    next_state = journal._reduce([event], initial=state)
    last = _save(journal, {"contract": VERSION, "first": event["seq"], "last": event["seq"],
                           "previous": pointer["last_segment"], "events": [event]})
    checkpoint = pointer["checkpoint"]
    if len(events) % CHECKPOINT_INTERVAL == CHECKPOINT_INTERVAL - 1:
        checkpoint = _checkpoint(journal, events + [event], next_state)
    _publish(journal, {**pointer, "generation": pointer["generation"] + 1, "event_count": len(events) + 1,
                       "head": event["hash"], "last_segment": last, "checkpoint": checkpoint})
    # The pointer is the commit boundary. A lost reply is reconciled using the
    # same event ID; any pre-pointer orphan object is inert and reusable.
    read(journal, include_events=False)
    return event
