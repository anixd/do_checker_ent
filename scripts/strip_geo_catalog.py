#!/usr/bin/env python3
"""
Strips regions / cities / isps out of data/catalog/soax_geo.json.

This build targets by country only: the gateway's region/city/isp rules need
slugs from an API we no longer have access to, so the nested lists are dead
weight. The keys are kept as empty arrays rather than removed, since
CatalogStore.get_regions() and friends still index into them.

Usage:
    python3 scripts/strip_geo_catalog.py                    # default path
    python3 scripts/strip_geo_catalog.py path/to/geo.json
    python3 scripts/strip_geo_catalog.py --dry-run          # report only
    python3 scripts/strip_geo_catalog.py --no-backup

Safe to run twice: a already-stripped file is left untouched.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

DEFAULT_PATH = Path("data/catalog/soax_geo.json")
NESTED_KEYS = ("regions", "cities", "isps")


def strip_catalog(path: Path, dry_run: bool = False, backup: bool = True) -> int:
    if not path.is_file():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 1

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: {path} is not valid JSON: {e}", file=sys.stderr)
        return 1

    countries = data.get("countries")
    if not isinstance(countries, list):
        print(f"ERROR: no 'countries' array in {path}", file=sys.stderr)
        return 1

    removed = {key: 0 for key in NESTED_KEYS}
    touched_countries = 0

    for country in countries:
        if not isinstance(country, dict):
            continue

        country_touched = False
        for key in NESTED_KEYS:
            values = country.get(key)
            if isinstance(values, list) and values:
                removed[key] += len(values)
                country_touched = True
            # Set unconditionally: also normalises a missing or malformed key.
            country[key] = []

        if country_touched:
            touched_countries += 1

    total_removed = sum(removed.values())

    print(f"File:      {path}")
    print(f"Countries: {len(countries)}")
    for key in NESTED_KEYS:
        print(f"  {key:<8} entries removed: {removed[key]}")
    print(f"Countries affected: {touched_countries}")

    if total_removed == 0:
        print("Nothing to strip - file is already clean.")
        return 0

    if dry_run:
        print("\nDry run: no changes written.")
        return 0

    if backup:
        backup_path = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup_path)
        print(f"\nBackup written to {backup_path}")

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp_path.replace(path)

    size_kb = path.stat().st_size / 1024
    print(f"Stripped {total_removed} entries. New size: {size_kb:.1f} KB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Empty the regions/cities/isps arrays in soax_geo.json"
    )
    parser.add_argument(
        "path", nargs="?", type=Path, default=DEFAULT_PATH,
        help=f"path to soax_geo.json (default: {DEFAULT_PATH})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report what would be removed without writing",
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help="skip writing the .bak copy",
    )
    args = parser.parse_args()

    return strip_catalog(
        args.path, dry_run=args.dry_run, backup=not args.no_backup
    )


if __name__ == "__main__":
    sys.exit(main())
