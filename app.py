"""Keyword news monitor. Run: python app.py"""
from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import sqlite3
import threading
import time
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont
import urllib.error
import webbrowser

from news_core import Article, Store, valid_link
from news_collection import CollectionReport, NewsCollector, ServerCoolingDown
from news_summary import ArticleSummarizer
from kakao_notifications import Notifications
from telegram_notifications import TelegramNotifications
from tk_runtime import create_root
from app_updates import app_directory, apply_pending, UpdateChecker
from version import VERSION
from search_rules import RuleError, SearchRule
from group_editor import build_group_editor
from ui_theme import GAP, PADDING, PALETTES, THEME_LABELS, RoundedPanel, ThemedButton, TextHint, resolve_theme

FONT = "맑은 고딕"


def timestamp(value):
    return time.strftime("%m.%d %H:%M", time.localtime(value)) if value else "아직 없음"


def age(value):
    minutes = max(0, int((time.time() - value) / 60))
    if minutes < 1:
        return "방금 전"
    if minutes < 60:
        return f"{minutes}분 전"
    if minutes < 1440:
        return f"{minutes // 60}시간 전"
    return f"{minutes // 1440}일 전"


class NewsMonitor:
    def __init__(self, root, directory, demo=False):
        self.root = root
        self.store = Store(directory)
        self.config = self.store.load_config()
        self.theme = resolve_theme(self.config["theme"])
        self.colors = PALETTES[self.theme]
        self.demo = demo
        self.updates = UpdateChecker()
        self.notifications = Notifications(directory)
        self.kakao_busy = False
        self.kakao_window = None
        self.kakao_next = 0
        self.telegram = TelegramNotifications(directory)
        self.telegram_window = None
        self.telegram_busy = False
        self.telegram_next = 0
        self.events = queue.Queue()
        self.collector = NewsCollector(fallback=True)
        self.summarizer = ArticleSummarizer()
        self.summary_busy = False
        self.summary_preview = None
        self.summary_stop = threading.Event()
        self.cycle_added = 0
        self.cycle_stored = set()
        self.cycle_storage_errors = []
        self.busy = False
        self.closed = False
        self.revision = 0
        self.next_fetch = time.monotonic()
        self.last_tick_wall = time.time()
        self.last_rotate = time.monotonic()
        self.page = 0
        self.selected = "전체"
        self.rows = []
        self.pending_rows = None
        self.preview_open = False
        self.settings_window = None
        self.last_status = "키워드를 등록하면 뉴스 수집을 시작합니다."
        self.collection_errors = []
        self.fullscreen = False
        self.tick_id = None
        self.refresh_id = None
        self.last_size = None
        root.title(f"뉴스 모니터 · 키워드 대시보드 · v{VERSION}" + (" · 데모" if demo else ""))
        root.geometry("1280x900")
        root.minsize(950, 720)
        root.configure(bg=self.colors["bg"])
        self.configure_styles()
        root.bind("<F11>", lambda e: self.toggle_fullscreen())
        root.bind("<Escape>", lambda e: self.exit_fullscreen())
        root.bind("<Left>", lambda e: self.change_page(-1))
        root.bind("<Right>", lambda e: self.change_page(1))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        if demo:
            self.seed_demo()
        self.reload(force=True)
        logging.info("News monitor started; %d active keywords, interval=%d minutes, demo=%s",
                     len(self.active_keywords()), self.config['interval_minutes'], self.demo)
        self.tick()
        if self.store.warning:
            messagebox.showwarning("설정 복구", self.store.warning, parent=root)

    def label(self, parent, text="", size=12, color=None, **kwargs):
        adaptive = len(text) > 45 and "wraplength" not in kwargs
        if adaptive:
            kwargs.update(wraplength=700, justify=kwargs.get('justify', 'left'))
        widget = tk.Label(parent, text=text, font=(FONT, size), fg=color or self.colors["fg"], bg=parent.cget("bg"), **kwargs)
        if adaptive:
            widget.bind('<Configure>', lambda e: widget.configure(wraplength=max(100, e.width)))
        return widget

    def configure_styles(self):
        c = self.colors
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=c["input"], background=c["panel"],
                        foreground=c["fg"], arrowcolor=c["muted"], bordercolor=c["border"],
                        lightcolor=c["input"], darkcolor=c["input"], arrowsize=14,
                        relief="flat", borderwidth=1, padding=(12, 9))
        style.map("TCombobox", fieldbackground=[("readonly", c["input"])],
                  foreground=[("readonly", c["fg"])], selectbackground=[("readonly", c["input"])],
                  selectforeground=[("readonly", c["fg"])],
                  background=[("active", c["hover"]), ("readonly", c["input"])],
                  bordercolor=[("focus", c["accent"]), ("!focus", c["border"])],
                  lightcolor=[("readonly", c["input"])], darkcolor=[("readonly", c["input"])])
        style.configure("Monitor.Vertical.TScrollbar", background=c["border"], troughcolor=c["panel"],
                        bordercolor=c["panel"], lightcolor=c["border"], darkcolor=c["border"],
                        relief="flat", borderwidth=0, width=10, arrowsize=0)
        style.layout("Monitor.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {
            "sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"sticky": "nswe", "expand": True})]})])
        style.map("Monitor.Vertical.TScrollbar", background=[("pressed", c["accent"]), ("active", c["muted"])])
        style.configure("Monitor.Horizontal.TScrollbar", background=c['border'], troughcolor=c['panel'],
                        bordercolor=c['panel'], lightcolor=c['border'], darkcolor=c['border'], arrowsize=0)
        style.layout("Monitor.Horizontal.TScrollbar", [('Horizontal.Scrollbar.trough', {'sticky':'ew', 'children':[
            ('Horizontal.Scrollbar.thumb', {'sticky':'nswe', 'expand': True})]})])
        style.configure("Monitor.Popup.TFrame", background=c["panel"], bordercolor=c["border"],
                        lightcolor=c["border"], darkcolor=c["border"], relief="solid", borderwidth=1)
        style.configure("Monitor.TNotebook", background=c["panel"], bordercolor=c["panel"],
                        lightcolor=c["panel"], darkcolor=c["panel"], borderwidth=0, tabmargins=(0, 0, 0, 8))
        style.configure("Monitor.TNotebook.Tab", background=c["input"], foreground=c["muted"],
                        bordercolor=c["panel"], lightcolor=c["panel"], darkcolor=c["panel"],
                        font=(FONT, 11), padding=(16, 10))
        style.map("Monitor.TNotebook.Tab", background=[("selected", c["hover"]), ("active", c["input"])],
                  foreground=[("selected", c["accent"]), ("active", c["fg"])],
                  bordercolor=[("selected", c["panel"])], lightcolor=[("selected", c["panel"])],
                  darkcolor=[("selected", c["panel"])])
        style.configure("Monitor.TSpinbox", fieldbackground=c["input"], foreground=c["fg"],
                        background=c["input"], arrowcolor=c["muted"], bordercolor=c["border"],
                        lightcolor=c["input"], darkcolor=c["input"], padding=(10, 7), relief="flat")
        style.map("Monitor.TSpinbox", bordercolor=[("focus", c["accent"])], background=[("active", c["hover"])])
        row_height = tkfont.Font(root=self.root, family=FONT, size=11).metrics("linespace") + 18
        for name in ("Treeview", "Company.Treeview"):
            style.configure(name, background=c["panel"], fieldbackground=c["panel"],
                            foreground=c["fg"], bordercolor=c["border"],
                            lightcolor=c["panel"], darkcolor=c["panel"], borderwidth=0,
                            relief="flat", rowheight=row_height, font=(FONT, 11))
            style.map(name, background=[("selected", c["hover"])],
                      foreground=[("selected", c["accent"])])
            style.configure(name + ".Heading", background=c["input"], foreground=c["muted"],
                            bordercolor=c["input"], lightcolor=c["input"], darkcolor=c["input"],
                            relief="flat", font=(FONT, 11, "bold"), padding=(12, 10))
            style.map(name + ".Heading", background=[("active", c["hover"])],
                      foreground=[("active", c["fg"])])
        style.layout("Company.Treeview", [("Treeview.padding", {"sticky": "nswe", "children": [
            ("Treeview.treearea", {"sticky": "nswe"})]})])
        self.root.option_add("*TCombobox*Listbox.background", c["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["hover"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["accent"])
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 12))
        self.root.option_add("*TCombobox*Listbox.relief", "flat")
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)
        self.root.option_add("*TCombobox*Listbox.selectBorderWidth", 0)

    def combobox(self, parent, **kwargs):
        widget = ttk.Combobox(parent, **kwargs)
        def prepare_popup():
            popup = self.root.tk.call("ttk::combobox::PopdownWindow", str(widget))
            self.root.tk.call(str(popup) + ".f", "configure", "-style", "Monitor.Popup.TFrame")
            self.root.tk.call(str(popup) + ".f.sb", "configure", "-style", "Monitor.Vertical.TScrollbar")
            self.root.tk.call(str(popup) + ".f.l", "configure", "-highlightthickness", 0,
                              "-background", self.colors["panel"], "-foreground", self.colors["fg"],
                              "-selectbackground", self.colors["hover"], "-selectforeground", self.colors["accent"])
            font = tkfont.Font(root=self.root, font=(FONT, 12))
            longest = max((font.measure(value) for value in widget.cget('values')), default=0)
            popup_width = min(widget.winfo_screenwidth() - 40, max(widget.winfo_width(), longest + 48))
            horizontal = str(popup) + '.f.hs'
            if longest + 48 > popup_width:
                if not self.root.tk.call('winfo', 'exists', horizontal):
                    self.root.tk.call('ttk::scrollbar', horizontal, '-orient', 'horizontal',
                                      '-style', 'Monitor.Horizontal.TScrollbar', '-command', str(popup) + '.f.l xview')
                self.root.tk.call('grid', horizontal, '-row', 1, '-column', 0, '-sticky', 'ew')
                self.root.tk.call(str(popup) + '.f.l', 'configure', '-xscrollcommand', horizontal + ' set')
            elif self.root.tk.call('winfo', 'exists', horizontal):
                self.root.tk.call('grid', 'remove', horizontal)
            self.root.tk.call('wm', 'minsize', popup, popup_width, 1)
            def place():
                if not widget.winfo_exists():
                    return
                height = self.root.tk.call('winfo', 'height', popup)
                x = max(10, min(widget.winfo_rootx(), widget.winfo_screenwidth() - popup_width - 20))
                y = self.root.tk.call('winfo', 'rooty', popup)
                self.root.tk.call('wm', 'geometry', popup, f'{popup_width}x{height}+{x}+{y}')
            widget.after_idle(place)
        widget.configure(postcommand=prepare_popup)
        TextHint(widget, lambda x, y: widget.get(), self.colors)
        return widget

    def apply_theme(self):
        theme = resolve_theme(self.config["theme"])
        if theme == self.theme:
            return
        self.theme = theme
        self.colors = PALETTES[theme]
        if self.refresh_id:
            self.root.after_cancel(self.refresh_id)
            self.refresh_id = None
        self.last_size = None
        for child in self.root.winfo_children():
            if not isinstance(child, tk.Toplevel):
                child.destroy()
        self.root.configure(bg=self.colors["bg"])
        self.configure_styles()
        self.build()
        self.filter["values"] = ["전체", *self.active_keywords()]
        self.filter_var.set(self.selected)
        self.render()

    def surface(self, parent, **kwargs):
        panel = RoundedPanel(parent, fill=self.colors["panel"], border=self.colors["border"], **kwargs)
        return panel, panel.body

    def button(self, parent, text, command, accent=False):
        return ThemedButton(parent, text, command, self.colors, accent)

    def flow_buttons(self, parent):
        widgets = parent.winfo_children()
        for widget in widgets:
            widget.pack_forget()
        previous = [None]
        def arrange(event):
            widest = max((widget.winfo_reqwidth() for widget in widgets), default=1) + 10
            columns = max(1, min(len(widgets), event.width // widest))
            if columns == previous[0]:
                return
            previous[0] = columns
            for index, widget in enumerate(widgets):
                widget.grid(row=index // columns, column=index % columns, sticky='w', padx=(0, 8), pady=3)
        parent.bind('<Configure>', arrange)
        for index, widget in enumerate(widgets):
            widget.grid(row=index // 3, column=index % 3, sticky='w', padx=(0, 8), pady=3)

    def build(self):
        c = self.colors
        header = tk.Frame(self.root, bg=c["bg"])
        header.pack(fill="x", padx=28, pady=(24, 20))
        left = tk.Frame(header, bg=c["bg"])
        left.pack(side="left")
        self.label(left, "N E W S R O O M  /  LIVE FEED", 10, c["accent"]).pack(anchor="w")
        self.label(left, "관심 뉴스, 한눈에", 26).pack(anchor="w", pady=(4, 6))
        self.label(left, "키워드로 모으고, 필요한 소식에 집중하세요." +
                   ("  ·  데모" if self.demo else ""), 11, c["muted"]).pack(anchor="w")
        actions = tk.Frame(header, bg=c["bg"])
        actions.pack(side="right", anchor="center")
        self.button(actions, "키워드 / 설정", self.settings).pack(side="right", padx=(GAP, 0))
        self.button(actions, "카카오 알림", self.kakao_settings).pack(side="right", padx=(GAP, 0))
        self.button(actions, "텔레그램 공유", self.telegram_settings).pack(side="right", padx=(GAP, 0))
        self.button(actions, "전체 화면", self.toggle_fullscreen).pack(side="right", padx=(GAP, 0))
        self.fetch_button = self.button(actions, "확인 중…" if self.busy else "지금 확인", self.manual_fetch, True)
        self.fetch_button.pack(side="right")
        self.fetch_button.configure(state="disabled" if self.busy else "normal")

        section, toolbar = self.surface(self.root, padding=PADDING)
        section.pack(fill="x", padx=28, pady=(0, GAP))
        toolbar.columnconfigure(1, weight=1)
        self.label(toolbar, "뉴스 피드", 16).grid(row=0, column=0, sticky='w', padx=(0, 20))
        self.filter_var = tk.StringVar(value=self.selected)
        self.filter = self.combobox(toolbar, textvariable=self.filter_var, state="readonly", width=17, font=(FONT, 11))
        self.filter.grid(row=0, column=1, sticky='ew', padx=(0, 16))
        self.filter.bind("<<ComboboxSelected>>", lambda e: self.select_filter())
        self.count_label = self.label(toolbar, "", 10, c["muted"])
        self.count_label.grid(row=1, column=0, sticky='w', pady=(10, 0))
        self.apply_button = self.button(toolbar, "새 결과 보기", self.apply_results)
        self.apply_button.grid(row=1, column=2, sticky='e', pady=(10, 0))
        self.apply_button.configure(state="disabled")
        self.rotate_var = tk.BooleanVar(value=self.config["auto_rotate"])
        tk.Checkbutton(toolbar, text="자동 넘김", variable=self.rotate_var,
                       command=self.set_rotation, bg=c["panel"], fg=c["fg"], selectcolor=c["input"],
                       activebackground=c["panel"], activeforeground=c["fg"], font=(FONT, 10)).grid(row=0, column=2, sticky='e')
        self.feed_name = self.label(toolbar, self.selected, 10, c['muted'], anchor='w', justify='left', wraplength=600)
        self.feed_name.grid(row=1, column=1, sticky='ew', padx=16, pady=(10, 0))
        self.feed_name.bind('<Configure>', lambda e: self.feed_name.configure(wraplength=max(100, e.width)))

        self.content = tk.Frame(self.root, bg=c["bg"])
        self.content.pack(fill="both", expand=True, padx=28)
        # The viewport is sized by the window, not by the cards inside it.
        self.content.pack_propagate(False)
        self.card_rows = None
        self.card_widgets = []
        section, footer = self.surface(self.root)
        section.pack(fill="x", padx=28, pady=(GAP, 24))
        self.status_label = self.label(footer, "", 10, c["muted"], anchor="w", justify="left")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.next_button = self.button(footer, "다음 →", lambda: self.change_page(1))
        self.next_button.pack(side="right")
        self.page_label = self.label(footer, "1 / 1", 11)
        self.page_label.pack(side="right", padx=16)
        self.prev_button = self.button(footer, "← 이전", lambda: self.change_page(-1))
        self.prev_button.pack(side="right")
        self.content.bind("<Configure>", self.resize_cards)

    def active_keywords(self):
        return [entry["name"] for entry in self.config["keywords"] if entry["enabled"]]

    def reload(self, force=False):
        active = self.active_keywords()
        self.filter["values"] = ["전체", *active]
        if self.selected not in ["전체", *active]:
            self.selected = "전체"
            self.filter_var.set("전체")
        self.feed_name.configure(text='전체 뉴스' if self.selected == '전체' else self.selected)
        keywords = active if self.selected == "전체" else [self.selected]
        fresh = self.store.list_articles(keywords, self.config["hours"])
        # New arrivals are committed at a page boundary, so readers keep their place.
        if force or not self.rows:
            self.rows = fresh
            self.pending_rows = None
            self.render()
        elif fresh != self.rows:
            self.pending_rows = fresh
            self.apply_button.configure(state="normal")

    def apply_results(self):
        self.page = 0
        self.last_rotate = time.monotonic()
        self.reload(force=True)

    def render(self, *, resize=False):
        self.apply_button.configure(state="normal" if self.pending_rows is not None else "disabled")
        self.apply_button.configure(bg=self.colors["accent"] if self.pending_rows is not None else self.colors["input"],
                                    fg=self.colors["on_accent"] if self.pending_rows is not None else self.colors["fg"])
        height = max(1, self.content.winfo_height())
        if height < 50:
            height = max(300, self.root.winfo_height() - 330)
        # Keep every card readable on smaller monitors, even with a high setting.
        size = min(self.config["page_size"], max(3, int(height / 130)))
        pages = max(1, (len(self.rows) + size - 1) // size)
        self.page = max(0, min(self.page, pages - 1))
        self.page_label.configure(text=f"{self.page + 1} / {pages}")
        self.prev_button.configure(state="normal" if self.page > 0 else "disabled")
        self.next_button.configure(state="normal" if self.page < pages - 1 else "disabled")
        self.page_count = pages
        self.count_label.configure(text=f"최근 {self.config['hours']}시간 · {len(self.rows)}건")
        width = max(300, self.content.winfo_width() - PADDING * 2)
        compact = height / size < 155
        title_size = 14 if compact else 17
        visible_rows = self.rows[self.page * size:(self.page + 1) * size]
        if resize and visible_rows == self.card_rows:
            self.layout_cards(width, compact, title_size)
            return
        for child in self.content.winfo_children():
            child.destroy()
        self.title_labels = []
        self.card_rows = visible_rows
        self.card_widgets = []
        if not self.rows:
            surface, empty = self.surface(self.content)
            surface.pack(fill="both", expand=True)
            if not self.active_keywords():
                title, detail = "관심 키워드를 등록해 주세요", "상단의 ‘키워드 / 설정’에서 키워드를 추가하면 자동 수집합니다."
            elif self.collection_errors:
                title = "뉴스 수집 서버에 연결하지 못했습니다"
                detail = "현재 조건에 맞는 뉴스가 없는 것으로 확인된 상태가 아닙니다. 연결 복구 후 자동으로 다시 확인합니다."
            else:
                title = "표시할 뉴스가 없습니다"
                detail = f"최근 {self.config['hours']}시간 기사를 확인합니다. 수집 상태는 화면 하단에서 볼 수 있습니다."
            self.label(empty, title, 24).pack(expand=True, anchor="s", pady=12)
            self.label(empty, detail, 12, self.colors["muted"], wraplength=800).pack(expand=True, anchor="n")
            self.button(empty, "키워드 등록" if not self.active_keywords() else "지금 확인",
                        self.settings if not self.active_keywords() else self.manual_fetch, True).pack(pady=24)
            return
        for index, row in enumerate(visible_rows):
            fresh = time.time() - row["discovered"] < self.config["new_minutes"] * 60
            surface, box = self.surface(self.content, padding=PADDING)
            surface.pack(fill="both", expand=True, pady=(0 if index == 0 else GAP, 0))
            surface.border = self.colors["accent"] if fresh else self.colors["border"]
            top = tk.Frame(box, bg=self.colors["card"])
            top.pack(fill="x")
            tag = " · ".join(row["keywords"])
            color = self.colors["tags"][self.active_keywords().index(row["keywords"][0]) % len(self.colors["tags"])]
            tag_widget = self.label(top, ("NEW  |  " if fresh else "") + tag, 11, color,
                                    wraplength=max(180, width - 350), anchor='w', justify='left')
            tag_widget.pack(side="left", fill='x', expand=True)
            TextHint(tag_widget, lambda x, y, full=tag: full, self.colors)
            self.label(top, f"{row['source']}  ·  {age(row['published'])}  ·  {timestamp(row['published'])}", 10, self.colors["muted"]).pack(side="right")
            max_chars = max(28, int(width / (title_size * 1.25))) * (1 if compact else 2)
            title_text = row["title"][:max_chars] + ("…" if len(row["title"]) > max_chars else "")
            title = self.label(box, title_text, title_size, anchor="w", justify="left",
                               wraplength=width, cursor="hand2")
            title.pack(fill="x", pady=(5, 3))
            self.title_labels.append(title)
            prefix = '본문 요약 · ' if row.get('summary_basis') == 'body' else 'RSS 요약 · '
            description = (prefix + row['summary']) if row.get('summary') else (row["description"] or "기사 미리보기와 원문 링크를 확인하려면 선택하세요.")
            short = description[:110] + ("…" if len(description) > 110 else "")
            description_label = self.label(box, short, 10, self.colors["muted"], anchor="w", justify="left",
                                           wraplength=width)
            if not compact:
                description_label.pack(fill="x")
            self.card_widgets.append((tag_widget, title, description_label))
            self.bind_card(surface, row)
            surface.configure(takefocus=True, cursor="hand2")
            surface.bind("<Return>", lambda e, item=row: self.preview(item))
            surface.bind("<space>", lambda e, item=row: self.preview(item))
            surface.bind("<FocusIn>", lambda e, panel=surface: panel.set_border(self.colors["accent"]))
            surface.bind("<FocusOut>", lambda e, panel=surface: panel.set_border(panel.border))
            surface.bind("<Enter>", lambda e, panel=surface: panel.set_border(self.colors["accent"]))
            surface.bind("<Leave>", lambda e, panel=surface: panel.set_border(panel.border))

    def layout_cards(self, width, compact, title_size):
        """Resize existing widgets without clearing the news viewport."""
        for row, (tag, title, description) in zip(self.card_rows, self.card_widgets):
            max_chars = max(28, int(width / (title_size * 1.25))) * (1 if compact else 2)
            text = row["title"][:max_chars] + ("…" if len(row["title"]) > max_chars else "")
            title.configure(text=text, font=(FONT, title_size), wraplength=width)
            tag.configure(wraplength=max(180, width - 350))
            description.configure(wraplength=width)
            if compact:
                description.pack_forget()
            elif not description.winfo_manager():
                description.pack(fill="x")

    def bind_card(self, widget, row):
        widget.bind("<Button-1>", lambda e, item=row: self.preview(item))
        for child in widget.winfo_children():
            self.bind_card(child, row)

    def resize_cards(self, event):
        size = (event.width, event.height)
        if size == self.last_size:
            return
        self.last_size = size
        if self.refresh_id:
            self.root.after_cancel(self.refresh_id)
        self.refresh_id = self.root.after(180, self.render_after_resize)

    def render_after_resize(self):
        self.refresh_id = None
        self.render(resize=True)

    def select_filter(self):
        self.selected = self.filter_var.get()
        self.page = 0
        self.last_rotate = time.monotonic()
        self.reload(force=True)

    def change_page(self, direction, *, wrap=False):
        if self.preview_open:
            return
        if self.pending_rows is not None:
            self.rows = self.pending_rows
            self.pending_rows = None
        previous_page = self.page
        self.page += direction
        self.last_rotate = time.monotonic()
        self.render()
        if wrap and direction > 0 and self.page == self.page_count - 1:
            # Automatic rotation continues from the first page after the last.
            if previous_page >= self.page_count - 1:
                self.page = 0
                self.render()

    def set_rotation(self):
        self.config["auto_rotate"] = self.rotate_var.get()
        self.save_settings()
        self.last_rotate = time.monotonic()

    def save_settings(self):
        try:
            self.store.save_config(self.config)
            return True
        except OSError as exc:
            logging.exception("Settings save failed")
            messagebox.showerror("저장 오류", f"설정을 저장하지 못했습니다.\n{exc}", parent=self.root)
            return False

    def manual_fetch(self):
        self.reload(force=True)
        self.start_fetch()

    def start_fetch(self):
        if self.busy or not self.active_keywords() or self.demo:
            return
        rules = [dict(entry) for entry in self.config["keywords"] if entry["enabled"]]
        hours = self.config["hours"]
        revision = self.revision
        self.busy = True
        self.cycle_added = 0
        self.cycle_stored = set()
        self.cycle_storage_errors = []
        self.last_status = "뉴스 수집 중 · Google 요청 간격 최소 5초…"

        def worker():
            try:
                report = self.collector.collect(
                    rules, hours, cancelled=lambda: self.closed or revision != self.revision,
                    on_result=lambda name, articles: self.events.put(("partial", revision, name, articles)),
                    on_progress=lambda done, total, name: self.events.put(("batch_progress", revision, done, total, name)),
                )
            except Exception as exc:
                logging.exception("Collection worker failed")
                report = CollectionReport([], [(entry['name'], exc) for entry in rules], 0, 0)
            errors = []
            for keyword, exc in report.errors:
                logging.warning("Collection failed for %s: %s", keyword, exc)
                if isinstance(exc, ServerCoolingDown):
                    reason = "뉴스 서버 일시 중단 · 자동 재시도 예정"
                elif isinstance(exc, urllib.error.HTTPError):
                    reason = f"서버 응답 {exc.code}"
                elif isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
                    reason = "연결 실패 / 시간 초과"
                else:
                    reason = "뉴스 응답 처리 실패"
                errors.append((keyword, reason))
            logging.info("Collection: %d requests, %.2fs, %d succeeded, %d failed, provider=%s, cancelled=%s",
                         report.requests, report.seconds, len(report.results), len(errors), report.provider, report.cancelled)
            self.events.put(("done", revision, report.results, errors,
                             {"requests": report.requests, "seconds": report.seconds, "cooldown": report.cooldown,
                              "provider": report.provider, "fallback_reason": report.fallback_reason}))

        try:
            threading.Thread(target=worker, daemon=True, name="news-fetch").start()
        except Exception:
            self.busy = False
            self.next_fetch = time.monotonic() + 60
            raise
        self.fetch_button.configure(state="disabled", text="확인 중…")

    def process_events(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == 'summary':
                _, url, summary = event
                self.store.save_summary(url, summary)
                values = self.store.get_summary(url)
                for rows in (self.rows, self.pending_rows or []):
                    for row in rows:
                        if row['url'] == url:
                            row.update(values)
                if self.summary_preview and self.summary_preview[0]['url'] == url:
                    row, update = self.summary_preview
                    row.update(values)
                    update()
                self.render()
            elif event[0] == 'summary_done':
                self.summary_busy = False
            elif event[0] == "partial":
                _, revision, keyword, articles = event
                if revision != self.revision:
                    continue
                try:
                    self.cycle_added += self.store.ingest(keyword, articles)
                    self.cycle_stored.add(keyword)
                    self.reload()
                    if not self.demo:
                        self.notifications.enqueue(self.store.list_articles(self.active_keywords(), self.config['hours']))
                        self.telegram.enqueue(self.store.list_articles(self.active_keywords(), self.config['hours']))
                except (sqlite3.Error, OSError) as exc:
                    logging.exception("Partial news storage failed")
                    self.cycle_storage_errors.append(str(exc))
            elif event[0] == "batch_progress":
                _, revision, done, total, keyword = event
                if revision == self.revision:
                    self.last_status = f"수집 {done}/{total} · ‘{keyword}’ 확인 완료"
            elif event[0] == "progress":
                self.last_status = f"‘{event[1]}’ 뉴스 확인 중…"
            else:
                _, revision, results, errors = event[:4]
                metrics = event[4] if len(event) > 4 else {}
                self.busy = False
                # Arm the next cycle before storage or rendering can fail.
                delay = 60 if errors else max(10, self.config["interval_minutes"]) * 60
                self.next_fetch = time.monotonic() + max(delay, metrics.get('cooldown', 0))
                self.fetch_button.configure(state="normal", text="지금 확인")
                if revision != self.revision:
                    self.next_fetch = time.monotonic()
                    continue
                new = self.cycle_added
                try:
                    for keyword, articles in results:
                        if keyword not in self.cycle_stored:
                            new += self.store.ingest(keyword, articles)
                    if self.cycle_storage_errors:
                        raise sqlite3.OperationalError(self.cycle_storage_errors[0])
                    self.store.prune()
                    self.collection_errors = errors
                    self.reload()
                    if not self.demo:
                        self.notifications.enqueue(self.store.list_articles(self.active_keywords(), self.config['hours']))
                        self.telegram.enqueue(self.store.list_articles(self.active_keywords(), self.config['hours']))
                    if errors:
                        names = ", ".join(f"{name}: {reason}" for name, reason in errors[:2])
                        if len(errors) > 2:
                            names += f" 외 {len(errors) - 2}개"
                        self.last_status = f"{'일부 수집 실패' if results else '수집 실패'} · {names} · 자동 재시도 예정"
                    else:
                        self.last_status = f"수집 정상 · 이번 확인 새 기사 {new}건"
                    if metrics:
                        self.last_status += f" · {metrics['seconds']:.1f}초 / 요청 {metrics['requests']}개"
                        if metrics.get('fallback_reason'):
                            self.last_status += ' · ' + metrics['fallback_reason']
                except (sqlite3.Error, OSError) as exc:
                    logging.exception("News storage failed")
                    self.last_status = f"기사 저장 실패 · {exc}"
                    self.next_fetch = time.monotonic() + max(60, metrics.get('cooldown', 0))

    def start_summaries(self):
        if self.demo or self.summary_busy or self.closed:
            return
        rows = self.store.summary_candidates(self.active_keywords(), self.config['hours'])
        if not rows:
            return
        self.summary_busy = True
        summarizer, events, stopped = self.summarizer, self.events, self.summary_stop

        def worker():
            try:
                for row in rows:
                    if stopped.is_set():
                        break
                    result = summarizer.create(row)
                    if not stopped.is_set():
                        events.put(('summary', row['url'], result))
            except Exception:
                logging.exception('Article summary worker failed')
            finally:
                events.put(('summary_done',))

        try:
            threading.Thread(target=worker, daemon=True, name='news-summary').start()
        except Exception:
            self.summary_busy = False
            raise

    def tick(self):
        if self.closed:
            return
        # A Tk callback exception used to permanently break automatic collection.
        # Cancel an existing timer as well, so calling tick never duplicates it.
        if self.tick_id:
            self.root.after_cancel(self.tick_id)
            self.tick_id = None
        try:
            self._tick()
        except Exception:
            logging.exception("Automatic monitor tick failed; retrying in one second")
        finally:
            if not self.closed:
                self.tick_id = self.root.after(1000, self.tick)

    def _tick(self):
        wall = time.time()
        if wall - self.last_tick_wall >= 120:
            logging.info("Monitor resumed after %.0fs; checking news automatically", wall - self.last_tick_wall)
            self.next_fetch = time.monotonic()
            if self.busy:
                # Discard pre-suspend work and let its cancellation event finish it.
                self.revision += 1
        self.last_tick_wall = wall
        self.process_events()
        self.start_summaries()
        if not self.demo and not self.telegram_busy and time.monotonic() >= self.telegram_next:
            self.telegram_busy = True
            def send_telegram():
                try:
                    self.telegram.deliver(stopped=lambda: self.closed)
                except Exception:
                    logging.error('Telegram delivery processing failed; check storage')
                finally:
                    self.telegram_busy = False
                    self.telegram_next = time.monotonic() + 4
            threading.Thread(target=send_telegram, daemon=True, name='telegram-send').start()
        if not self.demo and not self.kakao_busy and time.monotonic() >= self.kakao_next:
            self.kakao_busy = True
            def send_notifications():
                try:
                    self.notifications.deliver(stopped=lambda: self.closed)
                except sqlite3.Error:
                    logging.exception('Kakao delivery storage failed')
                finally:
                    self.kakao_busy = False
                    self.kakao_next = time.monotonic() + 30
            threading.Thread(target=send_notifications, daemon=True, name='kakao-send').start()
        now = time.monotonic()
        if now >= self.next_fetch and not self.busy:
            if self.active_keywords() and not self.demo:
                self.start_fetch()
            else:
                self.next_fetch = now + 60
        if self.config["theme"] == "system" and not self.preview_open and self.settings_window is None:
            self.apply_theme()
        if self.config["auto_rotate"] and not self.preview_open and self.settings_window is None:
            if now - self.last_rotate >= self.config["page_seconds"]:
                self.change_page(1, wrap=True)
        success = self.store.latest_success(self.active_keywords())
        remaining = max(0, int(self.next_fetch - now))
        waiting = "확인 중" if self.busy else f"다음 확인 {remaining // 60:02d}:{remaining % 60:02d}"
        if self.demo:
            waiting = "데모 모드 · 실제 수집 꺼짐"
        elif not self.active_keywords():
            waiting = "키워드 등록 대기"
            self.last_status = "활성 키워드가 없습니다."
        pending = " · 새 결과 도착, 페이지 전환 시 반영" if self.pending_rows is not None else ""
        if self.summary_busy:
            pending += ' · 기사 본문 확인·요약 중'
        text = f"{self.last_status}{pending}\n마지막 수집 성공 {timestamp(success)}  ·  {waiting}"
        self.status_label.configure(text=text, fg=self.colors["warning"] if self.collection_errors else self.colors["muted"],
                                    wraplength=max(500, self.root.winfo_width() - 340))

    def preview(self, row):
        if self.preview_open:
            return
        self.preview_open = True
        win = tk.Toplevel(self.root)
        win.title("뉴스 미리보기")
        win.geometry("850x580")
        win.minsize(650, 450)
        win.configure(bg=self.colors["bg"])
        win.transient(self.root)
        win.grab_set()
        section, body = self.surface(win)
        section.pack(fill="both", expand=True, padx=24, pady=24)
        self.label(body, " · ".join(row["keywords"]), 12, self.colors["accent"]).pack(anchor="w")
        heading = self.label(body, row["title"], 22, wraplength=780, justify="left", anchor="w")
        heading.pack(fill="x", pady=16)
        body.bind("<Configure>", lambda e: heading.configure(wraplength=max(400, e.width - 56)))
        self.label(body, f"{row['source']} · 발행 {timestamp(row['published'])} · 발견 {timestamp(row['discovered'])}", 11, self.colors["muted"]).pack(anchor="w")
        if row.get('related_count', 1) > 1:
            self.label(body, f"유사 기사 {row['related_count']}건 통합 · " + ' / '.join(row['sources']),
                       10, self.colors['muted'], wraplength=780, justify='left').pack(anchor='w', pady=(8, 0))
        text = tk.Text(body, bg=self.colors["bg"], fg=self.colors["fg"], font=(FONT, 13), relief="flat", wrap="word", padx=16, pady=16, height=7)
        text.pack(fill="both", expand=True, pady=20)
        row = {**row, **self.store.get_summary(row['url'])}
        def update_summary():
            text.configure(state='normal')
            text.delete('1.0', 'end')
            if row.get('summary'):
                label = '본문 핵심 요약' if row.get('summary_basis') == 'body' else 'RSS 설명 요약'
                text.insert('end', label + '\n' + row['summary'] + '\n\n' + row.get('summary_note', ''))
            elif row.get('summary_basis') == 'unavailable':
                text.insert('end', row['summary_note'])
            else:
                text.insert('end', '본문 확인·요약 대기 중\n' if not self.demo else '데모 기사 · RSS 설명\n')
            if row['description']:
                text.insert('end', '\n\nRSS 제공 설명\n' + row['description'])
            for entry in self.config['keywords']:
                if entry['name'] in row['keywords']:
                    evidence = SearchRule(entry).evidence(row['title'], row['description'])
                    if evidence:
                        text.insert('end', '\n\n[' + entry.get('category', entry['name']) + '] ' + evidence)
            text.configure(state='disabled')
        update_summary()
        self.summary_preview = (row, update_summary)
        self.label(body, "자동 요약은 핵심 문장 발췌입니다. 기사 열기로 원문을 확인하세요.", 10, self.colors["muted"]).pack(anchor="w")
        actions = tk.Frame(body, bg=self.colors["panel"])
        actions.pack(fill="x", pady=(14, 0))

        def close():
            self.preview_open = False
            self.summary_preview = None
            self.last_rotate = time.monotonic()
            win.destroy()

        self.button(actions, "기사 열기 ↗", lambda: self.open_article(row.get('content_url') or row["url"]), True).pack(side="left")
        self.button(actions, "닫기", close).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda e: close())

    def open_article(self, url):
        if valid_link(url):
            try:
                if not webbrowser.open(url):
                    messagebox.showerror("브라우저", "기본 브라우저를 열지 못했습니다.", parent=self.root)
            except webbrowser.Error as exc:
                messagebox.showerror("브라우저", str(exc), parent=self.root)

    def settings(self):
        if self.settings_window is not None:
            self.settings_window.lift()
            return
        win = tk.Toplevel(self.root)
        self.settings_window = win
        win.title("키워드 및 모니터링 설정")
        win.geometry(f"{min(920, win.winfo_screenwidth() - 80)}x{min(780, win.winfo_screenheight() - 100)}")
        win.minsize(700, 500)
        win.configure(bg=self.colors["bg"])
        win.transient(self.root)
        win.grab_set()
        draft = [dict(entry) for entry in self.config["keywords"]]
        section, shell = self.surface(win)
        section.pack(fill="both", expand=True, padx=24, pady=24)
        actions = tk.Frame(shell, bg=self.colors["panel"])
        actions.pack(side="bottom", fill="x", pady=(GAP, 0))
        notebook = ttk.Notebook(shell, style="Monitor.TNotebook")
        notebook.pack(fill="both", expand=True)

        def scroll_tab(title):
            tab = tk.Frame(notebook, bg=self.colors["panel"])
            notebook.add(tab, text=title)
            canvas = tk.Canvas(tab, bg=self.colors["panel"], highlightthickness=0)
            scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview, style="Monitor.Vertical.TScrollbar")
            scrollbar.pack(side="right", fill="y", padx=(8, 0))
            canvas.pack(side="left", fill="both", expand=True)
            canvas.configure(yscrollcommand=scrollbar.set)
            body = tk.Frame(canvas, bg=self.colors["panel"], padx=18, pady=15)
            item = canvas.create_window((0, 0), window=body, anchor="nw")
            body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
            return body, canvas

        box, rule_canvas = scroll_tab("키워드 / 검색 조건")
        monitor, monitor_canvas = scroll_tab("모니터링 설정")
        group_box, group_canvas = scroll_tab("발전그룹사 등록")
        # Fill the viewport while allowing a minimum usable table height.
        # The longer company-edit form retains the outer canvas scrollbar.
        group_resize_job = [None]
        def fit_groups():
            group_resize_job[0] = None
            item = group_canvas.find_all()[0]
            height = max(group_canvas.winfo_height(), group_box.winfo_reqheight())
            group_canvas.itemconfigure(item, width=group_canvas.winfo_width(), height=height)
            group_canvas.configure(scrollregion=group_canvas.bbox('all'))
        def resize_groups(event=None):
            group_canvas.itemconfigure(group_canvas.find_all()[0], width=group_canvas.winfo_width())
            if group_resize_job[0] is None:
                group_resize_job[0] = group_canvas.after_idle(fit_groups)
        group_canvas.bind('<Configure>', resize_groups)
        group_box.bind('<Configure>', resize_groups)
        update_box, update_canvas = scroll_tab("프로그램 / 업데이트")
        notebook.insert(0, group_canvas.master)
        def wheel(event):
            canvas = next(c for c in (rule_canvas, monitor_canvas, group_canvas, update_canvas) if str(c.master) == notebook.select())
            if canvas.bbox("all") and canvas.bbox("all")[3] > canvas.winfo_height():
                canvas.yview_scroll(-int(event.delta / 120), "units")
        win.bind("<MouseWheel>", wheel, add="+")
        self.label(box, "관심 키워드와 검색 조건", 20).pack(anchor="w")
        self.label(box, "일반 검색 조건 최대 20개 · 발전그룹사는 ‘발전그룹사 등록’ 탭에서 관리", 10, self.colors["muted"]).pack(anchor="w", pady=(4, 10))
        listing = tk.Listbox(box, bg=self.colors["input"], fg=self.colors["fg"], selectbackground=self.colors["hover"], selectforeground=self.colors["accent"], font=(FONT, 12),
                             relief="flat", bd=0, highlightthickness=1, highlightbackground=self.colors["border"],
                             highlightcolor=self.colors["accent"], selectborderwidth=0, height=4, exportselection=False)
        listing.pack(fill="x")
        list_scroll = ttk.Scrollbar(box, orient='horizontal', command=listing.xview, style='Monitor.Horizontal.TScrollbar')
        list_scroll.pack(fill='x')
        listing.configure(xscrollcommand=list_scroll.set)
        TextHint(listing, lambda x, y: listing.get(listing.nearest(y)) if listing.size() else '', self.colors)
        buttons = tk.Frame(box, bg=self.colors["panel"])
        buttons.pack(fill="x", pady=8)
        entry_var = tk.StringVar()
        condition_var = tk.StringVar()
        exclude_var = tk.StringVar()
        scope_var = tk.StringVar(value="제목 + RSS 설명")
        test_var = tk.StringVar()
        editing = [None]
        editing_identity = [None]

        def input_field(title, variable, name):
            self.label(box, title, 11, self.colors["muted"]).pack(anchor="w", pady=(7, 4))
            widget = tk.Entry(box, name=name, textvariable=variable, bg=self.colors["bg"], fg=self.colors["fg"],
                              insertbackground=self.colors["fg"], font=(FONT, 12), relief="flat", bd=0,
                              highlightthickness=1, highlightbackground=self.colors["border"], highlightcolor=self.colors["accent"])
            widget.pack(fill="x", ipady=7)
            TextHint(widget, lambda x, y: widget.get(), self.colors)
            return widget

        entry = input_field("표시 이름 (예: 한전 주요 소식)", entry_var, "rule_name")
        condition_entry = input_field("검색 조건 (비워 두면 표시 이름을 문구로 검색)", condition_var, "rule_condition")
        operators = tk.Frame(box, bg=self.colors["panel"])
        operators.pack(fill="x", pady=5)
        def insert_operator(value):
            condition_entry.insert("insert", value)
            condition_entry.focus_set()
        for text, value in (("AND 모두", " AND "), ("OR 하나 이상", " OR "),
                            ("NOT 제외", " NOT "), ("괄호 ( )", "( )"), ('문구 " "', '" "')):
            self.button(operators, text, lambda v=value: insert_operator(v)).pack(side="left", padx=(0, 4))
        self.flow_buttons(operators)
        self.label(box, '예: 한전 AND (전기 OR 요금) NOT 주가\n우선순위: NOT → AND → OR · 공백은 AND · 여러 단어의 문구는 "한국 전력"',
                   10, self.colors["muted"], justify="left", anchor="w").pack(fill="x", pady=4)
        input_field("추가 제외어 (쉼표로 구분 · 예: 주가, 채용)", exclude_var, "rule_exclude")
        scope_row = tk.Frame(box, bg=self.colors["panel"])
        scope_row.pack(fill="x", pady=10)
        self.label(scope_row, "조건 확인 범위", 11, self.colors["muted"]).pack(side="left", padx=(0, 12))
        self.combobox(scope_row, textvariable=scope_var, values=("제목 + RSS 설명", "제목만"),
                     state="readonly", font=(FONT, 11), width=20).pack(side="left")
        self.label(box, "상세 조건은 수집된 제목과 RSS 설명에 적용합니다. 기사 전문은 검사하지 않습니다.",
                   10, self.colors["muted"], anchor="w").pack(fill="x")
        query_label = self.label(box, "", 10, self.colors["accent"], justify="left", anchor="w", wraplength=760)
        query_label.pack(fill="x", pady=10)
        input_field("조건 테스트: 예시 기사 제목을 입력하면 포함 여부를 확인합니다", test_var, "rule_sample")
        test_label = self.label(box, "", 11, self.colors["muted"], anchor="w")
        test_label.pack(fill="x", pady=6)

        def form_entry(enabled=True):
            result = {"name": entry_var.get().strip(), "enabled": enabled}
            condition, exclude = condition_var.get().strip(), exclude_var.get().strip()
            scope = "title" if scope_var.get() == "제목만" else "title_description"
            if condition:
                result["condition"] = condition
            if exclude:
                result["exclude"] = exclude
            if condition or exclude or scope == "title":
                result["scope"] = scope
            return result

        def preview_condition(*args):
            try:
                rule = SearchRule(form_entry())
                query_label.configure(text="검색 조건 미리보기: " + rule.query, fg=self.colors["accent"])
                if test_var.get().strip():
                    match = rule.matches(test_var.get())
                    test_label.configure(text="✓ 조건에 포함됩니다" if match else "조건에서 제외됩니다",
                                         fg=self.colors["accent"] if match else self.colors["warning"])
                else:
                    test_label.configure(text="예시 제목을 입력해 테스트하세요.", fg=self.colors["muted"])
            except RuleError as exc:
                query_label.configure(text="조건 확인: " + str(exc), fg=self.colors["warning"])
                test_label.configure(text="올바른 조건을 먼저 입력해 주세요.", fg=self.colors["muted"])
        for variable in (entry_var, condition_var, exclude_var, scope_var, test_var):
            variable.trace_add("write", preview_condition)
        box.bind("<Configure>", lambda e: query_label.configure(wraplength=max(400, e.width - 40)), add="+")

        def clear_form():
            editing[0] = None
            editing_identity[0] = None
            listing.selection_clear(0, "end")
            entry_var.set("")
            condition_var.set("")
            exclude_var.set("")
            scope_var.set("제목 + RSS 설명")
            test_var.set("")
            entry.focus_set()

        def draw(selected=None):
            listing.delete(0, "end")
            for index, item in enumerate(draft):
                if item.get('profile') != 'company_topic':
                    listing.insert("end", f"{'● 사용' if item['enabled'] else '○ 중지'}   {item['name']}")
                    if index == selected:
                        listing.selection_set(listing.size() - 1)

        def selected_index():
            indices = listing.curselection()
            ordinary = [i for i, item in enumerate(draft) if item.get('profile') != 'company_topic']
            return ordinary[indices[0]] if indices else None

        def select(event=None):
            index = selected_index()
            if index is not None:
                editing[0] = index
                editing_identity[0] = draft[index]
                entry_var.set(draft[index]["name"])
                condition_var.set(draft[index].get("condition", ""))
                exclude_var.set(draft[index].get("exclude", ""))
                scope_var.set("제목만" if draft[index].get("scope") == "title" else "제목 + RSS 설명")

        def name_valid(name, current=None):
            if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
                messagebox.showwarning("키워드", "키워드를 1~80자로 입력해 주세요.", parent=win)
                return False
            if name == "전체":
                messagebox.showwarning("키워드", "‘전체’는 화면 필터 이름입니다. 다른 키워드를 입력해 주세요.", parent=win)
                return False
            if any(item["name"].casefold() == name.casefold() for i, item in enumerate(draft) if i != current):
                messagebox.showwarning("키워드", "이미 등록한 키워드입니다.", parent=win)
                return False
            try:
                SearchRule(form_entry())
            except RuleError as exc:
                messagebox.showwarning("검색 조건", str(exc), parent=win)
                return False
            return True

        def add():
            name = entry_var.get().strip()
            if sum(item.get('profile') != 'company_topic' for item in draft) >= 20:
                messagebox.showwarning("키워드", "키워드는 최대 20개까지 등록할 수 있습니다.", parent=win)
                return
            if name_valid(name):
                draft.append(form_entry())
                clear_form()
                draw()
                return True
            return False

        def edit():
            index = editing[0]
            name = entry_var.get().strip()
            if index is not None and name_valid(name, index):
                draft[index] = form_entry(draft[index]["enabled"])
                editing_identity[0] = draft[index]
                draw(index)
                return True
            return False

        def toggle():
            index = selected_index()
            if index is not None:
                draft[index]["enabled"] = not draft[index]["enabled"]
                draw(index)

        def delete():
            index = selected_index()
            if index is not None:
                draft.pop(index)
                clear_form()
                draw()

        listing.bind("<<ListboxSelect>>", select)
        entry.bind("<Return>", lambda e: add())
        for title, action in (("새 조건", clear_form), ("추가", add), ("선택 수정", edit),
                              ("사용 / 중지", toggle), ("선택 삭제", delete)):
            self.button(buttons, title, action).pack(side="left", padx=(0, 10))
        self.flow_buttons(buttons)
        self.label(monitor, "화면과 모니터링", 20).pack(anchor="w", pady=(0, 18))
        appearance = tk.Frame(monitor, bg=self.colors["panel"])
        appearance.pack(fill="x", pady=(0, GAP))
        self.label(appearance, "화면 테마", 12).pack(side="left")
        self.theme_var = tk.StringVar(value=THEME_LABELS[self.config["theme"]])
        self.combobox(appearance, textvariable=self.theme_var,
                     values=list(THEME_LABELS.values()), state="readonly",
                     width=16, font=(FONT, 11)).pack(side="right")
        self.label(monitor, "시스템 설정은 Windows 앱 모드를 따라 자동으로 변경됩니다.",
                   10, self.colors["muted"]).pack(anchor="w", pady=(0, 24))
        fields = tk.Frame(monitor, bg=self.colors["panel"])
        fields.pack(fill="x")
        variables = {}
        for index, (key, title, low, high) in enumerate((
            ("interval_minutes", "검색 간격 (분)", 10, 60), ("hours", "기사 조회 기간 (시간)", 1, 168),
            ("page_seconds", "페이지 전환 (초)", 5, 300), ("page_size", "화면당 기사 수", 3, 8),
            ("new_minutes", "NEW 표시 (분)", 1, 60),
        )):
            variable = tk.StringVar(value=str(self.config[key]))
            variables[key] = (variable, low, high, title)
            self.label(fields, title, 11, self.colors["muted"]).grid(row=index, column=0, sticky="w", pady=5)
            ttk.Spinbox(fields, from_=low, to=high, textvariable=variable, width=8,
                        font=(FONT, 11), style="Monitor.TSpinbox").grid(row=index, column=1, padx=18)
        self.label(monitor, "F11 전체 화면 · Esc 해제 · ← → 페이지 이동", 10, self.colors["muted"]).pack(anchor="w", pady=20)

        self.label(update_box, "프로그램 버전 및 자동 업데이트", 20).pack(anchor="w", pady=(0, 18))
        self.label(update_box, f"현재 버전  v{VERSION}", 14, name='update_current_version').pack(anchor="w", pady=(0, GAP))
        latest_label = self.label(update_box, "최신 버전  아직 확인하지 않음", 12, name='update_latest_version')
        latest_label.pack(anchor="w", pady=(0, GAP))
        checked_label = self.label(update_box, "마지막 확인  아직 없음", 10, self.colors['muted'])
        checked_label.pack(anchor="w", pady=(0, GAP))
        update_message = self.label(update_box, "", 12, wraplength=580, justify='left', name='update_status')
        update_message.pack(anchor="w", fill='x', pady=(0, GAP))
        self.label(update_box, "시작 후 및 6시간마다 새 버전을 확인합니다. 다운로드한 업데이트는 다음 실행 때 적용하며 설정과 뉴스는 유지합니다.",
                   10, self.colors['muted'], wraplength=580, justify='left').pack(anchor='w', fill='x', pady=(0, GAP))
        if self.demo:
            self.label(update_box, "데모에서는 업데이트 확인과 다운로드를 하지 않습니다.", 10, self.colors['muted']).pack(anchor='w')
        elif not getattr(sys, 'frozen', False):
            self.label(update_box, "Python 소스 실행에서는 버전 확인만 가능합니다. 자동 적용은 EXE에서 사용할 수 있습니다.",
                       10, self.colors['muted'], wraplength=580, justify='left').pack(anchor='w', fill='x')
        update_button = self.button(update_box, "업데이트 확인", self.updates.check, True)
        update_button.pack(anchor='w', pady=(GAP, 0))
        update_timer = [None]

        def refresh_updates():
            if self.settings_window is not win:
                return
            state = self.updates.snapshot()
            latest_label.configure(text=f"최신 버전  {state['latest'] or '아직 확인하지 않음'}")
            checked_label.configure(text=f"마지막 확인  {time.strftime('%Y.%m.%d %H:%M:%S', time.localtime(state['checked'])) if state['checked'] else '아직 없음'}")
            update_message.configure(text=state['message'])
            update_button.configure(text='확인 중…' if state['busy'] else '업데이트 확인',
                                    state='disabled' if self.demo or state['busy'] else 'normal')
            update_timer[0] = win.after(250, refresh_updates)
        refresh_updates()

        def refresh_ordinary():
            if editing_identity[0] is not None:
                editing[0] = next((i for i, item in enumerate(draft) if item is editing_identity[0]), None)
                if editing[0] is None:
                    clear_form()
            draw(editing[0])
        flush_groups = build_group_editor(self, group_box, draft, refresh_ordinary)

        def close():
            if update_timer[0] is not None:
                win.after_cancel(update_timer[0])
            self.settings_window = None
            self.last_rotate = time.monotonic()
            win.destroy()

        def save():
            index = editing[0]
            if index is not None:
                if form_entry(draft[index]["enabled"]) != draft[index] and not edit():
                    return
            elif entry_var.get().strip() or condition_var.get().strip() or exclude_var.get().strip():
                if not add():
                    return
            if not flush_groups():
                return
            updated = {**self.config, "keywords": draft,
                       "theme": next(key for key, label in THEME_LABELS.items() if label == self.theme_var.get())}
            for key, (variable, low, high, title) in variables.items():
                try:
                    value = int(variable.get())
                    if not low <= value <= high:
                        raise ValueError()
                    updated[key] = value
                except ValueError:
                    messagebox.showwarning("설정", f"{title}: {low}~{high} 사이의 정수를 입력해 주세요.", parent=win)
                    return
            previous = self.config
            self.config = updated
            if not self.save_settings():
                self.config = previous
                return
            self.revision += 1
            remaining = {item["name"]: item for item in draft}
            for item in previous["keywords"]:
                replacement = remaining.get(item["name"])
                changed = replacement is not None and any(item.get(key) != replacement.get(key) for key in ("condition", "exclude", "scope", "profile", "aliases", "terms", "category", "company", "disabled_terms"))
                if replacement is None or changed:
                    self.store.remove_keyword(item["name"])
            self.collection_errors = []
            self.page = 0
            self.reload(force=True)
            self.next_fetch = time.monotonic()
            close()
            self.apply_theme()

        self.button(actions, "저장하고 적용", save, True).pack(side="right")
        self.button(actions, "취소", close).pack(side="right", padx=10)
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda e: close())
        draw()
        preview_condition()
        notebook.select(group_canvas.master)

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def exit_fullscreen(self):
        self.fullscreen = False
        self.root.attributes("-fullscreen", False)

    def seed_demo(self):
        self.config["keywords"] = [{"name": name, "enabled": True} for name in ("반도체", "인공지능", "경쟁사")]
        now = time.time()
        titles = [
            "[데모] 반도체 산업의 새 소식을 이 화면에서 확인합니다",
            "[데모] 인공지능 관련 뉴스가 발견되면 NEW 표시로 강조합니다",
            "[데모] 경쟁사 소식을 키워드별로 모아 볼 수 있습니다",
            "[데모] 카드를 선택하면 기사 미리보기 창이 열립니다",
            "[데모] 모니터링 화면은 일정 간격으로 다음 페이지로 넘어갑니다",
            "[데모] 여러 키워드에 걸린 기사는 한 번만 표시합니다",
            "[데모] 네트워크 연결이 끊겨도 수집한 기사는 유지됩니다",
        ]
        for index, title in enumerate(titles):
            keyword = self.config["keywords"][index % 3]["name"]
            self.store.ingest(keyword, [Article(
                f"https://news.google.com/?demo={index}", title, "가상 뉴스 / 데모",
                now - (index * 13 + 3) * 60,
                "화면 확인을 위해 만든 가상 기사입니다. 실제 뉴스가 아닙니다. 실제 실행에서는 RSS가 제공하는 정보가 표시됩니다.",
            )], now=now - index * 120)
        self.last_status = "가상 기사로 화면을 확인하는 데모 모드"

    def kakao_settings(self):
        from kakao_editor import open_kakao_editor
        open_kakao_editor(self)

    def telegram_settings(self):
        from telegram_editor import open_telegram_editor
        open_telegram_editor(self)

    def close(self):
        logging.info("News monitor closing")
        self.closed = True
        self.summary_stop.set()
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.root.tk.call(self.settings_window.protocol('WM_DELETE_WINDOW'))
        if self.telegram_window is not None and self.telegram_window.winfo_exists():
            self.root.tk.call(self.telegram_window.protocol('WM_DELETE_WINDOW'))
        if self.kakao_window is not None and self.kakao_window.winfo_exists():
            self.root.tk.call(self.kakao_window.protocol('WM_DELETE_WINDOW'))
        if self.tick_id:
            self.root.after_cancel(self.tick_id)
        if self.refresh_id:
            self.root.after_cancel(self.refresh_id)
        self.store.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="키워드 뉴스 모니터링 GUI")
    parser.add_argument("--demo", action="store_true", help="별도 저장소에 가상 기사 표시")
    parser.add_argument("--data-dir", type=Path, help="설정과 기사 저장 폴더")
    parser.add_argument("--smoke-test", action="store_true", help="GUI 생성 후 자동 종료")
    parser.add_argument("--fullscreen", action="store_true", help="전체 화면으로 시작")
    parser.add_argument("--no-update", action="store_true", help="이번 실행에서 자동 업데이트 건너뛰기")
    args = parser.parse_args()
    directory = args.data_dir or app_directory() / ("demo_data" if args.demo else "data")
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(
        directory / "monitor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")],
        format="%(asctime)s %(levelname)s %(message)s")
    updates_enabled = getattr(sys, 'frozen', False) and not (args.demo or args.smoke_test or args.no_update)
    if updates_enabled:
        try:
            if apply_pending():
                return 0
        except Exception:
            logging.exception('Pending update failed; continuing current version')
    root = create_root()
    try:
        app = NewsMonitor(root, directory, demo=args.demo)
    except Exception as exc:
        logging.exception("Startup failed")
        root.withdraw()
        messagebox.showerror("실행 오류", f"프로그램을 시작하지 못했습니다.\n{exc}", parent=root)
        root.destroy()
        return 1
    if args.fullscreen:
        app.toggle_fullscreen()
    if args.smoke_test:
        root.after(1200, app.close)
    if updates_enabled:
        def check_updates():
            app.updates.check()
            if not app.closed:
                root.after(6 * 60 * 60 * 1000, check_updates)
        root.after(5000, check_updates)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
