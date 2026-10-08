"""Pixel-scrolling, keyboard-accessible flat list for monitoring terms."""
import time
import tkinter as tk
from tkinter import font as tkfont
from ui_theme import TextHint


class SmoothTermList(tk.Frame):
    """Small Treeview-compatible surface with a fixed header and moving rows.

    Tk Treeview quantizes its viewport to whole rows. A canvas viewport lets
    the rows themselves move between those positions during wheel scrolling.
    """
    def __init__(self, parent, *, colors, height=4, **kwargs):
        super().__init__(parent, bg=colors['input'], takefocus=True, **kwargs)
        self.colors = colors
        self.font = tkfont.Font(root=self, family='맑은 고딕', size=11)
        self.row_height = self.font.metrics('linespace') + 18
        self.header_height = self.row_height + 2
        self.rows = {}
        self.chosen = ()
        self.focused = ''
        self.headings = {'#0': '검색어', 'state': '사용 상태'}
        self.state_width = self.font.measure('사용 상태') + 24
        self.header = tk.Canvas(self, bg=colors['input'], height=self.header_height,
                                highlightthickness=0)
        self.header.pack(fill='x')
        self.viewport = tk.Canvas(self, bg=colors['panel'], height=height*self.row_height,
                                  width=300, highlightthickness=0, yscrollincrement=1)
        self.viewport.pack(fill='both', expand=True)
        self.timer = None
        self.target = 0.0
        self.viewport.bind('<Configure>', lambda e: self._draw())
        self.viewport.bind('<MouseWheel>', self._wheel)
        self.header.bind('<MouseWheel>', self._wheel)
        self.bind('<MouseWheel>', self._wheel)
        self.viewport.bind('<Button-4>', lambda e: self._scroll(-self.row_height*2))
        self.viewport.bind('<Button-5>', lambda e: self._scroll(self.row_height*2))
        self.viewport.bind('<Button-1>', self._click)
        self.bind('<Up>', lambda e: self._navigate(-1))
        self.bind('<Down>', lambda e: self._navigate(1))
        self.bind('<Home>', lambda e: self._navigate_to(0))
        self.bind('<End>', lambda e: self._navigate_to(len(self.rows)-1))
        self.bind('<Destroy>', self._destroy, add='+')
        self.hint = TextHint(self.viewport, self._hint_text, colors)

    def _hint_text(self, x, y):
        index = int(self.viewport.canvasy(y)//self.row_height)
        items = self.get_children()
        return self.rows[items[index]]['text'] if 0 <= index < len(items) else ''

    def _destroy(self, event):
        if event.widget is self:
            self._cancel()

    def _cancel(self):
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None

    def _offset(self):
        return max(0, self.viewport.canvasy(0))

    def _limit(self):
        return max(0, len(self.rows)*self.row_height-self.viewport.winfo_height())

    def _move(self, pixels):
        total = max(1, len(self.rows)*self.row_height)
        self.viewport.yview_moveto(max(0, min(self._limit(), pixels))/total)

    def _scroll(self, pixels):
        self.hint.hide()
        start = self._offset()
        self.target = max(0, min(self._limit(),
                                (self.target if self.timer is not None else start)+pixels))
        self._cancel()
        if abs(self.target-start) < 1:
            return 'break'
        began = time.monotonic()
        target = self.target
        def step():
            self.timer = None
            progress = min(1, (time.monotonic()-began)/.18)
            self._move(start+(target-start)*(1-(1-progress)**3))
            if progress < 1:
                self.timer = self.after(16, step)
        self.timer = self.after(16, step)
        return 'break'

    def _wheel(self, event):
        return self._scroll(-event.delta/120*self.row_height*2)

    def yview(self, *args):
        if not args:
            return self.viewport.yview()
        if args[0] == 'moveto':
            self.yview_moveto(float(args[1]))
        elif args[0] == 'scroll':
            unit = self.viewport.winfo_height()*.85 if args[2] == 'pages' else self.row_height
            self._scroll(int(args[1])*unit)

    def yview_moveto(self, fraction):
        self._cancel()
        self._move(float(fraction)*len(self.rows)*self.row_height)
        self.target = self._offset()

    def configure(self, cnf=None, **kwargs):
        command = kwargs.pop('yscrollcommand', None)
        if command is not None:
            self.viewport.configure(yscrollcommand=command)
        return super().configure(cnf, **kwargs)

    config = configure

    def heading(self, column, **kwargs):
        self.headings[column] = kwargs.get('text', self.headings.get(column, ''))
        self._draw()

    def column(self, column, **kwargs):
        if column == 'state':
            self.state_width = max(self.state_width, kwargs.get('width', 0))
        self._draw()

    def tag_configure(self, *args, **kwargs):
        pass  # Paused rows use semantic theme colors in _draw.

    def insert(self, parent, index, *, iid, text='', values=(), tags=()):
        self.rows[str(iid)] = {'text': text, 'values': tuple(values), 'tags': tuple(tags)}
        self._draw()
        return str(iid)

    def delete(self, *items):
        self._cancel()
        for item in items:
            self.rows.pop(str(item), None)
        self.chosen = tuple(i for i in self.chosen if i in self.rows)
        self._draw()

    def get_children(self, item=''):
        return tuple(self.rows)

    def item(self, iid, option=None):
        row = self.rows[str(iid)]
        return row.get(option) if option else dict(row)

    def selection(self):
        return self.chosen

    def selection_set(self, iid):
        self.chosen = (str(iid),) if str(iid) in self.rows else ()
        self._draw()

    def focus(self, iid=None):
        if iid is not None:
            self.focused = str(iid)
        return self.focused

    def identify_row(self, y):
        if y < self.header_height:
            return ''
        index = int(self.viewport.canvasy(y-self.header_height)//self.row_height)
        items = self.get_children()
        return items[index] if 0 <= index < len(items) else ''

    def _click(self, event):
        # Deliver the same row coordinate contract as a Treeview to the editor.
        self.focus_set()
        self.event_generate('<Button-1>', x=event.x, y=event.y+self.header_height)
        return 'break'

    def _navigate(self, direction):
        items = self.get_children()
        index = items.index(self.chosen[0]) if self.chosen else (-1 if direction > 0 else len(items))
        return self._navigate_to(index+direction)

    def _navigate_to(self, index):
        items = self.get_children()
        if not items:
            return 'break'
        index = max(0, min(len(items)-1, index))
        self.selection_set(items[index])
        self.focus(items[index])
        top = index*self.row_height
        offset = self._offset()
        if top < offset:
            self._scroll(top-offset)
        elif top+self.row_height > offset+self.viewport.winfo_height():
            self._scroll(top+self.row_height-offset-self.viewport.winfo_height())
        return 'break'

    def _draw(self):
        width = max(1, self.viewport.winfo_width())
        c = self.colors
        self.header.delete('all')
        self.header.create_text(12, self.header_height/2, text=self.headings['#0'], anchor='w', font=self.font, fill=c['muted'])
        self.header.create_text(width-self.state_width/2, self.header_height/2, text=self.headings['state'], font=self.font, fill=c['muted'])
        self.viewport.delete('all')
        for index, (iid, row) in enumerate(self.rows.items()):
            top = index*self.row_height
            chosen = iid in self.chosen
            bg = c['hover'] if chosen else c['panel'] if index%2 == 0 else c['input']
            fg = c['muted'] if 'paused' in row['tags'] else c['fg']
            self.viewport.create_rectangle(0, top, width, top+self.row_height, fill=bg, outline='')
            text = row['text']
            available = max(0, width-self.state_width-24)
            if self.font.measure(text) > available:
                while text and self.font.measure(text+'…') > available:
                    text = text[:-1]
                text += '…'
            self.viewport.create_text(12, top+self.row_height/2, text=text, anchor='w', font=self.font, fill=fg)
            value = row['values'][0] if row['values'] else ''
            self.viewport.create_text(width-self.state_width/2, top+self.row_height/2, text=value,
                                      font=self.font, fill=c['accent'] if value == 'ON' else c['muted'])
        self.viewport.configure(scrollregion=(0, 0, width, len(self.rows)*self.row_height))
