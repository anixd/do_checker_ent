"""
What a run is made of.

One shape for every mode - a list of CheckTask - and three pure functions that
build it, so that a single country, the ALL-country expansion and Multi-Geo do
not become three input formats with three places to keep in step.

Pure on purpose: no config, no network, no filesystem, so the parsing
and the session-id arithmetic are unit-testable, which is where the subtle parts
live (deduplication order, per-row parse errors, and the session scope rules
that the gateway enforces with 409 SESSION_PARAMS_MISMATCH).
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from engine.countries import error_for as country_error

# Kept here rather than imported from providers.soax_session so that this module
# stays dependency-free; the session-id format is asserted in tests.
from providers.soax_session import make_session_id

FORM_NETWORKS = ("res", "mob", "res_mob")
ALLOWED_TRANSPORTS = ("http", "socks5")
ALLOWED_RESOLVE = ("remote", "local")

# requests needs PySocks for socks5:// URLs. Without it every socks5 check comes
# back as "connect_error: Missing dependencies for SOCKS support", which reads as
# "the site is unreachable" - on a list of forty mirrors, forty false verdicts
# from one missing dependency. Checked up front instead, so the run is refused.
try:  # pragma: no cover - the import either works in this image or it does not
    import socks  # noqa: F401
    SOCKS_SUPPORT_AVAILABLE = True
except ImportError:
    SOCKS_SUPPORT_AVAILABLE = False


@dataclass(frozen=True)
class Channel:
    """
    How a run reaches the target: which provider, over which transport, with DNS
    resolved where.

    One run carries exactly one channel - HTTP and SOCKS5 are not mixed within a
    run, and the provider is picked explicitly. The field exists now so that the
    SOCKS5 and fastproxys work has somewhere to land without reshaping the task
    list again.
    """

    provider: str = "soax"
    transport: str = "http"
    resolve: str = "remote"  # remote (on the exit node) | local

    @property
    def slug(self) -> str:
        """
        Names the run's log subfolder, e.g. 'soax-http'. The resolve mode is left
        out: it does not change which node serves the request, so it would only
        make directory names longer.
        """
        return f"{self.provider}-{self.transport}"

    @property
    def proxy_scheme(self) -> str:
        """
        The URL scheme requests needs for this channel.

        socks5h hands the hostname to the proxy so the exit node resolves it;
        socks5 resolves here and sends an address. For mirror checking the first
        is usually what you want - a geo-targeted check whose DNS happened in
        our datacentre is only half a geo-targeted check - which is why `remote`
        is the default resolve mode.
        """
        if self.transport == "socks5":
            return "socks5h" if self.resolve == "remote" else "socks5"
        return self.transport

@dataclass
class CheckTask:
    """
    One check: one URL from one country over one channel.

    `index` is the position in the submitted list and survives the thread pool,
    which finishes tasks in whatever order it likes. It is what lets results be
    displayed in input order, and it feeds the session id under scope='task'.
    """

    index: int
    url: str
    country: str | None = None
    session_id: str | None = None
    parsing_error: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.parsing_error is None


def make_channel(provider: str | None, transport: str | None, resolve: str | None) -> Channel:
    """
    Validates what the form sent. Raises ValueError with a message meant for the
    person who submitted the form.

    There is no fallback for the transport here on purpose: the caller passes
    either the submitted value or the configured default, and if neither exists
    the run must be refused rather than guessed - http and socks5 can give
    different answers for the same mirror.
    """
    chosen_provider = (provider or "soax").strip().lower()
    chosen_transport = (transport or "").strip().lower()
    chosen_resolve = (resolve or "remote").strip().lower()

    if not chosen_transport:
        raise ValueError(
            "Transport is required: choose HTTP or SOCKS5 "
            "(no default is configured in app.yaml)"
        )
    if chosen_transport not in ALLOWED_TRANSPORTS:
        raise ValueError(
            f"Unknown transport {transport!r}; expected one of {'|'.join(ALLOWED_TRANSPORTS)}"
        )
    if chosen_resolve not in ALLOWED_RESOLVE:
        raise ValueError(
            f"Unknown DNS resolve mode {resolve!r}; expected one of {'|'.join(ALLOWED_RESOLVE)}"
        )
    if chosen_transport == "socks5" and not SOCKS_SUPPORT_AVAILABLE:
        raise ValueError(
            "SOCKS5 is not available in this build: PySocks is not installed. "
            "Rebuild the image (docker compose up -d --build) or pick HTTP."
        )

    return Channel(provider=chosen_provider, transport=chosen_transport, resolve=chosen_resolve)


@dataclass
class RunOptions:
    """
    Everything a run needs that is not per-task.

    `label` distinguishes the two entry points in log paths and in the SSE
    payload; `subfolder` is filled in by the orchestrator at start time so that
    every card of a run lands together.
    """

    run_id: str = ""
    label: str = "std-check"  # std-check | multi-geo
    channel: Channel = field(default_factory=Channel)
    network: str = "res_mob"
    timeout_sec: int = 60
    make_screenshot: bool = False
    debug_mode: bool = False
    subfolder: str = ""


def run_subfolder(label: str, channel: Channel, now: datetime | None = None) -> str:
    """
    Names a run's directory under ./logs/<date>/, e.g. '12-08-42_std-check_soax-http'.

    The channel is in the directory name rather than in every filename because a
    run has exactly one channel; this way the tree says what each run was checked
    with, and the filenames stay about the site.
    """
    now = now or datetime.now()
    return f"{now:%H-%M-%S}_{label}_{channel.slug}"


def normalize_url(raw_url: str) -> str:
    """
    Lowercases the domain and leaves the path and query alone.

    Domain.COM/LoCaL_PatH -> domain.com/LoCaL_PatH

    The case of the path matters: forcing the whole URL to lowercase used to turn
    a working link like /dDjpEn63 into a 404.
    """
    url = raw_url.strip()
    if not url:
        return ""

    has_scheme = "://" in url
    parse_url = url if has_scheme else f"http://{url}"

    try:
        parsed = urllib.parse.urlsplit(parse_url)
        scheme = parsed.scheme + "://" if has_scheme else ""
        new_url = f"{scheme}{parsed.netloc.lower()}{parsed.path}"
        if parsed.query:
            new_url += f"?{parsed.query}"
        if parsed.fragment:
            new_url += f"#{parsed.fragment}"
        return new_url
    except ValueError:
        return url


def run_totals(tasks: Iterable[CheckTask]) -> dict:
    """
    What the run consists of, for the totals shown above the results.

    Rows that never reached the provider are counted as checks - they are part
    of what was submitted and the reader has to see they exist - but not as
    domains or countries: a line that failed to parse was not checked anywhere,
    and counting `badline` as a domain would overstate the report. They come
    back separately as `invalid` so the plaque can say how many were skipped.
    """
    tasks = list(tasks)
    checked = [task for task in tasks if task.is_valid]
    return {
        "checks": len(tasks),
        "domains": len({task.url for task in checked if task.url}),
        "countries": len({task.country for task in checked if task.country}),
        "invalid": len(tasks) - len(checked),
    }


def _dedup(values: Iterable[str]) -> list[str]:
    """Drops repeats while keeping the submitted order."""
    return list(dict.fromkeys(values))


def build_single_country_tasks(urls_raw: str, country: str) -> list[CheckTask]:
    """One country, every submitted URL."""
    urls = _dedup(normalize_url(u) for u in urls_raw.splitlines() if u.strip())
    country = (country or "").strip().lower()
    return [CheckTask(index=i, url=u, country=country) for i, u in enumerate(urls) if u]


def build_all_country_tasks(urls_raw: str, country_codes: Iterable[str]) -> list[CheckTask]:
    """
    Every submitted URL against every country in the catalog (the ALL option).

    URL is the outer loop, so one site's countries stay together in the output -
    that is the order a reader scans.
    """
    urls = [u for u in _dedup(normalize_url(u) for u in urls_raw.splitlines() if u.strip()) if u]
    codes = _dedup((c or "").strip().lower() for c in country_codes)

    tasks: list[CheckTask] = []
    for url in urls:
        for code in codes:
            if code:
                tasks.append(CheckTask(index=len(tasks), url=url, country=code))
    return tasks


def build_multi_geo_tasks(urls_raw: str) -> list[CheckTask]:
    """
    One 'domain country_code' pair per line.

    A bad line becomes a task carrying parsing_error instead of failing the whole
    submission: the point of a mirror report is the rows that did work. The
    country code is checked against the ISO 3166-1 table, so a code that does
    not exist costs a row in the report rather than a request to the gateway.
    """
    tasks: list[CheckTask] = []
    seen_tasks: set[tuple[str, str]] = set()
    seen_errors: set[tuple[str, str]] = set()

    def add_error(url: str, message: str) -> None:
        key = (url, message)
        if key in seen_errors:
            return
        seen_errors.add(key)
        tasks.append(CheckTask(index=len(tasks), url=url, parsing_error=message))

    for line in (ln.strip() for ln in urls_raw.splitlines()):
        if not line:
            continue
        parts = line.split()

        if len(parts) < 2:
            add_error(parts[0].lower() if parts else "Unknown",
                      "Invalid format (expected: url cc)")
            continue

        url = normalize_url(parts[0])
        country = parts[1].lower()

        country_problem = country_error(parts[1])
        if country_problem:
            add_error(url, country_problem)
            continue

        key = (url, country)
        if key in seen_tasks:
            continue
        seen_tasks.add(key)
        tasks.append(CheckTask(index=len(tasks), url=url, country=country))

    return tasks


def assign_session_ids(tasks: list[CheckTask], run_id: str, scope: str) -> None:
    """
    Gives every valid task its SOAX session id, in place.

    scope='task': one session per check, so a single bad exit node spoils one row
    rather than the report.

    scope='geo': one session per country. Keyed on the country and not on the
    task index, because a session's rules are locked on its first request and
    reusing an id with different rules is rejected with 409
    SESSION_PARAMS_MISMATCH. A single-country run therefore collapses to one
    session, which is the old behaviour of the non-Multi-Geo path.
    """
    geo_index: dict[str, int] = {}
    for task in tasks:
        country = (task.country or "").strip().lower()
        if country and country not in geo_index:
            geo_index[country] = len(geo_index)

    for task in tasks:
        if not task.is_valid:
            continue
        country = (task.country or "").strip().lower()
        slot = task.index if scope == "task" else geo_index.get(country, 0)
        task.session_id = make_session_id(run_id, slot)
