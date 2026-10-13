"""Python companion to the private portal's bounded release-index-v2 pages.

Pages are immutable objects. Only the same existing conditional release root
selects which pages are authoritative; a cursor or orphan page grants no access.
This module does not publish, obtain credentials or change a provider setting.
"""

import base64
from copy import deepcopy
from hashlib import sha256
import re

from .models import SafetyError, canonical, decode_json, digest, identifier

KINDS = ("archive", "issues", "types", "aliases", "queue", "holds", "timeline", "revisions")
OPTIONAL_KINDS = ("lineage",)
SUPPORTED_KINDS = KINDS + OPTIONAL_KINDS
ROOT_LIMIT = 16 * 1024
PAGE_LIMIT = 256 * 1024
LEAF_LIMIT = 50
MAX_INTEGER = 2**53 - 1
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_HEX = re.compile(r"[a-f0-9]*\Z")


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= MAX_INTEGER


def release_bytes(value):
    def check(row):
        if row is None or type(row) in (str, bool):
            if isinstance(row, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in row):
                raise SafetyError("RELEASE_INVALID_UNICODE")
        elif type(row) is int:
            if not -MAX_INTEGER <= row <= MAX_INTEGER: raise SafetyError("RELEASE_NONINTEGER_JSON")
        elif isinstance(row, list):
            for child in row: check(child)
        elif isinstance(row, dict):
            for name, child in row.items():
                if not isinstance(name, str): raise SafetyError("RELEASE_INVALID_JSON")
                check(name); check(child)
        else: raise SafetyError("RELEASE_NONINTEGER_JSON")
    check(value)
    return canonical(value) + b"\n"


def _length(kind):
    if kind not in SUPPORTED_KINDS: raise SafetyError("RELEASE_LOOKUP_INVALID")
    return {"archive": 16, "queue": 80, "holds": 128, "timeline": 32, "revisions": 80,
            "lineage": 129}.get(kind, 64)


def _key(kind, value):
    return isinstance(value, str) and len(value) == _length(kind) and bool(_HEX.fullmatch(value))


def sequence_key(number):
    if not _integer(number, 1): raise SafetyError("RELEASE_SEQUENCE_INVALID")
    return f"{number:016x}"


def _descriptor(ref, client, kind):
    if (not isinstance(ref, dict) or set(ref) != {"key", "sha256", "size", "count"}
            or not isinstance(ref["sha256"], str) or not _HASH.fullmatch(ref["sha256"])
            or ref["key"] != f"clients/{client}/release-index-v2/pages/{kind}/{ref['sha256']}.json"
            or not _integer(ref["size"], 1) or ref["size"] > PAGE_LIMIT or not _integer(ref["count"], 1)):
        raise SafetyError("RELEASE_PAGE_REFERENCE_INVALID")
    return ref


def validate_release_root(root, client=None, *, server_at=None):
    fields = {"schema_version", "contract", "client_id", "generation", "previous_root_hash", "updated_at",
              "next_sequence", "settled_publication_count", "pages", "migration"}
    if (not isinstance(root, dict) or set(root) != fields or type(root["schema_version"]) is not int
            or root["schema_version"] != 2 or root["contract"] != "release-index-v2"
            or client is not None and root["client_id"] != client or len(release_bytes(root)) > ROOT_LIMIT):
        raise SafetyError("RELEASE_INDEX_INVALID")
    identifier(root["client_id"])
    from .release_index import _utc
    updated = _utc(root["updated_at"])
    if server_at is not None and updated > _utc(server_at): raise SafetyError("RELEASE_ROOT_FUTURE")
    if (not _integer(root["generation"], 1) or not _integer(root["next_sequence"], 1)
            or not _integer(root["settled_publication_count"])
            or root["previous_root_hash"] is not None and (not isinstance(root["previous_root_hash"], str)
                or not _HASH.fullmatch(root["previous_root_hash"]))
            or not isinstance(root["pages"], dict)
            or set(root["pages"]) not in (set(KINDS), set(SUPPORTED_KINDS))):
        raise SafetyError("RELEASE_INDEX_INVALID")
    migration = root["migration"]
    if migration is not None and (not isinstance(migration, dict) or set(migration) != {"source_etag", "source_hash"}
            or migration["source_etag"] is not None and (not isinstance(migration["source_etag"], str) or not migration["source_etag"])
            or not isinstance(migration["source_hash"], str) or not _HASH.fullmatch(migration["source_hash"])):
        raise SafetyError("RELEASE_MIGRATION_INVALID")
    for kind, ref in root["pages"].items():
        if ref is not None: _descriptor(ref, root["client_id"], kind)
    if ((root["pages"]["archive"] or {}).get("count", 0) != root["next_sequence"] - 1
            or (root["pages"]["revisions"] or {}).get("count", 0) != (root["pages"]["archive"] or {}).get("count", 0)
            or root["settled_publication_count"] > (root["pages"]["issues"] or {}).get("count", 0)):
        raise SafetyError("RELEASE_ROOT_COUNTS_INVALID")
    return root


def empty_release_root(client, at, migration=None):
    return validate_release_root({"schema_version": 2, "contract": "release-index-v2", "client_id": client,
        "generation": 1, "previous_root_hash": None, "updated_at": at, "next_sequence": 1,
        "settled_publication_count": 0, "pages": {kind: None for kind in KINDS}, "migration": deepcopy(migration)})


class ReleasePages:
    def __init__(self, root, read_object, put_immutable=None, *, read_limit=256, write_limit=512):
        self.root = deepcopy(validate_release_root(root))
        if not callable(read_object) or put_immutable is not None and not callable(put_immutable):
            raise SafetyError("RELEASE_STORAGE_ADAPTER_REQUIRED")
        if not _integer(read_limit, 1) or not _integer(write_limit, 1): raise SafetyError("RELEASE_READ_BUDGET")
        self.read_object, self.put_immutable = read_object, put_immutable
        self.read_limit, self.write_limit = read_limit, write_limit
        self.reads, self.cache, self.pending = 0, {}, {}

    def _validate_page(self, page, ref, kind, prefix):
        base = {"contract", "client_id", "kind", "generation", "prefix", "node"}
        if (not isinstance(page, dict) or page.get("contract") != "release-index-page-v2"
                or page.get("client_id") != self.root["client_id"] or page.get("kind") != kind
                or page.get("prefix") != prefix or not _integer(page.get("generation"), 1)
                or page["generation"] > self.root["generation"]):
            raise SafetyError("RELEASE_PAGE_INVALID")
        if page["node"] == "leaf":
            rows = page.get("entries")
            if (set(page) != base | {"entries"} or not isinstance(rows, list)
                    or not 1 <= len(rows) <= LEAF_LIMIT or len(rows) != ref["count"]):
                raise SafetyError("RELEASE_PAGE_INVALID")
            previous = None
            for row in rows:
                if (not isinstance(row, dict) or set(row) != {"key", "value"} or not _key(kind, row["key"])
                        or not row["key"].startswith(prefix) or previous is not None and previous >= row["key"]
                        or not isinstance(row["value"], dict)):
                    raise SafetyError("RELEASE_PAGE_INVALID")
                previous = row["key"]
        elif page["node"] == "branch":
            children = page.get("children")
            if (set(page) != base | {"children"} or len(prefix) >= _length(kind)
                    or not isinstance(children, dict) or not 1 <= len(children) <= 16):
                raise SafetyError("RELEASE_PAGE_INVALID")
            for digit, child in children.items():
                if not isinstance(digit, str) or len(digit) != 1 or digit not in "0123456789abcdef":
                    raise SafetyError("RELEASE_PAGE_INVALID")
                _descriptor(child, self.root["client_id"], kind)
            if sum(c["count"] for c in children.values()) != ref["count"]:
                raise SafetyError("RELEASE_PAGE_INVALID")
        else: raise SafetyError("RELEASE_PAGE_INVALID")
        return page

    def page(self, kind, ref, prefix):
        _descriptor(ref, self.root["client_id"], kind)
        if ref["key"] not in self.cache:
            if self.reads >= self.read_limit: raise SafetyError("RELEASE_READ_BUDGET")
            self.reads += 1
            raw = self.read_object(ref["key"])
            if not isinstance(raw, bytes) or len(raw) != ref["size"] or sha256(raw).hexdigest() != ref["sha256"]:
                raise SafetyError("RELEASE_PAGE_HASH_INVALID")
            page = decode_json(raw)
            if release_bytes(page) != raw: raise SafetyError("RELEASE_PAGE_JSON_INVALID")
            self.cache[ref["key"]] = page
        return self._validate_page(self.cache[ref["key"]], ref, kind, prefix)

    def get(self, kind, key):
        if not _key(kind, key): raise SafetyError("RELEASE_LOOKUP_INVALID")
        ref, prefix = self.root["pages"].get(kind), ""
        while ref is not None:
            page = self.page(kind, ref, prefix)
            if page["node"] == "leaf":
                return deepcopy(next((r["value"] for r in page["entries"] if r["key"] == key), None))
            digit = key[len(prefix)]; prefix += digit; ref = page["children"].get(digit)
        return None

    def scan(self, kind, *, after=None, prefix="", reverse=False, limit=20, before=None):
        length = _length(kind)
        if (not _integer(limit, 1) or limit > LEAF_LIMIT or type(reverse) is not bool
                or not isinstance(prefix, str) or not _HEX.fullmatch(prefix) or len(prefix) > length
                or after is not None and not _key(kind, after) or before is not None and not _key(kind, before)):
            raise SafetyError("RELEASE_SCAN_INVALID")
        rows = []
        def walk(ref, path):
            if ref is None or len(rows) > limit or not (path.startswith(prefix) or prefix.startswith(path)): return
            low, high = path.ljust(length, "0"), path.ljust(length, "f")
            if after is not None and (low >= after if reverse else high <= after) or before is not None and low >= before: return
            page = self.page(kind, ref, path)
            if page["node"] == "leaf":
                for row in reversed(page["entries"]) if reverse else page["entries"]:
                    k = row["key"]
                    if not k.startswith(prefix) or after is not None and (k >= after if reverse else k <= after) or before is not None and k >= before: continue
                    rows.append(row)
                    if len(rows) > limit: break
            else:
                for digit in sorted(page["children"], reverse=reverse):
                    walk(page["children"][digit], path + digit)
                    if len(rows) > limit: break
        walk(self.root["pages"].get(kind), "")
        more, rows = len(rows) > limit, rows[:limit]
        return {"rows": deepcopy(rows), "more": more, "last_key": rows[-1]["key"] if rows else after}

    def _stage(self, kind, prefix, node):
        page = {"contract": "release-index-page-v2", "client_id": self.root["client_id"], "kind": kind,
                "generation": self.root["generation"], "prefix": prefix, **node}
        raw = release_bytes(page)
        if len(raw) > PAGE_LIMIT: raise SafetyError("RELEASE_PAGE_LIMIT")
        hashed = sha256(raw).hexdigest()
        count = len(node["entries"]) if node["node"] == "leaf" else sum(c["count"] for c in node["children"].values())
        ref = {"key": f"clients/{self.root['client_id']}/release-index-v2/pages/{kind}/{hashed}.json",
               "sha256": hashed, "size": len(raw), "count": count}
        self._validate_page(page, ref, kind, prefix)
        if ref["key"] not in self.pending and len(self.pending) >= self.write_limit:
            raise SafetyError("RELEASE_WRITE_BUDGET")
        self.pending[ref["key"]] = raw; self.cache[ref["key"]] = page
        return ref

    def build(self, kind, entries, prefix=""):
        if not entries: return None
        if len(entries) <= LEAF_LIMIT:
            page = {"contract": "release-index-page-v2", "client_id": self.root["client_id"], "kind": kind,
                "generation": self.root["generation"], "prefix": prefix, "node": "leaf", "entries": entries}
            if len(release_bytes(page)) <= PAGE_LIMIT: return self._stage(kind, prefix, {"node": "leaf", "entries": entries})
        if len(prefix) >= _length(kind): raise SafetyError("RELEASE_ENTRY_LIMIT")
        groups = {}
        for row in entries:
            if not _key(kind, row["key"]) or not row["key"].startswith(prefix): raise SafetyError("RELEASE_LOOKUP_INVALID")
            groups.setdefault(row["key"][len(prefix)], []).append(row)
        return self._stage(kind, prefix, {"node": "branch", "children": {
            digit: self.build(kind, group, prefix + digit) for digit, group in sorted(groups.items())}})

    def set(self, kind, key, value):
        if not _key(kind, key) or value is not None and not isinstance(value, dict): raise SafetyError("RELEASE_LOOKUP_INVALID")
        def update(ref, prefix):
            if ref is None: return None if value is None else self.build(kind, [{"key": key, "value": deepcopy(value)}], prefix)
            page = self.page(kind, ref, prefix)
            if page["node"] == "leaf":
                entries = [r for r in page["entries"] if r["key"] != key]
                if value is not None: entries.append({"key": key, "value": deepcopy(value)})
                entries.sort(key=lambda r: r["key"])
                return ref if canonical(entries) == canonical(page["entries"]) else self.build(kind, entries, prefix)
            digit = key[len(prefix)]; children = dict(page["children"])
            child = update(children.get(digit), prefix + digit)
            if child: children[digit] = child
            else: children.pop(digit, None)
            if not children: return None
            return ref if canonical(children) == canonical(page["children"]) else self._stage(kind, prefix, {"node": "branch", "children": children})
        # A read or no-op deletion must not change an old eight-kind root.
        # Only an explicit nonempty lineage mutation introduces its descriptor.
        if kind not in self.root["pages"] and value is None: return
        self.root["pages"][kind] = update(self.root["pages"].get(kind), "")

    def persist(self):
        if self.put_immutable is None: raise SafetyError("RELEASE_STORAGE_ADAPTER_REQUIRED")
        for key, raw in self.pending.items():
            self.put_immutable(key, raw, sha256=sha256(raw).hexdigest(), media_type="application/json")
            if self.read_object(key) != raw: raise SafetyError("RELEASE_IMMUTABLE_PAGE_CONFLICT")


def release_cursor(root, kind, last_key, *, reverse=False, prefix=""):
    validate_release_root(root)
    if not _key(kind, last_key) or type(reverse) is not bool or not isinstance(prefix, str) or not _HEX.fullmatch(prefix) or len(prefix) > _length(kind):
        raise SafetyError("RELEASE_CURSOR_INVALID")
    value = {"contract": "release-cursor-v2", "client_id": root["client_id"], "root_hash": digest(root),
             "kind": kind, "last_key": last_key, "reverse": reverse, "prefix": prefix}
    return base64.urlsafe_b64encode(release_bytes(value).rstrip(b"\n")).decode("ascii").rstrip("=")


def read_release_cursor(token, root, kind):
    validate_release_root(root)
    if not isinstance(token, str) or len(token) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise SafetyError("RELEASE_CURSOR_INVALID")
    try: value = decode_json(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except Exception: raise SafetyError("RELEASE_CURSOR_INVALID") from None
    if (not isinstance(value, dict) or set(value) != {"contract", "client_id", "root_hash", "kind", "last_key", "reverse", "prefix"}
            or value["contract"] != "release-cursor-v2" or value["client_id"] != root["client_id"] or value["kind"] != kind
            or value["root_hash"] != digest(root) or not _key(kind, value["last_key"])
            or type(value["reverse"]) is not bool or not isinstance(value["prefix"], str)
            or not _HEX.fullmatch(value["prefix"]) or len(value["prefix"]) > _length(kind)):
        raise SafetyError("RELEASE_CURSOR_STALE")
    return value
