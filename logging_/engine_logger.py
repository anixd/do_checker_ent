import faulthandler
import logging
import logging.config
import os
import sys
from datetime import datetime
from flask import Flask, Response, request
from werkzeug.exceptions import HTTPException
from config.loader import ConfigStore

# One format for everything that writes a log line: the app, gunicorn's arbiter
# and workers, and any third-party library that logs through the root logger.
# %(ctx)s is filled by logging_.context.ContextFilter.
LOG_FORMAT = "%(asctime)s [%(levelname)s] [pid=%(process)d]%(ctx)s %(name)s: %(message)s"
ACCESS_FORMAT = "%(asctime)s [pid=%(process)d] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

ENGINE_LOG = "engine.log"
ACCESS_LOG = "access.log"

_engine_logger = None


def _request_target() -> str:
    """full_path appends a bare '?' when there is no query string."""
    return request.full_path.rstrip("?") or request.path


def get_engine_logger():
    """
    Gets the singleton logger instance.
    Setup is handled by setup_loggers() / logging_dict().
    """
    global _engine_logger
    if _engine_logger is None:
        _engine_logger = logging.getLogger("engine")
    return _engine_logger


def logging_dict(log_dir: str, level: str = "INFO") -> dict:
    """
    The single logging configuration, applied twice from two entry points:
    by setup_loggers() inside the app, and by gunicorn through `logconfig_dict`
    in docker/gunicorn.conf.py. Applying it in both places is what puts the
    arbiter's "Worker was sent SIGTERM!" and the engine's own lines into the
    same file, in the same format.

    dictConfig replaces handlers on the named loggers instead of appending, so
    running it a second time is harmless - appending instead would add a
    FileHandler per call and duplicate every line per gunicorn worker.
    """
    os.makedirs(log_dir, exist_ok=True)
    main_handlers = ["stdout", "engine_file"]

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "ctx": {"()": "logging_.context.ContextFilter"},
        },
        "formatters": {
            "main": {"format": LOG_FORMAT, "datefmt": DATE_FORMAT},
            "access": {"format": ACCESS_FORMAT, "datefmt": DATE_FORMAT},
        },
        "handlers": {
            # stdout so that `docker-compose logs` shows the application at all:
            # until now it only ever saw gunicorn, and the app logged to a file
            # nobody looks at while watching a container.
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "main",
                "filters": ["ctx"],
            },
            # All gunicorn workers append to this one file. There is no
            # cross-process locking: it holds because a FileHandler opens in
            # O_APPEND mode and writes each record in a single write() call,
            # which the kernel does not interleave for a reasonably short line.
            # A traceback arrives as one record too, so it stays in one piece.
            "engine_file": {
                "class": "logging.FileHandler",
                "filename": os.path.join(log_dir, ENGINE_LOG),
                "encoding": "utf-8",
                "formatter": "main",
                "filters": ["ctx"],
            },
            "access_file": {
                "class": "logging.FileHandler",
                "filename": os.path.join(log_dir, ACCESS_LOG),
                "encoding": "utf-8",
                "formatter": "access",
            },
        },
        "loggers": {
            "engine": {"level": level, "handlers": main_handlers, "propagate": False},
            "access": {"level": "INFO", "handlers": ["access_file"], "propagate": False},
            # gunicorn's own loggers. Its error log carries the worker lifecycle
            # (fork, timeout, SIGTERM), which is exactly what has to sit next to
            # the engine lines to be of any use.
            "gunicorn.error": {"level": "INFO", "handlers": main_handlers, "propagate": False},
            # Off on purpose: the app writes its own access.log in after_request.
            "gunicorn.access": {"level": "INFO", "handlers": [], "propagate": False},
        },
        # Catches libraries (urllib3, playwright, requests) that would otherwise
        # log into the void or straight to stderr with no context.
        "root": {"level": "WARNING", "handlers": main_handlers},
    }


def _local_version() -> str:
    try:
        with open(os.path.abspath("./VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def log_config_summary(logger: logging.Logger):
    """
    Dumps the effective configuration once per worker at startup.

    When a check misbehaves days later, the first question is always "with which
    settings was this running" - app.yaml is editable at runtime from /settings,
    so the file on disk is not evidence of what the process actually loaded.
    The package password is masked: these lines go to stdout and to ./logs.
    """
    from providers.soax_session import mask_secret

    cfg = ConfigStore.get()
    logger.info("Configuration summary (version %s, pid %s):", _local_version(), os.getpid())
    logger.info("  paths:       logs_dir=%s data_dir=%s", cfg.paths.logs_dir, cfg.paths.data_dir)
    logger.info("  execution:   max_concurrency=%s timeout_sec=%s",
                cfg.execution.max_concurrency, cfg.execution.timeout_sec)
    logger.info("  screenshots: enabled_default=%s max_workers=%s timeout_sec=%s wait_after_load_sec=%s",
                cfg.screenshots.enabled_default, cfg.screenshots.max_workers,
                cfg.screenshots.timeout_sec, cfg.screenshots.wait_after_load_sec)
    logger.info("  soax:        %s:%s session_scope=%s",
                cfg.soax.host, cfg.soax.port, cfg.soax.session_scope)
    logger.info("  transport:   default=%s (empty means it must be chosen per run)",
                cfg.soax.transport or "none")
    logger.info("  soax rules:  on_error=%s retries=%s bind_node=%s preflight=%s",
                cfg.soax.on_error, cfg.soax.on_error_retries, cfg.soax.bind_node, cfg.soax.preflight)
    logger.info("  credentials: package_password=%s package_id=%s",
                mask_secret(cfg.soax.package_password), mask_secret(cfg.soax.package_id))
    logger.info("  alive-check: enabled=%s targets=%s required_ok=%s timeout_sec=%s",
                cfg.alive_check.enabled, len(cfg.alive_check.targets),
                cfg.alive_check.required_ok, cfg.alive_check.timeout_sec)
    logger.info("  dns:         use_ipv6=%s (SOAX does not route IPv6)", cfg.dns.use_ipv6)
    logger.info("  logging:     level=%s", cfg.logging.level.upper())

    # Collected by ConfigStore.init(), which runs before the loggers exist.
    # This is where a bad transport value surfaces instead of killing startup.
    for warning in ConfigStore.warnings():
        logger.warning("Configuration: %s", warning)


def setup_loggers(app: Flask):
    """
    Applies the logging configuration and installs the diagnostics that make a
    crash readable after the fact: a faulthandler for hard failures, a handler
    for unhandled request exceptions, and the access log.
    """
    cfg = ConfigStore.get()
    logging.config.dictConfig(logging_dict(cfg.paths.logs_dir, cfg.logging.level.upper()))

    engine_logger = get_engine_logger()

    # A segfault or an abort inside Chromium's client library used to kill the
    # worker with no Python-level trace at all. Also arms the dumps triggered
    # from gunicorn's worker_abort hook.
    faulthandler.enable()

    @app.errorhandler(Exception)
    def _log_unhandled(exc):
        # HTTPException is Flask's own 404/405/400 machinery, not a failure.
        if isinstance(exc, HTTPException):
            return exc
        engine_logger.error(
            "Unhandled exception on %s %s", request.method, _request_target(), exc_info=True
        )
        return "Internal Server Error", 500

    access_logger = logging.getLogger("access")

    @app.after_request
    def log_access(response: Response):
        # SSE and static are skipped: too noisy to be worth anything.
        if request.path.startswith('/events/') or request.path.startswith('/static/'):
            return response

        access_logger.info(
            '%s - "%s %s" %s',
            request.remote_addr, request.method, _request_target(), response.status_code
        )
        return response

    engine_logger.info("Loggers initialized. Engine log level set to %s.", cfg.logging.level.upper())
    log_config_summary(engine_logger)
