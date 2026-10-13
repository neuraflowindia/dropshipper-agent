"""One Apify HTTP operation, isolated so DNS/body stalls have a real deadline."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .models import SafetyError, canonical, decode_json
from .storage import atomic_bytes, atomic_json, load_json


ROOT = Path(__file__).resolve().parents[2]
SELECTED_HEADERS = ("x-apify-pagination-offset", "x-apify-pagination-limit", "x-apify-pagination-count",
                    "x-apify-pagination-total", "retry-after")


class HttpFailure(SafetyError):
    def __init__(self, code, *, status=None, retry_after=None, retryable=False):
        super().__init__(code)
        self.status, self.retry_after, self.retryable = status, retry_after, retryable


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class WireResponse:
    body: bytes
    status: int
    headers: dict
    elapsed: float


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) != {"method", "path", "query", "body", "byte_limit", "token"}:
        raise SafetyError("INVALID_PROVIDER_REQUEST")
    method, path = spec["method"], spec["path"]
    if not isinstance(path, str):
        raise SafetyError("INVALID_PROVIDER_REQUEST")
    fixed = {"/v2/acts/curious_coder~facebook-ads-library-scraper",
             "/v2/acts/amazon-scraper~amazon-bestsellers-scraper",
             "/v2/acts/sourabhbgp~google-trends-scraper"}
    allowed = ((method == "POST" and path in {p + "/runs" for p in fixed})
               or (method == "GET" and (path in fixed | {"/v2/actor-runs", "/v2/users/me", "/v2/users/me/usage/monthly"}
                   or re.fullmatch(r"/v2/actor-runs/[A-Za-z0-9_-]{1,80}", path)
                   or re.fullmatch(r"/v2/actor-builds/[A-Za-z0-9_-]{1,80}", path)
                   or re.fullmatch(r"/v2/datasets/[A-Za-z0-9_-]{1,80}/items", path)
                   or re.fullmatch(r"/v2/key-value-stores/[A-Za-z0-9_-]{1,80}/records/INPUT", path))))
    limit, token = spec["byte_limit"], spec["token"]
    if not allowed or type(limit) is not int or not 1 <= limit <= 5 * 1024 * 1024:
        raise SafetyError("INVALID_PROVIDER_REQUEST")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", token):
        raise SafetyError("INVALID_TOKEN")
    if spec["query"] is not None and not isinstance(spec["query"], dict):
        raise SafetyError("INVALID_PROVIDER_REQUEST")
    query = spec["query"] or {}
    allowed_keys = ({"build", "timeout", "maxTotalChargeUsd", "restartOnError"} if method == "POST" else
                    {"offset", "limit", "desc", "startedAfter"} if path == "/v2/actor-runs" else
                    {"format", "offset", "limit"} if path.startswith("/v2/datasets/") else set())
    if set(query) - allowed_keys or any(type(value) not in (str, int) for value in query.values()):
        raise SafetyError("INVALID_PROVIDER_REQUEST")
    if method == "GET" and spec["body"] is not None:
        raise SafetyError("INVALID_PROVIDER_REQUEST")


def safe_retry_after(headers):
    value = headers.get("retry-after", "") if headers else ""
    return min(int(value), 30) if isinstance(value, str) and re.fullmatch(r"[0-9]{1,8}", value) else None


def perform_raw(spec, opener=None):
    """Worker-only in production; injected opener is for offline tests."""
    validate_spec(spec)
    url = "https://api.apify.com" + spec["path"]
    if spec["query"]:
        url += "?" + urlencode(spec["query"])
    req = Request(url, method=spec["method"], data=canonical(spec["body"]) if spec["body"] is not None else None,
                  headers={"Authorization": "Bearer " + spec["token"], "Accept": "application/json",
                           "Accept-Encoding": "identity", "Content-Type": "application/json",
                           "User-Agent": "NeuraFlow-Benchmark/1.1"})
    started = time.monotonic()
    client = opener if opener is not None else build_opener(ProxyHandler({}), NoRedirects())
    try:
        with client.open(req, timeout=10) as response:
            if response.status not in (200, 201):
                raise HttpFailure("PROVIDER_HTTP_ERROR", status=response.status)
            raw = response.read(spec["byte_limit"] + 1)
            source_headers = getattr(response, "headers", {})
            headers = {key: str(source_headers.get(key)) for key in SELECTED_HEADERS if source_headers.get(key) is not None}
        if len(raw) > spec["byte_limit"]:
            raise HttpFailure("RESPONSE_SIZE_LIMIT")
        if spec["token"].encode() in raw or any(spec["token"] in value for value in headers.values()):
            raise HttpFailure("CREDENTIAL_REFLECTION_REFUSED")
        if any(len(value) > 64 for value in headers.values()):
            raise HttpFailure("RESPONSE_SIZE_LIMIT")
        return WireResponse(raw, response.status, headers, time.monotonic() - started)
    except HTTPError as error:
        status = error.code
        headers = {"retry-after": error.headers.get("Retry-After", "")} if error.headers else {}
        error.close()
        if 300 <= status < 400:
            raise HttpFailure("PROVIDER_REDIRECT_REFUSED", status=status) from None
        raise HttpFailure("PROVIDER_HTTP_ERROR", status=status, retry_after=safe_retry_after(headers),
                          retryable=status == 429 or 500 <= status <= 599) from None
    except (URLError, TimeoutError, OSError):
        raise HttpFailure("PROVIDER_NETWORK_ERROR", retryable=True) from None


def worker_main(directory):
    """Fixed small result files; no stdout, credential files or raw errors."""
    folder = Path(directory)
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise SafetyError("REQUEST_SIZE_LIMIT")
        spec = decode_json(raw)
        wire = perform_raw(spec)
        atomic_bytes(folder / "body.bin", wire.body)
        atomic_json(folder / "result.json", {"ok": True, "status": wire.status, "headers": wire.headers,
                    "elapsed": wire.elapsed, "bytes": len(wire.body), "hash": hashlib.sha256(wire.body).hexdigest()})
    except HttpFailure as error:
        atomic_json(folder / "result.json", {"ok": False, "code": str(error), "status": error.status,
                    "retry_after": error.retry_after, "retryable": error.retryable})
    except Exception:
        atomic_json(folder / "result.json", {"ok": False, "code": "HTTP_WORKER_FAILED", "status": None,
                    "retry_after": None, "retryable": False})
    return 0


def worker_call(spec, *, timeout=30, command=None):
    validate_spec(spec)
    encoded = canonical(spec)
    if len(encoded) > 65536 or not 0 < timeout <= 30:
        raise SafetyError("REQUEST_SIZE_OR_TIME_LIMIT")
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR") if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="radar-http-") as temporary:
        arguments = (command or [sys.executable, str(ROOT / "scripts/apify_http_worker.py")]) + [temporary]
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=environment, cwd=ROOT, start_new_session=True)
        try:
            process.communicate(encoded, timeout=timeout)
        except BaseException as error:
            # Only this freshly started process group is ours. The worker never
            # invokes a browser or other unrelated project process.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
            if not isinstance(error, subprocess.TimeoutExpired):
                raise
            raise HttpFailure("HTTP_DEADLINE_EXCEEDED", retryable=True) from None
        finally:
            if process.stdin is not None:
                process.stdin.close()
        if process.returncode != 0:
            raise HttpFailure("HTTP_WORKER_FAILED")
        result = load_json(Path(temporary) / "result.json", 4096)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise HttpFailure("HTTP_WORKER_FAILED")
        if not result["ok"]:
            allowed = {"PROVIDER_HTTP_ERROR", "PROVIDER_REDIRECT_REFUSED", "PROVIDER_NETWORK_ERROR", "RESPONSE_SIZE_LIMIT",
                       "CREDENTIAL_REFLECTION_REFUSED", "HTTP_WORKER_FAILED"}
            if result.get("code") not in allowed:
                raise HttpFailure("HTTP_WORKER_FAILED")
            if ((result.get("status") is not None and (type(result["status"]) is not int or not 100 <= result["status"] <= 599))
                    or (result.get("retry_after") is not None and (type(result["retry_after"]) is not int or not 0 <= result["retry_after"] <= 30))
                    or type(result.get("retryable")) is not bool):
                raise HttpFailure("HTTP_WORKER_FAILED")
            raise HttpFailure(result["code"], status=result.get("status"), retry_after=result.get("retry_after"),
                              retryable=result.get("retryable") is True)
        raw_path = Path(temporary) / "body.bin"
        if raw_path.is_symlink():
            raise HttpFailure("HTTP_WORKER_FAILED")
        with raw_path.open("rb") as handle:
            raw = handle.read(spec["byte_limit"] + 1)
        if len(raw) > spec["byte_limit"] or result.get("bytes") != len(raw) or result.get("hash") != hashlib.sha256(raw).hexdigest():
            raise HttpFailure("HTTP_WORKER_FAILED")
        if type(result.get("status")) is not int or result["status"] not in (200, 201) or not isinstance(result.get("headers"), dict):
            raise HttpFailure("HTTP_WORKER_FAILED")
        if spec["token"].encode() in raw or any(not isinstance(value, str) or len(value) > 64 or spec["token"] in value for value in result["headers"].values()):
            raise HttpFailure("HTTP_WORKER_FAILED")
        return WireResponse(raw, result["status"], {key: result["headers"][key] for key in SELECTED_HEADERS if key in result["headers"]}, result.get("elapsed", 0))
