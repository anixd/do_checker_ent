from __future__ import annotations
import os, yaml
from dataclasses import dataclass, fields, field
from typing import Any, Dict, List


def _as_bool(val: Any) -> bool:
    """
    Env vars always arrive as strings, and bool("false") is True in Python.
    Without this, SOAX_BIND_NODE=false would silently enable bind-node.
    """
    return str(val).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class AppCfg:
    host: str
    port: int
    default_theme: str


@dataclass
class LoggingCfg:
    level: str


@dataclass
class PathsCfg:
    logs_dir: str
    data_dir: str


@dataclass
class ExecCfg:
    max_concurrency: int
    timeout_sec: int


@dataclass
class ShotsCfg:
    enabled_default: bool
    max_workers: int
    width: int
    height: int
    timeout_sec: int
    wait_after_load_sec: int


@dataclass
class SoaxCfg:
    """
    Connection settings for the SOAX gateway (proxy.soax.com:1337).

    The gateway takes targeting rules inside the proxy username and the package
    password as the proxy password, e.g.:
        country-az-network-res_mob-onerror-retry_3-session-dc1a2b3c04 : <password>

    Note: package_id (the UUID shown on the dashboard Settings tab) is NOT part
    of the connection string. It is kept here only for a future catalog API.

    `transport` is the default the forms preselect, not a restriction: a run
    carries its own transport. Empty or unrecognised means there is no default
    and the user must choose - deliberately a warning rather than a startup
    failure, because which transport suits the work at hand is the operator's
    call, not a reason to refuse to run.
    """
    host: str
    port: int
    transport: str | None     # http | socks5 | None - None means "no default, ask the user"
    proxy_type: str | None    # deprecated synonym of transport, kept for old app.yaml
    session_scope: str        # task | geo
    on_error: str             # replace | retry | fail  -> gateway 'onerror-*' rule
    on_error_retries: int     # 1..10, used when on_error == "retry"
    bind_node: bool           # gateway 'bind-node' flag; incompatible with 'replace'
    preflight: bool           # resolve ext_ip and actual geo before the real check
    package_id: str | None    # dashboard UUID; unused for now, reserved for the API
    package_password: str | None  # SECRET: .env only, never in app.yaml


@dataclass
class HttpCfg:
    user_agent: str
    accept: str
    accept_language: str
    custom_headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class RootCfg:
    app: AppCfg
    logging: LoggingCfg
    paths: PathsCfg
    execution: ExecCfg
    screenshots: ShotsCfg
    soax: SoaxCfg
    http_client: HttpCfg


# Applied key by key via setdefault, so a partially filled section in app.yaml
# does not blow up dataclass construction with "missing required argument".
_SECTION_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "app": {
        "host": "127.0.0.1",
        "port": 8888,
        "default_theme": "auto",
    },
    "logging": {
        "level": "INFO",
    },
    "paths": {
        "logs_dir": "/logs",
        "data_dir": "/data",
    },
    "execution": {
        "max_concurrency": 3,
        "timeout_sec": 60,
    },
    "screenshots": {
        "enabled_default": False,
        "max_workers": 1,
        "width": 1366,
        "height": 768,
        "timeout_sec": 30,
        "wait_after_load_sec": 15,
    },
    "soax": {
        "host": "proxy.soax.com",
        "port": 1337,
        "transport": None,
        "proxy_type": None,
        "session_scope": "task",
        "on_error": "retry",
        "on_error_retries": 3,
        "bind_node": False,
        "preflight": True,
        "package_id": None,
        "package_password": None,
    },
    "http_client": {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
        ),
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept_language": "en-US,en;q=0.5",
        "custom_headers": {},
    },
}


ALLOWED_TRANSPORTS = ("http", "socks5")


class ConfigStore:
    _cfg: RootCfg = None
    _yaml_text: str = ""
    # Collected during init(), which runs before the loggers exist, and emitted
    # by logging_/engine_logger.py::log_config_summary once they do.
    _warnings: List[str] = []

    @classmethod
    def init(cls):
        data_dir = os.environ.get("DATA_DIR", "/data")
        app_yaml = os.path.join(data_dir, "config", "app.yaml")

        try:
            with open(app_yaml, "r", encoding="utf-8") as f:
                text = f.read()
                cls._yaml_text = text
                data = yaml.safe_load(text)
        except FileNotFoundError:
            print(f"FATAL: Config file not found at {app_yaml}")
            print(f"       (Make sure ./data/config/app.yaml exists locally)")
            raise

        cls._warnings = []
        cfg = cls._build(data)
        cls._override_from_env(cfg)
        cls._warnings.extend(cls._normalize_transport(cfg))
        cls._validate(cfg)

        cls._cfg = cfg

        os.makedirs(cls._cfg.paths.data_dir, exist_ok=True)
        os.makedirs(cls._cfg.paths.logs_dir, exist_ok=True)

    @classmethod
    def _build(cls, data: Dict[str, Any] | None) -> RootCfg:
        """
        Turns raw YAML into a RootCfg, filling in per-key defaults first.
        Unknown sections in app.yaml (e.g. a leftover 'proxy:') are ignored.
        """
        data = data or {}

        for section, defaults in _SECTION_DEFAULTS.items():
            if not isinstance(data.get(section), dict):
                data[section] = {}
            for key, val in defaults.items():
                data[section].setdefault(key, val)

        # Drop keys the dataclasses do not know about, so an old app.yaml
        # left over from the port-mode version does not crash the startup.
        def _only_known(cls_, section: str) -> Dict[str, Any]:
            known = {f.name for f in fields(cls_)}
            return {k: v for k, v in data[section].items() if k in known}

        return RootCfg(
            app=AppCfg(**_only_known(AppCfg, "app")),
            logging=LoggingCfg(**_only_known(LoggingCfg, "logging")),
            paths=PathsCfg(**_only_known(PathsCfg, "paths")),
            execution=ExecCfg(**_only_known(ExecCfg, "execution")),
            screenshots=ShotsCfg(**_only_known(ShotsCfg, "screenshots")),
            soax=SoaxCfg(**_only_known(SoaxCfg, "soax")),
            http_client=HttpCfg(**_only_known(HttpCfg, "http_client")),
        )

    @classmethod
    def _override_from_env(cls, cfg: RootCfg):
        env_map = {
            "APP_HOST": (cfg.app, "host"),
            "APP_PORT": (cfg.app, "port", int),
            "DEFAULT_THEME": (cfg.app, "default_theme"),
            "LOG_LEVEL": (cfg.logging, "level"),
            "LOG_DIR": (cfg.paths, "logs_dir"),
            "DATA_DIR": (cfg.paths, "data_dir"),
            "MAX_CONCURRENCY": (cfg.execution, "max_concurrency", int),
            "CHECK_TIMEOUT_SEC": (cfg.execution, "timeout_sec", int),
            "MAX_SCREENSHOT_WORKERS": (cfg.screenshots, "max_workers", int),
            "SCREENSHOT_TIMEOUT_SEC": (cfg.screenshots, "timeout_sec", int),
            "SCREENSHOT_WAIT_AFTER_LOAD_SEC": (cfg.screenshots, "wait_after_load_sec", int),

            "SOAX_HOST": (cfg.soax, "host"),
            "SOAX_PORT": (cfg.soax, "port", int),
            "SOAX_TRANSPORT": (cfg.soax, "transport"),
            "SOAX_PROXY_TYPE": (cfg.soax, "proxy_type"),
            "SOAX_SESSION_SCOPE": (cfg.soax, "session_scope"),
            "SOAX_ON_ERROR": (cfg.soax, "on_error"),
            "SOAX_ON_ERROR_RETRIES": (cfg.soax, "on_error_retries", int),
            "SOAX_BIND_NODE": (cfg.soax, "bind_node", _as_bool),
            "SOAX_PREFLIGHT": (cfg.soax, "preflight", _as_bool),
            "SOAX_PACKAGE_ID": (cfg.soax, "package_id"),
            "SOAX_PACKAGE_PASSWORD": (cfg.soax, "package_password"),

            "USER_AGENT": (cfg.http_client, "user_agent"),
        }

        for env_key, info in env_map.items():
            val = os.environ.get(env_key)
            if val is not None:
                obj, attr_name = info[0], info[1]
                cast_func = info[2] if len(info) > 2 else str

                try:
                    setattr(obj, attr_name, cast_func(val))
                except (ValueError, TypeError):
                    print(f"Warning: Could not cast env var {env_key}={val} to {cast_func}")

    @classmethod
    def _normalize_transport(cls, cfg: RootCfg) -> List[str]:
        """
        Resolves the default transport, and never raises.

        `soax.transport` is the field; `soax.proxy_type` is the name it had
        before SOCKS5 existed and is still honoured, so an installation whose
        app.yaml says `proxy_type: http` keeps getting http as its default
        instead of silently losing it on upgrade.

        Anything unrecognised - including an empty string - leaves no default:
        cfg.soax.transport becomes None, the forms preselect nothing, and a run
        has to name its transport. A warning is returned rather than raised,
        per the decision that this field must not stop the app from starting.
        """
        warnings: List[str] = []
        s = cfg.soax

        raw = s.transport
        source = "soax.transport"
        if not (raw or "").strip() and (s.proxy_type or "").strip():
            raw = s.proxy_type
            source = "soax.proxy_type (deprecated, rename it to soax.transport)"

        value = (raw or "").strip().lower()
        if not value:
            s.transport = None
            warnings.append(
                "No default transport configured (soax.transport is empty): the "
                "transport has to be chosen explicitly for every run."
            )
        elif value in ALLOWED_TRANSPORTS:
            s.transport = value
            if source.startswith("soax.proxy_type"):
                warnings.append(f"Default transport taken from {source}.")
        else:
            s.transport = None
            warnings.append(
                f"Ignoring {source}={raw!r}: expected one of "
                f"{'|'.join(ALLOWED_TRANSPORTS)}. Running without a default "
                "transport, so it has to be chosen explicitly for every run."
            )
        return warnings

    @classmethod
    def warnings(cls) -> List[str]:
        return list(cls._warnings)

    @classmethod
    def _validate(cls, cfg: RootCfg):
        """
        Fail fast on a broken config instead of failing on every single check later.
        The combination rules mirror the gateway's own validation, which answers
        400 ERR_INVALID_COMBINATION for the same cases.
        """
        s = cfg.soax
        errors: List[str] = []

        if s.on_error not in ("replace", "retry", "fail"):
            errors.append(
                f"soax.on_error must be one of replace|retry|fail, got '{s.on_error}'"
            )

        if s.on_error == "retry" and not (1 <= s.on_error_retries <= 10):
            errors.append(
                f"soax.on_error_retries must be between 1 and 10, got {s.on_error_retries}"
            )

        if s.bind_node and s.on_error == "replace":
            errors.append(
                "soax.bind_node cannot be combined with on_error='replace' "
                "(the gateway rejects it with 400 ERR_INVALID_COMBINATION). "
                "Use on_error 'retry' or 'fail', or turn bind_node off."
            )

        if s.session_scope not in ("task", "geo"):
            errors.append(
                f"soax.session_scope must be 'task' or 'geo', got '{s.session_scope}'"
            )

        if not s.package_password:
            errors.append(
                "SOAX_PACKAGE_PASSWORD is not set. Put the package password from the "
                "SOAX dashboard (Settings tab) into .env. Do not put it in app.yaml: "
                "that file is rendered in the Settings page of this app."
            )

        if cfg.execution.max_concurrency < 1:
            errors.append(
                f"execution.max_concurrency must be >= 1, got {cfg.execution.max_concurrency}"
            )

        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))

    @classmethod
    def get(cls) -> RootCfg:
        if cls._cfg is None:
            cls.init()
        return cls._cfg

    @classmethod
    def raw_yaml(cls) -> str:
        return cls._yaml_text

    @classmethod
    def save_yaml(cls, text: str) -> tuple[bool, str | None]:
        """
        Validates before writing. The previous version wrote first and reloaded
        after, so a bad edit left a broken file on disk and a stale config in
        memory: the app kept running and then refused to start next time.

        Returns (ok, error_message).
        """
        data_dir = cls.get().paths.data_dir
        app_yaml = os.path.join(data_dir, "config", "app.yaml")

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            return False, f"YAML syntax error: {e}"

        try:
            candidate = cls._build(data)
            cls._override_from_env(candidate)
            new_warnings = cls._normalize_transport(candidate)
            cls._validate(candidate)
        except (TypeError, ValueError) as e:
            return False, str(e)

        try:
            with open(app_yaml, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            return False, f"Could not write {app_yaml}: {e}"

        cls._yaml_text = text
        cls._cfg = candidate
        cls._warnings = new_warnings
        return True, None
