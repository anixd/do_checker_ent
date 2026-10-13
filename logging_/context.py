"""
Per-check logging context.

Every line written while a check is in flight should say which run and which URL
it belongs to. Threading that through the call chain by hand is what produced the
f"[{run_id}]" prefixes scattered over the engine, and they stopped at the first
function that did not happen to take run_id as an argument.

contextvars carry it instead. Under gevent the pool in engine/orchestrator.py is
greenlets rather than threads, and contextvars are isolated per greenlet: three
concurrent tasks yielding to each other each read back their own value. Pool
workers are reused, so a value outlives the task that set it - bind() is called
at the start of every task, never once per run.
"""

from __future__ import annotations

import contextvars
import logging

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
_url: contextvars.ContextVar[str | None] = contextvars.ContextVar("url", default=None)


def bind(run_id: str | None = None, url: str | None = None) -> None:
    """Attaches run/url to every subsequent log line in this greenlet."""
    if run_id is not None:
        _run_id.set(run_id)
    if url is not None:
        _url.set(url)


def clear() -> None:
    _run_id.set(None)
    _url.set(None)


def as_suffix() -> str:
    """Renders the context as ' [run=abc123 url=example.com]', or '' when empty."""
    parts = []
    run_id = _run_id.get()
    url = _url.get()
    if run_id:
        parts.append(f"run={run_id}")
    if url:
        parts.append(f"url={url}")
    return f" [{' '.join(parts)}]" if parts else ""


class ContextFilter(logging.Filter):
    """
    Fills %(ctx)s on every record.

    Installed on the handlers rather than on our own loggers, so that records
    from gunicorn and from third-party libraries carry the field too - a
    formatter referencing a missing field raises instead of logging.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.ctx = as_suffix()
        return True
