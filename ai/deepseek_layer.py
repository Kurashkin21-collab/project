"""
ai/deepseek_layer.py

Слой DeepSeek.
Flash — еженедельный пересчёт плана (дёшево, быстро)
Pro   — онбординг и месячный глубокий анализ (качество)
"""

import json
import httpx
from config import DEEPSEEK_API_KEY, DEEPSEEK_URL, DEEPSEEK_FLASH, DEEPSEEK_PRO, TOKENS


def _headers():
    return {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }


async def _call(model: str, messages: list, max_tokens: int, temperature: float = 0.3) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # Для Pro отключаем thinking — нам нужен чистый JSON без reasoning
    if "pro" in model:
        payload["thinking"] = {"type": "disabled"}
    
    body = json.dumps(payload, ensure_ascii=False).encode()
    timeout = httpx.Timeout(900.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(DEEPSEEK_URL, headers=_headers(), content=body)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        # content может быть пустым если включён thinking — берём reasoning_content
        content = msg.get("content") or msg.get("reasoning_content") or ""
        return content


def _clean_json(raw: str) -> str:
    """Извлекает JSON из ответа модели — устойчиво к тексту вокруг."""
    import re
    # Убираем markdown блоки
    raw = raw.replace("```json", "").replace("```", "").strip()
    # Ищем первый { или [ и последний } или ]
    start_obj = raw.find("{")
    start_arr = raw.find("[")
    
    if start_obj == -1 and start_arr == -1:
        return raw
    
    # Определяем с чего начинается JSON
    if start_obj == -1:
        start = start_arr
        end_char = "]"
    elif start_arr == -1:
        start = start_obj
        end_char = "}"
    else:
        if start_arr < start_obj:
            start = start_arr
            end_char = "]"
        else:
            start = start_obj
            end_char = "}"
    
    end = raw.rfind(end_char)
    if end == -1:
        return raw[start:]
    return raw[start:end+1]


# ── Системный промпт ──────────────────────────────────────────────────────────

PLAN_SYSTEM = """Ты эксперт по нутрициологии и планированию бюджета.
Составь персональный план питания на неделю.

ПРИНЦИПЫ:
1. Оптимизируй бюджет + КБЖУ одновременно
2. Не планируй еду в дни доставки пользователя
3. Продукты должны пересекаться между блюдами
4. Скоропорт — на неделю, долгохран — на месяц
5. Цель — снизить траты до целевого бюджета
6. Сезон: май (дешёвые овощи: огурцы, помидоры, зелень)

ВАЖНО: Отвечай ТОЛЬКО валидным JSON без текста вокруг.
Не используй reasoning/thinking теги в ответе — только JSON."""


PLAN_SCHEMA = """{
  "weekly_menu": {
    "mon": {"breakfast": "блюдо или null", "lunch": "блюдо или null", "dinner": "блюдо"},
    "tue": {...},
    "wed": {...},
    "thu": {...},
    "fri": {...},
    "sat": {"breakfast": null, "lunch": null, "dinner": "доставка"},
    "sun": {...}
  },
  "dishes": [
    {
      "name": "название блюда",
      "ingredients": ["продукт 1", "продукт 2"],
      "cook_time_min": 20,
      "kbju": {"kcal": 400, "protein": 35, "fat": 8, "carbs": 45}
    }
  ],
  "shopping_weekly": [
    {
      "name": "куриное филе",
      "amount_g": 600,
      "price_per_100g": 28,
      "total_price": 168,
      "store": "Пятёрочка",
      "used_in": ["греча с курицей", "паста с курицей"]
    }
  ],
  "shopping_monthly": [
    {
      "name": "греча",
      "amount_g": 1000,
      "price_per_100g": 8,
      "total_price": 80,
      "store": "Пятёрочка",
      "shelf_life": "долгохран"
    }
  ],
  "budget": {
    "current_monthly": 5800,
    "target_monthly": 4200,
    "this_week_food": 1050,
    "this_week_vs_before": -350,
    "savings_strategy": "Основной резерв в доставке — заменить 1 из 2 субботних доставок на яичницу (5 мин)"
  },
  "kbju_day": {
    "target_kcal": 2100,
    "planned_kcal": 1950,
    "target_protein": 160,
    "planned_protein": 148,
    "gap_note": "Небольшой дефицит калорий — в рамках цели похудения"
  },
  "insights": [
    "Куриное филе даёт лучший белок за рубль в твоём профиле — 1г белка ≈ 4₽",
    "Пятница — доставка, не планируем"
  ],
  "adjustments_from_last": [],
  "price_check_needed": ["чечевица красная", "творог 5%", "куриное филе"]
}"""


# ── Онбординг — первичный анализ (Pro) ───────────────────────────────────────

async def analyze_onboarding(compressed_profile: str) -> dict:
    """
    Вызывается один раз при первом запуске.
    Строит полный план с нуля на основе профиля и истории трат.
    Использует Pro для максимального качества.
    """
    messages = [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": (
            f"Данные пользователя:\n{compressed_profile}\n\n"
            f"Составь первый план питания. Схема ответа:\n{PLAN_SCHEMA}"
        )},
    ]
    raw = await _call(DEEPSEEK_PRO, messages, TOKENS["ds_onboarding"])
    return json.loads(_clean_json(raw))


# ── Еженедельный пересчёт (Flash) ─────────────────────────────────────────────

async def weekly_update(
    compressed_profile: str,
    prev_plan: dict,
    products_with_prices: list[dict],
) -> dict:
    """
    Вызывается раз в неделю.
    Адаптирует план под новые данные и актуальные цены.
    Использует Flash — данные уже структурированы, глубокое мышление не нужно.
    """
    messages = [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": json.dumps({
            "mode":            "weekly_update",
            "profile_summary": compressed_profile,
            "prev_plan":       prev_plan,
            "current_prices":  products_with_prices,
            "instruction":     f"Обнови план на следующую неделю. Схема: {PLAN_SCHEMA}",
        }, ensure_ascii=False)},
    ]
    raw = await _call(DEEPSEEK_FLASH, messages, TOKENS["ds_weekly"])
    return json.loads(_clean_json(raw))


# ── Месячный глубокий анализ (Pro) ────────────────────────────────────────────

MONTHLY_SYSTEM = """Ты проводишь глубокий анализ пищевого поведения и финансов за месяц.

Задачи:
1. Выяви паттерны которые мешают достичь цели по бюджету
2. Оцени прогресс по КБЖУ — насколько план выполнялся реально
3. Скорректируй стратегию на следующий месяц
4. Обнови долгохранный список — что закончилось, что купить оптом

Верни строго JSON, никакого текста."""

MONTHLY_SCHEMA = """{
  "patterns_found": [
    "Доставка растёт по выходным — в среднем +650₽ к плану",
    "Завтраки выполнялись только 3 дня из 7"
  ],
  "budget_progress": {
    "planned": 4200,
    "actual": 4850,
    "gap": 650,
    "main_overrun": "доставка по выходным",
    "trend": "улучшается / ухудшается / стабильно"
  },
  "kbju_progress": {
    "kcal_avg_planned": 1950,
    "kcal_avg_actual": 2100,
    "protein_avg_planned": 148,
    "protein_avg_actual": 130,
    "gap_note": "Белок не добирается — заменить часть круп на творог"
  },
  "strategy_next_month": "Сократить доставку до 1 раза в выходные вместо 2. Добавить творог для белка.",
  "monthly_restock": [
    {"name": "греча", "amount_g": 2000, "reason": "закончилась"},
    {"name": "масло подсолнечное", "amount_g": 1000, "reason": "по плану"}
  ],
  "updated_plan": { "...": "полный обновлённый план как в weekly" }
}"""


async def monthly_deep_analysis(
    compressed_profile: str,
    nutrition_log_summary: str,
    all_transactions_summary: str,
) -> dict:
    """
    Вызывается раз в месяц.
    Глубокий анализ — только Pro.
    """
    messages = [
        {"role": "system", "content": MONTHLY_SYSTEM},
        {"role": "user", "content": json.dumps({
            "profile_summary":      compressed_profile,
            "nutrition_log":        nutrition_log_summary,
            "transactions_summary": all_transactions_summary,
            "schema":               MONTHLY_SCHEMA,
        }, ensure_ascii=False)},
    ]
    raw = await _call(DEEPSEEK_PRO, messages, TOKENS["ds_monthly"])
    return json.loads(_clean_json(raw))
