"""Guided company monitoring registration for the settings notebook."""
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter import font as tkfont
from pathlib import Path
from security_profiles import COMPANIES, TOPICS, phrases, make_entries, parse_template, validate_terms
from search_rules import RuleError, SearchRule
from ui_theme import TextHint
from smooth_list import SmoothTermList


def build_group_editor(app, body, draft, redraw):
    colors = app.colors
    parent = app.settings_window
    selected = [None]
    name = tk.StringVar()
    aliases = tk.StringVar()
    exclude = tk.StringVar()
    scope = tk.StringVar(value="제목 + RSS 설명")
    enabled_topics = {key: tk.BooleanVar(value=True) for key in TOPICS}
    topic_widgets = {}
    topic_defaults = dict(TOPICS)
    for item in draft:
        if item.get('profile') == 'company_topic':
            topic_defaults.setdefault(item['category'], item['terms'])
            enabled_topics.setdefault(item['category'], tk.BooleanVar(value=False))
    app.label(body, "그룹사 모니터링 조건", 17).pack(anchor="w")
    app.label(body, "그룹사를 선택해 분류·검색어 ON/OFF · 변경 후 저장하고 적용", 10,
              colors['muted'], wraplength=700).pack(anchor="w", pady=4)
    quick = tk.Frame(body, bg=colors['panel'])
    quick.pack(fill="x", pady=10)
    summary = app.label(body, "", 11, colors['accent'], anchor="w", wraplength=600)
    summary.pack(fill="x", pady=4)
    view_container = tk.Frame(body, bg=colors['panel'])
    view_container.pack(fill='both', expand=True)
    list_view = tk.Frame(view_container, bg=colors['panel'])
    editor_view = tk.Frame(view_container, bg=colors['panel'])
    list_view.pack(fill='both', expand=True)
    body = list_view
    actions = tk.Frame(body, bg=colors['panel'])
    actions.pack(fill='x', pady=6)
    search_row = tk.Frame(body, bg=colors['panel'])
    search_row.pack(fill='x', pady=(10, 8))
    app.label(search_row, '등록 그룹사 찾기', 11, colors['muted']).pack(side='left', padx=(0, 12))
    search = tk.StringVar()
    tk.Entry(search_row, name='company_filter', textvariable=search, bg=colors['input'],
             fg=colors['fg'], insertbackground=colors['fg'], relief='flat', bd=0,
             highlightthickness=1, highlightbackground=colors['border'], highlightcolor=colors['accent'],
             font=('맑은 고딕', 11)).pack(side='left', fill='x', expand=True, ipady=7)
    detail_row = tk.Frame(body, bg=colors['panel'])
    detail_row.pack(fill='both', expand=True)
    detail_row.rowconfigure(0, weight=1)
    detail_row.columnconfigure(0, weight=1, minsize=240, uniform='details')
    detail_row.columnconfigure(1, weight=1, minsize=300, uniform='details')
    table = tk.Frame(detail_row, bg=colors['border'], padx=1, pady=1)
    table.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
    tree = ttk.Treeview(table, name='company_list', columns=("topics", "state"), show="tree headings", height=2,
                        displaycolumns=('state',), selectmode="browse", style='Company.Treeview')
    tree.heading('#0', text="  그룹사", anchor='w')
    tree.heading('topics', text="  모니터링 분류", anchor='w')
    tree.heading('state', text="사용 상태", anchor='center')
    tree.column('#0', width=150, minwidth=110, stretch=True, anchor='w')
    tree.column('topics', width=370, minwidth=260, stretch=True, anchor='w')
    state_width = tkfont.Font(root=parent, family='맑은 고딕', size=11).measure('◐ 일부 사용') + 28
    tree.column('state', width=state_width, minwidth=state_width, stretch=False, anchor='center')
    scrollbar = ttk.Scrollbar(table, orient='vertical', command=tree.yview, style='Monitor.Vertical.TScrollbar')
    table.columnconfigure(0, weight=1)
    # The neighboring controls determine the shared height. The table's data
    # viewport must absorb that height instead of leaving a frame below it.
    table.rowconfigure(0, weight=1)
    scrollbar.grid(row=0, column=1, sticky='ns', padx=(4, 0))
    tree.configure(yscrollcommand=scrollbar.set)
    tree.grid(row=0, column=0, sticky='nsew')
    horizontal = ttk.Scrollbar(table, orient='horizontal', command=tree.xview, style='Monitor.Horizontal.TScrollbar')
    def horizontal_position(first, last):
        horizontal.set(first, last)
        if float(first) <= 0 and float(last) >= .999:
            horizontal.grid_remove()
        else:
            horizontal.grid(row=1, column=0, sticky='ew', pady=(3, 0))
    tree.configure(xscrollcommand=horizontal_position)
    def cell_text(x, y):
        row = tree.identify_row(y)
        column = tree.identify_column(x)
        if not row:
            return ''
        return tree.item(row, 'text') if column == '#0' else tree.set(row, column)
    TextHint(tree, cell_text, colors)
    def scroll_table(event):
        if event.delta:
            tree.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'
    tree.bind('<MouseWheel>', scroll_table)
    tree.tag_configure('even', background=colors['panel'])
    tree.tag_configure('odd', background=colors['input'])
    tree.tag_configure('paused', foreground=colors['muted'])
    table_hint = app.label(body, '그룹사를 선택해 오른쪽에서 분류와 검색어를 확인하세요.',
                           10, colors['muted'], anchor='w', wraplength=650)
    table_hint.pack(side='bottom', fill='x', pady=(2, 10), before=detail_row)
    controls = tk.Frame(detail_row, bg=colors['input'], padx=10, pady=8)
    controls.grid(row=0, column=1, sticky='nsew')
    stacked = [None]
    def arrange_details(event):
        compact = event.width < 560
        if compact == stacked[0]:
            return
        stacked[0] = compact
        if compact:
            detail_row.columnconfigure(0, minsize=0)
            detail_row.columnconfigure(1, minsize=0)
            detail_row.rowconfigure(1, weight=1)
            table.grid(row=0, column=0, columnspan=2, padx=0, pady=(0, 8))
            controls.grid(row=1, column=0, columnspan=2)
        else:
            detail_row.columnconfigure(0, minsize=240)
            detail_row.columnconfigure(1, minsize=300)
            detail_row.rowconfigure(1, weight=0)
            table.grid(row=0, column=0, columnspan=1, padx=(0, 8), pady=0)
            controls.grid(row=0, column=1, columnspan=1)
    detail_row.bind('<Configure>', arrange_details)
    selected_label = app.label(controls, '그룹사를 선택하면 분류와 검색어를 켜고 끌 수 있습니다.', 12,
                               colors['accent'], anchor='w', wraplength=370)
    selected_label.pack(fill='x')
    selected_label.bind('<Configure>', lambda event: selected_label.configure(wraplength=max(180, event.width)))
    control_row = tk.Frame(controls, bg=colors['input'])
    control_row.pack(fill='x', pady=6)
    category_var = tk.StringVar()
    category_picker = app.combobox(control_row, name='monitor_category', textvariable=category_var,
                                  values=(), state='disabled', style='Monitor.TCombobox', width=14)
    category_picker.pack(side='left', fill='x', expand=True, padx=(0, 8))
    category_on = tk.BooleanVar()
    category_check = tk.Checkbutton(control_row, name='category_on', text='분류 ON', variable=category_on,
                                   bg=colors['input'], fg=colors['fg'], selectcolor=colors['bg'],
                                   activebackground=colors['input'], activeforeground=colors['fg'],
                                   font=('맑은 고딕', 11), state='disabled')
    category_check.pack(side='right')
    keyword_frame = tk.Frame(controls, bg=colors['input'])
    keyword_frame.pack(fill='both', expand=True)
    keyword_tree = SmoothTermList(keyword_frame, name='monitor_terms', colors=colors, height=2)
    keyword_tree.heading('#0', text='검색어', anchor='w')
    keyword_tree.heading('state', text='사용 상태')
    keyword_tree.column('#0', width=220, minwidth=140)
    keyword_tree.column('state', width=80, stretch=False, anchor='center')
    keyword_scroll = ttk.Scrollbar(keyword_frame, orient='vertical', command=keyword_tree.yview,
                                   style='Monitor.Vertical.TScrollbar')
    keyword_tree.configure(yscrollcommand=keyword_scroll.set)
    keyword_scroll.pack(side='right', fill='y')
    keyword_tree.pack(side='left', fill='both', expand=True)
    keyword_tree.tag_configure('paused', foreground=colors['muted'])
    keyword_hint = app.label(controls, 'OFF 검색어는 이 그룹사의 수집·검색에서 제외됩니다. 변경 후 저장하고 적용하세요.',
                             10, colors['muted'], anchor='w', wraplength=370)
    keyword_hint.pack(side='bottom', fill='x', pady=(6, 0), before=keyword_frame)
    keyword_hint.bind('<Configure>', lambda event: keyword_hint.configure(wraplength=max(180, event.width)))
    body = editor_view
    back_row = tk.Frame(body, bg=colors['panel'])
    back_row.pack(fill='x', pady=(0, 8))
    app.label(body, '그룹사 추가 / 조건 편집', 15).pack(anchor='w', pady=(8, 4))
    app.label(body, '이름·약칭 중 하나 AND 분류 검색어 중 하나 · 예: 한전 + 안전 사고(화재, 산업재해)',
              10, colors['accent'], wraplength=650).pack(anchor='w', pady=(0, 8))
    def field(label, variable, widget_name):
        app.label(body, label, 11, colors['muted']).pack(anchor="w", pady=(8, 3))
        widget = tk.Entry(body, name=widget_name, textvariable=variable, bg=colors['bg'], fg=colors['fg'], insertbackground=colors['fg'], relief="flat", font=("맑은 고딕", 11))
        widget.pack(fill="x", ipady=6)
        TextHint(widget, lambda x, y: widget.get(), colors)
        return widget
    presets = app.combobox(body, values=list(COMPANIES), state="readonly", font=("맑은 고딕", 11), style='Monitor.TCombobox')
    presets.pack(fill="x", pady=6)
    def choose(event=None):
        selected[0] = None
        tree.selection_remove(*tree.selection())
        name.set(presets.get())
        aliases.set(" | ".join(COMPANIES[presets.get()]))
        exclude.set("")
        for var in enabled_topics.values():
            var.set(True)
        refresh_controls()
    presets.bind('<<ComboboxSelected>>', choose)
    field("그룹사 이름 (직접 입력하거나 위 목록에서 선택)", name, "company_name")
    field("검색 이름·약칭 ( | 또는 쉼표로 구분 · 여러 단어의 문구는 그대로 입력)", aliases, "company_aliases")
    app.label(body, "그룹사에 연결할 분류 · 체크 해제는 연결 제거, 일시 중지는 위의 분류 ON/OFF", 11,
              colors['muted'], wraplength=650).pack(anchor="w", pady=(14, 4))
    topic_box = tk.Frame(body, bg=colors['panel'])
    topic_box.pack(fill='x')
    def add_topic_widget(category, initial):
        enabled_topics.setdefault(category, tk.BooleanVar(value=True))
        tk.Checkbutton(topic_box, text=category, variable=enabled_topics[category], bg=colors['panel'], fg=colors['fg'], selectcolor=colors['bg'], activebackground=colors['panel'], activeforeground=colors['fg'], font=("맑은 고딕", 11)).pack(anchor="w")
        widget = tk.Text(topic_box, height=3, wrap="word", bg=colors['bg'], fg=colors['fg'], insertbackground=colors['fg'], relief="flat", font=("맑은 고딕", 10))
        widget.insert('1.0', ' | '.join(initial))
        widget.pack(fill="x", pady=(2, 6))
        topic_widgets[category] = widget
    for category, defaults in topic_defaults.items():
        initial = next((item['terms'] for item in draft if item.get('profile') == 'company_topic' and item['category'] == category), defaults)
        add_topic_widget(category, initial)
    app.label(body, '공통 검색어 목록 · 수정하면 해당 분류를 사용하는 모든 그룹사에 적용됩니다.',
              10, colors['muted'], wraplength=650).pack(anchor='w', pady=4)
    new_category = tk.StringVar()
    new_terms = tk.StringVar()
    field('새 분류 이름 · 예: 안전 사고 / 경영 이슈', new_category, 'new_category')
    field('분류 검색어 · 예: 산업재해 | 화재 | 안전 사고', new_terms, 'new_category_terms')
    add_category_row = tk.Frame(body, bg=colors['panel'])
    add_category_row.pack(fill='x', pady=6)
    field("이 그룹사의 제외어 (쉼표로 구분 · 예: 주가, 채용)", exclude, "company_exclude")
    app.combobox(body, textvariable=scope, values=("제목 + RSS 설명", "제목만"), state="readonly", style='Monitor.TCombobox').pack(anchor="w", pady=8)
    app.label(body, "제목·RSS 설명의 문구 포함을 검사합니다. 기사 전문은 검사하지 않습니다.\nKPS·KDN 등 짧은 약칭으로 잡음이 생기면 약칭 목록에서 삭제하세요.", 10, colors['muted'], justify="left", wraplength=600).pack(fill="x", pady=6)
    sample = tk.StringVar()
    field("예시 기사 제목으로 분류 확인", sample, "company_sample")
    sample_result = app.label(body, "", 11, colors['accent'], wraplength=600, anchor="w")
    sample_result.pack(fill="x", pady=6)

    def topics():
        return {key: validate_terms(phrases(widget.get('1.0', 'end'))) for key, widget in topic_widgets.items()}
    def entries():
        values = topics()
        return make_entries(name.get(), phrases(aliases.get()), {key: values[key] for key, var in enabled_topics.items() if var.get()}, exclude.get().strip(), 'title' if scope.get() == '제목만' else 'title_description')
    def preview(*args):
        try:
            rules = entries()
            hits = [item['category'] for item in rules if SearchRule(item).matches(sample.get())]
            sample_result.configure(text=("포함: " + ', '.join(hits) if hits else "선택한 조건에서 제외됩니다.") if sample.get().strip() else "이름·약칭과 분류 키워드가 함께 포함되어야 합니다.")
        except RuleError as exc:
            sample_result.configure(text=str(exc))
    sample.trace_add('write', preview)
    for variable in (name, aliases, exclude, scope, *enabled_topics.values()):
        variable.trace_add('write', preview)
    for widget in topic_widgets.values():
        widget.bind('<KeyRelease>', preview)

    def groups():
        result = {}
        for item in draft:
            if item.get('profile') == 'company_topic':
                result.setdefault(item['company'], []).append(item)
        return result
    def draw():
        previous = tree.selection()
        position = tree.yview()[0]
        tree.delete(*tree.get_children())
        current = groups()
        needle = search.get().strip().casefold()
        visible = [(company, rules) for company, rules in current.items()
                   if not needle or needle in company.casefold() or
                   any(needle in alias.casefold() for rule in rules for alias in rule['aliases'])]
        for index, (company, rules) in enumerate(visible):
            state = '● 사용' if all(x['enabled'] for x in rules) else '○ 중지' if not any(x['enabled'] for x in rules) else '◐ 일부 사용'
            tags = ['even' if index % 2 == 0 else 'odd']
            if not any(x['enabled'] for x in rules):
                tags.append('paused')
            tree.insert('', 'end', iid=company, text=company, values=(' · '.join(x['category'] for x in rules), state), tags=tags)
        if previous and tree.exists(previous[0]):
            tree.selection_set(previous[0])
            tree.focus(previous[0])
        elif visible:
            # Show useful detail immediately, including after a search changes
            # the visible selection, rather than an empty neighboring table.
            tree.selection_set(visible[0][0])
            tree.focus(visible[0][0])
            select()
        else:
            selected[0] = None
        tree.yview_moveto(position)
        table_hint.configure(text=(f'{len(visible)} / {len(current)}개 그룹사 · 선택해 분류·검색어 확인 · 더블클릭해 조건 편집'
                                   if visible else '일치하는 그룹사가 없습니다. 이름·약칭을 확인해 주세요.'
                                   if current else '등록된 그룹사가 없습니다. 일괄 등록 또는 아래 입력란에서 추가해 주세요.'))
        count = sum(map(len, current.values()))
        active = sum(item['enabled'] for rules in current.values() for item in rules)
        summary.configure(text=f"등록 {len(current)}개 그룹사 · 조건 {count}개 중 {active}개 ON · 저장하고 적용 시 반영")
        redraw()
        refresh_controls()
    search.trace_add('write', lambda *args: draw())
    def clear():
        selected[0] = None
        tree.selection_remove(*tree.selection())
        presets.set('')
        name.set('')
        aliases.set('')
        exclude.set('')
        scope.set('제목 + RSS 설명')
        for var in enabled_topics.values():
            var.set(True)
        refresh_controls()
    def upsert(rules, old=None):
        company = rules[0]['company']
        existing = groups()
        if old and old != company and company in existing:
            raise RuleError("이미 등록된 그룹사 이름입니다.")
        if company not in existing and len(existing) >= 40:
            raise RuleError("그룹사는 최대 40개까지 등록할 수 있습니다.")
        previous = {x['category']: x for x in existing.get(old or company, [])}
        for item in rules:
            prior = previous.get(item['category'], {})
            item['enabled'] = prior.get('enabled', True)
            if prior.get('disabled_terms'):
                item['disabled_terms'] = [term for term in prior['disabled_terms'] if term in item['terms']]
            SearchRule(item)
        removed = {x['name'] for x in draft if x.get('profile') == 'company_topic' and x['company'] in (old, company)}
        if any(x['name'].casefold() == rule['name'].casefold() for x in draft if x['name'] not in removed for rule in rules):
            raise RuleError("기존 검색 조건과 표시 이름이 겹칩니다.")
        draft[:] = [x for x in draft if x['name'] not in removed] + rules
    def register():
        try:
            upsert(entries(), selected[0])
            clear()
            draw()
            show_list()
            return True
        except RuleError as exc:
            messagebox.showwarning('그룹사 설정', str(exc), parent=parent)
            return False
    def select(event=None):
        if not tree.selection():
            return
        company = tree.selection()[0]
        rules = groups().get(company)
        if not rules:
            return
        selected[0] = company
        refresh_controls()

    def load_company_form():
        company = selected[0]
        rules = groups().get(company)
        if not rules:
            return
        name.set(company)
        aliases.set(' | '.join(rules[0]['aliases']))
        exclude.set(rules[0].get('exclude', ''))
        scope.set('제목만' if rules[0].get('scope') == 'title' else '제목 + RSS 설명')
        for key, var in enabled_topics.items():
            var.set(any(x['category'] == key for x in rules))
        refresh_controls()
    tree.bind('<<TreeviewSelect>>', select)
    def delete():
        if tree.selection():
            company = tree.selection()[0]
            draft[:] = [x for x in draft if x.get('company') != company or x.get('profile') != 'company_topic']
            clear()
            draw()
    def toggle():
        if tree.selection():
            rules = groups()[tree.selection()[0]]
            use = not all(x['enabled'] for x in rules)
            for item in rules:
                item['enabled'] = use
            draw()
    def current_rule():
        return next((item for item in groups().get(selected[0], [])
                     if item['category'] == category_var.get()), None)
    def refresh_controls(event=None):
        rules = groups().get(selected[0], [])
        categories = [item['category'] for item in rules]
        category_picker.configure(values=categories, state='readonly' if rules else 'disabled')
        if category_var.get() not in categories:
            category_var.set(categories[0] if categories else '')
        selected_label.configure(text=f"{selected[0]} · 분류별 모니터링" if rules else
                                  '그룹사를 선택하면 분류와 검색어를 켜고 끌 수 있습니다.')
        position = keyword_tree.yview()[0]
        keyword_tree.delete(*keyword_tree.get_children())
        rule = current_rule()
        category_on.set(bool(rule and rule['enabled']))
        category_check.configure(state='normal' if rule else 'disabled',
                                 text='분류 ON' if category_on.get() else '분류 OFF')
        if rule:
            for index, term in enumerate(rule['terms']):
                on = term not in rule.get('disabled_terms', [])
                keyword_tree.insert('', 'end', iid=str(index), text=term,
                                    values=('ON' if on else 'OFF',), tags=() if on else ('paused',))
            keyword_tree.yview_moveto(position)
            keyword_hint.configure(text=f"검색어 {len(rule['terms']) - len(rule.get('disabled_terms', []))}/{len(rule['terms'])}개 ON · 클릭 또는 Space로 전환 · "
                                   + ('분류 ON: 선택한 검색어를 수집합니다.' if rule['enabled'] else '분류 OFF: 이 분류의 수집이 중지됩니다.')
                                   + ' · 저장하고 적용 시 반영')
        else:
            keyword_hint.configure(text='등록 목록에서 그룹사를 선택해 주세요. 변경은 저장하고 적용 시 반영됩니다.')
    def toggle_category():
        rule = current_rule()
        if rule:
            rule['enabled'] = category_on.get()
            draw()
    category_check.configure(command=toggle_category)
    category_picker.bind('<<ComboboxSelected>>', refresh_controls)
    def toggle_term(event):
        rule = current_rule()
        iid = keyword_tree.identify_row(event.y) if event.type == tk.EventType.ButtonPress else next(iter(keyword_tree.selection()), '')
        if not rule or not iid:
            return
        term = rule['terms'][int(iid)]
        disabled = set(rule.get('disabled_terms', []))
        if term in disabled:
            disabled.remove(term)
        else:
            if len(disabled) == len(rule['terms']) - 1:
                keyword_hint.configure(text='검색어는 하나 이상 ON으로 유지해 주세요. 전체 중지는 분류 OFF를 사용하세요.')
                return 'break'
            disabled.add(term)
        rule['disabled_terms'] = [term for term in rule['terms'] if term in disabled]
        refresh_controls()
        keyword_tree.selection_set(iid)
        keyword_tree.focus(iid)
        keyword_tree.focus_set()
        return 'break'
    keyword_tree.bind('<Button-1>', toggle_term)
    keyword_tree.bind('<space>', toggle_term)
    def add_category():
        try:
            category = new_category.get().strip()
            terms = validate_terms(phrases(new_terms.get()))
            make_entries('그룹사', ['그룹사'], {category: terms})
            if category.casefold() in {key.casefold() for key in topic_widgets}:
                raise RuleError('이미 있는 분류입니다. 위의 검색어 목록에서 수정해 주세요.')
            if len(topic_widgets) >= 12:
                raise RuleError('분류는 최대 12개까지 추가할 수 있습니다.')
            add_topic_widget(category, terms)
            enabled_topics[category].trace_add('write', preview)
            topic_widgets[category].bind('<KeyRelease>', preview)
            new_category.set('')
            new_terms.set('')
            preview()
            summary.configure(text=f"‘{category}’ 분류 추가됨 · 그룹사 등록 / 수정 또는 저장하고 적용을 눌러 연결하세요.")
        except RuleError as exc:
            messagebox.showwarning('분류 추가', str(exc), parent=parent)
            return False
        return True
    app.button(add_category_row, '새 분류 추가', add_category, True).pack(side='left')
    def batch(companies, values):
        original = list(draft)
        try:
            for company, names in companies.items():
                upsert(make_entries(company, names, values))
        except RuleError:
            draft[:] = original
            raise
        clear()
        draw()
    def all_presets():
        try:
            values = topics()
            batch(COMPANIES, {key: values[key] for key, var in enabled_topics.items() if var.get()})
        except RuleError as exc:
            messagebox.showwarning('그룹사 설정', str(exc), parent=parent)
    def import_file():
        path = filedialog.askopenfilename(parent=parent, title='발전그룹사 키워드 텍스트 불러오기', filetypes=[('텍스트 파일', '*.txt')])
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = raw.decode('cp949')
            companies, values = parse_template(text)
            batch(companies, values)
            for key, widget in topic_widgets.items():
                if key not in values:
                    continue
                widget.delete('1.0', 'end')
                widget.insert('1.0', ' | '.join(values[key]))
        except (OSError, UnicodeError, RuleError) as exc:
            messagebox.showwarning('파일 불러오기', str(exc), parent=parent)
    app.button(quick, '전력 및 발전 그룹사 일괄 등록', all_presets, True).pack(side='left', padx=(0, 8))
    app.button(quick, '텍스트 파일 불러오기', import_file).pack(side='left')
    def show_view(view):
        for frame in (list_view, editor_view):
            frame.pack_forget()
        view.pack(fill='both', expand=True)
        view_container.update_idletasks()
        canvas = view_container.master.master
        if isinstance(canvas, tk.Canvas):
            canvas.yview_moveto(0)
    def show_list():
        show_view(list_view)
    def new_group():
        clear()
        show_view(editor_view)
        body.nametowidget('company_name').focus_set()
    def edit_group():
        if selected[0] is None:
            summary.configure(text='먼저 목록에서 편집할 그룹사를 선택해 주세요.')
            return
        load_company_form()
        show_view(editor_view)
    tree.bind('<Double-1>', lambda event: edit_group())
    app.button(back_row, '← 등록 목록 / ON·OFF', show_list).pack(side='left')
    for label, command in [('새 그룹사', new_group), ('조건·분류 추가', edit_group), ('전체 ON/OFF', toggle), ('그룹사 삭제', delete)]:
        app.button(actions, label, command).pack(side='left')
    app.flow_buttons(actions)
    app.button(add_category_row, '그룹사 등록 / 수정', register).pack(side='right')
    def flush():
        if new_category.get().strip() or new_terms.get().strip():
            if not add_category():
                return False
        if name.get().strip() or aliases.get().strip():
            if not register():
                return False
        try:
            values = topics()
            for item in draft:
                if item.get('profile') == 'company_topic':
                    item['terms'] = values[item['category']]
                    if 'disabled_terms' in item:
                        item['disabled_terms'] = [term for term in item['disabled_terms'] if term in item['terms']]
                    SearchRule(item)
            return True
        except RuleError as exc:
            messagebox.showwarning('분류 검색어', str(exc), parent=parent)
            return False
    draw()
    return flush
