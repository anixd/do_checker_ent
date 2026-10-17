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


def _as_list(val: Any) -> List[str]:
    """Comma-separated in the environment, a YAML list in the file."""
    if isinstance(val, (list, tuple)):
        return [str(item).strip() for item in val if str(item).strip()]
    return [part.strip() for part in str(val).split(",") if part.strip()]


@dataclass
class AliveCheckCfg:
    """
    The short "is the proxy service reachable at all" probe run before a check.

    Without it a provider outage reads as forty unavailable mirrors - a false
    report that also costs the traffic of producing it. Three different targets
    rather than three requests to one: "two of three failed" then means two
    independent destinations are unreachable, which is a property of the proxy
    and not of a site having a bad day.
    """
    enabled: bool
    targets: List[str]
    required_ok: int
    timeout_sec: int


@dataclass
class DnsCfg:
    """
    Whether this process is allowed to use IPv6 at all.

    Off by default: SOAX does not route IPv6 (verified), so an outbound attempt
    over IPv6 is a guaranteed failure that reads as "the mirror is down".
    See engine/dns_policy.py for what being off actually does.
    """
    use_ipv6: bool


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
    alive_check: AliveCheckCfg
    dns: DnsCfg
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
    "alive_check": {
        "enabled": True,
        # Three destinations run by three unrelated operators (IANA, Mozilla,
        # Cloudflare), each answering a few hundred bytes at most. Measured
        # through the gateway from az on 2026-10-17: 350-580 ms each.
        # google.com is deliberately absent - it answers proxy addresses with
        # captchas and 429, which would read as "the proxy is down" on a
        # perfectly healthy gateway. www.ripe.net was in the first draft and is
        # out for cost: it answers 138 KB per probe, which is paid proxy
        # traffic spent on asking a question, and its latency ranged from 1.3 s
        # to 9.6 s - against a 10 s timeout that is a false "dead" waiting to
        # happen.
        "targets": [
            "http://example.com",
            "http://detectportal.firefox.com/success.txt",
            "http://cloudflare.com/cdn-cgi/trace",
        ],
        "required_ok": 2,
        "timeout_sec": 10,
    },
    "dns": {
        "use_ipv6": False,
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

KNOWN_SECTIONS = tuple(_SECTION_DEFAULTS)


class _FirstWinsLoader(yaml.SafeLoader):
    """
    A SafeLoader that reports duplicate keys and keeps the first of them.

    PyYAML treats a duplicate key as perfectly fine and silently keeps the
    *last* one; a duplicated section is worse still, because the whole first
    block disappears and its keys fall back to defaults with nothing said. That
    is how an installation ends up running settings nobody wrote.

    First wins, because the accidental line is almost always the one added
    below - pasted in by hand, copied from another config - so keeping the
    first preserves what the author meant, while PyYAML's own rule applies
    precisely the line that was the mistake (решение 16).

    Duplicates are collected rather than raised: the whole point is that a
    config problem must not stop the application from starting.
    """

    def __init__(self, stream):
        super().__init__(stream)
        self.duplicate_warnings: List[str] = []

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        mapping = {}
        first_seen: Dict[Any, int] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            line = key_node.start_mark.line + 1
            if key in first_seen:
                self.duplicate_warnings.append(
                    f"{line}: duplicate key {key!r} (first set on line "
                    f"{first_seen[key]}); keeping the value from line {first_seen[key]}"
                )
                continue
            first_seen[key] = line
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


class ConfigStore:
    _cfg: RootCfg = None
    _yaml_text: str = ""
    # Collected during init(), which runs before the loggers exist, and emitted
    # by logging_/engine_logger.py::log_config_summary once they do.
    _warnings: List[str] = []
    # Problems that leave the app running but make a check pointless. Checked by
    # the run routes, which refuse with an explanation rather than producing one
    # failed check per mirror.
    _blocking: List[str] = []

    @classmethod
    def config_path(cls) -> str:
        data_dir = os.environ.get("DATA_DIR", "/data")
        return os.path.join(data_dir, "config", "app.yaml")

    @classmethod
    def file_text(cls) -> str:
        """
        The YAML as it is on disk right now.

        The Settings page edits a file, so it has to show that file - not the
        copy this worker parsed at startup. With four workers the in-memory copy
        is a trap: save through one of them, open the page again, land on
        another, see the old text and save it back over your own change
        (решение 18). Falls back to the parsed copy only if the file cannot be
        read at all.
        """
        try:
            with open(cls.config_path(), "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return cls._yaml_text

    @classmethod
    def _load_file(cls, path: str) -> tuple[str, Any, List[str]]:
        """
        Reads and parses the config file. Returns (text, data, warnings) and
        never raises: an unreadable file means defaults plus a warning, because
        a container that will not start is the worst possible answer to a typo
        (решение 16).
        """
        warnings: List[str] = []
        name = os.path.basename(path)

        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            return "", None, [
                f"Config file not found at {path}; running with built-in defaults. "
                "In Docker this file is created by docker/entrypoint.sh on first start."
            ]
        except OSError as e:
            return "", None, [f"Could not read {path} ({e}); running with built-in defaults."]

        loader = _FirstWinsLoader(text)
        try:
            data = loader.get_single_data()
            warnings.extend(f"{name}:{w}" for w in loader.duplicate_warnings)
        except yaml.YAMLError as e:
            where = ""
            mark = getattr(e, "problem_mark", None)
            if mark is not None:
                where = f" at line {mark.line + 1}, column {mark.column + 1}"
            problem = getattr(e, "problem", None) or str(e).splitlines()[0]
            return text, None, [
                f"{name} could not be parsed{where} ({problem}); "
                "falling back to built-in defaults for every setting."
            ]
        finally:
            loader.dispose()

        if data is not None and not isinstance(data, dict):
            return text, None, warnings + [
                f"{name} does not contain a mapping of sections; "
                "falling back to built-in defaults for every setting."
            ]

        return text, data, warnings

    @classmethod
    def init(cls):
        app_yaml = cls.config_path()

        text, data, warnings = cls._load_file(app_yaml)
        cls._yaml_text = text

        cls._warnings = list(warnings)
        cfg = cls._build(data)
        cls._override_from_env(cfg)
        cls._warnings.extend(cls._normalize_transport(cfg))
        cls._warnings.extend(cls._normalize_use_ipv6(cfg))
        cls._warnings.extend(cls._normalize_alive_check(cfg))
        cls._warnings.extend(cls._normalize_rules(cfg))
        cls._blocking = cls._blocking_errors(cfg)

        cls._cfg = cfg

        # Creating these must not be able to stop the start either: a config
        # pointing at a path we cannot write is a problem to report, not a
        # reason to refuse to boot (решение 16). The logging setup that follows
        # will complain in its own terms if the log directory is unusable.
        for label, path in (("paths.data_dir", cfg.paths.data_dir),
                            ("paths.logs_dir", cfg.paths.logs_dir)):
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as e:
                cls._warnings.append(f"Could not create {label}={path!r}: {e}")

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
            alive_check=AliveCheckCfg(**_only_known(AliveCheckCfg, "alive_check")),
            dns=DnsCfg(**_only_known(DnsCfg, "dns")),
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

            "ALIVE_CHECK_ENABLED": (cfg.alive_check, "enabled", _as_bool),
            "ALIVE_CHECK_TARGETS": (cfg.alive_check, "targets", _as_list),
            "ALIVE_CHECK_REQUIRED_OK": (cfg.alive_check, "required_ok", int),
            "ALIVE_CHECK_TIMEOUT_SEC": (cfg.alive_check, "timeout_sec", int),

            "DNS_USE_IPV6": (cfg.dns, "use_ipv6", _as_bool),

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
    def _normalize_use_ipv6(cls, cfg: RootCfg) -> List[str]:
        """
        Forces dns.use_ipv6 to a real bool, and never raises.

        YAML already turns true/false/yes/no into a bool, so anything left over
        is a typo. It must not become True by accident: a non-empty string is
        truthy in Python, so `use_ipv6: "maybe"` would silently switch IPv6 back
        on - the opposite of what whoever wrote it meant. Unrecognised values
        fall back to the safe side (off) with a warning, in the same spirit as
        soax.transport: a bad value here is not a reason to refuse to start.
        """
        warnings: List[str] = []
        raw = cfg.dns.use_ipv6

        if isinstance(raw, bool):
            return warnings

        value = str(raw).strip().lower()
        if value in ("1", "true", "yes", "on"):
            cfg.dns.use_ipv6 = True
        elif value in ("0", "false", "no", "off", ""):
            cfg.dns.use_ipv6 = False
        else:
            cfg.dns.use_ipv6 = False
            warnings.append(
                f"Ignoring dns.use_ipv6={raw!r}: expected a boolean (true|false). "
                "IPv6 stays disabled."
            )
        return warnings

    @classmethod
    def _normalize_alive_check(cls, cfg: RootCfg) -> List[str]:
        """
        Makes the alive-check section usable whatever is in the file, and never
        raises (решение 16).

        The fallbacks lean towards keeping the check running: it exists to stop
        a run against a dead provider, so a typo in `required_ok` must not
        quietly switch that protection off. An empty target list falls back to
        the defaults rather than disabling the check for the same reason.
        """
        warnings: List[str] = []
        ac = cfg.alive_check
        defaults = _SECTION_DEFAULTS["alive_check"]

        if not isinstance(ac.enabled, bool):
            raw = ac.enabled
            value = str(raw).strip().lower()
            if value in ("1", "true", "yes", "on"):
                ac.enabled = True
            elif value in ("0", "false", "no", "off"):
                ac.enabled = False
            else:
                ac.enabled = True
                warnings.append(
                    f"Ignoring alive_check.enabled={raw!r}: expected a boolean "
                    "(true|false). The check stays on."
                )

        targets = _as_list(ac.targets)
        if not targets:
            targets = list(defaults["targets"])
            warnings.append(
                "alive_check.targets is empty; falling back to the built-in list "
                f"({', '.join(targets)})."
            )
        ac.targets = targets

        try:
            required = int(ac.required_ok)
        except (TypeError, ValueError):
            required = defaults["required_ok"]
            warnings.append(
                f"Ignoring alive_check.required_ok={ac.required_ok!r}: expected a number. "
                f"Using {required}."
            )
        if required < 1:
            warnings.append(
                f"alive_check.required_ok={required} would make the check pass even when "
                "nothing answers. Raised to 1."
            )
            required = 1
        if required > len(ac.targets):
            warnings.append(
                f"alive_check.required_ok={required} exceeds the number of targets "
                f"({len(ac.targets)}), which no run could ever satisfy. Lowered to "
                f"{len(ac.targets)}."
            )
            required = len(ac.targets)
        ac.required_ok = required

        try:
            timeout = int(ac.timeout_sec)
        except (TypeError, ValueError):
            timeout = defaults["timeout_sec"]
            warnings.append(
                f"Ignoring alive_check.timeout_sec={ac.timeout_sec!r}: expected a number. "
                f"Using {timeout}."
            )
        if timeout < 1:
            warnings.append(
                f"alive_check.timeout_sec={timeout} is not a usable timeout; "
                f"using {defaults['timeout_sec']}."
            )
            timeout = defaults["timeout_sec"]
        ac.timeout_sec = timeout

        return warnings

    @classmethod
    def warnings(cls) -> List[str]:
        return list(cls._warnings)

    @classmethod
    def _normalize_rules(cls, cfg: RootCfg) -> List[str]:
        """
        Replaces unusable values with the defaults and says so, instead of
        refusing to start (решение 16).

        The combination rules mirror the gateway's own validation, which answers
        400 ERR_INVALID_COMBINATION for the same cases - the difference is that
        now a wrong value costs a warning and a default, not a container in a
        restart loop with nobody able to read why.
        """
        warnings: List[str] = []
        s = cfg.soax
        soax_defaults = _SECTION_DEFAULTS["soax"]

        if s.on_error not in ("replace", "retry", "fail"):
            warnings.append(
                f"Ignoring soax.on_error={s.on_error!r}: expected replace|retry|fail. "
                f"Using {soax_defaults['on_error']!r}."
            )
            s.on_error = soax_defaults["on_error"]

        try:
            retries = int(s.on_error_retries)
        except (TypeError, ValueError):
            retries = soax_defaults["on_error_retries"]
            warnings.append(
                f"Ignoring soax.on_error_retries={s.on_error_retries!r}: expected a number. "
                f"Using {retries}."
            )
        if not (1 <= retries <= 10):
            warnings.append(
                f"soax.on_error_retries={retries} is outside the 1..10 the gateway accepts. "
                f"Using {soax_defaults['on_error_retries']}."
            )
            retries = soax_defaults["on_error_retries"]
        s.on_error_retries = retries

        if s.bind_node and s.on_error == "replace":
            warnings.append(
                "soax.bind_node cannot be combined with on_error='replace' - the gateway "
                "answers 400 ERR_INVALID_COMBINATION. Turning bind_node off."
            )
            s.bind_node = False

        if s.session_scope not in ("task", "geo"):
            warnings.append(
                f"Ignoring soax.session_scope={s.session_scope!r}: expected task|geo. "
                f"Using {soax_defaults['session_scope']!r}."
            )
            s.session_scope = soax_defaults["session_scope"]

        exec_defaults = _SECTION_DEFAULTS["execution"]
        try:
            concurrency = int(cfg.execution.max_concurrency)
        except (TypeError, ValueError):
            concurrency = 0
        if concurrency < 1:
            warnings.append(
                f"execution.max_concurrency={cfg.execution.max_concurrency!r} would run no "
                f"checks at all. Using {exec_defaults['max_concurrency']}."
            )
            concurrency = exec_defaults["max_concurrency"]
        cfg.execution.max_concurrency = concurrency

        try:
            timeout = int(cfg.execution.timeout_sec)
        except (TypeError, ValueError):
            timeout = 0
        if timeout < 1:
            warnings.append(
                f"execution.timeout_sec={cfg.execution.timeout_sec!r} is not a usable timeout. "
                f"Using {exec_defaults['timeout_sec']}."
            )
            timeout = exec_defaults["timeout_sec"]
        cfg.execution.timeout_sec = timeout

        shots_defaults = _SECTION_DEFAULTS["screenshots"]
        try:
            workers = int(cfg.screenshots.max_workers)
        except (TypeError, ValueError):
            workers = 0
        if workers < 1:
            warnings.append(
                f"screenshots.max_workers={cfg.screenshots.max_workers!r} would block every "
                f"screenshot. Using {shots_defaults['max_workers']}."
            )
            workers = shots_defaults["max_workers"]
        cfg.screenshots.max_workers = workers

        return warnings

    @classmethod
    def _blocking_errors(cls, cfg: RootCfg) -> List[str]:
        """
        Problems that leave the app usable but make every check meaningless.

        Only one so far. Without the package password the gateway refuses
        everything, so a run would produce one failed check per mirror - forty
        false "site unavailable" verdicts from one missing line. The app still
        starts: a page that explains the problem is worth more to whoever has to
        fix it than a container restarting in silence.
        """
        errors: List[str] = []
        if not cfg.soax.package_password:
            errors.append(
                "SOAX_PACKAGE_PASSWORD is not set, so no check can reach the gateway. "
                "Put the package password from the SOAX dashboard (Settings tab) into .env "
                "and restart the container. Do not put it in app.yaml: that file is shown "
                "in the Settings page of this app."
            )
        return errors

    @classmethod
    def blocking_errors(cls) -> List[str]:
        if cls._cfg is None:
            cls.init()
        return list(cls._blocking)

    @classmethod
    def get(cls) -> RootCfg:
        if cls._cfg is None:
            cls.init()
        return cls._cfg

    @classmethod
    def raw_yaml(cls) -> str:
        return cls._yaml_text

    @classmethod
    def save_yaml(cls, text: str) -> tuple[bool, str | None, List[str]]:
        """
        Checks the text, writes it, and deliberately leaves the running config
        alone. Returns (ok, error_message, warnings).

        **The live config is not replaced** (решение 18). It used to be, but only
        in the worker that handled the POST - so after a save one worker out of
        four ran the new settings and three the old, and which one answered a
        request was luck. One rule instead: the file on disk is the next
        startup's configuration, and nothing changes until the container is
        restarted.

        Validation stays strict here, unlike at startup. Startup has to cope
        with whatever is already on disk; this is someone typing into a form,
        and the kind thing is to refuse the text and keep the old file. The
        strictness is also what closes дефект 1: `:::not yaml:::` parses into a
        one-key mapping, every section then fills in from defaults, and the
        result used to be written over a working config.
        """
        app_yaml = cls.config_path()

        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            return False, f"YAML syntax error: {e}", []

        if not isinstance(data, dict):
            return False, "The configuration must be a mapping of sections, e.g. 'soax:'.", []

        known = [section for section in data if section in KNOWN_SECTIONS]
        if not known:
            return False, (
                "No known configuration section found. Expected at least one of: "
                + ", ".join(KNOWN_SECTIONS)
                + ". Refusing to overwrite the current configuration."
            ), []

        try:
            candidate = cls._build(data)
            cls._override_from_env(candidate)
            warnings = cls._normalize_transport(candidate)
            warnings += cls._normalize_use_ipv6(candidate)
            warnings += cls._normalize_alive_check(candidate)
            warnings += cls._normalize_rules(candidate)
        except (TypeError, ValueError) as e:
            return False, str(e), []

        try:
            with open(app_yaml, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            return False, f"Could not write {app_yaml}: {e}", []

        return True, None, warnings
