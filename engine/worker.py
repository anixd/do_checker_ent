from __future__ import annotations
import contextlib
import faulthandler
import os
import time
import urllib.parse
import requests
import threading
from datetime import datetime
from config.loader import ConfigStore
from engine.tasks import CheckTask, RunOptions
from logging_.md_writer import ensure_day_dir, unique_file_path, render_md_card
from logging_.engine_logger import get_engine_logger
from providers.base import Provider, ProviderGatewayError
from providers.registry import get_provider
from providers.soax_session import ProxySession, sanitize_credentials

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

log = get_engine_logger()

screenshot_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()  # lock для инициализации семафора

_watchdog_fh = None
_watchdog_lock = threading.Lock()


@contextlib.contextmanager
def _hang_watchdog(seconds: int):
    """
    Dumps every thread's stack to faulthandler.log if the block it wraps has not
    finished within `seconds`, then lets it carry on.

    This is the only diagnostic that survives the worst case. A screenshot is
    the one unbounded operation in a check, and when Playwright wedges inside a
    C call the interpreter stops running Python altogether: gunicorn's
    worker_abort hook never fires, and the arbiter kills the worker leaving
    nothing but "was sent SIGTERM!" behind. dump_traceback_later runs on a
    watchdog thread in C, so it reports even then.

    The timer is process-global, so only one may be armed at a time. Screenshots
    are serialised by screenshot_semaphore (screenshots.max_workers, 1 by
    default); with that raised above 1 the watchdog covers whichever screenshot
    armed it last, which is why it is only a diagnostic and never load-bearing.
    """
    global _watchdog_fh

    with _watchdog_lock:
        if _watchdog_fh is None:
            path = os.path.join(ConfigStore.get().paths.logs_dir, "faulthandler.log")
            try:
                _watchdog_fh = open(path, "a", encoding="utf-8")
            except OSError as e:
                log.warning("Hang watchdog disabled, cannot open %s: %s", path, e)

    if _watchdog_fh is None:
        yield
        return

    log.debug("Arming hang watchdog for %ss", seconds)
    faulthandler.dump_traceback_later(seconds, repeat=False, file=_watchdog_fh, exit=False)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


def _normalize_url(raw: str) -> tuple[str, str]:
    """
    Ensures a URL has a scheme and extracts a clean netloc (domain)
    for use in filenames.

    Returns:
        tuple[str, str]: (domain, full_url_for_request)
    """
    url_full = raw
    if "://" not in raw:
        url_full = f"http://{raw}"

    # Now that we guaranteed a scheme, parse it
    parsed = urllib.parse.urlsplit(url_full)
    domain = parsed.netloc

    return domain, url_full


def _measure_http(url: str, proxies: dict, timeout_sec: int, provider: Provider,
                  max_redirects: int = 5):
    timings = {"dns_ms": None, "tcp_ms": None, "tls_ms": None, "ttfb_ms": None, "total_ms": None}
    redirects = []
    http_status = None
    bytes_count = None
    cfg = ConfigStore.get()
    headers = {
        "User-Agent": cfg.http_client.user_agent,
        "Accept": cfg.http_client.accept,
        "Accept-Language": cfg.http_client.accept_language,
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
    }

    headers.update(cfg.http_client.custom_headers)

    sess = requests.Session()
    sess.max_redirects = max_redirects
    sess.headers.update(headers)
    start = time.time()
    try:
        resp = sess.get(url, proxies=proxies, timeout=timeout_sec, stream=True, allow_redirects=True)

        # The first hop is plain HTTP (see _normalize_url), so a provider's
        # rejection arrives as a normal response with its headers intact and
        # only the provider can recognise it.
        provider.check_response(resp)

        http_status = resp.status_code
        first_chunk = next(resp.iter_content(chunk_size=1024), b"")
        ttfb = time.time() - start
        timings["ttfb_ms"] = int(ttfb * 1000)
        content = first_chunk + resp.content
        bytes_count = len(content)

        # берем историю редиректов из 'resp.history'.
        # каждый 'r' в 'history' - это Response-объект редиректа.
        redirects = []
        for r in resp.history:
            redirects.append((r.status_code, r.url, r.headers.get('Location', '')))

    except (requests.exceptions.RequestException, ProviderGatewayError) as e:
        end = time.time()
        timings["total_ms"] = int((end - start) * 1000)
        raise e
    end = time.time()
    timings["total_ms"] = int((end - start) * 1000)
    return http_status, bytes_count, redirects, timings, headers


def _take_screenshot(
        url: str, out_path: str, screenshot_timeout_sec: int, width: int, height: int,
        playwright_proxy: dict
) -> tuple[bool, str | None]:
    if not sync_playwright:
        return False, "playwright not installed or failed to import"

    cfg = ConfigStore.get()
    wait_after_load_sec = cfg.screenshots.wait_after_load_sec

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            # добавляем custom_headers в new_context playwright
            ctx = browser.new_context(
                viewport={"width": width, "height": height},
                user_agent=cfg.http_client.user_agent,
                proxy=playwright_proxy,
                extra_http_headers=cfg.http_client.custom_headers
            )
            page = ctx.new_page()
            # Use screenshot timeout (convert to ms)
            page.set_default_navigation_timeout(screenshot_timeout_sec * 1000)

            # 1. ждем спиннера
            page.goto(url, wait_until="load")

            # 2. если в конфиге > 0, ждем принудительно
            if wait_after_load_sec > 0:
                log.debug(
                    f"Waiting for {wait_after_load_sec}s (wait_after_load_sec) "
                    f"for SPA content on {url}"
                )
                page.wait_for_timeout(wait_after_load_sec * 1000)

            page.screenshot(path=out_path)  # делаем скрин _видимой_ части страницы.
            # full_page=True делает скрин ВСЕЙ высоты страницы (если нужно)
            # TODO: вынести этот параметр в конфиг
            browser.close()
        return True, None
    except Exception as e:
        log.error("Screenshot failed for %s: %s", url, e, exc_info=True)
        return False, str(e)


def execute_check(task: CheckTask, options: RunOptions) -> dict:
    """
    Runs one check and writes its Markdown card.

    Takes a CheckTask and the run's options instead of the old flat dict, so the
    two orchestrator paths can share it, and reaches the proxy service only
    through the provider seam (providers/base.py) - nothing here names SOAX.
    """
    global screenshot_semaphore
    cfg = ConfigStore.get()

    # initialize semaphore if needed
    if screenshot_semaphore is None:
        with _semaphore_lock:  # prevent race condition on first init
            if screenshot_semaphore is None:
                max_workers = cfg.screenshots.max_workers
                log.info(f"Initializing screenshot semaphore with max_workers={max_workers}")
                screenshot_semaphore = threading.Semaphore(max_workers)
    # end semaphore init

    logs_dir = cfg.paths.logs_dir
    day_dir = ensure_day_dir(logs_dir)

    target_dir = day_dir
    if options.subfolder:
        target_dir = os.path.join(day_dir, options.subfolder)
        os.makedirs(target_dir, exist_ok=True)

    # run_id and url are already in the logging context, bound by the pool
    # task in engine/orchestrator.py - see logging_/context.py.
    url = task.url
    geo = task.country
    log.debug("execute_check started.")

    provider = get_provider(options.channel.provider)
    timeout_sec = options.timeout_sec
    make_screenshot = options.make_screenshot
    debug_mode = options.debug_mode

    domain, url_full = _normalize_url(url)
    ts = datetime.now().strftime("%H-%M-%S")

    if geo:
        # Режим Multi-Geo: 01-17-56_domain.com_az
        base_name = f"{ts}_{domain}_{geo.lower()}"
    else:
        # Стандартный режим (если гео не передано явно): 01-17-56_domain.com
        base_name = f"{ts}_{domain}"

    md_path = unique_file_path(target_dir, base_name, "md")
    png_path = None

    ps = None
    debug_data = None
    http_status = None
    bytes_count = None
    redirects = []
    timings = {}
    notes = None
    exc = None
    sent_headers = None

    try:
        log.debug("Building the provider session (%s)...", provider.name)
        ps = provider.build_session(task, options)
        debug_data = ps.debug_info

        proxies = provider.proxies_for(ps, options.channel)

        log.debug("Preflight for session %s...", ps.session_id)
        pf_warning = provider.preflight(ps)
        if pf_warning:
            notes = pf_warning
            log.warning("Preflight warning: %s", pf_warning)
        if ps.ext_ip or ps.geo_actual or pf_warning:
            debug_data["preflight"] = {
                "ext_ip": ps.ext_ip,
                "geo_actual": ps.geo_actual,
                "warning": pf_warning,
            }

        log.debug("Calling _measure_http...")
        http_status, bytes_count, redirects, timings, sent_headers = _measure_http(
            url_full, proxies, timeout_sec, provider
        )
    except ProviderGatewayError as e:
        exc = e
        notes = str(e)
        if debug_data is None:
            debug_data = {}
        debug_data["gateway_error"] = {"code": e.code, "detail": e.detail}
        log.error("Provider gateway rejected the request: %s", e)
    except Exception as e:
        exc = e
        # requests embeds the full proxy URL - package password included -
        # into its exception messages, and notes end up in ./logs/*.md
        notes = sanitize_credentials(str(e))
        if debug_data is None:
            debug_data = {"error": sanitize_credentials(str(e))}
        # sanitize_credentials: requests embeds the full proxy URL, package
        # password included, in its exception messages.
        log.warning("_measure_http failed: %s", sanitize_credentials(str(e)))

    # Добавляем хедеры в debug_info (if debug_mode is on)
    if debug_mode and debug_data and sent_headers:
        debug_data["http_request_headers"] = sent_headers

    result = provider.classify(exc, http_status)
    log.debug("Result: %s", result)

    # логика скриншота, с семафором
    if make_screenshot and result == "success":
        png_path = unique_file_path(target_dir, base_name, "png")
        screenshot_timeout = timeout_sec + cfg.screenshots.wait_after_load_sec + 5

        log.info("Acquiring screenshot semaphore...")
        with screenshot_semaphore:
            log.info("Semaphore acquired. Taking screenshot...")
            # The provider decides how the browser reaches the proxy and keeps
            # whatever it had to start (a shim) alive only for this block.
            with provider.screenshot_proxy(ps, options.channel) as shot_proxy:
                # Playwright is given screenshot_timeout; anything past that plus
                # a margin means it is not going to come back on its own.
                with _hang_watchdog(screenshot_timeout + 10):
                    ok, s_err = _take_screenshot(
                        url_full, png_path, screenshot_timeout,
                        cfg.screenshots.width, cfg.screenshots.height,
                        shot_proxy.settings,
                    )
            log.info("Semaphore released.")

        if not ok:
            png_path = None  # don't link to failed screenshot !!!
            if notes:
                notes += f" | screenshot: {s_err}"
            else:
                notes = f"screenshot: {s_err}"
        elif shot_proxy.transport != options.channel.transport:
            # Only when the fallback kicked in: a picture taken over another
            # transport is not evidence about this one, so the card says so.
            note = (f"screenshot taken over {shot_proxy.transport} (same session), "
                    f"check ran over {options.channel.transport}")
            notes = f"{notes} | {note}" if notes else note

    geo_str = (task.country or "-").upper()
    if ps and ps.geo_actual:
        g = ps.geo_actual
        actual = " / ".join(x for x in (
            (g.get("country_code") or "").upper(),
            g.get("region"),
            g.get("city"),
            g.get("isp"),
        ) if x)
        asn = f" AS{g['asn']}" if g.get("asn") else ""
        geo_str = f"{geo_str} (requested) — exit node: {actual}{asn}"

    if ps:
        proxy_str = f"SOAX session {ps.session_id or '(none)'}, ext_ip: {ps.ext_ip or '-'}"
    else:
        proxy_str = "SOAX session not established"

    md_text = render_md_card(
        domain=domain,
        started=datetime.now().isoformat(timespec="seconds"),
        geo_str=geo_str,
        proxy_str=proxy_str,
        network=options.network or "-",
        transport=options.channel.transport,
        dns_resolve=options.channel.resolve,
        provider=options.channel.provider,
        timeout_sec=timeout_sec,
        url_show=url_full,
        redirects=redirects,
        timings=timings,
        http_status=http_status,
        bytes_count=bytes_count,
        result=result,
        screenshot_name=os.path.basename(png_path) if png_path else None,
        notes=notes,
        debug_info=debug_data if debug_mode else None
    )

    log.debug("Writing .md card to %s", md_path)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    return {
        "classification": result,
        "http_code": http_status,
        "bytes_count": bytes_count,
        "timings": timings,
        "redirects": redirects,
        "proxy_ext_ip": ps.ext_ip if ps else None,
        "md_path": md_path,
        "png_path": png_path,
        "notes": notes
    }
