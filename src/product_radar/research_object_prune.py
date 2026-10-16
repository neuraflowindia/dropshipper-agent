"""Remove research lookup pages that no saved checkpoint uses any more.

``research-v1/objects`` holds derived lookup pages (see ``research_segments``):
every published checkpoint writes new pages and older pages are left behind.
They are rebuilt from the original research events whenever needed, so they
are never evidence. This module removes only pages that are

* not reachable from the current published checkpoint pointer (every object
  it names, its event-segment chain and all nested lookup trees are kept), and
* older than a grace period, so pages a writer has just saved for a pointer
  it has not published yet are never touched.

The live set is worked out without holding the writer lock; deletion happens
under the shared journal lock and only if the pointer is still the same one.
A writer reads back every page its new pointer names (checkpoint pages and the
event segments it adds) before that lock and rechecks their files under it, so
a page removed in between is never published: that update fails and the next
one writes the page again. Original events,
the pointer and the activation marker are never touched. No website, provider
or model is contacted.
"""

from datetime import datetime, timezone
import os
import re
import time

from .models import SafetyError
from .storage import safe_path


_OBJECT = re.compile(r"[a-f0-9]{64}\.json\Z")
_NESTED = {"novelty-groups": ("aliases", "clients"), "novelty-clients": ("exposures", "holds")}


def live_object_names(memory, pointer):
    """Every object name the published ``pointer`` reaches (fail closed)."""
    from .research_segments import _read, _path
    live = set()

    def visit(ref):
        _path(memory, ref)  # validates the reference shape
        name = ref["sha256"] + ".json"
        if name in live:
            return None
        live.add(name)
        return _read(memory, ref)

    def tree(table, ref):
        if ref is None:
            return
        node = visit(ref)
        if node is None:
            return
        contract = node.get("contract") if isinstance(node, dict) else None
        if contract == "research-branch-v1" and node.get("table") == table and isinstance(node.get("children"), dict):
            for child in node["children"].values():
                tree(table, child)
        elif contract == "research-page-v1" and node.get("table") == table and isinstance(node.get("rows"), list):
            for row in node["rows"]:
                if table in _NESTED:
                    _nested(table, row)
        else:
            raise SafetyError("RESEARCH_PRUNE_PAGE_UNKNOWN")

    def _nested(table, row):
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[1], dict):
            raise SafetyError("RESEARCH_PRUNE_PAGE_UNKNOWN")
        child_table = {"aliases": "novelty-members", "clients": "novelty-clients",
                       "exposures": "novelty-exposures", "holds": "novelty-holds"}
        for name in _NESTED[table]:
            page = row[1].get(name)
            if not isinstance(page, dict) or set(page) != {"root", "rows"}:
                raise SafetyError("RESEARCH_PRUNE_PAGE_UNKNOWN")
            tree(child_table[name], page["root"])

    segment = pointer["last_segment"]
    while segment is not None:
        row = visit(segment)
        if row is None:
            break
        if not isinstance(row, dict) or row.get("contract") != "research-event-segment-v1":
            raise SafetyError("RESEARCH_PRUNE_SEGMENT_UNKNOWN")
        segment = row["previous"]
    checkpoint = visit(pointer["checkpoint"])
    if (not isinstance(checkpoint, dict) or checkpoint.get("contract") != "research-checkpoint-v1"
            or not isinstance(checkpoint.get("tables"), dict) or not isinstance(checkpoint.get("novelty"), dict)):
        raise SafetyError("RESEARCH_PRUNE_CHECKPOINT_UNKNOWN")
    for table, page in checkpoint["tables"].items():
        tree(table, page["root"])
    tree("event_ids", checkpoint["event_ids"]["root"])
    tree("novelty-aliases", checkpoint["novelty"]["aliases"]["root"])
    tree("novelty-groups", checkpoint["novelty"]["groups"]["root"])
    return live


def _pointer_and_fingerprint(memory):
    from .research_segments import POINTER, _pointer
    path = safe_path(memory.root / POINTER)
    return _pointer(memory), memory._fingerprint(path)


def prune_research_objects(memory, *, grace_seconds, now=None):
    """Remove unreachable lookup pages older than ``grace_seconds``.

    Returns a small record: state (``removed``, ``nothing_to_remove``,
    ``not_enabled`` or ``kept`` with a reason), counts and bytes. A damaged or
    unreadable checkpoint, or a busy journal lock, raises (``SafetyError`` or
    ``OSError``) before anything is removed; the caller records it.
    """
    from .research_segments import enabled
    if type(grace_seconds) is not int or grace_seconds < 3600:
        raise SafetyError("RESEARCH_PRUNE_SETTINGS_INVALID")
    result = {"state": "nothing_to_remove", "reason": None, "live_objects": 0, "removed_objects": 0,
              "removed_bytes": 0, "kept_unreachable_recent": 0}
    if not enabled(memory):
        return dict(result, state="not_enabled")
    folder = safe_path(memory.root, "research-v1", "objects")
    if not folder.is_dir():
        return result
    pointer, fingerprint = _pointer_and_fingerprint(memory)
    live = live_object_names(memory, pointer)
    result["live_objects"] = len(live)
    cutoff = (now if now is not None else time.time()) - grace_seconds
    candidates = []
    with os.scandir(folder) as entries:
        for entry in entries:
            if not _OBJECT.fullmatch(entry.name) or entry.name in live:
                continue
            stat = entry.stat(follow_symlinks=False)
            if not entry.is_file(follow_symlinks=False):
                continue
            if stat.st_mtime >= cutoff:
                result["kept_unreachable_recent"] += 1
                continue
            candidates.append((entry.name, stat))
    if not candidates:
        return result
    removed, size = 0, 0
    with memory.journal.locked(timeout=5):
        # The pointer can only change under this lock. A different pointer
        # may name some candidates again: keep everything and retry later.
        current, current_fingerprint = _pointer_and_fingerprint(memory)
        if current != pointer or current_fingerprint != fingerprint:
            return dict(result, state="kept", reason="RESEARCH_CHECKPOINT_CHANGED")
        for name, stat in candidates:
            path = folder / name
            try:
                now_stat = path.lstat()
            except FileNotFoundError:
                continue
            if (now_stat.st_ino, now_stat.st_size, now_stat.st_mtime_ns) != (stat.st_ino, stat.st_size, stat.st_mtime_ns):
                continue  # rewritten meanwhile: keep it
            os.unlink(path)
            removed += 1; size += stat.st_size
    return dict(result, state="removed" if removed else "nothing_to_remove", removed_objects=removed, removed_bytes=size)


def object_folder_bytes(memory):
    """Total bytes and count of saved lookup pages (diagnostics only)."""
    folder = safe_path(memory.root, "research-v1", "objects")
    total = count = 0
    if folder.is_dir():
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size; count += 1
    return {"objects": count, "bytes": total, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
