"""HTTP I/O in replaceable processes, with a deadline including DNS and reads.

Python cannot stop a thread blocked in DNS. A timed-out process is terminated
before its slot is reused, so repeated outages cannot exhaust future retries.
Only bytes and HTTP metadata cross the pipe; no application or database state.
"""
import atexit
from email.message import Message
from io import BytesIO
import multiprocessing
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class RequestNotSent(TimeoutError):
    """No worker was available; the request was never submitted."""


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme not in ('http', 'https'):
            raise ValueError('지원하지 않는 연결 주소')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_worker(connection):
    opener = urllib.request.build_opener(_Redirect())
    try:
        while True:
            request = connection.recv()
            if request is None:
                return
            url, data, headers, method, timeout, limit = request
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with opener.open(req, timeout=timeout) as response:
                    result = ('ok', response.read(limit + 1), list(response.headers.items()),
                              response.geturl(), response.status)
            except urllib.error.HTTPError as error:
                try:
                    result = ('http', error.read(8192), list(error.headers.items()),
                              error.geturl(), error.code)
                finally:
                    error.close()
            except TimeoutError:
                result = ('timeout',)
            except urllib.error.URLError as error:
                result = ('timeout',) if isinstance(error.reason, TimeoutError) else ('error',)
            except Exception:
                # URLs and exception strings can contain bot tokens.
                result = ('error',)
            connection.send(result)
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


class _Worker:
    def __init__(self):
        self.process = None
        self.connection = None
        self.last_used = 0

    def start(self):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=_http_worker, args=(child,), daemon=True,
                                  name='news-http')
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        child.close()
        self.process, self.connection = process, parent

    def stop(self):
        process, connection = self.process, self.connection
        self.process = self.connection = None
        if connection is not None:
            connection.close()
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            if not process.is_alive():
                process.close()


class HttpPool:
    def __init__(self, workers=8, idle_timeout=90):
        self.slots = [_Worker() for _ in range(workers)]
        # Reuse the most recently released worker before starting an empty slot.
        self.available = queue.LifoQueue()
        for slot in self.slots:
            self.available.put(slot)
        self.closed = False
        self.idle_timeout = max(.05, idle_timeout)
        self.maintenance_stop = threading.Event()
        self.maintenance_lock = threading.Lock()
        self.maintenance = None

    def _start_maintenance(self):
        with self.maintenance_lock:
            if not self.closed and self.maintenance is None:
                self.maintenance = threading.Thread(target=self._maintain, daemon=True,
                                                    name='news-http-idle')
                self.maintenance.start()

    def _maintain(self):
        while not self.maintenance_stop.wait(min(30, self.idle_timeout / 2)):
            self._reap()

    def _reap(self):
        # Taking a slot out of the queue leases it exclusively, just like a
        # request. Never kill a worker whose request is currently in flight.
        idle = []
        try:
            for _ in self.slots:
                try:
                    slot = self.available.get_nowait()
                except queue.Empty:
                    break
                idle.append(slot)
                if self.closed or time.monotonic() - slot.last_used >= self.idle_timeout:
                    slot.stop()
        finally:
            for slot in reversed(idle):
                if self.closed:
                    slot.stop()
                self.available.put(slot)

    def request(self, request, timeout=12, max_bytes=4 * 1024 * 1024):
        if self.closed:
            raise RequestNotSent('통신 작업이 종료되었습니다')
        self._start_maintenance()
        deadline = time.monotonic() + max(.01, timeout)
        try:
            slot = self.available.get(timeout=max(.01, timeout))
        except queue.Empty:
            raise RequestNotSent('통신 대기 시간 초과: 다음 확인에서 재시도합니다') from None
        try:
            if self.closed:
                raise RequestNotSent('통신 작업이 종료되었습니다')
            if slot.process is None or not slot.process.is_alive():
                slot.stop()
                slot.start()
            if time.monotonic() >= deadline:
                raise RequestNotSent('통신 준비 시간 초과: 다음 확인에서 재시도합니다')
            if isinstance(request, str):
                request = urllib.request.Request(request)
            slot.connection.send((request.full_url, request.data, dict(request.header_items()),
                                  request.get_method(), timeout, max_bytes))
            while not slot.connection.poll(min(.1, max(0, deadline - time.monotonic()))):
                if self.closed or time.monotonic() >= deadline:
                    raise TimeoutError('통신 전체 대기 시간 초과')
            result = slot.connection.recv()
            if result[0] == 'timeout':
                raise TimeoutError('통신 전체 대기 시간 초과')
        except RequestNotSent:
            slot.stop()
            raise
        except (EOFError, BrokenPipeError, OSError):
            # Includes TimeoutError. A fresh process handles the next request.
            slot.stop()
            raise TimeoutError('통신 연결 실패 또는 전체 대기 시간 초과') from None
        finally:
            slot.last_used = time.monotonic()
            if self.closed:
                slot.stop()
            self.available.put(slot)
        if result[0] == 'error':
            raise urllib.error.URLError('통신 연결 실패')
        kind, body, pairs, url, status = result
        headers = Message()
        for key, value in pairs:
            headers[key] = value
        if kind == 'http':
            raise urllib.error.HTTPError(url, status, 'HTTP response error', headers, BytesIO(body))
        return Response(body, headers, url, status)

    def close(self):
        self.closed = True
        self.maintenance_stop.set()
        self._reap()
        # Active requests observe closed in their bounded poll and stop their
        # own worker. Closing another thread's pipe here would race with recv.


class Response(BytesIO):
    def __init__(self, body, headers, url, status):
        super().__init__(body)
        self.headers, self.url, self.status = headers, url, status

    def geturl(self):
        return self.url


_pool = HttpPool()
atexit.register(_pool.close)


def urlopen(request, timeout=12, max_bytes=4 * 1024 * 1024):
    return _pool.request(request, timeout=timeout, max_bytes=max_bytes)


def smoke_test():
    """Exercise process startup, forced termination, and recovery in an EXE."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/stalled':
                release.wait(3)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'news-http-ok')
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pool = HttpPool(workers=1)
    base = 'http://127.0.0.1:' + str(server.server_port)
    try:
        with pool.request(base, timeout=20) as response:
            assert response.read() == b'news-http-ok'
        try:
            pool.request(base + '/stalled', timeout=.2)
        except TimeoutError:
            pass
        else:
            raise AssertionError('Stalled HTTP worker was not terminated')
        with pool.request(base, timeout=20) as response:
            assert response.read() == b'news-http-ok'
    finally:
        release.set()
        pool.close()
        server.shutdown()
        server.server_close()
