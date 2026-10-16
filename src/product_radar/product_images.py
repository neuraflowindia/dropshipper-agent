"""Retrieve genuine public product photos once, with source and byte provenance.

Photos support the report and optional visual matching; they are not sales or
identity proof. A failed photo leaves text research running. No generated art,
personal browser profile, billing change or model call exists in this module.
"""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import http.client
import queue
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit
import base64
import binascii
import warnings

from .models import SafetyError, digest, identifier, timestamp, utc_now
from . import public_pages
from .research_memory import key
from .runtime_storage import runtime_path
from .storage import Journal, atomic_bytes, atomic_json, load_json, safe_path
from .visual_identity import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS

VERSION = "public-product-image-v1"
SHARED_VERSION = "public-product-image-v2"
SHARED_READ_KIND = "public-product-image-read-v1"
RENEWABLE_VERSION = "public-product-image-v3"
RENEWABLE_READ_KIND = "public-product-image-read-v2"
_IMAGE_HEADERS = {"Accept": "image/jpeg,image/png,image/webp", "Accept-Encoding": "identity"}
MEDIA_ROOT = runtime_path(Path(__file__).resolve().parents[2], "media/live/product-images")
_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
MIN_REPORT_IMAGE_SHORT_EDGE = 128


def report_image_quality(width, height):
    """A minimum report size, not proof that an image depicts this product."""
    if any(type(value) is not int or value <= 0 for value in (width, height)):
        raise SafetyError("PRODUCT_IMAGE_REPORT_DIMENSIONS_INVALID")
    return min(width, height) >= MIN_REPORT_IMAGE_SHORT_EDGE


def inspect_image(raw):
    """Inspect original bytes without editing, resizing or converting them."""
    from PIL import Image, UnidentifiedImageError
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_IMAGE_BYTES:
        raise SafetyError("PRODUCT_IMAGE_SIZE_LIMIT")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if image.format not in _FORMATS or image.width * image.height > MAX_IMAGE_PIXELS:
                    raise SafetyError("PRODUCT_IMAGE_FORMAT_OR_PIXEL_LIMIT")
                image.load()
                return {"sha256": sha256(raw).hexdigest(), "format": image.format,
                        "width": image.width, "height": image.height, "bytes": len(raw)}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise SafetyError("PRODUCT_IMAGE_NOT_DECODABLE") from None


def _retrieve(url, control, resolver, connection_factory, access_guard=None):
    receipt = {"requested_url": url, "final_url": None, "http_status": None, "redirects": []}
    seen = set()
    for hop in range(public_pages.MAX_REDIRECTS + 1):
        url, host, port, scheme = public_pages._public_url(url)
        wait = access_guard(url) if access_guard is not None else None
        if wait is not None:
            skipped = dict(receipt, state="failed", reason=wait["reason"], http_status=None,
                body_complete=False, metadata=None, guard_url=url, access_wait=deepcopy(wait))
            skipped.pop("retry_after", None)  # a prior redirect's header is not this host's header
            return skipped, None
        if url in seen:
            raise SafetyError("PRODUCT_IMAGE_REDIRECT_LOOP")
        seen.add(url)
        control.remaining()
        addresses = public_pages._checked_addresses(host, port, resolver)
        if control.execution_snapshot()["execution_stage"] == "not_dispatched":
            control.start_attempt()
        connection = connection_factory(host, port, scheme, addresses[0], control)
        response = None
        try:
            parsed = urlsplit(url)
            connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers={
                "User-Agent": "NeuraFlow-Public-Research/1.0", "Accept": "image/jpeg,image/png,image/webp",
                "Accept-Encoding": "identity", "Connection": "close"})
            active_socket = connection.sock
            active_socket.settimeout(control.remaining())
            response = connection.getresponse()
            control.response_received()
            receipt.update(final_url=url, http_status=response.status)
            # Keep only the bounded retry hint, never a complete header set.
            # A redirect's hint cannot become the final destination's hint.
            receipt.pop("retry_after", None)
            retry_after = response.getheader("Retry-After")
            if (isinstance(retry_after, str) and len(retry_after) <= 256
                    and not any(ord(char) < 32 or ord(char) == 127 for char in retry_after)):
                receipt["retry_after"] = retry_after
            if response.status in (301, 302, 303, 307, 308):
                if hop == public_pages.MAX_REDIRECTS or not response.getheader("Location"):
                    raise SafetyError("PRODUCT_IMAGE_REDIRECT_LIMIT")
                next_url = public_pages._public_url(urljoin(url, response.getheader("Location")))[0]
                receipt["redirects"].append({"from": url, "to": next_url, "status": response.status})
                url = next_url
                continue
            if response.status != 200:
                return dict(receipt, state="failed", reason="PRODUCT_IMAGE_HTTP_FAILED",
                            body_complete=False, metadata=None), None
            mime = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if mime not in {"image/jpeg", "image/png", "image/webp", "application/octet-stream"}:
                raise SafetyError("PRODUCT_IMAGE_CONTENT_TYPE_INVALID")
            if (response.getheader("Content-Encoding") or "identity").strip().lower() != "identity":
                raise SafetyError("PRODUCT_IMAGE_ENCODING_UNSUPPORTED")
            length = response.getheader("Content-Length")
            if length is not None and (not length.strip().isdigit() or int(length) > MAX_IMAGE_BYTES):
                raise SafetyError("PRODUCT_IMAGE_SIZE_LIMIT")
            chunks, received = [], 0
            while not response.isclosed():
                active_socket.settimeout(control.remaining())
                block = response.read1(min(16384, MAX_IMAGE_BYTES + 1 - received))
                if not block:
                    break
                received += len(block)
                if received > MAX_IMAGE_BYTES:
                    raise SafetyError("PRODUCT_IMAGE_SIZE_LIMIT")
                chunks.append(block)
            if length is not None and not getattr(response, "chunked", False) and received != int(length):
                raise SafetyError("PRODUCT_IMAGE_BODY_INCOMPLETE")
            raw = b"".join(chunks)
            control.remaining()
            metadata = inspect_image(raw)
            return dict(receipt, state="usable", reason="Original public product photo was saved.",
                        body_complete=True, metadata=metadata), raw
        finally:
            if response is not None:
                response.close()
            connection.close()
    raise SafetyError("PRODUCT_IMAGE_REDIRECT_LIMIT")


def fetch_product_image(url, *, timeout=20, resolver=None, connection_factory=None, track_access=False, access_guard=None):
    """Bounded public read; a timed-out worker can never write a late photo."""
    receipt = {"state": "failed", "reason": "PRODUCT_IMAGE_INPUT_INVALID", "requested_url": None,
               "final_url": None, "http_status": None, "redirects": [], "body_complete": False,
               "checked_at": utc_now(), "metadata": None, "dispatch_started": False}
    try:
        if (type(track_access) is not bool or access_guard is not None and (not track_access or not callable(access_guard))
                or isinstance(timeout, bool)
                or not isinstance(timeout, (int, float)) or not 0 < timeout <= 20):
            raise SafetyError("PRODUCT_IMAGE_INPUT_INVALID")
        url = public_pages._public_url(url)[0]
    except SafetyError as error:
        return dict(receipt, reason=str(error)), None
    receipt["requested_url"] = url
    slots = public_pages._SLOTS
    if not slots.acquire(blocking=False):
        value = dict(receipt, reason="PRODUCT_IMAGE_READERS_BUSY")
        if track_access:
            value.update(owned_worker_exited=True, response_received=False, execution_stage="not_dispatched")
        return value, None
    control = public_pages._Control(time.monotonic() + timeout)
    output = queue.Queue(maxsize=1)
    def work():
        try:
            result, raw = _retrieve(url, control, resolver or socket.getaddrinfo,
                                    connection_factory or public_pages._PinnedConnection, access_guard=access_guard)
            output.put((dict(result, checked_at=utc_now(),
                dispatch_started=control.execution_snapshot()["execution_stage"] != "not_dispatched"), raw))
        except Exception as error:
            reason = str(error) if isinstance(error, SafetyError) else "PRODUCT_IMAGE_TRANSPORT_FAILED"
            output.put((dict(receipt, reason=reason, checked_at=utc_now(),
                             dispatch_started=control.execution_snapshot()["execution_stage"] != "not_dispatched"), None))
        finally:
            slots.release()
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    try:
        result, raw = output.get(timeout=timeout)
    except queue.Empty:
        control.abort()
        result, raw = dict(receipt, reason="PRODUCT_IMAGE_DEADLINE",
            dispatch_started=control.execution_snapshot()["execution_stage"] != "not_dispatched", checked_at=utc_now()), None
    if track_access:
        # The network owner here is a bounded thread, not a native browser
        # process. A still-running thread is explicitly unknown, never proof
        # that a replacement request is safe. Do not wait out the whole read.
        worker.join(timeout=.05)
        result = dict(result, **control.execution_snapshot(), owned_worker_exited=not worker.is_alive())
    else:
        result.pop("retry_after", None)  # exact legacy receipt behavior
    return result, raw


def _candidate(state, concept_id, candidate):
    identifier(concept_id)
    concept = state["concepts"].get(concept_id)
    observation = state["observations"].get(candidate.get("observation_id")) if isinstance(candidate, dict) else None
    if (not concept or not observation or candidate.get("relationship") not in {"exact_product", "relevant_family"}
            or candidate.get("observed_at") != observation["observed_at"]):
        raise SafetyError("PRODUCT_IMAGE_SOURCE_REQUIRED")
    known = list(observation["facts"].get("image_urls", []))
    original = observation.get("semantic", {}).get("original_image_url")
    if original:
        known.append(original)
    clean = public_pages._public_url(candidate.get("url"))[0]
    valid_urls = set()
    for url in known:
        try:
            valid_urls.add(public_pages._public_url(url)[0])
        except SafetyError:
            continue
    if clean not in valid_urls:
        raise SafetyError("PRODUCT_IMAGE_URL_NOT_IN_SOURCE")
    timestamp(candidate["observed_at"])
    return {"concept_id": concept_id, "description_hash": digest(concept["description"]),
            "observation_id": candidate["observation_id"], "source_url": clean,
            "observed_at": candidate["observed_at"], "relationship": candidate["relationship"]}


def capture_product_image(memory, concept_id, candidate, *, media_root=None, fetcher=fetch_product_image,
                          shared_acquisitions=False, renewable_acquisitions=False, as_of=None):
    if (type(shared_acquisitions) is not bool or type(renewable_acquisitions) is not bool
            or renewable_acquisitions and not shared_acquisitions):
        raise SafetyError("PRODUCT_IMAGE_SHARED_OPTION_INVALID")
    if renewable_acquisitions:
        return _capture_renewable_image(memory, concept_id, candidate, media_root=media_root,
                                        fetcher=fetcher, as_of=as_of)
    if shared_acquisitions:
        return _capture_shared_image(memory, concept_id, candidate, media_root=media_root, fetcher=fetcher)
    binding = _candidate(memory.snapshot(), concept_id, candidate)
    capture_id = key("product-image", [VERSION, binding])
    with Journal(memory.root / "image_captures").locked(timeout=5):
        state = memory.snapshot()
        old = state["source_checks"].get(capture_id)
        if old:
            if old.get("state") == "usable":
                _verify_saved_image(old, media_root)
            return dict(deepcopy(old), reused=True)
        staged_path = safe_path(memory.root / "image_captures", capture_id) / "capture.json"
        recovered = staged_path.exists()
        if recovered:
            staged = load_json(staged_path, 64 * 1024)
            if (not isinstance(staged, dict) or set(staged) != {"value", "hash"}
                    or digest(staged["value"]) != staged["hash"]):
                raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
            value = staged["value"]
            if (not isinstance(value, dict) or any(value.get(field) != expected for field, expected in binding.items())
                    or value.get("reference") != capture_id or value.get("version") != VERSION
                    or value.get("kind") != "public_product_image" or value.get("state") not in {"usable", "failed"}
                    or value.get("model_calls") != 0 or value.get("apify_usd") != "0"):
                raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
            timestamp(value.get("at"))
            if value["state"] == "usable":
                _verify_saved_image(value, media_root)
            elif value.get("image") is not None:
                raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
        else:
            receipt, raw = fetcher(binding["source_url"])
            value = {"kind": "public_product_image", "version": VERSION, "source": "product_photo",
                     "route": "public_image_http", "reference": capture_id, "at": receipt["checked_at"],
                     **binding, "state": receipt["state"], "reason": receipt["reason"], "receipt": receipt,
                     "image": None, "model_calls": 0, "apify_usd": "0"}
            if receipt["state"] == "usable":
                metadata = inspect_image(raw)
                if metadata != receipt["metadata"]:
                    raise SafetyError("PRODUCT_IMAGE_BYTES_CHANGED")
                root = safe_path(media_root or MEDIA_ROOT)
                path = root / (metadata["sha256"] + _FORMATS[metadata["format"]])
                if path.exists():
                    if path.is_symlink() or inspect_image(path.read_bytes()) != metadata:
                        raise SafetyError("PRODUCT_IMAGE_SAVED_BYTES_CHANGED")
                else:
                    atomic_bytes(path, raw)
                value["image"] = {"path": str(path), **metadata, "source_kind": "public_product_image"}
            # Keep the exact source binding and returned result before the
            # canonical save. A brief local writer conflict must not cause a
            # second public request or lose a successfully saved photograph.
            atomic_json(staged_path, {"value": value, "hash": digest(value)})
        def unchanged(current):
            if _candidate(current, concept_id, candidate) != binding:
                raise SafetyError("PRODUCT_IMAGE_PRODUCT_CHANGED")
        from .research_memory import change
        memory.commit(capture_id, [change("source_checks", capture_id, value)], validate=unchanged)
        return dict(deepcopy(value), reused=recovered)


def _shared_read_id(url):
    # This describes the actual fixed GET, not its eventual product consumer.
    return key("product-image-read", [SHARED_READ_KIND, "GET", url,
        {"Accept": "image/jpeg,image/png,image/webp", "Accept-Encoding": "identity"}])


def _shared_record(value, read_id, url):
    if (not isinstance(value, dict) or value.get("kind") != SHARED_READ_KIND
            or value.get("reference") != read_id or value.get("source_url") != url
            or value.get("record_hash") != digest({k: v for k, v in value.items() if k != "record_hash"})
            or read_id != _shared_read_id(url)
            or value.get("state") not in {"execution_unknown", "usable", "failed"}):
        raise SafetyError("PRODUCT_IMAGE_SHARED_RECORD_INVALID")
    timestamp(value.get("at"))
    return value


def _legacy_image_response(state, url, media_root):
    """Adopt a real original success, never retry a possibly dispatched v1 read."""
    rows = []
    for ref, row in state["source_checks"].items():
        if row.get("kind") != "public_product_image" or row.get("version") != VERSION:
            continue
        binding = {field: row.get(field) for field in ("concept_id", "description_hash", "observation_id",
                    "source_url", "observed_at", "relationship")}
        if ref != key("product-image", [VERSION, binding]):
            raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
        if row.get("source_url") == url:
            rows.append((ref, row))
    usable = []
    for ref, row in rows:
        if row.get("state") != "usable":
            continue
        binding = {field: row.get(field) for field in ("concept_id", "description_hash", "observation_id",
                    "source_url", "observed_at", "relationship")}
        if ref != key("product-image", [VERSION, binding]):
            raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
        _verify_saved_image(row, media_root)
        receipt = row.get("receipt", {})
        if (receipt.get("state") != "usable" or receipt.get("requested_url") != url
                or receipt.get("metadata") != {field: row["image"][field]
                    for field in ("sha256", "format", "width", "height", "bytes")}
                or receipt.get("checked_at") != row.get("at")):
            raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
        usable.append((timestamp(row["at"]), ref, receipt, Path(row["image"]["path"]).read_bytes()))
    if usable:
        _, reference, receipt, raw = max(usable, key=lambda row: (row[0], row[1]))
        return reference, deepcopy(receipt), raw
    if rows:
        # Old failed/uncertain proof is not erased by changing the schema.
        raise SafetyError("PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING")
    return None


def _read_shared_image(memory, url, media_root, fetcher):
    """Called under the existing image writer; original response precedes apply.

    One atomic response stores both original receipt and bounded original bytes.
    This closes the byte-save/metadata-save gap without another GET. An intent
    with no response is uncertain and cannot authorise a replacement request.
    """
    from .research_memory import change
    read_id = _shared_read_id(url)
    folder = safe_path(memory.root, "image_captures", "shared", read_id)
    # Validate destinations before reserving or starting a public read. A
    # dangling link is not a missing response and must not trigger a GET.
    response_path = safe_path(folder / "response.json")
    media_destination = safe_path(media_root or MEDIA_ROOT)
    state = memory.snapshot()
    old = state["source_checks"].get(read_id)
    if old is not None:
        _shared_record(old, read_id, url)
    if not response_path.exists():
        if old is not None:
            raise SafetyError("PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING")
        adopted = _legacy_image_response(state, url, media_root)
        if adopted is None:
            at = utc_now()
            intent = {"kind": SHARED_READ_KIND, "reference": read_id, "source_url": url,
                      "state": "execution_unknown", "at": at, "receipt": None, "image": None,
                      "origin_reference": read_id, "response_hash": None}
            intent["record_hash"] = digest(intent)
            # This is saved before any invocation; no networking occurs inside
            # the research commit. A lost launch/reply remains original-only.
            from .programme import programme_service_state
            from .work_turns import active_work_turn, reserve_stage_items
            if not programme_service_state(memory)["enabled"]:
                raise SafetyError("PROGRAMME_OWNER_PAUSED")
            changes = [change("source_checks", read_id, intent)]
            if active_work_turn(memory) is not None:
                reserve_stage_items(memory, "photos", read_id, 1, at=at, changes=changes)
            else:
                committed = memory.commit(key("product-image-intent", read_id), changes)
                if not committed:
                    raise SafetyError("PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING")
            old = intent
            if not programme_service_state(memory)["enabled"]:
                raise SafetyError("PROGRAMME_OWNER_PAUSED")
            receipt, raw = fetcher(url, track_access=True) if fetcher is fetch_product_image else fetcher(url)
            origin = read_id
        else:
            origin, receipt, raw = adopted
        if (not isinstance(receipt, dict) or receipt.get("state") not in {"usable", "failed"}
                or receipt.get("requested_url") != url):
            raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
        timestamp(receipt.get("checked_at"))
        if receipt["state"] == "usable":
            if receipt.get("dispatch_started") is not True or receipt.get("body_complete") is not True:
                raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
            if inspect_image(raw) != receipt.get("metadata"):
                raise SafetyError("PRODUCT_IMAGE_BYTES_CHANGED")
        elif raw is not None:
            raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
        response = {"kind": SHARED_READ_KIND, "reference": read_id, "source_url": url,
                    "origin_reference": origin, "receipt": deepcopy(receipt),
                    "body_base64": base64.b64encode(raw).decode("ascii") if raw is not None else None}
        atomic_json(response_path, {"value": response, "hash": digest(response)})
        reused = adopted is not None
    else:
        reused = True
    saved = load_json(response_path, 8 * 1024 * 1024)
    response = saved.get("value") if isinstance(saved, dict) else None
    if (not isinstance(response, dict) or set(response) != {"kind", "reference", "source_url", "origin_reference", "receipt", "body_base64"}
            or saved.get("hash") != digest(response) or response.get("kind") != SHARED_READ_KIND
            or response.get("reference") != read_id or response.get("source_url") != url):
        raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
    identifier(response["origin_reference"])
    receipt = response["receipt"]
    if (not isinstance(receipt, dict) or receipt.get("state") not in {"usable", "failed"}
            or receipt.get("requested_url") != url):
        raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
    timestamp(receipt.get("checked_at"))
    if old is None:
        # A new physical read must have its canonical intent. The only valid
        # intent-free bridge is a still-verifiable original legacy success.
        adopted = _legacy_image_response(state, url, media_root)
        if (adopted is None or adopted[0] != response["origin_reference"]
                or adopted[1] != receipt or response["body_base64"] != base64.b64encode(adopted[2]).decode("ascii")):
            raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
    image = None
    if receipt["state"] == "usable":
        try:
            encoded = response["body_base64"]
            if not isinstance(encoded, str):
                raise ValueError
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID") from None
        metadata = inspect_image(raw)
        if metadata != receipt.get("metadata"):
            raise SafetyError("PRODUCT_IMAGE_BYTES_CHANGED")
        path = safe_path(media_destination / (metadata["sha256"] + _FORMATS[metadata["format"]]))
        if path.exists():
            if inspect_image(path.read_bytes()) != metadata:
                raise SafetyError("PRODUCT_IMAGE_SAVED_BYTES_CHANGED")
        else:
            atomic_bytes(path, raw)
        image = {"path": str(path), **metadata, "source_kind": "public_product_image"}
    elif response["body_base64"] is not None:
        raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
    value = {"kind": SHARED_READ_KIND, "reference": read_id, "source_url": url, "state": receipt["state"],
             "at": receipt["checked_at"], "receipt": deepcopy(receipt), "image": image,
             "origin_reference": response["origin_reference"], "response_hash": saved["hash"]}
    value["record_hash"] = digest(value)
    if old is not None and old["state"] != "execution_unknown":
        if old != value:
            raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
    else:
        memory.commit(key("product-image-response", [read_id, saved["hash"]]),
                      [change("source_checks", read_id, value, old)] if old is not None
                      else [change("source_checks", read_id, value)])
    return read_id, value, reused


def _capture_shared_image(memory, concept_id, candidate, *, media_root, fetcher):
    from .research_memory import change
    binding = _candidate(memory.snapshot(), concept_id, candidate)
    capture_id = key("product-image", [SHARED_VERSION, binding])
    with Journal(memory.root / "image_captures").locked(timeout=5):
        read_id, original, reused = _read_shared_image(memory, binding["source_url"], media_root, fetcher)
        value = {"kind": "public_product_image", "version": SHARED_VERSION, "source": "product_photo",
                 "route": "public_image_http", "reference": capture_id, "at": original["at"], **binding,
                 "state": original["state"], "reason": original["receipt"]["reason"],
                 "receipt": deepcopy(original["receipt"]), "image": deepcopy(original["image"]),
                 "acquisition_ref": read_id, "model_calls": 0, "apify_usd": "0"}
        value["record_hash"] = digest(value)
        old = memory.snapshot()["source_checks"].get(capture_id)
        if old is not None:
            if old != value:
                raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
            return dict(deepcopy(old), reused=True)
        def unchanged(current):
            if _candidate(current, concept_id, candidate) != binding:
                raise SafetyError("PRODUCT_IMAGE_PRODUCT_CHANGED")
            if current["source_checks"].get(read_id) != original:
                raise SafetyError("PRODUCT_IMAGE_SHARED_RECORD_INVALID")
        memory.commit(capture_id, [change("source_checks", capture_id, value)], validate=unchanged)
        return dict(deepcopy(value), reused=reused)


def _image_request(url):
    return {"url": public_pages._public_url(url)[0], "transport": "public-http",
            "headers": {**_IMAGE_HEADERS, "User-Agent": "NeuraFlow-Public-Research/1.0"},
            "options": {"max_bytes": MAX_IMAGE_BYTES, "timeout_seconds": 20}}


def _renewable_read_id(acquisition_id):
    return key("product-image-read", [RENEWABLE_READ_KIND, acquisition_id])


def _renewable_read_record(value, read_id, url):
    """Portable exact owner/response proof; this does not assert byte presence."""
    from .research_questions import physical_request_fingerprint
    try:
        binding = value["acquisition_binding"]; response = value["response_proof"]
        acquisition_id = value["physical_acquisition_id"]
        if (value["kind"] != RENEWABLE_READ_KIND or value["reference"] != read_id
                or read_id != _renewable_read_id(acquisition_id) or value["source_url"] != url
                or value["record_hash"] != digest({k: v for k, v in value.items() if k != "record_hash"})
                or value["state"] not in {"usable", "failed"}
                or binding["route"] != "public_image_http" or binding["request"] != _image_request(url)
                or binding["acquisition_id"] != acquisition_id or binding["method"] != "GET"
                or value["acquisition_binding_hash"] != digest(binding)
                or binding["physical_request_fingerprint"] != physical_request_fingerprint(
                    "public_image_http", binding["request"], method="GET", capture_options=binding["capture_options"])
                or type(binding["generation"]) is not int or not 1 <= binding["generation"] <= 3
                or response["kind"] != "source-acquisition-stage-v1" or response["stage_type"] != "response"
                or response["acquisition_id"] != acquisition_id or response["binding_hash"] != digest(binding)
                or response["record_hash"] != digest({k: v for k, v in response.items() if k != "record_hash"})
                or value["original_response_id"] != key("source-acquisition-stage", {
                    k: v for k, v in response.items() if k not in {"record_hash", "recorded_at"}})
                or response["bridge_contract"] != "verified-image-response-v1"
                or response["source_state"] != value["state"] or response["observed_at"] != value["at"]
                or response["fetch_receipt"] != value["receipt"]
                or value["state"] == "usable" and response["body_sha256"] != value["image"]["sha256"]
                or value["receipt"]["checked_at"] != value["at"] or value["receipt"]["state"] != value["state"]
                or value["receipt"]["requested_url"] != url):
            raise SafetyError("PRODUCT_IMAGE_RENEWABLE_RECORD_INVALID")
        identifier(acquisition_id); timestamp(value["at"])
    except (KeyError, TypeError, ValueError):
        raise SafetyError("PRODUCT_IMAGE_RENEWABLE_RECORD_INVALID") from None
    return value


def _legacy_renewable_response(memory, state, url, media_root):
    """Resolve genuine saved older bytes, never reinterpret a lost read as zero."""
    old_id = _shared_read_id(url)
    if old_id in state["source_checks"]:
        _shared_record(state["source_checks"][old_id], old_id, url)
        def no_launch(*args, **kwargs):
            raise SafetyError("PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING")
        _, original, _ = _read_shared_image(memory, url, media_root, no_launch)
        raw = Path(original["image"]["path"]).read_bytes() if original["state"] == "usable" else None
        return old_id, original, original["receipt"], raw
    legacy = _legacy_image_response(state, url, media_root)
    if legacy is not None:
        ref, receipt, raw = legacy
        return ref, state["source_checks"][ref], receipt, raw
    return None


def _renewable_response_path(memory, acquisition_id):
    return safe_path(safe_path(memory.root, "image_captures", "physical", acquisition_id) / "response.json")


def _read_renewable_response(path):
    saved = load_json(path, 8 * 1024 * 1024)
    response = saved.get("value") if isinstance(saved, dict) else None
    if not isinstance(response, dict) or saved.get("hash") != digest(response):
        raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
    raw = None
    if response.get("body_base64") is not None:
        try:
            raw = base64.b64decode(response["body_base64"], validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID") from None
    return response, raw, saved["hash"]


def _image_wait_guard(state, binding, *, acquisition_id, probe_id=None, as_of, due_failure_hash=None):
    from .source_coverage import acquisition_access_scope, source_access_wait
    scope = acquisition_access_scope({"binding": binding}, state=state)
    wait = source_access_wait(state, scope, as_of=as_of)
    if (wait is not None and wait["phase"] == "due" and due_failure_hash is not None
            and wait["failure_hash"] == due_failure_hash):
        return  # the same commit must also install the exact prepared probe CAS
    if wait is not None and (wait["phase"] != "probe_pending" or wait["probe"]["id"] != probe_id
            or wait["probe"]["acquisition_id"] != acquisition_id):
        raise SafetyError(wait["reason"])


def _read_renewable_image(memory, url, media_root, fetcher, as_of):
    from .source_acquisitions import (_owners, _record, _response, claim_source_acquisition,
        mark_source_dispatching, record_image_response, saved_response_or_replacement, source_image_recovery)
    from .research_questions import physical_request_fingerprint
    from .source_coverage import (acquisition_access_scope, source_access_wait,
        marketplace_access_admission, _record_marketplace_access)
    from .programme import programme_service_state
    from .research_memory import change
    fixed_as_of = as_of is not None
    as_of = as_of or utc_now()
    timestamp(as_of)
    request = _image_request(url)
    # Both destinations are checked before even reserving an owner or slot.
    media = safe_path(media_root or MEDIA_ROOT)
    safe_path(memory.root, "image_captures", "physical")
    state = memory.snapshot(); owners = _owners(state, physical_request_fingerprint("public_image_http", request))
    legacy = _legacy_renewable_response(memory, state, url, media_root) if not owners else None
    state = memory.snapshot()
    prior_id, prior = owners[-1] if owners else (None, None)
    prior_path = _renewable_response_path(memory, prior_id) if prior_id else None
    # A late atomic original response is local replay even while the host waits;
    # a saved one a power cut emptied (or already set aside) is not replayed.
    if (prior_path is not None and prior_path.exists() and prior["state"] != "set_aside"
            and not saved_response_or_replacement(state, prior)[1]):
        record_image_response(memory, prior_id, response_path=prior_path, as_of=as_of)
        state = memory.snapshot(); prior = _record(state, prior_id)
        _record_marketplace_access(memory, prior_id, prior["active_response_id"], as_of=as_of)
        state = memory.snapshot(); prior = _record(state, prior_id)
    # A saved read whose files a power cut emptied is no reply: the claim below sets it
    # aside and reserves its one fresh read (``saved_response_or_replacement``).
    response = saved_response_or_replacement(state, prior)[0] if prior is not None else None
    scope = acquisition_access_scope(prior, response) if prior is not None else acquisition_access_scope(
        {"binding": {"route": "public_image_http", "request": request}})
    wait = source_access_wait(state, scope, as_of=as_of)
    recovery = access_recovery = None
    if response and not response["complete"]:
        if wait is not None and wait["phase"] == "due" and wait["acquisition_id"] == prior_id:
            access_recovery = {field: wait[field] for field in
                ("failure_id", "failure_hash", "response_id", "response_hash", "next_retry_at")}
        elif wait is None or wait["phase"] == "due":
            recovery = source_image_recovery(state, prior_id, as_of=as_of)
    use_prior = response is not None and (response["complete"] or recovery is None and access_recovery is None)
    if use_prior:
        acquired = {"acquisition_id": prior_id, "action": "reuse"}
    else:
        if wait is not None and wait["phase"] not in {"due", "probe_pending"}:
            raise SafetyError(wait["reason"])
        # A pending probe can only be recovered by its own reserved owner; a
        # different URL/consumer cannot become another probe for this host.
        if wait and wait["phase"] == "probe_pending" and wait["probe"]["acquisition_id"] != prior_id:
            raise SafetyError(wait["reason"])
        acquired = claim_source_acquisition(memory, "public_image_http", request,
            owner_id=key("public-image-owner", url), as_of=as_of,
            collection_channel="direct_local", incremental_collection_fee="known_zero",
            freshness_seconds=366 * 86400, access_recovery=access_recovery, image_recovery=recovery)
    acquisition_id = acquired["acquisition_id"]
    path = _renewable_response_path(memory, acquisition_id)
    old = _record(memory.snapshot(), acquisition_id)
    reused = True
    if not path.exists():
        if legacy is not None:
            origin, original, receipt, raw = legacy
            original_ref = {"table": "source_checks", "id": origin, "hash": digest(original)}
            intent_id = None
        elif old["state"] == "reserved" and not old["stage_ids"]:
            if not programme_service_state(memory)["enabled"]:
                raise SafetyError("PROGRAMME_OWNER_PAUSED")
            admission = marketplace_access_admission(memory, {"access_scope": scope, "acquisition_id": acquisition_id},
                caller_id=key("public-image-owner", url), as_of=as_of)
            probe_id = admission["context_fields"].get("marketplace_access_probe")
            due_failure_hash = wait["failure_hash"] if wait and wait["phase"] == "due" and admission["changes"] else None
            def guard(current, binding):
                _image_wait_guard(current, binding, acquisition_id=acquisition_id, probe_id=probe_id, as_of=as_of,
                                  due_failure_hash=due_failure_hash)
            dispatched = mark_source_dispatching(memory, acquisition_id, as_of=as_of, source_guard=guard,
                photo_admission=True, joined_changes=admission["changes"])
            if dispatched["action"] != "saved":
                raise SafetyError("PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING")
            current = _record(memory.snapshot(), acquisition_id)
            _image_wait_guard(memory.snapshot(), current["binding"], acquisition_id=acquisition_id,
                              probe_id=probe_id, as_of=as_of)
            if not programme_service_state(memory)["enabled"]:
                raise SafetyError("PROGRAMME_OWNER_PAUSED")
            def redirect_guard(destination):
                from .source_coverage import source_access_scope
                current_wait = source_access_wait(memory.snapshot(), source_access_scope(destination,
                    transport="public-http", access_identity="owned-public-http"), as_of=as_of if fixed_as_of else utc_now())
                if current_wait is None or (current_wait["phase"] == "probe_pending"
                        and current_wait["probe"]["acquisition_id"] == acquisition_id
                        and current_wait["probe"]["id"] == probe_id):
                    return None
                return current_wait
            receipt, raw = (fetcher(url, track_access=True, access_guard=redirect_guard)
                            if fetcher is fetch_product_image else fetcher(url))
            origin, original_ref, intent_id = acquisition_id, None, current["active_dispatch_intent_id"]
            reused = False
        else:
            raise SafetyError("PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING")
        value = {"kind": "public-product-image-response-v1", "reference": acquisition_id, "request": request,
            "origin_reference": origin, "original": original_ref, "dispatch_intent_id": intent_id,
            "receipt": deepcopy(receipt), "body_base64": base64.b64encode(raw).decode("ascii") if raw is not None else None}
        atomic_json(path, {"value": value, "hash": digest(value)})
    as_of = as_of if fixed_as_of else utc_now()
    record_image_response(memory, acquisition_id, response_path=path, as_of=as_of)
    state = memory.snapshot(); owner = _record(state, acquisition_id)
    _record_marketplace_access(memory, acquisition_id, owner["active_response_id"], as_of=as_of)
    state = memory.snapshot(); owner = _record(state, acquisition_id); response = _response(state, owner)
    saved, raw, response_hash = _read_renewable_response(path)
    receipt = saved["receipt"]; image = None
    if receipt["state"] == "usable":
        metadata = inspect_image(raw)
        if metadata != receipt.get("metadata"):
            raise SafetyError("PRODUCT_IMAGE_BYTES_CHANGED")
        destination = safe_path(media / (metadata["sha256"] + _FORMATS[metadata["format"]]))
        if destination.exists():
            if inspect_image(destination.read_bytes()) != metadata:
                raise SafetyError("PRODUCT_IMAGE_SAVED_BYTES_CHANGED")
        else:
            atomic_bytes(destination, raw)
        image = {"path": str(destination), **metadata, "source_kind": "public_product_image"}
    read_id = _renewable_read_id(acquisition_id)
    original = {"kind": RENEWABLE_READ_KIND, "reference": read_id, "source_url": url,
        "state": receipt["state"], "at": receipt["checked_at"], "receipt": deepcopy(receipt), "image": image,
        "origin_reference": saved["origin_reference"], "response_hash": response_hash,
        "physical_acquisition_id": acquisition_id, "acquisition_binding": deepcopy(owner["binding"]),
        "acquisition_binding_hash": owner["binding_hash"], "original_response_id": owner["active_response_id"],
        "response_proof": deepcopy(response)}
    original["record_hash"] = digest(original)
    _renewable_read_record(original, read_id, url)
    existing = state["source_checks"].get(read_id)
    if existing is not None and existing != original:
        raise SafetyError("PRODUCT_IMAGE_RENEWABLE_RECORD_INVALID")
    if existing is None:
        def unchanged(current):
            if current["source_checks"].get(owner["active_response_id"]) != response:
                raise SafetyError("PRODUCT_IMAGE_RENEWABLE_RECORD_INVALID")
        memory.commit(read_id, [change("source_checks", read_id, original)], validate=unchanged)
    return read_id, original, reused


def _capture_renewable_image(memory, concept_id, candidate, *, media_root, fetcher, as_of):
    from .research_memory import change
    binding = _candidate(memory.snapshot(), concept_id, candidate)
    with Journal(memory.root / "image_captures").locked(timeout=5):
        read_id, original, reused = _read_renewable_image(memory, binding["source_url"], media_root, fetcher, as_of)
        acquisition_id = original["physical_acquisition_id"]
        capture_id = key("product-image", [RENEWABLE_VERSION, binding, acquisition_id])
        value = {"kind": "public_product_image", "version": RENEWABLE_VERSION, "source": "product_photo",
            "route": "public_image_http", "reference": capture_id, "at": original["at"], **binding,
            "state": original["state"], "reason": original["receipt"]["reason"], "receipt": deepcopy(original["receipt"]),
            "image": deepcopy(original["image"]), "acquisition_ref": read_id,
            "physical_acquisition_id": acquisition_id, "generation": original["acquisition_binding"]["generation"],
            "next_retry_at": original["response_proof"]["image_failure"]["next_retry_at"],
            "failure_classification": original["response_proof"]["image_failure"]["classification"],
            "model_calls": 0, "apify_usd": "0"}
        if value["state"] == "failed" and value["generation"] >= 3 and value["failure_classification"] == "retryable":
            value.update(failure_classification="retry_limit", next_retry_at=None)
        value["record_hash"] = digest(value)
        existing = memory.snapshot()["source_checks"].get(capture_id)
        if existing is not None:
            if existing != value:
                raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
            return dict(deepcopy(existing), reused=True)
        def unchanged(current):
            if _candidate(current, concept_id, candidate) != binding:
                raise SafetyError("PRODUCT_IMAGE_PRODUCT_CHANGED")
            if current["source_checks"].get(read_id) != original:
                raise SafetyError("PRODUCT_IMAGE_RENEWABLE_RECORD_INVALID")
        memory.commit(capture_id, [change("source_checks", capture_id, value)], validate=unchanged)
        return dict(deepcopy(value), reused=reused)


def _receipt_report_quality(receipt, image, at, url):
    """Trust dimensions only when the saved receipt and image metadata agree."""
    if not isinstance(receipt, dict):
        raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
    if receipt.get("state") != "usable":
        return None
    if (not isinstance(image, dict) or receipt.get("requested_url") != url
            or receipt.get("checked_at") != at
            or receipt.get("metadata") != {field: image.get(field)
                for field in ("sha256", "format", "width", "height", "bytes")}
            or not isinstance(image.get("format"), str) or image["format"] not in _FORMATS
            or not isinstance(image.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", image["sha256"])
            or type(image.get("bytes")) is not int or not 0 < image["bytes"] <= MAX_IMAGE_BYTES):
        raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
    timestamp(at)
    return report_image_quality(image.get("width"), image.get("height"))


def _candidate_report_quality(state, binding, owners):
    """Pure original-metadata hint; actual recovery still validates original bytes."""
    from .source_acquisitions import _stage
    url = binding["source_url"]
    if owners:
        acquisition_id, owner = owners[-1]
        if not owner.get("active_response_id"):
            return None
        response = _stage(state, owner, owner["active_response_id"])
        if (response.get("stage_type") != "response"
                or response.get("bridge_contract") != "verified-image-response-v1"):
            raise SafetyError("PRODUCT_IMAGE_RENEWABLE_RECORD_INVALID")
        receipt = response.get("fetch_receipt")
        if response.get("source_state") != "usable":
            return None
        metadata = receipt.get("metadata") if isinstance(receipt, dict) else None
        if (response.get("complete") is not True or not isinstance(metadata, dict)
                or response.get("body_sha256") != metadata.get("sha256")):
            raise SafetyError("PRODUCT_IMAGE_RENEWABLE_RECORD_INVALID")
        read_id = _renewable_read_id(acquisition_id)
        original = state["source_checks"].get(read_id)
        if original is not None:
            original = _renewable_read_record(original, read_id, url)
            if (original["acquisition_binding"] != owner["binding"]
                    or original["response_proof"] != response
                    or original.get("receipt") != receipt
                    or original.get("image") is None
                    or {field: original["image"].get(field) for field in metadata} != metadata):
                raise SafetyError("PRODUCT_IMAGE_RENEWABLE_RECORD_INVALID")
        return _receipt_report_quality(receipt, metadata, response.get("observed_at"), url)
    old_id = _shared_read_id(url)
    old = state["source_checks"].get(old_id)
    if old is not None:
        original = _shared_record(old, old_id, url)
        if original["state"] != "usable":
            return None
        return _receipt_report_quality(original.get("receipt"), original.get("image"), original["at"], url)
    usable = []
    for ref, row in state["source_checks"].items():
        if (row.get("kind") != "public_product_image" or row.get("version") != VERSION
                or row.get("source_url") != url or row.get("state") != "usable"):
            continue
        original_binding = {field: row.get(field) for field in ("concept_id", "description_hash",
            "observation_id", "source_url", "observed_at", "relationship")}
        if ref != key("product-image", [VERSION, original_binding]) or row.get("reference") != ref:
            raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID")
        quality = _receipt_report_quality(row.get("receipt"), row.get("image"), row.get("at"), url)
        usable.append((timestamp(row["at"]), ref, quality))
    return max(usable, key=lambda row: (row[0], row[1]))[2] if usable else None


def candidate_report_quality(state, concept_id, candidate):
    """False only for a bound, verified saved original below the report minimum.

    None means unknown, not rejection. This never reads the website, restores
    media or reserves an attempt. Missing local bytes do not make a known small
    original worth restoring for a report. No provider URL heuristics are used.
    """
    from .source_acquisitions import _owners
    from .research_questions import physical_request_fingerprint
    binding = _candidate(state, concept_id, candidate)
    owners = _owners(state, physical_request_fingerprint("public_image_http", _image_request(binding["source_url"])))
    return _candidate_report_quality(state, binding, owners)


def product_image_readiness(memory, concept_id, candidate, *, as_of=None, state=None, require_report_quality=False):
    """Small saved-only wake/next-trigger view; never reserve or invoke a read.

    A late original file or missing consumer application is useful local work.
    An unchanged terminal result or uncertain original is not a reason to spin
    new programme turns. Another genuine candidate remains independent work.
    """
    from .source_acquisitions import _owners, _stage
    from .research_questions import physical_request_fingerprint
    from .source_coverage import acquisition_access_scope, source_access_wait
    if type(require_report_quality) is not bool:
        raise SafetyError("PRODUCT_IMAGE_REPORT_QUALITY_OPTION_INVALID")
    at = as_of or utc_now(); timestamp(at)
    state = memory.snapshot() if state is None else state
    binding = _candidate(state, concept_id, candidate)
    request = _image_request(binding["source_url"])
    owners = _owners(state, physical_request_fingerprint("public_image_http", request))
    scope = acquisition_access_scope(owners[-1][1], state=state) if owners else acquisition_access_scope(
        {"binding": {"route": "public_image_http", "request": request}})
    wait = source_access_wait(state, scope, as_of=at)
    result = {"ready": False, "local_saved": False, "next_retry_at": None, "reason": None,
              "signal": {"physical_request": physical_request_fingerprint("public_image_http", request),
                         "wait": wait}}
    if require_report_quality:
        quality = _candidate_report_quality(state, binding, owners)
        result["signal"]["report_quality"] = quality
        if quality is False:
            result["reason"] = "PRODUCT_IMAGE_REPORT_PHOTO_UNUSABLE"
            return result
    if not owners:
        old_id = _shared_read_id(binding["source_url"])
        old = state["source_checks"].get(old_id)
        if old is not None:
            _shared_record(old, old_id, binding["source_url"])
            if require_report_quality and old["state"] == "failed":
                result["reason"] = "PRODUCT_IMAGE_TERMINAL_FAILURE"
                return result
            path = safe_path(safe_path(memory.root, "image_captures", "shared", old_id) / "response.json")
            result["ready"] = result["local_saved"] = path.is_file()
            result["reason"] = None if path.is_file() else "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING"
            return result
        legacy = [row for row in state["source_checks"].values() if row.get("kind") == "public_product_image"
                  and row.get("version") == VERSION and row.get("source_url") == binding["source_url"]]
        if legacy:
            if require_report_quality:
                originals = []
                for row in legacy:
                    if row.get("state") != "usable":
                        continue
                    try:
                        original_path = safe_path(row["image"]["path"])
                    except (KeyError, TypeError):
                        raise SafetyError("PRODUCT_IMAGE_CAPTURE_INVALID") from None
                    try:
                        stat = original_path.stat() if original_path.is_file() else None
                    except OSError:
                        stat = None
                    originals.append([row["reference"],
                        [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns] if stat else None])
                result["signal"]["legacy_original_files"] = originals
                # Unlike v2/v3, a v1 consumer has no durable response body to
                # restore. Adoption validates every matching usable original;
                # missing one cannot be repaired by repeatedly invoking it.
                # Stat only here; execution retains the full byte/hash checks.
                if any(stat is None for _, stat in originals):
                    result["reason"] = "PRODUCT_IMAGE_SAVED_BYTES_CHANGED"
                    return result
            result["ready"] = result["local_saved"] = any(row.get("state") == "usable" for row in legacy)
            result["reason"] = None if result["ready"] else "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING"
            return result
        result["ready"] = True
    else:
        acquisition_id, owner = owners[-1]
        path = _renewable_response_path(memory, acquisition_id)
        stat = path.stat() if path.is_file() else None
        result["signal"].update(acquisition_id=acquisition_id, state=owner["state"],
            original_file=[stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns] if stat else None,
            response_id=owner["active_response_id"])
        capture_id = key("product-image", [RENEWABLE_VERSION, binding, acquisition_id])
        if stat and capture_id not in state["source_checks"]:
            result.update(ready=True, local_saved=True)
            return result
        if owner["state"] == "reserved" and not owner["stage_ids"]:
            result["ready"] = True
        elif owner["state"] == "set_aside":
            # Its saved files were lost (a power cut): one fresh read may replace it, not a lost replacement.
            from .source_acquisitions import damage_replacement_allowed
            if not damage_replacement_allowed(owner):
                result["reason"] = "SOURCE_ACQUISITION_DAMAGE_REPEATED"
                return result
            result["ready"] = True
        elif not owner["active_response_id"]:
            result["reason"] = "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING"
            return result
        else:
            response = _stage(state, owner, owner["active_response_id"])
            policy = response.get("image_failure", {})
            if response.get("complete"):
                # Missing shared bytes are recoverable from the same original,
                # without changing the semantic/photo acquisition identity.
                saved = state["source_checks"].get(capture_id, {})
                image_path = saved.get("image", {}).get("path") if isinstance(saved.get("image"), dict) else None
                if image_path and not safe_path(image_path).is_file() and stat:
                    result.update(ready=True, local_saved=True)
                return result
            if owner["binding"]["generation"] >= 3 or policy.get("classification") != "retryable":
                result["reason"] = "PRODUCT_IMAGE_RETRY_LIMIT" if owner["binding"]["generation"] >= 3 else "PRODUCT_IMAGE_ORIGINAL_RESPONSE_PENDING" if policy.get("classification") == "original_pending" else "PRODUCT_IMAGE_TERMINAL_FAILURE"
                return result
            due = policy["next_retry_at"]
            result.update(ready=timestamp(at) >= timestamp(due), next_retry_at=due, reason="PRODUCT_IMAGE_RETRY_WAIT")
    if wait is not None:
        own_id = owners[-1][0] if owners else None
        permitted_probe = wait["phase"] == "due" or wait["phase"] == "probe_pending" and wait["probe"]["acquisition_id"] == own_id
        if not permitted_probe:
            result.update(ready=False, next_retry_at=wait["next_retry_at"] if wait["phase"] == "waiting" else None,
                          reason=wait["reason"])
    return result


def _verify_saved_image(value, media_root):
    try:
        root = safe_path(media_root or MEDIA_ROOT).resolve()
        image = value["image"]
        path = safe_path(image["path"])
        if not path.resolve().is_relative_to(root) or not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            raise ValueError
        if inspect_image(path.read_bytes()) != {field: image[field] for field in ("sha256", "format", "width", "height", "bytes")}:
            raise ValueError
    except (SafetyError, OSError, KeyError, TypeError, ValueError):
        raise SafetyError("PRODUCT_IMAGE_SAVED_BYTES_CHANGED") from None


def current_public_image(state, concept_id, candidates, *, media_root=None, as_of=None, verify_bytes=True,
                         require_report_quality=False):
    """Read one applicable photo; metadata-only recovery cannot prove image bytes.

    The opt-in metadata branch is only for matching an existing revision-owned
    display copy after shared source loss. Rendering still checks owned bytes.
    Ordinary collection/selection callers retain the byte-verified default.
    require_report_quality is a separate opt-in minimum size check. It never
    changes original successful download records or certifies product identity.
    """
    if type(verify_bytes) is not bool:
        raise SafetyError("PRODUCT_IMAGE_VERIFY_OPTION_INVALID")
    if type(require_report_quality) is not bool:
        raise SafetyError("PRODUCT_IMAGE_REPORT_QUALITY_OPTION_INVALID")
    root = safe_path(media_root or MEDIA_ROOT)
    cutoff = timestamp(as_of) if as_of is not None else None
    allowed = []
    for candidate in candidates:
        try:
            binding = _candidate(state, concept_id, candidate)
            for ref, renewable in state["source_checks"].items():
                if (renewable.get("kind") != "public_product_image" or renewable.get("version") != RENEWABLE_VERSION
                        or renewable.get("state") != "usable"
                        or any(renewable.get(field) != expected for field, expected in binding.items())):
                    continue
                try:
                    original = _renewable_read_record(state["source_checks"].get(renewable["acquisition_ref"]),
                        renewable["acquisition_ref"], binding["source_url"])
                    if (ref != key("product-image", [RENEWABLE_VERSION, binding, original["physical_acquisition_id"]])
                            or renewable.get("reference") != ref
                            or renewable.get("record_hash") != digest({k: v for k, v in renewable.items() if k != "record_hash"})
                            or renewable.get("physical_acquisition_id") != original["physical_acquisition_id"]
                            or renewable.get("generation") != original["acquisition_binding"]["generation"]
                            or renewable.get("image") != original["image"] or renewable.get("receipt") != original["receipt"]
                            or renewable.get("at") != original["at"] or renewable.get("model_calls") != 0
                            or renewable.get("apify_usd") != "0"
                            or cutoff is not None and (timestamp(renewable["at"]) > cutoff or timestamp(binding["observed_at"]) > cutoff)):
                        continue
                    path = safe_path(renewable["image"]["path"])
                    if not path.is_relative_to(root):
                        continue
                    if verify_bytes:
                        _verify_saved_image(renewable, media_root)
                    if require_report_quality and not report_image_quality(renewable["image"].get("width"), renewable["image"].get("height")):
                        continue
                    allowed.append((binding["relationship"] != "exact_product", timestamp(binding["observed_at"]), ref, renewable))
                except (SafetyError, OSError, KeyError, TypeError):
                    continue
            shared_id = key("product-image", [SHARED_VERSION, binding])
            capture_id = shared_id if shared_id in state["source_checks"] else key("product-image", [VERSION, binding])
            record = state["source_checks"].get(capture_id)
            if not record or record.get("state") != "usable" or record.get("version") not in {VERSION, SHARED_VERSION}:
                continue
            if not verify_bytes and (record.get("kind") != "public_product_image"
                    or record.get("reference") != capture_id
                    or any(record.get(field) != expected for field, expected in binding.items())):
                continue
            if record["version"] == SHARED_VERSION:
                read_id = _shared_read_id(binding["source_url"])
                original = _shared_record(state["source_checks"].get(read_id), read_id, binding["source_url"])
                if (record.get("kind") != "public_product_image" or record.get("source") != "product_photo"
                        or record.get("route") != "public_image_http"
                        or record.get("model_calls") != 0 or record.get("apify_usd") != "0"
                        or record.get("record_hash") != digest({k: v for k, v in record.items() if k != "record_hash"})
                        or record.get("acquisition_ref") != read_id or original["state"] != "usable"
                        or record.get("image") != original["image"] or record.get("receipt") != original["receipt"]
                        or record.get("at") != original["at"] or record.get("reference") != capture_id
                        or any(record.get(field) != expected for field, expected in binding.items())):
                    continue
            if cutoff is not None and (timestamp(record["at"]) > cutoff or timestamp(binding["observed_at"]) > cutoff):
                continue
            photo = record["image"]
            path = safe_path(photo["path"])
            if (not path.is_relative_to(root) or photo.get("format") not in _FORMATS
                    or any(type(photo.get(field)) is not int or photo[field] <= 0 for field in ("width", "height", "bytes"))
                    or photo["bytes"] > MAX_IMAGE_BYTES
                    or not isinstance(photo.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", photo["sha256"])):
                continue
            if verify_bytes and (not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES
                    or inspect_image(path.read_bytes()) != {field: photo[field] for field in ("sha256", "format", "width", "height", "bytes")}):
                continue
            if require_report_quality and not report_image_quality(photo["width"], photo["height"]):
                continue
            allowed.append((binding["relationship"] != "exact_product", timestamp(binding["observed_at"]),
                            capture_id, record))
        except (SafetyError, OSError, KeyError, TypeError):
            continue
    if not allowed:
        return None
    _, _, capture_id, record = min(allowed, key=lambda row: (row[0], -row[1].timestamp(), row[2]))
    return {"id": capture_id, "owner_id": concept_id, "path": record["image"]["path"],
            "sha256": record["image"]["sha256"], "source_kind": "public_product_image",
            "source_ref": record["observation_id"], "source_url": record["source_url"],
            "observed_at": record["observed_at"], "relationship": record["relationship"]}
