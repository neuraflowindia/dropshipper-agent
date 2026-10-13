"""Offline only: no Cloudflare account, network request or publication."""

from copy import deepcopy
import base64
import hashlib
import io
from pathlib import Path
import struct
import subprocess
import sys
import time
import tempfile
import traceback
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, canonical
from product_radar.portal_storage import (PortalStorage, PortalResponse, MAX_INDEX, MAX_FACTS, MAX_FILE,
    _perform_raw, _worker_request, StdlibPortalTransport, _checked_response)
from product_radar.release_index import release_receipt

TOKEN = "manufacturedheader.manufacturedpayload.manufacturedsignature"
ORIGIN = "https://reports.example.com"
CLIENT = "client-one"
INDEX = f"clients/{CLIENT}/release-index.json"
KEY = f"clients/{CLIENT}/issues/issue-one/r1/report-en.pdf"
AT = "2026-10-12T10:00:00.000Z"


def response(value, status=200, headers=None):
    raw = value if isinstance(value, bytes) else canonical(value)
    return PortalResponse(status, headers or {"content-type": "application/json", "content-length": str(len(raw))}, raw)


def snapshot():
    return {"index": {"schema_version": 1, "client_id": CLIENT, "issues": []}, "etag": None,
            "server_observed_at": AT, "verified": True, "authority": "release_index"}


class FakeTransport:
    def __init__(self, values=()):
        self.values, self.calls = list(values), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class PortalStorageTests(unittest.TestCase):
    def test_private_draft_diagnostic_is_a_fixed_label_only(self):
        raw = b"ADMIN_DRAFT_AUTOMATION_ASSERTION_INVALID"
        result = _checked_response(response(raw, status=403,
            headers={"content-type": "text/plain", "X-NeuraFlow-Draft-Check": "audience_shape"}), MAX_INDEX, TOKEN)
        self.assertEqual(result.headers["x-neuraflow-draft-check"], "audience_shape")
        for value in ("private-claim", TOKEN, "audience_shape\nprivate"):
            self.assert_code("PORTAL_RESPONSE_INVALID", lambda: _checked_response(response(raw, status=403,
                headers={"x-neuraflow-draft-check": value}), MAX_INDEX, TOKEN))

    def storage(self, values=()):
        wire = FakeTransport(values)
        return PortalStorage(ORIGIN, CLIENT, TOKEN, transport=wire), wire

    def assert_code(self, code, function):
        with self.assertRaisesRegex(SafetyError, "^" + code + "$"):
            function()

    def test_explicit_public_https_origin_and_named_user_token_shape(self):
        bad = ["http://reports.example.com", "https://localhost", "https://127.0.0.1", "https://[::1]",
               "https://127.1", "https://0x7f.1", "https://127.0.0.01", "https://0177.1", "https://2130706433",
               "https://one.local", "https://one.internal", "https://me:secret@reports.example.com",
               ORIGIN + "/admin", ORIGIN + "?secret=x", ORIGIN + "#x", ORIGIN + ":8443",
               "https://evil.example\\@reports.example.com", "https://%65vil.example", " https://reports.example.com"]
        for value in bad:
            with self.subTest(value=value):
                self.assert_code("PORTAL_ORIGIN_INVALID", lambda: PortalStorage(value, CLIENT, TOKEN))
        for token in [None, "", "browser-signed-in", "one.two.three\r\nX-Header:secret", {"service_token": TOKEN}]:
            self.assert_code("PORTAL_ACCESS_TOKEN_REQUIRED", lambda: PortalStorage(ORIGIN, CLIENT, token))
        self.assertEqual(PortalStorage("https://REPORTS.example.com:443/", CLIENT, TOKEN).origin, ORIGIN)

    def test_time_limit_and_trusted_transport_are_explicit(self):
        for value in [0, -1, 121, True, float("inf"), "30"]:
            self.assert_code("PORTAL_TIMEOUT_INVALID", lambda: PortalStorage(ORIGIN, CLIENT, TOKEN, timeout=value))
        self.assert_code("PORTAL_TRANSPORT_INVALID", lambda: PortalStorage(ORIGIN, CLIENT, TOKEN, transport=object()))

    def test_private_keys_are_fixed_to_one_client_and_five_names(self):
        store, transport = self.storage()
        for key in ["index", "clients/other/release-index.json", INDEX + "?url=https://evil.example"]:
            self.assert_code("PORTAL_INDEX_KEY_INVALID", lambda: store.read_index(key))
        for key in [KEY.replace(CLIENT, "other"), KEY + "?url=x", KEY.replace("report-en.pdf", "secret.txt"),
                    KEY.replace("issue-one", ".."), KEY.replace("r1", "r01"), KEY.replace("r1", "r" + "1" * 5000),
                    "https://evil.example/report-en.pdf", None]:
            self.assert_code("PORTAL_OBJECT_KEY_INVALID", lambda: store.read_object(key))
        self.assertEqual(transport.calls, [])

    def test_verified_server_snapshot_keeps_actual_etag_and_time(self):
        expected = snapshot(); expected["etag"] = "actual-opaque-provider-etag"
        store, transport = self.storage([response(expected)])
        self.assertEqual(store.read_index(INDEX), expected)
        method, url, request = transport.calls[0]
        self.assertEqual((method, url), ("GET", ORIGIN + "/admin/index"))
        self.assertEqual(request["headers"], {"cf-access-token": TOKEN, "X-NeuraFlow-Release-Action": "v1", "Accept-Encoding": "identity"})
        self.assertEqual(request["body"], b"")
        self.assertEqual(request["max_response_bytes"], MAX_INDEX)
        self.assertNotIn(TOKEN, repr(store))

    def test_snapshot_cannot_invent_client_authority_or_server_time(self):
        changes = [lambda v: v["index"].update(client_id="other"), lambda v: v.update(verified=False),
                   lambda v: v.update(authority="laptop"), lambda v: v.pop("server_observed_at"),
                   lambda v: v.update(server_observed_at="2026-10-12T10:00:00"), lambda v: v.update(etag=4)]
        for edit in changes:
            changed = snapshot(); edit(changed)
            store, _ = self.storage([response(changed)])
            with self.assertRaises(SafetyError):
                store.read_index(INDEX)

    def test_upload_verifies_hash_bytes_type_and_exact_acknowledgement(self):
        raw = b"%PDF-1.7\nManufactured fixture"; sha = hashlib.sha256(raw).hexdigest()
        store, wire = self.storage([response({"key": KEY, "sha256": sha, "size": len(raw)})])
        self.assertIsNone(store.put_immutable(KEY, raw, sha256=sha, media_type="application/pdf"))
        method, url, request = wire.calls[0]
        self.assertEqual((method, url), ("PUT", ORIGIN + "/admin/objects/issue-one/1/report-en.pdf"))
        self.assertEqual(request["body"], raw)
        self.assertEqual(request["headers"]["X-Content-SHA256"], sha)
        for bad_raw, bad_sha, bad_type in [(b"", sha, "application/pdf"), (raw, "f" * 64, "application/pdf"),
                                            (raw, sha, "text/plain"), (bytearray(raw), sha, "application/pdf")]:
            store, wire = self.storage()
            self.assert_code("PORTAL_UPLOAD_INVALID", lambda: store.put_immutable(KEY, bad_raw, sha256=bad_sha, media_type=bad_type))
            self.assertEqual(wire.calls, [])
        for acknowledgement in [{"key": KEY.replace(CLIENT, "other"), "sha256": sha, "size": len(raw)},
                                {"key": KEY, "sha256": sha, "size": len(raw) + 1}, {"key": KEY, "sha256": "f" * 64, "size": len(raw)}]:
            store, _ = self.storage([response(acknowledgement)])
            self.assert_code("PORTAL_UPLOAD_RECEIPT_INVALID", lambda: store.put_immutable(KEY, raw, sha256=sha, media_type="application/pdf"))

    def test_object_reads_validate_type_length_and_actual_hash(self):
        raw = b"%PDF-1.7\nManufactured fixture"
        headers = {"content-type": "application/pdf", "content-length": str(len(raw)), "x-content-sha256": hashlib.sha256(raw).hexdigest()}
        store, _ = self.storage([response(raw, headers=headers)])
        self.assertEqual(store.read_object(KEY), raw)
        for change in [lambda v: v.update({"content-type": "text/html"}), lambda v: v.pop("content-length"),
                       lambda v: v.update({"x-content-sha256": "a" * 64}), lambda v: v.update({"content-length": "1"})]:
            altered = deepcopy(headers); change(altered)
            store, _ = self.storage([response(raw, headers=altered)])
            with self.assertRaises(SafetyError):
                store.read_object(KEY)

    def test_facts_and_manifest_have_separate_response_limits(self):
        for name, limit in [("final_report_data.json", MAX_FACTS), ("delivery-manifest.json", MAX_INDEX), ("shortlist.xlsx", MAX_FILE)]:
            media = "application/json" if name.endswith(".json") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            raw = b"{}"; sha = hashlib.sha256(raw).hexdigest()
            store, wire = self.storage([response(raw, headers={"content-type": media, "content-length": "2", "x-content-sha256": sha})])
            self.assertEqual(store.read_object(KEY.rsplit("/", 1)[0] + "/" + name), raw)
            self.assertEqual(wire.calls[0][2]["max_response_bytes"], limit)

    def test_owner_draft_page_is_read_only_and_has_checked_html(self):
        raw = b'<html><a href="/admin/download/issue-one/1/report-en.pdf">English</a></html>'
        store, wire = self.storage([response(raw, headers={"content-type": "text/html; charset=utf-8", "content-length": str(len(raw))})])
        self.assertEqual(store.read_private_draft_list(), raw.decode())
        self.assertEqual(wire.calls[0][:2], ("GET", ORIGIN + "/admin/drafts"))
        for body, media in [(raw, "application/json"), (b"\xff", "text/html; charset=utf-8")]:
            store, _ = self.storage([response(body, headers={"content-type": media, "content-length": str(len(body))})])
            self.assert_code("PORTAL_PRIVATE_DRAFT_LIST_INVALID", store.read_private_draft_list)

    def test_owner_download_uses_exact_read_only_link_and_expected_bytes(self):
        raw = b"%PDF-1.7\nManufactured fixture"; sha = hashlib.sha256(raw).hexdigest()
        headers = {"content-type": "application/pdf", "content-length": str(len(raw)), "x-content-sha256": sha}
        store, wire = self.storage([response(raw, headers=headers)])
        self.assertEqual(store.read_private_draft_download(KEY, sha256=sha, size=len(raw)), raw)
        self.assertEqual(wire.calls[0][:2], ("GET", ORIGIN + "/admin/download/issue-one/1/report-en.pdf"))
        for edit in [lambda v: v.update({"content-type": "text/html"}), lambda v: v.update({"content-length": "1"}),
                     lambda v: v.update({"x-content-sha256": "f" * 64})]:
            changed = dict(headers); edit(changed)
            store, _ = self.storage([response(raw, headers=changed)])
            with self.assertRaises(SafetyError):
                store.read_private_draft_download(KEY, sha256=sha, size=len(raw))

    def test_owner_download_rejects_other_client_facts_and_invalid_expectations_before_request(self):
        store, wire = self.storage()
        for key in [KEY.replace(CLIENT, "other"), KEY.replace("report-en.pdf", "final_report_data.json"),
                    KEY.replace("report-en.pdf", "delivery-manifest.json"), KEY + "?url=x"]:
            self.assert_code("PORTAL_OBJECT_KEY_INVALID", lambda: store.read_private_draft_download(key, sha256="f" * 64, size=12))
        for sha, size in [("bad", 12), ("f" * 64, 0), ("f" * 64, True), ("f" * 64, MAX_FILE + 1)]:
            self.assert_code("PORTAL_PRIVATE_DRAFT_DOWNLOAD_INVALID", lambda: store.read_private_draft_download(KEY, sha256=sha, size=size))
        self.assertEqual(wire.calls, [])

    def test_cas_serializes_only_mutation_with_real_expected_etag_and_no_retry(self):
        changed = snapshot(); changed["etag"] = "actual-after"
        mutation = {"kind": "withdraw", "client_id": CLIENT, "issue_id": "issue-one", "revision": 1}
        store, wire = self.storage([response(changed)])
        self.assertEqual(store.compare_and_swap_index(INDEX, "actual-before", mutation), changed)
        self.assertEqual(len(wire.calls), 1)
        self.assertEqual(wire.calls[0][:2], ("POST", ORIGIN + "/admin/index"))
        self.assertEqual(wire.calls[0][2]["body"], canonical({"expected_etag": "actual-before", "mutation": mutation}))
        self.assertNotIn("server_activated_at", wire.calls[0][2]["body"].decode())
        store, wire = self.storage([response(b"ADMIN_INDEX_CONFLICT", 409, {"content-type": "text/plain"})])
        self.assertIsNone(store.compare_and_swap_index(INDEX, None, mutation)); self.assertEqual(len(wire.calls), 1)
        store, _ = self.storage([response(b"ADMIN_PREVIOUS_REVISION_MISMATCH", 409, {"content-type": "text/plain"})])
        self.assert_code("PORTAL_HTTP_REQUEST_REJECTED", lambda: store.compare_and_swap_index(INDEX, None, mutation))

    def test_invalid_mutation_etag_and_client_never_send(self):
        store, wire = self.storage()
        for etag in ["", 3, "\nprivate", "x" * 257]:
            self.assert_code("PORTAL_ETAG_INVALID", lambda: store.compare_and_swap_index(INDEX, etag, {"kind": "withdraw", "client_id": CLIENT}))
        for mutation in [{"kind": "replace-index", "client_id": CLIENT}, {"kind": "withdraw", "client_id": "other"}, None]:
            self.assert_code("PORTAL_MUTATION_INVALID", lambda: store.compare_and_swap_index(INDEX, None, mutation))
        self.assertEqual(wire.calls, [])

    def test_errors_redirects_auth_and_reflections_do_not_reveal_token_or_retry(self):
        for value, expected in [(response(b"login", 302, {"content-type": "text/html"}), "PORTAL_REDIRECT_REFUSED"),
                                (response(b"denied", 403, {"content-type": "text/plain"}), "PORTAL_OPERATOR_ACCESS_REQUIRED"),
                                (response(TOKEN.encode()), "PORTAL_CREDENTIAL_REFLECTION_REFUSED"),
                                (RuntimeError("private error " + TOKEN), "PORTAL_REQUEST_FAILED")]:
            store, wire = self.storage([value])
            try:
                store.read_index(INDEX)
            except SafetyError as error:
                self.assertEqual(str(error), expected)
                self.assertNotIn(TOKEN, "".join(traceback.format_exception(error)))
            else:
                self.fail("failure expected")
            self.assertEqual(len(wire.calls), 1)
        store, wire = self.storage([TimeoutError(TOKEN)])
        self.assert_code("PORTAL_REQUEST_FAILED_RECONCILE", lambda: store.compare_and_swap_index(INDEX, None, {"client_id": CLIENT, "kind": "withdraw"}))
        self.assertEqual(len(wire.calls), 1)

    def test_bounded_response_and_ambiguous_json_refused(self):
        for value in [response(b"x" * (MAX_INDEX + 1)), response(b'{"verified":true,"verified":false}'),
                      PortalResponse(200, {"content-type": "application/json", "Content-Type": "application/json"}, b"{}"),
                      response(b"{}", headers={"content-type": "application/json", "content-encoding": "gzip"}),
                      response(b"private login HTML", headers={"content-type": "text/html"})]:
            store, _ = self.storage([value])
            with self.assertRaises(SafetyError):
                store.read_index(INDEX)

    def test_read_snapshot_cannot_create_publication_receipt_for_empty_index(self):
        store, _ = self.storage([response(snapshot())])
        self.assert_code("RELEASE_REVISION_MISSING", lambda: release_receipt(store.read_index(INDEX), "issue-one", 1))


class IsolatedTransportTests(unittest.TestCase):
    def spec(self, method="GET"):
        return {"method": method, "url": ORIGIN + "/admin/index", "max_response_bytes": MAX_INDEX,
                "headers": {"cf-access-token": TOKEN, "X-NeuraFlow-Release-Action": "v1", "Accept-Encoding": "identity",
                            **({"Content-Type": "application/json"} if method == "POST" else {})}}

    def test_stdlib_never_follows_redirect_or_reads_redirect_body(self):
        class FakeConnection:
            instance = None
            def __init__(self, hostname, timeout):
                FakeConnection.instance = self; self.calls = []; self.closed = False
                self.status = 302
            def request(self, method, path, **kwargs): self.calls.append((method, path, kwargs))
            def getresponse(self): return self
            def getheaders(self): return [("Location", "https://evil.example"), ("Content-Length", "9999999999")]
            def read(self, limit): raise AssertionError("redirect body must not be read")
            def close(self): self.closed = True
        result = _perform_raw(self.spec(), b"", connection_factory=FakeConnection)
        self.assertEqual(result.status, 302); self.assertEqual(result.body, b"")
        self.assertEqual(len(FakeConnection.instance.calls), 1); self.assertTrue(FakeConnection.instance.closed)

    def test_owner_view_routes_cannot_be_used_for_mutating_requests(self):
        from product_radar.portal_storage import _validate_spec
        for path in ["/admin/drafts", "/admin/download/issue-one/1/report-en.pdf"]:
            spec = self.spec(); spec["url"] = ORIGIN + path
            _validate_spec(spec, b"")
            for method in ("PUT", "POST", "DELETE", "HEAD"):
                spec["method"] = method
                with self.assertRaisesRegex(SafetyError, "PORTAL_REQUEST_INVALID"):
                    _validate_spec(spec, b"")

    def test_oversized_body_is_refused_before_read(self):
        class FakeConnection:
            status = 200
            def __init__(self, *args, **kwargs): pass
            def request(self, *args, **kwargs): pass
            def getresponse(self): return self
            def getheaders(self): return [("Content-Length", str(MAX_INDEX + 1))]
            def read(self, limit): raise AssertionError("oversized response must not be read")
            def close(self): pass
        with self.assertRaisesRegex(SafetyError, "PORTAL_RESPONSE_LIMIT"):
            _perform_raw(self.spec(), b"", connection_factory=FakeConnection)

    def test_owned_process_deadline_covers_stalled_dns_or_body_and_reaps_process(self):
        started = time.monotonic()
        with self.assertRaisesRegex(SafetyError, "PORTAL_REQUEST_DEADLINE_RECONCILE"):
            _worker_request(self.spec(), b"", timeout=0.08, command=[sys.executable, "-c", "import time;time.sleep(30)"])
        self.assertLess(time.monotonic() - started, 3)

    def test_private_spec_uses_stdin_not_args_environment_or_files(self):
        metadata = canonical({"ok": True, "status": 200, "headers": {"content-type": "application/json"}})
        output = struct.pack(">I", len(metadata)) + metadata + canonical(snapshot())
        class FakeProcess:
            returncode = 0
            def __init__(self): self.stdin = io.BytesIO(); self.stdout = io.BytesIO(); self.input = None
            def communicate(self, value, timeout): self.input = value; return output, None
            def poll(self): return 0
        process = FakeProcess()
        with patch("product_radar.portal_storage.subprocess.Popen", return_value=process) as created:
            result = _worker_request(self.spec(), b"", timeout=30)
        arguments, keywords = created.call_args
        self.assertNotIn(TOKEN, repr(arguments)); self.assertNotIn(TOKEN, repr(keywords))
        self.assertIn(TOKEN.encode(), process.input)
        self.assertEqual(result.body, canonical(snapshot()))
        self.assertTrue(process.stdin.closed); self.assertTrue(process.stdout.closed)

    def test_worker_discards_arbitrary_error_output_and_preserves_no_secrets(self):
        command = [sys.executable, "-c", "import sys;sys.stdin.buffer.read();sys.stdout.write('bad-output');sys.stderr.write('private error');sys.exit(1)"]
        with self.assertRaisesRegex(SafetyError, "PORTAL_NETWORK_FAILED"):
            _worker_request(self.spec(), b"", timeout=5, command=command)

    def test_default_transport_uses_isolated_worker_not_provider_adapter(self):
        with patch("product_radar.portal_storage._worker_request", return_value=response(snapshot())) as isolated:
            result = StdlibPortalTransport().request("GET", ORIGIN + "/admin/index", headers=self.spec()["headers"], body=b"", timeout=30, max_response_bytes=MAX_INDEX)
        self.assertEqual(result.status, 200); self.assertEqual(isolated.call_count, 1)

    def test_actual_child_reads_binary_private_pipe_and_returns_checked_response(self):
        # Replace HTTPSConnection inside this owned child only. No DNS/socket.
        child = """
import sys
sys.path.insert(0, sys.argv[1])
import product_radar.portal_storage as p
class Connection:
    status = 200
    def __init__(self, *args, **kwargs): pass
    def request(self, *args, **kwargs): pass
    def getresponse(self): return self
    def getheaders(self): return [('Content-Type', 'application/json'), ('Content-Length', '2')]
    def read(self, limit): return b'{}'
    def close(self): pass
p.http.client.HTTPSConnection = Connection
p._worker_main()
"""
        result = _worker_request(self.spec(), b"", timeout=10, command=[sys.executable, "-c", child, str(ROOT / "src")])
        self.assertEqual(result, PortalResponse(200, {"content-type": "application/json", "content-length": "2"}, b"{}"))

    def test_owned_worker_accepts_connection_factory_without_replacing_default_https(self):
        child = """
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import product_radar.portal_storage as p
class Connection:
    status = 200
    def __init__(self, hostname, timeout):
        assert hostname == 'reports.example.com' and timeout == 15
    def request(self, method, path, **kwargs):
        assert method == 'GET' and path == '/admin/index'
    def getresponse(self): return self
    def getheaders(self): return [('Content-Type', 'application/json'), ('Content-Length', '2')]
    def read(self, limit): return b'{}'
    def close(self): pass
p._worker_main(connection_factory=Connection)
"""
        result = _worker_request(self.spec(), b"", timeout=10,
                                 command=[sys.executable, "-c", child, str(ROOT / "src")])
        self.assertEqual(result, PortalResponse(200, {"content-type": "application/json", "content-length": "2"}, b"{}"))

    def test_connection_factory_cannot_bypass_worker_request_validation(self):
        child = """
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import product_radar.portal_storage as p
def forbidden(*args, **kwargs):
    sys.stderr.write('FACTORY_WAS_CALLED')
    raise AssertionError('connection must not be attempted')
p._worker_main(connection_factory=forbidden)
"""
        invalid = self.spec()
        invalid["url"] = ORIGIN + "/unrestricted"
        raw = canonical(invalid)
        process = subprocess.run([sys.executable, "-c", child, str(ROOT / "src")],
            input=struct.pack(">I", len(raw)) + raw, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10, check=False)
        size = struct.unpack(">I", process.stdout[:4])[0]
        self.assertEqual(process.stdout[4:4 + size], b'{"ok":false}')
        self.assertEqual(process.stderr, b"")

    def test_injected_factory_remains_inside_owned_worker_deadline(self):
        child = """
import sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import product_radar.portal_storage as p
def stalled(*args, **kwargs): time.sleep(30)
p._worker_main(connection_factory=stalled)
"""
        started = time.monotonic()
        with self.assertRaisesRegex(SafetyError, "PORTAL_REQUEST_DEADLINE_RECONCILE"):
            _worker_request(self.spec(), b"", timeout=0.08,
                            command=[sys.executable, "-c", child, str(ROOT / "src")])
        self.assertLess(time.monotonic() - started, 3)


class ActualWorkerFixtureTransport:
    """Actual JS bridge, in-memory R2, manufactured bytes; no hosted requests."""
    script = r"""
import {handleAdminRequest} from './portal/release-admin.mjs';
let raw='';for await(const part of process.stdin)raw+=part;
const data=JSON.parse(raw), objects=new Map(data.objects.map(([key,body,etag])=>[key,{body:Buffer.from(body,'base64'),etag}]));
let version=data.version;
const bucket={
async get(key){const v=objects.get(key);return v?{size:v.body.length,etag:v.etag,arrayBuffer:async()=>new Uint8Array(v.body).buffer}:null;},
async put(key,body,options){const old=objects.get(key),c=options.onlyIf;
if(c instanceof Headers&&c.get('If-None-Match')==='*'&&old)return null;
if(c?.etagMatches!==undefined&&old?.etag!==c.etagMatches)return null;
const etag='fixture-etag-'+(++version);objects.set(key,{body:Buffer.from(body),etag});return{etag};}}
const env={REPORTS:bucket,CLIENT_ID:data.client,ACCESS_AUD:'fixture-audience',OPERATOR_EMAILS:'["operator@example.com"]',ALLOWED_EMAILS:'["client@example.com"]',PUBLICATION_TIME:'08:00',ONGOING_LANGUAGE:''};
const ctx={access:{aud:'fixture-audience',getIdentity:async()=>({email:'operator@example.com'})}};
const r=data.request;
const result=await handleAdminRequest(new Request(r.url,{method:r.method,headers:r.headers,...(r.body?{body:Buffer.from(r.body,'base64')}:{})}),env,ctx,{clock:()=>new Date('2026-10-12T10:00:00.000Z')});
const selected=['content-type','content-length','content-encoding','x-content-sha256'];
process.stdout.write(JSON.stringify({status:result.status,headers:Object.fromEntries([...result.headers].filter(([k])=>selected.includes(k))),body:Buffer.from(await result.arrayBuffer()).toString('base64'),version,objects:[...objects].map(([k,v])=>[k,v.body.toString('base64'),v.etag])}));
"""

    def __init__(self, client):
        self.client, self.objects, self.version, self.calls = client, [], 0, []

    def request(self, method, url, *, headers, body, timeout, max_response_bytes):
        self.calls.append((method, url))
        raw = canonical({"client": self.client, "objects": self.objects, "version": self.version,
                         "request": {"method": method, "url": url, "headers": headers, "body": base64.b64encode(body).decode()}})
        result = subprocess.run(["node", "--input-type=module", "-e", self.script], cwd=ROOT, input=raw,
                                capture_output=True, timeout=min(timeout, 20), check=True)
        from product_radar.models import decode_json
        data = decode_json(result.stdout)
        self.objects, self.version = data["objects"], data["version"]
        return PortalResponse(data["status"], data["headers"], base64.b64decode(data["body"], validate=True))


class ActualBridgeIntegrationTests(unittest.TestCase):
    def test_actual_python_package_adapter_worker_and_receipt_remain_private_until_schedule(self):
        from test_live_outputs import fixture
        from test_delivery_package import packet
        from test_release_index import approved, SCHEDULE
        from product_radar.delivery_package import build_delivery_manifest
        from product_radar.release_index import activate_delivery_package, publication_receipt
        issue, dataset = fixture()
        # Captured is only a schema rehearsal here. Every product/evidence row
        # and PDF remains plainly fictional, and storage is an in-memory fake.
        dataset["evidence_type"] = "captured"
        with tempfile.TemporaryDirectory() as root, patch("test_delivery_package.fixture", return_value=(issue, dataset)):
            issue, _, drafts, receipts = packet(root)
            manifest = build_delivery_manifest(issue, drafts, receipts, root=root)
            wire = ActualWorkerFixtureTransport(issue["client_id"])
            store = PortalStorage(ORIGIN, issue["client_id"], TOKEN, transport=wire)
            result = activate_delivery_package(store, issue, manifest, root=root, approval=approved(manifest),
                                               scheduled_at=SCHEDULE, expected_etag=None)
            self.assertFalse(result["published"])
            self.assertIsNone(publication_receipt(result["snapshot"], issue["issue_id"], 1))
            self.assertEqual(result["snapshot"]["index"]["issues"][0]["server_actor_email"], "operator@example.com")
            self.assertEqual(result["snapshot"]["index"]["issues"][0]["languages"], ["en", "hi-Latn"])
            self.assertEqual(sum(method == "POST" for method, _ in wire.calls), 1)
            self.assertEqual(len(wire.objects), 6)  # Five staged files plus index.


if __name__ == "__main__":
    unittest.main()
