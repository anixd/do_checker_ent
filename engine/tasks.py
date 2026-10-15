"""
What a run is made of.

Before this module the orchestrator had two near-identical paths: one taking a
flat list of URLs with a single country, the other a list of pre-parsed
dictionaries for Multi-Geo. The ALL-country mode of the first path built the
second path's input by hand in the route. Three shapes, three places to keep in
step.

Here there is one shape - a list of CheckTask - and three pure functions that
build it. Pure on purpose: no config, no network, no filesystem, so the parsing
and the session-id arithmetic are unit-testable, which is where the subtle parts
live (deduplication order, per-row parse errors, and the session scope rules
that the gateway enforces with 409 SESSION_PARAMS_MISMATCH).
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

# Kept here rather than imported from providers.soax_session so that this module
# stays dependency-free; the session-id format is asserted in tests.
from providers.soax_session import make_session_id

FORM_NETWORKS = ("res", "mob", "res_mob")


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
    country code is only checked for shape here - whether 'zz' exists is the
    gateway's business, and it answers ERR_INVALID_VALUE.
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

        if len(country) != 2 or not country.isalpha():
            add_error(url, f"Invalid country code: {parts[1]!r} (expected 2 letters)")
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
