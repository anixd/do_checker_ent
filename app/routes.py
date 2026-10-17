from flask import (
    Blueprint, render_template, request, redirect,
    url_for, Response, jsonify, flash
)
from datetime import datetime
import threading
import shutil
import os
from engine.alive import ensure_proxy_alive
from engine.orchestrator import start_run, get_run_state
from engine.tasks import (
    ALLOWED_RESOLVE,
    ALLOWED_TRANSPORTS,
    FORM_NETWORKS,
    RunOptions,
    make_channel,
    build_all_country_tasks,
    build_multi_geo_tasks,
    build_single_country_tasks,
)
from config.loader import ConfigStore
from providers.registry import available_providers, get_provider
from providers.soax import CatalogStore, refresh_catalog_data
from logging_.engine_logger import get_engine_logger
from .utils import render_markdown_file

log = get_engine_logger()

bp = Blueprint("routes", __name__)


def _channel_from_form(form):
    """
    Reads the channel the run should use, and raises ValueError with a message
    for the submitter if it cannot.

    The transport falls back to the configured default and, if there is none,
    the run is refused rather than guessed (решение 11). The provider is
    resolved here so that an unknown or not-yet-implemented one fails on the
    request instead of inside a worker thread.
    """
    configured_default = ConfigStore.get().soax.transport
    channel = make_channel(
        provider=form.get("provider"),
        transport=form.get("transport") or configured_default,
        resolve=form.get("resolve"),
    )
    get_provider(channel.provider)
    return channel


@bp.get("/")
def index():
    cfg = ConfigStore.get()

    countries = CatalogStore.get_countries()

    return render_template(
        "index.html",
        active_page="checker",
        countries=countries,
        defaults=dict(
            soax_host=cfg.soax.host,
            soax_port_default=cfg.soax.port,
            timeout_sec=cfg.execution.timeout_sec,
            screenshots_enabled=cfg.screenshots.enabled_default,
            transport=cfg.soax.transport,
            resolve="remote",
            transports=ALLOWED_TRANSPORTS,
            resolve_modes=ALLOWED_RESOLVE,
            providers=available_providers(),
        ),
        logs_dir=cfg.paths.logs_dir,
    )


@bp.post("/run")
def launch_run():
    urls_raw = (request.form.get("urls") or "").strip()
    if not urls_raw:
        log.warning("Run rejected: No URLs provided.")
        return Response("No URLs provided", status=400)

    country_code = (request.form.get("country") or "").strip().lower()
    if not country_code:
        log.warning("Run rejected: Country is required.")
        return Response("Country is required", status=400)

    # Validated here rather than deeper down: the gateway answers 200 to a
    # request carrying no targeting rules and routes it through an arbitrary
    # country, which would yield a plausible but meaningless result.
    network = (request.form.get("network") or "").strip().lower()
    if network not in FORM_NETWORKS:
        log.warning(f"Run rejected: invalid network {network!r}.")
        return Response(
            "Network type is required (residential / mobile / both)", status=400
        )

    try:
        channel = _channel_from_form(request.form)
    except ValueError as e:
        log.warning("Run rejected: %s", e)
        return Response(str(e), status=400)

    if country_code == "all":
        # ALL is the Multi-Geo mechanism with the catalog as the country list,
        # so it gets the same label and the same log layout.
        countries = [c["code"] for c in CatalogStore.get_countries()]
        tasks = build_all_country_tasks(urls_raw, countries)
        label = "multi-geo"
    else:
        tasks = build_single_country_tasks(urls_raw, country_code)
        label = "std-check"

    if not tasks:
        log.warning("Run rejected: No valid URLs after normalization.")
        return Response("No valid URLs provided", status=400)

    options = RunOptions(
        label=label,
        channel=channel,
        network=network,
        timeout_sec=int(request.form.get("timeout_sec") or 60),
        make_screenshot=bool(request.form.get("make_screenshot")),
        debug_mode=bool(request.form.get("debug_mode")),
    )

    blocking = ConfigStore.blocking_errors()
    if blocking:
        message = "Configuration is incomplete: " + " ".join(blocking)
        log.error("%s rejected: %s", "Run", message)
        return jsonify({"error": message}), 503

    alive = ensure_proxy_alive(tasks, options)
    if not alive.ok:
        log.error("Run rejected: %s", alive.reason)
        return jsonify({"error": alive.reason}), 503

    log.info(
        "Accepted %s run request: %s tasks, country=%s, channel=%s.",
        label, len(tasks), country_code, channel.slug,
    )
    run_id = start_run(tasks, options)
    return jsonify({"run_id": run_id}), 202


@bp.get("/catalog")
def catalog():
    # используем get_countries() для загрузки всего кеша
    return render_template(
        "catalog.html",
        active_page="catalog",
        catalog=CatalogStore._load_or_cache(force_reload=True)
    )


@bp.post("/catalog/refresh")
def catalog_refresh():
    """
    Запускает обновление каталога в фоновом потоке.
    """
    log.info("Received request to refresh catalog. Starting background thread...")

    # запускаем тяжелую задачу в отдельном потоке
    thread = threading.Thread(
        target=refresh_catalog_data,
        daemon=True
    )
    thread.start()

    flash("Catalog refresh started in the background. It may take a minute.", "catalog")

    return redirect(url_for("routes.catalog"))


@bp.post("/catalog/update-list")
def catalog_update_list():
    """
    Handles adding new countries and removing existing ones from the catalog JSON.
    """
    try:
        new_countries_str = request.form.get("new_countries", "")
        existing_countries_keep = request.form.getlist("countries_to_keep")

        new_countries_list = [
            code.strip().lower()
            for code in new_countries_str.split()
            if code.strip()
        ]

        final_codes_list = []
        seen_codes = set()

        for code in existing_countries_keep:
            if code not in seen_codes:
                final_codes_list.append(code)
                seen_codes.add(code)

        for code in new_countries_list:
            if code not in seen_codes:
                final_codes_list.append(code)
                seen_codes.add(code)

        CatalogStore.update_country_list(final_codes_list)

        flash("Catalog country list updated successfully.", "catalog")

    except Exception as e:
        log.error(f"Failed to update catalog list: {e}", exc_info=True)
        flash(f"Error updating catalog: {e}", "catalog")

    return redirect(url_for("routes.catalog"))


@bp.get("/settings")
def settings():
    # From disk, not from this worker's memory: the page edits a file, and with
    # four workers the in-memory copy can be older than the file (решение 18).
    return render_template(
        "settings.html",
        active_page="settings",
        yaml=ConfigStore.file_text()
    )


@bp.post("/settings")
def settings_save():
    yaml_text = request.form.get("yaml") or ""
    ok, err, warnings = ConfigStore.save_yaml(yaml_text)
    if ok:
        # Saying it here rather than only in the docs: nobody reads the manual
        # at the moment of pressing Save, and nothing takes effect until the
        # container restarts.
        flash("Saved. Restart the container for the changes to take effect.", "settings")
        for warning in warnings:
            flash(f"Will be corrected at startup: {warning}", "settings")
    else:
        flash(f"Settings NOT saved: {err}", "settings")
    return redirect(url_for("routes.settings"))


@bp.get("/api/geo/regions")
def api_get_regions():
    country = request.args.get("country")
    if not country:
        return jsonify([])
    return jsonify(CatalogStore.get_regions(country))


@bp.get("/api/geo/cities")
def api_get_cities():
    country = request.args.get("country")
    region = request.args.get("region")
    if not country:
        return jsonify([])
    return jsonify(CatalogStore.get_cities(country, region))


@bp.get("/api/geo/isps")
def api_get_isps():
    country = request.args.get("country")
    if not country:
        return jsonify([])
    return jsonify(CatalogStore.get_isps(country))


@bp.post("/logs/clear")
def clear_logs():
    cfg = ConfigStore.get()
    logs_dir = os.path.abspath(cfg.paths.logs_dir)

    log.warning(f"Attempting to clear contents of log directory: {logs_dir}")

    # Проверка, что /logs существует и это действительно dir
    if not os.path.isdir(logs_dir):
        log.error(f"Log directory not found or is not a directory: {logs_dir}")
        flash("Error: Log directory not found.", "checker")
        return jsonify({"success": False, "message": "Log directory not found"}), 500

    if 'logs' not in logs_dir.split(os.path.sep)[-2:]:  # Проверянм последние два компонента пути
        log.error(f"Safety check failed: Log directory path seems unsafe: {logs_dir}")
        flash("Error: Log directory path seems unsafe.", "checker")
        return jsonify({"success": False, "message": "Unsafe log directory path"}), 500

    try:
        # грохаем всё внутри /logs
        for filename in os.listdir(logs_dir):
            file_path = os.path.join(logs_dir, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                log.error(f"Failed to delete {file_path}. Reason: {e}")

        log.info(f"Successfully cleared contents of log directory: {logs_dir}")
        flash("Logs cleared successfully.", "checker")
        return jsonify({"success": True}), 200

    except Exception as e:
        log.error(f"Failed to clear log directory {logs_dir}: {e}", exc_info=True)
        flash(f"Error clearing logs: {e}", "checker")
        return jsonify({"success": False, "message": str(e)}), 500


@bp.get("/help")
def help_page():
    help_html = render_markdown_file("help.md")
    return render_template(
        "help.html",
        active_page="help",
        help_content=help_html
    )


@bp.get("/multi-geo")
def multi_geo_page():
    cfg = ConfigStore.get()
    return render_template(
        "multi_geo.html",
        active_page="multi_geo",
        countries=[], # Здесь список для гео-выпадайки не нужен
        defaults=dict(
            soax_host=cfg.soax.host,
            soax_port_default=cfg.soax.port,
            timeout_sec=cfg.execution.timeout_sec,
            screenshots_enabled=cfg.screenshots.enabled_default,
            transport=cfg.soax.transport,
            resolve="remote",
            transports=ALLOWED_TRANSPORTS,
            resolve_modes=ALLOWED_RESOLVE,
            providers=available_providers(),
        ),
        logs_dir=cfg.paths.logs_dir,
    )


@bp.post("/run-multi-geo")
def launch_multi_geo_run():
    urls_raw = (request.form.get("urls") or "").strip()
    if not urls_raw:
        log.warning("Multi-Geo Run rejected: No input provided.")
        return Response("No URLs and countries provided", status=400)

    network = (request.form.get("network") or "").strip().lower()
    if network not in FORM_NETWORKS:
        log.warning(f"Multi-Geo Run rejected: invalid network {network!r}.")
        return Response(
            "Network type is required (residential / mobile / both)", status=400
        )

    try:
        channel = _channel_from_form(request.form)
    except ValueError as e:
        log.warning("Multi-Geo Run rejected: %s", e)
        return Response(str(e), status=400)

    tasks = build_multi_geo_tasks(urls_raw)
    if not tasks:
        return Response("No valid tasks found", status=400)

    options = RunOptions(
        label="multi-geo",
        channel=channel,
        network=network,
        timeout_sec=int(request.form.get("timeout_sec") or 60),
        make_screenshot=bool(request.form.get("make_screenshot")),
        debug_mode=bool(request.form.get("debug_mode")),
    )

    blocking = ConfigStore.blocking_errors()
    if blocking:
        message = "Configuration is incomplete: " + " ".join(blocking)
        log.error("%s rejected: %s", "Multi-Geo Run", message)
        return jsonify({"error": message}), 503

    alive = ensure_proxy_alive(tasks, options)
    if not alive.ok:
        log.error("Multi-Geo Run rejected: %s", alive.reason)
        return jsonify({"error": alive.reason}), 503

    log.info("Accepted /run-multi-geo request: %s tasks, channel=%s.",
             len(tasks), channel.slug)
    run_id = start_run(tasks, options)
    return jsonify({"run_id": run_id}), 202
