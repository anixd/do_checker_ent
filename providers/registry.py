"""
Which provider serves a channel.

A dict rather than a plugin mechanism: there is one entry today and there will
be two. `fastproxys` is listed as unavailable on purpose, so picking it fails
with a sentence a person can act on instead of a KeyError.
"""

from __future__ import annotations

from .base import Provider
from .soax_provider import SoaxProvider

_PROVIDERS: dict[str, Provider] = {
    SoaxProvider.name: SoaxProvider(),
}

_PLANNED = {
    "fastproxys": "fastproxys.net is not implemented yet",
}


def get_provider(name: str | None) -> Provider:
    key = (name or SoaxProvider.name).strip().lower()
    if key in _PROVIDERS:
        return _PROVIDERS[key]
    if key in _PLANNED:
        raise ValueError(_PLANNED[key])
    raise ValueError(
        f"unknown proxy provider {name!r}; available: {', '.join(sorted(_PROVIDERS))}"
    )


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)
