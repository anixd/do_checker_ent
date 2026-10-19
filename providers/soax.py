from __future__ import annotations
import os, json, time
from typing import Dict, List
from datetime import datetime
from config.loader import ConfigStore
from engine.countries import name_of
from logging_.engine_logger import get_engine_logger
from .soax_api import SoaxApiClient

log = get_engine_logger()
CATALOG_PATH = None


def _catalog_path() -> str:
    """Gets the path to the geo catalog, respecting config."""
    global CATALOG_PATH
    if CATALOG_PATH:
        return CATALOG_PATH
    try:
        data_dir = ConfigStore.get().paths.data_dir
        CATALOG_PATH = os.path.join(data_dir, "catalog", "soax_geo.json")
        return CATALOG_PATH
    except Exception as e:
        log.error(f"Failed to resolve CATALOG_PATH: {e}")
        return "./data/catalog/soax_geo.json"


# хелперы для нормализации входящих данных
def _normalize_regions(api_regions: List[str]) -> List[Dict[str, str]]:
    if not api_regions:
        return []

    regions = []
    for slug in api_regions:
        if not isinstance(slug, str): continue  # пропускаем, если API вернул что-то не то
        # Создаем "Name" из "slug"
        name = slug.replace('+', ' ').title()
        regions.append({"code": slug, "name": name})
    return regions


def _normalize_cities(api_cities: List[str]) -> List[Dict[str, str]]:
    if not api_cities:
        return []

    cities = []
    for slug in api_cities:
        if not isinstance(slug, str): continue
        name = slug.replace('+', ' ').title()
        cities.append({"code": slug, "name": name})
    return cities


def _normalize_isps(api_isps: List[str]) -> List[str]:
    """
    API возвращает список строк, что нам собсно и нужно.
    Просто фильтруем не-строковые значения.
    """
    if not api_isps:
        return []
    return [isp for isp in api_isps if isinstance(isp, str)]


class CatalogStore:
    _cache: dict | None = None

    @classmethod
    def _load_or_cache(cls, force_reload: bool = False) -> dict:
        if cls._cache and not force_reload:
            return cls._cache
        try:
            with open(_catalog_path(), "r", encoding="utf-8") as f:
                cls._cache = json.load(f)
                return cls._cache
        except Exception as e:
            log.error(f"Failed to load soax_geo.json from {_catalog_path()}: {e}")
            return {"version": 0, "countries": []}

    @classmethod
    def get_countries(cls) -> list[dict]:
        data = cls._load_or_cache()
        return [{"code": c.get("code"), "name": c.get("name")}
                for c in data.get("countries", [])
                if c.get("code") and c.get("name")]

    @classmethod
    def get_regions(cls, country_code: str) -> list[dict]:
        data = cls._load_or_cache()
        for c in data.get("countries", []):
            if c.get("code") == country_code:
                return c.get("regions", [])
        return []

    @classmethod
    def get_cities(cls, country_code: str, region_code: str | None = None) -> list[dict]:
        data = cls._load_or_cache()
        for c in data.get("countries", []):
            if c.get("code") == country_code:
                return c.get("cities", [])
        return []

    @classmethod
    def get_isps(cls, country_code: str) -> list[str]:
        data = cls._load_or_cache()
        for c in data.get("countries", []):
            if c.get("code") == country_code:
                return c.get("isps", [])
        return []

    @classmethod
    def save(cls, data: dict):
        """Saves data back to the JSON file."""
        try:
            with open(_catalog_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            cls._cache = None  # Invalidate cache
            log.info("CatalogStore.save() successful.")
        except Exception as e:
            log.error(f"Failed to save soax_geo.json to {_catalog_path()}: {e}")


    @classmethod
    def update_country_list(cls, final_codes: list[str]):
        """
        Rebuilds the countries list in soax_geo.json based on the provided list of codes.
        Keeps existing data for known codes, creates stubs for new codes.
        """
        log.info(f"Updating catalog country list with {len(final_codes)} codes...")
        try:
            data = cls._load_or_cache(force_reload=True)

            # Create a lookup map of old data
            old_countries_map = {
                c.get('code'): c
                for c in data.get("countries", [])
                if c.get('code')
            }

            new_countries_list = []

            for code in final_codes:
                if not code:  # Skip any empty strings
                    continue

                if code in old_countries_map:
                    # Keep existing entry
                    new_countries_list.append(old_countries_map[code])
                else:
                    # The name comes from the ISO 3166-1 table, so the dropdown
                    # reads 'Ukraine' rather than 'UA'; an unknown code keeps
                    # the code as its own label instead of showing nothing.
                    log.info(f"Adding new geo for country code: {code}")
                    new_geo = {
                        "code": code,
                        "name": name_of(code) or code.upper(),
                        "regions": [],
                        "cities": [],
                        "isps": []
                    }
                    new_countries_list.append(new_geo)

            data["countries"] = new_countries_list
            cls.save(data)
            log.info("Catalog country list updated successfully.")

        except Exception as e:
            log.error(f"Failed to run update_country_list: {e}", exc_info=True)


    @classmethod
    def update_catalog_from_api(cls):
        """
        Основная логика обновления каталога.
        Проходит по *существующим* странам в JSON и обновляет их данные.
        """
        log.info("Starting SOAX catalog refresh...")
        api = SoaxApiClient()
        if not api.is_configured():
            log.error("Catalog refresh failed: SOAX_API_KEY or SOAX_PACKAGE_KEY not set.")
            return

        data = cls._load_or_cache(force_reload=True)
        countries_to_update = data.get("countries", [])
        if not countries_to_update:
            log.warning("Catalog refresh failed: soax_geo.json is empty or has no countries.")
            return

        conn_type = "wifi"

        for country in countries_to_update:
            code = country.get("code")
            if not code:
                continue

            log.info(f"Fetching geo data for country: {code}...")

            # Получаем регионы
            api_regions = api.get_regions(code, conn_type)
            if api_regions is not None:
                country["regions"] = _normalize_regions(api_regions)

            # Получаем города
            api_cities = api.get_cities(code, conn_type=conn_type)
            if api_cities is not None:
                country["cities"] = _normalize_cities(api_cities)

            # Получаем ISPs
            api_isps = api.get_isps(code)
            if api_isps is not None:
                country["isps"] = _normalize_isps(api_isps)

            time.sleep(1.0)

        data["generated_at"] = datetime.now().isoformat(timespec="seconds")
        data["version"] = data.get("version", 1) + 1

        cls.save(data)
        log.info("SOAX catalog refresh finished successfully.")


# функция запуска в фоновом потоке (интересно, взлетит или буду ипацца два дня?) :)
def refresh_catalog_data():
    """Точка входа для Thread, вызывает обновление."""
    try:
        CatalogStore.update_catalog_from_api()
    except Exception as e:
        log.error(f"Unhandled exception in refresh_catalog_data thread: {e}", exc_info=True)
