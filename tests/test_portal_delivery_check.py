"""Offline fixtures only; no provider request, login or publication."""

from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar import portal_delivery_check as delivery
from product_radar.models import SafetyError
from product_radar.models import canonical
from product_radar.portal_storage import PortalStorage, PortalResponse

TOKEN = "manufacturedheader.manufacturedpayload.manufacturedsignature"


class Storage:
    def __init__(self):
        self.objects, self.calls = {}, []
        self.index = {"index": {"schema_version": 1, "client_id": "default", "issues": []}, "etag": None}
        self.unknown, self.unknown_commits = False, True
        self.corrupt, self.changed_index, self.error = False, False, None

    def read_index(self, key):
        self.calls.append(("index", key))
        value = deepcopy(self.index)
        value["server_observed_at"] = str(len(self.calls))
        if self.changed_index and len(self.calls) > 1:
            value["etag"] = "changed"
        return value

    def put_immutable(self, key, raw, **kwargs):
        self.calls.append(("put", key))
        if self.error:
            raise self.error
        if key in self.objects and self.objects[key] != raw:
            raise SafetyError("PORTAL_HTTP_REQUEST_REJECTED")
        if not self.unknown or self.unknown_commits:
            self.objects[key] = raw
        if self.unknown:
            raise SafetyError("PORTAL_REQUEST_FAILED_RECONCILE")

    def read_object(self, key):
        self.calls.append(("read", key))
        if key not in self.objects:
            raise SafetyError("PORTAL_HTTP_REQUEST_REJECTED")
        return b"wrong" if self.corrupt else self.objects[key]


class PrivateDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        records = []
        for relative, name, media, _, _ in delivery.ARTIFACTS:
            raw = b"%PDF-1.7\nIllustrative sample; not live research" if name.endswith("pdf") else b"PK\nIllustrative sample; not live research"
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            records.append((relative, name, media, len(raw), hashlib.sha256(raw).hexdigest()))
        self.records = tuple(records)
        self.patcher = patch.object(delivery, "ARTIFACTS", self.records)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.store = Storage()
        self.loader = Mock(return_value=TOKEN)
        self.factory = Mock(return_value=self.store)

    def check(self, live=True):
        return delivery.check_private_delivery(self.root, live=live,
            token_loader=self.loader, storage_factory=self.factory)

    def assert_code(self, code, action):
        with self.assertRaisesRegex(SafetyError, "^" + code + "$"):
            action()

    def test_offline_default_reads_only_fixed_files_and_never_authenticates(self):
        result = self.check(False)
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(result["artifact_count"], 2)
        self.assertFalse(result["client_report"])
        self.assertFalse(result["publication_attempted"])
        self.loader.assert_not_called()
        self.factory.assert_not_called()
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_live_uses_fixed_origin_client_and_only_two_fixed_object_keys(self):
        result = self.check()
        self.factory.assert_called_once_with(delivery.ORIGIN, "default", TOKEN)
        self.assertEqual(result["state"], "private_bytes_verified")
        self.assertTrue(result["index_unchanged"])
        self.assertEqual(result["verified_artifact_count"], 2)
        self.assertEqual([x[0] for x in self.store.calls], ["index", "put", "read", "put", "read", "index"])
        self.assertEqual(set(self.store.objects), {delivery.PREFIX + "report-en.pdf", delivery.PREFIX + "shortlist.xlsx"})
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn("server_observed_at", json.dumps(result))

    def test_idempotent_replay_keeps_original_objects_and_no_release_index_write(self):
        self.check()
        original = dict(self.store.objects)
        self.check()
        self.assertEqual(original, self.store.objects)
        self.assertTrue(all(key != delivery.INDEX_KEY for action, key in self.store.calls if action == "put"))

    def test_unknown_committed_writes_read_back_once_without_retry(self):
        self.store.unknown = True
        result = self.check()
        self.assertEqual(result["recovered_write_count"], 2)
        self.assertEqual([x[0] for x in self.store.calls], ["index", "put", "read", "put", "read", "index"])

    def test_unknown_uncommitted_write_stops_without_retry_or_second_object(self):
        self.store.unknown = True
        self.store.unknown_commits = False
        self.assert_code("PRIVATE_DELIVERY_WRITE_UNCONFIRMED", self.check)
        self.assertEqual([x[0] for x in self.store.calls], ["index", "put", "read", "index"])

    def test_unknown_committed_wrong_bytes_are_not_accepted(self):
        self.store.unknown = self.store.corrupt = True
        self.assert_code("PRIVATE_DELIVERY_WRITE_UNCONFIRMED", self.check)

    def test_corrupt_readback_stops_before_second_upload(self):
        self.store.corrupt = True
        self.assert_code("PRIVATE_DELIVERY_READBACK_MISMATCH", self.check)
        self.assertEqual([x[0] for x in self.store.calls], ["index", "put", "read", "index"])

    def test_changed_index_etag_is_reported_not_repaired(self):
        self.store.changed_index = True
        self.assert_code("PRIVATE_DELIVERY_INDEX_CHANGED", self.check)

    def test_changed_index_body_even_same_etag_is_detected(self):
        original = self.store.read_index
        def read(key):
            value = original(key)
            if len(self.store.calls) > 1:
                value["index"]["issues"] = [{"unexpected": "private"}]
            return value
        self.store.read_index = read
        self.assert_code("PRIVATE_DELIVERY_INDEX_CHANGED", self.check)

    def test_existing_different_object_is_never_overwritten(self):
        key = delivery.PREFIX + "report-en.pdf"
        self.store.objects[key] = b"different original"
        self.assert_code("PORTAL_HTTP_REQUEST_REJECTED", self.check)
        self.assertEqual(self.store.objects[key], b"different original")

    def test_invalid_local_hash_refuses_authentication(self):
        (self.root / self.records[0][0]).write_bytes(b"%PDF-wrong same purpose")
        self.assert_code("PRIVATE_DELIVERY_ARTIFACT_INVALID", self.check)
        self.loader.assert_not_called()

    def test_missing_artifact_refuses_authentication(self):
        (self.root / self.records[1][0]).unlink()
        self.assert_code("PRIVATE_DELIVERY_ARTIFACT_INVALID", self.check)
        self.loader.assert_not_called()

    def test_symlink_artifact_is_refused_even_when_hash_matches(self):
        path = self.root / self.records[0][0]
        replacement = self.root / "copy.pdf"
        replacement.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(replacement)
        self.assert_code("PRIVATE_DELIVERY_ARTIFACT_INVALID", self.check)

    def test_symlink_parent_is_refused(self):
        folder = self.root / "outputs" / "client-preview"
        relocated = self.root / "relocated"
        folder.rename(relocated)
        folder.symlink_to(relocated, target_is_directory=True)
        self.assert_code("PRIVATE_DELIVERY_ARTIFACT_INVALID", self.check)

    def test_oversized_and_nonregular_files_are_refused(self):
        with patch.object(delivery, "MAX_FILE", 1):
            self.assert_code("PRIVATE_DELIVERY_ARTIFACT_INVALID", self.check)
        path = self.root / self.records[0][0]
        path.unlink()
        path.mkdir()
        self.assert_code("PRIVATE_DELIVERY_ARTIFACT_INVALID", self.check)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires filesystem FIFO support")
    def test_fifo_is_refused_before_any_blocking_read(self):
        path = self.root / self.records[0][0]
        path.unlink()
        os.mkfifo(path)
        self.assert_code("PRIVATE_DELIVERY_ARTIFACT_INVALID", self.check)
        self.loader.assert_not_called()

    def test_real_portal_storage_interface_uses_only_get_and_fixed_object_put(self):
        objects, calls = {}, []
        class Wire:
            def request(wire, method, url, **kwargs):
                calls.append((method, url))
                if url.endswith("/admin/index"):
                    self.assertEqual(method, "GET")
                    raw = canonical({"index": {"schema_version": 1, "client_id": "default", "issues": []},
                        "etag": None, "verified": True, "authority": "release_index",
                        "server_observed_at": "2026-10-06T21:00:00+00:00"})
                    media = "application/json"
                elif method == "PUT":
                    name = url.rsplit("/", 1)[1]
                    objects[name] = kwargs["body"]
                    raw = canonical({"key": delivery.PREFIX + name, "sha256": hashlib.sha256(objects[name]).hexdigest(), "size": len(objects[name])})
                    media = "application/json"
                else:
                    name = url.rsplit("/", 1)[1]
                    raw = objects[name]
                    media = "application/pdf" if name.endswith("pdf") else delivery.ARTIFACTS[1][2]
                headers = {"content-type": media, "content-length": str(len(raw))}
                if method == "GET" and not url.endswith("/admin/index"):
                    headers["x-content-sha256"] = hashlib.sha256(raw).hexdigest()
                return PortalResponse(200, headers, raw)
        result = delivery.check_private_delivery(self.root, live=True, token_loader=self.loader,
            storage_factory=lambda origin, client, token: PortalStorage(origin, client, token, transport=Wire()))
        self.assertEqual(result["verified_artifact_count"], 2)
        self.assertEqual([method for method, _ in calls], ["GET", "PUT", "GET", "PUT", "GET", "GET"])

    def test_raw_prepared_bytes_are_absent_from_repr(self):
        items = delivery.prepare_artifacts(self.root)
        self.assertNotIn("Illustrative sample", repr(items))

    def test_mode_is_exact_boolean(self):
        for value in [1, "yes", None]:
            self.assert_code("PRIVATE_DELIVERY_MODE_INVALID", lambda: self.check(value))

    def test_sanitized_failures_never_expose_provider_or_token_text(self):
        for error in [ValueError(TOKEN), SafetyError(TOKEN), RuntimeError("private provider body")]:
            result = delivery.public_failure(error)
            self.assertEqual(result["reason"], "PRIVATE_DELIVERY_CHECK_FAILED")
            self.assertNotIn(TOKEN, json.dumps(result))

    def test_unexpected_storage_error_is_fixed_and_index_checked(self):
        self.store.error = RuntimeError(TOKEN)
        self.assert_code("PRIVATE_DELIVERY_CHECK_FAILED", self.check)
        self.assertEqual([x[0] for x in self.store.calls], ["index", "put", "index"])


class CachedOwnerTokenTests(unittest.TestCase):
    def test_missing_executable_never_starts_process(self):
        run = Mock()
        with self.assertRaisesRegex(SafetyError, "PRIVATE_DELIVERY_CLOUDFLARED_REQUIRED"):
            delivery.cached_owner_token(find_executable=lambda name: None, run=run)
        run.assert_not_called()

    def test_cached_token_command_is_exact_private_bounded_noninteractive(self):
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout=(TOKEN + "\n").encode(), stderr=b""))
        self.assertEqual(delivery.cached_owner_token(find_executable=lambda name: "/trusted/cloudflared", run=run), TOKEN)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/trusted/cloudflared", "access", "token", "--app", delivery.ORIGIN])
        self.assertEqual(kwargs, {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                                  "stderr": subprocess.PIPE, "timeout": 20, "check": False})
        self.assertNotIn(TOKEN, repr(run.call_args))

    def test_unknown_errors_timeout_or_malformed_output_never_claim_missing_login(self):
        outcomes = [SimpleNamespace(returncode=1, stdout=TOKEN.encode(), stderr=b"unknown provider error"),
                    SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
                    SimpleNamespace(returncode=0, stdout=b"not a JWT", stderr=b""),
                    SimpleNamespace(returncode=0, stdout=b"x" * 20000, stderr=b""),
                    SimpleNamespace(returncode=0, stdout=b"\xff", stderr=b""),
                    subprocess.TimeoutExpired("cloudflared", 20, output=TOKEN.encode()),
                    OSError(TOKEN)]
        for outcome in outcomes:
            run = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
            with self.subTest(outcome=type(outcome).__name__):
                with self.assertRaisesRegex(SafetyError, "^PRIVATE_DELIVERY_AUTH_CHECK_UNAVAILABLE$"):
                    delivery.cached_owner_token(find_executable=lambda name: "/trusted/cloudflared", run=run)
                self.assertEqual(run.call_count, 1)
                self.assertNotIn("login", run.call_args.args[0])

    def test_explicit_installed_binary_path_supported_without_path_lookup(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "cloudflared"
            binary.touch()
            binary.chmod(0o700)
            lookup = Mock()
            run = Mock(return_value=SimpleNamespace(returncode=0, stdout=TOKEN.encode(), stderr=b""))
            self.assertEqual(delivery.cached_owner_token(executable_path=binary, find_executable=lookup, run=run), TOKEN)
            lookup.assert_not_called()
            self.assertEqual(run.call_args.args[0][0], str(binary))

    def test_invalid_explicit_binary_never_runs(self):
        for path in [Path("cloudflared"), Path("/missing-neuraflow-cloudflared")]:
            run = Mock()
            with self.assertRaisesRegex(SafetyError, "PRIVATE_DELIVERY_CLOUDFLARED_REQUIRED"):
                delivery.cached_owner_token(executable_path=path, run=run)
            run.assert_not_called()

    def test_exact_fixed_host_application_lookup_failure_has_route_code(self):
        for diagnostic in [f"failed to find Access application at {delivery.ORIGIN}\n",
                           f'2026-10-06 ERR error="failed to find Access application at {delivery.ORIGIN}"\n']:
            run = Mock(return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=diagnostic.encode()))
            with self.assertRaisesRegex(SafetyError, "^PRIVATE_DELIVERY_ACCESS_ROUTE_UNAVAILABLE$"):
                delivery.cached_owner_token(find_executable=lambda name: "/trusted/cloudflared", run=run)
            self.assertEqual(run.call_count, 1)

    def test_other_host_or_suffix_does_not_establish_current_route_failure(self):
        for host in ["https://other.example.com", delivery.ORIGIN + ".evil.example", delivery.ORIGIN + "/other"]:
            run = Mock(return_value=SimpleNamespace(returncode=1, stdout=b"",
                stderr=f"failed to find Access application at {host}".encode()))
            with self.assertRaisesRegex(SafetyError, "^PRIVATE_DELIVERY_AUTH_CHECK_UNAVAILABLE$"):
                delivery.cached_owner_token(find_executable=lambda name: "/trusted/cloudflared", run=run)

    def test_only_actual_missing_cache_diagnostic_requires_owner_login(self):
        for status in [0, 1]:
            run = Mock(return_value=SimpleNamespace(returncode=status, stdout=b"", stderr=
                b"Unable to find token for provided application. Please run login command to generate token.\n"))
            with self.assertRaisesRegex(SafetyError, "^PRIVATE_DELIVERY_OWNER_LOGIN_REQUIRED$"):
                delivery.cached_owner_token(find_executable=lambda name: "/trusted/cloudflared", run=run)
            self.assertNotIn("login", run.call_args.args[0])

    def test_route_failure_takes_precedence_over_missing_cache_text(self):
        run = Mock(return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=(
            f"failed to find Access application at {delivery.ORIGIN}\n"
            "Unable to find token for provided application. Please run login command to generate token.\n").encode()))
        with self.assertRaisesRegex(SafetyError, "^PRIVATE_DELIVERY_ACCESS_ROUTE_UNAVAILABLE$"):
            delivery.cached_owner_token(find_executable=lambda name: "/trusted/cloudflared", run=run)

    def test_oversized_stderr_is_not_examined_or_exposed(self):
        run = Mock(return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=
            (f"failed to find Access application at {delivery.ORIGIN}\n" + TOKEN * 1000).encode()))
        with self.assertRaisesRegex(SafetyError, "^PRIVATE_DELIVERY_AUTH_CHECK_UNAVAILABLE$"):
            delivery.cached_owner_token(find_executable=lambda name: "/trusted/cloudflared", run=run)

    def test_private_diagnostics_are_never_in_public_failure(self):
        run = Mock(return_value=SimpleNamespace(returncode=1, stdout=TOKEN.encode(), stderr=
            f'{TOKEN}\nERR error="failed to find Access application at {delivery.ORIGIN}"'.encode()))
        with self.assertRaises(SafetyError) as error:
            delivery.cached_owner_token(find_executable=lambda name: "/trusted/cloudflared", run=run)
        output = json.dumps(delivery.public_failure(error.exception))
        self.assertNotIn(TOKEN, output)
        self.assertNotIn("failed to find", output)
        self.assertIn("PRIVATE_DELIVERY_ACCESS_ROUTE_UNAVAILABLE", output)


class PrivateDeliveryCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("private_delivery_cli", ROOT / "scripts/check_portal_private_delivery.py")
        cls.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cli)

    def test_cli_default_offline_and_json_receipt_only(self):
        with patch.object(self.cli, "check_private_delivery", return_value={"state": "prepared"}) as check, patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(self.cli.main([]), 0)
            self.assertEqual(json.loads(out.getvalue()), {"state": "prepared"})
            self.assertFalse(check.call_args.kwargs["live"])

    def test_cli_live_is_explicit_and_binary_option_uses_private_loader(self):
        with patch.object(self.cli, "cached_owner_token", return_value=TOKEN) as load, patch.object(self.cli, "check_private_delivery", return_value={"state": "private_bytes_verified"}) as check, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(self.cli.main(["--live", "--cloudflared-path", "/trusted/cloudflared"]), 0)
            self.assertTrue(check.call_args.kwargs["live"])
            self.assertEqual(check.call_args.kwargs["token_loader"](), TOKEN)
            load.assert_called_once_with(executable_path=Path("/trusted/cloudflared"))

    def test_cli_failure_is_sanitized_and_nonzero(self):
        with patch.object(self.cli, "check_private_delivery", side_effect=RuntimeError(TOKEN)), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(self.cli.main(["--live"]), 1)
            self.assertNotIn(TOKEN, out.getvalue())
            self.assertEqual(json.loads(out.getvalue())["reason"], "PRIVATE_DELIVERY_CHECK_FAILED")

    def test_cli_has_no_token_or_arbitrary_artifact_or_publication_options(self):
        for flag in ["--token", "--artifact", "--publish", "--client-id", "--issue", "--origin"]:
            with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                self.cli.main([flag, "private"])
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
