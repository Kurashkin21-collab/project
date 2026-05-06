"""
parsers/price_parser.py

Парсер цен с сайтов Пятёрочки, Перекрёстка, Магнита.
Запускается когда DeepSeek возвращает price_check_needed.
Результаты отдаются Ламе для выбора лучшего варианта по скору.
"""

import asyncio
import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Заголовки браузера чтобы не блокировали ───────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "application/json, text/html",
}


# ── Пятёрочка ────────────────────────────────────────────────────────────────

async def search_pyaterochka(query: str) -> list[dict]:
    """Ищет продукт в каталоге Пятёрочки через API."""
    try:
        url = "https://www.pyaterochka.ru/api/v2/catalog/products/search"
        params = {"text": query, "limit": 5, "store_id": "S073"}  # Москва
        async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            results = []
            for item in data.get("products", [])[:5]:
                price = item.get("price", {}).get("regular_price")
                weight = _extract_weight(item.get("name", ""), item.get("unit_value"))
                if not price or not weight:
                    continue
                price_100g = round(price / weight * 100, 2)
                results.append({
                    "name":         item.get("name", query),
                    "store":        "Пятёрочка",
                    "price":        price,
                    "weight_g":     weight,
                    "price_per_100g": price_100g,
                    "kcal_100g":    None,  # Лама достанет из описания
                    "protein_100g": None,
                    "fat_100g":     None,
                    "carbs_100g":   None,
                    "url":          f"https://www.pyaterochka.ru{item.get('url', '')}",
                })
            return results
    except Exception as e:
        logger.warning(f"Пятёрочка: {e}")
        return []


# ── Перекрёсток ───────────────────────────────────────────────────────────────

async def search_perekrestok(query: str) -> list[dict]:
    """Ищет продукт в каталоге Перекрёстка."""
    try:
        url = "https://www.perekrestok.ru/api/customer/1.4.1.0/catalog/product/search"
        params = {"search": query, "perPage": 5}
        async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            results = []
            for item in data.get("content", {}).get("items", [])[:5]:
                price = item.get("price", {}).get("price")
                weight = _extract_weight(
                    item.get("title", ""),
                    item.get("weightObj", {}).get("value")
                )
                if not price or not weight:
                    continue
                price_100g = round(price / 100 / weight * 100, 2)  # цена в копейках
                results.append({
                    "name":           item.get("title", query),
                    "store":          "Перекрёсток",
                    "price":          price / 100,
                    "weight_g":       weight,
                    "price_per_100g": price_100g,
                    "kcal_100g":      None,
                    "protein_100g":   None,
                    "fat_100g":       None,
                    "carbs_100g":     None,
                })
            return results
    except Exception as e:
        logger.warning(f"Перекрёсток: {e}")
        return []


# ── Магнит ────────────────────────────────────────────────────────────────────

async def search_magnit(query: str) -> list[dict]:
    """Ищет продукт в каталоге Магнита."""
    try:
        url = "https://magnit.ru/api/v1/goods/search"
        params = {"query": query, "limit": 5}
        headers = {**HEADERS, "X-Store-Id": "4"}
        async with httpx.AsyncClient(headers=headers, timeout=15) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            results = []
            for item in data.get("goods", [])[:5]:
                price = item.get("price")
                weight = _extract_weight(
                    item.get("name", ""),
                    item.get("weight")
                )
                if not price or not weight:
                    continue
                price_100g = round(price / weight * 100, 2)
                results.append({
                    "name":           item.get("name", query),
                    "store":          "Магнит",
                    "price":          price,
                    "weight_g":       weight,
                    "price_per_100g": price_100g,
                    "kcal_100g":      None,
                    "protein_100g":   None,
                    "fat_100g":       None,
                    "carbs_100g":     None,
                })
            return results
    except Exception as e:
        logger.warning(f"Магнит: {e}")
        return []


# ── Общий поиск по всем магазинам ─────────────────────────────────────────────

async def search_all_stores(query: str) -> list[dict]:
    """
    Ищет продукт во всех магазинах параллельно.
    Возвращает все варианты — Лама выберет лучший по скору.
    """
    tasks = [
        search_pyaterochka(query),
        search_perekrestok(query),
        search_magnit(query),
        search_lavka(query),
        search_chizhik(query),
        search_lenta(query),
        search_auchan(query),
    ]
    results_by_store = await asyncio.gather(*tasks, return_exceptions=True)
    all_results = []
    for r in results_by_store:
        if isinstance(r, list):
            all_results.extend(r)
    return all_results


async def search_products_batch(product_names: list[str]) -> dict[str, list[dict]]:
    """
    Ищет несколько продуктов параллельно.
    Возвращает словарь {название: [варианты]}.
    """
    tasks = {name: search_all_stores(name) for name in product_names}
    results = {}
    for name, coro in tasks.items():
        try:
            results[name] = await coro
            await asyncio.sleep(0.3)  # небольшая пауза между запросами
        except Exception as e:
            logger.warning(f"Ошибка поиска '{name}': {e}")
            results[name] = []
    return results


# ── Хелперы ───────────────────────────────────────────────────────────────────

def _extract_weight(name: str, weight_value=None) -> Optional[float]:
    """Извлекает вес в граммах из названия или поля weight."""
    if weight_value:
        try:
            w = float(str(weight_value).replace(",", "."))
            # Если меньше 10 — скорее всего в кг
            return w * 1000 if w < 10 else w
        except Exception:
            pass

    # Пробуем из названия
    import re
    patterns = [
        r"(\d+(?:[.,]\d+)?)\s*кг",
        r"(\d+(?:[.,]\d+)?)\s*г(?:р)?(?:\b|\.)",
        r"(\d+(?:[.,]\d+)?)\s*ml",
        r"(\d+(?:[.,]\d+)?)\s*л(?:\b|\.)",
    ]
    for p in patterns:
        m = re.search(p, name.lower())
        if m:
            val = float(m.group(1).replace(",", "."))
            if "кг" in p or "л" in p.lower():
                return val * 1000
            return val
    return None
