from __future__ import annotations
import json, queue, threading, uuid, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List
from config.loader import ConfigStore
from logging_.context import bind as bind_log_context
from logging_.engine_logger import get_engine_logger
from .tasks import CheckTask, RunOptions, assign_session_ids, run_subfolder
from .worker import execute_check

_engine_logger = get_engine_logger()

_runs_state: Dict[str, Dict[str, Any]] = {}
_sse_queues: Dict[str, "queue.Queue[str]"] = {}
_lock = threading.Lock()


def _sse_emit(run_id: str, payload: dict):
    msg = json.dumps(payload, ensure_ascii=False)

    _engine_logger.debug(
        "Emitting SSE event %s for run %s (url=%s)",
        payload.get("type"), run_id, payload.get("url", "N/A"),
    )

    with _lock:
        q = _sse_queues.get(run_id)
    if q:
        try:
            q.put(msg, block=False)
        except queue.Full:
            _engine_logger.warning("SSE queue full for run %s", run_id)


def sse_subscribe(run_id: str) -> "queue.Queue[str]":
    """Вызывается UI (SSE) для подписки на события."""
    q: "queue.Queue[str]" = queue.Queue(maxsize=100)
    with _lock:
        _sse_queues[run_id] = q
    return q


def sse_unsubscribe(run_id: str):
    """Вызывается, когда SSE-поток завершен."""
    with _lock:
        _sse_queues.pop(run_id, None)


def get_run_state(run_id: str):
    with _lock:
        return _runs_state.get(run_id, {"total": 0, "done": 0})


def _sse_settings(tasks: List[CheckTask], options: RunOptions) -> dict:
    """
    The run_started payload, kept byte-for-byte compatible with what the two
    old paths sent, so app/static/js/main.js needs no changes in this branch.

    That includes the oddity of the standard run not carrying `urls` at all,
    which is why the results header shows "? URLs". The payload is replaced
    wholesale in Этап 4, together with the delivery fix - today run_started
    rarely reaches the client at all (see dev/planning.md, дефект 2).
    """
    if options.label == "multi-geo":
        return {
            "country": "MULTI",
            "urls": {str(task.index): task.url for task in tasks},
        }

    countries = [task.country for task in tasks if task.country]
    return {
        "country": countries[0] if countries else None,
        "network": options.network,
        "timeout_sec": options.timeout_sec,
        "make_screenshot": options.make_screenshot,
        "debug_mode": options.debug_mode,
    }


def start_run(tasks: List[CheckTask], options: RunOptions) -> str:
    """
    Вызывается из роутов (HTTP). Должен вернуться НЕМЕДЛЕННО.

    One entry point for both pages: the route decides what the tasks are, this
    decides nothing about them.
    """
    run_id = uuid.uuid4().hex[:12]
    options.run_id = run_id
    options.subfolder = run_subfolder(options.label, options.channel)

    cfg = ConfigStore.get()
    assign_session_ids(tasks, run_id, cfg.soax.session_scope)

    total = len(tasks)
    # Not bound to the logging context here: this runs in the request greenlet,
    # which is reused for later requests on the same connection.
    _engine_logger.info(
        "[%s] Creating run state: %s tasks, label=%s, channel=%s, session_scope=%s",
        run_id, total, options.label, options.channel.slug, cfg.soax.session_scope,
    )

    state = {
        "run_id": run_id,
        "total": total,
        "done": 0,
        "rows": [],
        "started_at": time.time(),
    }
    with _lock:
        _runs_state[run_id] = state

    # Создаем очередь SSE _до_ запуска потока
    sse_subscribe(run_id)

    _sse_emit(run_id, {
        "type": "run_started",
        "run_id": run_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "settings": _sse_settings(tasks, options),
    })

    _engine_logger.info("[%s] Spawning background thread...", run_id)
    thread = threading.Thread(
        target=_run_async,
        args=(tasks, options),
        daemon=True,  # Поток умрет, если gunicorn (parent поток) умрет
    )
    thread.start()

    return run_id


def _row_for_failed_task(task: CheckTask) -> dict:
    """A row for a line that never reached the provider (bad format, bad geo)."""
    return {
        "url": task.url,
        "country": task.country,
        "result": "error",
        "notes": task.parsing_error,
    }


def _png_relative(png_path: str | None, logs_dir: str) -> str:
    if not png_path:
        return ""
    try:
        relative = os.path.relpath(png_path, logs_dir)
        return relative.replace(os.path.sep, "/")
    except ValueError:
        _engine_logger.error("Could not get relative path for screenshot: %s",
                             png_path, exc_info=True)
        return os.path.basename(png_path)


def _run_async(tasks: List[CheckTask], options: RunOptions):
    """
    Выполняется в отдельном потоке (Thread) и делает всю тяжелую работу.
    """
    run_id = options.run_id

    # Bound once for the whole run: this thread serves exactly one run_id, so
    # every line below carries it without being told.
    bind_log_context(run_id=run_id)
    _engine_logger.info("Background thread started (%s tasks).", len(tasks))

    cfg = ConfigStore.get()

    def worker_task(task: CheckTask) -> dict:
        # Pool workers are reused, so the context is bound per task rather
        # than per run - otherwise the previous URL would stick around.
        bind_log_context(run_id=run_id, url=task.url)

        if not task.is_valid:
            _engine_logger.info("Task rejected before the provider: %s", task.parsing_error)
            row = _row_for_failed_task(task)
            _sse_emit(run_id, {"type": "check_finished", "run_id": run_id, **row})
            return row

        _engine_logger.info("Task started.")
        _sse_emit(run_id, {
            "type": "check_started",
            "run_id": run_id,
            "url": task.url,
            "country": task.country,
        })

        try:
            _engine_logger.debug("Calling execute_check...")
            res = execute_check(task, options)
            _engine_logger.debug("execute_check finished.")
        except Exception as e:
            _engine_logger.error("Unhandled exception in execute_check: %s", e, exc_info=True)
            res = {
                "classification": "connect_error",
                "notes": f"Worker failed: {e}",
                "timings": {},
                "md_path": "error.md",
                "proxy_ext_ip": None,
                "png_path": None,
            }

        row = {
            "url": task.url,
            "country": task.country,
            "result": res["classification"],
            "http_code": res.get("http_code"),
            "ttfb_ms": res.get("timings", {}).get("ttfb_ms"),
            "ext_ip": res.get("proxy_ext_ip") or "-",
            "md_name": os.path.basename(res.get("md_path", "")),
            "png_name": _png_relative(res.get("png_path"), cfg.paths.logs_dir),
            "notes": res.get("notes"),
        }
        _sse_emit(run_id, {"type": "check_finished", "run_id": run_id, **row})
        return row

    with ThreadPoolExecutor(max_workers=cfg.execution.max_concurrency) as pool:
        _engine_logger.debug("Submitting %s tasks to the pool (max_workers=%s).",
                             len(tasks), cfg.execution.max_concurrency)
        futures = [pool.submit(worker_task, task) for task in tasks]
        for future in as_completed(futures):
            try:
                row = future.result()
                with _lock:
                    state = _runs_state[run_id]
                    state["rows"].append(row)
                    state["done"] += 1
            except Exception as e:
                _engine_logger.error("Future failed: %s", e, exc_info=True)

    _engine_logger.info("All tasks finished.")
    state = _runs_state[run_id]

    _sse_emit(run_id, {
        "type": "run_finished",
        "run_id": run_id,
        "totals": {
            "ok": sum(1 for r in state["rows"] if r.get("result") == "success"),
            "err": sum(1 for r in state["rows"] if r.get("result") != "success"),
            "time_ms": int((time.time() - state["started_at"]) * 1000),
        },
        "summary": None,
    })

    _engine_logger.info("'run_finished' emitted. Unsubscribing SSE.")

    # Сигналим обработчику SSE, что пора закрываться. The Multi-Geo path used to
    # skip this, so its stream stayed open until the client gave up.
    with _lock:
        q = _sse_queues.get(run_id)
    if q:
        try:
            q.put(None, block=False)
        except queue.Full:
            _engine_logger.warning("SSE queue full while sending the None sentinel for run %s",
                                   run_id)

    sse_unsubscribe(run_id)
