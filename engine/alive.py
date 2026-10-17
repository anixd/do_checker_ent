"""
The pre-run reachability check.

Why it exists: when the proxy service is down, a run reports every mirror as
unavailable. That is a false report - the mirrors may be perfectly fine - and
it costs the traffic of producing it, forty checks deep. One short probe before
the run turns forty wrong answers into one correct message.

Why it is not the preflight: the preflight proves the gateway accepted us and
handed out an exit node. It does not prove that node goes anywhere. This does,
by fetching third-party sites through it.

Policy lives here, the requests live in the provider (providers/base.py::
check_alive). The provider says what happened to each target; how many of them
have to answer is not a provider question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from config.loader import ConfigStore
from logging_.engine_logger import get_engine_logger
from providers.base import AliveProbe
from providers.registry import get_provider

from .tasks import CheckTask, RunOptions

log = get_engine_logger()


@dataclass
class AliveResult:
    ok: bool
    reason: str = ""
    probes: List[AliveProbe] = field(default_factory=list)
    country: str = ""
    skipped: bool = False


def verdict(probes: List[AliveProbe], required_ok: int) -> bool:
    """Pure: did enough targets answer? Separated out so the rule is testable."""
    return sum(1 for probe in probes if probe.ok) >= required_ok


def _country_for_run(tasks: List[CheckTask]) -> str:
    """
    The country of the run's first real task.

    One rule for all three modes: the dropdown choice in Main Checker, the first
    row's country in Multi-Geo, the first catalog country in ALL mode. Probing
    every country is out of the question - the point of this check is to find
    out quickly whether the provider is up, and twelve countries times three
    requests is not quickly.
    """
    for task in tasks:
        if task.is_valid and task.country:
            return task.country
    return ""


def ensure_proxy_alive(tasks: List[CheckTask], options: RunOptions) -> AliveResult:
    """
    Runs the probe and returns a verdict. Never raises: a failure to probe is
    itself a "not alive" answer, and the caller has to be able to say so.

    Deliberately uncached. A cached "alive" from a minute ago is exactly the
    reassurance that is worthless - the provider can go down in that minute -
    and the saving would be a couple of seconds.
    """
    cfg = ConfigStore.get().alive_check

    if not cfg.enabled:
        return AliveResult(ok=True, skipped=True, reason="alive-check disabled in config")

    country = _country_for_run(tasks)
    if not country:
        # Nothing in this run will reach the provider anyway (every row failed
        # to parse), so there is nothing to protect and no country to probe with.
        return AliveResult(ok=True, skipped=True, reason="no valid task to take a country from")

    provider = get_provider(options.channel.provider)
    marker = f"[alive-check {provider.name}]"
    transport = options.channel.transport

    # Carries the country and nothing else: sessions are the provider's
    # business, and it hands out one per probe.
    probe_task = CheckTask(index=0, url="", country=country)

    try:
        probes = provider.check_alive(
            probe_task, options, cfg.targets, cfg.timeout_sec,
            stop_after_ok=cfg.required_ok,
        )
    except Exception as e:
        # A configuration problem (no package password, a rule the gateway will
        # not take). Starting the run would turn it into one failed check per
        # mirror.
        log.error("%s could not run: %s", marker, e, exc_info=True)
        return AliveResult(
            ok=False,
            country=country,
            reason=f"{provider.name.upper()} proxy service could not be probed: {e}",
        )

    for probe in probes:
        if probe.ok:
            log.info("%s %s -> %s in %s ms", marker, probe.target, probe.status, probe.ms)
        else:
            log.warning("%s %s -> FAILED in %s ms: %s",
                        marker, probe.target, probe.ms, probe.error)

    ok_count = sum(1 for probe in probes if probe.ok)
    alive = verdict(probes, cfg.required_ok)

    # Denominator is the configured target count, not the number of probes that
    # ran: the provider stops early once the verdict is settled, and "2/2" would
    # hide that there was a third target it never needed to try.
    log.log(
        20 if alive else 40,  # INFO / ERROR
        "%s %s: %s/%s targets ok (need %s) via %s over %s%s",
        marker, "alive" if alive else "dead", ok_count, len(cfg.targets),
        cfg.required_ok, country, transport,
        "" if len(probes) == len(cfg.targets) else f" [stopped after {len(probes)} probes]",
    )

    if alive:
        return AliveResult(ok=True, probes=probes, country=country)

    return AliveResult(
        ok=False,
        probes=probes,
        country=country,
        reason=(
            f"{provider.name.upper()} proxy service is not reachable: "
            f"{ok_count} of {len(cfg.targets)} alive-check targets responded "
            f"(need {cfg.required_ok}). Try again later."
        ),
    )
