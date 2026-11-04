"""Inline composition regression checks using real Tk and native messages."""
import ctypes
from ctypes import wintypes
import sys
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from tk_runtime import create_root
from windows_ime import (Composition, WM_IME_STARTCOMPOSITION, WM_IME_COMPOSITION,
                         WM_IME_ENDCOMPOSITION, WM_IME_SETCONTEXT,
                         ISC_SHOWUICOMPOSITIONWINDOW, GCS_COMPSTR, GCS_RESULTSTR)


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.root = create_root()
        self.addCleanup(self.root.destroy)

    def entry(self, factory=tk.Entry, text='앞 뒤', cursor=2, **kwargs):
        w = factory(self.root, font=('맑은 고딕', 11), **kwargs)
        w.pack()
        w.insert(0, text)
        w.icursor(cursor)
        self.root.update()
        return w

    def test_syllable_stays_at_anchor_and_commits_once(self):
        for factory in (tk.Entry, ttk.Entry, ttk.Combobox, tk.Spinbox, ttk.Spinbox):
            with self.subTest(factory=factory):
                w = self.entry(factory)
                state = Composition(w)
                for preedit in ('ㅎ', '하', '한'):
                    state.replace(preedit)
                    self.root.update_idletasks()
                    self.assertEqual(w.get(), '앞 ' + preedit + '뒤')
                    self.assertEqual(w.index('insert'), 3)
                    self.assertEqual(state.start, 2)
                state.finish('한')
                state.replace('ㄱ')
                state.replace('글')
                state.finish('글')
                state.finish()
                self.assertEqual(w.get(), '앞 한글뒤')
                w.destroy()

    def test_cancel_restores_selection_and_backspace_only_removes_preedit(self):
        w = self.entry(text='앞 선택 뒤')
        w.selection_range(2, 4)
        state = Composition(w)
        state.replace('한')
        self.assertEqual(w.get(), '앞 한 뒤')
        state.finish()
        self.assertEqual(w.get(), '앞 선택 뒤')
        w.icursor(2)
        state.replace('하')
        state.replace('ㅎ')
        state.replace('')
        state.finish()
        self.assertEqual(w.get(), '앞 선택 뒤')

    def test_long_entry_keeps_visible_caret_and_anchor(self):
        w = self.entry(text='가나다라마바사 ' * 20, cursor=160, width=12)
        w.icursor('end')
        w.xview('end')
        self.root.update_idletasks()
        initial = w.xview()[0]
        state = Composition(w)
        anchor = w.index('insert')
        for preedit in ('ㅎ', '하', '한', '하', 'ㅎ', '한'):
            state.replace(preedit)
            self.root.update_idletasks()
            self.assertEqual(state.start, anchor)
            self.assertEqual(w.index('insert'), anchor + 1)
            self.assertGreater(w.xview()[0], initial - 0.02)
            x, _, width, _ = w.bbox(anchor)
            self.assertGreaterEqual(x, 0)
            self.assertLess(x, w.winfo_width())
        state.finish('한')
        self.assertTrue(w.get().endswith(' 한'))

    def test_text_composition_is_one_undo_group(self):
        w = tk.Text(self.root, undo=True)
        w.pack()
        w.insert('1.0', '앞 뒤')
        w.edit_reset()
        w.mark_set('insert', '1.2')
        state = Composition(w)
        for text in ('ㅎ', '하', '한'):
            state.replace(text)
        state.finish('한')
        self.assertEqual(w.get('1.0', 'end-1c'), '앞 한뒤')
        w.edit_undo()
        self.assertEqual(w.get('1.0', 'end-1c'), '앞 뒤')
        w.edit_redo()
        self.assertEqual(w.get('1.0', 'end-1c'), '앞 한뒤')


@unittest.skipUnless(sys.platform == 'win32', 'Windows IME only')
class NativeImeTests(CompositionTests):
    def send(self, widget, message, flags=0, drain=True):
        user = ctypes.WinDLL('user32')
        user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user.SendMessageW.restype = ctypes.c_ssize_t
        result = user.SendMessageW(widget.winfo_id(), message, 0, flags)
        if drain:
            self.root._windows_ime.drain()
        return result

    def test_native_messages_render_inline_commit_and_cancel(self):
        w = self.entry()
        ime = self.root._windows_ime
        hwnd = w.winfo_id()
        self.assertIn(hwnd, ime.controls)
        # Supply deterministic IME payloads, retaining the real HWND subclass
        # dispatch and real Tk rendering. No keyboard/layout changes are needed.
        with patch.object(ime.imm, 'ImmGetContext', return_value=1), \
                patch.object(ime.imm, 'ImmReleaseContext'), \
                patch.object(ime.imm, 'ImmGetCompositionStringW', return_value=0), \
                patch.object(ime, 'read_string') as read:
            self.send(w, WM_IME_STARTCOMPOSITION)
            for text in ('ㅎ', '하', '한'):
                read.return_value = text
                self.send(w, WM_IME_COMPOSITION, GCS_COMPSTR)
                self.assertEqual(w.get(), '앞 ' + text + '뒤')
                self.assertEqual(w.index('insert'), 3)
            read.side_effect = ['한', 'ㄱ']
            self.send(w, WM_IME_COMPOSITION, GCS_RESULTSTR | GCS_COMPSTR)
            self.assertEqual(w.get(), '앞 한ㄱ뒤')
            read.side_effect = None
            read.return_value = '글'
            self.send(w, WM_IME_COMPOSITION, GCS_RESULTSTR)
            self.send(w, WM_IME_ENDCOMPOSITION)
            self.assertEqual(w.get(), '앞 한글뒤')
            self.send(w, WM_IME_STARTCOMPOSITION)
            read.return_value = 'ㅁ'
            self.send(w, WM_IME_COMPOSITION, GCS_COMPSTR)
            self.send(w, WM_IME_COMPOSITION, 0)
            self.send(w, WM_IME_ENDCOMPOSITION)
            self.assertEqual(w.get(), '앞 한글뒤')
        w.destroy()
        self.assertNotIn(hwnd, ime.controls)

    def test_real_ime_payload_and_result(self):
        w = self.entry()
        w.focus_force()
        self.root.update()
        ime = self.root._windows_ime
        context = ime.imm.ImmGetContext(w.winfo_id())
        if not context:
            self.skipTest('No desktop IME context in this session')
        setter = ime.imm.ImmSetCompositionStringW
        setter.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        setter.restype = wintypes.BOOL
        try:
            self.send(self.root, WM_IME_STARTCOMPOSITION)
            for text in ('ㅎ', '하', '한'):
                encoded = text.encode('utf-16-le')
                buffer = ctypes.create_string_buffer(encoded)
                if not setter(context, 9, buffer, len(encoded), None, 0):
                    self.skipTest('Active IME does not support SCS_SETSTR')
                # SCS_SETSTR updates IMM storage; explicitly deliver the status
                # notification, just as the input method does after a keystroke.
                self.send(self.root, WM_IME_COMPOSITION, GCS_COMPSTR)
                self.root.update()
                ime.drain()
                self.assertEqual(w.get(), '앞 ' + text + '뒤')
                self.assertEqual(w.index('insert'), 3)
            self.assertTrue(ime.imm.ImmNotifyIME(context, 0x15, 1, 0))
            self.root.update()
            ime.drain()
            self.assertEqual(w.get(), '앞 한뒤')
            self.assertIsNone(ime.controls[w.winfo_id()].start)
        finally:
            ime.imm.ImmReleaseContext(w.winfo_id(), context)

    def test_focus_switch_commits_to_original_field_and_releases_it(self):
        first = self.entry()
        second = self.entry(text='other', cursor=5)
        first.focus_force()
        self.root.update()
        ime = self.root._windows_ime
        state = ime.controls[first.winfo_id()]
        state.replace('한')
        with patch.object(ime.imm, 'ImmGetContext', return_value=0):
            second.focus_force()
            self.root.update()
        self.assertEqual(first.get(), '앞 한뒤')
        self.assertEqual(second.get(), 'other')
        self.assertIsNone(state.start)
        self.assertIs(ime.focused[self.root.winfo_id()], ime.controls[second.winfo_id()])
        button = tk.Button(self.root, text='Save')
        button.pack()
        button.focus_force()
        self.root.update()
        self.assertNotIn(self.root.winfo_id(), ime.focused)

    def test_text_preedit_caret_stays_on_right_with_zero_ime_offset(self):
        w = tk.Text(self.root)
        w.pack()
        w.insert('1.0', '앞 뒤')
        w.mark_set('insert', '1.2')
        self.root.update()
        ime = self.root._windows_ime
        state = ime.controls[w.winfo_id()]
        for text in ('ㅎ', '하', '한'):
            ime.pending.append((state, 'composition', (None, text, 0)))
            ime.drain()
            self.assertEqual(w.get('1.0', 'end-1c'), '앞 ' + text + '뒤')
            self.assertEqual(w.index('insert'), '1.3')
            self.assertEqual(state.start, '1.2')

    def test_hides_only_composition_ui_and_leaves_readonly_alone(self):
        w = self.entry()
        ime = self.root._windows_ime
        flags = ISC_SHOWUICOMPOSITIONWINDOW | 0xF
        with patch.object(ime.comctl, 'DefSubclassProc', return_value=0) as default:
            ime.window_proc(w.winfo_id(), WM_IME_SETCONTEXT, 1, flags, 1, 0)
            self.assertEqual(default.call_args.args[-1], 0xF)
            w.configure(state='readonly')
            ime.editable(ime.controls[w.winfo_id()])
            ime.window_proc(w.winfo_id(), WM_IME_SETCONTEXT, 1, flags, 1, 0)
            self.assertEqual(default.call_args.args[-1], flags)

    def test_click_finishes_preedit_before_caret_moves(self):
        w = self.entry()
        ime = self.root._windows_ime
        state = ime.controls[w.winfo_id()]
        state.replace('한')
        with patch.object(ime.imm, 'ImmGetContext', return_value=0):
            w.event_generate('<ButtonPress-1>', x=1, y=5)
        self.assertIsNone(state.start)
        self.assertEqual(w.get(), '앞 한뒤')
        self.assertEqual(w.index('insert'), 0)

    def test_native_callback_never_calls_tk_or_schedules_tk_work(self):
        w = self.entry()
        state = self.root._windows_ime.controls[w.winfo_id()]
        with patch.object(w, 'cget') as cget, patch.object(state, 'begin') as begin, \
                patch.object(self.root, 'after') as after, \
                patch.object(self.root, 'after_idle') as idle:
            self.send(w, WM_IME_STARTCOMPOSITION, drain=False)
            cget.assert_not_called()
            begin.assert_not_called()
            after.assert_not_called()
            idle.assert_not_called()
        self.root._windows_ime.drain()
        self.assertEqual(state.start, 2)

    def test_queued_result_precedes_following_english_key(self):
        w = self.entry(text='', cursor=0)
        w.focus_force()
        self.root.update()
        ime = self.root._windows_ime
        state = ime.controls[w.winfo_id()]
        ime.pending.append((state, 'composition', ('한', None, -1)))
        w.event_generate('<KeyPress>', keysym='a')
        self.assertEqual(w.get(), '한a')

    def test_queued_work_is_discarded_when_control_is_destroyed(self):
        w = self.entry()
        ime = self.root._windows_ime
        state = ime.controls[w.winfo_id()]
        self.send(w, WM_IME_STARTCOMPOSITION, drain=False)
        w.destroy()
        with patch.object(state, 'begin') as begin:
            ime.drain()
            begin.assert_not_called()
        self.assertFalse(state.alive)

    def test_posted_messages_inside_mainloop_do_not_abort_python(self):
        probe = Path(__file__).with_name('ime_crash_probe.py')
        result = subprocess.run([sys.executable, '-X', 'faulthandler', str(probe), '300'],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS: 300 syllables', result.stdout)

    def test_reopen_dialog_drops_pending_work_for_old_window(self):
        ime = self.root._windows_ime
        for _ in range(25):
            dialog = tk.Toplevel(self.root)
            value = tk.StringVar(dialog)
            entry = tk.Entry(dialog, textvariable=value)
            entry.pack()
            entry.focus_force()
            self.root.update()
            hwnd, owner = entry.winfo_id(), dialog.winfo_id()
            state = ime.controls[hwnd]
            ime.pending.append((state, 'composition', ('한', None, -1)))
            ime.drain()
            self.assertEqual(value.get(), '한')
            ime.pending.append((state, 'composition', ('글', None, -1)))
            dialog.destroy()
            ime.drain()
            self.assertEqual(value.get(), '한')
            self.assertNotIn(hwnd, ime.controls)
            self.assertNotIn(owner, ime.hooks)
            self.assertNotIn(owner, ime.focused)
