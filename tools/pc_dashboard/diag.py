"""Service diagnostics (2026-11-23, owner: the standalone build hung on a friend's PC with nothing to show why).

setup(path): the packaged exe has no console (stdout / stderr are None), so everything printed, every uncaught
exception (main and threads) and a stack dump of all threads on a hard crash go to `path` (service.log, kept
< 2 MB by starting over). A watchdog thread dumps every thread's stack when an HTTP request has been running
> SLOW_S, at most once a minute, so a hang shows where it sits. Source runs only use the request watchdog.
"""
import faulthandler
import os
import sys
import threading
import time
import traceback

SLOW_S = 10.0
_f = None
_inflight = {}                    # thread id -> (start time, request line)
_lock = threading.Lock()


def log(msg):
    if _f is None:
        return
    try:
        _f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg.rstrip() + "\n")
        _f.flush()
    except Exception:
        pass


def setup(path, frozen):
    global _f
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 2_000_000:
            os.replace(path, path + ".old")
        _f = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:
        _f = None
        return
    if frozen or sys.stdout is None:
        sys.stdout = sys.stderr = _f
    try:
        faulthandler.enable(_f, all_threads=True)
    except Exception:
        pass

    def th_hook(a):
        log(f"uncaught in thread {a.thread.name if a.thread else '?'}:\n" +
            "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    threading.excepthook = th_hook
    old = sys.excepthook

    def hook(t, v, tb):
        log("uncaught:\n" + "".join(traceback.format_exception(t, v, tb)))
        old(t, v, tb)
    sys.excepthook = hook
    log(f"service start pid {os.getpid()} exe {sys.executable} argv {sys.argv[1:]}")
    threading.Thread(target=_watch, name="diag-watch", daemon=True).start()


def begin(line):
    with _lock:
        _inflight[threading.get_ident()] = (time.monotonic(), line)


def end():
    with _lock:
        _inflight.pop(threading.get_ident(), None)


def _watch():
    last = 0.0
    while True:
        time.sleep(2)
        now = time.monotonic()
        with _lock:
            slow = [(now - t0, ln) for t0, ln in _inflight.values() if now - t0 > SLOW_S]
        if slow and now - last > 60:
            last = now
            log("slow request(s): " + "; ".join(f"{ln} {s:.0f} s" for s, ln in slow) + " - all threads:")
            try:
                faulthandler.dump_traceback(_f, all_threads=True)
            except Exception:
                pass
