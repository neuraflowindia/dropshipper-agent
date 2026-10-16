"""Opt-in bounded research checkpoints over the retained canonical events.

Checkpoint roots and pages are derived, not a second research database. A cold
ResearchMemory still verifies canonical replay before accepting a checkpoint;
warm readers then verify only the unchanged objects and canonical tail. Original
event paths/bytes remain compatible with old readers and backup selection.
"""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re

from .models import SafetyError, canonical, digest, identifier, timestamp
from .storage import atomic_json, load_json, safe_path


VERSION = "research-segments-v1"
POINTER = "research-checkpoint-v1.json"
MARKER = "research-segments-enabled-v1.json"
MAX_PAGE_ROWS = 128
MAX_PAGE_BYTES = 1024 * 1024
MAX_OBJECT = 8 * 1024 * 1024 + 4096
MAX_SEGMENT_EVENTS = 64
ZERO = "0" * 64
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def enabled(memory):
    return safe_path(memory.root / POINTER).exists() or safe_path(memory.root / MARKER).exists()


def _marker(memory):
    path = safe_path(memory.root / MARKER)
    if not path.exists(): return None
    row = load_json(path, 16384)
    if (not isinstance(row, dict) or set(row) != {"contract", "evidence_type", "initial_sequence", "initial_head", "hash"}
            or row["contract"] != VERSION or row["evidence_type"] != memory.evidence_type
            or type(row["initial_sequence"]) is not int or row["initial_sequence"] < 0
            or not isinstance(row["initial_head"], str) or not _HASH.fullmatch(row["initial_head"])
            or row["hash"] != digest({k: v for k, v in row.items() if k != "hash"})):
        raise SafetyError("RESEARCH_CHECKPOINT_MARKER_INVALID")
    return row


def _path(memory, ref):
    if (not isinstance(ref, dict) or set(ref) != {"sha256", "bytes"}
            or not isinstance(ref["sha256"], str) or not _HASH.fullmatch(ref["sha256"])
            or type(ref["bytes"]) is not int or not 0 < ref["bytes"] <= MAX_OBJECT):
        raise SafetyError("RESEARCH_SEGMENT_REFERENCE_INVALID")
    return safe_path(memory.root, "research-v1", "objects") / (ref["sha256"] + ".json")


def _read(memory, ref, files=None):
    path = safe_path(_path(memory, ref)); before = memory._fingerprint(path)
    with path.open("rb") as stream:
        raw = stream.read(MAX_OBJECT + 1)
    if (len(raw) != ref["bytes"] or sha256(raw).hexdigest() != ref["sha256"]
            or memory._fingerprint(path) != before):
        raise SafetyError("RESEARCH_SEGMENT_HASH_CHANGED")
    if files is not None: files[path] = before
    from .models import decode_json
    return decode_json(raw)


def _save(memory, value):
    raw = canonical(value) + b"\n"
    if len(raw) > MAX_OBJECT: raise SafetyError("RESEARCH_SEGMENT_OBJECT_SIZE_LIMIT")
    ref = {"sha256": sha256(raw).hexdigest(), "bytes": len(raw)}
    path = _path(memory, ref)
    if not path.exists(): atomic_json(path, value)
    if canonical(_read(memory, ref)) != canonical(value): raise SafetyError("RESEARCH_SEGMENT_HASH_CHANGED")
    return ref


def _tree(memory, table, rows, *, prefix=""):
    """Fixed fan-out, bounded leaves: neither root nor ID lookup grows forever."""
    items = [list(row) for row in sorted(rows.items(), key=lambda row: (digest(row[0]), row[0]))]
    body = {"contract": "research-page-v1", "table": table, "prefix": prefix, "rows": items}
    if (len(items) <= MAX_PAGE_ROWS and (len(canonical(body)) + 1 <= MAX_PAGE_BYTES or len(items) <= 1)):
        return _save(memory, body) if items else None
    if len(prefix) >= 64: raise SafetyError("RESEARCH_PAGE_COLLISION")
    groups = {}
    for rid, value in items:
        groups.setdefault(digest(rid)[len(prefix)], {})[rid] = value
    children = {part: _tree(memory, table, group, prefix=prefix + part) for part, group in groups.items()}
    return _save(memory, {"contract": "research-branch-v1", "table": table,
        "prefix": prefix, "children": children})


def _node(memory, table, ref, prefix, files=None):
    node = _read(memory, ref, files)
    if not isinstance(node, dict) or node.get("table") != table or node.get("prefix") != prefix:
        raise SafetyError("RESEARCH_CHECKPOINT_PAGE_INVALID")
    if node.get("contract") == "research-page-v1":
        if (set(node) != {"contract", "table", "prefix", "rows"}
                or not isinstance(node["rows"], list) or not 1 <= len(node["rows"]) <= MAX_PAGE_ROWS):
            raise SafetyError("RESEARCH_CHECKPOINT_PAGE_INVALID")
        seen, order = set(), []
        for row in node["rows"]:
            if (not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str)
                    or row[0] in seen or not digest(row[0]).startswith(prefix)):
                raise SafetyError("RESEARCH_CHECKPOINT_PAGE_INVALID")
            identifier(row[0]); seen.add(row[0]); order.append((digest(row[0]), row[0]))
        if order != sorted(order): raise SafetyError("RESEARCH_CHECKPOINT_PAGE_INVALID")
    elif node.get("contract") == "research-branch-v1":
        if (set(node) != {"contract", "table", "prefix", "children"} or len(prefix) >= 64
                or not isinstance(node["children"], dict) or not 1 <= len(node["children"]) <= 16
                or any(not isinstance(part, str) or len(part) != 1 or part not in "0123456789abcdef" for part in node["children"])):
            raise SafetyError("RESEARCH_CHECKPOINT_PAGE_INVALID")
    else: raise SafetyError("RESEARCH_CHECKPOINT_PAGE_INVALID")
    return node


def _rows(memory, table, ref, *, prefix="", files=None, after=None):
    if ref is None: return
    if after is not None and prefix < after[0][:len(prefix)]: return
    node = _node(memory, table, ref, prefix, files)
    if node["contract"] == "research-page-v1":
        for rid, value in node["rows"]:
            if after is None or (digest(rid), rid) > after: yield rid, value
    else:
        for part, child in sorted(node["children"].items()):
            yield from _rows(memory, table, child, prefix=prefix + part, files=files, after=after)


def _lookup(memory, table, ref, rid):
    prefix, hashed = "", digest(rid)
    while ref is not None:
        node = _node(memory, table, ref, prefix)
        if node["contract"] == "research-page-v1":
            return next((value for key, value in node["rows"] if key == rid), None)
        part = hashed[len(prefix)]; prefix += part; ref = node["children"].get(part)
    return None


def _pointer(memory):
    row = load_json(safe_path(memory.root / POINTER), 16384)
    if (not isinstance(row, dict) or set(row) != {"contract", "evidence_type", "generation", "sequence",
            "head", "last_segment", "checkpoint", "hash"} or row["contract"] != VERSION
            or row["evidence_type"] != memory.evidence_type or type(row["generation"]) is not int or row["generation"] < 1
            or type(row["sequence"]) is not int or row["sequence"] < 0
            or not isinstance(row["head"], str) or not _HASH.fullmatch(row["head"])
            or row["hash"] != digest({k: v for k, v in row.items() if k != "hash"})):
        raise SafetyError("RESEARCH_CHECKPOINT_POINTER_INVALID")
    return row


def _event_proof(memory, sequence):
    from .research_memory import EVENT_LIMIT, load_event
    path = safe_path(memory.root, "events") / f"{sequence:08d}.json"
    before = memory._fingerprint(path)
    with path.open("rb") as stream: raw = stream.read(EVENT_LIMIT + 1)
    event = load_event(path)
    if len(raw) > EVENT_LIMIT or before != memory._fingerprint(path):
        raise SafetyError("RESEARCH_EVENTS_INVALID")
    return {"sequence": sequence, "name": path.name, "bytes": len(raw), "sha256": sha256(raw).hexdigest(),
        "id": event["id"], "event_hash": event["hash"], "changes_hash": digest(event["changes"])}, event


def _segments(memory, start, stop, previous):
    chunk = []; previous_head = ZERO if start == 1 else _event_proof(memory, start - 1)[1]["hash"]
    for sequence in range(start, stop + 1):
        proof, event = _event_proof(memory, sequence); chunk.append(proof)
        if len(chunk) == MAX_SEGMENT_EVENTS or sequence == stop:
            previous = _save(memory, {"contract": "research-event-segment-v1", "evidence_type": memory.evidence_type,
                "first": chunk[0]["sequence"], "last": sequence, "event_count": len(chunk),
                "previous": previous, "previous_head": previous_head, "head": event["hash"], "events": chunk})
            previous_head = event["hash"]; chunk = []
    return previous


def _verify_segments(memory, root, files, previous=None):
    reference, expected_last, expected_head, seen = root["last_segment"], root["sequence"], root["head"], set()
    stop = previous["root"]["last_segment"] if previous else None
    ids = dict(previous["ids"]) if previous else {}
    while reference != stop:
        if reference is None: raise SafetyError("RESEARCH_SEGMENT_CHAIN_INVALID")
        if reference["sha256"] in seen: raise SafetyError("RESEARCH_SEGMENT_CHAIN_INVALID")
        seen.add(reference["sha256"]); row = _read(memory, reference, files)
        if (not isinstance(row, dict) or set(row) != {"contract", "evidence_type", "first", "last", "event_count",
                "previous", "previous_head", "head", "events"} or row["contract"] != "research-event-segment-v1"
                or row["evidence_type"] != memory.evidence_type or row["last"] != expected_last or row["head"] != expected_head
                or not isinstance(row["events"], list) or not 1 <= len(row["events"]) <= MAX_SEGMENT_EVENTS
                or type(row["first"]) is not int or type(row["last"]) is not int
                or type(row["event_count"]) is not int or row["event_count"] != len(row["events"])
                or row["first"] != row["last"] - len(row["events"]) + 1):
            raise SafetyError("RESEARCH_SEGMENT_CHAIN_INVALID")
        head = row["previous_head"]
        for sequence, proof in enumerate(row["events"], row["first"]):
            actual, event = _event_proof(memory, sequence)
            if canonical(proof) != canonical(actual) or event["previous"] != head:
                raise SafetyError("RESEARCH_SEGMENT_ORIGINAL_CHANGED")
            if actual["id"] in ids: raise SafetyError("RESEARCH_SEGMENT_CHAIN_INVALID")
            ids[actual["id"]] = {"sequence": sequence, "event_hash": actual["event_hash"], "changes_hash": actual["changes_hash"]}
            files[memory.root / "events" / actual["name"]] = memory._fingerprint(memory.root / "events" / actual["name"])
            head = event["hash"]
        if head != row["head"]: raise SafetyError("RESEARCH_SEGMENT_CHAIN_INVALID")
        reference, expected_last, expected_head = row["previous"], row["first"] - 1, row["previous_head"]
    expected = (previous["root"]["sequence"], previous["root"]["head"]) if previous else (0, ZERO)
    if (expected_last, expected_head) != expected: raise SafetyError("RESEARCH_SEGMENT_CHAIN_INVALID")
    return ids


def prefix_target(memory):
    """The published checkpoint's event count when the next check needs it.

    ``None`` when checkpoints are off, the pointer is unreadable (the check
    then reports that itself) or this instance already verified that exact
    checkpoint. Only a hint for keeping one copy during a cold replay.
    """
    try:
        if not enabled(memory): return None
        root = _pointer(memory)
    except (SafetyError, OSError, KeyError, TypeError, ValueError, AttributeError):
        return None
    cache = getattr(memory, "_research_pages_verified", None)
    if cache and cache["root"] == root: return None
    return root["sequence"] or None


def _captured_prefix(memory, root):
    """The state at ``root``'s count kept by this call's own verified replay.

    Accepted only when it is from the replay whose manifest this instance has
    just verified (same files, same fingerprints, same namespace); otherwise
    ``None`` and the caller replays the canonical prefix itself.
    """
    captured, verified = getattr(memory, "_replay_capture", None), memory._verified
    if (not captured or verified is None or captured["sequence"] != root["sequence"]
            or captured["evidence_type"] != memory.evidence_type
            or verified["manifest"][:captured["sequence"]] != captured["manifest"]):
        return None
    memory._replay_capture = None
    return captured["state"], captured["ids"], captured["head"]


def _prefix(memory, sequence):
    """Canonical verification, never a checkpoint's self-hash assertion."""
    from .research_memory import TABLES, load_event
    state, ids, head = {table: {} for table in TABLES}, {}, ZERO
    for number in range(1, sequence + 1):
        event = load_event(safe_path(memory.root, "events") / f"{number:08d}.json")
        if (event["seq"] != number or event["previous"] != head or event["evidence_type"] != memory.evidence_type
                or event["id"] in ids or event["hash"] != digest({k: v for k, v in event.items() if k != "hash"})):
            raise SafetyError("RESEARCH_EVENTS_INVALID")
        identifier(event["id"]); timestamp(event["at"]); memory._apply(state, event["changes"])
        ids[event["id"]] = {"sequence": number, "event_hash": event["hash"], "changes_hash": digest(event["changes"])}
        head = event["hash"]
    return state, ids, head


def check(memory, state, head, sequence, *, _verified_manifest=None):
    """Check derived state; corruption falls back to verified canonical replay.

    Only ResearchMemory._state supplies its just-verified original manifest,
    while holding its cache lock. This is a point-in-time prefix, not a second
    filesystem snapshot: an uncooperative rewrite after enumeration is detected
    by the next fresh read/locked commit. Standalone callers, derived objects,
    cold proofs and activation/rollback guards retain their direct file checks.
    """
    if not enabled(memory): return None
    marker = _marker(memory)
    if marker and marker["initial_sequence"] > sequence:
        raise SafetyError("RESEARCH_EVENTS_INVALID")
    if marker and marker["initial_sequence"]:
        # This small once-published marker is not checkpoint state authority;
        # it prevents a missing pointer from silently enabling the legacy
        # empty-namespace path after the original history disappears.
        anchor_path = memory.root / "events" / f"{marker['initial_sequence']:08d}.json"
        anchor_fingerprint = memory._fingerprint(anchor_path)
        previous_anchor = getattr(memory, "_research_activation_anchor", None)
        if previous_anchor != (marker, anchor_fingerprint):
            original = _event_proof(memory, marker["initial_sequence"])[1]
            if original["hash"] != marker["initial_head"]: raise SafetyError("RESEARCH_EVENTS_INVALID")
            memory._research_activation_anchor = (deepcopy(marker), anchor_fingerprint)
    try:
        candidate = _pointer(memory)
    except (SafetyError, OSError, KeyError, TypeError, ValueError, AttributeError):
        candidate = None
    if candidate and candidate["sequence"] > sequence:
        # The published writer anchor protects a fresh process against valid
        # prefix truncation, including removal of every original event. A
        # concurrently appended, actually present tail can leave this read at
        # its earlier consistent prefix; the next writer rechecks all events.
        if not all((memory.root / "events" / f"{number:08d}.json").exists()
                   for number in (sequence + 1, candidate["sequence"])):
            raise SafetyError("RESEARCH_EVENTS_INVALID")
        memory.last_checkpoint_diagnostic = "RESEARCH_CHECKPOINT_NEWER_CANONICAL_PREFIX"
        return None
    try:
        root = candidate if candidate is not None else _pointer(memory)
        cache = getattr(memory, "_research_pages_verified", None)
        verified = memory._verified
        # A passed dictionary, different tuple or mismatched prefix is not
        # eligible. Freshness comes from _state's immediate replay, NOT from
        # tuple identity alone: do not pass a retained tuple from another call.
        # Never cache this view across calls or use it for derived objects.
        events = (dict(_verified_manifest) if _verified_manifest is not None
            and verified is not None and _verified_manifest is verified["manifest"]
            and (verified["head"], verified["sequence"], verified["evidence_type"])
                == (head, sequence, memory.evidence_type) else {})
        def unchanged_file(path, fingerprint):
            if path in events:
                return events[path] == fingerprint
            return memory._fingerprint(path) == fingerprint
        unchanged = cache and all(unchanged_file(path, fingerprint) for path, fingerprint in cache["files"].items())
        if unchanged and cache["root"] == root:
            memory.last_checkpoint_diagnostic = None
            return root
        previous = cache if unchanged and root["sequence"] >= cache["root"]["sequence"] else None
        files = dict(previous["segment_files"]) if previous else {}
        actual_ids = _verify_segments(memory, root, files, previous)
        segment_files = dict(files)
        checkpoint = _read(memory, root["checkpoint"], files)
        from .research_memory import TABLES
        if (not isinstance(checkpoint, dict) or set(checkpoint) != {"contract", "evidence_type", "sequence", "head", "tables", "event_ids", "novelty"}
                or checkpoint["contract"] != "research-checkpoint-v1" or checkpoint["evidence_type"] != memory.evidence_type
                or checkpoint["sequence"] != root["sequence"] or checkpoint["head"] != root["head"]
                or set(checkpoint["tables"]) != set(TABLES)):
            raise SafetyError("RESEARCH_CHECKPOINT_INVALID")
        if root["sequence"] == sequence:
            actual_state, actual_head = state, head
        else:
            # The same canonical prefix, kept by the replay that just
            # verified these files, or replayed again when not available.
            actual_state, prefix_ids, actual_head = (_captured_prefix(memory, root)
                                                     or _prefix(memory, root["sequence"]))
            if canonical(prefix_ids) != canonical(actual_ids): raise SafetyError("RESEARCH_CHECKPOINT_CANONICAL_MISMATCH")
        if actual_head != root["head"]: raise SafetyError("RESEARCH_CHECKPOINT_CANONICAL_MISMATCH")
        _verify_checkpoint_values(memory, checkpoint, actual_state, actual_ids, files)
        memory._research_pages_verified = {"root": root, "checkpoint": checkpoint, "files": files,
            "segment_files": segment_files, "ids": actual_ids}
        memory.last_checkpoint_diagnostic = None
        return root
    except (SafetyError, OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        memory._research_pages_verified = None
        memory.last_checkpoint_diagnostic = str(error) if isinstance(error, SafetyError) else "RESEARCH_CHECKPOINT_UNAVAILABLE"
        return None


def _aliases(state):
    from .functional_aliases import alias_binding, alias_projection
    if alias_binding(state) is not None:
        # This is a derived view of reviewed original identities, not another
        # registry. Preserve the exact old redirect semantics until a reviewed
        # alias actually exists in the canonical research events.
        projection = alias_projection(state)
        has_lineage = any(row.get("lineage_refs") for row in projection.values())
        return {cid: {"canonical_id": row["canonical_id"], "reason": row["reason"],
                      **({"ancestor_ids": row.get("ancestor_ids", [])} if has_lineage else {})}
                for cid, row in projection.items()}
    output = {}
    for cid in state["concepts"]:
        current, seen = cid, set()
        while current not in seen:
            seen.add(current); row = state["concepts"].get(current)
            if row is None:
                output[cid] = {"canonical_id": None, "reason": "ALIAS_TARGET_MISSING"}; break
            if row.get("status") != "merged" or not row.get("merged_into"):
                output[cid] = {"canonical_id": current, "reason": None}; break
            current = row["merged_into"]
        else: output[cid] = {"canonical_id": None, "reason": "ALIAS_CYCLE"}
    return output


def _novelty_rows(state):
    aliases, groups = _aliases(state), {}
    members, exposure_targets, hold_targets = {}, {}, {}
    for cid, row in aliases.items():
        if row["canonical_id"] is not None:
            groups.setdefault(row["canonical_id"], {"aliases": {}, "clients": {}})["aliases"][cid] = digest(state["concepts"][cid])
            members.setdefault(row["canonical_id"], set()).add(cid)
    # Exposure follows an explicit parent->child direction. Holds additionally
    # overlap an actual ancestor/descendant, but a common ancestor never makes
    # two siblings aliases or gives one sibling the other's publication.
    for canonical_id, own_members in members.items():
        row = aliases[min(own_members)]
        history = own_members | set(row.get("ancestor_ids", []))
        for source_id in history:
            exposure_targets.setdefault(source_id, set()).add(canonical_id)
            hold_targets.setdefault(source_id, set()).add(canonical_id)
        for ancestor_id in row.get("ancestor_ids", []):
            ancestor = aliases.get(ancestor_id, {}).get("canonical_id")
            if ancestor is not None:
                for member in own_members:
                    hold_targets.setdefault(member, set()).add(ancestor)
    def group(cid, client):
        canonical_id = aliases.get(cid, {}).get("canonical_id")
        # Orphan history is retained under its real saved ID, not discarded.
        canonical_id = canonical_id or cid
        return groups.setdefault(canonical_id, {"aliases": {}, "clients": {}})["clients"].setdefault(client,
            {"exposures": {}, "holds": {}})
    for rid, row in state["published_history"].items():
        if row.get("status") == "published":
            targets = exposure_targets.get(row["concept_id"], {row["concept_id"]})
            for target in targets:
                group(target, row.get("client_id", "default"))["exposures"][rid] = deepcopy(row)
    for rid, row in state["issue_reservations"].items():
        if row.get("state") in {"reserved", "uploaded", "published"}:
            for product in row.get("products", []):
                targets = hold_targets.get(product["concept_id"], {product["concept_id"]})
                for target in targets:
                    group(target, row.get("client_id", "default"))["holds"][rid] = deepcopy(row)
    return aliases, groups


def _save_novelty(memory, state):
    aliases, groups = _novelty_rows(state); roots = {}
    for cid, group in groups.items():
        clients = {}
        for client, rows in group["clients"].items():
            clients[client] = {name: {"root": _tree(memory, "novelty-" + name, values), "rows": len(values)}
                              for name, values in rows.items()}
        roots[cid] = {"aliases": {"root": _tree(memory, "novelty-members", group["aliases"]), "rows": len(group["aliases"])},
                      "clients": {"root": _tree(memory, "novelty-clients", clients), "rows": len(clients)}}
    return {"aliases": {"root": _tree(memory, "novelty-aliases", aliases), "rows": len(aliases)},
            "groups": {"root": _tree(memory, "novelty-groups", roots), "rows": len(roots)}}


def _check_novelty(memory, state, novelty, files):
    aliases, groups = _novelty_rows(state)
    def read(table, page, count):
        if not isinstance(page, dict) or set(page) != {"root", "rows"} or type(page["rows"]) is not int or page["rows"] != count:
            raise SafetyError("RESEARCH_NOVELTY_PROJECTION_INVALID")
        rows = dict(_rows(memory, table, page["root"], files=files))
        if len(rows) != count: raise SafetyError("RESEARCH_NOVELTY_PROJECTION_INVALID")
        return rows
    if not isinstance(novelty, dict) or set(novelty) != {"aliases", "groups"}:
        raise SafetyError("RESEARCH_NOVELTY_PROJECTION_INVALID")
    if canonical(read("novelty-aliases", novelty["aliases"], len(aliases))) != canonical(aliases):
        raise SafetyError("RESEARCH_NOVELTY_PROJECTION_INVALID")
    saved = read("novelty-groups", novelty["groups"], len(groups))
    for cid, group in groups.items():
        row = saved[cid]
        if set(row) != {"aliases", "clients"} or canonical(read("novelty-members", row["aliases"], len(group["aliases"]))) != canonical(group["aliases"]):
            raise SafetyError("RESEARCH_NOVELTY_PROJECTION_INVALID")
        clients = read("novelty-clients", row["clients"], len(group["clients"]))
        for client, values in group["clients"].items():
            if set(clients[client]) != {"exposures", "holds"}: raise SafetyError("RESEARCH_NOVELTY_PROJECTION_INVALID")
            for name, expected in values.items():
                if canonical(read("novelty-" + name, clients[client][name], len(expected))) != canonical(expected):
                    raise SafetyError("RESEARCH_NOVELTY_PROJECTION_INVALID")


def _verify_checkpoint_values(memory, checkpoint, state, ids, files):
    for table, expected in list(state.items()) + [("event_ids", ids)]:
        page = checkpoint["event_ids"] if table == "event_ids" else checkpoint["tables"][table]
        if (not isinstance(page, dict) or set(page) != {"root", "rows"} or type(page["rows"]) is not int or page["rows"] != len(expected)
                or canonical(dict(_rows(memory, table, page["root"], files=files))) != canonical(expected)):
            raise SafetyError("RESEARCH_CHECKPOINT_CANONICAL_MISMATCH")
    _check_novelty(memory, state, checkpoint["novelty"], files)


def update(memory, state, head, sequence):
    """Write immutable objects outside the writer, CAS-publish current root."""
    pointer_path = safe_path(memory.root / POINTER)
    pointer_before = memory._fingerprint(pointer_path) if pointer_path.exists() else None
    try: current = _pointer(memory) if enabled(memory) else None
    except (SafetyError, OSError): current = None
    valid = current is not None and check(memory, state, head, sequence) is not None
    start = current["sequence"] + 1 if valid else 1
    last = _segments(memory, start, sequence, current["last_segment"] if valid else None)
    if valid:
        ids = dict(memory._research_pages_verified["ids"])
        actual_head = current["head"]
        for number in range(start, sequence + 1):
            proof, event = _event_proof(memory, number)
            if event["previous"] != actual_head or event["id"] in ids:
                raise SafetyError("RESEARCH_EVENTS_INVALID")
            ids[event["id"]] = {"sequence": number, "event_hash": event["hash"], "changes_hash": proof["changes_hash"]}
            actual_head = event["hash"]
    else:
        _, ids, actual_head = _prefix(memory, sequence)
    if actual_head != head: raise SafetyError("RESEARCH_CHECKPOINT_CANONICAL_MISMATCH")
    checkpoint = {"contract": "research-checkpoint-v1", "evidence_type": memory.evidence_type,
        "sequence": sequence, "head": head,
        "tables": {table: {"root": _tree(memory, table, values), "rows": len(values)} for table, values in state.items()},
        "event_ids": {"root": _tree(memory, "event_ids", ids), "rows": len(ids)}, "novelty": _save_novelty(memory, state)}
    candidate = {"contract": VERSION, "evidence_type": memory.evidence_type,
        "generation": current["generation"] + 1 if valid else 1, "sequence": sequence, "head": head,
        "last_segment": last, "checkpoint": _save(memory, checkpoint)}
    candidate["hash"] = digest(candidate)
    # Exact object readback before pointer publication. The canonical root
    # selected for this projection must still be current under the same writer.
    if canonical(_read(memory, candidate["checkpoint"])) != canonical(checkpoint):
        raise SafetyError("RESEARCH_CHECKPOINT_CANONICAL_MISMATCH")
    files = {}
    _verify_checkpoint_values(memory, checkpoint, state, ids, files)
    # Every event segment this pointer adds (newly written or reused) is read
    # back too, so its file is checked again under the lock below: a page
    # removed meanwhile (for example by the unused-page pruner) is never
    # published; this update then fails and the next one writes it again.
    stop, reference, seen = (current["last_segment"] if valid else None), last, set()
    while reference is not None and reference != stop:
        if reference["sha256"] in seen: raise SafetyError("RESEARCH_SEGMENT_CHAIN_INVALID")
        seen.add(reference["sha256"])
        row = _read(memory, reference, files)
        reference = row.get("previous") if isinstance(row, dict) else None
    with memory.journal.locked(timeout=5):
        _, current_head, current_sequence, _ = memory._replay_state(copy_state=False)
        if (current_head, current_sequence) != (head, sequence): return False
        if (memory._fingerprint(pointer_path) if pointer_path.exists() else None) != pointer_before: return False
        if any(memory._fingerprint(path) != fingerprint for path, fingerprint in files.items()):
            raise SafetyError("RESEARCH_SEGMENT_HASH_CHANGED")
        atomic_json(safe_path(memory.root / POINTER), candidate)
        marker = _marker(memory)
        if marker is None or marker["initial_sequence"] == 0 and sequence > 0:
            marker = {"contract": VERSION, "evidence_type": memory.evidence_type,
                "initial_sequence": sequence, "initial_head": head}
            marker["hash"] = digest(marker)
            atomic_json(safe_path(memory.root / MARKER), marker)
    if check(memory, state, head, sequence) is None:
        raise SafetyError("RESEARCH_CHECKPOINT_READBACK_FAILED")
    return True


def migrate(memory):
    with memory._cache_lock:
        state, head, sequence, _ = memory._state(readonly=True, copy_state=False)
        reused = enabled(memory)
        verified = getattr(memory, "_research_pages_verified", None)
        if not reused or verified is None or (verified["root"]["sequence"], verified["root"]["head"]) != (sequence, head):
            if not update(memory, state, head, sequence): raise SafetyError("RESEARCH_CHECKPOINT_CHANGED")
        return {"contract": VERSION, "event_count": sequence, "head": head,
            "reused": reused, "original_events_unchanged": True,
            "cold_canonical_verification_required": True}


def _view(memory):
    with memory._cache_lock:
        state, head, sequence, _ = memory._state(readonly=True, copy_state=False)
        verified = getattr(memory, "_research_pages_verified", None)
        # An older valid checkpoint cannot answer current rows. Canonical
        # fallback is correct while a writer finishes publishing its pages.
        checkpoint = verified["checkpoint"] if verified and verified["root"]["sequence"] == sequence else None
        return state, head, sequence, checkpoint


def read_records(memory, table, record_ids):
    from .research_memory import TABLES
    if table not in TABLES or not isinstance(record_ids, (list, tuple)) or len(record_ids) > MAX_PAGE_ROWS:
        raise SafetyError("RESEARCH_BOUNDED_READ_INVALID")
    keys = [identifier(rid) for rid in record_ids]
    if len(set(keys)) != len(keys): raise SafetyError("RESEARCH_BOUNDED_READ_INVALID")
    state, head, sequence, checkpoint = _view(memory)
    try:
        rows = {rid: (_lookup(memory, table, checkpoint["tables"][table]["root"], rid) if checkpoint else state[table].get(rid)) for rid in keys}
    except (SafetyError, OSError):
        checkpoint = None
        memory._research_pages_verified = None
        memory.last_checkpoint_diagnostic = "RESEARCH_CHECKPOINT_READ_UNAVAILABLE"
        rows = {rid: state[table].get(rid) for rid in keys}
    if any(canonical(value) != canonical(state[table].get(rid)) for rid, value in rows.items()):
        raise SafetyError("RESEARCH_CHECKPOINT_CANONICAL_MISMATCH")
    return {"head": head, "event_count": sequence, "evidence_type": memory.evidence_type,
            "records": deepcopy(rows), "checkpoint_used": checkpoint is not None}


def _page(memory, table, page, *, head, sequence, limit, cursor, fallback=None):
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_ROWS: raise SafetyError("RESEARCH_BOUNDED_READ_INVALID")
    after = None
    if cursor is not None:
        if (not isinstance(cursor, dict) or set(cursor) != {"contract", "evidence_type", "head", "sequence", "table", "after"}
                or cursor["contract"] != "research-page-cursor-v1" or cursor["evidence_type"] != memory.evidence_type
                or cursor["head"] != head or cursor["sequence"] != sequence or cursor["table"] != table
                or not isinstance(cursor["after"], list) or len(cursor["after"]) != 2
                or cursor["after"][0] != digest(identifier(cursor["after"][1]))):
            raise SafetyError("RESEARCH_PAGE_CURSOR_STALE")
        after = tuple(cursor["after"])
    iterator = (_rows(memory, table, page["root"], after=after) if page else
        ((rid, value) for rid, value in sorted(fallback.items(), key=lambda row: (digest(row[0]), row[0]))
         if after is None or (digest(rid), rid) > after))
    rows = []
    for row in iterator:
        rows.append(row)
        if len(rows) > limit: break
    more = len(rows) > limit; rows = rows[:limit]
    next_cursor = ({"contract": "research-page-cursor-v1", "evidence_type": memory.evidence_type,
        "head": head, "sequence": sequence, "table": table, "after": [digest(rows[-1][0]), rows[-1][0]]} if more else None)
    return {"head": head, "event_count": sequence, "evidence_type": memory.evidence_type,
        "records": deepcopy(rows), "next_cursor": next_cursor, "total_rows": page["rows"] if page else len(fallback)}


def read_table_page(memory, table, *, limit=MAX_PAGE_ROWS, cursor=None):
    from .research_memory import TABLES
    if table not in TABLES: raise SafetyError("RESEARCH_BOUNDED_READ_INVALID")
    state, head, sequence, checkpoint = _view(memory)
    try:
        return _page(memory, table, checkpoint["tables"][table] if checkpoint else None,
            head=head, sequence=sequence, limit=limit, cursor=cursor, fallback=state[table])
    except (SafetyError, OSError) as error:
        if isinstance(error, SafetyError) and str(error) in {"RESEARCH_BOUNDED_READ_INVALID", "RESEARCH_PAGE_CURSOR_STALE", "INVALID_IDENTIFIER"}:
            raise
        memory._research_pages_verified = None
        memory.last_checkpoint_diagnostic = "RESEARCH_CHECKPOINT_READ_UNAVAILABLE"
        return _page(memory, table, None, head=head, sequence=sequence, limit=limit, cursor=cursor, fallback=state[table])


def event_id_record(memory, event_id):
    identifier(event_id)
    _, head, sequence, checkpoint = _view(memory)
    if checkpoint:
        try: result = _lookup(memory, "event_ids", checkpoint["event_ids"]["root"], event_id)
        except (SafetyError, OSError):
            checkpoint = None
            memory._research_pages_verified = None
            memory.last_checkpoint_diagnostic = "RESEARCH_CHECKPOINT_READ_UNAVAILABLE"
    if checkpoint is None:
        _, ids, _ = _prefix(memory, sequence); result = ids.get(event_id)
    return {"head": head, "event_count": sequence, "record": deepcopy(result)}


def novelty_page(memory, concept_id, *, client_id="default", section="exposures", limit=MAX_PAGE_ROWS, cursor=None):
    identifier(concept_id); identifier(client_id)
    if section not in {"exposures", "holds", "aliases"}: raise SafetyError("RESEARCH_BOUNDED_READ_INVALID")
    state, head, sequence, checkpoint = _view(memory)
    try:
        return _novelty_page(memory, state, head, sequence, checkpoint, concept_id,
            client_id=client_id, section=section, limit=limit, cursor=cursor)
    except (SafetyError, OSError) as error:
        if isinstance(error, SafetyError) and str(error) in {"RESEARCH_BOUNDED_READ_INVALID", "RESEARCH_PAGE_CURSOR_STALE", "RESEARCH_NOVELTY_ALIAS_UNCLEAR", "INVALID_IDENTIFIER"}:
            raise
        memory._research_pages_verified = None
        memory.last_checkpoint_diagnostic = "RESEARCH_CHECKPOINT_READ_UNAVAILABLE"
        return _novelty_page(memory, state, head, sequence, None, concept_id,
            client_id=client_id, section=section, limit=limit, cursor=cursor)


def _novelty_page(memory, state, head, sequence, checkpoint, concept_id, *, client_id, section, limit, cursor):
    if checkpoint:
        alias = _lookup(memory, "novelty-aliases", checkpoint["novelty"]["aliases"]["root"], concept_id)
        alias = alias or {"canonical_id": concept_id, "reason": None}
        groups = None
    else:
        aliases, groups = _novelty_rows(state)
        alias = aliases.get(concept_id, {"canonical_id": concept_id, "reason": None})
    canonical_id = alias["canonical_id"]
    if canonical_id is None: raise SafetyError("RESEARCH_NOVELTY_ALIAS_UNCLEAR")
    group = groups.get(canonical_id, {"aliases": {}, "clients": {}}) if groups is not None else None
    expected = (group["aliases"] if section == "aliases" else group["clients"].get(client_id, {}).get(section, {})) if group else {}
    page = None
    if checkpoint:
        saved = _lookup(memory, "novelty-groups", checkpoint["novelty"]["groups"]["root"], canonical_id)
        if saved:
            if section == "aliases": page = saved["aliases"]
            else:
                client = _lookup(memory, "novelty-clients", saved["clients"]["root"], client_id)
                if client: page = client[section]
    table = "novelty-members" if section == "aliases" else "novelty-" + section
    # Bind scope into the cursor so another client/type cannot reuse it.
    cursor_table = table + ":" + digest([client_id, canonical_id])
    result = _page(memory, table, page, head=head, sequence=sequence, limit=limit,
        cursor=None if cursor is None else dict(cursor, table=table) if isinstance(cursor, dict) and cursor.get("table") == cursor_table else cursor,
        fallback=expected)
    if cursor is not None and (not isinstance(cursor, dict) or cursor.get("table") != cursor_table): raise SafetyError("RESEARCH_PAGE_CURSOR_STALE")
    if result["next_cursor"]: result["next_cursor"]["table"] = cursor_table
    return {**result, "concept_id": concept_id, "canonical_id": canonical_id, "client_id": client_id, "section": section}
