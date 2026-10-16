"""Local research records, independent of money and client publication history.

One existing project lock protects short commits. Immutable, hash-linked events
are authoritative; the index is disposable. No provider or model is called here.
"""

from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
import errno
import os
import re
import stat
import threading
import time

from .models import SafetyError, canonical, decode_json, digest, identifier, prepared_request, request_stage, timestamp, utc_now
from .providers.apify import map_prepared_rows
from .storage import atomic_json, load_json, safe_path
from .source_deadline import local_source_work

TABLES = ("captures", "observations", "descriptions", "concepts", "associations", "assessments", "problems", "jobs",
          "query_outcomes", "query_tasks", "batch_plans", "source_checks", "catalogue_additions", "supplier_assessments", "trends_evidence",
          "issue_reservations", "published_history", "market_reviews", "batch_runs", "brand_reviews")
EVENT_LIMIT = 8 * 1024 * 1024
INDEX_LIMIT = 16 * 1024 * 1024


def load_event(path, limit=EVENT_LIMIT):
    """Read an event in the already checked events directory, not an index.

    Replay validates the parent once and the complete original file manifest.
    Repeating all mounted-drive ancestry checks for every historical file made
    a 24 MB cold read take two minutes. Open without following a final symlink
    or waiting on a substituted special file, then verify its actual descriptor
    before and after the bounded read. Replay also checks the original manifest
    fingerprint, sequence, hash, namespace and dependencies, as before.
    """
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SafetyError("RESEARCH_EVENTS_INVALID")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > limit:
            raise SafetyError("SAVED_FILE_SIZE_LIMIT")
        fingerprint = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if fingerprint(before) != fingerprint(after) or len(raw) != before.st_size:
            raise SafetyError("RESEARCH_EVENTS_INVALID")
        return decode_json(raw)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SafetyError("SYMLINK_PATH_REFUSED") from None
        raise SafetyError("SAVED_FILE_UNAVAILABLE") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def key(prefix, value):
    return prefix + "-" + digest(value)[:40]


class ResearchMemory:
    def __init__(self, root, journal, *, evidence_type="captured"):
        if evidence_type not in ("captured", "synthetic"):
            raise SafetyError("RESEARCH_NAMESPACE_INVALID")
        self.root = safe_path(root)
        self.journal = journal
        self.evidence_type = evidence_type
        # This process-local cache contains only replay-verified event data.
        # The disposable on-disk index is never read as evidence or authority.
        self._verified = None
        self._cache_lock = threading.RLock()
        self.last_commit_diagnostic = None
        self.last_read_diagnostic = None
        self.last_checkpoint_diagnostic = None
        self._projection_defer_depth = 0
        self._projection_dirty = False
        # A lookup copy known to be too large: its size, the chain position it
        # was measured at, and how much this instance's later events could
        # have shrunk it at most (replaced records). Speed only, never evidence.
        self._index_oversize = None
        # The verified state at a published checkpoint's event count, kept
        # from one cold replay for that same call's checkpoint check only.
        self._replay_capture = None

    @contextmanager
    def defer_lookup_projection(self, *, flush_if=None):
        """Save original events now; coalesce only their disposable lookup copy.

        This is instance-local and nested. An interrupted round leaves all its
        canonical events readable, without delaying cancellation for optional
        work. A later successful outer exit verifies the then-current chain,
        including any independent appends, before attempting one projection.
        """
        if flush_if is not None and not callable(flush_if):
            raise SafetyError("RESEARCH_PROJECTION_GUARD_INVALID")
        with self._cache_lock:
            self._projection_defer_depth += 1
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            with self._cache_lock:
                self._projection_defer_depth -= 1
                if succeeded and self._projection_defer_depth == 0 and self._projection_dirty:
                    try:
                        if flush_if is None or flush_if():
                            self._flush_lookup_projection()
                    except (OSError, SafetyError) as error:
                        # The original commits already succeeded. A lookup-copy
                        # failure is diagnostic, never another research action.
                        self.last_commit_diagnostic = (str(error) if isinstance(error, SafetyError)
                                                       else "RESEARCH_INDEX_WRITE_UNAVAILABLE")
                        from .operations import record_event
                        record_event("research_lookup_save_outcome", state="waiting",
                                     reason=self.last_commit_diagnostic, useful_result=False)

    @local_source_work("local_apply")
    def _flush_lookup_projection(self):
        from .operations import _ACTIVE, LOCAL_PHASE_DEADLINES, record_event
        recorder = _ACTIVE.get()
        # Do not replace an enclosing handler/stage clock with an optional
        # nested phase. Normal admitted round exits have already ended it.
        logged = recorder is not None and recorder.phase is None
        started = time.monotonic()
        if logged:
            record_event("phase_started", phase="research_lookup_save",
                         deadline_seconds=LOCAL_PHASE_DEADLINES["research_lookup_save"], useful_result=False)
        try:
            state, head, _, _ = self._state(readonly=True, copy_state=False)
            self._projection_dirty = not self._write_index(state, head)
        except (OSError, SafetyError) as error:
            self.last_commit_diagnostic = (str(error) if isinstance(error, SafetyError)
                                           else "RESEARCH_INDEX_WRITE_UNAVAILABLE")
        finally:
            outcome = {"state": "waiting" if self._projection_dirty else "saved",
                       "reason": self.last_commit_diagnostic, "useful_result": False,
                       "timings": {"research_lookup_save": time.monotonic() - started}}
            if logged:
                record_event("phase_finished", phase="research_lookup_save", next_phase=None,
                             deadline_seconds=None, **outcome)
            else:
                # A durable diagnostic must not replace the enclosing live
                # phase, reset its clock, or consume a website deadline.
                record_event("research_lookup_save_outcome", **outcome)

    def _fingerprint(self, path):
        """Detect old-event replacement/rewrites without rereading JSON bytes.

        Cooperating writers hold the shared journal lock. These metadata checks
        are an efficiency aid, not protection against a malicious OS account.
        ctime catches a rewritten file even if its mtime is restored manually.
        """
        try:
            value = path.lstat()
        except OSError as error:
            self.last_read_diagnostic = {"operation": "event_manifest", "errno": error.errno}
            raise SafetyError("SAVED_FILE_UNAVAILABLE") from None
        return ResearchMemory._stat_fingerprint(value)

    @staticmethod
    def _stat_fingerprint(value):
        if stat.S_ISLNK(value.st_mode):
            raise SafetyError("SYMLINK_PATH_REFUSED")
        if not stat.S_ISREG(value.st_mode):
            raise SafetyError("RESEARCH_EVENTS_INVALID")
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    def _state(self, *, readonly=False, copy_state=True):
        with self._cache_lock:
            from .research_segments import check, prefix_target
            # A cold check of an older checkpoint needs the verified state at
            # that checkpoint's event count. Keep it from this one replay
            # instead of replaying the whole history a second time.
            try:
                result = self._replay_state(readonly=readonly, copy_state=copy_state,
                                            capture_at=prefix_target(self) if self._verified is None else None)
                # This exact manifest has just been enumerated and verified by
                # replay under our cache lock. The checkpoint reader can compare
                # its original-event fingerprints with that same read, rather
                # than lstat every original a second time. Derived object files
                # still get fresh checks; every later read/commit rescans events.
                check(self, result[0], result[1], result[2],
                      _verified_manifest=self._verified["manifest"])
                return result
            finally:
                self._replay_capture = None

    def _replay_state(self, *, readonly=False, copy_state=True, capture_at=None):
        """Verified state from the original events (only new ones when warm).

        ``capture_at``: on a full (cold) replay that passes this event count
        before its end, also keep a copy of the state, head and event proofs
        at that count in ``_replay_capture`` for the checkpoint check. It is
        the same verified read, never another source of authority.
        """
        if not readonly:
            self.journal._require_lock()
        events = safe_path(self.root, "events")
        # scandir can reuse metadata returned with each directory entry on
        # mounted Windows filesystems. Keep the same per-file fingerprint and
        # hash-chain verification; do not trust a directory timestamp or index.
        try:
            directory = os.scandir(events)
        except FileNotFoundError:
            directory = None
        except OSError as error:
            self.last_read_diagnostic = {"operation": "event_directory", "errno": error.errno}
            raise SafetyError("SAVED_FILE_UNAVAILABLE") from None
        if directory is None:
            manifest = ()
        else:
            try:
                with directory as entries:
                    manifest = tuple(sorted(((Path(entry.path), self._stat_fingerprint(entry.stat(follow_symlinks=False)))
                                             for entry in entries if entry.name.endswith(".json")), key=lambda row: row[0].name))
            except OSError as error:
                # A vanished entry is a changing read, not an empty namespace.
                self.last_read_diagnostic = {"operation": "event_manifest", "errno": error.errno}
                raise SafetyError("SAVED_FILE_UNAVAILABLE") from None
        previous = self._verified
        prefix_unchanged = (previous is not None and previous["evidence_type"] == self.evidence_type
                            and len(manifest) >= len(previous["manifest"])
                            and manifest[:len(previous["manifest"])] == previous["manifest"])
        if prefix_unchanged:
            if len(manifest) == len(previous["manifest"]):
                return (deepcopy(previous["state"]) if copy_state else previous["state"]), previous["head"], previous["sequence"], (dict(previous["ids"]) if copy_state else previous["ids"])
            state = deepcopy(previous["state"])
            head, sequence, event_ids = previous["head"], previous["sequence"], dict(previous["ids"])
            pending = manifest[len(previous["manifest"]):]
        else:
            state = {table: {} for table in TABLES}
            head, sequence, event_ids = "0" * 64, 0, {}
            pending = manifest
        # Only a full replay sees every event's own proof; the checkpoint at
        # the final count needs no copy (its check uses this state directly).
        capture = (capture_at if not prefix_unchanged and type(capture_at) is int
                   and 0 < capture_at < len(manifest) else None)
        proofs = {} if capture is not None else None
        for path, fingerprint in pending:
            event = load_event(path, EVENT_LIMIT)
            try:
                body = {k: v for k, v in event.items() if k != "hash"}
                if (event["seq"] != sequence + 1 or type(event["seq"]) is not int
                        or path.name != f"{event['seq']:08d}.json"
                        or event["previous"] != head or digest(body) != event["hash"]
                        or event["evidence_type"] != self.evidence_type
                        or event["id"] in event_ids):
                    raise ValueError
                identifier(event["id"])
                timestamp(event["at"])
                self._apply(state, event["changes"])
                head, sequence = event["hash"], event["seq"]
                event_ids[event["id"]] = digest(event["changes"])
            except (KeyError, TypeError, ValueError):
                raise SafetyError("RESEARCH_EVENTS_INVALID") from None
            if self._fingerprint(path) != fingerprint:
                raise SafetyError("RESEARCH_EVENTS_INVALID")
            if proofs is not None:
                proofs[event["id"]] = {"sequence": sequence, "event_hash": head, "changes_hash": event_ids[event["id"]]}
                if sequence == capture:
                    self._replay_capture = {"sequence": sequence, "head": head, "state": deepcopy(state),
                        "ids": proofs, "evidence_type": self.evidence_type, "manifest": manifest[:sequence]}
                    proofs = None
        # A valid shorter chain is still a rollback relative to this instance's
        # verified history. A fresh process has no independent tail checkpoint;
        # this cache does not claim to detect a valid rollback after restart.
        if previous is not None and sequence < previous["sequence"]:
            raise SafetyError("RESEARCH_EVENTS_INVALID")
        self._verified = {"manifest": manifest, "state": state, "head": head,
                          "sequence": sequence, "ids": event_ids, "evidence_type": self.evidence_type}
        return (deepcopy(state) if copy_state else state), head, sequence, (dict(event_ids) if copy_state else event_ids)

    @staticmethod
    def _apply(state, changes):
        if not isinstance(changes, list) or not changes:
            raise SafetyError("RESEARCH_CHANGES_INVALID")
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"table", "id", "value", "previous_hash"}:
                raise SafetyError("RESEARCH_CHANGE_INVALID")
            table, record_id = change["table"], identifier(change["id"])
            if table not in TABLES or not isinstance(change["value"], dict):
                raise SafetyError("RESEARCH_CHANGE_INVALID")
            old = state[table].get(record_id)
            if (digest(old) if old is not None else None) != change["previous_hash"]:
                raise SafetyError("RESEARCH_DEPENDENCY_CHANGED")
            if table in ("captures", "observations") and old is not None and old != change["value"]:
                raise SafetyError("RESEARCH_CAPTURE_CONFLICT")
            state[table][record_id] = deepcopy(change["value"])

    @local_source_work("local_read")
    def snapshot(self):
        # Each event is atomically published and immutable. A verified prefix
        # is a consistent point-in-time read even if a cooperating writer adds
        # a later event while it is replayed. Do not hold the financial/writer
        # lock over a cold scan of thousands of saved files. Actual mutation
        # still rechecks the current chain and dependencies under that lock.
        for attempt in range(3):
            try:
                state, _, _, _ = self._state(readonly=True)
                self.last_read_diagnostic = None
                return state
            except SafetyError as error:
                # A fresh verified read can recover a transient mounted-drive
                # error. Never return a cached prefix on failure and never
                # retry malformed, changed or untrusted event contents.
                if str(error) != "SAVED_FILE_UNAVAILABLE" or attempt == 2:
                    raise

    def verified_sequence(self):
        """How many saved events this instance has verified, its own saves
        included, read without touching any file (``None`` before a read)."""
        with self._cache_lock:
            return None if self._verified is None else self._verified["sequence"]

    def verified_position(self):
        """``(event count, head)`` this instance has verified, read together
        without touching any file (``None`` before a read or after a lost one)."""
        with self._cache_lock:
            return None if self._verified is None else (self._verified["sequence"], self._verified["head"])

    def snapshot_with_provenance(self):
        """Read the same verified prefix and its exact head without any write.

        The local cache lock keeps this state's metadata paired with the read;
        a later event from another process does not change the verified prefix.
        This is inspection provenance, not mutation/launch authority.
        """
        with self._cache_lock:
            state = self.snapshot()
            return {"state": state, "head": self._verified["head"],
                    "event_count": self._verified["sequence"], "evidence_type": self.evidence_type}

    @local_source_work("local_apply")
    def commit(self, event_id, changes, *, validate=None):
        # Serialise this instance's cache users, not independent read-only
        # processes. RLock permits the internal replay while the same writer
        # owns the cache through its atomic event/cache update.
        with self._cache_lock:
            for attempt in range(3):
                try:
                    return self._commit(event_id, changes, validate=validate)
                except SafetyError as error:
                    # Only a proven local admission failure is retried. No
                    # provider or model call exists in this method. Each try
                    # rechecks the chain, event ID and exact dependencies.
                    if str(error) != "RUNNER_ALREADY_ACTIVE" or attempt == 2:
                        raise

    def _commit(self, event_id, changes, *, validate=None):
        identifier(event_id)
        if validate is not None and not callable(validate):
            raise SafetyError("RESEARCH_COMMIT_VALIDATOR_INVALID")
        # Warm a cold verified cache without blocking another short writer.
        # The locked replay below checks any intervening appended events.
        if self._verified is None:
            self.snapshot()
        with self.journal.locked(timeout=5):
            state, head, sequence, ids = self._state()
            if event_id in ids:
                if ids[event_id] != digest(changes):
                    raise SafetyError("RESEARCH_EVENT_CONFLICT")
                self.last_commit_diagnostic = None
                return False
            if validate is not None:
                # Read-only binding check inside the same short writer lock.
                # No provider/model calls or nested lock acquisition here.
                # An exact already-committed event above remains a replay even
                # when its former dependencies have since been superseded.
                validate(deepcopy(state))
            oversize = self._index_oversize
            if oversize is not None and oversize["sequence"] == sequence:
                # A replaced record can shrink the copy by at most its old size.
                shrink = sum(len(canonical(state[row["table"]][row["id"]])) for row in changes
                             if isinstance(row, dict) and row.get("table") in state
                             and isinstance(row.get("id"), str) and row["id"] in state[row["table"]])
                oversize = dict(oversize, sequence=sequence + 1, shrink=oversize["shrink"] + shrink)
            self._apply(state, changes)
            event = {"seq": sequence + 1, "id": event_id, "at": utc_now(),
                     "evidence_type": self.evidence_type, "previous": head, "changes": changes}
            event["hash"] = digest(event)
            if len(canonical(event)) > EVENT_LIMIT:
                raise SafetyError("RESEARCH_EVENT_SIZE_LIMIT")
            event_path = safe_path(self.root, "events") / f"{sequence + 1:08d}.json"
            try:
                atomic_json(event_path, event)
            except (OSError, SafetyError):
                # A filesystem reply can be lost after its atomic rename. Only
                # the exact intended event is proof; never retry a different
                # event or swallow a genuinely failed canonical write.
                if not event_path.exists() or load_json(event_path, EVENT_LIMIT) != event:
                    raise
            ids[event_id] = digest(event["changes"])
            # Update verified memory immediately after the authoritative event
            # write, before the optional index. A failed index write cannot
            # erase committed data or cause the next read to replay old files.
            try:
                fingerprint = self._fingerprint(event_path)
            except SafetyError:
                # The event is saved but this instance could not check its
                # file: forget the verified copy, so no reader keeps an older
                # copy as current; the next read verifies from the files.
                self._verified = None
                raise
            self._verified = {"manifest": self._verified["manifest"] + ((event_path, fingerprint),),
                              "state": deepcopy(state), "head": event["hash"],
                              "sequence": sequence + 1, "ids": dict(ids), "evidence_type": self.evidence_type}
            self._index_oversize = oversize if oversize is not None and oversize["sequence"] == sequence + 1 else None
        # The optional multi-megabyte index is never read as authority. Write
        # it outside the short canonical/financial lock so copying a dashboard
        # cache cannot block a model answer, product capture or billing guard.
        # Concurrent projections may leave an older index; verified event
        # replay, not this projection, determines every actual decision.
        self._projection_dirty = True
        if self._projection_defer_depth == 0:
            self._projection_dirty = not self._write_index(state, event["hash"])
        return True

    def _write_index(self, state, head):
        self.last_commit_diagnostic = None
        from .research_segments import enabled, update
        if enabled(self):
            try:
                result = update(self, state, head, self._verified["sequence"])
                if not result: self.last_commit_diagnostic = "RESEARCH_CHECKPOINT_CHANGED"
                return result
            except (OSError, SafetyError) as error:
                # Original event already committed. A projection failure never
                # erases it, resets novelty, or repeats provider/model work.
                self.last_commit_diagnostic = str(error) if isinstance(error, SafetyError) else "RESEARCH_CHECKPOINT_WRITE_UNAVAILABLE"
                return False
        sequence = self._verified["sequence"]
        known = self._index_oversize
        if known is not None and known["sequence"] == sequence and known["bytes"] - known["shrink"] > INDEX_LIMIT:
            # Still certainly too large: skip building the whole text only to refuse it.
            self.last_commit_diagnostic = "RESEARCH_INDEX_SIZE_LIMIT"
            return False
        index = {"head": head, "state": state}
        size = len(canonical(index))
        if size > INDEX_LIMIT:
            self._index_oversize = {"bytes": size, "sequence": sequence, "shrink": 0}
            self.last_commit_diagnostic = "RESEARCH_INDEX_SIZE_LIMIT"
            return False
        self._index_oversize = None
        try:
            atomic_json(self.root / "index.json", index)
        except (OSError, SafetyError) as error:
            # This is a disposable projection, not another canonical event.
            # Logging an event here would recursively attempt the same index.
            self.last_commit_diagnostic = (str(error) if isinstance(error, SafetyError)
                                           else "RESEARCH_INDEX_WRITE_UNAVAILABLE")
            return False
        return True

    def rebuild_index(self):
        """Rebuild only from the verified chain, without calls or new events."""
        if self._verified is None:
            self._state(readonly=True)
        with self.journal.locked(timeout=5):
            state, head, _, _ = self._state()
        return self._write_index(state, head)

    def migrate_segments(self):
        """Explicit local reader-first migration; original events stay exact."""
        from .research_segments import migrate
        return migrate(self)

    def read_records(self, table, record_ids):
        from .research_segments import read_records
        return read_records(self, table, record_ids)

    def read_table_page(self, table, *, limit=128, cursor=None):
        from .research_segments import read_table_page
        return read_table_page(self, table, limit=limit, cursor=cursor)

    def event_id_record(self, event_id):
        from .research_segments import event_id_record
        return event_id_record(self, event_id)

    def novelty_page(self, concept_id, *, client_id="default", section="exposures", limit=128, cursor=None):
        from .research_segments import novelty_page
        return novelty_page(self, concept_id, client_id=client_id, section=section, limit=limit, cursor=cursor)

    def put(self, table, record_id, value, *, previous=None, event_id=None):
        return self.commit(event_id or key("put", [table, record_id, value]),
                           [change(table, record_id, value, previous)])


class UnchangedSnapshot:
    """One full copy of the research state, read again only after something
    was saved since it was read (a full copy is costly: seconds on a large
    store). A save by another process is taken in by this process's next read
    or save, as any earlier snapshot would be. It is a read, never authority:
    every save still checks its exact dependencies under the writer lock.
    Callers only read the copy; it is shared between them, never changed.
    """

    def __init__(self, memory, provenance=None):
        """``provenance``: an earlier ``snapshot_with_provenance()`` read of this
        same memory, used until something is saved after it (optional)."""
        self.memory, self.state, self.position = memory, None, None
        if provenance is not None:
            if (not isinstance(provenance, dict) or not isinstance(provenance.get("state"), dict)
                    or provenance.get("evidence_type") != memory.evidence_type
                    or type(provenance.get("event_count")) is not int or not isinstance(provenance.get("head"), str)):
                raise SafetyError("RESEARCH_SNAPSHOT_PROVENANCE_INVALID")
            self.state, self.position = provenance["state"], (provenance["event_count"], provenance["head"])

    def __call__(self):
        # The position and head are compared together; the copy and its
        # position come from one read, so a save in between is never missed.
        position = self.memory.verified_position()
        if self.state is None or position is None or position != self.position:
            read = self.memory.snapshot_with_provenance()
            self.state, self.position = read["state"], (read["event_count"], read["head"])
        return self.state


def change(table, record_id, value, previous=None):
    return {"table": table, "id": record_id, "value": value,
            "previous_hash": digest(previous) if previous is not None else None}


def source_items(source, mapped):
    """Expand creative cards without multiplying their parent ad identity."""
    for record in mapped["records"]:
        if source == "meta":
            presentation = record.get("presentation", {})
            cards = presentation.get("cards") or [dict(presentation, card_index="parent")]
            # A deeper paid read of the same ad is its own current item, not a
            # replacement of the free read's; the Meta count joins them by ad.
            prefix = record["source_route"] + ":" if record.get("source_route") else ""
            for card in cards:
                semantic = {k: card[k] for k in ("title", "body", "link_description", "link_url", "original_image_url") if k in card}
                # Parent text can qualify every card; it is context, not an
                # assumed product price or a separate extra recommendation.
                semantic["parent_context"] = {k: presentation[k] for k in ("title", "body", "link_description") if k in presentation}
                yield prefix + str(record["ad_id"]) + ":" + str(card["card_index"]), semantic, {
                    "ad_id": record["ad_id"], "advertiser_id": record["advertiser_id"],
                    **({"advertiser_url": record["advertiser_url"]} if record.get("advertiser_url") else {}),
                    "active": record["active"], "started_at": record["started_at"],
                    "url": record["library_url"], "limitations": record["limitations"],
                    **({"source_route": record["source_route"]} if record.get("source_route") else {})}
        elif source == "amazon":
            yield record["identity"], {"title": record["title"], "product_url": record["product_url"]}, {
                "asin": record["identity"], "list_type": record["list_type"], "position": record["position"],
                "url": record["product_url"], "price": record["price_observation"],
                "limitations": record["defects"], "requested_list_url": record["requested_list_url"]}
        # Trends series is validation evidence, never a new product listing.


def import_capture(memory, *, source, acquisition_id, observed_at, mapped, capture_hash, reference, recovery=None):
    if source not in ("meta", "amazon", "trends"):
        raise SafetyError("RESEARCH_SOURCE_INVALID")
    identifier(acquisition_id)
    timestamp(observed_at)
    if recovery is not None and (not isinstance(recovery, dict)
            or recovery.get("version") != "source-deadline-reparse-v1"
            or set(recovery) != {"version", "original_receipt_sha256", "original_artifacts_hash"}
            or any(not isinstance(recovery[field], str) or not re.fullmatch(r"[a-f0-9]{64}", recovery[field])
                   for field in ("original_receipt_sha256", "original_artifacts_hash"))):
        raise SafetyError("RESEARCH_CAPTURE_RECOVERY_INVALID")
    capture_id = key("capture", [source, acquisition_id] if recovery is None else [source, acquisition_id, recovery])
    state = memory.snapshot()
    old = state["captures"].get(capture_id)
    if old:
        if old["capture_hash"] != capture_hash or old["mapping_hash"] != digest(mapped):
            raise SafetyError("RESEARCH_CAPTURE_CONFLICT")
        return {"new_observations": 0, "reused_capture": True, "capture_id": capture_id}
    capture = {"source": source, "acquisition_id": acquisition_id, "observed_at": observed_at,
               "capture_hash": capture_hash, "mapping_hash": digest(mapped), "reference": reference,
               "imported_at": utc_now(), "mapping_status": mapped["status"],
               "rejected": mapped["rejected"], "mapper_version": "existing-source-mappers-v1"}
    if recovery is not None:
        capture["recovery"] = deepcopy(recovery)
    changes = [change("captures", capture_id, capture)]
    for reference_id, semantic, facts in source_items(source, mapped):
        observation_id = key("obs", [source, acquisition_id, reference_id] if recovery is None else
                              [source, acquisition_id, recovery, reference_id])
        semantic_id = key("item", semantic)
        changes.append(change("observations", observation_id, {
            "source": source, "acquisition_id": acquisition_id, "source_item": reference_id,
            "capture_id": capture_id, "observed_at": observed_at, "semantic_id": semantic_id,
            "semantic": semantic, "facts": facts}))
    memory.commit(capture_id, changes)
    return {"new_observations": len(changes) - 1, "reused_capture": False, "capture_id": capture_id}


def saved_attempt_mapping(memory, attempt_id, *, timeout=0):
    """A committed provider capture, verified against the ledger, and its mapping.

    Read-only: no key, remote access or research event. Returns (attempt,
    prepared request, capture manifest, mapped rows, captured rows).
    """
    identifier(attempt_id)
    journal = memory.journal
    with journal.locked(timeout=timeout):
        attempt = journal.state()[1].get(attempt_id)
        if not attempt or not attempt.get("capture") or not attempt.get("dataset_id"):
            raise SafetyError("SAVED_CAPTURE_REQUIRED")
        folder = safe_path(journal.root, attempt_id)
        # Attempt state saves the bare request; dated preparation files use an
        # outer envelope. Only the committed attempt-state form is imported.
        prepared = prepared_request(load_json(folder / "prepared.json"))
        manifest = load_json(folder / "capture_manifest.json")
        rows = load_json(folder / "captured_rows.json", 50 * 1024 * 1024)
        if (prepared.attempt_id != attempt_id or prepared.config_hash != attempt["config_hash"]
                or digest(rows) != attempt["capture"] or manifest["captured_hash"] != attempt["capture"]
                or manifest["dataset_id"] != attempt["dataset_id"]):
            raise SafetyError("SAVED_CAPTURE_MISMATCH")
        mapped = map_prepared_rows(rows, prepared)
        if mapped != load_json(folder / "mapped_rows.json", 16 * 1024 * 1024):
            raise SafetyError("SAVED_MAPPING_CHANGED")
    if memory.evidence_type != "captured":
        raise SafetyError("RESEARCH_NAMESPACE_INVALID")
    return attempt, prepared, manifest, mapped, rows


def import_saved_attempt(memory, attempt_id):
    """Read-only reuse of a committed benchmark, with no key or remote access."""
    attempt, prepared, manifest, mapped, _ = saved_attempt_mapping(memory, attempt_id)
    return import_capture(memory, source=request_stage(prepared), acquisition_id=attempt["dataset_id"],
                          observed_at=manifest["observed_at"], mapped=mapped, capture_hash=attempt["capture"],
                          reference={"attempt_id": attempt_id, "file": "captured_rows.json"})


def import_direct_marketplace(memory, folder, *, saved_only_recovery=False):
    """Import a verified free-browser acquisition without a paid-run fiction.

    Keeps list/search scope, original displayed quote, pack text and photo URL.
    Generic search context is never promoted to a bestseller or sales signal.
    Failed, challenged and explicit no-result attempts remain separate captures
    with no fabricated product observations or negative-demand conclusion.
    """
    from .marketplace_browser import VERSION, reconcile_marketplace, replay_marketplace
    if type(saved_only_recovery) is not bool:
        raise SafetyError("MARKETPLACE_RECOVERY_OPTION_INVALID")
    folder = safe_path(folder)
    receipt = reconcile_marketplace(folder) if saved_only_recovery else replay_marketplace(folder)
    if memory.evidence_type != receipt["evidence_type"]:
        raise SafetyError("RESEARCH_NAMESPACE_INVALID")
    request = receipt["request"]
    source = "flipkart" if request["route"] == "flipkart-search" else "amazon"
    acquisition_id = receipt["acquisition_id"]
    observed_at = receipt["observed_at"]
    timestamp(observed_at)
    parsed_file = receipt.get("parsed_file", "parsed.json")
    parsed = load_json(folder / parsed_file, 2 * 1024 * 1024) if parsed_file in receipt["artifacts"] else None
    usable = receipt["state"] in ("usable", "partial")
    records = parsed["records"] if usable and parsed else []
    capture_hash = digest(receipt)
    capture_id = key("capture", [source, acquisition_id])
    if receipt.get("recovery") is not None:
        # Do not overwrite an earlier failed import of this same request.
        # The acquisition is unchanged; only the verified parsing attribution
        # has a separate immutable capture identity.
        capture_id = key("capture", [source, acquisition_id, receipt["recovery"]])
    mapping = {"version": VERSION, "source": source, "state": receipt["state"], "records": records,
               "rejected": parsed["rejected"] if parsed else [], "request": request}
    state = memory.snapshot()
    old = state["captures"].get(capture_id)
    if old:
        if old["capture_hash"] != capture_hash or old["mapping_hash"] != digest(mapping):
            raise SafetyError("RESEARCH_CAPTURE_CONFLICT")
        return {"capture_id": capture_id, "new_observations": 0, "reused_capture": True,
                "observation_ids": sorted(i for i, o in state["observations"].items() if o["capture_id"] == capture_id),
                "state": receipt["state"], "route": request["route"]}
    capture = {"source": source, "acquisition_id": acquisition_id, "observed_at": observed_at,
               "capture_hash": capture_hash, "mapping_hash": digest(mapping),
               "reference": {"route": request["route"], "folder": str(folder), "file": receipt.get("receipt_file", "receipt.json"),
                             "request": request, "receipt_hash": capture_hash},
               "imported_at": utc_now(), "mapping_status": receipt["state"],
               "rejected": mapping["rejected"], "mapper_version": VERSION,
               "provider_requests": 0, "provider_charge_usd": "0"}
    changes, observation_ids = [change("captures", capture_id, capture)], []
    for record in records:
        # List and search acquisitions of the same ASIN are separate contexts.
        # A new generic search must not erase a still-fresh verified list rank.
        source_item = record["identity"] + ":" + digest([request["route"], request["department"], request["term"]])[:20]
        observation_id = key("obs", [source, acquisition_id, source_item])
        semantic = {"title": record["title"], "product_url": record["product_url"]}
        facts = {"asin" if source == "amazon" else "product_id": record["identity"],
                 "list_type": record["list_type"], "position": record["position"], "search_position": record["search_position"],
                 "position_kind": record["position_kind"], "url": record["product_url"],
                 "price": {"displayed": record["raw_price"], "currency": record["currency"],
                           "basis": record["price_basis"], "pack_basis": record["unit_pack_basis"],
                           "quote_complete": record["quote_complete"], "source_url": record["product_url"]},
                 "limitations": list(record["limitations"]), "requested_list_url": request["url"],
                 "scope_verified": parsed["scope_verified"], "query": request["term"],
                 "image_urls": [record["image_url"]] if record["image_url"] else [],
                 "quantity": record["unit_pack_basis"], "source_route": request["route"],
                 "direct_capture": {"capture_id": capture_id, "receipt_hash": capture_hash,
                                    "card_refs": record["card_refs"]}}
        changes.append(change("observations", observation_id, {"source": source, "acquisition_id": acquisition_id,
                        "source_item": source_item, "capture_id": capture_id, "observed_at": observed_at,
                        "semantic_id": key("item", semantic), "semantic": semantic, "facts": facts}))
        observation_ids.append(observation_id)
    memory.commit(capture_id, changes)
    return {"capture_id": capture_id, "new_observations": len(observation_ids), "reused_capture": False,
            "observation_ids": observation_ids, "state": receipt["state"], "route": request["route"]}
