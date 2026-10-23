"""A single, accessible editor for the schedule used by both sharing channels."""
import tkinter as tk

from notification_schedule import validate_schedule


def build_sharing_editor(app, body):
    c = app.colors
    schedule = app.config['sharing_schedule']
    app.label(body, '자동 공유 시간', 20).pack(anchor='w')
    app.label(body, '카카오 알림과 텔레그램 공유에 함께 적용됩니다. · 한국시간 (UTC+9)',
              10, c['muted']).pack(anchor='w', pady=(6, 20))
    enabled = tk.BooleanVar(value=schedule['enabled'])
    toggle = tk.Checkbutton(body, name='sharing_enabled', text='매일 지정한 시간에는 공유 보류',
        variable=enabled, bg=c['panel'], fg=c['fg'], selectcolor=c['input'],
        activebackground=c['panel'], activeforeground=c['fg'], font=('맑은 고딕', 12))
    toggle.pack(anchor='w', pady=(0, 18))
    times = tk.Frame(body, bg=c['panel'])
    times.pack(fill='x')
    variables, controls = {}, []
    for row, (key, title) in enumerate((('start', '보류 시작'), ('end', '모아서 공유할 시각'))):
        app.label(times, title, 12).grid(row=row, column=0, sticky='w', padx=(0, 24), pady=8)
        hour, minute = schedule[key].split(':')
        hour_var, minute_var = tk.StringVar(value=hour), tk.StringVar(value=minute)
        variables[key] = (hour_var, minute_var)
        for column, (part, variable, count) in enumerate((('hour', hour_var, 24), ('minute', minute_var, 60))):
            field = app.combobox(times, name=f'sharing_{key}_{part}', textvariable=variable,
                values=[f'{number:02d}' for number in range(count)], state='readonly', width=4,
                font=('맑은 고딕', 12))
            field.grid(row=row, column=column * 2 + 1, pady=8)
            app.label(times, '시' if column == 0 else '분', 11, c['muted']).grid(
                row=row, column=column * 2 + 2, padx=(6, 12))
            controls.append(field)
    app.label(body, '24시간 형식 · 시작 시각부터 보류하고 종료 시각부터 공유합니다.\n'
              '예: 22:00 → 09:00은 오늘 밤부터 다음 날 아침까지 보류합니다.',
              10, c['muted'], justify='left').pack(anchor='w', pady=(12, 22))
    preview = app.label(body, '', 12, c['accent'], name='sharing_preview', justify='left', wraplength=580)
    preview.pack(anchor='w', fill='x', pady=(0, 20))
    app.label(body, '보류 중에도 뉴스 수집은 계속되며, 공유 대상 기사는 대기열에 저장됩니다.\n'
              '종료 시각부터 중복·중요도 기준과 기관별 대표 기사 규칙에 따라 모아서 전송합니다.\n'
              '프로그램이 꺼져 있었다면 공유 가능한 시간에 다시 실행할 때 대기 뉴스를 전송합니다.\n'
              '직접 누른 테스트 메시지는 보류 시간에도 즉시 전송합니다.',
              10, c['muted'], justify='left', wraplength=580).pack(anchor='w', fill='x')
    app.label(body, '아래 ‘저장하고 적용’을 누르면 변경한 시간이 바로 적용됩니다.',
              11, c['muted']).pack(anchor='w', pady=(24, 0))

    def fit_text(event):
        width = max(100, event.width - int(body.cget('padx')) * 2)
        for child in body.winfo_children():
            if isinstance(child, tk.Label):
                child.configure(wraplength=width)
    body.bind('<Configure>', fit_text, add='+')

    def draft():
        return validate_schedule({'enabled': enabled.get(), **{
            key: ':'.join(variable.get() for variable in pair) for key, pair in variables.items()}})

    def refresh(*args):
        for field in controls:
            field.configure(state='readonly' if enabled.get() else 'disabled')
        try:
            value = draft()
        except ValueError as exc:
            preview.configure(text=str(exc), fg=c['warning'])
            return
        text = '시간 제한 없이 공유 대상 뉴스를 자동 전송합니다.'
        if value['enabled']:
            following = '다음 날 ' if value['start'] > value['end'] else ''
            text = (f"매일 {value['start']}부터 {following}{value['end']}까지 공유를 보류합니다.\n"
                    f"{value['end']}부터 대기 뉴스를 모아서 자동 공유합니다.")
        preview.configure(text=text, fg=c['accent'])

    enabled.trace_add('write', refresh)
    for pair in variables.values():
        for variable in pair:
            variable.trace_add('write', refresh)
    refresh()
    return draft
