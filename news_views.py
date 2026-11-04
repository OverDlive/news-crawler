"""Bounded background article views. This worker never owns Tk objects."""
from copy import deepcopy
import logging
import threading

from news_core import Store


class ViewLoader:
    def __init__(self, directory, events):
        self.directory, self.events = directory, events
        self.condition = threading.Condition()
        self.pending = None
        self.closed = False
        self.thread = None

    def submit(self, serial, keywords, active, hours, rules, notify):
        request = (serial, tuple(keywords), tuple(active), hours, deepcopy(rules), notify)
        with self.condition:
            if self.closed:
                return
            # Supersede queued refreshes instead of accumulating work per result.
            self.pending = request
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name='news-views')
                try:
                    self.thread.start()
                except Exception:
                    self.thread = None
                    raise
            self.condition.notify()

    def close(self):
        with self.condition:
            self.closed = True
            self.pending = None
            self.condition.notify()

    def _run(self):
        store = None
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.closed or self.pending is not None)
                    if self.closed:
                        return
                    request, self.pending = self.pending, None
                serial, keywords, active, hours, rules, notify = request
                try:
                    if store is None:
                        store = Store(self.directory, read_only=True)
                        store.db.set_progress_handler(lambda: int(self.closed), 1000)
                    display = store.list_articles(keywords, hours, rules=rules)
                    shared = (display if keywords == active else
                              store.list_articles(active, hours, rules=rules)) if notify else None
                    if not self.closed:
                        self.events.put(('view_ready', serial, display, shared))
                except Exception:
                    if not self.closed:
                        logging.exception('Article view failed; retrying in background')
                        self.events.put(('view_failed', serial))
                    if store is not None:
                        store.close()
                        store = None
        finally:
            if store is not None:
                store.close()
