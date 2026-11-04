"""Render Windows IME preedit inside Tk controls, with a stable text anchor.

Native results are consumed here so Tk cannot insert them twice. No keyboard
layout or system IME preferences are changed.
"""
import ctypes
from collections import deque
from ctypes import wintypes
import logging
import sys
import tkinter as tk

WM_NCDESTROY = 0x0082
WM_IME_STARTCOMPOSITION = 0x010D
WM_IME_ENDCOMPOSITION = 0x010E
WM_IME_COMPOSITION = 0x010F
WM_IME_SETCONTEXT = 0x0281
ISC_SHOWUICOMPOSITIONWINDOW = 0x80000000
GCS_COMPSTR = 0x0008
GCS_CURSORPOS = 0x0080
GCS_RESULTSTR = 0x0800


class Composition:
    """Replace only the preedit range, leaving surrounding text and view intact."""
    def __init__(self, widget):
        self.widget = widget
        self.multiline = isinstance(widget, tk.Text)
        self.start = None
        self.length = 0
        self.original = ''
        self.auto_separators = None
        self.alive = True
        self.editable = str(widget.cget('state')) not in ('disabled', 'readonly')
        self.owner = widget.winfo_toplevel().winfo_id()

    def offset(self, count):
        if self.multiline:
            return self.widget.index(f'{self.start} + {count} chars')
        return self.start + count

    def current(self):
        if self.multiline:
            return self.widget.get(self.start, self.offset(self.length))
        # Tk 8.6 indices count UTF-16 units; Python slices count code points.
        return str(self.widget.tk.call('string', 'range', self.widget.get(),
                                       self.start, self.start + self.length - 1))

    def begin(self):
        if self.start is not None:
            return
        w = self.widget
        self.start = w.index('insert')
        self.length = 0
        self.original = ''
        if self.multiline:
            if w.cget('undo'):
                w.edit_separator()
                self.auto_separators = w.cget('autoseparators')
                w.configure(autoseparators=False)
            selection = w.tag_ranges('sel')
            if selection:
                self.start = str(selection[0])
                self.original = w.get(*selection)
                self.length = int(w.tk.call('string', 'length', self.original))
                w.tag_remove('sel', '1.0', 'end')
        elif w.selection_present():
            self.start = w.index('sel.first')
            self.length = w.index('sel.last') - self.start
            self.original = self.current()
            w.selection_clear()

    def replace(self, value, cursor=None):
        self.begin()
        w = self.widget
        track_view = not self.multiline and w.winfo_ismapped()
        left = w.index('@0') if track_view else None
        changed = self.current() != value
        if self.multiline:
            if changed:
                w.replace(self.start, self.offset(self.length), value)
        else:
            if changed:
                w.delete(self.start, self.offset(self.length))
                w.insert(self.start, value)
        self.length = int(w.tk.call('string', 'length', value))
        position = self.offset(self.length if cursor is None else max(0, min(cursor, self.length)))
        if self.multiline:
            w.mark_set('insert', position)
            w.see('insert')
        else:
            w.icursor(position)
            # Deleting preedit can scroll a long Entry back to the left.
            # Restore its view before Tk's next display pass shows the caret.
            if track_view:
                if changed and w.index('@0') != left:
                    w.xview(left)
                w.tk.call('::tk::EntrySeeInsert', w._w)

    def finish(self, result=None):
        if result is not None:
            self.replace(result)
        elif self.start is not None:
            self.replace(self.original)
        if self.auto_separators is not None:
            self.widget.configure(autoseparators=self.auto_separators)
            self.widget.edit_separator()
        self.start = None
        self.length = 0
        self.original = ''
        self.auto_separators = None


class WindowsIme:
    def __init__(self, root):
        self.root = root
        self.controls = {}
        self.hooks = set()
        self.focused = {}
        self.pending = deque()
        self.draining = False
        self.timer = None
        self.closed = False
        self.imm = ctypes.WinDLL('imm32')
        self.comctl = ctypes.WinDLL('comctl32')
        self.callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
        self.callback = self.callback_type(self.window_proc)
        self.imm.ImmGetContext.argtypes = [wintypes.HWND]
        self.imm.ImmGetContext.restype = wintypes.HANDLE
        self.imm.ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
        self.imm.ImmReleaseContext.restype = wintypes.BOOL
        self.imm.ImmGetCompositionStringW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                    ctypes.c_void_p, wintypes.DWORD]
        self.imm.ImmGetCompositionStringW.restype = wintypes.LONG
        self.imm.ImmNotifyIME.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        wintypes.DWORD, wintypes.DWORD]
        self.imm.ImmNotifyIME.restype = wintypes.BOOL
        self.comctl.SetWindowSubclass.argtypes = [wintypes.HWND, self.callback_type,
                                                  ctypes.c_size_t, ctypes.c_size_t]
        self.comctl.SetWindowSubclass.restype = wintypes.BOOL
        self.comctl.RemoveWindowSubclass.argtypes = [wintypes.HWND, self.callback_type,
                                                     ctypes.c_size_t]
        self.comctl.RemoveWindowSubclass.restype = wintypes.BOOL
        self.comctl.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT,
                                               wintypes.WPARAM, wintypes.LPARAM]
        self.comctl.DefSubclassProc.restype = ctypes.c_ssize_t
        self.tag = 'InlineWindowsIme'
        for event in ('<ButtonPress-1>', '<<Cut>>', '<<Paste>>', '<<Clear>>',
                      '<<Undo>>', '<<Redo>>'):
            root.bind_class(self.tag, event, self.before_edit)
        # Process queued IME results before a following space/English key/caret
        # movement. This binding does not force completion on every IME key.
        root.bind_class(self.tag, '<KeyPress>', lambda event: self.drain())
        for name in ('Entry', 'TEntry', 'TCombobox', 'Spinbox', 'TSpinbox', 'Text'):
            # Map occurs before the first focus/WM_IME_SETCONTEXT message.
            root.bind_class(name, '<Map>', self.attach, add='+')
            root.bind_class(name, '<FocusIn>', self.attach, add='+')
            root.bind_class(name, '<FocusOut>', self.before_edit, add='+')
        root.bind('<Destroy>', self.destroy, add='+')
        self.timer = root.after(8, self.poll)

    def destroy(self, event):
        if event.widget is self.root:
            self.closed = True
            if self.timer is not None:
                self.root.after_cancel(self.timer)
                self.timer = None
            self.pending.clear()

    def poll(self):
        self.timer = None
        try:
            self.drain()
            for state in tuple(self.focused.values()):
                if state.alive:
                    self.editable(state)
        finally:
            if not self.closed:
                self.timer = self.root.after(8, self.poll)

    def drain(self):
        """Only call from Tk callbacks, never from a native window procedure.

        _tkinter owns a saved Tcl/Python thread state while dispatching Windows
        messages. Calling *any* Tcl command from a ctypes WNDPROC (even cget or
        after_idle) overwrites that state and aborts on the next Tcl callback.
        """
        if self.draining:
            return
        self.draining = True
        try:
            while self.pending:
                state, action, values = self.pending.popleft()
                if not state.alive or not self.editable(state):
                    continue
                if action == 'start':
                    state.begin()
                elif action == 'end':
                    state.finish()
                elif action == 'composition':
                    result, preedit, _ime_cursor = values
                    if result is not None:
                        state.finish(result)
                    if preedit is not None:
                        # Korean IMEs can report offset 0 for the syllable being
                        # composed. Keep the visible caret after the preedit,
                        # just like ordinary typing; the replacement anchor
                        # remains fixed independently of that visible caret.
                        state.replace(preedit)
        finally:
            self.draining = False

    def hook(self, hwnd):
        if hwnd not in self.hooks:
            if self.comctl.SetWindowSubclass(hwnd, self.callback, 1, 0):
                self.hooks.add(hwnd)
        return hwnd in self.hooks

    def attach(self, event):
        self.drain()
        w = event.widget
        hwnd = w.winfo_id()
        owner = w.winfo_toplevel().winfo_id()
        # Tk keeps native keyboard focus on the toplevel, not its Entry HWND.
        # Real IME messages therefore arrive at the toplevel window procedure.
        self.hook(owner)
        if hwnd not in self.controls and self.hook(hwnd):
            self.controls[hwnd] = Composition(w)
            w.bindtags((self.tag,) + w.bindtags())
        if hwnd in self.controls and w.focus_get() == w:
            previous = self.focused.get(owner)
            if previous is not None and previous is not self.controls[hwnd]:
                self.complete(previous.widget.winfo_id())
            self.focused[owner] = self.controls[hwnd]

    @staticmethod
    def editable(state):
        state.editable = str(state.widget.cget('state')) not in ('disabled', 'readonly')
        return state.editable

    def before_edit(self, event):
        self.drain()
        self.complete(event.widget.winfo_id())
        if event.type == tk.EventType.FocusOut:
            owner = event.widget.winfo_toplevel().winfo_id()
            state = self.focused.get(owner)
            if state is not None and state.widget is event.widget:
                self.focused.pop(owner, None)

    def complete(self, hwnd):
        state = self.controls.get(hwnd) or self.focused.get(hwnd)
        if state is None or state.start is None:
            return
        owner = state.widget.winfo_toplevel().winfo_id()
        context = self.imm.ImmGetContext(owner)
        if context:
            try:
                # NI_COMPOSITIONSTR / CPS_COMPLETE sends the result synchronously.
                self.imm.ImmNotifyIME(context, 0x0015, 0x0001, 0)
            finally:
                self.imm.ImmReleaseContext(owner, context)
            self.drain()
        if state.start is not None:
            # Preserve visible preedit if the IME supplies no result on blur.
            state.finish(state.current())

    def read_string(self, context, kind):
        length = self.imm.ImmGetCompositionStringW(context, kind, None, 0)
        if length < 0:
            return None
        buffer = ctypes.create_string_buffer(length + 2)
        copied = self.imm.ImmGetCompositionStringW(context, kind, buffer, length)
        if copied < 0:
            return None
        return buffer.raw[:copied].decode('utf-16-le')

    def composition(self, hwnd, state, flags):
        if flags == 0:
            self.pending.append((state, 'end', ()))
            return
        context = self.imm.ImmGetContext(hwnd)
        if not context:
            return
        try:
            # A message can commit one syllable and start the next together.
            result = self.read_string(context, GCS_RESULTSTR) if flags & GCS_RESULTSTR else None
            preedit = self.read_string(context, GCS_COMPSTR) if flags & GCS_COMPSTR else None
            cursor = self.imm.ImmGetCompositionStringW(context, GCS_CURSORPOS, None, 0)
        finally:
            self.imm.ImmReleaseContext(hwnd, context)
        self.pending.append((state, 'composition', (result, preedit, cursor)))

    def window_proc(self, hwnd, message, wparam, lparam, subclass_id, reference):
        state = self.controls.get(hwnd) or self.focused.get(hwnd)
        try:
            if message == WM_NCDESTROY:
                self.comctl.RemoveWindowSubclass(hwnd, self.callback, subclass_id)
                removed = self.controls.pop(hwnd, None)
                if removed is not None:
                    removed.alive = False
                self.hooks.discard(hwnd)
                self.focused.pop(hwnd, None)
                for owner, focused in tuple(self.focused.items()):
                    if focused is state:
                        self.focused.pop(owner, None)
            elif state is not None:
                if message == WM_IME_SETCONTEXT and state.editable:
                    # Only hide preedit UI; preserve Hanja/candidate UI flags.
                    lparam &= ~ISC_SHOWUICOMPOSITIONWINDOW
                elif message == WM_IME_STARTCOMPOSITION and state.editable:
                    self.pending.append((state, 'start', ()))
                    return 0
                elif message == WM_IME_COMPOSITION and state.editable:
                    self.composition(hwnd, state, lparam)
                    return 0
                elif message == WM_IME_ENDCOMPOSITION and state.editable:
                    self.pending.append((state, 'end', ()))
                    return 0
        except Exception:
            # Never let Python exceptions escape a native window procedure.
            logging.exception('Inline IME message failed')
            if message in (WM_IME_STARTCOMPOSITION, WM_IME_COMPOSITION, WM_IME_ENDCOMPOSITION):
                return 0
        return self.comctl.DefSubclassProc(hwnd, message, wparam, lparam)


def install(root):
    if sys.platform == 'win32' and not hasattr(root, '_windows_ime'):
        root._windows_ime = WindowsIme(root)
