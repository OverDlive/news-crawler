"""Real Tk smoke and integration checks; no network required."""
from pathlib import Path
import sqlite3
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from ui_theme import ThemedButton
from app import NewsMonitor
from news_collection import CollectionReport
from news_core import Article
from tk_runtime import create_root
from ui_theme import GAP, RADIUS, RoundedPanel


class GuiTests(unittest.TestCase):
    def test_settings_show_version_and_disable_checks_in_demo(self):
        from tkinter import ttk
        from version import VERSION
        self.app.settings()
        self.root.update()
        win = self.app.settings_window
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(win))
        notebook = next(w for w in widgets if isinstance(w, ttk.Notebook))
        update_tab = next(tab for tab in notebook.tabs() if notebook.tab(tab, 'text') == '프로그램 / 업데이트')
        notebook.select(update_tab)
        self.root.update()
        version = next(w for w in widgets if w.winfo_name() == 'update_current_version')
        self.assertIn(VERSION, version.cget('text'))
        button = next(w for w in widgets if isinstance(w, ThemedButton) and w.cget('text') == '업데이트 확인')
        self.assertEqual(button.cget('state'), 'disabled')

    def test_settings_manual_update_check_displays_result_and_survives_reopen(self):
        self.app.demo = False
        self.app.next_fetch = time.monotonic() + 3600
        with patch('app_updates.latest_release', return_value={'tag_name': 'v' + self.app.updates.current}):
            self.app.settings()
            win = self.app.settings_window
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            widgets = list(descendants(win))
            status = next(w for w in widgets if w.winfo_name() == 'update_status')
            button = next(w for w in widgets if isinstance(w, ThemedButton) and w.cget('text') == '업데이트 확인')
            button.invoke()
            deadline = time.monotonic() + 3
            while '최신 버전' not in status.cget('text') and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.02)
            self.assertIn('최신 버전', status.cget('text'))
            self.root.tk.call(win.protocol('WM_DELETE_WINDOW'))
            self.app.settings()
            status = next(w for w in descendants(self.app.settings_window) if w.winfo_name() == 'update_status')
            self.assertIn('최신 버전', status.cget('text'))
        self.app.demo = True
        deadline = time.monotonic() + 3
        while (self.app.telegram_busy or self.app.kakao_busy) and time.monotonic() < deadline:
            time.sleep(.02)

    def test_telegram_token_only_workflow_discovers_selects_and_sends(self):
        from tkinter import ttk
        original_discover = self.app.telegram.discover
        original_activate = self.app.telegram.activate
        original_deliver = self.app.telegram.deliver
        calls = []
        def request(token, method, fields=None):
            calls.append((method, fields))
            return {'getMe': {'id': 123456, 'is_bot': True, 'username': 'news_bot'},
                    'getWebhookInfo': {'url': ''},
                    'getUpdates': [{'update_id': 1, 'message': {'chat': {'id': -100, 'type': 'group', 'title': '뉴스방'}}}],
                    'getChatMember': {'status': 'member'},
                    'sendMessage': {'message_id': 42}}[method]
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        def wait_for(condition):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                self.root.update()
                if condition():
                    return
                time.sleep(.03)
            self.fail('Telegram setup did not complete')
        self.app.demo = False
        self.app.next_fetch = time.monotonic() + 3600
        with patch.object(self.app.telegram, 'discover', side_effect=lambda token: original_discover(token, request=request)), \
             patch.object(self.app.telegram, 'activate', side_effect=lambda chat: original_activate(chat, request=request)), \
             patch.object(self.app.telegram, 'deliver', side_effect=lambda **kwargs: original_deliver(request=request, **kwargs)):
            self.app.telegram_settings()
            self.root.update()
            win = self.app.telegram_window
            widgets = list(descendants(win))
            buttons = {w.cget('text'): w for w in widgets if isinstance(w, ThemedButton)}
            entry = next(w for w in widgets if isinstance(w, tk.Entry))
            tree = next(w for w in widgets if isinstance(w, ttk.Treeview))
            entry.insert(0, '123456:' + 'a' * 30)
            buttons['토큰 저장 / 방 찾기'].invoke()
            wait_for(lambda: tree.exists('-100') and buttons['자동 공유 시작'].cget('state') == 'normal')
            self.assertEqual(entry.get(), '')
            self.assertEqual(tree.selection(), ('-100',))
            buttons['자동 공유 시작'].invoke()
            wait_for(lambda: self.app.telegram.settings()['enabled'] and buttons['테스트 보내기'].cget('state') == 'normal')
            buttons['테스트 보내기'].invoke()
            # Wait for the background operation to commit and the editor to
            # consume its completion before releasing mocks or deleting SQLite.
            wait_for(lambda: buttons['테스트 보내기'].cget('state') == 'normal'
                     and any(row['state'] == 'sent' for row in self.app.telegram.status()))
            self.assertEqual(next(fields['chat_id'] for method, fields in calls if method == 'sendMessage'), -100)
            for button in buttons.values():
                self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), win.winfo_rooty() + win.winfo_height())
            buttons['닫기'].invoke()
            self.app.demo = True
            wait_for(lambda: not self.app.telegram_busy and not self.app.kakao_busy)

    def test_telegram_editor_demo_blocks_network_and_closes_cleanly(self):
        self.app.telegram_settings()
        self.root.update()
        win = self.app.telegram_window
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        buttons = [w for w in descendants(win) if isinstance(w, ThemedButton)]
        self.assertEqual(len(buttons), 6)
        for button in buttons:
            if button.cget('text') != '닫기':
                self.assertEqual(button.cget('state'), 'disabled')
        self.app.telegram_settings()
        self.assertIs(self.app.telegram_window, win)
        next(b for b in buttons if b.cget('text') == '닫기').invoke()
        self.assertIsNone(self.app.telegram_window)

    def test_kakao_subscription_can_toggle_by_button_and_state_cell(self):
        from tkinter import ttk
        self.app.notifications.save_user('첫 사용자', 'first-test-token')
        self.app.notifications.save_user('둘째 사용자', 'second-test-token')
        self.app.kakao_settings()
        self.root.update()
        win = self.app.kakao_window
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(win))
        tree = next(w for w in widgets if isinstance(w, ttk.Treeview))
        toggle = next(w for w in widgets if isinstance(w, ThemedButton) and w.cget('text') == '수신 ON으로 변경')
        self.assertEqual(toggle.cget('state'), 'disabled')
        first, second = [str(user[0]) for user in self.app.notifications.users()]
        tree.selection_set(first)
        self.root.update()
        self.assertEqual(toggle.cget('text'), '수신 OFF로 변경')
        toggle.invoke()
        self.root.update()
        self.assertEqual([u[2] for u in self.app.notifications.users()], [0, 1])
        self.assertEqual(tree.item(first, 'values')[1], 'OFF')
        checkbox = next(w for w in widgets if isinstance(w, tk.Checkbutton))
        self.assertEqual(win.getvar(checkbox.cget('variable')), 0)
        x, y, width, height = tree.bbox(second, 'state')
        tree.event_generate('<Button-1>', x=x + width // 2, y=y + height // 2)
        self.root.update()
        self.assertEqual([u[2] for u in self.app.notifications.users()], [0, 0])
        self.assertEqual(tree.selection(), (second,))
        self.assertEqual(toggle.cget('text'), '수신 ON으로 변경')
        toggle.invoke()
        self.root.update()
        self.assertEqual([u[2] for u in self.app.notifications.users()], [0, 1])
        self.assertEqual(tree.item(second, 'values')[1], 'ON')

    def test_immediate_kakao_button_runs_in_background_and_blocks_duplicate_clicks(self):
        import threading
        from tkinter import ttk
        self.app.notifications.save_user('테스트 수신자', 'test-only-token')
        self.app.kakao_settings()
        self.root.update()
        win = self.app.kakao_window
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(win))
        button = next(w for w in widgets if isinstance(w, ThemedButton) and w.cget('text') == '즉시 알람 보내기')
        tree = next(w for w in widgets if isinstance(w, ttk.Treeview))
        tree.selection_set(str(self.app.notifications.users()[0][0]))
        self.root.update()
        self.assertEqual(button.cget('state'), 'disabled')  # Demo cannot send.
        self.app.demo = False
        tree.event_generate('<<TreeviewSelect>>')
        self.root.update()
        release, started = threading.Event(), threading.Event()
        def send(*args, **kwargs):
            started.set()
            release.wait(5)
            return 'sent', '전송 성공'
        with patch.object(self.app.notifications, 'send_test', side_effect=send) as call:
            try:
                button.invoke()
                self.assertTrue(started.wait(2))
                self.assertTrue(self.app.kakao_busy)
                self.assertEqual(button.cget('state'), 'disabled')
                button.invoke()
                self.assertEqual(call.call_count, 1)
            finally:
                release.set()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                self.root.update()
                if any(isinstance(w, tk.Label) and '나와의 채팅방을 확인' in w.cget('text') for w in widgets):
                    break
                time.sleep(.02)
            self.assertTrue(any(isinstance(w, tk.Label) and '나와의 채팅방을 확인' in w.cget('text') for w in widgets))
        self.app.demo = True

    def test_kakao_auto_refresh_settings_are_masked_and_saved_in_demo(self):
        self.app.notifications.save_user('갱신 테스트', 'access-test-token')
        self.app.kakao_settings()
        self.root.update()
        win = self.app.kakao_window
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        from tkinter import ttk
        tree = next(w for w in descendants(win) if isinstance(w, ttk.Treeview))
        user = self.app.notifications.users()[0][0]
        tree.selection_set(str(user))
        self.root.update()
        button = next(w for w in descendants(win) if isinstance(w, ThemedButton) and w.cget('text') == '자동 갱신')
        button.invoke()
        self.root.update()
        popup = next(w for w in win.winfo_children() if isinstance(w, tk.Toplevel))
        entries = [w for w in descendants(popup) if isinstance(w, tk.Entry)]
        self.assertEqual(len(entries), 3)
        for entry, value in zip(entries, ('app-test-key', 'refresh-test-token', 'test-client-secret')):
            self.assertTrue(entry.cget('show'))
            entry.insert(0, value)
        save = next(w for w in descendants(popup) if isinstance(w, ThemedButton) and w.cget('text') == '저장')
        self.assertLessEqual(save.winfo_rooty() + save.winfo_height(),
                             popup.winfo_rooty() + popup.winfo_height() - 20)
        with patch.object(self.app.notifications, 'refresh_due') as refresh:
            save.invoke()
            if self.app.tick_id:
                self.root.after_cancel(self.app.tick_id)
            self.app.tick()
            self.assertFalse(refresh.called)
        self.assertTrue(self.app.notifications.refresh_status(user)[0])

    def test_kakao_editor_matches_themes_and_keeps_controls_visible(self):
        from tkinter import ttk
        for theme in ('dark', 'light'):
            self.app.config['theme'] = theme
            self.app.apply_theme()
            self.app.kakao_settings()
            win = self.app.kakao_window
            self.root.update()
            win.geometry('860x700')
            self.root.update()
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            widgets = list(descendants(win))
            for entry in (w for w in widgets if isinstance(w, tk.Entry)):
                self.assertEqual(entry.cget('bg'), self.app.colors['input'])
                self.assertEqual(entry.cget('fg'), self.app.colors['fg'])
            history = next(w for w in widgets if isinstance(w, tk.Text))
            self.assertEqual(history.cget('bg'), self.app.colors['input'])
            self.assertIn('아직 전송 내역', history.get('1.0', 'end'))
            self.assertGreater(history.winfo_height(), 60)
            buttons = [w for w in widgets if isinstance(w, ThemedButton)]
            for button in buttons:
                self.assertTrue(button.winfo_viewable())
                self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                     win.winfo_rooty() + win.winfo_height())
            self.assertEqual(next(w for w in buttons if w.cget('text') == '선택 삭제').cget('state'), 'disabled')
            tree = next(w for w in widgets if isinstance(w, ttk.Treeview))
            self.assertEqual(len(tree.get_children()), 0)
            self.root.tk.call(win.protocol('WM_DELETE_WINDOW'))

    def test_kakao_registration_masks_token_and_demo_never_queues(self):
        self.app.kakao_settings()
        self.root.update()
        win = self.app.kakao_window
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(win))
        entries = [widget for widget in widgets if isinstance(widget, tk.Entry)]
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[1].cget('show'))
        entries[0].insert(0, '테스트 수신자')
        entries[1].insert(0, 'test-only-token')
        save = next(widget for widget in widgets if isinstance(widget, ThemedButton)
                    and widget.cget('text') == '등록 / 수정')
        save.invoke()
        self.assertEqual(self.app.notifications.users()[0][1], '테스트 수신자')
        self.assertEqual(entries[1].get(), '')
        article = Article('https://example.com/kakao-new', '새 기사', '신문', time.time(), '설명')
        self.app.events.put(('done', self.app.revision, [('반도체', [article])], []))
        self.app.process_events()
        self.assertEqual(self.app.notifications.status(), [])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = create_root()
        self.app = NewsMonitor(self.root, Path(self.directory.name), demo=True)
        summary_read = patch.object(self.app.summarizer, 'read', side_effect=OSError('offline test'))
        summary_read.start()
        self.addCleanup(summary_read.stop)
        self.root.update()

    def tearDown(self):
        self.app.close()
        # Windows may briefly hold a just-closed directory handle (e.g. a file
        # scanner). Retry cleanup; persistent locks still fail the test.
        for attempt in range(20):
            try:
                self.directory.cleanup()
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in (5, 32) or attempt == 19:
                    raise
                time.sleep(.1)

    def test_summary_worker_updates_open_preview_and_cards_without_blocking(self):
        import threading
        from news_summary import Summary
        self.app.start_summaries()
        self.assertFalse(self.app.summary_busy)  # Demo skips all publisher requests.
        row = self.app.rows[0]
        self.app.preview(row)
        self.app.demo = False
        release, started = threading.Event(), threading.Event()
        def create(item):
            started.set()
            release.wait(5)
            return Summary('본문에서 확인한 핵심 내용입니다.', 'body', '기사 본문 핵심 문장 자동 발췌', item['url'])
        with patch.object(self.app.summarizer, 'create', side_effect=create):
            try:
                self.app.start_summaries()
                self.assertTrue(started.wait(2))
                self.assertTrue(self.app.summary_busy)
                self.root.update_idletasks()
            finally:
                release.set()
            deadline = time.monotonic() + 4
            while self.app.summary_busy and time.monotonic() < deadline:
                time.sleep(.01)
                self.app.process_events()
        self.assertFalse(self.app.summary_busy)
        self.assertEqual(self.app.store.get_summary(row['url'])['summary_basis'], 'body')
        self.assertEqual(self.app.summary_preview[0]['summary'], '본문에서 확인한 핵심 내용입니다.')
        self.assertEqual(self.app.rows[0]['summary_basis'], 'body')
        win = next(child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel))
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        text = next(widget for widget in descendants(win) if isinstance(widget, tk.Text))
        self.assertIn('본문 핵심 요약', text.get('1.0', 'end'))
        self.root.tk.call(win.protocol('WM_DELETE_WINDOW'))
        self.assertIsNone(self.app.summary_preview)

    def test_filter_preview_settings_and_fullscreen(self):
        self.assertEqual(len(self.app.rows), 7)
        self.app.filter_var.set("반도체")
        self.app.select_filter()
        self.assertEqual(len(self.app.rows), 3)
        self.app.preview(self.app.rows[0])
        self.root.update()
        self.assertTrue(self.app.preview_open)
        win = next(child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel))
        win.event_generate("<Escape>")
        self.root.update()
        if win.winfo_exists():
            self.root.tk.call(win.protocol("WM_DELETE_WINDOW"))
        self.assertFalse(self.app.preview_open)
        self.app.settings()
        self.root.update()
        self.assertIsNotNone(self.app.settings_window)
        self.root.tk.call(self.app.settings_window.protocol("WM_DELETE_WINDOW"))
        self.app.toggle_fullscreen()
        self.assertTrue(self.app.fullscreen)
        self.app.exit_fullscreen()
        self.assertFalse(self.app.fullscreen)

    def test_new_result_waits_until_requested(self):
        now = time.time()
        self.app.store.ingest("반도체", [Article("https://example.com/new", "새 기사", "신문", now, "")])
        self.app.reload()
        self.assertEqual(len(self.app.rows), 7)
        self.assertEqual(len(self.app.pending_rows), 8)
        self.app.apply_results()
        self.assertEqual(len(self.app.rows), 8)
        self.assertIsNone(self.app.pending_rows)

    def test_failed_collection_preserves_articles(self):
        self.app.events.put(("done", self.app.revision, [], [("반도체", "연결 실패 / 시간 초과")]))
        self.app.process_events()
        self.assertEqual(len(self.app.rows), 7)
        self.assertIn("수집 실패", self.app.last_status)
        self.assertGreater(self.app.next_fetch, time.monotonic())

    def test_timer_recovers_from_ui_error_and_still_starts_due_collection(self):
        self.app.demo = False
        self.app.kakao_next = float('inf')
        self.app.next_fetch = float('inf')
        with patch.object(self.app.status_label, 'configure', side_effect=tk.TclError('temporary UI error')):
            self.app.tick()
        self.assertIsNotNone(self.app.tick_id)
        # Run the actual scheduled callback, not a manual fetch or tick.
        self.app.next_fetch = 0
        with patch.object(self.app, 'start_fetch') as fetch:
            self.wait_for_layout(1100)
            fetch.assert_called_once()
        self.app.demo = True

    def test_resume_checks_immediately_even_when_monotonic_deadline_is_in_future(self):
        self.app.demo = False
        self.app.kakao_next = float('inf')
        self.app.next_fetch = time.monotonic() + 900
        self.app.last_tick_wall = time.time() - 8 * 3600
        self.app.preview_open = True
        with patch.object(self.app, 'start_fetch') as fetch:
            self.app.tick()
            fetch.assert_called_once()
        self.app.demo = True

    def test_failed_cycle_retries_automatically_and_respects_server_cooldown(self):
        self.app.demo = False
        self.app.kakao_next = float('inf')
        self.app.busy = True
        self.app.events.put(('done', self.app.revision, [], [('반도체', '연결 실패')]))
        with patch('app.time.monotonic', return_value=100):
            self.app.tick()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.next_fetch, 160)
        with patch('app.time.monotonic', return_value=160), patch.object(self.app, 'start_fetch') as fetch:
            self.app.tick()
            fetch.assert_called_once()
        self.app.events.put(('done', self.app.revision, [], [('반도체', '서버 응답 429')], {'cooldown': 300}))
        # Metrics without display details are omitted from this synthetic event.
        with patch('app.time.monotonic', return_value=200):
            with patch.object(self.app, 'reload', side_effect=sqlite3.OperationalError('temporary storage error')):
                self.app.tick()
        self.assertEqual(self.app.next_fetch, 500)
        self.app.demo = True

    def test_done_render_failure_keeps_next_collection_armed(self):
        self.app.busy = True
        self.app.events.put(('done', self.app.revision, [], []))
        with patch.object(self.app, 'reload', side_effect=ValueError('temporary render error')):
            self.app.tick()
        self.assertFalse(self.app.busy)
        self.assertGreater(self.app.next_fetch, time.monotonic())
        self.assertIsNotNone(self.app.tick_id)

    def test_automatic_failed_fetch_then_success_saves_news_without_manual_button(self):
        self.app.demo = False
        self.app.kakao_next = float('inf')
        self.app.next_fetch = 0
        article = Article('https://example.com/automatic-recovery', '반도체 자동 복구', '신문', time.time(), '')
        reports = [CollectionReport([], [('반도체', TimeoutError('offline'))], 1, .1),
                   CollectionReport([('반도체', [article])], [], 1, .1)]
        try:
            with patch.object(self.app.collector, 'collect', side_effect=reports) as collect:
                with patch('app.time.monotonic', return_value=100):
                    self.app.tick()
                    self.app.events.put(self.app.events.get(timeout=2))
                    self.app.tick()
                self.assertEqual(self.app.next_fetch, 160)
                with patch('app.time.monotonic', return_value=160):
                    self.app.tick()
                    self.app.events.put(self.app.events.get(timeout=2))
                    self.app.tick()
                self.assertEqual(collect.call_count, 2)
                self.assertFalse(self.app.busy)
                self.assertFalse(self.app.collection_errors)
                self.assertTrue(any(row['url'] == article.url for row in self.app.store.list_articles(['반도체'])))
        finally:
            self.app.demo = True

    def test_successful_bing_fallback_clears_errors_and_displays_provider_status(self):
        self.app.collection_errors = [('반도체', '서버 응답 503')]
        article = Article('https://example.com/bing', '반도체 새 기사', '신문', time.time(), '설명')
        self.app.events.put(('done', self.app.revision, [('반도체', [article])], [],
                             {'seconds': 1.5, 'requests': 3, 'cooldown': 0, 'provider': 'bing',
                              'fallback_reason': 'Google RSS 연결 실패 · Bing RSS 대체 수집'}))
        self.app.process_events()
        self.app.apply_results()
        self.assertFalse(self.app.collection_errors)
        self.assertIn('수집 정상', self.app.last_status)
        self.assertIn('Bing RSS 대체 수집', self.app.last_status)
        self.assertTrue(any(row['url'] == article.url for row in self.app.rows))

    def test_obsolete_collection_is_ignored(self):
        self.app.events.put(("done", self.app.revision - 1,
                            [("삭제한 키워드", [Article("https://example.com/stale", "기사", "신문", time.time(), "")])], []))
        self.app.process_events()
        self.assertEqual(self.app.store.list_articles(["삭제한 키워드"]), [])

    def test_partial_results_save_early_and_stale_partials_are_ignored(self):
        article = Article('https://example.com/partial', '새 기사', '신문', time.time(), '')
        self.app.events.put(('partial', self.app.revision - 1, '반도체', [article]))
        self.app.process_events()
        self.assertFalse(any(row['url'] == article.url for row in self.app.store.list_articles(['반도체'])))
        self.app.events.put(('partial', self.app.revision, '반도체', [article]))
        self.app.process_events()
        self.assertEqual(self.app.cycle_added, 1)
        self.assertEqual(len(self.app.rows), 7)
        self.assertEqual(len(self.app.pending_rows), 8)
        self.app.events.put(('done', self.app.revision, [('반도체', [article])], [], {'seconds': 1.2, 'requests': 3, 'cooldown': 0}))
        self.app.process_events()
        self.assertIn('새 기사 1건', self.app.last_status)
        self.assertIn('1.2초', self.app.last_status)

    def test_register_keyword_and_restore_after_save(self):
        self.app.settings()
        self.root.update()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        widgets = list(descendants(self.app.settings_window))
        entry = next(widget for widget in widgets if isinstance(widget, tk.Entry))
        entry.insert(0, "로봇")
        next(widget for widget in widgets if isinstance(widget, ThemedButton) and widget.cget("text") == "추가").invoke()
        next(widget for widget in widgets if isinstance(widget, ThemedButton) and widget.cget("text") == "저장하고 적용").invoke()
        self.assertIn("로봇", self.app.active_keywords())
        self.assertIn({"name": "로봇", "enabled": True}, self.app.store.load_config()["keywords"])
        self.assertIsNone(self.app.settings_window)

    def test_cards_fit_small_monitor(self):
        self.app.config["page_size"] = 8
        self.root.geometry("950x720")
        self.root.update()
        self.app.render()
        self.root.update()
        cards = self.app.content.winfo_children()
        self.assertLessEqual(len(cards), 4)
        for card in cards:
            self.assertLessEqual(card.winfo_y() + card.winfo_height(), self.app.content.winfo_height())

    def wait_for_layout(self, milliseconds=250):
        done = tk.BooleanVar(self.root, False)
        self.root.after(milliseconds, lambda: done.set(True))
        self.root.wait_variable(done)
        self.root.update()

    def test_resize_preserves_cards_and_updates_compact_layout(self):
        self.app.config["page_size"] = 3
        self.app.rows[0]["title"] = "창 크기에 따라 줄바꿈과 생략을 확인하는 긴 뉴스 제목 " * 12
        self.app.render()
        self.wait_for_layout()
        cards = self.app.content.winfo_children()
        surfaces = [card.find_withtag("surface") for card in cards]
        wide_title = self.app.title_labels[0].cget("text")
        for geometry in ("1100x820", "950x720", "1280x900"):
            self.root.geometry(geometry)
            self.wait_for_layout()
            self.assertEqual(self.app.content.winfo_children(), cards)
            self.assertEqual([card.find_withtag("surface") for card in cards], surfaces)
            self.assertIsNone(self.app.refresh_id)
            compact = self.app.content.winfo_height() / 3 < 155
            for _, title, description in self.app.card_widgets:
                self.assertEqual(int(title.cget("wraplength")), self.app.content.winfo_width() - 40)
                self.assertEqual(description.winfo_manager(), "" if compact else "pack")
            if geometry == "950x720":
                self.assertTrue(compact)
                self.assertLess(len(self.app.title_labels[0].cget("text")), len(wide_title))
            elif geometry == "1280x900":
                self.assertFalse(compact)

    def test_resize_page_capacity_settles_without_repeated_rendering(self):
        self.app.config["page_size"] = 8
        self.app.render()
        self.wait_for_layout()
        with patch.object(self.app, "render", wraps=self.app.render) as render:
            self.root.geometry("950x720")
            self.wait_for_layout(700)
            self.assertEqual(render.call_count, 1)
            self.assertIsNone(self.app.refresh_id)
            cards = self.app.content.winfo_children()
            self.assertLessEqual(len(cards), 4)
            for card in cards:
                self.assertLessEqual(card.winfo_y() + card.winfo_height(), self.app.content.winfo_height())
            self.wait_for_layout(400)
            self.assertEqual(render.call_count, 1)
            self.assertEqual(self.app.content.winfo_children(), cards)

    def test_manual_pagination_stops_at_boundaries(self):
        self.app.config["page_size"] = 3
        self.app.rows = self.app.rows * 9
        self.app.render()
        self.assertEqual(self.app.page_label.cget("text"), "1 / 21")
        self.assertEqual(self.app.prev_button.cget("state"), "disabled")
        self.app.prev_button.invoke()
        self.app.change_page(-1)
        self.assertEqual(self.app.page, 0)
        self.app.next_button.invoke()
        self.assertEqual(self.app.page, 1)
        self.assertEqual(self.app.prev_button.cget("state"), "normal")
        self.app.prev_button.invoke()
        self.assertEqual(self.app.page, 0)
        self.app.page = 20
        self.app.render()
        self.assertEqual(self.app.next_button.cget("state"), "disabled")
        self.app.next_button.invoke()
        self.app.change_page(1)
        self.assertEqual(self.app.page, 20)

    def test_automatic_pagination_wraps_after_last_page(self):
        self.app.config["page_size"] = 3
        self.app.render()
        self.app.page = self.app.page_count - 2
        self.app.change_page(1, wrap=True)
        self.assertEqual(self.app.page, self.app.page_count - 1)
        self.app.change_page(1, wrap=True)
        self.assertEqual(self.app.page, 0)

    def test_pagination_clamps_when_results_shrink(self):
        self.app.config["page_size"] = 3
        self.app.page = 2
        self.app.pending_rows = self.app.rows[:1]
        self.app.change_page(-1)
        self.assertEqual(self.app.page, 0)
        self.assertEqual(self.app.page_label.cget("text"), "1 / 1")
        self.assertEqual(self.app.prev_button.cget("state"), "disabled")
        self.assertEqual(self.app.next_button.cget("state"), "disabled")

    def test_theme_saved_and_applied_from_settings(self):
        self.app.settings()
        self.root.update()
        self.app.theme_var.set("다크")

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        save = next(w for w in descendants(self.app.settings_window)
                    if isinstance(w, ThemedButton) and w.cget("text") == "저장하고 적용")
        save.invoke()
        self.root.update()
        self.assertEqual(self.app.theme, "dark")
        self.assertEqual(self.app.store.load_config()["theme"], "dark")
        self.assertEqual(self.root.cget("bg"), self.app.colors["bg"])
        self.app.settings()
        self.assertEqual(self.app.theme_var.get(), "다크")
        self.root.tk.call(self.app.settings_window.protocol("WM_DELETE_WINDOW"))

    def test_system_theme_preserves_reader_state(self):
        self.app.config["theme"] = "system"
        self.app.page = 1
        self.app.busy = True
        self.app.pending_rows = list(self.app.rows)
        with patch("ui_theme.system_theme", return_value="dark"):
            self.app.apply_theme()
        self.root.update()
        self.assertEqual(self.app.theme, "dark")
        self.assertEqual(self.app.page, 1)
        self.assertIsNotNone(self.app.pending_rows)
        self.assertEqual(str(self.app.fetch_button.cget("state")), "disabled")
        with patch("ui_theme.system_theme", return_value="light"):
            self.app.apply_theme()
        self.root.update()
        self.assertEqual(self.app.theme, "light")
        self.assertEqual(self.app.page, 1)

    def test_card_geometry_is_consistent_in_both_themes(self):
        for theme in ("light", "dark"):
            self.app.config["theme"] = theme
            self.app.apply_theme()
            self.root.update()
            self.app.render()
            self.root.update()
            sections = [w for w in self.root.winfo_children() if isinstance(w, RoundedPanel)]
            for section in sections:
                self.assertGreaterEqual(section.winfo_height(), section.body.winfo_reqheight() + 40)
            cards = self.app.content.winfo_children()
            for i, card in enumerate(cards):
                self.assertIsInstance(card, RoundedPanel)
                self.assertEqual(card.radius, RADIUS)
                self.assertGreater(card.body.winfo_height(), 40)
                if i:
                    previous = cards[i-1]
                    self.assertEqual(card.winfo_y() - previous.winfo_y() - previous.winfo_height(), GAP)

    def test_modify_advanced_rule_and_clear_old_matches(self):
        self.app.settings()
        self.root.update()
        win = self.app.settings_window

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        widgets = list(descendants(win))
        listing = next(widget for widget in widgets if isinstance(widget, tk.Listbox))
        listing.selection_set(0)
        listing.event_generate("<<ListboxSelect>>")
        self.root.update()
        condition = next(widget for widget in widgets if widget.winfo_name() == "rule_condition")
        condition.insert(0, "반도체 AND (수출 OR 투자) NOT 주가")
        exclude = next(widget for widget in widgets if widget.winfo_name() == "rule_exclude")
        exclude.insert(0, "채용")
        next(widget for widget in widgets if isinstance(widget, ThemedButton) and widget.cget("text") == "저장하고 적용").invoke()
        self.assertEqual(self.app.config["keywords"][0]["condition"], "반도체 AND (수출 OR 투자) NOT 주가")
        self.assertEqual(self.app.config["keywords"][0]["exclude"], "채용")
        self.assertEqual(self.app.store.list_articles(["반도체"]), [])
        self.assertGreater(len(self.app.rows), 0)  # Other keywords keep their articles.
        self.assertEqual(self.app.store.load_config(), self.app.config)

    def test_settings_footer_remains_visible(self):
        self.app.settings()
        self.root.update()
        win = self.app.settings_window
        win.geometry("700x500")
        self.root.update()
        footer = win.winfo_children()[0]
        self.assertLessEqual(footer.winfo_y() + footer.winfo_height(), win.winfo_height())
        self.assertGreater(footer.winfo_height(), 40)

    def group_widgets(self):
        def descend(widget):
            for child in widget.winfo_children():
                yield child
                yield from descend(child)
        return list(descend(self.app.settings_window))

    def click_settings(self, label):
        next(w for w in self.group_widgets() if isinstance(w, (tk.Button, ThemedButton)) and w.cget('text') == label).invoke()
        self.root.update()

    def test_company_batch_register_restore_and_disable(self):
        self.app.settings()
        self.root.update()
        self.click_settings('전력 및 발전 그룹사 일괄 등록')
        self.click_settings('전력 및 발전 그룹사 일괄 등록')  # Idempotent.
        tree = next(w for w in self.group_widgets() if isinstance(w, __import__('tkinter').ttk.Treeview))
        self.assertEqual(len(tree.get_children()), 11)
        tree.selection_set('한국전력')
        tree.event_generate('<<TreeviewSelect>>')
        self.root.update()
        self.click_settings('전체 ON/OFF')
        self.click_settings('저장하고 적용')
        profiles = [x for x in self.app.config['keywords'] if x.get('profile') == 'company_topic']
        self.assertEqual(len(profiles), 22)
        self.assertTrue(all(not x['enabled'] for x in profiles if x['company'] == '한국전력'))
        self.assertEqual(self.app.store.load_config(), self.app.config)
        self.assertIn('한국남동발전 · 공격 유형', self.app.active_keywords())

    def test_custom_company_auto_saved_and_changed_terms_clear_matches(self):
        from security_profiles import COMPANIES, TOPICS, make_entries
        self.app.config['keywords'] += make_entries('한국전력', COMPANIES['한국전력'], TOPICS)
        key = '한국전력 · 공격 유형'
        self.app.store.ingest(key, [Article('https://example.com/company', '한전 해킹', '신문', time.time(), '')])
        self.app.settings()
        self.root.update()
        widgets = self.group_widgets()
        next(w for w in widgets if w.winfo_name() == 'company_name').insert(0, '신규 발전')
        next(w for w in widgets if w.winfo_name() == 'company_aliases').insert(0, '신규 발전 | NEW POWER')
        text = next(w for w in widgets if isinstance(w, tk.Text))
        text.delete('1.0', 'end')
        text.insert('1.0', '해킹 | 랜섬웨어')
        self.click_settings('저장하고 적용')
        self.assertIn('신규 발전 · 공격 유형', self.app.active_keywords())
        self.assertEqual(self.app.store.list_articles([key]), [])
        profiles = [x for x in self.app.config['keywords'] if x.get('profile') == 'company_topic' and x['category'] == '공격 유형']
        self.assertTrue(all(x['terms'] == ['해킹', '랜섬웨어'] for x in profiles))

    def test_import_company_file(self):
        from security_profiles import COMPANIES, TOPICS
        path = Path(self.directory.name) / 'keywords.txt'
        text = '[발전그룹사 키워드]\n' + '\nOR '.join('"' + a + '"' for aliases in COMPANIES.values() for a in aliases)
        for key, terms in TOPICS.items():
            text += '\n[' + key + ']\n' + ' | '.join(terms)
        path.write_text(text, encoding='utf-8-sig')
        self.app.settings()
        self.root.update()
        with patch('group_editor.filedialog.askopenfilename', return_value=str(path)):
            self.click_settings('텍스트 파일 불러오기')
        self.click_settings('저장하고 적용')
        self.assertEqual(len([x for x in self.app.config['keywords'] if x.get('profile') == 'company_topic']), 22)

    def select_company(self, company='한국전력'):
        tree = next(w for w in self.group_widgets()
                    if isinstance(w, tk.ttk.Treeview) and w.winfo_name() != 'monitor_terms')
        tree.selection_set(company)
        tree.event_generate('<<TreeviewSelect>>')
        self.root.update()

    def test_company_category_and_keyword_switches_saved_and_cancelled(self):
        from security_profiles import make_entries
        from search_rules import SearchRule
        self.app.config['keywords'] += make_entries('한국전력', ['한전'],
                                                   {'공격 유형': ['해킹', '랜섬웨어'], '안전 사고': ['화재', '산업재해']})
        self.app.settings()
        self.root.update()
        self.select_company()
        checkbox = next(w for w in self.group_widgets() if w.winfo_name() == 'category_on')
        checkbox.invoke()
        self.root.update()
        terms = next(w for w in self.group_widgets() if w.winfo_name() == 'monitor_terms')
        terms.selection_set('0')
        terms.focus_force()
        self.root.update()
        terms.event_generate('<space>')
        self.root.update()
        self.assertEqual(terms.item('0', 'values'), ('OFF',))
        self.click_settings('저장하고 적용')
        rule = next(x for x in self.app.config['keywords'] if x['name'] == '한국전력 · 공격 유형')
        self.assertFalse(rule['enabled'])
        self.assertEqual(rule['disabled_terms'], ['해킹'])
        self.assertFalse(SearchRule(rule).matches('한전 해킹'))
        self.assertEqual(self.app.store.load_config(), self.app.config)
        self.app.settings()
        self.root.update()
        self.select_company()
        checkbox = next(w for w in self.group_widgets() if w.winfo_name() == 'category_on')
        checkbox.invoke()
        self.click_settings('취소')
        self.assertFalse(rule['enabled'])

    def test_add_category_to_selected_company_and_reopen(self):
        from security_profiles import make_entries
        self.app.config['keywords'] += make_entries('한국전력', ['한전'], {'공격 유형': ['해킹']})
        self.app.settings()
        self.root.update()
        self.select_company()
        self.click_settings('조건·분류 추가')
        widgets = self.group_widgets()
        next(w for w in widgets if w.winfo_name() == 'new_category').insert(0, '안전 사고')
        next(w for w in widgets if w.winfo_name() == 'new_category_terms').insert(0, '화재 | 산업재해')
        self.click_settings('새 분류 추가')
        self.click_settings('그룹사 등록 / 수정')
        self.click_settings('저장하고 적용')
        self.assertIn('한국전력 · 안전 사고', self.app.active_keywords())
        self.app.settings()
        self.root.update()
        self.select_company()
        picker = next(w for w in self.group_widgets() if w.winfo_name() == 'monitor_category')
        self.assertIn('안전 사고', picker.cget('values'))

    def test_single_keyword_cannot_be_switched_off(self):
        from security_profiles import make_entries
        self.app.config['keywords'] += make_entries('한국전력', ['한전'], {'공격 유형': ['해킹']})
        self.app.settings()
        self.root.update()
        self.select_company()
        terms = next(w for w in self.group_widgets() if w.winfo_name() == 'monitor_terms')
        terms.selection_set('0')
        terms.focus_force()
        self.root.update()
        terms.event_generate('<space>')
        self.root.update()
        self.assertEqual(terms.item('0', 'values'), ('ON',))

    def test_group_switches_fit_default_settings_window(self):
        from security_profiles import COMPANIES, TOPICS, make_entries
        self.app.config['keywords'] += [rule for company, aliases in COMPANIES.items()
                                       for rule in make_entries(company, aliases, TOPICS)]
        self.app.settings()
        self.root.update()
        self.select_company()
        terms = next(w for w in self.group_widgets() if w.winfo_name() == 'monitor_terms')
        canvas = terms.master
        while not isinstance(canvas, tk.Canvas):
            canvas = canvas.master
        self.assertLessEqual(terms.winfo_rooty() + terms.winfo_height(),
                             canvas.winfo_rooty() + canvas.winfo_height())
        self.app.settings_window.geometry('700x500')
        self.root.update()
        self.assertLessEqual(terms.winfo_rootx() + terms.winfo_width(),
                             canvas.winfo_rootx() + canvas.winfo_width())

    def test_company_table_fills_panel_and_filter_keeps_detail_in_sync(self):
        from security_profiles import COMPANIES, TOPICS, make_entries
        self.app.config['keywords'] += [rule for company, aliases in COMPANIES.items()
                                       for rule in make_entries(company, aliases, TOPICS)]
        self.app.settings()
        self.root.update()
        tree = next(w for w in self.group_widgets() if w.winfo_name() == 'company_list')
        table = tree.master
        # No unused frame remains under the five-row viewport.
        self.assertLessEqual(table.winfo_height() - tree.winfo_height(), 3)
        self.assertEqual(table.grid_rowconfigure(0)['weight'], 1)
        self.assertTrue(tree.selection())
        terms = next(w for w in self.group_widgets() if w.winfo_name() == 'monitor_terms')
        self.assertTrue(terms.get_children())
        search = next(w for w in self.group_widgets() if w.winfo_name() == 'company_filter')
        search.insert(0, '한전KPS')
        self.root.update()
        self.assertEqual(tree.get_children(), ('한전KPS',))
        self.assertEqual(tree.selection(), ('한전KPS',))
        search.delete(0, 'end')
        search.insert(0, '없는 그룹사')
        self.root.update()
        self.assertFalse(tree.get_children())
        self.assertFalse(terms.get_children())

    def test_group_tables_resize_and_small_window_can_scroll_to_bottom(self):
        self.app.settings()
        self.root.update()
        self.click_settings('전력 및 발전 그룹사 일괄 등록')
        win = self.app.settings_window
        widgets = self.group_widgets()
        tree = next(w for w in widgets if w.winfo_name() == 'company_list')
        terms = next(w for w in widgets if w.winfo_name() == 'monitor_terms')
        canvas = terms.master
        while not isinstance(canvas, tk.Canvas):
            canvas = canvas.master
        heights = []
        for geometry in ('1000x850', '1000x1000', '1000x850'):
            win.geometry(geometry)
            self.root.update()
            heights.append((tree.winfo_height(), terms.winfo_height()))
            self.assertLessEqual(terms.winfo_rooty() + terms.winfo_height(),
                                 canvas.winfo_rooty() + canvas.winfo_height())
        for small, large, restored in zip(heights[0], heights[1], heights[2]):
            self.assertGreater(large, small)
            self.assertEqual(restored, small)
        win.geometry('700x500')
        self.root.update()
        canvas.yview_moveto(1)
        self.root.update()
        self.assertLessEqual(terms.winfo_rooty() + terms.winfo_height(),
                             canvas.winfo_rooty() + canvas.winfo_height())
        save = next(w for w in widgets if isinstance(w, ThemedButton)
                    and w.cget('text') == '저장하고 적용')
        self.assertTrue(save.winfo_ismapped())
        self.assertLessEqual(save.winfo_rooty() + save.winfo_height(),
                             win.winfo_rooty() + win.winfo_height())
        self.click_settings('조건·분류 추가')
        self.click_settings('← 등록 목록 / ON·OFF')
        self.assertAlmostEqual(canvas.yview()[0], 0, places=2)

    def test_terms_scroll_moves_rows_smoothly_and_stays_in_its_panel(self):
        from security_profiles import make_entries
        self.app.config['keywords'] += make_entries('한국전력', ['한전'],
                                                   {'공격 유형': [f'검색어 {i}' for i in range(20)]})
        self.app.settings()
        self.root.update()
        terms = next(w for w in self.group_widgets() if w.winfo_name() == 'monitor_terms')
        self.assertEqual(len(terms.get_children()), 20)

        def wait(ms):
            done = tk.BooleanVar(value=False)
            self.root.after(ms, lambda: done.set(True))
            self.root.wait_variable(done)

        outer = terms.master
        while not isinstance(outer, tk.Canvas):
            outer = outer.master
        page_position = outer.yview()
        header_position = terms.header.winfo_y()
        terms.viewport.event_generate('<MouseWheel>', delta=-120)
        self.assertIsNotNone(terms.timer)
        wait(45)
        intermediate = terms._offset()
        self.assertGreater(intermediate, 0)
        self.assertLess(intermediate, terms.row_height*2)
        self.assertNotEqual(intermediate % terms.row_height, 0)
        wait(220)
        self.assertAlmostEqual(terms._offset(), terms.row_height*2, delta=1)
        self.assertEqual(terms.header.winfo_y(), header_position)
        self.assertEqual(outer.yview(), page_position)
        terms.viewport.event_generate('<Button-1>', x=20, y=terms.row_height//2)
        self.root.update()
        self.assertEqual(terms.item('2', 'values'), ('OFF',))
        terms.viewport.event_generate('<MouseWheel>', delta=-120)
        terms.viewport.event_generate('<MouseWheel>', delta=-120)
        terms.viewport.event_generate('<MouseWheel>', delta=120)
        wait(230)
        self.assertAlmostEqual(terms._offset(), terms.row_height*4, delta=1)
        terms.yview('moveto', 1)
        terms.viewport.event_generate('<MouseWheel>', delta=-120)
        wait(220)
        self.assertLessEqual(terms._offset(), terms._limit()+1)
        terms.viewport.event_generate('<MouseWheel>', delta=120)
        self.assertIsNotNone(terms.timer)
        self.click_settings('취소')
        self.assertIsNone(terms.timer)


if __name__ == "__main__":
    unittest.main()
