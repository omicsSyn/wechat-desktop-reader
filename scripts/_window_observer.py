"""Read-only foreground/minimized observer. Never activates or changes windows."""
import ctypes
from ctypes import wintypes
import json
import sys
import threading
import time
u = ctypes.WinDLL('user32', use_last_error=True)
u.GetForegroundWindow.restype = wintypes.HWND
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u.IsIconic.argtypes = [wintypes.HWND]
u.IsWindow.argtypes = [wintypes.HWND]
u.IsIconic.restype = wintypes.BOOL
u.IsWindow.restype = wintypes.BOOL
u.IsWindowVisible.argtypes=[wintypes.HWND]
u.IsWindowVisible.restype=wintypes.BOOL
u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
HOOK = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
u.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, HOOK, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
u.SetWinEventHook.restype = wintypes.HANDLE
u.UnhookWinEvent.argtypes = [wintypes.HANDLE]

def run(pid, hwnd):

    def window_pid(handle):
        value = wintypes.DWORD()
        u.GetWindowThreadProcessId(handle, ctypes.byref(value))
        return value.value

    def state():
        return {'exists': bool(u.IsWindow(hwnd)), 'visible':bool(u.IsWindowVisible(hwnd)), 'minimized': bool(u.IsIconic(hwnd)), 'wechat_foreground': window_pid(u.GetForegroundWindow()) == pid}
    if window_pid(hwnd) != pid:
        raise RuntimeError('Window/PID mismatch')
    events = []
    restore_events = []

    @HOOK
    def callback(hook, event, handle, obj, child, tid, stamp):
        if handle and window_pid(handle) == pid:
            if event == 3:
                events.append(int(stamp))
            elif event == 23 and handle == hwnd:
                restore_events.append(int(stamp))
    hook = u.SetWinEventHook(3, 3, None, callback, 0, 0, 0)
    if not hook:
        raise RuntimeError('Foreground observer unavailable')
    minimize_hook = u.SetWinEventHook(22, 23, None, callback, 0, 0, 0)
    if not minimize_hook:
        u.UnhookWinEvent(hook)
        raise RuntimeError('Minimization observer unavailable')
    stop = threading.Event()

    def input_thread():
        sys.stdin.readline()
        stop.set()
    threading.Thread(target=input_thread, daemon=True).start()
    start = time.monotonic()
    initial = state()
    print(json.dumps({'ready': True, 'initial': initial}), flush=True)
    count = 0
    minimized = 0
    foreground = 0
    missing = 0
    msg = wintypes.MSG()
    try:
        while not stop.is_set() and time.monotonic() - start < 180:
            while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            s = state()
            count += 1
            minimized += s['minimized']
            foreground += s['wechat_foreground']
            missing += not s['exists']
            stop.wait(0.02)
    finally:
        u.UnhookWinEvent(hook)
        u.UnhookWinEvent(minimize_hook)
    print(json.dumps({'final': state(), 'samples': count, 'minimized_samples': minimized, 'foreground_samples': foreground, 'missing_samples': missing, 'wechat_activation_events': len(events), 'minimize_end_events': len(restore_events), 'seconds': round(time.monotonic() - start, 3), 'completed_on_request': stop.is_set()}), flush=True)
if __name__ == '__main__':
    try:
        run(int(sys.argv[1]), int(sys.argv[2]))
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__}), flush=True)
        sys.exit(1)
