import fcntl
import tempfile
import threading
import time
import os
import requests
from logging_.engine_logger import get_engine_logger
from packaging import version

log = get_engine_logger()

REMOTE_VERSION_URL = "https://raw.githubusercontent.com/anixd/do_checker_ent/master/VERSION"

LOCAL_VERSION_FILE = os.path.abspath("./VERSION")

CHECK_INTERVAL_SEC = 3 * 60 * 60
INITIAL_DELAY_SEC = 20

# Глобальное состояние
UPDATE_AVAILABLE = False
_lock = threading.Lock()

# Keeps the flock alive for the lifetime of the process: closing the file or
# letting it be garbage collected releases the lock.
_singleton_handle = None


def get_update_status() -> bool:
    """
    Безопасно получает текущий статус обновления.
    """
    with _lock:
        return UPDATE_AVAILABLE


def _set_update_status(status: bool):
    """
    Безопасно устанавливает статус обновления.
    """
    global UPDATE_AVAILABLE
    with _lock:
        UPDATE_AVAILABLE = status


def check_for_updates_thread():
    """
    Функция для запуска в фоновом потоке (daemon thread).
    Проверяет обновления при старте и каждые 3 часа.
    """
    log.info("Update checker thread started. Initial check in %s seconds...", INITIAL_DELAY_SEC)
    time.sleep(INITIAL_DELAY_SEC)  # Даем приложению запуститься

    while True:
        try:
            log.info("Checking for new version...")

            # 1. Получаем удаленный текст версии
            try:
                response = requests.get(REMOTE_VERSION_URL, timeout=15)
                response.raise_for_status()
                remote_version_str = response.text.strip()
            except requests.exceptions.RequestException as e:
                log.warning(f"Failed to fetch remote VERSION file: {e}")
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            # 2. Получаем локальный текст версии
            try:
                with open(LOCAL_VERSION_FILE, "r") as f:
                    local_version_str = f.read().strip()
            except FileNotFoundError:
                log.error(f"FATAL: Local VERSION file not found at {LOCAL_VERSION_FILE}. Update check disabled.")
                _set_update_status(False)
                return
            except Exception as e:
                log.error(f"Failed to read local VERSION file: {e}")
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            # 3. Сравниваем версии (SemVer)
            try:
                v_remote = version.parse(remote_version_str)
                v_local = version.parse(local_version_str)

                if v_remote > v_local:
                    log.info(f"Update available! Local={local_version_str}, Remote={remote_version_str}")
                    _set_update_status(True)
                else:
                    log.info(f"App is up to date (Local={local_version_str}, Remote={remote_version_str})")
                    _set_update_status(False)
            except Exception as e:
                log.error(f"Failed to parse version strings: {e}")
                # Fallback к обычному сравнению, если формат строк не соответствует SemVer
                _set_update_status(local_version_str != remote_version_str and remote_version_str != "")

            # 4. спим 3 часа
            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            log.error(f"Unhandled error in update checker thread: {e}", exc_info=True)
            log.info("Retrying update check in %s seconds...", CHECK_INTERVAL_SEC)
            time.sleep(CHECK_INTERVAL_SEC)


def start_background_checker() -> bool:
    """
    Starts the checker in exactly one gunicorn worker.

    create_app() runs once per worker, so the thread used to be started four
    times over: four requests to GitHub every three hours and every log line
    about versions printed four times. An flock on a file in the log directory
    is the cheapest cross-process arbiter available here - no shared state, and
    the lock is released by the kernel if the worker dies, so a restarted
    worker can pick the job up.
    """
    global _singleton_handle

    # Deliberately not in the log directory: that is a bind mount, and Docker
    # Desktop does not honour advisory locks on it - with four workers two of
    # them took the "exclusive" lock at once. The container's own /tmp is a
    # normal filesystem, where flock behaves.
    lock_path = os.path.join(tempfile.gettempdir(), "do_checker_ent.version_checker.lock")
    try:
        handle = open(lock_path, "w", encoding="utf-8")
    except OSError as e:
        log.warning("Could not open %s (%s); update checker not started.", lock_path, e)
        return False

    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        log.debug("Update checker already running in another worker; skipping.")
        return False

    handle.write(f"{os.getpid()}\n")
    handle.flush()
    _singleton_handle = handle

    thread = threading.Thread(target=check_for_updates_thread, daemon=True)
    thread.start()
    return True
