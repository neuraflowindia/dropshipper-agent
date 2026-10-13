"""Fixed private portal routes for an explicitly authenticated operator session.

No login, deployment, publication approval or retry occurs here. The owner caller
supplies a short-lived named-user Access application token in memory. Provider
route: https://developers.cloudflare.com/cloudflare-one/tutorials/cli/
Browser sign-in alone is not such a credential. This is an operator CLI session,
not an unattended service-token route. DraftAutomationStorage is a separate,
restricted service-token transport: it cannot mutate the release index or use
owner pages. The Worker decides the verified identity for both routes.

Injected trusted transport: request(method, url, *, headers, body, timeout,
max_response_bytes) -> PortalResponse. It must not log private headers/bodies.
The default transport isolates one stdlib HTTPS operation so the total deadline
covers DNS, connection and response-body stalls. Secrets pass through pipes only.
"""

from dataclasses import dataclass, field
import hashlib
import http.client
import ipaddress
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit

from .models import SafetyError, canonical, decode_json, identifier
from .release_index import _snapshot, _utc

MAX_INDEX = 512 * 1024
MAX_FACTS = 4 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_SPEC = 64 * 1024
HASH = re.compile(r"[a-f0-9]{64}\Z")
TYPES = {"shortlist.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "report-en.pdf": "application/pdf", "report-hi-Latn.pdf": "application/pdf",
         "final_report_data.json": "application/json", "delivery-manifest.json": "application/json"}
SELECTED_HEADERS = {"content-type", "content-length", "content-encoding", "x-content-sha256", "x-neuraflow-draft-check"}
AUTOMATION_PREFIX = "/_automation/drafts"


@dataclass(frozen=True)
class PortalResponse:
    status: int
    headers: dict = field(repr=False)
    body: bytes = field(repr=False)


class PortalOwnerActionRequired(SafetyError):
    """Fixed public error; exact private repair IDs remain separate attributes."""
    def __init__(self, reason, issue_ids, next_available_at=None):
        super().__init__("PORTAL_OWNER_ACTION_REQUIRED")
        self.reason, self.affected_issue_ids = reason, tuple(issue_ids)
        self.next_available_at = next_available_at


def _owner_action(response):
    if response.status != 409 or response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        return
    from .release_index import _utc
    try:
        value = decode_json(response.body)
        if not isinstance(value, dict) or value.get("error") not in {
            "ADMIN_QUEUE_OWNER_ACTION_REQUIRED", "ADMIN_ALIAS_QUEUE_OWNER_ACTION_REQUIRED",
            "ADMIN_LINEAGE_QUEUE_OWNER_ACTION_REQUIRED",
            "ADMIN_RELEASE_AVAILABILITY_CHANGED"}: return
        issues = value.get("affected_issue_ids")
        if (not isinstance(issues, list) or not 1 <= len(issues) <= 2000
                or any(not isinstance(cid, str) for cid in issues) or len(set(issues)) != len(issues)):
            return
        for cid in issues: identifier(cid)
        at = value.get("next_available_at")
        if at is not None: _utc(at)
    except (SafetyError, TypeError, ValueError):
        return
    raise PortalOwnerActionRequired(value["error"], issues, at)


def _origin(value):
    try:
        if not isinstance(value, str) or len(value) > 300 or re.search(r"[\s\\%]", value):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/") or parsed.port not in (None, 443)
                or len(host) > 253 or "." not in host or host.endswith((".localhost", ".local", ".internal"))
                # Numeric final labels can be shorthand/legacy IPv4 spellings
                # such as 127.1 or 0x7f.1, not a public DNS application origin.
                or not re.match(r"[a-z]", host.rsplit(".", 1)[-1])
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split("."))):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
        return "https://" + host
    except (ValueError, TypeError):
        raise SafetyError("PORTAL_ORIGIN_INVALID") from None


def _token(value):
    if not isinstance(value, str) or len(value) > 16384 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value):
        raise SafetyError("PORTAL_ACCESS_TOKEN_REQUIRED")
    return value


def _service_credentials(client_id, client_secret):
    if (not isinstance(client_id, str) or not re.fullmatch(r"[a-fA-F0-9]{32}\.access", client_id)
            or not isinstance(client_secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,512}", client_secret)):
        raise SafetyError("PORTAL_DRAFT_SERVICE_CREDENTIALS_INVALID")
    return client_id, client_secret


def _credentials(headers):
    if "cf-access-token" in headers:
        return (_token(headers["cf-access-token"]),)
    return _service_credentials(headers.get("CF-Access-Client-Id"), headers.get("CF-Access-Client-Secret"))


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 120:
        raise SafetyError("PORTAL_TIMEOUT_INVALID")
    return value


def _route(path):
    if path == "/admin/index":
        return "application/json", MAX_INDEX
    if path.startswith("/admin/index-page?"):
        _page_query(path)
        return "application/json", MAX_INDEX
    if path == "/admin/draft-page" or path.startswith("/admin/draft-page?"):
        _draft_page_query(path)
        return "application/json", MAX_INDEX
    if re.fullmatch(r"/admin/release-pages/(archive|issues|types|aliases|queue|holds|timeline|revisions|lineage)/[a-f0-9]{64}\.json", path):
        return "application/json", 256 * 1024
    if re.fullmatch(r"/admin/alias-records/functional-alias-[a-f0-9]{40}\.json", path):
        return "application/json", MAX_INDEX
    if re.fullmatch(r"/admin/lineage-records/functional-lineage-[a-f0-9]{40}\.json", path):
        return "application/json", MAX_INDEX
    if path == "/admin/drafts":
        return "text/html; charset=utf-8", MAX_INDEX
    download = re.fullmatch(r"/admin/download/([A-Za-z0-9_-]{1,80})/([1-9]\d{0,15})/(shortlist\.xlsx|report-en\.pdf|report-hi-Latn\.pdf)", path)
    if download and int(download[2]) <= 9007199254740991:
        return TYPES[download[3]], MAX_FILE
    match = re.fullmatch(r"/admin/objects/([A-Za-z0-9_-]{1,80})/([1-9]\d{0,15})/([^/]+)", path)
    if not match or match[3] not in TYPES or int(match[2]) > 9007199254740991:
        raise SafetyError("PORTAL_OBJECT_KEY_INVALID")
    name = match[3]
    return TYPES[name], MAX_INDEX if name == "delivery-manifest.json" else MAX_FACTS if name == "final_report_data.json" else MAX_FILE


def _page_query(path):
    """Only the fixed owner's bounded history queries, never an arbitrary URL."""
    from .release_pages import SUPPORTED_KINDS, MAX_INTEGER
    parsed = urlsplit(path)
    try:
        pairs = parse_qsl(parsed.query, strict_parsing=True, keep_blank_values=True)
        values = dict(pairs)
        if (parsed.path != "/admin/index-page" or parsed.fragment or len(values) != len(pairs)
                or values.get("kind") not in SUPPORTED_KINDS or len(path) > 4096):
            raise ValueError
        if values["kind"] == "lineage" and ("endpoint" in values or "direction" in values):
            identifier(values.get("endpoint"))
            if values.get("direction") not in {"0", "1"} or set(values) not in (
                    {"kind", "endpoint", "direction"}, {"kind", "endpoint", "direction", "cursor"}):
                raise ValueError
            if "cursor" in values and not re.fullmatch(r"[A-Za-z0-9_-]{1,3072}", values["cursor"]):
                raise ValueError
        elif "id" in values:
            identifier(values["id"])
            allowed = {"kind", "id"}
            if values["kind"] == "revisions":
                allowed.add("revision")
                if not re.fullmatch(r"[1-9]\d{0,15}", values.get("revision", "")) or int(values["revision"]) > MAX_INTEGER:
                    raise ValueError
            elif values["kind"] not in {"issues", "types", "aliases"}:
                raise ValueError
            if set(values) != allowed: raise ValueError
        elif set(values) not in ({"kind"}, {"kind", "cursor"}):
            raise ValueError
        elif "cursor" in values and not re.fullmatch(r"[A-Za-z0-9_-]{1,3072}", values["cursor"]):
            raise ValueError
        if urlencode(pairs) != parsed.query: raise ValueError
        return values
    except (ValueError, TypeError):
        raise SafetyError("PORTAL_HISTORY_QUERY_INVALID") from None


def _draft_page_query(path):
    parsed = urlsplit(path)
    try:
        pairs = parse_qsl(parsed.query, strict_parsing=True, keep_blank_values=True)
        if (parsed.path != "/admin/draft-page" or parsed.fragment or len(path) > 4096
                or len(pairs) > 1 or pairs and (pairs[0][0] != "cursor"
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,3072}", pairs[0][1]))
                or urlencode(pairs) != parsed.query):
            raise ValueError
        return dict(pairs)
    except (TypeError, ValueError):
        raise SafetyError("PORTAL_PRIVATE_DRAFT_QUERY_INVALID") from None


def _validate_spec(spec, raw):
    if not isinstance(spec, dict) or set(spec) != {"method", "url", "headers", "max_response_bytes"} or not isinstance(raw, bytes):
        raise SafetyError("PORTAL_REQUEST_INVALID")
    try:
        parsed = urlsplit(spec["url"])
        origin = _origin("https://" + parsed.netloc)
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        if spec["url"] != origin + target or parsed.fragment:
            raise ValueError
        machine = parsed.path.startswith(AUTOMATION_PREFIX + "/")
        if machine and parsed.query:
            raise ValueError
        path = "/admin" + parsed.path[len(AUTOMATION_PREFIX):] if machine else target
        if machine and path != "/admin/index" and not path.startswith("/admin/objects/"):
            raise ValueError
        media, limit = _route(path)
        method = spec["method"]
        methods = ("GET",) if machine and path == "/admin/index" else ("GET", "POST") if path == "/admin/index" else ("GET",) if path in {"/admin/drafts", "/admin/draft-page"} or path.startswith(("/admin/download/", "/admin/index-page?", "/admin/draft-page?", "/admin/release-pages/", "/admin/alias-records/", "/admin/lineage-records/")) else ("GET", "PUT")
        if method not in methods:
            raise ValueError
        headers = spec["headers"]
        expected = {"CF-Access-Client-Id", "CF-Access-Client-Secret", "X-NeuraFlow-Release-Action", "Accept-Encoding"} if machine else {"cf-access-token", "X-NeuraFlow-Release-Action", "Accept-Encoding"}
        if method != "GET":
            expected.add("Content-Type")
        if method == "PUT":
            expected.add("X-Content-SHA256")
        if not isinstance(headers, dict) or set(headers) != expected or headers["X-NeuraFlow-Release-Action"] != "v1" or headers["Accept-Encoding"] != "identity":
            raise ValueError
        _credentials(headers)
        if method == "GET" and raw or method != "GET" and (not raw or len(raw) > limit or headers["Content-Type"] != media):
            raise ValueError
        if method == "PUT" and (not isinstance(headers["X-Content-SHA256"], str) or not HASH.fullmatch(headers["X-Content-SHA256"]) or hashlib.sha256(raw).hexdigest() != headers["X-Content-SHA256"]):
            raise ValueError
        response_limit = limit if method == "GET" else MAX_INDEX
        if type(spec["max_response_bytes"]) is not int or not 1 <= spec["max_response_bytes"] <= response_limit:
            raise ValueError
    except (KeyError, ValueError, TypeError, OverflowError):
        raise SafetyError("PORTAL_REQUEST_INVALID") from None


def _checked_response(value, limit, token):
    if (not isinstance(value, PortalResponse) or type(value.status) is not int or not 100 <= value.status <= 599
            or not isinstance(value.body, bytes) or len(value.body) > limit or not isinstance(value.headers, dict)):
        raise SafetyError("PORTAL_RESPONSE_INVALID")
    headers = {}
    for key, content in value.headers.items():
        if not isinstance(key, str) or not isinstance(content, str) or len(content) > 256 or key.lower() not in SELECTED_HEADERS or key.lower() in headers:
            raise SafetyError("PORTAL_RESPONSE_INVALID")
        headers[key.lower()] = content
    if ("x-neuraflow-draft-check" in headers and headers["x-neuraflow-draft-check"] not in
            {"encoding", "header", "payload", "type", "issuer", "audience", "audience_shape",
             "audience_count", "audience_format", "audience_mismatch", "subject", "client", "time"}):
        raise SafetyError("PORTAL_RESPONSE_INVALID")
    secrets = (token,) if isinstance(token, str) else token
    if any(secret.encode() in value.body or any(secret in content for content in headers.values()) for secret in secrets):
        raise SafetyError("PORTAL_CREDENTIAL_REFLECTION_REFUSED")
    if headers.get("content-encoding", "identity") not in ("", "identity"):
        raise SafetyError("PORTAL_RESPONSE_ENCODING_INVALID")
    length = headers.get("content-length")
    if length is not None and (not re.fullmatch(r"[0-9]{1,12}", length) or int(length) != len(value.body)):
        raise SafetyError("PORTAL_RESPONSE_LENGTH_INVALID")
    return PortalResponse(value.status, headers, value.body)


def _perform_raw(spec, raw, *, connection_factory=None):
    """Child only in production; fake connection injection supports offline tests."""
    _validate_spec(spec, raw)
    parsed = urlsplit(spec["url"])
    connection = None
    try:
        factory = connection_factory or http.client.HTTPSConnection
        connection = factory(parsed.hostname, timeout=15)
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection.request(spec["method"], target, body=raw if raw else None, headers=spec["headers"])
        response = connection.getresponse()
        headers = {}
        for key, value in response.getheaders():
            if key.lower() in SELECTED_HEADERS:
                if key.lower() in headers:
                    raise SafetyError("PORTAL_RESPONSE_INVALID")
                headers[key.lower()] = value
        # Never follow redirects, including login redirects. Do not read their
        # potentially private body or forward the credential to another host.
        if 300 <= response.status < 400:
            return PortalResponse(response.status, {}, b"")
        limit = spec["max_response_bytes"]
        length = headers.get("content-length")
        if length is not None and (not re.fullmatch(r"[0-9]{1,12}", length) or int(length) > limit):
            raise SafetyError("PORTAL_RESPONSE_LIMIT")
        result = PortalResponse(response.status, headers, response.read(limit + 1))
        return _checked_response(result, limit, _credentials(spec["headers"]))
    except SafetyError:
        raise
    except Exception:
        raise SafetyError("PORTAL_NETWORK_FAILED") from None
    finally:
        if connection is not None:
            connection.close()


def _worker_main(*, connection_factory=None):
    """Private pipes; an owned worker may supply its trusted connection route.

    The default remains ordinary verified HTTPS. The optional factory changes
    connection setup only, not request/response checks or the parent's deadline.
    """
    try:
        length_raw = sys.stdin.buffer.read(4)
        if len(length_raw) != 4:
            raise ValueError
        length = struct.unpack(">I", length_raw)[0]
        if not 1 <= length <= MAX_SPEC:
            raise ValueError
        spec = decode_json(sys.stdin.buffer.read(length))
        raw = sys.stdin.buffer.read(MAX_FILE + 1)
        response = _perform_raw(spec, raw, connection_factory=connection_factory)
        metadata = canonical({"ok": True, "status": response.status, "headers": response.headers})
        payload = response.body
    except Exception:
        metadata, payload = b'{"ok":false}', b""
    sys.stdout.buffer.write(struct.pack(">I", len(metadata)) + metadata + payload)
    sys.stdout.buffer.flush()


def _worker_request(spec, raw, *, timeout, command=None):
    _validate_spec(spec, raw)
    _timeout(timeout)
    encoded = canonical(spec)
    if len(encoded) > MAX_SPEC:
        raise SafetyError("PORTAL_REQUEST_LIMIT")
    source = str(Path(__file__).resolve().parents[1])
    arguments = command or [sys.executable, "-c", "import runpy,sys;sys.path.insert(0,sys.argv[1]);runpy.run_module('product_radar.portal_storage',run_name='__main__')", source]
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR") if key in os.environ}
    process = None
    try:
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment)
        output, _ = process.communicate(struct.pack(">I", len(encoded)) + encoded + raw, timeout=timeout)
        if process.returncode != 0 or len(output) < 4:
            raise SafetyError("PORTAL_NETWORK_FAILED")
        length = struct.unpack(">I", output[:4])[0]
        if not 1 <= length <= MAX_SPEC or len(output) < 4 + length:
            raise SafetyError("PORTAL_RESPONSE_INVALID")
        metadata = decode_json(output[4:4 + length])
        if not isinstance(metadata, dict) or metadata.get("ok") is not True:
            raise SafetyError("PORTAL_NETWORK_FAILED")
        return _checked_response(PortalResponse(metadata.get("status"), metadata.get("headers"), output[4 + length:]),
                                 spec["max_response_bytes"], _credentials(spec["headers"]))
    except subprocess.TimeoutExpired:
        raise SafetyError("PORTAL_REQUEST_DEADLINE_RECONCILE") from None
    except SafetyError:
        raise
    except Exception:
        raise SafetyError("PORTAL_NETWORK_FAILED") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


class StdlibPortalTransport:
    def request(self, method, url, *, headers, body, timeout, max_response_bytes):
        return _worker_request({"method": method, "url": url, "headers": headers, "max_response_bytes": max_response_bytes}, body or b"", timeout=timeout)


class PortalStorage:
    """The four-method release_index storage interface, no cloud login or retry."""

    __slots__ = ("origin", "client_id", "_access_token", "_transport", "timeout")

    def __init__(self, origin, client_id, access_token, *, transport=None, timeout=30):
        self.origin, self.client_id = _origin(origin), identifier(client_id)
        self._access_token, self.timeout = _token(access_token), _timeout(timeout)
        self._transport = transport if transport is not None else StdlibPortalTransport()
        if not callable(getattr(self._transport, "request", None)):
            raise SafetyError("PORTAL_TRANSPORT_INVALID")

    def __repr__(self):
        return f"PortalStorage(origin={self.origin!r}, client_id={self.client_id!r})"

    def _index_key(self, key):
        if key != f"clients/{self.client_id}/release-index.json":
            raise SafetyError("PORTAL_INDEX_KEY_INVALID")

    def _object(self, key):
        if not isinstance(key, str):
            raise SafetyError("PORTAL_OBJECT_KEY_INVALID")
        match = re.fullmatch(r"clients/" + re.escape(self.client_id) + r"/issues/([A-Za-z0-9_-]{1,80})/r([1-9]\d{0,15})/([^/]+)", key)
        if not match:
            raise SafetyError("PORTAL_OBJECT_KEY_INVALID")
        path = f"/admin/objects/{match[1]}/{match[2]}/{match[3]}"
        media, limit = _route(path)
        return path, media, limit

    def _call(self, method, path, *, raw=b"", media=None, sha256=None, limit=MAX_INDEX, conflict=False):
        path = self._request_path(path)
        headers = {**self._auth_headers(), "X-NeuraFlow-Release-Action": "v1", "Accept-Encoding": "identity"}
        if media:
            headers["Content-Type"] = media
        if sha256:
            headers["X-Content-SHA256"] = sha256
        try:
            response = self._transport.request(method, self.origin + path, headers=headers, body=raw,
                                               timeout=self.timeout, max_response_bytes=limit)
        except Exception:
            # No transport diagnostics or private request may escape. A timed
            # out write may have committed: the caller must read back first.
            raise SafetyError("PORTAL_REQUEST_FAILED_RECONCILE" if method in ("PUT", "POST") else "PORTAL_REQUEST_FAILED") from None
        response = _checked_response(response, limit, _credentials(headers))
        if 300 <= response.status < 400:
            raise SafetyError("PORTAL_REDIRECT_REFUSED")
        if response.status in (401, 403):
            raise SafetyError("PORTAL_OPERATOR_ACCESS_REQUIRED")
        if (response.status == 404 and method == "GET" and "/objects/" in path
                and response.body == b"ADMIN_OBJECT_NOT_FOUND"):
            # Only this authenticated Worker's explicit missing-object result
            # permits reconciliation to attempt the original immutable PUT.
            # A generic host/router 404 does not establish storage absence.
            raise SafetyError("PORTAL_OBJECT_NOT_FOUND")
        if response.status == 409 and conflict and response.body == b"ADMIN_INDEX_CONFLICT":
            return None
        _owner_action(response)
        if response.status != 200:
            raise SafetyError("PORTAL_HTTP_REQUEST_REJECTED")
        return response

    def _request_path(self, path):
        return path

    def _auth_headers(self):
        return {"cf-access-token": self._access_token}

    @staticmethod
    def _json(response):
        if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise SafetyError("PORTAL_JSON_RESPONSE_REQUIRED")
        try:
            return decode_json(response.body)
        except Exception:
            raise SafetyError("PORTAL_JSON_RESPONSE_INVALID") from None

    def read_index(self, key):
        self._index_key(key)
        result = self._json(self._call("GET", "/admin/index"))
        return _snapshot(result, self.client_id)

    def read_private_draft_list(self):
        response = self._call("GET", "/admin/drafts")
        if response.headers.get("content-type") != "text/html; charset=utf-8":
            raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID")
        try:
            return response.body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID") from None

    def read_private_draft_download(self, key, *, sha256, size):
        path, media, _ = self._object(key)
        if path.rsplit("/", 1)[-1] not in ("shortlist.xlsx", "report-en.pdf", "report-hi-Latn.pdf"):
            raise SafetyError("PORTAL_OBJECT_KEY_INVALID")
        if not isinstance(sha256, str) or not HASH.fullmatch(sha256) or type(size) is not int or not 1 <= size <= MAX_FILE:
            raise SafetyError("PORTAL_PRIVATE_DRAFT_DOWNLOAD_INVALID")
        response = self._call("GET", path.replace("/admin/objects/", "/admin/download/", 1), limit=MAX_FILE)
        if (response.headers.get("content-type") != media or response.headers.get("content-length") != str(size)
                or response.headers.get("x-content-sha256") != sha256 or len(response.body) != size
                or hashlib.sha256(response.body).hexdigest() != sha256):
            raise SafetyError("PORTAL_OBJECT_VERIFICATION_FAILED")
        return response.body

    def put_immutable(self, key, raw, *, sha256, media_type):
        path, media, limit = self._object(key)
        if (not isinstance(raw, bytes) or not 1 <= len(raw) <= limit or not isinstance(sha256, str)
                or not HASH.fullmatch(sha256) or hashlib.sha256(raw).hexdigest() != sha256 or media_type != media):
            raise SafetyError("PORTAL_UPLOAD_INVALID")
        result = self._json(self._call("PUT", path, raw=raw, media=media, sha256=sha256))
        if not isinstance(result, dict) or result.get("key") != key or result.get("sha256") != sha256 or type(result.get("size")) is not int or result["size"] != len(raw):
            raise SafetyError("PORTAL_UPLOAD_RECEIPT_INVALID")

    def read_object(self, key):
        page = re.fullmatch(r"clients/" + re.escape(self.client_id) + r"/release-index-v2/pages/(archive|issues|types|aliases|queue|holds|timeline|revisions|lineage)/([a-f0-9]{64})\.json", key) if isinstance(key, str) else None
        alias_original = re.fullmatch(r"clients/" + re.escape(self.client_id) + r"/release-index-v2/alias-records/(functional-alias-[a-f0-9]{40})\.json", key) if isinstance(key, str) else None
        lineage_original = re.fullmatch(r"clients/" + re.escape(self.client_id) + r"/release-index-v2/lineage-records/(functional-lineage-[a-f0-9]{40})\.json", key) if isinstance(key, str) else None
        if page:
            path = f"/admin/release-pages/{page[1]}/{page[2]}.json"
            media, limit = _route(path)
        elif alias_original:
            path = f"/admin/alias-records/{alias_original[1]}.json"
            media, limit = _route(path)
        elif lineage_original:
            path = f"/admin/lineage-records/{lineage_original[1]}.json"
            media, limit = _route(path)
        else:
            path, media, limit = self._object(key)
        response = self._call("GET", path, limit=limit)
        sha = response.headers.get("x-content-sha256")
        if (response.headers.get("content-type") != media or "content-length" not in response.headers or not response.body or not isinstance(sha, str)
                or not HASH.fullmatch(sha) or hashlib.sha256(response.body).hexdigest() != sha
                or page and sha != page[2]):
            raise SafetyError("PORTAL_OBJECT_VERIFICATION_FAILED")
        return response.body

    def compare_and_swap_index(self, key, expected_etag, mutation):
        return self._compare_and_swap_index(key, expected_etag, mutation)

    def compare_and_swap_index_v2(self, key, expected_etag, mutation):
        return self._compare_and_swap_index(key, expected_etag, mutation, contract="release-index-v2")

    def _compare_and_swap_index(self, key, expected_etag, mutation, *, contract=None):
        self._index_key(key)
        if expected_etag is not None and (not isinstance(expected_etag, str) or not expected_etag or len(expected_etag) > 256 or re.search(r"[\x00-\x1f\x7f]", expected_etag)):
            raise SafetyError("PORTAL_ETAG_INVALID")
        kinds = ("activate", "withdraw") if contract is None else ("activate", "withdraw", "migrate", "reconcile", "aliases", "lineage")
        if not isinstance(mutation, dict) or mutation.get("client_id") != self.client_id or mutation.get("kind") not in kinds:
            raise SafetyError("PORTAL_MUTATION_INVALID")
        try:
            request = {"expected_etag": expected_etag, "mutation": mutation}
            if contract is not None: request["contract"] = contract
            raw = canonical(request)
        except Exception:
            raise SafetyError("PORTAL_MUTATION_INVALID") from None
        if len(raw) > MAX_INDEX:
            raise SafetyError("PORTAL_MUTATION_LIMIT")
        response = self._call("POST", "/admin/index", raw=raw, media="application/json", conflict=True)
        return None if response is None else _snapshot(self._json(response), self.client_id)

    def read_private_draft_page(self, *, cursor=None):
        """One bounded owner-only page; a continuation is not the full inventory."""
        path = "/admin/draft-page" + ("?" + urlencode({"cursor": cursor}) if cursor is not None else "")
        _draft_page_query(path)
        result = self._json(self._call("GET", path))
        if (not isinstance(result, dict) or result.get("schema_version") != 2
                or result.get("contract") != "private-draft-list-v2" or result.get("client_id") != self.client_id
                or result.get("source_prefix") != f"clients/{self.client_id}/issues/"
                or not isinstance(result.get("index_hash"), str) or not HASH.fullmatch(result["index_hash"])
                or result.get("index_etag") is not None and (not isinstance(result["index_etag"], str)
                    or not 1 <= len(result["index_etag"]) <= 256)
                or result.get("index_write_attempted") is not False or result.get("publication_attempted") is not False
                or not isinstance(result.get("drafts"), list) or not isinstance(result.get("skipped"), list)):
            raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID")
        for name, maximum in (("checked_versions", 8), ("released_versions", 8), ("scanned_versions", 8),
                              ("scanned_objects", 300), ("source_pages", 3)):
            if type(result.get(name)) is not int or not 0 <= result[name] <= maximum:
                raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID")
        token = result.get("next_cursor")
        if (result["checked_versions"] != len(result["drafts"]) + len(result["skipped"])
                or result["scanned_versions"] != result["checked_versions"] + result["released_versions"]
                or type(result.get("scan_limited")) is not bool or result["scan_limited"] != (token is not None)
                or token is not None and (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,3072}", token))):
            raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID")
        _utc(result.get("server_observed_at"))
        seen = set()
        for row in result["drafts"] + result["skipped"]:
            if not isinstance(row, dict): raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID")
            identifier(row.get("issue_id"))
            revision = row.get("revision")
            if type(revision) is not int or not 1 <= revision <= 9007199254740991 or (row["issue_id"], revision) in seen:
                raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID")
            seen.add((row["issue_id"], revision))
        for row in result["drafts"]:
            if (row.get("state") != "stored_for_owner_review" or row.get("approved") is not False
                    or row.get("client_available") is not False or type(row.get("product_count")) is not int
                    or not 10 <= row["product_count"] <= 20 or not isinstance(row.get("package_hash"), str)
                    or not HASH.fullmatch(row["package_hash"]) or not isinstance(row.get("files"), list)
                    or not 2 <= len(row["files"]) <= 3):
                raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID")
            names = set()
            for file in row["files"]:
                if (not isinstance(file, dict) or file.get("name") not in {"shortlist.xlsx", "report-en.pdf", "report-hi-Latn.pdf"}
                        or file["name"] in names or type(file.get("size")) is not int or not 1 <= file["size"] <= MAX_FILE
                        or not isinstance(file.get("sha256"), str) or not HASH.fullmatch(file["sha256"])
                        or file.get("url") != f'/admin/download/{row["issue_id"]}/{row["revision"]}/{file["name"]}'):
                    raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID")
                names.add(file["name"])
            if "shortlist.xlsx" not in names:
                raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID")
        for row in result["skipped"]:
            if not isinstance(row.get("reason"), str) or not re.fullmatch(r"[A-Z0-9_]{1,160}", row["reason"]):
                raise SafetyError("PORTAL_PRIVATE_DRAFT_LIST_INVALID")
        return result

    def read_index_page(self, kind, *, cursor=None, item_id=None, revision=None, endpoint=None, direction=None):
        values = [("kind", kind)]
        if cursor is not None: values.append(("cursor", cursor))
        if item_id is not None: values.append(("id", item_id))
        if revision is not None: values.append(("revision", str(revision)))
        if endpoint is not None: values.append(("endpoint", endpoint))
        if direction is not None: values.append(("direction", str(direction)))
        path = "/admin/index-page?" + urlencode(values)
        _page_query(path)
        result = self._json(self._call("GET", path))
        _snapshot(result, self.client_id)
        if result["index"]["schema_version"] != 2 or result.get("kind") != kind:
            raise SafetyError("PORTAL_HISTORY_RESPONSE_INVALID")
        # Derive the answer from hash-checked immutable pages, not a supplied
        # count, lookup payload or a cursor treated as authority.
        from .release_pages import ReleasePages, read_release_cursor, release_cursor, sequence_key
        from .models import digest
        tree = ReleasePages(result["index"], self.read_object)
        if item_id is not None:
            key = digest(item_id) + (sequence_key(revision) if kind == "revisions" else "")
            value = tree.get(kind, key)
            if kind == "revisions" and value is not None:
                value = tree.get("archive", sequence_key(value.get("sequence")))
                if value is None or value.get("issue_id") != item_id or value.get("revision") != revision:
                    raise SafetyError("PORTAL_HISTORY_RESPONSE_INVALID")
            if canonical(result.get("value")) != canonical(value): raise SafetyError("PORTAL_HISTORY_RESPONSE_INVALID")
        else:
            parsed = read_release_cursor(cursor, tree.root, kind) if cursor is not None else None
            options = {"after": parsed["last_key"], "reverse": parsed["reverse"], "prefix": parsed["prefix"]} if parsed else {"reverse": kind in {"archive", "timeline"}}
            if endpoint is not None:
                prefix = digest(endpoint) + str(direction)
                if parsed and options["prefix"] != prefix:
                    raise SafetyError("PORTAL_HISTORY_RESPONSE_INVALID")
                options["prefix"] = prefix
            page = tree.scan(kind, limit=20, **options)
            next_cursor = release_cursor(tree.root, kind, page["last_key"], reverse=options.get("reverse", False),
                prefix=options.get("prefix", "")) if page["more"] else None
            if canonical(result.get("rows")) != canonical(page["rows"]) or result.get("next_cursor") != next_cursor:
                raise SafetyError("PORTAL_HISTORY_RESPONSE_INVALID")
        return result


class DraftAutomationStorage(PortalStorage):
    """Only immutable private-draft files and release-index reads, never release."""

    __slots__ = ("_service_id", "_service_secret")

    def __init__(self, origin, client_id, service_id, service_secret, *, transport=None, timeout=30):
        self.origin, self.client_id = _origin(origin), identifier(client_id)
        self._service_id, self._service_secret = _service_credentials(service_id, service_secret)
        self._access_token, self.timeout = None, _timeout(timeout)
        self._transport = transport if transport is not None else StdlibPortalTransport()
        if not callable(getattr(self._transport, "request", None)):
            raise SafetyError("PORTAL_TRANSPORT_INVALID")

    def __repr__(self):
        return f"DraftAutomationStorage(origin={self.origin!r}, client_id={self.client_id!r})"

    def _auth_headers(self):
        return {"CF-Access-Client-Id": self._service_id, "CF-Access-Client-Secret": self._service_secret}

    def _request_path(self, path):
        if path != "/admin/index" and not path.startswith("/admin/objects/"):
            raise SafetyError("PORTAL_DRAFT_ONLY_ROUTE_REQUIRED")
        return AUTOMATION_PREFIX + path[len("/admin"):]

    def _call(self, method, path, **kwargs):
        if method not in ("GET", "PUT") or path == "/admin/index" and method != "GET":
            raise SafetyError("PORTAL_DRAFT_ONLY_ROUTE_REQUIRED")
        return super()._call(method, path, **kwargs)

    def compare_and_swap_index(self, key, expected_etag, mutation):
        raise SafetyError("PORTAL_DRAFT_ONLY_ROUTE_REQUIRED")


if __name__ == "__main__":
    _worker_main()
