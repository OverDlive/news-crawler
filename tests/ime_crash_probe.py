"""Isolated fatal-crash regression: real Windows dispatch, not SendMessage.

The old IME bridge aborts in PyEval_RestoreThread before 100 iterations. Posted
messages must be delivered by mainloop while StringVar traces schedule Tcl work.
Run in a child process so a regression cannot kill the test runner.
"""
import ctypes
from ctypes import wintypes
import faulthandler
from pathlib import Path
import sys
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tk_runtime import create_root


def main(cycles):
    faulthandler.enable()
    faulthandler.dump_traceback_later(max(45, cycles // 10), exit=True)
    root = create_root()
    value = tk.StringVar(root)
    entry = tk.Entry(root, textvariable=value)
    entry.pack()
    value.trace_add('write', lambda *args: root.after_idle(lambda: None))
    root.update()
    # Target the hooked control directly so this synthetic payload test neither
    # takes keyboard focus nor loses messages when another app becomes active.
    # Real toplevel routing is covered by test_real_ime_payload_and_result.
    root.withdraw()
    target = entry.winfo_id()
    ime = root._windows_ime
    user = ctypes.WinDLL('user32')
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL
    count = 0
    failures = []

    def report(*error):
        failures.append(str(error))
        root.destroy()
    root.report_callback_exception = report

    def post():
        nonlocal count
        if count == cycles:
            ime.drain()
            if value.get() != '한' * cycles:
                failures.append(f'Text mismatch: expected {cycles}, got {len(value.get())}')
            root.destroy()
            return
        count += 1
        if count % 250 == 0:
            print(f'Posted {count} syllables', flush=True)
        for message, flags in ((0x10d, 0), (0x10f, 8), (0x10f, 0x800), (0x10e, 0)):
            if not user.PostMessageW(target, message, 0, flags):
                raise RuntimeError('PostMessageW failed')
        root.after(1, post)

    with patch.object(ime.imm, 'ImmGetContext', return_value=1), \
            patch.object(ime.imm, 'ImmReleaseContext'), \
            patch.object(ime.imm, 'ImmGetCompositionStringW', return_value=1), \
            patch.object(ime, 'read_string', return_value='한'):
        root.after(10, post)
        root.mainloop()
    if failures:
        raise AssertionError(failures)
    assert not ime.hooks and not ime.controls and not ime.pending
    assert ime.timer is None
    faulthandler.cancel_dump_traceback_later()
    print(f'PASS: {cycles} syllables, {cycles * 4} posted IME messages; exact text preserved')


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 300)
