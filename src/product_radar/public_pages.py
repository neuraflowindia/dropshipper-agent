"""Bounded retrieval of already-discovered public supplier URLs.

No search, cookies, credentials, proxies, contact, interpretation or memory write.
Connections are pinned to a checked numeric destination; HTTPS still verifies
the original hostname. Receipt text is source material, not a stock/fulfilment
claim. Python transport references: docs.python.org/3/library/ssl.html and
docs.python.org/3/library/http.client.html.
"""

from hashlib import sha256
from html.parser import HTMLParser
import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from uuid import uuid4
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit
import zlib

from .models import SafetyError, identifier, utc_now
from .source_deadline import SourceActionDeadline, source_phase

MAX_HTML = 1024 * 1024
MAX_TEXT = 24000
MAX_REDIRECTS = 3
MAX_SECONDS = 20
_SLOTS = threading.BoundedSemaphore(2)
_SECRET_FIELDS = {"token", "access_token", "api_token", "api_key", "apikey", "password", "secret",
                  "client_secret", "session_token", "authorization"}


def _public_url(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise SafetyError("PUBLIC_URL_INVALID")
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise SafetyError("PUBLIC_URL_INVALID")
    # Encoded line breaks must not enter either the request target or a log.
    if re.search(r"[\x00-\x1f\x7f]", unquote(value)):
        raise SafetyError("PUBLIC_URL_INVALID")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or "%" in parsed.hostname
                or _SECRET_FIELDS & {name.casefold() for name in parse_qs(parsed.query, keep_blank_values=True)}):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        netloc = f"[{host}]" if ":" in host else host
        if parsed.port is not None:
            netloc += f":{port}"
        clean = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
        clean.encode("ascii")  # Discovered Unicode paths must be properly URL-encoded by the source adapter.
        return clean, host, port, parsed.scheme
    except (ValueError, UnicodeError):
        raise SafetyError("PUBLIC_URL_INVALID") from None


def _checked_addresses(host, port, resolver):
    answers = resolver(host, port, type=socket.SOCK_STREAM)
    if not answers or len(answers) > 64:
        raise SafetyError("PUBLIC_DNS_UNAVAILABLE")
    checked = []
    for family, kind, protocol, _, address in answers:
        if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM:
            continue
        try:
            ip = ipaddress.ip_address(address[0])
        except ValueError:
            raise SafetyError("PUBLIC_DESTINATION_REJECTED") from None
        effective = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        embedded = []
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.sixtofour:
                embedded.append(ip.sixtofour)
            if ip.teredo:
                embedded.extend(ip.teredo)
            if ip in ipaddress.ip_network("64:ff9b::/96"):
                embedded.append(ipaddress.IPv4Address(int(ip) & 0xffffffff))
        if (not effective.is_global or effective.is_multicast or effective.is_reserved
                or effective.is_loopback or effective.is_link_local or effective.is_unspecified
                or any(not address.is_global or address.is_multicast or address.is_reserved for address in embedded)
                or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)):
            raise SafetyError("PUBLIC_DESTINATION_REJECTED")
        # Never connect to a resolver-returned alternate port or scope identifier.
        destination = (str(ip), port) if family == socket.AF_INET else (str(ip), port, 0, 0)
        checked.append((family, protocol, destination))
    if not checked:
        raise SafetyError("PUBLIC_DNS_UNAVAILABLE")
    return checked


class _Control:
    def __init__(self, deadline, on_dispatch=None):
        self.deadline, self.cancelled = deadline, threading.Event()
        self.lock, self.sock = threading.Lock(), None
        self.on_dispatch = on_dispatch
        self.execution = {"execution_stage": "not_dispatched", "attempt_started_at": None, "execution_ref": None}

    def start_attempt(self):
        """Owned worker admission precedes a real bounded DNS/network attempt."""
        with self.lock:
            if self.cancelled.is_set() or time.monotonic() >= self.deadline:
                raise SafetyError("PUBLIC_FETCH_DEADLINE")
            if self.on_dispatch is not None:
                self.on_dispatch()
            self.execution = {"execution_stage": "attempt_started", "attempt_started_at": utc_now(),
                              "execution_ref": "public-fetch-" + uuid4().hex}
            return dict(self.execution)

    def response_received(self):
        with self.lock:
            self.execution["execution_stage"] = "response_received"
            return dict(self.execution)

    def execution_snapshot(self):
        with self.lock:
            return dict(self.execution)

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise SafetyError("PUBLIC_FETCH_DEADLINE")
        return remaining

    def register(self, sock):
        with self.lock:
            self.sock = sock
            if self.cancelled.is_set():
                sock.close()
                raise SafetyError("PUBLIC_FETCH_DEADLINE")

    def abort(self, *, wait=True):
        self.cancelled.set()
        # Owner interruption must not wait for the dispatch callback's local
        # write. That callback holds this lock before DNS; remaining() prevents
        # its worker from starting transport after cancellation is set.
        if not self.lock.acquire(blocking=wait):
            return
        try:
            if self.sock is not None:
                try:
                    try:
                        self.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                finally:
                    self.sock.close()
        finally:
            self.lock.release()


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, scheme, address, control):
        super().__init__(host, port, timeout=control.remaining())
        self.scheme, self.address, self.control = scheme, address, control

    def connect(self):
        family, protocol, destination = self.address
        raw = socket.socket(family, socket.SOCK_STREAM, protocol)
        self.sock = raw
        self.control.register(raw)
        raw.settimeout(self.control.remaining())
        raw.connect(destination)  # Numeric address: no second DNS lookup/rebinding.
        if self.scheme == "https":
            context = ssl.create_default_context()
            # Do not honour SSLKEYLOGFILE from the caller's environment.
            context.keylog_filename = None
            secure = context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self.sock = secure
            self.control.register(secure)
            secure.settimeout(self.control.remaining())
            secure.do_handshake()


class _PageText(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BREAK = {"p", "br", "div", "section", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "header", "footer",
             "a", "button", "input", "select", "option", "textarea"}

    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.skip, self.parts, self.title, self.in_title, self.links = url, [], [], [], False, []
        self.span_boundaries = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip.append(tag)
        if self.skip:
            return
        if tag == "title":
            self.in_title = True
        attributes = dict(attrs)
        span_boundary = tag == "span" and ("onclick" in attributes or attributes.get("role") in ("button", "link"))
        if tag == "span":
            self.span_boundaries.append(span_boundary)
        if tag in self.BREAK or span_boundary:
            self.parts.append("\n")
        if tag == "a" and len(self.links) < 60:
            href = attributes.get("href")
            if href:
                try:
                    found = _public_url(urljoin(self.url, href))[0]
                    if found not in self.links:
                        self.links.append(found)
                except SafetyError:
                    pass

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag == "title":
            self.in_title = False
        span_boundary = self.span_boundaries.pop() if tag == "span" and self.span_boundaries else False
        if tag in self.BREAK or span_boundary:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            (self.title if self.in_title else self.parts).append(data)

    def extracted(self):
        text = "\n".join(" ".join(line.split()) for line in "".join(self.parts).splitlines() if line.strip())
        return {"title": " ".join("".join(self.title).split())[:2000], "text": text[:MAX_TEXT],
                "text_truncated": len(text) > MAX_TEXT, "discovered_links": self.links}


def _body(response, active_socket, control, receipt):
    content_length = response.getheader("Content-Length")
    if content_length is not None:
        if not re.fullmatch(r"\d+", content_length.strip()):
            raise SafetyError("PUBLIC_HEADERS_INVALID")
        if int(content_length) > MAX_HTML:
            raise SafetyError("PUBLIC_HTML_TOO_LARGE")
    encoding = (response.getheader("Content-Encoding") or "identity").casefold().strip()
    if encoding not in ("identity", "gzip", "x-gzip", "deflate"):
        raise SafetyError("PUBLIC_ENCODING_UNSUPPORTED")
    decoder = zlib.decompressobj(31 if encoding in ("gzip", "x-gzip") else zlib.MAX_WBITS) if encoding != "identity" else None
    chunks, received, expanded = [], 0, 0
    wire_hash = sha256()
    # HTTPResponse owns the socket's retained file after Connection: close.
    # Its final Content-Length read closes that file and the socket descriptor;
    # check completion before setting another timeout on the old socket handle.
    while not response.isclosed():
        active_socket.settimeout(control.remaining())
        block = response.read1(min(16384, MAX_HTML + 1 - received))
        if not block:
            break
        received += len(block)
        wire_hash.update(block)
        receipt.update(received_bytes=received, wire_sha256=wire_hash.hexdigest())
        if received > MAX_HTML:
            raise SafetyError("PUBLIC_HTML_TOO_LARGE")
        piece = decoder.decompress(block, MAX_HTML + 1 - expanded) if decoder else block
        expanded += len(piece)
        receipt["html_bytes"] = expanded
        if expanded > MAX_HTML or (decoder and decoder.unconsumed_tail):
            raise SafetyError("PUBLIC_HTML_TOO_LARGE")
        chunks.append(piece)
    # read1() can close its file on premature EOF without raising IncompleteRead.
    # Content-Length describes wire bytes, not decompressed HTML. Chunked
    # framing owns its boundary instead and HTTPResponse validates its ending.
    if content_length is not None and not getattr(response, "chunked", False) and received != int(content_length):
        raise SafetyError("PUBLIC_BODY_INCOMPLETE")
    if decoder:
        tail = decoder.flush(MAX_HTML + 1 - expanded)
        if expanded + len(tail) > MAX_HTML:
            raise SafetyError("PUBLIC_HTML_TOO_LARGE")
        if not decoder.eof or decoder.unused_data:
            raise SafetyError("PUBLIC_ENCODING_INVALID")
        chunks.append(tail)
    control.remaining()
    receipt["body_complete"] = True
    return b"".join(chunks), received, wire_hash.hexdigest()


def _retrieve(item, receipt, control, resolver, connection_factory):
    url, seen = receipt["requested_url"], set()
    for hop in range(MAX_REDIRECTS + 1):
        control.remaining()
        url, host, port, scheme = _public_url(url)
        if url in seen:
            raise SafetyError("PUBLIC_REDIRECT_LOOP")
        seen.add(url)
        addresses = _checked_addresses(host, port, resolver)
        control.remaining()
        connection = connection_factory(host, port, scheme, addresses[0], control)
        response = None
        try:
            parsed = urlsplit(url)
            target = parsed.path + ("?" + parsed.query if parsed.query else "")
            connection.request("GET", target, headers={"User-Agent": "NeuraFlow-Research/1.0", "Accept": "text/html,application/xhtml+xml",
                                                        "Accept-Encoding": "identity,gzip", "Connection": "close"})
            connection.sock.settimeout(control.remaining())
            active_socket = connection.sock
            response = connection.getresponse()
            receipt.update(control.response_received())
            receipt["final_url"], receipt["http_status"] = url, response.status
            # Retain only this bounded nonsecret response header, not the
            # whole header set. Redirect headers cannot describe the next
            # host's response or accidentally extend its wait.
            retry_after = response.getheader("Retry-After")
            receipt.pop("retry_after", None)
            if (isinstance(retry_after, str) and len(retry_after) <= 256
                    and not any(ord(c) < 32 or ord(c) == 127 for c in retry_after)):
                receipt["retry_after"] = retry_after
            if response.status in (301, 302, 303, 307, 308):
                if hop == MAX_REDIRECTS:
                    raise SafetyError("PUBLIC_REDIRECT_LIMIT")
                location = response.getheader("Location")
                if not location:
                    raise SafetyError("PUBLIC_REDIRECT_INVALID")
                next_url = _public_url(urljoin(url, location))[0]
                receipt["redirects"].append({"from": url, "to": next_url, "status": response.status})
                url = next_url
                continue
            if response.status != 200:
                raise SafetyError("PUBLIC_HTTP_UNAVAILABLE")
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].casefold().strip()
            if content_type not in ("text/html", "application/xhtml+xml"):
                raise SafetyError("PUBLIC_CONTENT_TYPE_UNSUPPORTED")
            body, received, wire_hash = _body(response, active_socket, control, receipt)
            charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", response.getheader("Content-Type") or "", re.I)
            charset_name = charset[1] if charset else "utf-8"
            try:
                html = body.decode(charset_name, errors="replace")
            except LookupError:
                raise SafetyError("PUBLIC_CHARSET_UNSUPPORTED") from None
            parser = _PageText(url)
            parser.feed(html); parser.close()
            extracted = parser.extracted()
            receipt.update(extracted, html=html, received_bytes=received, html_bytes=len(body),
                           charset=charset_name, decode_replacements="\ufffd" in html,
                           wire_sha256=wire_hash, html_sha256=sha256(body).hexdigest(),
                           text_sha256=sha256(extracted["text"].encode()).hexdigest())
            control.remaining()
            if not extracted["text"]:
                raise SafetyError("PUBLIC_TEXT_UNAVAILABLE")
            receipt.update(state="usable", reason="Accessible public HTML; supplier interpretation remains separate.")
            return receipt
        finally:
            if response is not None:
                response.close()
            connection.close()


@source_phase("transport")
def fetch_public_page(item, *, timeout=MAX_SECONDS, resolver=None, connection_factory=None, on_dispatch=None,
                      retain_transport=False):
    """Fetch one explicit discovered URL and return a persistable receipt.

    The caller saves this receipt and passes only id/url/title/text to role F.
    No request occurs for invalid URLs. Injection arguments are trusted local
    test seams, never accepted from source text. A fixed two-worker bound also
    contains a DNS resolver which does not return after its caller's deadline.
    """
    receipt = {"schema_version": 2, "id": None, "state": "rejected", "reason": "PUBLIC_INPUT_INVALID",
               "requested_url": None, "final_url": None, "checked_at": utc_now(), "http_status": None,
               "redirects": [], "received_bytes": 0, "html_bytes": 0, "title": "", "text": "",
               "body_complete": False,
               "text_truncated": False, "discovered_links": [], "html": None,
               "charset": None, "decode_replacements": False,
               "wire_sha256": None, "html_sha256": None, "text_sha256": None,
               "execution_stage": "not_dispatched", "attempt_started_at": None, "execution_ref": None}
    try:
        if (not isinstance(item, dict) or not 0 < timeout <= MAX_SECONDS or isinstance(timeout, bool)
                or (resolver is not None and not callable(resolver))
                or (connection_factory is not None and not callable(connection_factory))
                or (on_dispatch is not None and not callable(on_dispatch)) or type(retain_transport) is not bool):
            raise SafetyError("PUBLIC_INPUT_INVALID")
        receipt["id"] = identifier(item["id"])
        receipt["requested_url"] = _public_url(item["url"])[0]
    except (KeyError, TypeError, SafetyError) as error:
        receipt["reason"] = str(error) if isinstance(error, SafetyError) else "PUBLIC_INPUT_INVALID"
        return receipt
    # Construct local bookkeeping before owning a scarce reader slot.
    control, output = _Control(time.monotonic() + timeout, on_dispatch), queue.Queue(maxsize=1)
    slot_lock, slot_owner = threading.Lock(), "unadmitted"
    slots = _SLOTS

    def release_unstarted_slot():
        nonlocal slot_owner
        # If this short gate is busy, the worker is claiming or releasing the
        # slot itself. Never wait for it or free capacity while it still reads.
        if not slot_lock.acquire(blocking=False):
            return
        try:
            if slot_owner == "caller":
                slot_owner = "released"
                slots.release()
        finally:
            slot_lock.release()

    def cancel_owned_reader():
        # Preserve the owner's original signal even if socket cleanup fails.
        # No returned body, failed request, or worker-exit proof is fabricated.
        try:
            control.abort(wait=False)
        except BaseException:
            pass
        try:
            release_unstarted_slot()
        except BaseException:
            pass

    def work():
        nonlocal slot_owner
        with slot_lock:
            if slot_owner != "caller":
                return  # A delayed start after owner cancellation owns no slot.
            slot_owner = "worker"
        result = dict(receipt, redirects=[])
        try:
            result.update(control.start_attempt())
            result = _retrieve(item, result, control, resolver or socket.getaddrinfo, connection_factory or _PinnedConnection)
        except SafetyError as error:
            result.update(state="rejected" if str(error) in {"PUBLIC_DESTINATION_REJECTED", "PUBLIC_URL_INVALID"} else "unavailable", reason=str(error))
        except (OSError, http.client.HTTPException, zlib.error, ValueError, UnicodeError):
            result.update(state="unavailable", reason="PUBLIC_TRANSPORT_UNAVAILABLE")
        except Exception:
            # Never expose arbitrary injected/server exception strings.
            result.update(state="unavailable", reason="PUBLIC_FETCH_INTERNAL_ERROR")
        finally:
            with slot_lock:
                slot_owner = "released"
                slots.release()
        result.update(control.execution_snapshot(), checked_at=utc_now())
        output.put(result)
    try:
        # Install the owner cleanup before admission. Helper definitions above
        # cannot leave an admitted slot outside the protected lifecycle.
        slot_owner = "caller" if slots.acquire(blocking=False) else "released"
        if slot_owner == "released":
            return dict(receipt, state="unavailable", reason="PUBLIC_FETCH_WORKERS_BUSY")
        try:
            worker = threading.Thread(target=work, name="neuraflow-public-page", daemon=True)
            worker.start()
        except SourceActionDeadline as error:
            control.abort()
            release_unstarted_slot()
            error.execution_receipt = dict(receipt, **control.execution_snapshot(),
                state="unavailable", reason="SOURCE_ACTION_TIMEOUT", checked_at=utc_now())
            raise
        except RuntimeError:
            cancel_owned_reader()
            return dict(receipt, state="unavailable", reason="PUBLIC_FETCH_WORKERS_BUSY")
        except Exception:
            cancel_owned_reader()
            raise
        try:
            result = output.get(timeout=control.remaining())
            if retain_transport:
                # A returned reply is not proof that its owned worker has exited.
                # Only the optional shared-owner path adds this actual join fact.
                worker.join(timeout=max(0, control.deadline - time.monotonic()))
                result.update(transport_contract="public-page-owned-http-v1", owned_worker_exited=not worker.is_alive())
            return result
        except SourceActionDeadline as error:
            # The outer source callback is over, not a provider retry. Abort the
            # owned reader so it cannot connect/follow a redirect after this return.
            control.abort()
            release_unstarted_slot()
            execution = control.execution_snapshot()
            # A response may have arrived inside the worker, but no returned HTTP
            # status/body is available here. Retain its actual start, not a result.
            if execution["execution_stage"] == "response_received":
                execution["execution_stage"] = "attempt_started"
            error.execution_receipt = dict(receipt, **execution, state="unavailable",
                reason="SOURCE_ACTION_TIMEOUT", checked_at=utc_now())
            raise
        except (queue.Empty, SafetyError):
            control.abort()
            release_unstarted_slot()
            return dict(receipt, **control.execution_snapshot(), state="unavailable", reason="PUBLIC_FETCH_DEADLINE",
                        checked_at=utc_now(), received_bytes=None, html_bytes=None)
        except Exception:
            cancel_owned_reader()
            raise
    except (KeyboardInterrupt, SystemExit):
        cancel_owned_reader()
        raise
