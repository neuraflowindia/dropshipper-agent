"""Owned public-reader interruption checks. All DNS and sockets are synthetic."""

from pathlib import Path
import queue
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar import public_pages
from product_radar.models import SafetyError
from product_radar.source_deadline import SourceActionDeadline

REAL_THREAD = threading.Thread
REAL_QUEUE = queue.Queue
REAL_CONTROL = public_pages._Control
ITEM = {"id": "synthetic-owner-interruption", "url": "https://example.com/supplier"}


def public_resolver(_host, port, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class Socket:
    def __init__(self, release=None, shutdown_error=None, close_error=None):
        self.release, self.shutdown_error, self.close_error = release, shutdown_error, close_error
        self.shutdowns, self.closes, self.closed = 0, 0, False

    def settimeout(self, _timeout):
        pass

    def shutdown(self, _how):
        self.shutdowns += 1
        if self.release is not None:
            self.release.set()
        if self.shutdown_error is not None:
            raise self.shutdown_error

    def close(self):
        self.closes += 1
        self.closed = True
        if self.release is not None:
            self.release.set()
        if self.close_error is not None:
            raise self.close_error


class Response:
    def __init__(self):
        self.status, self.closed, self.reads = 200, False, 0

    def getheader(self, name):
        return "text/html; charset=utf-8" if name == "Content-Type" else None

    def read1(self, _size):
        self.reads += 1
        return b"<p>Public supplier terms.</p>" if self.reads == 1 else b""

    def isclosed(self):
        return self.closed

    def close(self):
        self.closed = True


class PublicPageInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.threads, self.controls, self.releases = [], [], []
        self.slots = threading.BoundedSemaphore(2)
        outer = self

        class ObservedControl(REAL_CONTROL):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                outer.controls.append(self)

        class ObservedThread(REAL_THREAD):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                outer.threads.append(self)

        self.patches = [patch.object(public_pages, "_SLOTS", self.slots),
                        patch.object(public_pages, "_Control", ObservedControl),
                        patch.object(public_pages.threading, "Thread", ObservedThread)]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)

    def tearDown(self):
        for release in self.releases:
            release.set()
        for worker in self.threads:
            # Join through the real base method, including the test that makes
            # the public join raise. Never let an injected worker escape a test.
            if worker.ident is not None:
                REAL_THREAD.join(worker, timeout=2)
                self.assertFalse(worker.is_alive(), "Synthetic reader did not exit")

    def gate(self):
        release = threading.Event()
        self.releases.append(release)
        return release

    def available_slots(self):
        acquired = 0
        while self.slots.acquire(blocking=False):
            acquired += 1
        for _ in range(acquired):
            self.slots.release()
        return acquired

    def interrupted_queue(self, error, ready):
        class InterruptedQueue(REAL_QUEUE):
            def get(self, *args, **kwargs):
                if not ready.wait(2):
                    raise AssertionError("Synthetic reader did not reach the requested phase")
                raise error
        return patch.object(public_pages.queue, "Queue", InterruptedQueue)

    def fetch(self, *, resolver=public_resolver, connection_factory=None, **kwargs):
        if connection_factory is None:
            connection_factory = self.connection_factory()
        return public_pages.fetch_public_page(ITEM, resolver=resolver,
            connection_factory=connection_factory, **kwargs)

    def connection_factory(self, *, active=None, release=None, sock=None, requests=None, response=None):
        sock = sock or Socket()
        class Connection:
            def __init__(self, control):
                self.sock, self.response = sock, response or Response()
                control.register(sock)

            def request(self, *args, **kwargs):
                if requests is not None:
                    requests.append((args, kwargs))

            def getresponse(self):
                if active is not None:
                    active.set()
                if release is not None and not release.wait(2):
                    raise AssertionError("Synthetic response was not released")
                return self.response

            def close(self):
                self.sock.close()

        return lambda _host, _port, _scheme, _address, control: Connection(control)

    def assert_original(self, error, action):
        with self.assertRaises(type(error)) as caught:
            action()
        self.assertIs(caught.exception, error)
        self.assertFalse(hasattr(error, "execution_receipt"))
        self.assertFalse(hasattr(error, "native_result"))

    def test_active_dns_keyboard_interruption_cancels_without_claiming_worker_exit(self):
        active, release = threading.Event(), self.gate()
        requests = []
        def resolver(*_args, **_kwargs):
            active.set()
            release.wait(2)
            return public_resolver("example.com", 443)
        error = KeyboardInterrupt("original owner signal")
        with self.interrupted_queue(error, active):
            self.assert_original(error, lambda: self.fetch(resolver=resolver,
                connection_factory=self.connection_factory(requests=requests), retain_transport=True))
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertTrue(self.threads[0].is_alive())  # Cancellation cannot stop a stuck resolver.
        self.assertEqual(self.available_slots(), 1)
        release.set()
        REAL_THREAD.join(self.threads[0], timeout=2)
        self.assertFalse(self.threads[0].is_alive())
        self.assertEqual(self.available_slots(), 2)
        self.assertEqual(requests, [])

    def test_active_dns_system_exit_preserves_exit_value_and_does_not_retry(self):
        active, release, calls = threading.Event(), self.gate(), []
        def resolver(*_args, **_kwargs):
            calls.append("dns")
            active.set()
            release.wait(2)
            raise SafetyError("PUBLIC_DNS_UNAVAILABLE")
        error = SystemExit(73)
        with self.interrupted_queue(error, active):
            self.assert_original(error, lambda: self.fetch(resolver=resolver))
        self.assertEqual(error.code, 73)
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertEqual(calls, ["dns"])

    def test_owner_cleanup_does_not_wait_for_dispatch_callback_lock(self):
        for error in (KeyboardInterrupt(), SystemExit(74)):
            with self.subTest(signal=type(error).__name__):
                active, release, calls = threading.Event(), self.gate(), []
                def on_dispatch():
                    calls.append("dispatch")
                    active.set()
                    release.wait(2)
                def resolver(*args, **kwargs):
                    calls.append("dns")
                    return public_resolver(*args, **kwargs)
                started = time.monotonic()
                with self.interrupted_queue(error, active):
                    self.assert_original(error, lambda: self.fetch(resolver=resolver, on_dispatch=on_dispatch))
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(self.controls[-1].cancelled.is_set())
                self.assertEqual(calls, ["dispatch"])
                self.assertEqual(self.available_slots(), 1)
                release.set()
                REAL_THREAD.join(self.threads[-1], timeout=2)
                self.assertFalse(self.threads[-1].is_alive())
                self.assertEqual(calls, ["dispatch"])
                self.assertEqual(self.available_slots(), 2)

    def test_registered_socket_is_closed_on_keyboard_interruption(self):
        active, release, requests = threading.Event(), self.gate(), []
        sock = Socket(release)
        error = KeyboardInterrupt()
        with self.interrupted_queue(error, active):
            self.assert_original(error, lambda: self.fetch(connection_factory=self.connection_factory(
                active=active, release=release, sock=sock, requests=requests), retain_transport=True))
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertTrue(sock.closed)
        self.assertEqual(sock.shutdowns, 1)
        self.assertEqual(len(requests), 1)

    def test_registered_socket_is_closed_on_system_exit(self):
        active, release = threading.Event(), self.gate()
        sock, error = Socket(release), SystemExit(75)
        with self.interrupted_queue(error, active):
            self.assert_original(error, lambda: self.fetch(connection_factory=self.connection_factory(
                active=active, release=release, sock=sock)))
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertTrue(sock.closed)

    def test_interrupted_body_read_closes_socket_without_returning_partial_body(self):
        active, release = threading.Event(), self.gate()
        class BlockingBody(Response):
            def read1(self, _size):
                active.set()
                release.wait(2)
                return b"<p>Partial public supplier terms.</p>"
        sock, error = Socket(release), KeyboardInterrupt()
        with self.interrupted_queue(error, active):
            self.assert_original(error, lambda: self.fetch(connection_factory=self.connection_factory(
                sock=sock, response=BlockingBody()), retain_transport=True))
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertTrue(sock.closed)
        self.assertEqual(self.controls[0].execution_snapshot()["execution_stage"], "response_received")

    def test_unexpected_socket_shutdown_failure_cannot_replace_original_signal(self):
        active, release = threading.Event(), self.gate()
        sock = Socket(release, shutdown_error=ValueError("synthetic cleanup failure"))
        error = KeyboardInterrupt()
        with self.interrupted_queue(error, active):
            self.assert_original(error, lambda: self.fetch(connection_factory=self.connection_factory(
                active=active, release=release, sock=sock)))
        self.assertTrue(sock.closed)  # close still runs if shutdown itself fails.
        self.assertTrue(self.controls[0].cancelled.is_set())

    def test_socket_close_failure_cannot_replace_original_exit(self):
        active, release = threading.Event(), self.gate()
        sock = Socket(release, close_error=OSError("synthetic cleanup failure"))
        error = SystemExit(76)
        with self.interrupted_queue(error, active):
            self.assert_original(error, lambda: self.fetch(connection_factory=self.connection_factory(
                active=active, release=release, sock=sock)))
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertTrue(sock.closed)

    def test_interruption_during_retained_worker_join_does_not_return_arrived_reply(self):
        for error in (KeyboardInterrupt(), SystemExit(77)):
            with self.subTest(signal=type(error).__name__):
                completed, release = threading.Event(), self.gate()
                outer = self
                class ReplyThenWaitQueue(REAL_QUEUE):
                    def put(self, *args, **kwargs):
                        super().put(*args, **kwargs)
                        completed.set()
                        release.wait(2)
                class InterruptedJoin(REAL_THREAD):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        outer.threads.append(self)
                    def join(self, *args, **kwargs):
                        if not completed.wait(2):
                            raise AssertionError("Synthetic reply did not arrive")
                        raise error
                with patch.object(public_pages.queue, "Queue", ReplyThenWaitQueue), \
                     patch.object(public_pages.threading, "Thread", InterruptedJoin):
                    self.assert_original(error, lambda: self.fetch(retain_transport=True))
                self.assertTrue(self.controls[-1].cancelled.is_set())
                self.assertEqual(self.controls[-1].execution_snapshot()["execution_stage"], "response_received")
                self.assertTrue(self.threads[-1].is_alive())
                release.set()
                REAL_THREAD.join(self.threads[-1], timeout=2)
                self.assertFalse(self.threads[-1].is_alive())
                self.assertEqual(self.available_slots(), 2)

    def test_before_start_interruption_releases_once_and_delayed_entry_never_reads(self):
        for error in (KeyboardInterrupt(), SystemExit(78)):
            with self.subTest(signal=type(error).__name__):
                outer, calls = self, []
                class DelayedThread(REAL_THREAD):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        outer.threads.append(self)
                    def start(self):
                        raise error
                def resolver(*args, **kwargs):
                    calls.append("dns")
                    return public_resolver(*args, **kwargs)
                with patch.object(public_pages.threading, "Thread", DelayedThread):
                    self.assert_original(error, lambda: self.fetch(resolver=resolver))
                self.assertTrue(self.controls[-1].cancelled.is_set())
                self.assertEqual(self.available_slots(), 2)
                REAL_THREAD.start(self.threads[-1])  # Reproduce delayed entry after start raised.
                REAL_THREAD.join(self.threads[-1], timeout=2)
                self.assertFalse(self.threads[-1].is_alive())
                self.assertEqual(calls, [])
                self.assertEqual(self.available_slots(), 2)

    def test_started_worker_keeps_slot_if_thread_start_raises_owner_signal(self):
        for error in (KeyboardInterrupt(), SystemExit(79)):
            with self.subTest(signal=type(error).__name__):
                active, release, calls = threading.Event(), self.gate(), []
                outer = self
                class StartedThenInterruptedThread(REAL_THREAD):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        outer.threads.append(self)
                    def start(self):
                        super().start()
                        if not active.wait(2):
                            raise AssertionError("Synthetic DNS did not start")
                        raise error
                def resolver(*_args, **_kwargs):
                    calls.append("dns")
                    active.set()
                    release.wait(2)
                    raise SafetyError("PUBLIC_DNS_UNAVAILABLE")
                with patch.object(public_pages.threading, "Thread", StartedThenInterruptedThread):
                    self.assert_original(error, lambda: self.fetch(resolver=resolver))
                self.assertTrue(self.controls[-1].cancelled.is_set())
                self.assertTrue(self.threads[-1].is_alive())
                self.assertEqual(self.available_slots(), 1)
                release.set()
                REAL_THREAD.join(self.threads[-1], timeout=2)
                self.assertFalse(self.threads[-1].is_alive())
                self.assertEqual(calls, ["dns"])
                self.assertEqual(self.available_slots(), 2)

    def test_thread_constructor_signal_does_not_leak_slot(self):
        for error in (KeyboardInterrupt(), SystemExit(80)):
            with self.subTest(signal=type(error).__name__):
                with patch.object(public_pages.threading, "Thread", side_effect=error):
                    self.assert_original(error, self.fetch)
                self.assertTrue(self.controls[-1].cancelled.is_set())
                self.assertEqual(self.available_slots(), 2)

    def test_first_python_line_after_slot_admission_is_already_interruption_protected(self):
        for error in (KeyboardInterrupt(), SystemExit(83)):
            with self.subTest(signal=type(error).__name__):
                prior_trace = sys.gettrace()
                function = public_pages.fetch_public_page.__wrapped__
                def interrupt_after_admission(frame, event, _argument):
                    if (event == "line" and frame.f_code is function.__code__
                            and frame.f_locals.get("slots") is self.slots
                            and "worker" not in frame.f_locals and self.available_slots() == 1):
                        raise error
                    return interrupt_after_admission
                try:
                    sys.settrace(interrupt_after_admission)
                    self.assert_original(error, self.fetch)
                finally:
                    sys.settrace(prior_trace)
                self.assertTrue(self.controls[-1].cancelled.is_set())
                self.assertEqual(self.available_slots(), 2)
                self.assertEqual(self.threads, [])

    def test_interruption_before_admission_never_frees_another_readers_slot(self):
        self.assertTrue(self.slots.acquire(blocking=False))  # A different owned reader.
        error = KeyboardInterrupt()
        with patch.object(self.slots, "acquire", side_effect=error):
            self.assert_original(error, self.fetch)
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertEqual(self.available_slots(), 1)
        self.slots.release()
        self.assertEqual(self.available_slots(), 2)

    def test_queue_constructor_signal_happens_before_slot_admission(self):
        for error in (KeyboardInterrupt(), SystemExit(81)):
            with self.subTest(signal=type(error).__name__):
                with patch.object(public_pages.queue, "Queue", side_effect=error):
                    self.assert_original(error, self.fetch)
                self.assertEqual(self.available_slots(), 2)
                self.assertEqual(self.threads, [])

    def test_control_constructor_signal_happens_before_slot_admission(self):
        for error in (KeyboardInterrupt(), SystemExit(82)):
            with self.subTest(signal=type(error).__name__):
                with patch.object(public_pages, "_Control", side_effect=error):
                    self.assert_original(error, self.fetch)
                self.assertEqual(self.available_slots(), 2)
                self.assertEqual(self.threads, [])

    def test_failed_thread_start_preserves_workers_busy_and_releases_slot(self):
        with patch.object(public_pages.threading.Thread, "start", side_effect=RuntimeError("synthetic startup failure")):
            result = self.fetch()
        self.assertEqual(result["reason"], "PUBLIC_FETCH_WORKERS_BUSY")
        self.assertEqual(result["execution_stage"], "not_dispatched")
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertEqual(self.available_slots(), 2)

    def test_unexpected_thread_constructor_error_cleans_up_without_replacing_error(self):
        error = ValueError("synthetic constructor failure")
        with patch.object(public_pages.threading, "Thread", side_effect=error):
            with self.assertRaises(ValueError) as caught:
                self.fetch()
        self.assertIs(caught.exception, error)
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertEqual(self.available_slots(), 2)

    def test_unexpected_queue_wait_error_cancels_active_reader(self):
        active, release = threading.Event(), self.gate()
        error = ValueError("synthetic queue failure")
        def resolver(*_args, **_kwargs):
            active.set()
            release.wait(2)
            raise SafetyError("PUBLIC_DNS_UNAVAILABLE")
        with self.interrupted_queue(error, active):
            with self.assertRaises(ValueError) as caught:
                self.fetch(resolver=resolver)
        self.assertIs(caught.exception, error)
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertEqual(self.available_slots(), 1)

    def test_queue_error_before_delayed_worker_entry_releases_once_without_dns(self):
        outer, calls = self, []
        class DelayedThread(REAL_THREAD):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                outer.threads.append(self)
            def start(self):
                pass  # Deliberately enter the target only after the caller exits.
        error = ValueError("synthetic early queue failure")
        class FailedQueue(REAL_QUEUE):
            def get(self, *args, **kwargs):
                raise error
        def resolver(*args, **kwargs):
            calls.append("dns")
            return public_resolver(*args, **kwargs)
        with patch.object(public_pages.threading, "Thread", DelayedThread), \
             patch.object(public_pages.queue, "Queue", FailedQueue):
            with self.assertRaises(ValueError) as caught:
                self.fetch(resolver=resolver)
        self.assertIs(caught.exception, error)
        self.assertTrue(self.controls[-1].cancelled.is_set())
        self.assertEqual(self.available_slots(), 2)
        REAL_THREAD.start(self.threads[-1])
        REAL_THREAD.join(self.threads[-1], timeout=2)
        self.assertFalse(self.threads[-1].is_alive())
        self.assertEqual(calls, [])
        self.assertEqual(self.available_slots(), 2)

    def test_genuine_start_deadline_retains_deadline_identity_and_releases_slot(self):
        error = SourceActionDeadline()
        with patch.object(public_pages.threading.Thread, "start", side_effect=error):
            with self.assertRaises(SourceActionDeadline) as caught:
                self.fetch()
        self.assertIs(caught.exception, error)
        self.assertEqual(error.execution_receipt["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertEqual(error.execution_receipt["execution_stage"], "not_dispatched")
        self.assertEqual(self.available_slots(), 2)

    def test_genuine_wait_deadline_retains_original_attempt_not_unreturned_response(self):
        active, release = threading.Event(), self.gate()
        error = SourceActionDeadline()
        def resolver(*_args, **_kwargs):
            active.set()
            release.wait(2)
            raise SafetyError("PUBLIC_DNS_UNAVAILABLE")
        with self.interrupted_queue(error, active):
            with self.assertRaises(SourceActionDeadline) as caught:
                self.fetch(resolver=resolver)
        self.assertIs(caught.exception, error)
        self.assertEqual(error.execution_receipt["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertEqual(error.execution_receipt["execution_stage"], "attempt_started")
        self.assertIsNone(error.execution_receipt["http_status"])
        self.assertIsNone(error.execution_receipt["html"])
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertEqual(self.available_slots(), 1)

    def test_genuine_body_deadline_does_not_promote_unreturned_http_response(self):
        active, release = threading.Event(), self.gate()
        class BlockingBody(Response):
            def read1(self, _size):
                active.set()
                release.wait(2)
                return b"<p>Partial public supplier terms.</p>"
        sock, error = Socket(release), SourceActionDeadline()
        with self.interrupted_queue(error, active):
            with self.assertRaises(SourceActionDeadline) as caught:
                self.fetch(connection_factory=self.connection_factory(sock=sock, response=BlockingBody()))
        self.assertIs(caught.exception, error)
        self.assertEqual(error.execution_receipt["reason"], "SOURCE_ACTION_TIMEOUT")
        self.assertEqual(error.execution_receipt["execution_stage"], "attempt_started")
        self.assertIsNone(error.execution_receipt["http_status"])
        self.assertIsNone(error.execution_receipt["html"])
        self.assertFalse(error.execution_receipt["body_complete"])
        self.assertTrue(self.controls[0].cancelled.is_set())
        self.assertTrue(sock.closed)

    def test_ordinary_success_keeps_actual_owned_exit_receipt_and_slot_count(self):
        result = self.fetch(retain_transport=True)
        self.assertEqual(result["state"], "usable")
        self.assertEqual(result["transport_contract"], "public-page-owned-http-v1")
        self.assertTrue(result["owned_worker_exited"])
        self.assertTrue(result["body_complete"])
        self.assertFalse(self.controls[0].cancelled.is_set())
        self.assertEqual(self.available_slots(), 2)

    def test_ordinary_unretained_success_does_not_add_owned_exit_claim(self):
        result = self.fetch()
        self.assertEqual(result["state"], "usable")
        self.assertNotIn("transport_contract", result)
        self.assertNotIn("owned_worker_exited", result)
        self.assertEqual(self.available_slots(), 2)


if __name__ == "__main__":
    unittest.main()
