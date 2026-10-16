from __future__ import annotations
import json, queue, threading, uuid, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List
from config.loader import ConfigStore
from logging_.context import bind as bind_log_context
from logging_.engine_logger import get_engine_logger
from .tasks import CheckTask, RunOptions, assign_session_ids, run_subfolder, run_totals
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


# How long a finished run's queue is kept for a client that never turned up.
# Swept lazily when the next run starts, so there is no janitor thread.
_ABANDONED_RUN_TTL_SEC = 15 * 60


def sse_open(run_id: str) -> "queue.Queue[str]":
    """
    Creates the run's queue. Called by start_run **before** the worker thread,
    so that nothing emitted can arrive before there is somewhere to put it.

    Unbounded on purpose. The previous queue held 100 messages and dropped the
    rest with a warning; buffering events until the browser connects is the
    whole point of this queue, and an ALL-mode run emits a few thousand. It is
    bounded in practice by the size of one run and freed when the stream ends.
    """
    q: "queue.Queue[str]" = queue.Queue()
    with _lock:
        _sse_queues[run_id] = q
    return q


def sse_attach(run_id: str) -> "queue.Queue[str] | None":
    """
    Hands the run's queue to an SSE client. Returns None when this process does
    not know the run.

    It used to create a fresh queue and store it under the run id, which
    silently replaced the one start_run had made - so `run_started`, emitted
    before any client could connect, went into a queue nobody would ever read
    (dev/planning.md, дефект 2). Attaching instead of subscribing is the fix,
    and it is what lets a client that connects late still receive everything.

    None has two causes, and the client cannot tell them apart: a run served by
    another gunicorn worker (state lives in process memory - the half of дефект
    2 this does not fix), or a run whose stream has already been read and
    cleaned up. Either way the caller answers once and closes, rather than
    holding open a stream that will never carry anything.
    """
    with _lock:
        return _sse_queues.get(run_id)


def sse_detach(run_id: str):
    """Called once the stream has delivered the closing sentinel."""
    with _lock:
        _sse_queues.pop(run_id, None)
        _runs_state.pop(run_id, None)


def _sweep_abandoned_runs():
    """
    Drops queues of finished runs nobody ever connected to.

    A client normally cleans up after itself via sse_detach; this covers the
    browser tab that was closed before the run ended, and the curl that never
    came. Runs still in flight are never touched - they have no finished_at.
    """
    deadline = time.time() - _ABANDONED_RUN_TTL_SEC
    with _lock:
        stale = [
            run_id for run_id, state in _runs_state.items()
            if state.get("finished_at") and state["finished_at"] < deadline
        ]
        for run_id in stale:
            _sse_queues.pop(run_id, None)
            _runs_state.pop(run_id, None)
    if stale:
        _engine_logger.debug("Swept %s abandoned run(s): %s", len(stale), ", ".join(stale))


def get_run_state(run_id: str):
    with _lock:
        return _runs_state.get(run_id, {"total": 0, "done": 0})


def _sse_settings(tasks: List[CheckTask], options: RunOptions) -> dict:
    """
    What the run was asked to do, for the results header.

    `country` is "MULTI" for a multi-geo run and the single country otherwise;
    the old payload carried a `urls` mapping for multi-geo only, which is why
    the header showed "? URLs" for every standard run. The task list is now a
    field of its own, so the header counts tasks instead of guessing.
    """
    countries = [task.country for task in tasks if task.country]
    return {
        "label": options.label,
        "country": "MULTI" if options.label == "multi-geo" else (countries[0] if countries else None),
        "network": options.network,
        "transport": options.channel.transport,
        "resolve": options.channel.resolve,
        "timeout_sec": options.timeout_sec,
        "make_screenshot": options.make_screenshot,
        "debug_mode": options.debug_mode,
    }


def _sse_task_list(tasks: List[CheckTask]) -> List[dict]:
    """
    The run's rows in submitted order, so the client can draw them all at once
    and fill each one in place as results arrive.

    `index` is the task's position in the input and the row's identity for the
    rest of the run: check_started and check_finished carry it, and the client
    addresses rows by it rather than by url+country. That is what makes the
    output order the input order (решение 5) instead of the order the pool
    happened to finish in.
    """
    return [
        {
            "index": task.index,
            "url": task.url,
            "country": task.country,
            "parsing_error": task.parsing_error,
        }
        for task in tasks
    ]


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

    _sweep_abandoned_runs()

    # Создаем очередь SSE _до_ запуска потока
    sse_open(run_id)

    _sse_emit(run_id, {
        "type": "run_started",
        "run_id": run_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "settings": _sse_settings(tasks, options),
        "totals": run_totals(tasks),
        "tasks": _sse_task_list(tasks),
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
        "index": task.index,
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
            "index": task.index,
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
            "index": task.index,
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
    with _lock:
        state = _runs_state[run_id]
        state["finished_at"] = time.time()

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

    _engine_logger.info("'run_finished' emitted. Closing the SSE queue.")

    # Сигналим обработчику SSE, что пора закрываться.
    with _lock:
        q = _sse_queues.get(run_id)
    if q:
        q.put(None)

    # Deliberately no cleanup here: a run can finish before the browser's
    # EventSource has even connected (300 unparseable rows take milliseconds),
    # and dropping the queue now would lose the whole run for that client. The
    # stream removes it once it has delivered the sentinel - see sse_detach -
    # and _sweep_abandoned_runs covers the client that never arrives.
