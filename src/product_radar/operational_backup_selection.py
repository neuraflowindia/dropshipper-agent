"""Select an exact, bounded local recovery closure from supplied verified state.

Ordinary selection does not replay records, write files, contact a provider, or
read login settings. A v3 caller may explicitly stream already-validated bytes
to its existing immutable store. Original byte hashes and canonical JSON digests
are separate. Missing references are limitations, never permission to recollect.
"""

from collections import deque
from copy import deepcopy
from hashlib import sha256
import ipaddress
from pathlib import Path, PurePosixPath
import re
from urllib.parse import parse_qs, urlsplit

from .models import SafetyError, decode_json, digest, identifier, json_safe, timestamp
from .operational_backup import MAX_ATTACHMENT_BYTES, MAX_BACKUP_BYTES, MAX_BACKUP_FILES, _read
from .public_pages import _public_url
from .report_assets import RECEIPT as REPORT_ASSET_RECEIPT, validate_report_asset_receipt
from .research_memory import TABLES, key
from .runtime_storage import logical_path, runtime_path
from .storage import safe_path


SELECTION_VERSION = "essential-backup-selection-v1"
MULTIPART_SELECTION_VERSION = "essential-backup-selection-v2"
MAX_REFERENCES = MAX_BACKUP_FILES
MAX_CLOSURE_DEPTH = 8
MAX_JSON_BYTES = MAX_ATTACHMENT_BYTES
KINDS = ("public_image", "report_package", "model_receipt", "catalogue_material",
         "operating_control", "source_capture", "application_code", "public_setting",
         "public_asset", "dependency_manifest", "operator_documentation")
ESSENTIAL_FILE_KINDS = KINDS
_HASH = re.compile(r"[0-9a-f]{64}")
_TRENDS_SCOPE_FIELDS = ("term", "geo", "time_range", "property", "kind", "collected_at", "source_reference", "scope_verified")
_TRENDS_RELATED_FIELDS = _TRENDS_SCOPE_FIELDS + ("widget", "displayed_section")
_PRIVATE = {".git", ".codex", ".agents", ".aws", ".venv", "node_modules", "__pycache__",
            "profiles", "browser-profile", "native-profile", "research-browser-profile",
            "signed-in-quarantine"}  # signed_in_saved.QUARANTINE: signed-in results that could not be cleaned
_PRIVATE_FILES = {"auth.json", "credentials.json", "tokens.json", "apify_accounts.json",
                  "providers.env", "quickcommerce.env", "cookies.json", "login.json"}
_SECRET_FIELDS = {"password", "api_key", "apikey", "access_token", "refresh_token", "id_token",
                  "authorization", "cookie", "set_cookie", "client_secret", "secret_access_key"}
_DATA_EXTENSIONS = {".json", ".jsonl", ".csv", ".html", ".txt", ".pdf", ".xlsx", ".png", ".jpg", ".jpeg", ".webp"}
_NATIVE_PROOF_SCRIPTS = {"native_expression.js", "native_ready.js"}
_SOURCE_AREAS = {"direct_marketplace", "direct_trends", "direct_meta", "supplier_routes", "supplier_pages",
                 "quickcommerce_captures", "all_source_dispatch", "trends_captures",
                 "trends_browser", "marketplace_browser", "amazon_trials", "image_captures",
                 "trends_checks", "supplier_searches", "dropship_platforms", "meta_depth", "wider_discovery"}
_MODEL_FILES = ("intent.json", "result.json", "completion.json", "failure.json", "runner_execution.json",
                "execution_failure.json", "runner_preflight.json", "derived_result.json", "retries.json")
_PROOF_FILES = {"snapshot.json", "scope.json", "transport.json", "parsed.json", "interpretation.json",
                "rendered.html", "multiTimeline.csv", "native_result.json", "native_dispatch.json", "owned_process.json",
                "native_expression.js", "native_ready.js",
                "native_outer_deadline.json", "outer_deadline.json", "deadline_parsed.json",
                "deadline_interpretation.json", "deadline_scope.json", "deadline_failed_snapshot.json",
                "deadline_reparse.json", "deadline_snapshot.json", "receipt.json", "capture.json", "probe.json", "intent.json",
                "supplier_outer_deadline.json", "dispatch-recovery.json", "capture-dispatch-recovery.json"}
_SCRIPTS = ("run_programme.py", "run_source_coverage.py", "benchmark_providers.py", "apify_http_worker.py",
            "check_quickcommerce_connection.py", "render_live_pdf.py", "generate_sample_excel.py",
            "generate_sample_pdf.py", "build_review_package.py", "rehearse_operational_restore.py",
            "stage_private_draft.py", "setup_programme_task.ps1", "adopt_local_storage.py",
            "desktop_research_control.py", "research_sign_in.py", "windows/DropshipperDesktop.cs", "windows/DropshipperDesktop.manifest",
            "windows/Build-Dropshipper.ps1", "windows/Install-Dropshipper.ps1", "windows/Test-Dropshipper.ps1")
_PORTAL = ("worker.mjs", "release-admin.mjs", "admin-status.mjs", "access-service-assertion.mjs",
           "functional-packet.mjs", "supplier-scoped.mjs", "release-pages.mjs", "release-history.mjs", "alias-proof.mjs",
           "lineage-proof.mjs", "definition-proof.mjs", "example-proof.mjs", "opportunity-score.mjs")
_TESTS = ("tests/test_source_exhausted_preparation.py", "tests/test_source_preparation_independent_review.py",
          "tests/test_operation_records.py", "tests/test_research_projection_batch.py",
          "tests/test_report_print_layout.py", "tests/test_live_outputs.py")
_PUBLIC_SETTINGS = ("config/service.json", "config/research/catalogue.json", "config/ai/codex_luna.json",
                    "config/operations/backup.json", "config/research/collection_cost_baseline.json",
                    "config/research/spend_watch.json", "config/research/signed_in_reads.json",
                    "config/research/dropship_platforms.json", "config/research/crowding.json",
                    "config/research/opportunity_score.json",
                    "config/research/apify_allocation.json", "config/benchmarks/meta_first.json",
                    "config/research/quickcommerce_locations.json", "config/research/wider_discovery.json",
                    "config/research/round_limits.json", "config/research/product_map.json")
_DEPENDENCIES = ("requirements-research.txt", "requirements-preview.txt")
_ASSETS = ("media/NeuraFlow/long_maroon_path_transparent_1024.png",
           "assets/windows/neuraflow-dropshipper.ico", "assets/windows/neuraflow-dropshipper.png")
_DOCS = ("docs/README.md", "docs/product/india_dropshipping_product_opportunity_radar_product_brief.md",
         "docs/operations/benchmarks/2026-10-03/benchmark_record.md",
         "docs/operations/benchmarks/2026-10-03/offline_runner_guide.md",
         "docs/operations/benchmarks/2026-10-03/execution_specification.md",
         "docs/operations/build_activation_and_costs_2026-10-03.md",
         "docs/operations/windows_desktop_launcher.md", "docs/operations/implementation_2026-10-11.md",
         "docs/reviews/2026-10-10_programme_operation_audit.md",
         "docs/reviews/2026-10-11_operating_repairs_focused_review.md",
         "docs/reviews/2026-10-11_private_legacy_transition_review.md")
_FILE_FIELDS = {"path", "relative_path", "kind", "required", "expected_sha256", "expected_json_digest", "bytes", "origins"}
_SELECTION_FIELDS = {"schema_version", "kind", "files", "unresolved_required", "absent_optional", "unsafe",
                     "limitations", "selected_bytes", "selection_fingerprint", "research_state_hash", "financial_state_hash"}


def validate_backup_settings(value):
    """Only the reviewed same-project local destination; never cloud/auth."""
    multipart = isinstance(value, dict) and value.get("schema_version") == 2
    fields = {"schema_version", "enabled", "destination", "timezone"}
    if multipart:
        fields.add("backup_contract")
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] != (2 if multipart else 1)
            or multipart and value["backup_contract"] != "essential-operational-record-backup-v3"
            or type(value["enabled"]) is not bool or value["destination"] != "data/backups"
            or value["timezone"] != "Asia/Kolkata"):
        raise SafetyError("BACKUP_SETTINGS_INVALID")
    return deepcopy(value)


def _project_root(root):
    if not isinstance(root, (str, Path)) or not str(root) or "\x00" in str(root) or ".." in Path(root).parts:
        raise SafetyError("BACKUP_SELECTION_ROOT_INVALID")
    return Path(root).absolute()


def _logical(root, path):
    relative = logical_path(root, path)
    if relative is None:
        raise SafetyError("BACKUP_SELECTION_OUTSIDE_PROJECT")
    return relative


def _lexical_reference(value, kind, root):
    if (kind not in KINDS or not isinstance(value, (str, Path)) or not str(value)
            or any(char in str(value) for char in ("\x00", "\\", ":"))
            or ".." in str(value).split("/")):
        raise SafetyError("BACKUP_SELECTION_PATH_INVALID")
    candidate = Path(value)
    # Logical project spelling; data/ and media/live/ may be physically
    # relocated by the machine-local runtime-storage setting. The mapping is
    # lexical (setting only, no store probe), so saved references stay
    # checkable after the working-record folder itself is lost.
    if candidate.is_absolute():
        path, relative = candidate, logical_path(root, candidate, lexical=True)
    else:
        relative = PurePosixPath(candidate.as_posix())
        path = runtime_path(root, relative, lexical=True) if relative.parts else None
    if relative is None or path is None or not relative.parts:
        raise SafetyError("BACKUP_SELECTION_OUTSIDE_PROJECT")
    parts, name = relative.parts, relative.name.casefold()
    if (not parts or any(part.casefold() in _PRIVATE or part.startswith(".") for part in parts)
            or name in _PRIVATE_FILES or name.endswith(".env")
            or parts[:2] == ("data", "backups") or parts[:3] == ("data", "operations", "backup_controls")):
        raise SafetyError("BACKUP_SELECTION_PRIVATE_OR_RECURSIVE")
    spelling = relative.as_posix()
    allowed = False
    if kind == "application_code":
        allowed = ((parts[:2] == ("src", "product_radar") and path.suffix == ".py"
                    and (len(parts) == 3 or len(parts) == 4 and parts[2] == "providers"))
                   or spelling in {"scripts/" + name for name in _SCRIPTS}
                   or spelling in {"portal/" + name for name in _PORTAL}
                   or spelling in _TESTS)
    elif kind == "public_setting":
        allowed = (spelling in _PUBLIC_SETTINGS or len(parts) == 5
                   and parts[:3] == ("config", "local", "apify_origins")
                   and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", parts[3]) and name == "origin.json")
    elif kind == "public_asset":
        allowed = spelling in _ASSETS
    elif kind == "dependency_manifest":
        allowed = spelling in _DEPENDENCIES
    elif kind == "operator_documentation":
        allowed = spelling in _DOCS
    elif kind == "public_image":
        allowed = parts[:3] == ("media", "live", "product-images") and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
    elif kind == "report_package":
        allowed = parts[:2] == ("data", "outputs") or parts[:3] == ("media", "live", "reports")
    elif kind == "model_receipt":
        allowed = len(parts) >= 6 and parts[:4] == ("data", "research", "captured", "ai_jobs") and name in _MODEL_FILES
    elif kind == "catalogue_material":
        allowed = (len(parts) >= 5 and parts[:3] == ("data", "research", "captured")
                   and parts[3] in {"catalogue_materials", "discovery_cycles"})
    elif kind == "operating_control":
        allowed = (parts[:3] == ("data", "research", "captured") and len(parts) >= 5
                   and parts[3] in {"programme_runs", "programme_service", "all_source_dispatch", "spend_watch"})
    elif kind == "source_capture":
        allowed = (parts[:3] == ("data", "research", "captured") and len(parts) >= 5 and parts[3] in _SOURCE_AREAS
                   or parts[:3] in (("data", "research", "connected-trends"), ("data", "research", "catalogue-public"))
                   or parts[:4] == ("data", "benchmarks", "state", "apify") and len(parts) >= 6
                   or parts[:3] in (("media", "live", "trends"), ("media", "live", "supplier-captures")))
    native_proof_script = kind == "source_capture" and path.name in _NATIVE_PROOF_SCRIPTS
    if not allowed or (kind not in {"application_code", "public_asset", "dependency_manifest", "operator_documentation"}
                       and path.suffix.casefold() not in _DATA_EXTENSIONS and not native_proof_script):
        raise SafetyError("BACKUP_SELECTION_SCOPE_INVALID")
    return path, spelling


def validate_essential_backup_reference(ref, *, root):
    """Lexical, no filesystem probes: also usable after original files are lost."""
    if (not isinstance(ref, dict) or set(ref) != _FILE_FIELDS or type(ref["required"]) is not bool
            or type(ref["bytes"]) is not int or not 0 <= ref["bytes"] <= MAX_ATTACHMENT_BYTES
            or not isinstance(ref["expected_sha256"], str) or not _HASH.fullmatch(ref["expected_sha256"])
            or ref["expected_json_digest"] is not None
            and (not isinstance(ref["expected_json_digest"], str) or not _HASH.fullmatch(ref["expected_json_digest"]))
            or not isinstance(ref["origins"], list) or not 1 <= len(ref["origins"]) <= MAX_REFERENCES
            or any(not isinstance(value, str) or not value or len(value) > 500 for value in ref["origins"])
            or ref["origins"] != sorted(set(ref["origins"]))):
        raise SafetyError("BACKUP_SELECTION_REFERENCE_INVALID")
    path, relative = _lexical_reference(ref["path"], ref["kind"], _project_root(root))
    if str(path) != ref["path"] or relative != ref["relative_path"]:
        raise SafetyError("BACKUP_SELECTION_REFERENCE_INVALID")
    return deepcopy(ref)


def validate_essential_backup_selection(selection, *, root):
    """Verify the frozen selector contract, without reopening original files."""
    multipart = isinstance(selection, dict) and selection.get("kind") == MULTIPART_SELECTION_VERSION
    if (not isinstance(selection, dict) or set(selection) != _SELECTION_FIELDS
            or type(selection["schema_version"]) is not int or selection["schema_version"] != (2 if multipart else 1)
            or selection["kind"] != (MULTIPART_SELECTION_VERSION if multipart else SELECTION_VERSION)
            or not isinstance(selection["files"], list)
            or not multipart and len(selection["files"]) > MAX_REFERENCES
            or type(selection["selected_bytes"]) is not int or not 0 <= selection["selected_bytes"] <= (9007199254740991 if multipart else MAX_BACKUP_BYTES)):
        raise SafetyError("BACKUP_SELECTION_INVALID")
    for field in ("research_state_hash", "financial_state_hash"):
        if not isinstance(selection[field], str) or not _HASH.fullmatch(selection[field]):
            raise SafetyError("BACKUP_SELECTION_INVALID")
    for field in ("unresolved_required", "absent_optional", "unsafe"):
        rows = selection[field]
        if (not isinstance(rows, list) or not multipart and len(rows) > MAX_REFERENCES or any(not isinstance(row, dict)
                or set(row) != {"origin", "reason", "relative_path"} or not isinstance(row["origin"], str)
                or not isinstance(row["reason"], str) or row["relative_path"] is not None
                and (not isinstance(row["relative_path"], str) or PurePosixPath(row["relative_path"]).is_absolute()
                     or ".." in row["relative_path"].split("/")) for row in rows)):
            raise SafetyError("BACKUP_SELECTION_INVALID")
    if not isinstance(selection["limitations"], list) or any(not isinstance(row, str) for row in selection["limitations"]):
        raise SafetyError("BACKUP_SELECTION_INVALID")
    rows = [validate_essential_backup_reference(row, root=root) for row in selection["files"]]
    paths = [row["relative_path"] for row in rows]
    if (paths != sorted(set(paths)) or sum(row["bytes"] for row in rows) != selection["selected_bytes"]
            or selection["selection_fingerprint"] != digest({field: value for field, value in selection.items()
                                                            if field != "selection_fingerprint"})):
        raise SafetyError("BACKUP_SELECTION_FINGERPRINT_INVALID")
    return deepcopy(selection)


def _has_secret_field(value):
    queue, visited = [value], 0
    while queue:
        item = queue.pop(); visited += 1
        if visited > MAX_REFERENCES:
            raise SafetyError("BACKUP_SELECTION_JSON_BOUND")
        if isinstance(item, dict):
            if any(str(field).casefold().replace("-", "_") in _SECRET_FIELDS for field in item):
                return True
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item)
    return False


def _legacy_observation_citation(record_id, record):
    """Recognize two untyped historical observations, never a capture receipt.

    The six-field form is the exact save_source_check writer contract. The
    eight-field form is the known India Trends family-review observation,
    whose old IDs were not generated by that writer. Neither promised saved
    webpage bytes. This is syntactic citation validation only: no DNS, page
    fetch, history refresh, current product assessment or source-state upgrade.
    """
    fields = {"source", "route", "at", "state", "reason", "reference"}
    if not isinstance(record, dict) or set(record) not in (fields, fields | {"concept_id", "description_hash"}):
        return False
    if (record["source"] not in ("meta", "marketplace", "trends", "supplier")
            or record["state"] not in ("usable", "insufficient", "blocked", "failed")
            or any(not isinstance(record[field], str) or not record[field].strip() or len(record[field]) > 4096
                   or any(ord(char) < 32 or ord(char) == 127 for char in record[field])
                   for field in ("route", "reason", "reference"))):
        return False
    try:
        timestamp(record["at"])
        _, host, port, scheme = _public_url(record["reference"])
        parsed = urlsplit(record["reference"])
        if parsed.fragment or port != (443 if scheme == "https" else 80):
            return False
        # Do not resolve a citation. Reject known local spellings and literal
        # private/embedded-private IPs without sending any network request.
        host = host.casefold()
        domain = host.rstrip(".")
        try:
            address = ipaddress.ip_address(domain)
        except ValueError:
            labels = domain.split(".")
            if (len(labels) < 2
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                    or all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", label) for label in labels)
                    or labels[-1] in {"localhost", "local", "localdomain", "internal", "lan", "home", "corp",
                                      "intranet", "private", "invalid", "test", "example", "onion"}
                    or domain == "home.arpa" or domain.endswith(".home.arpa")):
                return False
        else:
            effective = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
            embedded = []
            if isinstance(address, ipaddress.IPv6Address):
                if address.sixtofour:
                    embedded.append(address.sixtofour)
                if address.teredo:
                    embedded.extend(address.teredo)
                if address in ipaddress.ip_network("64:ff9b::/96"):
                    embedded.append(ipaddress.IPv4Address(int(address) & 0xffffffff))
            if (not effective.is_global or effective.is_multicast or effective.is_reserved
                    or effective.is_loopback or effective.is_link_local or effective.is_unspecified
                    or isinstance(address, ipaddress.IPv6Address) and address.is_site_local
                    or any(not item.is_global or item.is_multicast or item.is_reserved for item in embedded)):
                return False
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=100)
        secret_fields = _SECRET_FIELDS | {"token", "api_token", "secret", "session", "session_token", "auth",
                                          "code", "signature", "credential", "credentials", "access_key", "key",
                                          "auth_token", "session_id", "sessionid", "accesstoken", "refreshtoken",
                                          "idtoken", "clientsecret", "sessiontoken", "accesskey"}
        for name in query:
            normalized = name.casefold().replace("-", "_")
            if (not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or normalized in secret_fields
                    or normalized.startswith(("x_amz_", "x_goog_"))):
                return False
        if set(record) == fields:
            return record_id == key("source-check", [record["source"], record["route"], record["at"], record["reference"]])
        return (isinstance(record_id, str) and re.fullmatch(r"source-check-[0-9a-f]{40}", record_id) is not None
            and record["source"] == "trends" and record["state"] == "usable"
            and record["route"] == "connected_public_browser_family_review"
            and isinstance(record["concept_id"], str) and re.fullmatch(r"concept-[0-9a-f]{40}", record["concept_id"]) is not None
            and isinstance(record["description_hash"], str) and _HASH.fullmatch(record["description_hash"]) is not None
            and scheme == "https" and host == "trends.google.com" and parsed.path == "/trends/explore"
            and set(query) in ({"geo", "q"}, {"geo", "q", "hl"})
            and query["geo"] == ["IN"] and len(query["q"]) == 1 and bool(query["q"][0].strip())
            and ("hl" not in query or query["hl"] == ["en"]))
    except (SafetyError, TypeError, ValueError, UnicodeError):
        return False


class _Selection:
    def __init__(self, research, finances, root, *, multipart=False, freeze_bytes=None):
        self.state, self.finances, self.root = research, finances, root
        self.memory = runtime_path(root, "data/research/captured")
        self.files, self.values, self.queue, self.closed = {}, {}, deque(), set()
        self.unresolved, self.optional, self.unsafe = [], [], []
        self.jobs, self.issues, self.materials = {}, {}, set()
        self.attempts = set()
        self.legacy_trends_exports = None
        self.legacy_trends_scopes = {}
        self.legacy_trends_receipts = {}
        self.legacy_related_scopes = {}
        # Point-in-time validation only; never reused across selections or a
        # later checkpoint, and never populated by owner/history enumeration.
        self.pointer_owners, self.pointer_origins = {}, {}
        self.canonical_ids = {ref for table in research.values() for ref in table}
        self.references, self.total = 0, 0
        self.multipart = multipart
        self.freeze_bytes = freeze_bytes

    def note(self, target, origin, reason, relative=None):
        record = {"origin": origin, "reason": reason, "relative_path": relative}
        if record not in target:
            if not self.multipart and len(target) >= MAX_REFERENCES:
                raise SafetyError("BACKUP_SELECTION_REFERENCE_BOUND")
            target.append(record)

    def add(self, value, kind, origin, *, required=True, byte_hash=None, json_hash=None, close=True, depth=0,
            validate_bytes=None):
        self.references += 1
        if (not self.multipart and self.references > MAX_REFERENCES) or depth > MAX_CLOSURE_DEPTH:
            raise SafetyError("BACKUP_SELECTION_REFERENCE_BOUND")
        try:
            path, relative = _lexical_reference(value, kind, self.root)
            for expected in (byte_hash, json_hash):
                if expected is not None and (not isinstance(expected, str) or not _HASH.fullmatch(expected)):
                    raise SafetyError("BACKUP_SELECTION_EXPECTED_HASH_INVALID")
        except SafetyError as error:
            self.note(self.unsafe, origin, str(error)); return None
        if str(path) in self.files:
            row = self.files[str(path)]; parsed = self.values.get(str(path))
            if ((byte_hash is not None and row["expected_sha256"] != byte_hash)
                    or json_hash is not None and (parsed is None or digest(parsed) != json_hash)):
                self.note(self.unsafe, origin, "BACKUP_SELECTION_HASH_MISMATCH", relative); return None
            if validate_bytes is not None:
                try:
                    # A new semantic proof must not inherit another caller's
                    # byte-only cache hit. Reread only this matching original;
                    # do not retain an all-attachment raw-byte cache.
                    raw = _read(path, MAX_ATTACHMENT_BYTES)
                    if sha256(raw).hexdigest() != row["expected_sha256"]:
                        raise SafetyError("BACKUP_SELECTION_HASH_MISMATCH")
                    validate_bytes(raw)
                except SafetyError as error:
                    reason = str(error)
                    target = (self.unresolved if required else self.optional) if reason == "BACKUP_FILE_UNAVAILABLE" else self.unsafe
                    self.note(target, origin, reason, relative); return None
            row["required"] |= required
            row["origins"] = sorted(set(row["origins"] + [origin]))
            if KINDS.index(kind) < KINDS.index(row["kind"]):
                row["kind"] = kind
            if json_hash is not None:
                row["expected_json_digest"] = json_hash
            if close and parsed is not None:
                self.queue.append((path, parsed, kind, depth))
            return parsed
        try:
            raw = _read(path, MAX_ATTACHMENT_BYTES)
            actual_hash = sha256(raw).hexdigest()
            if path.suffix == ".json" and len(raw) > MAX_JSON_BYTES:
                raise SafetyError("BACKUP_SELECTION_PRIVATE_OR_OVERSIZED_JSON")
            parsed = decode_json(raw) if path.suffix == ".json" else None
            if path.suffix == ".json" and _has_secret_field(parsed):
                raise SafetyError("BACKUP_SELECTION_PRIVATE_OR_OVERSIZED_JSON")
            if (byte_hash is not None and actual_hash != byte_hash
                    or json_hash is not None and (parsed is None or digest(parsed) != json_hash)):
                raise SafetyError("BACKUP_SELECTION_HASH_MISMATCH")
            if validate_bytes is not None:
                validate_bytes(raw)
            if not self.multipart and self.total + len(raw) > MAX_BACKUP_BYTES:
                raise SafetyError("BACKUP_SELECTION_TOTAL_SIZE_LIMIT")
        except SafetyError as error:
            reason = str(error)
            target = (self.unresolved if required else self.optional) if reason == "BACKUP_FILE_UNAVAILABLE" else self.unsafe
            self.note(target, origin, reason, relative); return None
        self.files[str(path)] = {"path": str(path), "relative_path": relative, "kind": kind, "required": required,
            "expected_sha256": actual_hash, "expected_json_digest": json_hash, "bytes": len(raw), "origins": [origin]}
        self.values[str(path)] = parsed; self.total += len(raw)
        if kind == "public_setting":
            self.public_settings(path, parsed, origin)
            if str(path) not in self.files:
                return None
        if self.freeze_bytes is not None:
            # Use this same checked read, not a later reread of a mutable
            # control/doc/code file. The v3 caller owns its existing object
            # store; an unsuccessful save must not publish a frozen plan.
            self.freeze_bytes(raw)
        if close and parsed is not None:
            self.queue.append((path, parsed, kind, depth))
        return parsed

    def reference(self, value, kind, origin, *, required=True, json_hash=None, depth=0):
        if isinstance(value, dict):
            if "folder" in value and "file" in value:
                file = value["file"]
                if not isinstance(file, str) or file not in _PROOF_FILES or not isinstance(value["folder"], str):
                    self.note(self.unsafe, origin, "BACKUP_SELECTION_RECEIPT_FILE_INVALID"); return
                self.add(Path(value["folder"]) / file, kind, origin, required=required,
                         json_hash=value.get("receipt_hash", json_hash), depth=depth)
            elif "attempt_id" in value and value.get("file") == "captured_rows.json":
                self.attempt(value["attempt_id"], origin)
            else:
                self.note(self.unresolved if required else self.optional, origin, "BACKUP_SELECTION_REFERENCE_UNRESOLVED")
        elif isinstance(value, str) and value in self.canonical_ids:
            return  # Canonical IDs are not filesystem paths.
        elif isinstance(value, str) and (value.startswith(("http://", "https://")) or value in {"retained-public-receipt", "supplied-manifest-mapping"}):
            self.note(self.unresolved if required else self.optional, origin, "BACKUP_SELECTION_NONLOCAL_REFERENCE")
        elif isinstance(value, (str, Path)):
            self.add(value, kind, origin, required=required, json_hash=json_hash, depth=depth)
        else:
            self.note(self.unresolved if required else self.optional, origin, "BACKUP_SELECTION_REFERENCE_UNRESOLVED")

    def native_reference(self, receipt, field, kind, origin, *, depth=0):
        value = receipt.get(field)
        if value is None:
            return
        artifacts = receipt.get("artifacts", {})
        proof = artifacts.get("native_result.json") if isinstance(artifacts, dict) and field == "native_reference" else None
        expected = proof if isinstance(proof, str) else (proof.get("sha256")
            if isinstance(proof, dict) and isinstance(proof.get("path"), (str, Path))
            and isinstance(value, (str, Path))
            and self.root / Path(proof["path"]) == self.root / Path(value) else None)
        # A failed child may retain only its intended output pathname so that
        # later recovery can find the original. Only an explicit saved=false
        # declaration distinguishes that case from genuinely missing bytes.
        # Hash-bound artifacts are still added separately as required files.
        expected_only = (field == "native_reference"
                         and receipt.get("native_result_saved") is False
                         and receipt.get("state") in ("failed", "unavailable")
                         and isinstance(value, str) and Path(value).name == "native_result.json"
                         and expected is None)
        self.add(value, kind, origin + "/" + field, required=not expected_only, byte_hash=expected, depth=depth)
        if expected_only:
            try:
                native, _ = _lexical_reference(value, kind, self.root)
            except SafetyError:
                return
            # Preserve any original dispatch/failure evidence even when the
            # output was never produced. This is one exact directory and a
            # fixed public proof list, never a recursive profile search.
            for name in ("native_dispatch.json", "owned_process.json", "native_expression.js"):
                self.add(native.parent / name, kind, origin + "/native_failure_proof/" + name,
                         required=False, depth=depth)

    def job(self, job_id, origin, *, required=False):
        try:
            identifier(job_id)
        except SafetyError:
            self.note(self.unsafe, origin, "BACKUP_SELECTION_JOB_ID_INVALID"); return
        if job_id in self.jobs:
            if required and not self.jobs[job_id]:
                self.note(self.unresolved, origin, "BACKUP_SELECTION_JOB_ARTIFACTS_MISSING")
            if required:
                prefix = str(self.memory / "ai_jobs" / job_id) + "/"
                for path, row in self.files.items():
                    if path.startswith(prefix):
                        row["required"] = True
                        row["origins"] = sorted(set(row["origins"] + [origin]))
            return
        self.jobs[job_id] = False; folder = self.memory / "ai_jobs" / job_id
        request = self.state["jobs"].get(job_id, {}).get("request")
        request_hash = digest(request) if isinstance(request, dict) else None
        retries = self.add(folder / "retries.json", "model_receipt", origin + "/retries", required=False, close=False)
        if retries is not None and (not isinstance(retries, list) or not 1 <= len(retries) <= 20):
            self.note(self.unsafe, origin, "BACKUP_SELECTION_RETRIES_INVALID"); return
        found = False
        # A missing/damaged retry pointer must not hide original sent receipts.
        # Inspect this one owned job directory only; never recurse into profiles.
        actual = set()
        try:
            for entry in folder.iterdir():
                if entry.name.startswith("retry-"):
                    suffix = entry.name[6:]
                    if not suffix.isdigit() or not 1 <= int(suffix) <= 20 or str(int(suffix)) != suffix:
                        self.note(self.unsafe, origin, "BACKUP_SELECTION_RETRIES_INVALID"); continue
                    actual.add(int(suffix))
        except FileNotFoundError:
            pass
        except OSError:
            self.note(self.unresolved if required else self.optional, origin, "BACKUP_FILE_UNAVAILABLE")
        named = set(range(1, len(retries or []) + 1))
        if actual - named:
            self.note(self.unresolved, origin, "BACKUP_SELECTION_RETRY_POINTER_MISSING")
        for attempt in [folder] + [folder / ("retry-" + str(number)) for number in sorted(named | actual)]:
            for file in _MODEL_FILES[:-1]:
                value = self.add(attempt / file, "model_receipt", origin + "/" + attempt.name + "/" + file, required=False)
                if value is None:
                    continue
                if not isinstance(value, dict):
                    self.note(self.unsafe, origin, "BACKUP_SELECTION_MODEL_RECEIPT_INVALID"); continue
                found = True
                if file == "intent.json" and request is not None and value.get("request") != request:
                    self.note(self.unsafe, origin, "BACKUP_SELECTION_JOB_REQUEST_CHANGED")
                if file == "result.json" and request_hash is not None and value.get("request_hash") != request_hash:
                    self.note(self.unsafe, origin, "BACKUP_SELECTION_JOB_REQUEST_CHANGED")
        if required and not found:
            self.note(self.unresolved, origin, "BACKUP_SELECTION_JOB_ARTIFACTS_MISSING")
        self.jobs[job_id] = found

    def attempt(self, attempt_id, origin):
        try:
            identifier(attempt_id)
        except SafetyError:
            self.note(self.unsafe, origin, "BACKUP_SELECTION_ATTEMPT_ID_INVALID"); return
        if attempt_id in self.attempts:
            return
        self.attempts.add(attempt_id)
        folder = runtime_path(self.root, "data/benchmarks/state/apify") / attempt_id
        attempt = self.finances[1].get(attempt_id, {})
        for file in ("prepared.json", "terminal.json", "capture_manifest.json", "captured_rows.json", "mapped_rows.json",
                     "policy.json", "http_activity.json", "capture_progress.json", "recovery.json", "result.json"):
            required = file == "prepared.json" or file in {"capture_manifest.json", "captured_rows.json", "mapped_rows.json"} and bool(attempt.get("capture"))
            self.add(folder / file, "source_capture", origin + "/" + file, required=required,
                     json_hash=attempt.get("capture") if file == "captured_rows.json" else None)
        self.add(self.root / "config/local/apify_origins" / attempt_id / "origin.json", "public_setting",
                 origin + "/account_origin", required=False)
        for read_id in attempt.get("renewed_reads", {}):
            try:
                identifier(read_id)
            except SafetyError:
                self.note(self.unsafe, origin, "BACKUP_SELECTION_RENEWED_READ_INVALID"); continue
            self.add(folder / "funded_reads" / read_id / "funding.json", "source_capture", origin + "/original_read_funding", required=False)

    def acquisition(self, acquisition_id, route, origin):
        """Original known route locations also retain pre-import/crash proof."""
        if route in {"amazon-bestsellers", "amazon-search", "flipkart-search"}:
            folders = [self.memory / "direct_marketplace" / acquisition_id]
        elif route == "meta-ads":
            folders = [self.memory / "direct_meta" / acquisition_id]
        elif route == "meta-ads-depth":
            # The per-question receipt; the paid run's own files are kept with its ledger attempt.
            folders = [self.memory / "meta_depth" / acquisition_id]
        elif route == "google-trends":
            folders = [self.memory / "direct_trends" / acquisition_id]
        elif route in {"blinkit", "zepto", "instamart"}:
            folders = [self.memory / "quickcommerce_captures" / acquisition_id]
        elif route in {"google-suppliers", "google-maps", "indiamart", "tradeindia", "company-pages"}:
            folders = [self.memory / "supplier_routes" / acquisition_id]
        elif route in {"deodap", "baapstore", "dropship-india", "dropdash"}:
            # The per-type match receipt; the platforms' raw reads stay in their own rolling folders.
            folders = [self.memory / "dropship_platforms/matches" / acquisition_id]
        else:
            return
        for folder in folders:
            for name in ("receipt.json", "capture.json", "probe.json", "intent.json", "progress.json", "outer_deadline.json",
                         "supplier_outer_deadline.json", "original_response.json", "funded_intent.json"):
                self.add(folder / name, "source_capture", origin + "/original_acquisition/" + name, required=False)
            for native in (folder, folder / "native"):
                for name in ("native_result.json", "native_dispatch.json", "native_outer_deadline.json"):
                    self.add(native / name, "source_capture", origin + "/native/" + name, required=False)

    def source_stage_proof(self, proof, owner, origin, *, name=None):
        """Read one exact canonical proof, never paths hidden in its body."""
        try:
            if (not isinstance(proof, dict) or set(proof) != {"path", "sha256"}
                    or not isinstance(proof["path"], str)
                    or not isinstance(proof["sha256"], str) or not _HASH.fullmatch(proof["sha256"])):
                raise SafetyError("BACKUP_SELECTION_SOURCE_STAGE_PROOF_INVALID")
            path, _ = _lexical_reference(proof["path"], "source_capture", self.root)
            if (not any(path.is_relative_to(Path(root)) for root in owner["binding"]["artifact_roots"])
                    or name is not None and (not isinstance(name, str) or name in ("", ".", "..")
                        or Path(name).name != name or any(char in name for char in ("\x00", "\\", ":")))):
                raise SafetyError("BACKUP_SELECTION_SOURCE_STAGE_PROOF_INVALID")
        except SafetyError as error:
            self.note(self.unsafe, origin, str(error)); return
        # The canonical stage already enumerates its files. Preserve those
        # exact bytes without interpreting arbitrary nested source content as
        # another file manifest or widening the generic proof-name allowlist.
        # Artifact keys are logical labels: the supplier bridge deliberately
        # calls a retained capture.json "original.json" or "native-original-N".
        self.add(path, "source_capture", origin, byte_hash=proof["sha256"], close=False)

    def source_stage(self, stage_id, record, origin):
        """Close only validated acquisition stage metadata, without replay."""
        from .source_acquisitions import _record, _stage
        try:
            owner = _record(self.state, record.get("acquisition_id"))
            stage = _stage(self.state, owner, stage_id)
            kind = stage.get("stage_type")
            if kind == "response" and stage_id not in owner["response_ids"]:
                raise SafetyError("SOURCE_ACQUISITION_STAGE_INVALID")
            if kind == "parse":
                response_id = stage.get("response_id")
                if stage_id not in owner["parse_ids"] or response_id not in owner["response_ids"]:
                    raise SafetyError("SOURCE_ACQUISITION_STAGE_INVALID")
                response = _stage(self.state, owner, response_id)
                if response.get("stage_type") != "response" or response.get("response_hash") != stage.get("response_hash"):
                    raise SafetyError("SOURCE_ACQUISITION_STAGE_INVALID")
            elif kind not in ("response", "prelaunch_zero", "legacy_origin", "dispatch_intent", "execution_unknown"):
                raise SafetyError("SOURCE_ACQUISITION_STAGE_INVALID")
        except (SafetyError, OSError, KeyError, TypeError, ValueError) as error:
            reason = str(error) if isinstance(error, SafetyError) else "SOURCE_ACQUISITION_STAGE_INVALID"
            self.note(self.unsafe, origin, reason); return
        if kind == "parse":
            self.source_stage_proof(stage.get("parsed"), owner, origin + "/parsed")
        elif kind in ("response", "prelaunch_zero"):
            self.source_stage_proof(stage.get("receipt"), owner, origin + "/receipt")
            artifacts = stage.get("artifacts")
            if not isinstance(artifacts, dict):
                self.note(self.unsafe, origin + "/artifacts", "BACKUP_SELECTION_SOURCE_STAGE_PROOF_INVALID"); return
            for name, proof in artifacts.items():
                if (not isinstance(name, str) or name in ("", ".", "..") or Path(name).name != name
                        or any(char in name for char in ("\x00", "\\", ":"))):
                    self.note(self.unsafe, origin + "/artifacts", "BACKUP_SELECTION_SOURCE_STAGE_PROOF_INVALID"); continue
                self.source_stage_proof(proof, owner, origin + "/artifacts", name=name)

    def source_pointer_task(self, task_id, acquisition, route):
        """Validate a named caller from this supplied checkpoint only."""
        identifier(task_id)
        task = self.state["query_tasks"].get(task_id)
        if (not isinstance(task, dict) or task.get("kind") != "all_source_task_v1"
                or not isinstance(task.get("request"), dict) or not task["request"]
                or task.get("acquisition_id") != acquisition or task.get("route") != route
                or task.get("request_hash") != digest(task["request"])
                or task_id != key("source-task", [task.get("run_id"), route, task["request"]])):
            raise SafetyError("BACKUP_SELECTION_SOURCE_POINTER_CALLER_INVALID")
        identifier(task.get("run_id"))
        return task

    def source_pointer_owner(self, acquisition):
        from .source_acquisitions import _record
        if acquisition not in self.pointer_owners:
            try:
                self.pointer_owners[acquisition] = _record(self.state, acquisition)
            except (SafetyError, OSError, KeyError, TypeError, ValueError):
                self.pointer_owners[acquisition] = None
        owner = self.pointer_owners[acquisition]
        if owner is None:
            raise SafetyError("BACKUP_SELECTION_SOURCE_POINTER_OWNER_INVALID")
        return owner

    def source_pointer_shared(self, task, owner):
        """Recognized caller contracts bind the exact default physical read."""
        from .research_questions import physical_request_fingerprint
        binding = owner["binding"]
        pairs = (("source", "shared-source-caller-v1"), ("marketplace", "shared-marketplace-caller-v1"))
        present = False
        for prefix, contract in pairs:
            marker, physical = prefix + "_dispatch_contract", prefix + "_physical_request"
            if marker not in task and physical not in task:
                continue
            present = True
            if (task.get(marker) != contract or task.get(physical) != binding["request"]
                    or not isinstance(task.get(physical), dict)):
                raise SafetyError("BACKUP_SELECTION_SOURCE_POINTER_CONTRACT_INVALID")
        # These existing caller contracts carry no alternate method/options.
        # Do not infer their ownership of a POST or differently configured read.
        if (not present or task.get("acquisition_owner_ref") != binding["acquisition_id"]
                or task.get("route") != binding["route"] or binding["method"] != "GET"
                or binding["capture_options"] != {}
                or physical_request_fingerprint(binding["route"], binding["request"])
                   != binding["physical_request_fingerprint"]):
            raise SafetyError("BACKUP_SELECTION_SOURCE_POINTER_CONTRACT_INVALID")

    def source_pointer_original(self, task_id, task, owner):
        from .source_acquisitions import _stage
        acquisition = owner["binding"]["acquisition_id"]
        if acquisition not in self.pointer_origins:
            origins = []
            try:
                for stage_id in owner["stage_ids"]:
                    stage = self.state["source_checks"].get(stage_id)
                    if isinstance(stage, dict) and stage.get("stage_type") == "legacy_origin":
                        origins.append(_stage(self.state, owner, stage_id).get("origin"))
            except (SafetyError, KeyError, TypeError, ValueError):
                origins = None
            self.pointer_origins[acquisition] = origins
        origins = self.pointer_origins[acquisition]
        if origins is None:
            raise SafetyError("BACKUP_SELECTION_SOURCE_POINTER_ORIGIN_INVALID")
        if not any(isinstance(origin, dict) and origin.get("table") == "query_tasks"
                   and origin.get("id") == task_id and origin.get("question_id") is None
                   and origin.get("original") == task and origin.get("hash") == digest(task)
                   for origin in origins):
            # No field stripping: changed targets, runtime, generation or
            # receipts need their own producer-backed proof, not a guess.
            raise SafetyError("BACKUP_SELECTION_SOURCE_POINTER_ORIGIN_UNVERIFIED")

    def source_pointer(self, task_id, task, pointer):
        """Retain a shared reply without applying it to a different caller."""
        acquisition, route = task["acquisition_id"], task.get("route")
        origin = "query_tasks/" + task_id
        try:
            if (not isinstance(pointer, dict) or pointer.get("acquisition_id") != acquisition
                    or pointer.get("route") != route):
                raise SafetyError("BACKUP_SELECTION_SOURCE_POINTER_CHANGED")
            named_id = pointer.get("task_id", task_id)
            if named_id == task_id:
                if pointer.get("request_hash") != task.get("request_hash"):
                    raise SafetyError("BACKUP_SELECTION_SOURCE_POINTER_CHANGED")
                return  # Preserve exact direct/legacy matching, including no task_id.
            named = self.source_pointer_task(named_id, acquisition, route)
            if (pointer.get("request_hash") != named["request_hash"]
                    or not isinstance(pointer.get("receipt_hash"), str)
                    or not _HASH.fullmatch(pointer["receipt_hash"])):
                raise SafetyError("BACKUP_SELECTION_SOURCE_POINTER_CALLER_INVALID")
            owner = self.source_pointer_owner(acquisition)
            self.source_pointer_shared(named, owner)
            current = self.source_pointer_task(task_id, acquisition, route)
            if any(field in current for field in ("source_dispatch_contract", "source_physical_request",
                                                 "marketplace_dispatch_contract", "marketplace_physical_request")):
                self.source_pointer_shared(current, owner)
            else:
                self.source_pointer_original(task_id, current, owner)
        except (SafetyError, KeyError, TypeError, ValueError) as error:
            reason = (str(error) if isinstance(error, SafetyError)
                      and str(error).startswith("BACKUP_SELECTION_SOURCE_POINTER_")
                      else "BACKUP_SELECTION_SOURCE_POINTER_CALLER_INVALID")
            self.note(self.unsafe, origin, reason)

    def material(self, material_id, origin):
        try:
            identifier(material_id)
        except SafetyError:
            self.note(self.unsafe, origin, "BACKUP_SELECTION_MATERIAL_ID_INVALID"); return
        if material_id not in self.materials:
            self.materials.add(material_id)
            self.add(self.memory / "catalogue_materials/items" / material_id / "material.json", "catalogue_material", origin)

    def issue(self, issue_id, revision, origin, *, required=False):
        try:
            identifier(issue_id)
            if type(revision) is not int or not 1 <= revision <= 9007199254740991:
                raise SafetyError("BACKUP_SELECTION_REPORT_REVISION_INVALID")
        except SafetyError as error:
            self.note(self.unsafe, origin, str(error)); return
        binding = (issue_id, revision)
        if binding in self.issues and (self.issues[binding] or not required):
            return
        self.issues[binding] = required
        folder = runtime_path(self.root, "data/outputs") / issue_id / ("revision-" + str(revision))
        for name in ("package-input.json", "package-status.json", "delivery-manifest.json", "manifest-en.json",
                     "manifest-hi-Latn.json", "final_report_data.json", "shortlist.xlsx",
                     "private-staging-receipt.json", "private-staging-control.json",
                     "pdf-intent-en.json", "pdf-receipt-en.json", "pdf-intent-hi-Latn.json", "pdf-receipt-hi-Latn.json"):
            self.add(folder / name, "report_package", origin + "/" + name,
                     required=required and name in {"package-input.json", "delivery-manifest.json"})
        # The original renderer/stage can be interrupted before a delivery
        # manifest exists. Preserve its exact owned intent/payload/receipt, not
        # every file in an output folder or any newly reconstructed document.
        stages = folder / "internal-stages"
        for name in ("core-intent.json", "core-receipt.json", "checked-product-facts.json", "checked-product-facts.payload.json",
                     "workbook-intent.json", "workbook-receipt.json", "shortlist.xlsx", "shortlist.payload.xlsx",
                     "edition-en-intent.json", "edition-en-receipt.json", "edition-hi-Latn-intent.json", "edition-hi-Latn-receipt.json",
                     "pdf-intent-en.json", "pdf-receipt-en.json", "pdf-intent-hi-Latn.json", "pdf-receipt-hi-Latn.json"):
            self.add(stages / name, "report_package", origin + "/internal-stages/" + name, required=False)
        media = runtime_path(self.root, "media/live/reports") / issue_id / ("revision-" + str(revision))
        # A copy can finish before the packet/hold is saved. Preserve the one
        # known revision receipt and only its exact declared display bytes.
        # Shared staging directories and unrelated pictures are not traversed.
        self.add(media / "assets/receipt.json", "report_package", origin + "/media/assets", required=False)
        for name in ("report-en.html", "report-hi-Latn.html", "report-en.pdf", "report-hi-Latn.pdf"):
            self.add(media / name, "report_package", origin + "/media/" + name, required=False)
            self.add(media / "internal-stages" / name, "report_package", origin + "/media/internal-stages/" + name, required=False)
        for language in ("en", "hi-Latn"):
            self.add(media / "internal-stages" / ("report-" + language + ".payload.html"),
                     "report_package", origin + "/media/internal-payload/" + language, required=False)

    def trends(self, evidence_id, record):
        """Only exact historical acquisition locations; no raw-data synthesis."""
        origin = "trends_evidence/" + evidence_id
        try:
            acquisition = identifier(record.get("acquisition_id"))
        except SafetyError:
            self.note(self.unsafe, origin, "BACKUP_SELECTION_ACQUISITION_INVALID"); return
        parsed = record.get("parsed")
        if not isinstance(parsed, dict) or not isinstance(parsed.get("raw_sha256"), str) or not _HASH.fullmatch(parsed["raw_sha256"]):
            self.note(self.unresolved, origin, "BACKUP_SELECTION_TRENDS_RAW_REFERENCE_MISSING"); return
        found = False
        for base in (self.memory / "direct_trends", runtime_path(self.root, "data/research/connected-trends"),
                     runtime_path(self.root, "media/live/trends"), self.memory / "trends_captures"):
            folder = base / acquisition
            receipt = self.add(folder / "receipt.json", "source_capture", origin + "/receipt", required=False)
            if receipt is None:
                continue
            if not isinstance(receipt, dict) or receipt.get("acquisition_id", acquisition) != acquisition:
                self.note(self.unsafe, origin, "BACKUP_SELECTION_TRENDS_RECEIPT_CHANGED"); continue
            csv = folder / "multiTimeline.csv"
            self.add(csv, "source_capture", origin + "/original_csv", byte_hash=parsed["raw_sha256"])
            found = found or str(csv) in self.files and self.files[str(csv)]["expected_sha256"] == parsed["raw_sha256"]
        if not found and re.fullmatch(r"(?:connected-csv-|csv-|reviewed-csv-binding-)[0-9a-f]{40}", acquisition):
            found = self.legacy_trends_export(parsed, origin)
        if (not found and re.fullmatch(r"connected-related-[0-9a-f]{40}", acquisition)
                and record.get("evidence_kind") == "related_queries"):
            found = self.legacy_related_export(parsed, origin)
        if not found:
            self.note(self.unresolved, origin, "BACKUP_SELECTION_TRENDS_RAW_REFERENCE_MISSING")

    def legacy_trends_export(self, parsed, origin):
        """Bind preserved supervised exports, never infer or redownload CSV.

        Historical imports used a derived acquisition identity while preserving
        the export in a product/date folder. Its original metadata and exact raw
        hash, not the folder spelling or a filename similarity, bind that file.
        Only this one existing public export directory is indexed per selection.
        """
        fields = _TRENDS_SCOPE_FIELDS
        metadata = parsed.get("metadata")
        if (parsed.get("parser_version") != "google-trends-csv-v1" or parsed.get("acquisition_route") != "browser_csv"
                or not isinstance(metadata, dict) or any(name not in metadata for name in fields)
                or type(metadata.get("scope_verified")) is not bool
                or not isinstance(parsed.get("raw_sha256"), str) or not _HASH.fullmatch(parsed["raw_sha256"])):
            return False
        scope = digest({name: metadata[name] for name in fields})
        self.legacy_trends_index()
        candidates = (self.legacy_trends_exports.get(parsed["raw_sha256"], [])
                      + self.legacy_trends_scopes.get(scope, []))
        for path, original in candidates:
            if any(original.get(name) != metadata[name] for name in fields):
                continue
            bound = self.add(path, "source_capture", origin + "/original_metadata", json_hash=digest(original))
            if bound is None:
                continue
            csv = path.parent / "multiTimeline.csv"
            self.add(csv, "source_capture", origin + "/original_csv", byte_hash=parsed["raw_sha256"])
            if str(csv) in self.files and self.files[str(csv)]["expected_sha256"] == parsed["raw_sha256"]:
                self.add(path.parent / "browser-capture.json", "source_capture", origin + "/original_browser_capture", required=False)
                return True
        for path, original, csv, scope_path in self.legacy_trends_receipts.get(scope, []):
            if original["csv_sha256"] != parsed["raw_sha256"]:
                continue
            bound = self.add(path, "source_capture", origin + "/original_receipt",
                             json_hash=digest(original), close=False)
            if bound is None:
                continue
            self.add(csv, "source_capture", origin + "/original_csv", byte_hash=parsed["raw_sha256"])
            if str(csv) in self.files and self.files[str(csv)]["expected_sha256"] == parsed["raw_sha256"]:
                self.add(scope_path, "source_capture", origin + "/original_scope",
                         required="scope_path" in original, close=False)
                return True
        return False

    def legacy_trends_index(self):
        """Index known public receipt shapes, not their unrelated CSV bytes."""
        if self.legacy_trends_exports is not None:
            return
        self.legacy_trends_exports = {}
        base = runtime_path(self.root, "media/live/trends")
        if base.is_symlink():
            return
        for path in sorted(base.glob("*/metadata.json")):
            try:
                _lexical_reference(path, "source_capture", self.root)
                value = decode_json(_read(path, MAX_JSON_BYTES))
                if (not isinstance(value, dict) or _has_secret_field(value)
                        or type(value.get("scope_verified")) is not bool):
                    continue
                # Extra or partial modern fields cannot downgrade to either
                # historical shape. The exact raw hash lives in canonical
                # parsed evidence; no CSV is read while building this index.
                if set(value) == set(_TRENDS_SCOPE_FIELDS):
                    self.legacy_trends_scopes.setdefault(digest(value), []).append((path, value))
                elif set(value) == set(_TRENDS_RELATED_FIELDS):
                    self.legacy_related_scopes.setdefault(digest(value), []).append((path, value))
                elif (value.get("route") == "connected_tool_browser_csv"
                        and isinstance(value.get("csv_sha256"), str) and _HASH.fullmatch(value["csv_sha256"])):
                    self.legacy_trends_exports.setdefault(value["csv_sha256"], []).append((path, value))
            except SafetyError:
                continue  # An unrelated export cannot certify this record.
        for name in ("receipt.json", "reopened-receipt.json"):
            for path in sorted(base.glob("*/" + name)):
                try:
                    _lexical_reference(path, "source_capture", self.root)
                    value = decode_json(_read(path, MAX_JSON_BYTES))
                    if (not isinstance(value, dict) or _has_secret_field(value)
                            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                            or value.get("route") not in ("connected_playwright_browser_csv", "connected_tool_browser_csv")
                            or any(field not in value for field in _TRENDS_SCOPE_FIELDS)
                            or type(value.get("scope_verified")) is not bool
                            or not isinstance(value.get("csv_sha256"), str) or not _HASH.fullmatch(value["csv_sha256"])
                            or not isinstance(value.get("csv_path"), str)):
                        continue
                    csv, _ = _lexical_reference(value["csv_path"], "source_capture", self.root)
                    if csv.parent != path.parent or csv.suffix.casefold() != ".csv":
                        continue
                    if (value["route"] == "connected_playwright_browser_csv"
                            and (value.get("state") != "Captured" or not isinstance(value.get("scope_path"), str))):
                        continue
                    scope_path, _ = _lexical_reference(value.get("scope_path", path.parent / "scope.json"),
                                                       "source_capture", self.root)
                    if scope_path.parent != path.parent or scope_path.suffix != ".json":
                        continue
                    scope = digest({field: value[field] for field in _TRENDS_SCOPE_FIELDS})
                    self.legacy_trends_receipts.setdefault(scope, []).append((path, value, csv, scope_path))
                except SafetyError:
                    continue

    def legacy_related_export(self, parsed, origin):
        """Retain the exact ten-field export behind a saved v2 related record."""
        metadata = parsed.get("metadata")
        fields = set(_TRENDS_RELATED_FIELDS)
        if (set(parsed) != {"metadata", "parser_version", "queries", "raw_sha256", "reason", "state", "top_queries"}
                or parsed.get("parser_version") != "google-trends-related-csv-v2"
                or not isinstance(metadata, dict) or set(metadata) not in (fields, fields | {"export_date_range", "export_date_format"})
                or type(metadata.get("scope_verified")) is not bool
                or metadata.get("widget") != "related_queries" or metadata.get("displayed_section") != "Rising"):
            return False
        self.legacy_trends_index()
        scope = digest({field: metadata[field] for field in _TRENDS_RELATED_FIELDS})
        for path, original in self.legacy_related_scopes.get(scope, []):
            bound = self.add(path, "source_capture", origin + "/original_metadata", json_hash=digest(original), close=False)
            if bound is None:
                continue
            csv = path.parent / "relatedQueries.csv"
            validated = False
            def validate(raw):
                nonlocal validated
                from .trends_related import parse_related_queries_csv
                # Canonical v2 metadata may contain two date fields derived
                # from the exact CSV header. Preserve, rather than strip or
                # invent, those fields; false scope stays Insufficient.
                derived = parse_related_queries_csv(raw, original)
                if digest(derived["metadata"]) != digest(metadata):
                    raise SafetyError("BACKUP_SELECTION_TRENDS_RELATED_METADATA_CHANGED")
                validated = True
            self.add(csv, "source_capture", origin + "/original_csv", byte_hash=parsed["raw_sha256"],
                     validate_bytes=validate)
            if validated and str(csv) in self.files and self.files[str(csv)]["expected_sha256"] == parsed["raw_sha256"]:
                self.add(path.parent / "browser-capture.json", "source_capture",
                         origin + "/original_browser_capture", required=False, close=False)
                return True
        return False

    def close(self):
        while self.queue:
            path, value, kind, depth = self.queue.popleft()
            tag = (str(path), kind)
            if tag in self.closed:
                continue
            self.closed.add(tag)
            if path.name == "http_activity.json" and isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        self.note(self.unsafe, path.name, "BACKUP_SELECTION_HTTP_ACTIVITY_INVALID"); continue
                    try:
                        reference = identifier(item.get("id"))
                    except SafetyError:
                        self.note(self.unsafe, path.name, "BACKUP_SELECTION_HTTP_ACTIVITY_INVALID"); continue
                    metadata = self.add(path.parent / "http" / (reference + ".json"), "source_capture",
                                        "financial_http/" + reference, required=item.get("received_bytes") is not None, depth=depth + 1)
                    if isinstance(metadata, dict):
                        self.add(path.parent / "http" / (reference + ".body.json"), "source_capture",
                                 "financial_http/" + reference + "/body", byte_hash=metadata.get("hash"), depth=depth + 1)
                continue
            origin, next_depth = _logical(self.root, path).as_posix(), depth + 1
            if kind == "report_package" and (path.parts[-2:] == ("assets", "receipt.json")
                    or isinstance(value, dict) and value.get("contract") == REPORT_ASSET_RECEIPT):
                self.report_assets(path, value, origin, next_depth)
                continue
            if not isinstance(value, dict):
                if kind in {"model_receipt", "operating_control", "catalogue_material"}:
                    self.note(self.unsafe, path.name, "BACKUP_SELECTION_DOCUMENT_INVALID")
                continue
            # Interpret only known top-level schema fields. Do not recursively
            # follow paths, URLs or instructions hidden in product/source text.
            value = dict(value)
            for field in ("source_jobs", "job_ids", "source_job_ids", "product_facts", "products", "receipts", "records", "material_ids"):
                if field in value and not isinstance(value[field], list):
                    self.note(self.unsafe, origin, "BACKUP_SELECTION_DOCUMENT_FIELD_INVALID")
                    value[field] = []
            for field in ("artifacts", "pdf_receipts", "bundle", "recovery", "receipt", "issue"):
                if field in value and not isinstance(value[field], dict):
                    self.note(self.unsafe, origin, "BACKUP_SELECTION_DOCUMENT_FIELD_INVALID")
                    value[field] = {}
            if kind in {"source_capture", "catalogue_material", "report_package", "operating_control"}:
                artifacts = value.get("artifacts", {})
                if isinstance(artifacts, dict):
                    for name, proof in artifacts.items():
                        if isinstance(proof, str):
                            if name not in _PROOF_FILES:
                                self.note(self.unsafe, origin, "BACKUP_SELECTION_ARTIFACT_NAME_INVALID"); continue
                            self.add(path.parent / name, kind, origin + "/artifacts/" + name, byte_hash=proof, depth=next_depth)
                        elif isinstance(proof, dict) and "path" in proof:
                            self.add(proof["path"], kind, origin + "/artifacts/" + name,
                                     byte_hash=proof.get("sha256"), depth=next_depth)
                            if kind == "report_package" and proof.get("payload_path") is not None:
                                self.add(proof["payload_path"], kind, origin + "/payloads/" + name,
                                         byte_hash=proof.get("sha256"), depth=next_depth)
                        else:
                            self.note(self.unsafe, origin, "BACKUP_SELECTION_ARTIFACT_INVALID")
                else:
                    self.note(self.unsafe, origin, "BACKUP_SELECTION_ARTIFACT_INVALID")
            if kind == "operating_control":
                self.control(path, value, origin)
            elif kind == "model_receipt":
                for job_id in value.get("source_jobs", []):
                    self.job(job_id, origin + "/source_jobs", required=True)
            elif kind == "catalogue_material":
                for material_id in value.get("material_ids", []):
                    self.material(material_id, origin + "/material_ids")
                for field in ("page_reference", "original_reference", "manifest_reference", "prepared_reference"):
                    if field in value:
                        self.reference(value[field], "source_capture" if field == "original_reference" else kind, origin + "/" + field,
                                       required=field not in {"original_reference", "manifest_reference"}, depth=next_depth)
                receipt = value.get("receipt", {})
                if isinstance(receipt, dict) and isinstance(receipt.get("origin"), dict) and receipt["origin"].get("kind") == "verified_marketplace":
                    self.reference(receipt["origin"].get("receipt"), "source_capture", origin + "/original_marketplace", depth=next_depth)
                if value.get("reference") and "receipt" in value:
                    self.reference(value["reference"], "source_capture", origin + "/original_page", required=False, depth=next_depth)
            elif kind == "report_package":
                for field in ("job_ids", "source_job_ids"):
                    for job_id in value.get(field, []):
                        self.job(job_id, origin + "/" + field, required=True)
                source = value.get("presentation_source")
                if isinstance(source, dict):
                    self.issue(value.get("issue", value).get("issue_id"), source.get("revision"), origin + "/presentation_source", required=True)
                for proof in value.get("pdf_receipts", {}).values():
                    if isinstance(proof, dict) and "path" in proof:
                        self.add(proof["path"], kind, origin + "/pdf_receipts", byte_hash=proof.get("pdf_sha256"), depth=next_depth)
                if path.name in {"pdf-intent-en.json", "pdf-intent-hi-Latn.json", "pdf-receipt-en.json", "pdf-receipt-hi-Latn.json"}:
                    self.add(value.get("path"), kind, origin + "/rendered_pdf", byte_hash=value.get("pdf_sha256"),
                             required=path.name.startswith("pdf-receipt-"), depth=next_depth)
                for product in value.get("product_facts", value.get("products", [])):
                    if isinstance(product, dict):
                        image = product.get("image_provenance", product.get("image"))
                        if isinstance(image, dict) and "path" in image:
                            self.add(image["path"], "public_image", origin + "/product_photo", byte_hash=image.get("sha256"), depth=next_depth)
            elif kind == "source_capture":
                for field, expected in (("csv_path", value.get("csv_sha256")), ("screenshot_path", None)):
                    if value.get(field) is not None:
                        self.add(value[field], kind, origin + "/" + field, byte_hash=expected, depth=next_depth)
                for field in ("native_reference", "native_wrapper_reference"):
                    self.native_reference(value, field, kind, origin, depth=next_depth)
                recovery = value.get("recovery", {})
                if isinstance(recovery, dict):
                    original_file = recovery.get("original_receipt_file")
                    if recovery.get("version") == "source-deadline-reparse-v1":
                        original_file = "receipt.json"
                    if original_file is not None:
                        if not isinstance(original_file, str) or original_file not in _PROOF_FILES:
                            self.note(self.unsafe, origin, "BACKUP_SELECTION_ARTIFACT_NAME_INVALID")
                        else:
                            self.add(path.parent / original_file, kind, origin + "/original_receipt",
                                     byte_hash=recovery.get("original_receipt_sha256"), depth=next_depth)
                if value.get("original_reference") is not None:
                    if value.get("version") == "supplier-original-dispatch-recovery-v1":
                        self.add(value["original_reference"], kind, origin + "/original_reference",
                                 byte_hash=value.get("original_sha256"), depth=next_depth)
                    else:
                        self.reference(value["original_reference"], kind, origin + "/original_reference",
                                       json_hash=value.get("original_hash"), depth=next_depth)
                if path.name == "capture-dispatch-recovery.json":
                    self.add(path.parent / "dispatch-recovery.json", kind, origin + "/dispatch_recovery", depth=next_depth)
                proofs = value.get("dispatch_proofs", {})
                if isinstance(proofs, dict):
                    for proof in proofs.values():
                        if isinstance(proof, dict) and "dispatch_reference" in proof:
                            self.add(proof["dispatch_reference"], kind, origin + "/original_dispatch",
                                     byte_hash=proof.get("dispatch_sha256"), depth=next_depth)
                if value.get("page_reference") is not None:
                    self.reference(value["page_reference"], kind, origin + "/supplier_page", json_hash=value.get("page_hash"), depth=next_depth)
                bundle = value.get("bundle", {})
                receipts = value.get("receipts", bundle.get("receipts", []) if isinstance(bundle, dict) else [])
                if isinstance(value.get("records"), list):
                    receipts = list(receipts) if isinstance(receipts, list) else []
                    receipts += [row["receipt"] for row in value["records"] if isinstance(row, dict) and isinstance(row.get("receipt"), dict)]
                if isinstance(value.get("receipt"), dict):
                    receipts = list(receipts) if isinstance(receipts, list) else []
                    receipts.append(value["receipt"])
                for receipt in receipts if isinstance(receipts, list) else []:
                    if isinstance(receipt, dict):
                        for field in ("native_reference", "native_wrapper_reference"):
                            self.native_reference(receipt, field, kind, origin + "/receipts", depth=next_depth)
                        recovered = receipt.get("native_deadline_recovery")
                        if isinstance(recovered, dict) and recovered.get("reference"):
                            self.add(recovered["reference"], kind, origin + "/native_recovery",
                                     byte_hash=recovered.get("sha256"), depth=next_depth)
                        dispatch = receipt.get("saved_dispatch_recovery")
                        if isinstance(dispatch, dict) and dispatch.get("dispatch_reference"):
                            self.add(dispatch["dispatch_reference"], kind, origin + "/saved_dispatch",
                                     byte_hash=dispatch.get("dispatch_sha256"), depth=next_depth)
                for field, expected in (("snapshot_file", value.get("raw_hash")), ("parsed_file", None), ("scope_file", None),
                                        ("interpretation_file", None)):
                    name = value.get(field)
                    if name is None:
                        continue
                    if not isinstance(name, str) or name not in _PROOF_FILES:
                        self.note(self.unsafe, origin, "BACKUP_SELECTION_ARTIFACT_NAME_INVALID"); continue
                    self.add(path.parent / name, kind, origin + "/" + field,
                             byte_hash=value.get("artifacts", {}).get(name, expected), depth=next_depth)
                if path.name == "http_activity.json":
                    self.note(self.unsafe, origin, "BACKUP_SELECTION_HTTP_ACTIVITY_INVALID")
                if value.get("reference") and "target_results" in value:
                    self.reference(value["reference"], "source_capture", origin + "/source_receipt", depth=next_depth)
            if path.name == "pending.json" and isinstance(value.get("receipt_hash"), str):
                expected = value["receipt_hash"]
                self.add(path.parent / (expected + ".json"), "source_capture", origin + "/returned_receipt",
                         json_hash=expected, depth=next_depth)

    def public_settings(self, path, value, origin):
        relative = _logical(self.root, path).as_posix()
        try:
            if not isinstance(value, dict):
                raise SafetyError("BACKUP_PUBLIC_SETTINGS_INVALID")
            if relative == "config/operations/backup.json":
                validate_backup_settings(value)
            elif relative == "config/service.json":
                from .service import _validate_policy
                _validate_policy(value)
            elif relative == "config/research/catalogue.json":
                from .catalogue import validate_catalogue
                validate_catalogue(value)
            elif relative == "config/research/collection_cost_baseline.json":
                from .programme_cost_snapshot import _baseline
                baseline = value.get("baseline")
                if not isinstance(baseline, dict):
                    raise SafetyError("BACKUP_PUBLIC_SETTINGS_INVALID")
                _baseline(value, timestamp(baseline.get("observed_at")))
            elif relative == "config/ai/codex_luna.json":
                if (set(value) - {"model", "reasoning_effort", "authentication", "paid_fallback", "executable", "probe_timeout_seconds"}
                        or value.get("authentication") != "chatgpt" or value.get("paid_fallback") is not False
                        or not isinstance(value.get("model"), str) or value.get("reasoning_effort") != "high"
                        or "probe_timeout_seconds" in value and (type(value["probe_timeout_seconds"]) is not int
                                                                 or not 1 <= value["probe_timeout_seconds"] <= 90)
                        or "executable" in value and (not isinstance(value["executable"], str) or not Path(value["executable"]).is_absolute())):
                    raise SafetyError("BACKUP_PUBLIC_SETTINGS_INVALID")
            elif relative == "config/research/spend_watch.json":
                from .spend_watch import validate_settings
                validate_settings(value)
            elif relative == "config/research/signed_in_reads.json":
                from .signed_in_pacing import validate_pacing_settings
                validate_pacing_settings(value)
            elif relative == "config/research/dropship_platforms.json":
                from .dropship_platforms import validate_settings
                validate_settings(value)
            elif relative == "config/research/crowding.json":
                from .crowding import validate_thresholds
                validate_thresholds(value)
            elif relative == "config/research/opportunity_score.json":
                from .opportunity_score import validate_settings as validate_score_settings
                validate_score_settings(value)
            elif relative == "config/research/apify_allocation.json":
                from .meta_depth_money import validate_allocation
                validate_allocation(value)
            elif relative == "config/benchmarks/meta_first.json":
                from .meta_depth import validate_reviewed_config
                validate_reviewed_config(value)
            elif relative == "config/research/wider_discovery.json":
                from .wider_candidates import validate_settings
                validate_settings(value)
            elif relative == "config/research/round_limits.json":
                from .round_limits import validate_round_limits
                validate_round_limits(value)
            elif relative == "config/research/product_map.json":
                from .map_materials import validate_settings
                validate_settings(value)
            elif relative == "config/research/quickcommerce_locations.json":
                from .quickcommerce_plan import validate_settings
                validate_settings(value)
            elif relative.endswith("/origin.json"):
                if (set(value) != {"schema_version", "attempt_id", "config_hash", "account_id", "namespace", "generation"}
                        or type(value["schema_version"]) is not int or value["schema_version"] != 1
                        or value["attempt_id"] != path.parent.name or not _HASH.fullmatch(value["config_hash"])
                        or any(not isinstance(value[field], str) for field in ("account_id", "namespace", "generation"))):
                    raise SafetyError("BACKUP_PUBLIC_SETTINGS_INVALID")
        except (SafetyError, KeyError, TypeError, ValueError) as error:
            self.files.pop(str(path), None)
            self.total = sum(row["bytes"] for row in self.files.values())
            self.note(self.unsafe, origin, str(error) if isinstance(error, SafetyError) else "BACKUP_PUBLIC_SETTINGS_INVALID", relative)

    def report_assets(self, path, value, origin, depth):
        """No arbitrary child paths: resolve this checked owner and hash only."""
        try:
            parts = _logical(self.root, path).parts
            if (len(parts) != 7 or parts[:3] != ("media", "live", "reports")
                    or parts[-2:] != ("assets", "receipt.json")
                    or not re.fullmatch(r"revision-[1-9][0-9]{0,15}", parts[4])):
                raise SafetyError("BACKUP_SELECTION_REPORT_ASSET_OWNER_INVALID")
            receipt = validate_report_asset_receipt(value, issue_id=parts[3], revision=int(parts[4][9:]))
        except (SafetyError, ValueError, TypeError, KeyError) as error:
            reason = str(error) if isinstance(error, SafetyError) else "BACKUP_SELECTION_REPORT_ASSET_RECEIPT_INVALID"
            self.note(self.unsafe, origin, reason)
            return
        self.files[str(path)]["expected_json_digest"] = digest(receipt)
        extensions = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
        for row in receipt["plan"]["entries"]:
            if row["state"] == "ready":
                display = row["display"]
                self.add(path.parent / (display["sha256"] + extensions[display["format"]]), "report_package",
                         origin + "/" + row["asset_id"], byte_hash=display["sha256"], required=True,
                         close=False, depth=depth)

    def control(self, path, value, origin):
        if path.name == "pending-control.json":
            record = value.get("record")
            if not isinstance(record, dict) or set(value) != {"record", "record_hash", "previous_hash"} or digest(record) != value["record_hash"]:
                self.note(self.unsafe, origin, "BACKUP_SELECTION_PROGRAMME_CONTROL_INVALID"); return
            from .programme import _binding
            try:
                original = self.state["batch_runs"].get(key("programme", record.get("programme_id")))
                if (not isinstance(original, dict) or _binding(original) != _binding(record)
                        or value["record_hash"] != digest(original) and value["previous_hash"] != digest(original)):
                    raise SafetyError("BACKUP_SELECTION_PROGRAMME_CONTROL_INVALID")
            except (SafetyError, KeyError, TypeError, ValueError):
                self.note(self.unsafe, origin, "BACKUP_SELECTION_PROGRAMME_CONTROL_INVALID")
        elif path.parent.name == "programme_service" and path.name == "control.json":
            control = value.get("control")
            if (set(value) != {"control", "hash"} or not isinstance(control, dict)
                    or set(control) != {"schema_version", "enabled", "at"} or value["hash"] != digest(control)
                    or type(control["schema_version"]) is not int or control["schema_version"] != 1
                    or type(control["enabled"]) is not bool):
                self.note(self.unsafe, origin, "BACKUP_SELECTION_SERVICE_CONTROL_INVALID")
            elif control["at"] is not None:
                try:
                    timestamp(control["at"])
                except SafetyError:
                    self.note(self.unsafe, origin, "BACKUP_SELECTION_SERVICE_CONTROL_INVALID")


def select_essential_backup_files(research_state, financial_state, *, root, pending_controls=(), multipart=False,
                                 freeze_bytes=None):
    """Read selected owned bytes only; supplied states are never refreshed.

    ``root`` is the project root; ``financial_state`` is Journal.state()'s
    already-verified (budgets, attempts) pair. Pending controls may be exact
    paths or {path, record_hash} descriptors, never arbitrary folder roots.
    An explicit v3 ``freeze_bytes`` callback receives one already-validated
    file at a time. The default remains read-only; no all-file buffer is kept.
    """
    if (not isinstance(research_state, dict) or set(research_state) != set(TABLES)
            or any(not isinstance(rows, dict) for rows in research_state.values())
            or any(not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ref)
                   for rows in research_state.values() for ref in rows)
            or any(not isinstance(record, dict) for rows in research_state.values() for record in rows.values())
            or not isinstance(financial_state, (tuple, list)) or len(financial_state) != 2
            or any(not isinstance(rows, dict) for rows in financial_state)
            or not isinstance(pending_controls, (tuple, list)) or len(pending_controls) > 1000):
        raise SafetyError("BACKUP_SELECTION_INPUT_INVALID")
    if (type(multipart) is not bool
            or freeze_bytes is not None and (not multipart or not callable(freeze_bytes))):
        raise SafetyError("BACKUP_SELECTION_INPUT_INVALID")
    selection = _Selection(research_state, financial_state, safe_path(_project_root(root)),
                           multipart=multipart, freeze_bytes=freeze_bytes)
    for capture_id, capture in research_state["captures"].items():
        if not isinstance(capture, dict):
            selection.note(selection.unsafe, "captures", "BACKUP_SELECTION_RECORD_INVALID"); continue
        ref = capture.get("reference")
        expected = capture.get("capture_hash") if isinstance(ref, str) and capture.get("source") != "meta" else None
        selection.reference(ref, "source_capture", "captures/" + capture_id, json_hash=expected)
    for job_id in research_state["jobs"]:
        selection.job(job_id, "jobs/" + job_id)
    for evidence_id, record in research_state["trends_evidence"].items():
        selection.trends(evidence_id, record)
    from .source_acquisitions import STAGE_KIND
    citation_only = False
    for table in ("assessments", "supplier_assessments", "market_reviews", "problems", "catalogue_additions", "source_checks"):
        for record_id, record in research_state[table].items():
            if not isinstance(record, dict):
                selection.note(selection.unsafe, table, "BACKUP_SELECTION_RECORD_INVALID"); continue
            origin = table + "/" + record_id
            if table == "source_checks" and record.get("kind") == STAGE_KIND:
                selection.source_stage(record_id, record, origin)
                continue
            if record.get("job_id"):
                selection.job(record["job_id"], origin + "/job", required=True)
            if table == "catalogue_additions" and record.get("material_id"):
                selection.material(record["material_id"], origin + "/material")
            if table in {"supplier_assessments", "source_checks"}:
                for field in ("receipt_reference", "reference"):
                    if record.get(field) and record.get("kind") != "public_product_image":
                        if table == "source_checks" and field == "reference" and _legacy_observation_citation(record_id, record):
                            selection.note(selection.optional, origin + "/reference", "BACKUP_SELECTION_CITATION_ONLY")
                            citation_only = True
                        else:
                            selection.reference(record[field], "source_capture", origin + "/" + field)
                if isinstance(record.get("envelope"), dict) and record["envelope"].get("reference"):
                    selection.reference(record["envelope"]["reference"], "source_capture", origin + "/envelope")
                for ref in record.get("envelope", {}).get("evidence_refs", []) if isinstance(record.get("envelope"), dict) else []:
                    selection.reference(ref, "source_capture", origin + "/evidence_ref")
            if record.get("kind") == "public_product_image":
                image = record.get("image")
                if isinstance(image, dict):
                    selection.add(image.get("path"), "public_image", origin + "/image", byte_hash=image.get("sha256"))
                if isinstance(record.get("reference"), str):
                    selection.add(selection.memory / "image_captures" / record["reference"] / "capture.json",
                                  "source_capture", origin + "/image_capture", required=False)
    for task_id, task in research_state["query_tasks"].items():
        if not isinstance(task, dict) or not task.get("acquisition_id"):
            continue
        acquisition = task["acquisition_id"]
        try:
            identifier(acquisition)
        except SafetyError:
            selection.note(selection.unsafe, "query_tasks/" + task_id, "BACKUP_SELECTION_ACQUISITION_INVALID"); continue
        pointer = selection.memory / "all_source_dispatch/returned_receipts" / acquisition / "pending.json"
        saved = selection.add(pointer, "operating_control", "query_tasks/" + task_id + "/returned", required=False)
        if saved is not None:
            selection.source_pointer(task_id, task, saved)
        selection.acquisition(acquisition, task.get("route"), "query_tasks/" + task_id)
        for ref in task.get("receipt_refs", []):
            if ref not in research_state["source_checks"]:
                selection.note(selection.unresolved, "query_tasks/" + task_id, "BACKUP_SELECTION_CANONICAL_RECEIPT_MISSING")
    for attempt_id in financial_state[1]:
        selection.attempt(attempt_id, "financial_attempts/" + attempt_id)
    for record_id, record in research_state["batch_runs"].items():
        if not isinstance(record, dict):
            continue
        origin = "batch_runs/" + record_id
        if record.get("kind") == "research-programme-v1":
            selection.add(selection.memory / "programme_runs" / key("programme", record.get("programme_id")) / "pending-control.json",
                          "operating_control", origin + "/control", required=False)
            for issue_id, draft in record.get("drafts", {}).items():
                if isinstance(draft, dict):
                    for field in ("revision", "retained_revision"):
                        if draft.get(field) is not None:
                            selection.issue(issue_id, draft[field], origin + "/draft", required=draft.get("complete") is True)
        elif record.get("kind") == "all_source_run_v1":
            selection.add(selection.memory / "all_source_dispatch/pass_returns" / (record_id + ".json"),
                          "operating_control", origin + "/progress", required=False)
        for field in ("manifest_reference", "reference", "prepared_reference"):
            if record.get(field):
                selection.reference(record[field], "catalogue_material", origin + "/" + field, required=field != "manifest_reference")
        for branch in record.get("planned_branches", []):
            if isinstance(branch, dict):
                if branch.get("prepared_reference"):
                    selection.reference(branch["prepared_reference"], "catalogue_material", origin + "/prepared")
                for material_id in branch.get("material_ids", []):
                    selection.material(material_id, origin + "/material")
        for branch in record.get("branches", {}).values():
            if isinstance(branch, dict) and branch.get("prepared_reference"):
                selection.reference(branch["prepared_reference"], "catalogue_material", origin + "/prepared")
        if isinstance(record.get("accepted_matrix"), dict):
            selection.reference(record["accepted_matrix"].get("reference"), "catalogue_material", origin + "/matrix",
                                json_hash=record["accepted_matrix"].get("hash"))
    for table in ("issue_reservations", "published_history"):
        for record_id, record in research_state[table].items():
            if isinstance(record, dict) and record.get("issue_id") and record.get("revision") is not None:
                selection.issue(record["issue_id"], record["revision"], table + "/" + record_id,
                                required=table == "published_history")
    selection.add(selection.memory / "programme_service/control.json", "operating_control", "programme_service", required=False)
    # Spend-watch records: the allowance state and every dated alert log.
    selection.add(selection.memory / "spend_watch/state.json", "operating_control", "spend_watch", required=False)
    alerts = selection.memory / "spend_watch/alerts"
    if alerts.is_symlink():
        selection.note(selection.unsafe, "spend_watch/alerts", "SYMLINK_PATH_REFUSED")
    elif alerts.is_dir():
        for path in sorted(alerts.glob("*.jsonl")):
            selection.add(path, "operating_control", "spend_watch/alerts", required=False)
    for control in pending_controls:
        path = control.get("path") if isinstance(control, dict) else control
        saved = selection.add(path, "operating_control", "supplied_pending_control")
        if isinstance(control, dict) and (set(control) != {"path", "record_hash"}
                or not isinstance(saved, dict) or saved.get("record_hash") != control["record_hash"]):
            selection.note(selection.unsafe, "supplied_pending_control", "BACKUP_SELECTION_PROGRAMME_CONTROL_INVALID")
    for relative, kind in ([("scripts/" + name, "application_code") for name in _SCRIPTS]
                           + [("portal/" + name, "application_code") for name in _PORTAL]
                           + [(name, "application_code") for name in _TESTS]
                           + [(name, "public_setting") for name in _PUBLIC_SETTINGS]
                           + [(name, "dependency_manifest") for name in _DEPENDENCIES]
                           + [(name, "public_asset") for name in _ASSETS]
                           + [(name, "operator_documentation") for name in _DOCS]):
        selection.add(selection.root / relative, kind, "public_application/" + relative, required=False)
    # Only these two source-code directories; no recursive project walk.
    for relative in ("src/product_radar", "src/product_radar/providers"):
        folder = selection.root / relative
        if folder.is_symlink():
            selection.note(selection.unsafe, "public_application", "SYMLINK_PATH_REFUSED"); continue
        for path in sorted(folder.glob("*.py")):
            selection.add(path, "application_code", "public_application/" + _logical(selection.root, path).as_posix())
    selection.close()
    result = {"schema_version": 2 if multipart else 1,
        "kind": MULTIPART_SELECTION_VERSION if multipart else SELECTION_VERSION,
        "files": sorted(selection.files.values(), key=lambda row: row["relative_path"]),
        "unresolved_required": sorted(selection.unresolved, key=lambda row: (row["origin"], row["reason"], row["relative_path"] or "")),
        "absent_optional": sorted(selection.optional, key=lambda row: (row["origin"], row["reason"], row["relative_path"] or "")),
        "unsafe": sorted(selection.unsafe, key=lambda row: (row["origin"], row["reason"], row["relative_path"] or "")),
        "limitations": ["Canonical records and the financial journal are copied by the caller from these exact supplied checkpoints.",
            "Selected original bytes do not prove independent-drive protection or relocated application recovery.",
            "Credentials, browser profiles, private account-context files and installed dependencies are excluded.",
            "Backup files and backup-control history are excluded; only the public backup settings are selected.",
            "Unresolved, unsafe or changed references prevent a claim of complete essential-file recovery."],
        "selected_bytes": selection.total, "research_state_hash": digest(research_state),
        "financial_state_hash": digest(json_safe(list(financial_state)))}
    if citation_only:
        result["limitations"].append("Public page citations remain in the supplied canonical records; historical webpage bytes are not included.")
    result["selection_fingerprint"] = digest(result)
    return validate_essential_backup_selection(result, root=selection.root)
