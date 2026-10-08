"""Token-only setup with discovered group/channel selection."""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from telegram_notifications import TelegramError


def open_telegram_editor(app):
    if app.telegram_window is not None and app.telegram_window.winfo_exists():
        app.telegram_window.lift()
        return
    c = app.colors
    win = app.telegram_window = tk.Toplevel(app.root)
    app.configure_icon(win)
    win.title('뉴스 모니터 · 텔레그램 자동 공유')
    win.geometry('760x780')
    win.minsize(680, 720)
    win.configure(bg=c['bg'])
    win.transient(app.root)
    card, body = app.surface(win, padding=24)
    card.pack(fill='both', expand=True, padx=20, pady=20)
    app.label(body, '텔레그램 자동 공유', 20).pack(anchor='w')
    instructions = ('1. BotFather에서 만든 봇을 단체방에 초대하세요. 채널은 게시 권한을 주세요.\n'
                    '2. 토큰 입력 후 방 찾기를 누르세요. 방이 없으면 단체방에서 /start@봇이름을\n'
                    '   보내거나 채널에 새 게시물을 올린 뒤 다시 방 찾기를 누르세요.\n'
                    '3. 대상 방을 선택하고 자동 공유 시작을 누르세요. 방 ID 입력은 필요 없습니다.')
    app.label(body, instructions, 10, c['muted'], justify='left', anchor='w',
              wraplength=640).pack(fill='x', pady=(10, 12))
    app.label(body, '봇 토큰 · 저장 후 빈칸은 기존 토큰 사용', 11).pack(anchor='w')
    token = tk.StringVar()
    tk.Entry(body, textvariable=token, show='•', bg=c['input'], fg=c['fg'],
             insertbackground=c['fg'], font=('맑은 고딕', 11), relief='flat').pack(fill='x', ipady=8, pady=6)
    result_queue = queue.Queue()
    busy = [False]
    buttons = []
    feedback = app.label(body, '', 10, c['accent'], justify='left', anchor='w', wraplength=640)
    timer = [None]

    def launch(operation, success):
        if busy[0] or app.demo:
            return
        busy[0] = True
        feedback.configure(text='처리 중입니다…', fg=c['accent'])
        for button in buttons:
            button.configure(state='disabled')
        def worker():
            try:
                operation()
                message = success
            except (ValueError, TelegramError) as exc:
                message = str(exc)
            except Exception:
                message = '처리하지 못했습니다. 연결과 저장소를 확인하세요.'
            result_queue.put(message)
        threading.Thread(target=worker, daemon=True, name='telegram-setup').start()

    def find_rooms():
        supplied = token.get()
        token.set('')
        launch(lambda: app.telegram.discover(supplied), '토큰을 저장했습니다. 대상 방을 선택하세요. 방이 없으면 안내대로 다시 찾으세요.')

    find_button = app.button(body, '토큰 저장 / 방 찾기', find_rooms, True)
    find_button.pack(anchor='w', pady=(4, 12))
    buttons.append(find_button)
    app.label(body, '자동 공유할 단체방 / 채널', 12).pack(anchor='w')
    table = tk.Frame(body, bg=c['panel'])
    table.pack(fill='x', pady=6)
    listing = ttk.Treeview(table, columns=('title', 'kind'), show='headings',
                          height=4, selectmode='browse', style='Company.Treeview')
    listing.heading('title', text='방 이름')
    listing.heading('kind', text='종류')
    listing.column('title', width=430)
    listing.column('kind', width=100)
    listing.pack(side='left', fill='both', expand=True)
    scroll = ttk.Scrollbar(table, orient='vertical', command=listing.yview)
    scroll.pack(side='right', fill='y')
    listing.configure(yscrollcommand=scroll.set)
    status = app.label(body, '', 10, c['muted'], anchor='w', wraplength=640)
    status.pack(fill='x', pady=6)
    actions = tk.Frame(body, bg=c['panel'])
    actions.pack(fill='x', pady=6)

    def activate():
        chosen = listing.selection()
        if not chosen:
            feedback.configure(text='공유할 방을 선택하세요.', fg=c['warning'])
            return
        chat_id = int(chosen[0])
        launch(lambda: app.telegram.activate(chat_id), '자동 공유를 시작했습니다. 최초 연결 이후 발견한 새 기사부터 공유합니다.')

    for title, operation in [('자동 공유 시작', activate),
            ('공유 중지', lambda: launch(app.telegram.pause, '자동 공유를 중지했습니다.')),
            ('테스트 보내기', lambda: launch(lambda: app.telegram.deliver(test=True, stopped=lambda: app.closed), '전송 내역에서 테스트 결과를 확인하세요.')),
            ('실패건 재시도', lambda: launch(app.telegram.retry_failed, '실패건을 대기열에 넣었습니다. 자동 공유 시작을 눌러 재개하세요.'))]:
        button = app.button(actions, title, operation, title == '자동 공유 시작')
        button.pack(side='left', padx=(0, 6))
        buttons.append(button)
    feedback.pack(fill='x', pady=6)
    app.label(body, '프로그램 실행 중에만 공유합니다. 기존 카카오 알림도 계속 동작합니다.\n'
              '결과 불명인 기사는 중복 방지를 위해 자동 재전송하지 않습니다.\n'
              '데모에서는 토큰 확인·방 찾기·발송을 하지 않습니다.', 10, c['muted'],
              justify='left', anchor='w').pack(fill='x', pady=6)
    app.label(body, '최근 전송 내역', 12).pack(anchor='w', pady=(6, 4))
    history = tk.Text(body, height=5, bg=c['input'], fg=c['fg'], font=('맑은 고딕', 10),
                      wrap='word', relief='flat', state='disabled')
    history.pack(fill='both', expand=True)
    snapshot = [None]
    states = {'pending': '대기', 'sending': '전송 중', 'sent': '성공', 'failed': '실패', 'unknown': '결과 불명'}

    def refresh():
        try:
            message = result_queue.get_nowait()
        except queue.Empty:
            message = None
        if message is not None:
            busy[0] = False
            feedback.configure(text=message)
            app.telegram_next = 0
        config, rooms = app.telegram.settings(), app.telegram.chats()
        selected = listing.selection()
        if snapshot[0] != rooms:
            snapshot[0] = rooms
            listing.delete(*listing.get_children())
            for room in rooms:
                listing.insert('', 'end', iid=str(room['id']), values=(room['title'],
                    '채널' if room['kind'] == 'channel' else '단체방'))
            chosen = selected[0] if selected else str(config['chat_id'])
            if listing.exists(chosen):
                listing.selection_set(chosen)
            elif len(rooms) == 1:
                listing.selection_set(str(rooms[0]['id']))
        target = next((r['title'] for r in rooms if r['id'] == config['chat_id']), '미선택')
        status.configure(text=('데모 · 발송 중지' if app.demo else '자동 공유 ' + ('ON' if config['enabled'] else 'OFF')) +
            f" · 봇 @{config['username'] or '미등록'} · 대상 {target}\n" + config['detail'])
        for button in buttons:
            button.configure(state='disabled' if busy[0] or app.demo else 'normal')
        if not config['enabled']:
            buttons[3].configure(state='disabled')
        history.configure(state='normal')
        history.delete('1.0', 'end')
        for row in app.telegram.status():
            history.insert('end', f"{states.get(row['state'], row['state'])} · {row['detail'] or row['url']}\n")
        history.configure(state='disabled')
        timer[0] = win.after(500, refresh)

    def close():
        if timer[0]:
            win.after_cancel(timer[0])
        app.telegram_window = None
        win.destroy()
    app.button(body, '닫기', close).pack(anchor='e', pady=(10, 0))
    win.protocol('WM_DELETE_WINDOW', close)
    win.bind('<Escape>', lambda event: close())
    refresh()
    win.update_idletasks()
    height = max(780, body.winfo_reqheight() + 100)
    win.minsize(680, height)
    win.geometry(f'760x{height}')
