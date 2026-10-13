"""
Gunicorn configuration.

Kept in a file rather than on the compose command line for three reasons:

* the hooks at the bottom are what turn a killed worker into a diagnosis
  instead of a bare "Worker (pid:N) was sent SIGTERM!";
* `logconfig_dict` routes gunicorn's own loggers through the application's
  handlers and format, so arbiter lines and engine lines end up side by side;
* the exec-form CMD in docker/Dockerfile cannot expand ${APP_PORT} - as a shell
  would - so the bind address has to be computed here.
"""

import faulthandler
import os
import sys
from datetime import datetime

# The config file is loaded by gunicorn before the application package, and its
# own directory (not the project root) is what ends up on sys.path.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from logging_.engine_logger import logging_dict  # noqa: E402

_LOG_DIR = os.environ.get("LOG_DIR", "/logs")
os.makedirs(_LOG_DIR, exist_ok=True)

# Written by the hooks below. Separate from engine.log on purpose: a raw
# faulthandler dump is not a log line, and interleaving it with formatted
# records would break anything that parses them.
_FAULT_LOG = os.path.join(_LOG_DIR, "faulthandler.log")

# faulthandler keeps the file descriptor, so the handle has to outlive the call
# that arms it.
_fault_fh = None


def _as_bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


bind = f"0.0.0.0:{os.environ.get('APP_PORT', '8888')}"
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
worker_class = "gevent"

# Previously unset, so gunicorn's own default of 30s applied. A screenshot
# (navigation plus screenshots.wait_after_load_sec) routinely outlives that, and
# the worker was killed in the middle of a run. Raised, but deliberately still
# bounded: a genuinely stuck worker must still be reaped, and worker_abort below
# records where it was stuck.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "180"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))

reload = _as_bool(os.environ.get("GUNICORN_RELOAD"), default=True)

# The app writes its own access.log in logging_/engine_logger.py.
accesslog = None
logconfig_dict = logging_dict(_LOG_DIR, os.environ.get("LOG_LEVEL", "INFO").upper())


def _arm_faulthandler():
    """
    Arms faulthandler for hard crashes: SIGSEGV, SIGBUS, SIGFPE, SIGILL and
    SIGABRT get a C-level handler that writes every thread's stack to
    faulthandler.log. Chromium's client library taking the worker down used to
    leave nothing behind at all.

    Note the division of labour around SIGABRT, which is the signal the arbiter
    sends a worker that missed its heartbeat:

    * faulthandler.register() cannot be used for it - enable() already owns the
      fatal signals and register() refuses with "signal 6 cannot be registered";
    * gunicorn installs its own SIGABRT handler in init_signals(), which runs
      *after* the post_fork hook, so for a timeout it is gunicorn's handler that
      wins and the worker_abort hook below is what produces the traceback;
    * that hook is Python, so it cannot run while the worker is wedged inside a
      C call. For that case engine/worker.py arms a C-level watchdog
      (faulthandler.dump_traceback_later) around the screenshot, which is the
      only unbounded operation in a check.
    """
    global _fault_fh
    if _fault_fh is None:
        try:
            _fault_fh = open(_FAULT_LOG, "a", encoding="utf-8")
        except OSError:
            faulthandler.enable()
            return
    faulthandler.enable(file=_fault_fh, all_threads=True)


def _dump_traceback(reason: str, pid: int, log):
    """
    Marks the dump in faulthandler.log and repeats it on stderr, where
    `docker-compose logs` picks it up immediately.
    """
    log.error("%s (pid=%s); traceback in %s", reason, pid, _FAULT_LOG)
    if _fault_fh is not None:
        try:
            _fault_fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} pid={pid} {reason} =====\n")
            _fault_fh.flush()
            faulthandler.dump_traceback(file=_fault_fh, all_threads=True)
        except (OSError, ValueError) as e:
            log.error("Could not write %s: %s", _FAULT_LOG, e)
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)


def on_starting(server):
    _arm_faulthandler()
    server.log.info(
        "Arbiter starting: bind=%s workers=%s class=%s timeout=%ss graceful=%ss reload=%s",
        bind, workers, worker_class, timeout, graceful_timeout, reload,
    )


def post_fork(server, worker):
    _arm_faulthandler()
    worker.log.info("Worker forked (pid=%s)", worker.pid)


def worker_abort(worker):
    """
    Fired on SIGABRT, which is what the arbiter sends a worker that has missed
    its heartbeat for `timeout` seconds. The dump here is the entire point of
    this file: it names the function the worker was stuck in.

    Under gevent the check pool is greenlets on the main thread, so the blocked
    greenlet's own frames are what the dump shows.
    """
    _dump_traceback(f"Worker aborted after {timeout}s without a heartbeat", worker.pid, worker.log)


def worker_int(worker):
    _dump_traceback("Worker interrupted (SIGINT/SIGQUIT)", worker.pid, worker.log)


def worker_exit(server, worker):
    # The arbiter's view of the exit; the worker logs its own "Worker exiting".
    server.log.info("Arbiter reaped worker (pid=%s)", getattr(worker, "pid", "?"))


def on_exit(server):
    server.log.info("Arbiter shutting down")
