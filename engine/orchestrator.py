from __future__ import annotations
import json, queue, threading, uuid, os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, Any
from config.loader import ConfigStore
from logging_.engine_logger import get_engine_logger
from .worker import execute_check
from logging_.context import bind as bind_log_context
from providers.soax_session import make_session_id

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


def _run_checks_async(run_params: dict[str, Any], run_id: str):
    """
    Эта функция выполняется в отдельном потоке (Thread)
    и делает всю тяжелую работу.
    """

    # Bound once for the whole run: this thread serves exactly one run_id, so
    # every line below carries it without being told.
    bind_log_context(run_id=run_id)
    _engine_logger.info("Background thread started.")

    cfg = ConfigStore.get()
    urls = run_params.get("urls", [])

    # One SOAX session per checked URL by default. With session_scope="geo"
    # every URL shares a single session, and therefore a single exit IP -
    # this run always targets one country, so index 0 covers them all.
    scope = cfg.soax.session_scope

    task_params = run_params.copy()
    task_params["run_id"] = run_id

    session_ids = {
        u: make_session_id(run_id, i if scope == "task" else 0)
        for i, u in enumerate(urls)
    }

    def worker_task(u: str):
        # Pool workers are reused, so the context is bound per task rather
        # than per run - otherwise the previous URL would stick around.
        bind_log_context(run_id=run_id, url=u)
        _engine_logger.info("Task started.")

        # Передаем копию словаря + URL в воркер
        task_specific_params = task_params.copy()
        task_specific_params["url"] = u
        task_specific_params["session_id"] = session_ids[u]

        _sse_emit(run_id, {
            "type": "check_started",
            "run_id": run_id, "url": u,
            "country": task_params.get("country")
            # "region": task_params.get("region_code"),
            # "isp": task_params.get("isp")
        })

        try:
            # log: Перед блокирующим вызовом
            _engine_logger.debug("Calling execute_check...")
            res = execute_check(task_specific_params)
            _engine_logger.debug("execute_check finished.")
        except Exception as e:
            _engine_logger.error("Unhandled exception in execute_check: %s", e, exc_info=True)
            res = {  # Создаем 'error' результат
                "classification": "connect_error",
                "notes": f"Worker failed: {e}",
                "timings": {},
                "md_path": "error.md",
                "proxy_ext_ip": None,
                "png_path": None,
            }

        png_relative_path = None
        if res.get("png_path"):
            try:
                png_relative_path = os.path.relpath(res["png_path"], cfg.paths.logs_dir)
                png_relative_path = png_relative_path.replace(os.path.sep, '/')
            except ValueError:
                _engine_logger.error("Could not get relative path for screenshot: %s",
                                     res["png_path"], exc_info=True)
                png_relative_path = os.path.basename(res["png_path"])  # fallback на старое поведение

        row = {
            "url": u,
            "country": task_params.get("country"),
            "result": res["classification"],
            "http_code": res.get("http_code"),
            "ttfb_ms": res.get("timings", {}).get("ttfb_ms"),
            "ext_ip": res.get("proxy_ext_ip") or "-",
            "md_name": os.path.basename(res["md_path"]),
            "png_name": png_relative_path if png_relative_path else "",
            "notes": res.get("notes")  # Передаем 'notes' в UI
        }
        _sse_emit(run_id, {"type": "check_finished", "run_id": run_id, **row})
        return row

    # пул потоков для проверок
    with ThreadPoolExecutor(max_workers=cfg.execution.max_concurrency) as pool:
        _engine_logger.debug("Submitting %s tasks to the pool (max_workers=%s).",
                             len(urls), cfg.execution.max_concurrency)
        futs = [pool.submit(worker_task, u) for u in urls]
        for fut in as_completed(futs):
            try:
                row = fut.result()
                with _lock:
                    st = _runs_state[run_id]
                    st["rows"].append(row)
                    st["done"] += 1
            except Exception as e:
                _engine_logger.error("Future failed: %s", e, exc_info=True)

    # завершение запуска
    _engine_logger.info("All tasks finished.")
    st = _runs_state[run_id]

    # try:
        # summary_path = write_run_summary(cfg.paths.logs_dir, st["rows"])
        # summary_name = os.path.basename(summary_path) # если понадобится summary-лог, раскомментировать это и импорт
    # except Exception as e:
    #     _engine_logger.error(f"[{run_id}] Failed to write summary: {e}", exc_info=True)
    #     summary_name = "error.md"
    summary_name = None

    _sse_emit(run_id, {"type": "run_finished", "run_id": run_id,
                       "totals": {"ok": sum(1 for r in st["rows"] if r["result"] == "success"),
                                  "err": sum(1 for r in st["rows"] if r["result"] != "success"),
                                  "time_ms": int((time.time() - st["started_at"]) * 1000)},
                       "summary": summary_name})

    _engine_logger.info("'run_finished' emitted. Unsubscribing SSE.")

    # сигналим обработчику SSE, что пора закрываться
    with _lock:
        q = _sse_queues.get(run_id)
    if q:
        try:
            q.put(None, block=False)  # отправляем None как сигнал
        except queue.Full:
            _engine_logger.warning("SSE queue full while sending the None sentinel for run %s", run_id)

    # отписываемся от SSE, чтобы позволить Response() завершиться
    sse_unsubscribe(run_id)


def start_run(run_params: dict[str, Any]) -> str:
    """
    Вызывается из /run (HTTP).
    Должен вернуться НЕМЕДЛЕННО.
    """
    run_id = uuid.uuid4().hex[:12]
    total = len(run_params.get("urls", []))

    ts_folder = datetime.now().strftime("%H-%M-%S") + "_std-check"
    run_params["subfolder"] = ts_folder

    # Not bound to the context here: this runs in the request greenlet, which
    # is reused for later requests on the same connection.
    _engine_logger.info("[%s] Creating run state (total %s URLs).", run_id, total)

    state = {
        "run_id": run_id,
        "total": total,
        "done": 0,
        "rows": [],
        "started_at": time.time(),
    }
    with _lock:
        _runs_state[run_id] = state

    # Удаляем 'urls' из настроек, которые пойдут в SSE
    settings_for_sse = {k: v for k, v in run_params.items() if k != 'urls'}

    # Создаем очередь SSE _до_ запуска потока
    sse_subscribe(run_id)

    _sse_emit(run_id, {"type": "run_started", "run_id": run_id, "ts": datetime.now().isoformat(timespec="seconds"),
                       "settings": settings_for_sse})

    # запук в фоновом потоке
    _engine_logger.info("[%s] Spawning background thread...", run_id)
    thread = threading.Thread(
        target=_run_checks_async,
        args=(run_params, run_id),
        daemon=True  # Поток умрет, если gunicorn (parent поток) умрет
    )
    thread.start()

    return run_id


def start_multi_geo_run(run_params: dict[str, Any]) -> str:
    run_id = uuid.uuid4().hex[:12]
    tasks = run_params.get("tasks", [])
    total = len(tasks)

    _engine_logger.info("[%s] Creating Multi-Geo run state (total %s tasks).", run_id, total)

    # Инициализируем состояние запуска
    state = {
        "run_id": run_id,
        "total": total,
        "done": 0,
        "rows": [],
        "started_at": time.time(),
    }
    with _lock:
        _runs_state[run_id] = state

    # Подписываемся на SSE
    sse_subscribe(run_id)

    ts_folder = datetime.now().strftime("%H-%M-%S") + "_multi-geo"
    run_params["subfolder"] = ts_folder  # Передадим это воркеру

    # Стартовое событие для фронта
    _sse_emit(run_id, {
        "type": "run_started",
        "run_id": run_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "settings": {
            "country": "MULTI",  # Пометка для заголовка
            "urls": {str(i): t['url'] for i, t in enumerate(tasks)}
        }
    })

    # Запуск фонового процесса
    thread = threading.Thread(
        target=_run_multi_geo_async,
        args=(run_params, run_id),
        daemon=True
    )
    thread.start()

    return run_id


def _run_multi_geo_async(run_params: dict[str, Any], run_id: str):
    bind_log_context(run_id=run_id)
    _engine_logger.info("Multi-Geo background thread started.")
    cfg = ConfigStore.get()
    tasks = run_params.get("tasks", [])
    scope = cfg.soax.session_scope

    # Rules are locked on a session's first request: reusing one session ID
    # across countries would be rejected with 409 SESSION_PARAMS_MISMATCH.
    # Under scope="geo" the ID is therefore keyed on the country, not the task.
    geo_index: Dict[str, int] = {}
    for t in tasks:
        c = (t.get("country") or "").strip().lower()
        if c and c not in geo_index:
            geo_index[c] = len(geo_index)

    for i, t in enumerate(tasks):
        c = (t.get("country") or "").strip().lower()
        t["session_id"] = make_session_id(
            run_id, i if scope == "task" else geo_index.get(c, 0)
        )

    def worker_task(task_item: dict):
        url = task_item["url"]
        country = task_item.get("country")
        bind_log_context(run_id=run_id, url=url)

        if "parsing_error" in task_item:
            row = {
                "url": url,
                "country": country,
                "result": "error",
                "notes": task_item["parsing_error"]
            }
            _sse_emit(run_id, {"type": "check_finished", "run_id": run_id, **row})
            return row

        task_specific = run_params.copy()
        task_specific.update({
            "url": url,
            "country": country,
            "run_id": run_id,
            "session_id": task_item.get("session_id"),
        })
        task_specific.pop("tasks", None)

        _sse_emit(run_id, {
            "type": "check_started",
            "run_id": run_id, "url": url, "country": country
        })

        try:
            res = execute_check(task_specific)
        except Exception as e:
            _engine_logger.error("Worker failed: %s", e, exc_info=True)
            # АХТУНГ! Нужно вернуть _полную_ структуру, чтобы не упасть ниже
            res = {
                "classification": "connect_error",
                "notes": f"Worker failed: {e}",
                "timings": {},
                "md_path": "error.md",
                "proxy_ext_ip": None,
                "png_path": None,
            }

        png_relative_path = ""
        if res.get("png_path"):
            try:
                png_relative_path = os.path.relpath(res["png_path"], cfg.paths.logs_dir)
                png_relative_path = png_relative_path.replace(os.path.sep, '/')
            except Exception:
                png_relative_path = os.path.basename(res["png_path"])

        row = {
            "url": url,
            "country": country,
            "result": res["classification"],
            "http_code": res.get("http_code"),
            "ttfb_ms": res.get("timings", {}).get("ttfb_ms"),
            "ext_ip": res.get("proxy_ext_ip") or "-",
            "md_name": os.path.basename(res.get("md_path", "")),
            "png_name": png_relative_path,
            "notes": res.get("notes")
        }
        _sse_emit(run_id, {"type": "check_finished", "run_id": run_id, **row})
        return row

    with ThreadPoolExecutor(max_workers=cfg.execution.max_concurrency) as pool:
        futs = [pool.submit(worker_task, t) for t in tasks]
        for fut in as_completed(futs):
            try:
                row = fut.result()
                with _lock:
                    _runs_state[run_id]["rows"].append(row)
                    _runs_state[run_id]["done"] += 1
            except Exception as e:
                _engine_logger.error("Multi-Geo future failed: %s", e, exc_info=True)

    st = _runs_state[run_id]
    _sse_emit(run_id, {
        "type": "run_finished",
        "run_id": run_id,
        "totals": {
            "ok": sum(1 for r in st["rows"] if r.get("result") == "success"),
            "err": sum(1 for r in st["rows"] if r.get("result") != "success"),
            "time_ms": int((time.time() - st["started_at"]) * 1000)
        }
    })
    sse_unsubscribe(run_id)
