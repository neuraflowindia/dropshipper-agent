"""Two fixed illustrative files: private staging/readback, never publication.

Default work is local only. Explicit live work uses an existing named-owner
Access session; it never starts login or accepts a token argument/file. The
provider's `access token` command reads its existing application token, unlike
`access login` or `access curl`, which can initiate authentication:
https://github.com/cloudflare/cloudflared/blob/master/cmd/cloudflared/access/cmd.go
"""

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from .models import SafetyError
from .portal_storage import MAX_FILE, PortalStorage, _token

ORIGIN = "https://dropshipper.neuraflow.co.in"
CLIENT = "default"
INDEX_KEY = f"clients/{CLIENT}/release-index.json"
PREFIX = f"clients/{CLIENT}/issues/internal-storage-proof-20261006/r1/"
AUTH_OUTPUT_LIMIT = 16386
ARTIFACTS = (
    ("outputs/client-preview/NeuraFlow_Product_Opportunity_Brief_Illustrative_2026-08-15.pdf",
     "report-en.pdf", "application/pdf", 2044549,
     "4cdd5081b53007a6f5e2ec4ebbd470046e93ee0e2d743d1f332ad328a601a66b"),
    ("outputs/client-preview/NeuraFlow_Product_Opportunity_Evidence_Illustrative_2026-08-15.xlsx",
     "shortlist.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 110425,
     "a0cea05454565986db3b6e8421c29c66dbcc76455e57d6f8bfc9dc136a609066"),
)


@dataclass(frozen=True)
class PreparedArtifact:
    source: str
    name: str
    media_type: str
    sha256: str
    raw: bytes = field(repr=False)

    def metadata(self):
        return {"source": self.source, "key": PREFIX + self.name,
                "size": len(self.raw), "sha256": self.sha256,
                "media_type": self.media_type, "purpose": "illustrative_internal_storage_check"}


def prepare_artifacts(project_root):
    """Read only fixed, regular, bounded, hash-pinned project artifacts."""
    try:
        root = Path(project_root).resolve(strict=True)
        prepared = []
        for relative, name, media, expected_size, expected_hash in ARTIFACTS:
            path = root / relative
            current = root
            for part in Path(relative).parts:
                if part in (".", ".."):
                    raise ValueError
                current = current / part
                if current.is_symlink():
                    raise ValueError
            if not path.resolve(strict=True).is_relative_to(root):
                raise ValueError
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size or not 1 <= before.st_size <= MAX_FILE:
                    raise ValueError
                raw = stream.read(MAX_FILE + 1)
                after = os.fstat(stream.fileno())
            if (before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
                    or len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_hash
                    or not raw.startswith(b"%PDF-" if name.endswith(".pdf") else b"PK")):
                raise ValueError
            prepared.append(PreparedArtifact(relative, name, media, expected_hash, raw))
        return tuple(prepared)
    except Exception:
        raise SafetyError("PRIVATE_DELIVERY_ARTIFACT_INVALID") from None


def cached_owner_token(*, executable_path=None, find_executable=None, run=None):
    """Capture an existing CLI session privately; no interactive/login fallback."""
    if executable_path is not None:
        try:
            selected = Path(executable_path)
            if not selected.is_absolute() or not selected.is_file() or not os.access(selected, os.X_OK):
                raise ValueError
            executable = str(selected)
        except Exception:
            raise SafetyError("PRIVATE_DELIVERY_CLOUDFLARED_REQUIRED") from None
    else:
        executable = (find_executable or shutil.which)("cloudflared")
    if not executable:
        raise SafetyError("PRIVATE_DELIVERY_CLOUDFLARED_REQUIRED")
    try:
        result = (run or subprocess.run)(
            [executable, "access", "token", "--app", ORIGIN],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
    except Exception:
        raise SafetyError("PRIVATE_DELIVERY_AUTH_CHECK_UNAVAILABLE") from None
    stdout, stderr = getattr(result, "stdout", None), getattr(result, "stderr", None)
    if (not isinstance(stdout, bytes) or not isinstance(stderr, bytes)
            or len(stdout) > AUTH_OUTPUT_LIMIT or len(stderr) > AUTH_OUTPUT_LIMIT):
        raise SafetyError("PRIVATE_DELIVERY_AUTH_CHECK_UNAVAILABLE")
    # GetAppInfo runs before the cached-token lookup. A route/lookup failure
    # does not establish that a token is missing. Never print provider stderr.
    diagnostic = stderr.decode("utf-8", errors="replace")
    if re.search(r"failed to find Access application at " + re.escape(ORIGIN) + r"(?=$|\s|[\"'])", diagnostic):
        raise SafetyError("PRIVATE_DELIVERY_ACCESS_ROUTE_UNAVAILABLE")
    if "Unable to find token for provided application. Please run login command to generate token." in diagnostic:
        raise SafetyError("PRIVATE_DELIVERY_OWNER_LOGIN_REQUIRED")
    if result.returncode != 0 or not stdout:
        raise SafetyError("PRIVATE_DELIVERY_AUTH_CHECK_UNAVAILABLE")
    try:
        return _token(stdout.decode("ascii").strip())
    except Exception:
        raise SafetyError("PRIVATE_DELIVERY_AUTH_CHECK_UNAVAILABLE") from None


def check_private_delivery(project_root, *, live=False, token_loader=None, storage_factory=None):
    """Return public metadata; never alter an index, approve or activate a report."""
    if type(live) is not bool:
        raise SafetyError("PRIVATE_DELIVERY_MODE_INVALID")
    artifacts = prepare_artifacts(project_root)
    receipt = {"schema_version": 1, "state": "prepared", "live": live,
               "purpose": "illustrative_internal_storage_check", "client_report": False,
               "origin": ORIGIN, "artifact_count": len(artifacts),
               "artifacts": [item.metadata() for item in artifacts],
               "index_write_attempted": False, "publication_attempted": False}
    if not live:
        return receipt
    token = (token_loader or cached_owner_token)()
    store = (storage_factory or PortalStorage)(ORIGIN, CLIENT, token)
    before = store.read_index(INDEX_KEY)
    recovered = 0
    failure = None
    try:
        for item in artifacts:
            key = PREFIX + item.name
            try:
                store.put_immutable(key, item.raw, sha256=item.sha256, media_type=item.media_type)
            except SafetyError as error:
                if str(error) != "PORTAL_REQUEST_FAILED_RECONCILE":
                    raise
                # A write may have committed. Read its exact bytes before any
                # further action; no blind retry and no overwrite is attempted.
                try:
                    raw = store.read_object(key)
                except Exception:
                    raise SafetyError("PRIVATE_DELIVERY_WRITE_UNCONFIRMED") from None
                if raw != item.raw:
                    raise SafetyError("PRIVATE_DELIVERY_WRITE_UNCONFIRMED")
                recovered += 1
            else:
                raw = store.read_object(key)
            if raw != item.raw or hashlib.sha256(raw).hexdigest() != item.sha256:
                raise SafetyError("PRIVATE_DELIVERY_READBACK_MISMATCH")
    except Exception as error:
        failure = error
    after = store.read_index(INDEX_KEY)
    if after["index"] != before["index"] or after["etag"] != before["etag"]:
        raise SafetyError("PRIVATE_DELIVERY_INDEX_CHANGED")
    if failure is not None:
        if isinstance(failure, SafetyError):
            raise failure
        raise SafetyError("PRIVATE_DELIVERY_CHECK_FAILED") from None
    receipt.update(state="private_bytes_verified", index_unchanged=True,
                   verified_artifact_count=len(artifacts), recovered_write_count=recovered)
    return receipt


PUBLIC_ERRORS = {
    "PRIVATE_DELIVERY_ARTIFACT_INVALID", "PRIVATE_DELIVERY_MODE_INVALID",
    "PRIVATE_DELIVERY_CLOUDFLARED_REQUIRED", "PRIVATE_DELIVERY_OWNER_LOGIN_REQUIRED",
    "PRIVATE_DELIVERY_ACCESS_ROUTE_UNAVAILABLE", "PRIVATE_DELIVERY_AUTH_CHECK_UNAVAILABLE",
    "PRIVATE_DELIVERY_WRITE_UNCONFIRMED", "PRIVATE_DELIVERY_READBACK_MISMATCH",
    "PRIVATE_DELIVERY_INDEX_CHANGED", "PRIVATE_DELIVERY_CHECK_FAILED",
    "PORTAL_OPERATOR_ACCESS_REQUIRED", "PORTAL_REDIRECT_REFUSED",
    "PORTAL_REQUEST_FAILED", "PORTAL_REQUEST_FAILED_RECONCILE",
    "PORTAL_HTTP_REQUEST_REJECTED", "PORTAL_OBJECT_VERIFICATION_FAILED",
}


def public_failure(error):
    code = str(error) if isinstance(error, SafetyError) else ""
    return {"schema_version": 1, "state": "not_verified", "client_report": False,
            "purpose": "illustrative_internal_storage_check",
            "reason": code if code in PUBLIC_ERRORS else "PRIVATE_DELIVERY_CHECK_FAILED",
            "publication_attempted": False, "index_write_attempted": False}
