from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
import urllib.error

from bounded_http import HttpPool, RequestNotSent, smoke_test


class BoundedHttpTests(unittest.TestCase):
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
