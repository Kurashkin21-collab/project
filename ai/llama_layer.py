"""
ai/llama_layer.py

Слой Ламы (Groq Llama 4 Scout).
Отвечает за:
  - Парсинг CSV выписки Т-Банка
  - Парсинг пересланных пушей от банка
  - Парсинг фото чеков и ценников (Vision)
  - Адаптивная сборка профиля для DeepSeek
  - Выбор лучшего продукта по скору КБЖУ/цена
  - Общение с пользователем
"""

import json
import base64
import httpx
from config import GROQ_API_KEY, GROQ_URL, GROQ_MODEL, TOKENS


def _headers():
    return {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


async def _call(messages: list, max_tokens: int, temperature: float = 0.1) -> str:
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(GROQ_URL, headers=_headers(), content=body)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _clean_json(raw: str) -> str:
    return raw.replace("```json", "").replace("```", "").strip()


# ── Парсинг CSV выписки Т-Банка ───────────────────────────────────────────────

PARSE_CSV_PROMPT = """Ты парсишь выписку банка Т-Банк (Тинькофф) из CSV.
Извлеки все расходные операции и верни ТОЛЬКО валидный JSON массив.

Каждый элемент:
{
  "date": "ГГГГ-ММ-ДД",
  "amount": число (всегда положительное),
  "shop": "название магазина/сервиса",
  "category": "одна из: продукты / доставка / кафе / транспорт / развлечения / одежда / здоровье / связь / другое",
  "description": "краткое описание"
}

Только расходы (не пополнения). Только JSON массив, никакого текста."""


async def parse_csv(csv_text: str) -> list[dict]:
    """Парсит CSV выписки и возвращает список транзакций."""
    messages = [
        {"role": "system", "content": PARSE_CSV_PROMPT},
        {"role": "user", "content": csv_text[:20000]},  # обрезаем на всякий случай
    ]
    raw = await _call(messages, TOKENS["parse_csv"])
    return json.loads(_clean_json(raw))


# ── Парсинг пуша от банка ─────────────────────────────────────────────────────

PARSE_PUSH_PROMPT = """Извлеки из текста пуш-уведомления банка информацию о трате.
Верни ТОЛЬКО JSON объект:
{
  "amount": число,
  "shop": "название",
  "category": "продукты / доставка / кафе / транспорт / развлечения / одежда / здоровье / связь / другое",
  "description": "краткое описание"
}
Если это не трата а пополнение — верни {"skip": true}.
Только JSON, никакого текста."""


async def parse_push(push_text: str) -> dict:
    messages = [
        {"role": "system", "content": PARSE_PUSH_PROMPT},
        {"role": "user", "content": push_text},
    ]
    raw = await _call(messages, TOKENS["parse_push"])
    return json.loads(_clean_json(raw))


# ── Парсинг фото чека (Vision) ────────────────────────────────────────────────

PARSE_RECEIPT_PROMPT = """Ты читаешь фото чека из магазина.
Извлеки все купленные позиции и верни ТОЛЬКО JSON массив:
[
  {
    "name": "название товара",
    "amount_g": число или null,   // вес в граммах если указан
    "price": число,               // цена за единицу
    "qty": число                  // количество
  }
]
Итоговую сумму не включай. Только позиции. Только JSON."""


async def parse_receipt_photo(image_bytes: bytes, mime: str = "image/jpeg") -> list[dict]:
    b64 = base64.b64encode(image_bytes).decode()
    messages = [
        {"role": "system", "content": PARSE_RECEIPT_PROMPT},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": "Прочитай чек и верни JSON."},
        ]},
    ]
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": TOKENS["parse_receipt"],
        "temperature": 0.1,
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(GROQ_URL, headers=_headers(), content=body)
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
    return json.loads(_clean_json(raw))


# ── Парсинг фото ценника (Vision) ─────────────────────────────────────────────

PARSE_PRICE_TAG_PROMPT = """Ты читаешь фото ценника в магазине.
Извлеки информацию и верни ТОЛЬКО JSON:
{
  "name": "название товара",
  "price": число,
  "unit": "г / кг / мл / л / шт",
  "amount": число,    // количество единиц (например 900 если 900г)
  "store": "название магазина если видно иначе null"
}
Только JSON, никакого текста."""


async def parse_price_tag(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    messages = [
        {"role": "system", "content": PARSE_PRICE_TAG_PROMPT},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": "Прочитай ценник."},
        ]},
    ]
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": 500,
        "temperature": 0.1,
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(GROQ_URL, headers=_headers(), content=body)
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
    return json.loads(_clean_json(raw))


# ── Адаптивная сборка профиля для DeepSeek ────────────────────────────────────

PROFILE_PROMPTS = {
    "meal_plan": """Ты сжимаешь данные пользователя для передачи в модель планирования питания.
Верни текстовое резюме до {max_tokens} токенов.
ДЕТАЛЬНО включи: пищевое поведение, режим готовки, паттерны трат на еду, дни доставки, пропуски приёмов пищи, бюджет и цель, КБЖУ норму, предпочтения в еде.
КРАТКО (одной строкой): остальные категории трат суммарно.
Не включай: {ignore}""",

    "budget_analysis": """Ты сжимаешь данные пользователя для анализа бюджета.
Верни текстовое резюме до {max_tokens} токенов.
ДЕТАЛЬНО включи: все категории трат с суммами и динамикой, тренды по месяцам, аномалии.
КРАТКО: еда только итогом и трендом.
Не включай: {ignore}""",

    "plan_update": """Ты сжимаешь ТОЛЬКО изменения за последнюю неделю относительно предыдущего плана.
Верни текст до {max_tokens} токенов.
Включи только дельту: что не купили, что изменилось в тратах, новые паттерны.
НЕ пересылай весь профиль — только изменения.""",
}

PROFILE_TOKENS = {
    "meal_plan":      1500,
    "budget_analysis": 2000,
    "plan_update":     800,
}

PROFILE_IGNORE = {
    "meal_plan":      "транспорт, развлечения, одежда, связь",
    "budget_analysis": "детали блюд, предпочтения в еде",
    "plan_update":    "",
}


async def build_profile_for_deepseek(
    mode: str,
    transactions: list[dict],
    user_profile: dict,
    prev_plan: dict | None = None,
) -> str:
    """Адаптивно сжимает профиль под нужный режим DeepSeek."""
    max_tok = PROFILE_TOKENS.get(mode, 1500)
    ignore  = PROFILE_IGNORE.get(mode, "")

    system = PROFILE_PROMPTS.get(mode, PROFILE_PROMPTS["meal_plan"]).format(
        max_tokens=max_tok,
        ignore=ignore,
    )

    # Формируем данные для анализа
    data = {
        "profile":       user_profile,
        "transactions":  transactions[-200:],  # последние 200 трат
        "prev_plan":     prev_plan,
    }

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False, default=str)},
    ]
    return await _call(messages, max_tok, temperature=0.2)


# ── Выбор лучшего продукта по скору КБЖУ/цена ────────────────────────────────

CHOOSE_PRODUCT_PROMPT = """Ты выбираешь лучший вариант продукта из списка.

Скор = (КБЖУ_индекс / цена_за_100г) × 100
Для белковых продуктов КБЖУ_индекс = белок на 100г
Для углеводных = (белок × 1.5 + клетчатка) / калории × 100

Правило выбора: побеждает максимальный скор.
Цена и КБЖУ равноценны — скор сам находит баланс.
Если скор одинаковый — предпочитай более короткий состав.

Верни ТОЛЬКО JSON:
{
  "chosen_index": число (индекс в массиве),
  "score": число,
  "reason": "краткое объяснение на русском (1-2 предложения)"
}"""


async def choose_best_product(product_name: str, variants: list[dict]) -> dict:
    """Выбирает лучший вариант продукта по скору КБЖУ/цена."""
    messages = [
        {"role": "system", "content": CHOOSE_PRODUCT_PROMPT},
        {"role": "user", "content": json.dumps({
            "product": product_name,
            "variants": variants,
        }, ensure_ascii=False)},
    ]
    raw = await _call(messages, TOKENS["choose_product"])
    result = json.loads(_clean_json(raw))
    idx = result["chosen_index"]
    return {
        **variants[idx],
        "score":  result["score"],
        "reason": result["reason"],
    }


# ── Форматирование плана для Telegram ─────────────────────────────────────────

FORMAT_PLAN_PROMPT = """Ты форматируешь план питания для отправки в Telegram.
Используй HTML разметку: <b>жирный</b>, <i>курсив</i>.
Сделай красиво, читаемо, с эмодзи.
Разбей на секции: меню на неделю, список покупок (скоропорт / долгохран), бюджет.
Для каждого блюда указывай КБЖУ.
Бюджет показывай как "было X₽ → станет Y₽, экономия Z₽".
Не используй Markdown, только HTML теги."""


async def format_plan_for_telegram(plan_json: dict) -> str:
    messages = [
        {"role": "system", "content": FORMAT_PLAN_PROMPT},
        {"role": "user", "content": json.dumps(plan_json, ensure_ascii=False)},
    ]
    return await _call(messages, TOKENS["format_plan"], temperature=0.3)


# ── Общение с пользователем ───────────────────────────────────────────────────

CHAT_SYSTEM = """Ты персональный финансовый и нутриционный ассистент.
Помогаешь пользователю контролировать траты на еду и питаться правильно в рамках бюджета.
Отвечай по-русски, дружелюбно, конкретно. Без воды.
Если вопрос не по теме еды/финансов — мягко возвращай к теме."""


async def chat(user_message: str, history: list[dict] | None = None) -> str:
    messages = [{"role": "system", "content": CHAT_SYSTEM}]
    if history:
        messages.extend(history[-6:])  # последние 3 обмена
    messages.append({"role": "user", "content": user_message})
    return await _call(messages, TOKENS["chat"], temperature=0.5)


# ── Расчёт КБЖУ нормы по формуле Миффлина ────────────────────────────────────

def calculate_kbju(height: int, weight: float, age: int, goal: str) -> dict:
    """Считает норму КБЖУ по формуле Миффлина-Сан Жеора."""
    # Базовый обмен (для мужчин — адаптируем под универсальный)
    bmr = 10 * weight + 6.25 * height - 5 * age + 5
    # Коэффициент активности — умеренный (студент)
    tdee = bmr * 1.55

    if goal == "lose":
        kcal = tdee - 400
    elif goal == "gain":
        kcal = tdee + 300
    else:
        kcal = tdee

    protein = weight * 1.8          # 1.8г на кг
    fat     = kcal * 0.25 / 9       # 25% калорий из жиров
    carbs   = (kcal - protein * 4 - fat * 9) / 4

    return {
        "kcal":    round(kcal),
        "protein": round(protein),
        "fat":     round(fat),
        "carbs":   round(carbs),
    }
