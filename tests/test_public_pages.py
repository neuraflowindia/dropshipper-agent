from hashlib import sha256
import gzip
import http.client
import io
from pathlib import Path
import socket
import ssl
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar import public_pages
from product_radar.public_pages import MAX_HTML, _Control, _PinnedConnection, fetch_public_page
from product_radar.suppliers import _check_citations, validate_supplier_input

PUBLIC_IP = "93.184.216.34"
HTML = b'<html><title>Supplier terms</title><p>One piece can be ordered.</p><p>Dispatch is not stated.</p></html>'


def resolver(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]


class Sock:
    def __init__(self):
        self.timeouts, self.closed, self.destination = [], False, None
    def settimeout(self, value):
        self.timeouts.append(value)
    def connect(self, destination):
        self.destination = destination
    def shutdown(self, *_):
        pass
    def close(self):
        self.closed = True


class Response:
    def __init__(self, body=HTML, status=200, headers=None, reader=None):
        self.body, self.status, self.offset = body, status, 0
        self.headers = {key.casefold(): value for key, value in (headers or {"Content-Type": "text/html; charset=utf-8"}).items()}
        self.reader, self.closed = reader, False
    def getheader(self, key):
        return self.headers.get(key.casefold())
    def read1(self, size):
        if self.reader:
            return self.reader(size)
        block = self.body[self.offset:self.offset + size]
        self.offset += len(block)
        return block
    def close(self):
        self.closed = True
    def isclosed(self):
        return self.closed


class Transport:
    def __init__(self, responses, detach=False):
        self.responses, self.connections, self.requests = list(responses), [], []
        self.detach = detach
    def __call__(self, host, port, scheme, address, control):
        outer = self
        response = self.responses.pop(0)
        class Connection:
            def __init__(self):
                self.sock = Sock()
                self.closed = False
            def request(self, method, target, headers):
                outer.requests.append({"host": host, "port": port, "scheme": scheme, "address": address,
                                       "method": method, "target": target, "headers": headers})
            def getresponse(self):
                if outer.detach:
                    self.sock = None  # http.client does this for Connection: close.
                return response
            def close(self):
                self.closed = True
        connection = Connection()
        self.connections.append(connection)
        return connection


class NativeResponseTransport:
    """Actual HTTPResponse lifecycle with socket-file ownership simulated offline."""
    def __init__(self, body=HTML, headers=None):
        self.body = body
        self.headers = {"Content-Type": "text/html; charset=utf-8",
                        "Content-Length": str(len(body)), "Connection": "close", **(headers or {})}
        self.connections, self.sockets = [], []

    def __call__(self, host, port, scheme, address, control):
        wire = ("HTTP/1.1 200 OK\r\n" + "".join(f"{key}: {value}\r\n" for key, value in self.headers.items() if value is not None)
                + "\r\n").encode("ascii") + self.body
        class RetainedFile(io.BytesIO):
            def close(stream):
                if not stream.closed:
                    client.references -= 1
                    if client.closed and not client.references:
                        client.descriptor_closed = True
                super().close()
        class ResponseSocket(Sock):
            def __init__(sock):
                super().__init__()
                sock.references, sock.descriptor_closed = 0, False
            def makefile(sock, _mode):
                sock.references += 1
                return RetainedFile(wire)
            def sendall(sock, _data):
                pass  # The standard library's outgoing request stays offline.
            def settimeout(sock, value):
                if sock.descriptor_closed:
                    raise OSError(9, "Synthetic closed descriptor")
                super().settimeout(value)
            def close(sock):
                sock.closed = True
                if not sock.references:
                    sock.descriptor_closed = True
            def fileno(sock):
                return -1 if sock.descriptor_closed else 10
        client = ResponseSocket()
        # The standard library request/getresponse path creates a SocketIO
        # reader in real use. The retained file above models that ownership;
        # HTTPResponse itself performs its genuine close at Content-Length EOF.
        connection = http.client.HTTPConnection(host, port, timeout=control.remaining())
        connection.sock = client
        control.register(client)
        self.connections.append(connection)
        self.sockets.append(client)
        return connection

    def close(self):
        for connection in self.connections:
            connection.close()
        for client in self.sockets:
            client.close()


def fetch(transport, url="https://supplier.example.org/product", **kwargs):
    return fetch_public_page({"id": "source-one", "url": url}, resolver=kwargs.pop("resolver", resolver),
                             connection_factory=transport, **kwargs)


class PublicPageTests(unittest.TestCase):
    def test_opt_in_owned_transport_proof_records_actual_finished_worker_only(self):
        old = fetch(Transport([Response()]))
        self.assertNotIn("transport_contract", old); self.assertNotIn("owned_worker_exited", old)
        shared = fetch(Transport([Response()]), retain_transport=True)
        self.assertEqual(shared["transport_contract"], "public-page-owned-http-v1")
        self.assertTrue(shared["owned_worker_exited"])

    def test_dispatch_notification_fires_after_worker_admission_before_dns(self):
        events = []
        def observed_resolver(*args, **kwargs):
            events.append("dns")
            return resolver(*args, **kwargs)
        result = fetch(Transport([Response()]), resolver=observed_resolver,
                       on_dispatch=lambda: events.append("dispatch"))
        self.assertEqual(result["state"], "usable")
        self.assertEqual(events[:2], ["dispatch", "dns"])
        self.assertEqual(events.count("dispatch"), 1)

    def test_dispatch_notification_is_not_sent_for_invalid_or_unadmitted_read(self):
        events = []
        rejected = fetch_public_page({"id": "../invalid", "url": "https://supplier.example.org/"},
                                     on_dispatch=lambda: events.append("dispatch"))
        with patch.object(public_pages, "_SLOTS", threading.BoundedSemaphore(0)):
            busy = fetch(Transport([]), on_dispatch=lambda: events.append("dispatch"))
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(busy["reason"], "PUBLIC_FETCH_WORKERS_BUSY")
        self.assertEqual(events, [])

    def test_source_receipt_has_actual_html_text_hashes_and_f_compatible_page(self):
        transport = Transport([Response()])
        result = fetch(transport)
        self.assertEqual(result["state"], "usable")
        self.assertEqual(result["title"], "Supplier terms")
        self.assertIn("One piece can be ordered.", result["text"])
        self.assertEqual(result["received_bytes"], len(HTML))
        self.assertEqual(result["html_sha256"], sha256(HTML).hexdigest())
        page = {"id": result["id"], "url": result["final_url"], "title": result["title"], "text": result["text"]}
        validate_supplier_input({"id": "lead-one", "product": "Saved product", "pages": [page]})
        self.assertTrue(transport.connections[0].closed)

    def test_script_style_template_and_noscript_are_not_quote_material(self):
        result = fetch(Transport([Response(b'<title>Terms</title><script>free supplier</script><style>hidden</style>'
            b'<template>invented stock</template><noscript>fallback</noscript><p>No subscription required.</p>')]))
        self.assertEqual(result["text"], "No subscription required.")
        self.assertIn("invented stock", result["html"])

    def test_actual_price_spans_do_not_glue_quote_unit_to_interactive_get_latest_price(self):
        # Structure inspected in the saved Styeva public-page HTML. Keep source
        # wording and ordinary inline price spans; only the onclick CTA breaks.
        for amount, unit in (("225", "Roll"), ("200", "Piece")):
            with self.subTest(unit=unit):
                body = (f'<span class="fnt21 m37 ds txt7">Approx. Price:&nbsp;'
                        f'<span class="fnt18 clr16">Rs&nbsp;{amount}</span>'
                        f'<span class="fnt21 clr19"> / {unit}</span>'
                        '<span class="glp_gp fnt12 cu_p" onclick="page_name(\'productpage\');">'
                        'Get Latest Price</span></span>').encode()
                result = fetch(Transport([Response(body)]))
                self.assertEqual(result["state"], "usable")
                self.assertEqual(result["text"], f"Approx. Price: Rs {amount} / {unit}\nGet Latest Price")
                self.assertNotIn(unit + "Get", result["text"])
                self.assertEqual(result["html_sha256"], sha256(body).hexdigest())
                quotation = {"references": [result["id"]], "quotes": [{"page_id": result["id"],
                             "excerpt": f"Approx. Price: Rs {amount} / {unit}"}]}
                _check_citations(quotation, {result["id"]: {"text": result["text"]}}, required=True)
                self.assertEqual(quotation["quotes"][0]["excerpt"], f"Approx. Price: Rs {amount} / {unit}")

    def test_link_button_and_form_control_boundaries_preserve_source_words(self):
        body = (b'<p>Price Rs 50 / Piece<a href="/quote">Get Quote</a>MOQ 20</p>'
                b'<p>Terms<button>Ask Now</button>No subscription stated</p>'
                b'<p>Dispatch<select><option>India</option><option>Other</option></select>Not confirmed</p>')
        result = fetch(Transport([Response(body)]))
        self.assertEqual(result["text"], "Price Rs 50 / Piece\nGet Quote\nMOQ 20\nTerms\nAsk Now\n"
                         "No subscription stated\nDispatch\nIndia\nOther\nNot confirmed")
        self.assertEqual(result["discovered_links"], ["https://supplier.example.org/quote"])

    def test_nested_control_spans_separate_control_edges_without_splitting_inline_price(self):
        body = (b'<p>Rs <span>2<span>25</span></span> / Roll'
                b'<span role="button">Get <span>Latest</span> Price</span>'
                b'<span>MOQ 500 Roll</span></p>')
        result = fetch(Transport([Response(body)]))
        self.assertEqual(result["text"], "Rs 225 / Roll\nGet Latest Price\nMOQ 500 Roll")

    def test_actual_dns_address_is_checked_and_given_to_transport_without_relookup(self):
        transport = Transport([Response()])
        result = fetch(transport)
        self.assertEqual(result["state"], "usable")
        self.assertEqual(transport.requests[0]["address"][2], (PUBLIC_IP, 443))
        self.assertEqual(transport.requests[0]["headers"]["Connection"], "close")
        self.assertFalse({"Cookie", "Authorization", "Proxy-Authorization"} & set(transport.requests[0]["headers"]))

    def test_rejects_private_loopback_link_local_reserved_multicast_and_mapped_addresses(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "100.64.0.1", "0.0.0.0",
                   "224.0.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "ff02::1",
                   "64:ff9b::7f00:1", "2002:7f00:1::"):
            with self.subTest(ip=ip):
                family = socket.AF_INET6 if ":" in ip else socket.AF_INET
                resolve = lambda *args, **kwargs: [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]
                transport = Transport([])
                result = fetch(transport, resolver=resolve)
                self.assertEqual(result["state"], "rejected")
                self.assertEqual(result["reason"], "PUBLIC_DESTINATION_REJECTED")
                self.assertEqual(transport.requests, [])

    def test_mixed_public_and_private_dns_response_is_not_selectively_accepted(self):
        mixed = lambda *args, **kwargs: resolver(*args, **kwargs) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]
        self.assertEqual(fetch(Transport([]), resolver=mixed)["reason"], "PUBLIC_DESTINATION_REJECTED")

    def test_invalid_urls_do_not_call_dns_or_echo_embedded_secrets(self):
        for url in ("file:///etc/passwd", "https://name:password@site.example/", "https://site.example/?token=secret",
                    "https://site.example/%0d%0aHeader:yes", "https://site.example/\n", "https://site.example\\@localhost/", "http://[fe80::1%25lo]/"):
            result = fetch_public_page({"id": "source", "url": url}, resolver=lambda *_args, **_kw: self.fail("DNS called"))
            self.assertEqual(result["state"], "rejected")
            self.assertIsNone(result["requested_url"])
            self.assertNotIn("secret", result["reason"])

    def test_redirect_destination_gets_its_own_dns_check_before_connection(self):
        transport = Transport([Response(status=302, headers={"Location": "http://127.0.0.1/private"})])
        def resolve(host, port, **kwargs):
            return resolver(host, port, **kwargs) if host != "127.0.0.1" else [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
        result = fetch(transport, resolver=resolve)
        self.assertEqual(result["reason"], "PUBLIC_DESTINATION_REJECTED")
        self.assertEqual(len(transport.requests), 1)

    def test_three_redirects_allowed_fourth_not_followed_and_loops_stop(self):
        responses = [Response(status=302, headers={"Location": f"/page{n}"}) for n in range(3)] + [Response()]
        result = fetch(Transport(responses))
        self.assertEqual(result["state"], "usable")
        self.assertEqual(len(result["redirects"]), 3)
        overflow = fetch(Transport([Response(status=302, headers={"Location": f"/p{n}"}) for n in range(4)]))
        self.assertEqual(overflow["reason"], "PUBLIC_REDIRECT_LIMIT")
        loop = fetch(Transport([Response(status=301, headers={"Location": "/product"})]))
        self.assertEqual(loop["reason"], "PUBLIC_REDIRECT_LOOP")

    def test_non_success_blocked_status_is_unavailable_not_supplier_absence(self):
        for status in (401, 403, 404, 429, 500):
            result = fetch(Transport([Response(status=status)]))
            self.assertEqual(result["state"], "unavailable")
            self.assertEqual(result["http_status"], status)
            self.assertEqual(result["reason"], "PUBLIC_HTTP_UNAVAILABLE")
            self.assertIsNone(result["html_sha256"])

    def test_response_retains_only_the_safe_retry_after_not_other_headers(self):
        result = fetch(Transport([Response(status=429, headers={"Retry-After": "3600",
            "Set-Cookie": "private-cookie", "X-Private-Value": "not-for-receipt"})]))
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["retry_after"], "3600")
        self.assertIsNone(result["html_sha256"])
        self.assertNotIn("private-cookie", str(result))
        self.assertNotIn("not-for-receipt", str(result))

    def test_redirect_retry_after_does_not_describe_the_next_response(self):
        result = fetch(Transport([Response(status=302, headers={"Location": "/actual", "Retry-After": "3600"}),
            Response(status=429, headers={})]))
        self.assertEqual(result["http_status"], 429)
        self.assertNotIn("retry_after", result)

    def test_unsafe_or_unbounded_retry_header_is_not_saved(self):
        for value in ("120\r\nprivate-value", "1" * 257, 3600):
            with self.subTest(value_type=type(value).__name__):
                result = fetch(Transport([Response(status=429, headers={"Retry-After": value})]))
                self.assertNotIn("retry_after", result)
                self.assertEqual(result["http_status"], 429)

    def test_content_type_compression_and_charset_failures_are_named(self):
        for headers, code in [({"Content-Type": "application/pdf"}, "PUBLIC_CONTENT_TYPE_UNSUPPORTED"),
                              ({"Content-Type": "text/html", "Content-Encoding": "br"}, "PUBLIC_ENCODING_UNSUPPORTED"),
                              ({"Content-Type": "text/html; charset=unknown-encoding"}, "PUBLIC_CHARSET_UNSUPPORTED"),
                              ({"Content-Type": "text/html", "Content-Length": "invalid"}, "PUBLIC_HEADERS_INVALID")]:
            self.assertEqual(fetch(Transport([Response(headers=headers)]))["reason"], code)

    def test_streamed_and_declared_body_size_cannot_exceed_one_megabyte(self):
        for response in (Response(b"x" * (MAX_HTML + 1)), Response(headers={"Content-Type": "text/html", "Content-Length": str(MAX_HTML + 1)})):
            self.assertEqual(fetch(Transport([response]))["reason"], "PUBLIC_HTML_TOO_LARGE")

    def test_gzip_is_bounded_in_wire_and_expanded_form(self):
        headers = {"Content-Type": "text/html", "Content-Encoding": "gzip"}
        result = fetch(Transport([Response(gzip.compress(HTML), headers=headers)]))
        self.assertEqual(result["state"], "usable")
        self.assertTrue(result["body_complete"])
        self.assertEqual(result["html_bytes"], len(HTML))
        bomb = fetch(Transport([Response(gzip.compress(b"x" * (MAX_HTML + 1)), headers=headers)]))
        self.assertEqual(bomb["reason"], "PUBLIC_HTML_TOO_LARGE")
        broken = fetch(Transport([Response(gzip.compress(HTML)[:-5], headers=headers)]))
        self.assertEqual(broken["reason"], "PUBLIC_ENCODING_INVALID")

    def test_text_excerpt_is_explicitly_truncated_and_links_are_discovered_not_fetched(self):
        body = b'<p>' + b'a' * 25000 + b'</p><a href="/terms">Terms</a><a href="mailto:contact@example.org">Email</a>'
        transport = Transport([Response(body)])
        result = fetch(transport)
        self.assertEqual(len(result["text"]), 24000)
        self.assertTrue(result["text_truncated"])
        self.assertEqual(result["discovered_links"], ["https://supplier.example.org/terms"])
        self.assertEqual(len(transport.requests), 1)

    def test_no_accessible_text_is_an_unavailable_capture_not_a_completed_search(self):
        result = fetch(Transport([Response(b'<script>only javascript</script>')]))
        self.assertEqual(result["reason"], "PUBLIC_TEXT_UNAVAILABLE")
        self.assertEqual(result["state"], "unavailable")

    def test_connection_close_response_keeps_stream_timeout_control(self):
        result = fetch(Transport([Response()], detach=True))
        self.assertEqual(result["state"], "usable")

    def test_native_content_length_close_response_finishes_before_touching_closed_socket(self):
        transport = NativeResponseTransport()
        self.addCleanup(transport.close)
        result = fetch(transport)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["state"], "usable")
        self.assertTrue(result["body_complete"])
        self.assertEqual(result["received_bytes"], len(HTML))
        self.assertEqual(result["html_sha256"], sha256(HTML).hexdigest())
        self.assertIsNone(transport.connections[0].sock)
        self.assertEqual(transport.sockets[0].fileno(), -1)

    def test_native_gzip_close_response_preserves_complete_wire_and_html_hashes(self):
        compressed = gzip.compress(HTML)
        transport = NativeResponseTransport(compressed, {"Content-Encoding": "gzip"})
        self.addCleanup(transport.close)
        result = fetch(transport)
        self.assertEqual(result["state"], "usable")
        self.assertTrue(result["body_complete"])
        self.assertEqual(result["received_bytes"], len(compressed))
        self.assertEqual(result["html_bytes"], len(HTML))
        self.assertEqual(result["wire_sha256"], sha256(compressed).hexdigest())
        self.assertEqual(result["html_sha256"], sha256(HTML).hexdigest())

    def test_native_chunked_close_response_reads_the_terminal_chunk(self):
        wire_body = f"{len(HTML):x}\r\n".encode("ascii") + HTML + b"\r\n0\r\n\r\n"
        transport = NativeResponseTransport(wire_body, {"Transfer-Encoding": "chunked", "Content-Length": None})
        self.addCleanup(transport.close)
        result = fetch(transport)
        self.assertEqual(result["state"], "usable")
        self.assertTrue(result["body_complete"])
        self.assertEqual(result["received_bytes"], len(HTML))
        self.assertEqual(result["html_sha256"], sha256(HTML).hexdigest())

    def test_native_eof_delimited_close_response_remains_usable(self):
        transport = NativeResponseTransport(HTML, {"Content-Length": None})
        self.addCleanup(transport.close)
        result = fetch(transport)
        self.assertEqual(result["state"], "usable")
        self.assertTrue(result["body_complete"])
        self.assertEqual(result["html_sha256"], sha256(HTML).hexdigest())

    def test_native_empty_close_response_is_unavailable_text_not_transport_failure(self):
        transport = NativeResponseTransport(b"")
        self.addCleanup(transport.close)
        result = fetch(transport)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["reason"], "PUBLIC_TEXT_UNAVAILABLE")
        self.assertTrue(result["body_complete"])
        self.assertEqual(result["received_bytes"], 0)

    def test_native_premature_content_length_eof_preserves_partial_receipt_not_supplier_evidence(self):
        transport = NativeResponseTransport(HTML, {"Content-Length": str(len(HTML) + 50)})
        self.addCleanup(transport.close)
        result = fetch(transport)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["reason"], "PUBLIC_BODY_INCOMPLETE")
        self.assertFalse(result["body_complete"])
        self.assertEqual(result["received_bytes"], len(HTML))
        self.assertEqual(result["wire_sha256"], sha256(HTML).hexdigest())
        self.assertIsNone(result["html"])
        self.assertIsNone(result["html_sha256"])
        self.assertEqual(result["text"], "")

    def test_native_gzip_premature_eof_compares_declared_length_to_wire_not_expanded_bytes(self):
        compressed = gzip.compress(HTML)
        transport = NativeResponseTransport(compressed, {"Content-Encoding": "gzip",
                                           "Content-Length": str(len(compressed) + 50)})
        self.addCleanup(transport.close)
        result = fetch(transport)
        self.assertEqual(result["reason"], "PUBLIC_BODY_INCOMPLETE")
        self.assertEqual(result["received_bytes"], len(compressed))
        self.assertEqual(result["html_bytes"], len(HTML))
        self.assertFalse(result["body_complete"])
        self.assertIsNone(result["html_sha256"])

    def test_native_chunked_boundary_is_not_compared_to_a_content_length_header(self):
        wire_body = f"{len(HTML):x}\r\n".encode("ascii") + HTML + b"\r\n0\r\n\r\n"
        transport = NativeResponseTransport(wire_body, {"Transfer-Encoding": "chunked"})
        self.addCleanup(transport.close)
        result = fetch(transport)
        self.assertEqual(result["state"], "usable")
        self.assertTrue(result["body_complete"])
        self.assertEqual(result["received_bytes"], len(HTML))

    def test_native_chunked_premature_eof_remains_incomplete(self):
        wire_body = f"{len(HTML):x}\r\n".encode("ascii") + HTML + b"\r\n"
        transport = NativeResponseTransport(wire_body, {"Transfer-Encoding": "chunked", "Content-Length": None})
        self.addCleanup(transport.close)
        result = fetch(transport)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["reason"], "PUBLIC_TRANSPORT_UNAVAILABLE")
        self.assertFalse(result["body_complete"])
        self.assertIsNone(result["html_sha256"])

    def test_dns_failure_is_safely_reported_without_raw_server_or_exception_details(self):
        def broken(*_args, **_kwargs):
            raise socket.gaierror("private raw details")
        result = fetch(Transport([]), resolver=broken)
        self.assertEqual(result["reason"], "PUBLIC_TRANSPORT_UNAVAILABLE")
        self.assertNotIn("private", repr(result))

    def test_overall_deadline_includes_stuck_dns_without_unbounded_workers(self):
        release = threading.Event()
        def stuck(*args, **kwargs):
            release.wait(2)
            return resolver(*args, **kwargs)
        start = time.monotonic()
        try:
            result = fetch(Transport([]), resolver=stuck, timeout=0.03)
            self.assertEqual(result["reason"], "PUBLIC_FETCH_DEADLINE")
            self.assertLess(time.monotonic() - start, 0.3)
        finally:
            release.set()

    def test_worker_capacity_is_bounded_when_previous_lookups_are_stuck(self):
        with patch.object(public_pages, "_SLOTS", threading.BoundedSemaphore(0)):
            self.assertEqual(fetch(Transport([]))["reason"], "PUBLIC_FETCH_WORKERS_BUSY")

    def test_tls_verification_uses_original_hostname_but_numeric_destination(self):
        raw, secure, control = Sock(), Sock(), _Control(time.monotonic() + 2)
        context = unittest.mock.Mock()
        context.wrap_socket.return_value = secure
        secure.do_handshake = unittest.mock.Mock()
        connection = _PinnedConnection("supplier.example.org", 443, "https", (socket.AF_INET, 6, (PUBLIC_IP, 443)), control)
        with patch.object(public_pages.socket, "socket", return_value=raw), patch.object(public_pages.ssl, "create_default_context", return_value=context):
            connection.connect()
        self.assertEqual(raw.destination, (PUBLIC_IP, 443))
        context.wrap_socket.assert_called_once_with(raw, server_hostname="supplier.example.org", do_handshake_on_connect=False)
        self.assertIsNone(context.keylog_filename)
        secure.do_handshake.assert_called_once()

    def test_invalid_timeout_or_identifier_has_no_network_effect(self):
        for timeout in (0, 21, True, None):
            result = fetch_public_page({"id": "source", "url": "https://supplier.example.org"}, timeout=timeout)
            self.assertEqual(result["state"], "rejected")
        self.assertEqual(fetch_public_page({"id": "../outside", "url": "https://supplier.example.org"})["state"], "rejected")

    def test_certificate_failure_remains_unavailable_without_turning_off_verification(self):
        def failed_connection(*_args):
            raise ssl.SSLCertVerificationError("private certificate details")
        result = fetch(failed_connection)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["reason"], "PUBLIC_TRANSPORT_UNAVAILABLE")
        self.assertNotIn("certificate details", repr(result))

    def test_partial_overflow_receipt_has_actual_bounded_length_not_a_fake_zero(self):
        result = fetch(Transport([Response(b"x" * (MAX_HTML + 100))]))
        self.assertEqual(result["received_bytes"], MAX_HTML + 1)
        self.assertFalse(result["body_complete"])
        self.assertIsNotNone(result["wire_sha256"])
        self.assertIsNone(result["html_sha256"])

    def test_one_deadline_also_contains_a_blocked_response_read(self):
        release = threading.Event()
        def stuck(_size):
            release.wait(2)
            return b""
        try:
            result = fetch(Transport([Response(reader=stuck)]), timeout=0.03)
            self.assertEqual(result["reason"], "PUBLIC_FETCH_DEADLINE")
            self.assertIsNone(result["received_bytes"])
        finally:
            release.set()

    def test_declared_text_charset_and_decoding_limitations_are_retained(self):
        result = fetch(Transport([Response('<p>Caf\u00e9 supplier</p>'.encode('latin-1'), headers={"Content-Type": "text/html; charset=iso-8859-1"})]))
        self.assertEqual(result["text"], "Caf\u00e9 supplier")
        self.assertEqual(result["charset"], "iso-8859-1")
        self.assertFalse(result["decode_replacements"])


if __name__ == "__main__":
    unittest.main()
