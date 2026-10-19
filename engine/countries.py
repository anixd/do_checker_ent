"""
Country codes, checked against the ISO 3166-1 alpha-2 table.

Everything targeted at a proxy exit node is addressed by a two-letter code, and
until now only the shape of that code was checked: 'zz' passed the forms, went
to the gateway and came back as a refusal that had already cost a request. A
list of forty mirrors spends forty of them before anybody reads the first row.

So the table is consulted at the point of entry instead. The module is pure
data plus string work - no config, no network, no filesystem - which keeps
engine/tasks.py testable without a running application.

The error messages are written for the person who typed the code, and carry a
correction whenever the input resembles something real: 'uk' is not the United
Kingdom (that is 'gb'), 'usa' is the alpha-3 form of 'us', and a full country
name is what someone types when they do not know there is a code at all.
"""

from __future__ import annotations

import re

from iso3166 import countries, countries_by_alpha2, countries_by_alpha3, countries_by_name

# Not a general misspelling table: one entry, for the one code people actually
# type. The United Kingdom is 'gb' in ISO 3166-1; 'uk' is reserved but unused,
# and the gateway charges for the attempt.
_ALIASES = {"uk": "gb"}

# The names below are what a reader sees in the country dropdown and in the
# catalog, so length is a feature: 'United Kingdom of Great Britain and Northern
# Ireland' is the correct name of 'gb' and makes the control wider than the rest
# of the page put together.
#
# The ones listed have no comma or bracket for the rule below to work with, or
# would collide with another country if it did ('Congo' and 'Korea' each name
# two). What stays long is uninhabited or tiny - 'South Georgia and the South
# Sandwich Islands' is nobody's exit node, and an invented abbreviation for it
# would be worse than the real name.
_NAME_OVERRIDES = {
    "gb": "United Kingdom",
    "us": "United States",
    "kr": "South Korea",
    "kp": "North Korea",
    "cd": "DR Congo",
    "la": "Laos",
}


def _build_display_names() -> dict[str, str]:
    """
    Shortens the ISO names once, at import.

    ISO moves the qualifier behind a comma - 'Iran, Islamic Republic of',
    'Bolivia, Plurinational State of' - and puts the disambiguating half in
    brackets - 'Saint Martin (French part)'. Dropping either leaves the country
    as everyone writes it.

    Only when no other country shortens to the same thing, though: 'Virgin
    Islands, British' and 'Virgin Islands, U.S.' keep their full names, because
    what tells them apart is exactly the half being dropped.
    """
    def shorten(name: str) -> str:
        return re.sub(r"\s*\([^)]*\)$", "", name).split(",")[0].strip()

    stems: dict[str, list[str]] = {}
    for country in countries:
        stems.setdefault(shorten(country.apolitical_name).lower(), []).append(country.alpha2)

    names: dict[str, str] = {}
    for country in countries:
        code = country.alpha2.lower()
        stem = shorten(country.apolitical_name)
        unique = len(stems[stem.lower()]) == 1
        names[code] = _NAME_OVERRIDES.get(code) or (stem if unique else country.apolitical_name)
    return names


_DISPLAY_NAMES = _build_display_names()


def normalize(raw: str | None) -> str:
    """Lowercased and trimmed, the form every other function here expects."""
    return (raw or "").strip().lower()


def is_valid(raw: str | None) -> bool:
    """True if this is a code the gateway can target."""
    return normalize(raw).upper() in countries_by_alpha2


def name_of(raw: str | None) -> str | None:
    """
    The country as it should be labelled, or None if the code is not a real one.

    Short on purpose - this ends up in the dropdown and in the catalog file -
    and apolitical where the table offers a choice: 'Taiwan', not 'Taiwan,
    Province of China'. See _build_display_names for how the shortening works.
    """
    return _DISPLAY_NAMES.get(normalize(raw))


def suggest(raw: str | None) -> str | None:
    """
    The alpha-2 code the input probably meant, or None if nothing fits.

    Three sources, cheapest first: the alias above, the alpha-3 column of the
    same table ('usa', 'deu'), and the country names ('germany'). Guessing by
    edit distance is deliberately not attempted - 'at' and 'au' are one letter
    apart and are different continents, so a wrong suggestion would be worse
    than none.
    """
    code = normalize(raw)
    if not code:
        return None

    if code in _ALIASES:
        return _ALIASES[code]

    country = countries_by_alpha3.get(code.upper()) or countries_by_name.get(code.upper())
    if country:
        return country.alpha2.lower()

    return None


def error_for(raw: str | None) -> str | None:
    """
    None if the code is usable, otherwise the message to show the submitter.

    Shape and existence are reported differently on purpose: 'zzz' is the wrong
    length, 'zz' is the right length and still not a country, and conflating
    the two leaves the reader guessing which of the two mistakes they made.
    """
    code = normalize(raw)
    if not code:
        return "Country code is required."

    if is_valid(code):
        return None

    hint = suggest(code)
    hint_text = f" Did you mean {hint!r} ({name_of(hint)})?" if hint else ""

    if len(code) != 2 or not code.isalpha():
        return f"Invalid country code: {raw!r} (expected 2 letters).{hint_text}"

    return f"Unknown country code: {raw!r} (not in ISO 3166-1 alpha-2).{hint_text}"


def split_valid(codes: list[str]) -> tuple[list[str], list[str]]:
    """
    Partitions codes into usable and not, keeping order and dropping repeats.

    For the catalog, which is a stored list rather than a single submission: one
    bad entry there should not stop the rest from being used, but it must not
    reach the gateway either - in the ALL mode it would be paired with every
    URL in the run.
    """
    valid: list[str] = []
    invalid: list[str] = []
    for raw in codes:
        code = normalize(raw)
        if not code:
            continue
        target = valid if is_valid(code) else invalid
        if code not in target:
            target.append(code)
    return valid, invalid
