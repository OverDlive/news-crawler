"""Kakao notification editor using the monitor's shared visual system."""
import sqlite3
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from ui_theme import GAP, PADDING


def open_kakao_editor(app):
    if app.kakao_window is not None and app.kakao_window.winfo_exists():
        app.kakao_window.lift()
        return
    c = app.colors
    win = app.kakao_window = tk.Toplevel(app.root)
    win.title('뉴스 모니터 · 카카오 알림')
    win.geometry('980x860')
    win.minsize(860, 700)
    win.configure(bg=c['bg'])
    win.transient(app.root)
    outer = tk.Frame(win, bg=c['bg'])
    outer.pack(fill='both', expand=True, padx=24, pady=20)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(2, weight=1)
    header = tk.Frame(outer, bg=c['bg'])
    header.grid(row=0, column=0, sticky='ew', pady=(0, GAP))
    app.label(header, '카카오 알림', 22).pack(side='left')
    app.label(header, '데모 · 발송 중지' if app.demo else '새 기사 자동 알림', 10,
              c['warning'] if app.demo else c['accent']).pack(side='right')
    app.label(outer, '등록한 사용자에게 새 기사 제목·요약·링크를 나와의 채팅방으로 보냅니다.',
              11, c['muted'], anchor='w').grid(row=1, column=0, sticky='ew', pady=(0, GAP))

    content = tk.Frame(outer, bg=c['bg'])
    content.grid(row=2, column=0, sticky='nsew')
    content.columnconfigure(0, weight=2, minsize=260)
    content.columnconfigure(1, weight=3, minsize=410)
    content.rowconfigure(0, weight=0)
    content.rowconfigure(1, weight=1)
    panel, users_box = app.surface(content, padding=PADDING)
    panel.grid(row=0, column=0, sticky='nsew', padx=(0, GAP), pady=(0, GAP))
    users_box.columnconfigure(0, weight=1)
    users_box.rowconfigure(2, weight=1)
    app.label(users_box, '수신 사용자', 15).grid(row=0, column=0, sticky='w')
    count = app.label(users_box, '', 10, c['muted'])
    count.grid(row=1, column=0, sticky='w', pady=(4, 10))
    table = tk.Frame(users_box, bg=c['panel'])
    table.grid(row=2, column=0, sticky='nsew')
    table.columnconfigure(0, weight=1)
    table.rowconfigure(0, weight=1)
    listing = ttk.Treeview(table, name='kakao_users', columns=('name', 'state'), show='headings',
                          height=5, selectmode='browse', style='Company.Treeview')
    listing.heading('name', text='사용자')
    listing.heading('state', text='수신')
    listing.column('name', width=160, minwidth=100)
    listing.column('state', width=65, minwidth=65, stretch=False, anchor='center')
    listing.grid(row=0, column=0, sticky='nsew')
    scroll = ttk.Scrollbar(table, orient='vertical', command=listing.yview, style='Monitor.Vertical.TScrollbar')
    scroll.grid(row=0, column=1, sticky='ns')
    listing.configure(yscrollcommand=scroll.set)
    empty = app.label(table, '등록된 사용자가 없습니다.\n오른쪽에서 첫 사용자를 추가하세요.',
                      10, c['muted'], justify='center')

    form_panel, form = app.surface(content, padding=PADDING)
    form_panel.grid(row=0, column=1, sticky='nsew', pady=(0, GAP))
    form_title = app.label(form, '새 사용자 등록', 15)
    form_title.pack(anchor='w')
    name, token = tk.StringVar(), tk.StringVar()
    enabled = tk.BooleanVar(value=True)
    for title, variable, masked in [('사용자 이름', name, False), ('액세스 토큰', token, True)]:
        app.label(form, title, 11, c['muted']).pack(anchor='w', pady=(10, 4))
        entry = tk.Entry(form, textvariable=variable, show='•' if masked else '',
                         bg=c['input'], fg=c['fg'], insertbackground=c['fg'],
                         selectbackground=c['hover'], selectforeground=c['fg'], font=('맑은 고딕', 11),
                         relief='flat', bd=0, highlightthickness=1,
                         highlightbackground=c['border'], highlightcolor=c['accent'])
        entry.pack(fill='x', ipady=6)
    token_hint = app.label(form, '카카오 로그인으로 발급받은 토큰을 입력하세요.', 9, c['muted'], anchor='w')
    token_hint.configure(justify='left', wraplength=360)
    token_hint.bind('<Configure>', lambda event: token_hint.configure(wraplength=max(100, event.width)))
    token_hint.pack(fill='x', pady=(4, 6))
    tk.Checkbutton(form, text='새 기사 수신', variable=enabled, bg=c['panel'], fg=c['fg'],
                   selectcolor=c['input'], activebackground=c['panel'], activeforeground=c['fg'],
                   font=('맑은 고딕', 11), relief='flat', bd=0, highlightthickness=0).pack(anchor='w')
    feedback = app.label(form, '', 10, c['accent'], anchor='w')
    feedback.configure(justify='left', wraplength=360)
    feedback.bind('<Configure>', lambda event: feedback.configure(wraplength=max(100, event.width)))
    feedback.pack(fill='x', pady=(4, 0))
    users = []
    test_results = queue.Queue()
    snapshot = [None]
    def selected():
        selection = listing.selection()
        return next((user for user in users if str(user[0]) in selection), None)
    def refresh_users():
        current = listing.selection()
        users[:] = app.notifications.users()
        if snapshot[0] != users:
            snapshot[0] = list(users)
            listing.delete(*listing.get_children())
            for uid, label, active in users:
                listing.insert('', 'end', iid=str(uid), values=(label, 'ON' if active else 'OFF'))
            if current and listing.exists(current[0]):
                listing.selection_set(current)
            if users:
                empty.place_forget()
            else:
                empty.place(relx=.5, rely=.5, anchor='center')
        count.configure(text=f'{len(users)}명 등록 · {sum(bool(u[2]) for u in users)}명 수신 중')
        remove_button.configure(state='normal' if selected() else 'disabled')
        retry_button.configure(state='normal' if selected() and not app.demo else 'disabled')
        refresh_button.configure(state='normal' if selected() else 'disabled')
        test_button.configure(state='normal' if selected() and not app.demo and not app.kakao_busy else 'disabled')
        user = selected()
        if user:
            automatic, expiry, error = app.notifications.refresh_status(user[0])
            status = '자동 갱신 사용 안 함'
            if automatic:
                status = '자동 갱신: 설정 확인 필요' if error else ('자동 갱신: 확인 대기' if not expiry else
                    '자동 갱신 · 만료 ' + time.strftime('%m.%d %H:%M', time.localtime(expiry)))
            renewal_label.configure(text=status, fg=c['warning'] if error else c['muted'])
        else:
            renewal_label.configure(text='사용자를 선택해 자동 갱신을 설정하세요.', fg=c['muted'])
        toggle_button.configure(state='normal' if user else 'disabled',
                                text='수신 OFF로 변경' if user and user[2] else '수신 ON으로 변경')
    def select(event=None):
        user = selected()
        if user:
            name.set(user[1]); enabled.set(bool(user[2])); token.set('')
            form_title.configure(text='사용자 수정')
            token_hint.configure(text='비워 두면 기존 토큰을 유지합니다. 만료 시 교체하세요.')
        refresh_users()
    def clear():
        listing.selection_remove(*listing.selection())
        name.set(''); token.set(''); enabled.set(True)
        form_title.configure(text='새 사용자 등록')
        token_hint.configure(text='카카오 로그인으로 발급받은 토큰을 입력하세요.')
        feedback.configure(text='')
        refresh_users()
    def save():
        user = selected()
        if user and name.get().strip() != user[1]:
            feedback.configure(text='이름 변경은 새 사용자 등록에서 진행하세요.', fg=c['warning'])
            return
        try:
            app.notifications.save_user(name.get(), token.get(), enabled.get())
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showwarning('카카오 알림', str(exc), parent=win)
            return
        saved_name = name.get().strip()
        token.set('')
        refresh_users()
        uid = next(user[0] for user in users if user[1] == saved_name)
        listing.selection_set(str(uid))
        select()
        feedback.configure(text='저장했습니다. 등록 이후 발견한 새 기사부터 적용됩니다.', fg=c['accent'])
        app.kakao_next = 0
    def remove():
        user = selected()
        if user:
            app.notifications.remove(user[0])
            clear()
            feedback.configure(text='사용자를 삭제했습니다.', fg=c['accent'])
    def toggle_receiving():
        user = selected()
        if not user:
            return
        active = not bool(user[2])
        try:
            app.notifications.set_enabled(user[0], active)
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showwarning('카카오 알림', str(exc), parent=win)
            return
        enabled.set(active)
        refresh_users()
        feedback.configure(text=f'{user[1]} · 수신 {"ON" if active else "OFF"}로 저장했습니다.', fg=c['accent'])
        app.kakao_next = 0
    def toggle_cell(event):
        if listing.identify_region(event.x, event.y) != 'cell' or listing.identify_column(event.x) != '#2':
            return
        uid = listing.identify_row(event.y)
        if uid:
            listing.selection_set(uid)
            listing.focus(uid)
            toggle_receiving()
            return 'break'
    def retry():
        user = selected()
        if user and not app.demo:
            if not user[2]:
                feedback.configure(text='수신을 켜고 저장한 뒤 다시 시도하세요.', fg=c['warning'])
                return
            app.notifications.retry(user[0])
            app.kakao_next = 0
            feedback.configure(text='실패한 전송을 다시 대기열에 넣었습니다.', fg=c['accent'])
    listing.bind('<<TreeviewSelect>>', select)
    listing.bind('<Button-1>', toggle_cell)
    actions = tk.Frame(form, bg=c['panel'])
    actions.pack(fill='x', pady=(8, 0))
    app.button(actions, '등록 / 수정', save, True).pack(side='left', padx=(0, 8))
    app.button(actions, '새 사용자', clear).pack(side='left')
    def send_test():
        user = selected()
        if not user or app.demo or app.kakao_busy:
            return
        if not user[2]:
            feedback.configure(text='새 기사 수신을 켜고 저장한 뒤 테스트하세요.', fg=c['warning'])
            return
        articles = app.store.list_articles(app.active_keywords(), app.config['hours'])
        url = articles[0]['url'] if articles else 'https://news.google.com'
        app.kakao_busy = True
        test_button.configure(state='disabled')
        feedback.configure(text=f'{user[1]} · 테스트 알림을 전송 중입니다…', fg=c['accent'])
        def worker():
            try:
                state, detail = app.notifications.send_test(user[0], url, stopped=lambda: app.closed)
            except Exception:
                state, detail = 'failed', '테스트 전송 처리 실패: 설정과 저장소를 확인하세요'
            finally:
                app.kakao_next = time.monotonic() + 30
                app.kakao_busy = False
            test_results.put((user[1], state, detail))
        threading.Thread(target=worker, daemon=True, name='kakao-test').start()
    test_button = app.button(form, '즉시 알람 보내기', send_test, True)
    test_button.pack(anchor='w', pady=(8, 0))
    app.label(form, '선택한 사용자에게 테스트 메시지 1건을 보냅니다. 저장한 설정을 사용합니다.',
              9, c['muted'], anchor='w', wraplength=360).pack(fill='x', pady=(4, 0))
    app.label(form, '토큰 소유자의 나와의 채팅방에 전송됩니다. 팝업·소리 알림은 제공되지 않습니다.',
              9, c['warning'], anchor='w', wraplength=360).pack(fill='x', pady=(4, 0))
    toggle_button = app.button(users_box, '수신 ON으로 변경', toggle_receiving)
    toggle_button.grid(row=3, column=0, sticky='ew', pady=(8, 0))
    user_actions = tk.Frame(users_box, bg=c['panel'])
    user_actions.grid(row=4, column=0, sticky='ew', pady=(8, 0))
    remove_button = app.button(user_actions, '선택 삭제', remove)
    remove_button.pack(side='left')
    def refresh_dialog():
        user = selected()
        if not user:
            return
        popup = tk.Toplevel(win)
        popup.title('카카오 알림 · 자동 갱신 설정')
        popup.transient(win)
        popup.configure(bg=c['bg'])
        popup.geometry('620x800')
        popup.minsize(580, 760)
        card, body = app.surface(popup, padding=PADDING)
        card.pack(fill='both', expand=True, padx=20, pady=20)
        app.label(body, user[1] + ' · 자동 갱신', 16).pack(anchor='w')
        active, _, error = app.notifications.refresh_status(user[0])
        use_refresh = tk.BooleanVar(value=True)
        tk.Checkbutton(body, text='액세스 토큰 만료 10분 전에 자동 갱신', variable=use_refresh,
            bg=c['panel'], fg=c['fg'], selectcolor=c['input'], activebackground=c['panel'],
            activeforeground=c['fg'], font=('맑은 고딕', 11)).pack(anchor='w', pady=(10, 8))
        app.label(body, '같은 카카오 앱에서 발급한 키와 리프레시 토큰이 필요합니다.\n기존 설정은 빈칸으로 두면 유지됩니다. 체크 해제 후 저장하면 삭제됩니다.',
                  10, c['muted'], justify='left', anchor='w').pack(fill='x')
        values = []
        for title in ('REST API 키', '리프레시 토큰', '클라이언트 시크릿 (앱에서 활성화한 경우 필수)'):
            app.label(body, title, 11, c['muted']).pack(anchor='w', pady=(12, 4))
            variable = tk.StringVar()
            values.append(variable)
            tk.Entry(body, textvariable=variable, show='•', bg=c['input'], fg=c['fg'],
                insertbackground=c['fg'], font=('맑은 고딕', 11), relief='flat', bd=0,
                highlightthickness=1, highlightbackground=c['border'], highlightcolor=c['accent']).pack(fill='x', ipady=6)
        app.label(body, error or '저장 후 첫 갱신으로 만료 시간을 확인합니다.\n프로그램 실행 중 갱신하며, 새 리프레시 토큰도 자동 저장합니다.',
                  10, c['warning'] if error else c['muted'], anchor='w', justify='left', wraplength=500).pack(fill='x', pady=12)
        def save_refresh():
            try:
                app.notifications.save_refresh(user[0], *(value.get() for value in values), enabled=use_refresh.get())
            except (ValueError, OSError, sqlite3.Error) as exc:
                messagebox.showwarning('자동 갱신', str(exc), parent=popup)
                return
            for value in values:
                value.set('')
            app.kakao_next = 0
            popup.destroy()
            feedback.configure(text='자동 갱신 설정을 저장했습니다.', fg=c['accent'])
            refresh_users()
        actions = tk.Frame(body, bg=c['panel'])
        actions.pack(fill='x')
        app.button(actions, '저장', save_refresh, True).pack(side='right')
        app.button(actions, '취소', popup.destroy).pack(side='right', padx=8)
        popup.update_idletasks()
        popup.minsize(580, max(760, body.winfo_reqheight() + 100))
        popup.bind('<Escape>', lambda event: popup.destroy())
    refresh_button = app.button(user_actions, '자동 갱신', refresh_dialog)
    refresh_button.pack(side='right')
    renewal_label = app.label(users_box, '', 9, c['muted'], anchor='w')
    renewal_label.grid(row=5, column=0, sticky='ew', pady=(8, 0))
    app.label(users_box, '목록의 ON / OFF를 클릭하면 바로 저장됩니다.', 9,
              c['muted'], anchor='w').grid(row=6, column=0, sticky='ew', pady=(4, 0))

    log_panel, log = app.surface(content, padding=PADDING)
    log_panel.grid(row=1, column=0, columnspan=2, sticky='nsew')
    log.columnconfigure(0, weight=1)
    log.rowconfigure(2, weight=1)
    app.label(log, '전송 내역', 15).grid(row=0, column=0, sticky='w')
    retry_button = app.button(log, '선택 실패건 재시도', retry)
    retry_button.grid(row=0, column=1, sticky='e')
    app.label(log, '실패 시 해당 사용자 수신이 중지됩니다. 결과 불명 건은 카카오톡 확인 후 재시도하세요.',
              10, c['muted'], anchor='w').grid(row=1, column=0, columnspan=2, sticky='ew', pady=(4, 10))
    history_box = tk.Frame(log, bg=c['panel'])
    history_box.grid(row=2, column=0, columnspan=2, sticky='nsew')
    history_box.columnconfigure(0, weight=1)
    history_box.rowconfigure(0, weight=1)
    history = tk.Text(history_box, name='kakao_history', height=5, state='disabled', wrap='word',
                      bg=c['input'], fg=c['fg'], font=('맑은 고딕', 10), relief='flat', bd=0,
                      padx=12, pady=10, highlightthickness=1, highlightbackground=c['border'])
    history.grid(row=0, column=0, sticky='nsew')
    scroll = ttk.Scrollbar(history_box, orient='vertical', command=history.yview, style='Monitor.Vertical.TScrollbar')
    scroll.grid(row=0, column=1, sticky='ns')
    history.configure(yscrollcommand=scroll.set)
    states = {'pending': '대기', 'sending': '전송 중', 'sent': 'API 전송 성공', 'failed': '실패', 'unknown': '결과 불명'}
    last_history = [None]
    timer = [None]
    def update_history():
        if app.closed or not win.winfo_exists():
            return
        refresh_users()
        while not test_results.empty():
            label, state, detail = test_results.get_nowait()
            feedback.configure(text=f'{label} · {detail}' + (' · 나와의 채팅방을 확인하세요.' if state == 'sent' else ''),
                               fg=c['accent'] if state == 'sent' else c['warning'])
        rows = app.notifications.status()
        for uid, label, _ in users:
            _, _, error = app.notifications.refresh_status(uid)
            if error:
                rows.insert(0, (label, '갱신 보류', error))
        if rows != last_history[0]:
            last_history[0] = rows
            history.configure(state='normal')
            history.delete('1.0', 'end')
            if not rows:
                history.insert('end', '아직 전송 내역이 없습니다. 새 기사를 발견하면 여기에 표시됩니다.')
            for label, state, detail in rows:
                history.insert('end', f'{label}  ·  {states.get(state, state)}  ·  {detail}\n')
            history.configure(state='disabled')
        timer[0] = win.after(2000, update_history)
    def help_dialog():
        messagebox.showinfo('카카오 알림 설정 안내',
            '각 사용자가 카카오 로그인에서 메시지 전송(talk_message)에 동의한 액세스 토큰이 필요합니다.\n\n'
            '카카오 앱의 제품 링크 관리에 기사 웹 도메인을 등록하세요.\n'
            '사용자를 선택하고 자동 갱신에서 REST API 키·리프레시 토큰을 등록하세요.\n'
            '자동 갱신 미설정 또는 리프레시 토큰 만료 시 새 토큰이 필요합니다.\n\n'
            '프로그램이 실행 중일 때만 전송합니다. 데모에서는 발송하지 않습니다.', parent=win)
    def close():
        if timer[0]:
            win.after_cancel(timer[0])
        app.kakao_window = None
        win.destroy()
    footer = tk.Frame(outer, bg=c['bg'])
    footer.grid(row=3, column=0, sticky='ew', pady=(GAP, 0))
    app.button(footer, '설정 안내', help_dialog).pack(side='left')
    app.button(footer, '닫기', close).pack(side='right')
    win.protocol('WM_DELETE_WINDOW', close)
    win.bind('<Escape>', lambda event: close())
    update_history()
    win.update_idletasks()
    # RoundedPanel is a canvas: reserve its body's requested height explicitly.
    form_height = form.winfo_reqheight() + PADDING * 2 + GAP + 10
    content.rowconfigure(0, minsize=form_height)
    log_height = log.winfo_reqheight() + PADDING * 2
    content.rowconfigure(1, minsize=log_height)
    minimum = header.winfo_reqheight() + form_height + log_height + footer.winfo_reqheight() + 100
    win.minsize(860, minimum)
    win.geometry(f'980x{max(860, minimum)}')
