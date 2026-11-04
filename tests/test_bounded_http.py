from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from bounded_http import HttpPool, RequestNotSent, smoke_test


class BoundedHttpTests(unittest.TestCase):
    def local_server(self, action=lambda: None):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                action()
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'ok')
                except OSError:
                    pass

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def test_sequential_requests_reuse_one_worker_and_idle_workers_are_released(self):
        url = self.local_server()
        pool = HttpPool(workers=8, idle_timeout=.2)
        try:
            for _ in range(8):
                with pool.request(url, timeout=10) as response:
                    self.assertEqual(response.read(), b'ok')
            self.assertEqual(sum(slot.process is not None for slot in pool.slots), 1)
            deadline = time.monotonic() + 3
            while any(slot.process is not None for slot in pool.slots) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(all(slot.process is None for slot in pool.slots))
            with pool.request(url, timeout=10) as response:
                self.assertEqual(response.read(), b'ok')
        finally:
            pool.close()

    def test_idle_reaper_does_not_stop_active_requests_and_pool_limit_is_preserved(self):
        entered, release = threading.Event(), threading.Event()
        count, lock = [0], threading.Lock()
        def hold():
            with lock:
                count[0] += 1
                if count[0] == 2:
                    entered.set()
            release.wait(5)
        url = self.local_server(hold)
        pool = HttpPool(workers=2, idle_timeout=.05)
        try:
            with ThreadPoolExecutor(2) as executor:
                futures = [executor.submit(pool.request, url, timeout=10) for _ in range(2)]
                try:
                    self.assertTrue(entered.wait(5))
                    time.sleep(.15)
                    with self.assertRaises(RequestNotSent):
                        pool.request(url, timeout=.05)
                    self.assertEqual(count[0], 2)
                    self.assertTrue(all(not task.done() for task in futures))
                finally:
                    release.set()
                for task in futures:
                    with task.result(timeout=5) as response:
                        self.assertEqual(response.read(), b'ok')
        finally:
            release.set()
            pool.close()

    def test_close_during_request_stops_worker_and_rejects_new_work(self):
        entered, release = threading.Event(), threading.Event()
        def hold():
            entered.set()
            release.wait(5)
        url = self.local_server(hold)
        pool = HttpPool(workers=1)
        try:
            with ThreadPoolExecutor(1) as executor:
                future = executor.submit(pool.request, url, timeout=10)
                self.assertTrue(entered.wait(5))
                pool.close()
                with self.assertRaises(TimeoutError):
                    future.result(timeout=3)
                self.assertTrue(all(slot.process is None for slot in pool.slots))
                with self.assertRaises(RequestNotSent):
                    pool.request(url)
        finally:
            release.set()
            pool.close()

    def test_stalled_process_is_terminated_and_next_request_recovers(self):
        smoke_test()

    def test_http_errors_preserve_retry_metadata_and_limit_response_size(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(429 if self.path == '/limited' else 200)
                self.send_header('Retry-After', '45')
                self.end_headers()
                self.wfile.write(b'x' * 100)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        pool = HttpPool(workers=1)
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                pool.request(base + '/limited', timeout=20)
            self.assertEqual(error.exception.code, 429)
            self.assertEqual(error.exception.headers['Retry-After'], '45')
            self.assertEqual(error.exception.read(), b'x' * 100)
            error.exception.close()
            with pool.request(base, timeout=20, max_bytes=10) as response:
                self.assertEqual(response.read(), b'x' * 11)
        finally:
            pool.close()
            server.shutdown()
            server.server_close()

    def test_saturated_pool_does_not_submit_another_request(self):
        pool = HttpPool(workers=1)
        slot = pool.available.get_nowait()
        try:
            started = time.monotonic()
            with self.assertRaises(RequestNotSent):
                pool.request('https://example.com/not-sent', timeout=.05)
            self.assertLess(time.monotonic() - started, .5)
            self.assertIsNone(slot.process)
        finally:
            pool.available.put(slot)
            pool.close()
