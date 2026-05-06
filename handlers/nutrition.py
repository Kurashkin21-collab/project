"""
handlers/nutrition.py

Лог питания — что съел, КБЖУ за день.
"""

from datetime import date

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from database import add_nutrition_log, get_nutrition_today, get_profile
from ai.llama_layer import chat

router = Router()

# Простая база КБЖУ на 100г для быстрого расчёта
QUICK_KBJU = {
    "греча":          {"kcal": 313, "protein": 12.6, "fat": 3.3,  "carbs": 57.1},
    "рис":            {"kcal": 344, "protein": 6.7,  "fat": 0.7,  "carbs": 78.9},
    "макароны":       {"kcal": 338, "protein": 10.4, "fat": 1.1,  "carbs": 69.7},
    "куриное филе":   {"kcal": 113, "protein": 23.6, "fat": 1.9,  "carbs": 0.0},
    "яйцо":           {"kcal": 157, "protein": 12.7, "fat": 11.5, "carbs": 0.7},
    "творог":         {"kcal": 101, "protein": 16.7, "fat": 1.8,  "carbs": 1.3},
    "молоко":         {"kcal": 61,  "protein": 3.2,  "fat": 3.6,  "carbs": 4.8},
    "хлеб":           {"kcal": 242, "protein": 8.1,  "fat": 1.0,  "carbs": 48.8},
    "картофель":      {"kcal": 77,  "protein": 2.0,  "fat": 0.4,  "carbs": 16.3},
}

PARSE_MEAL_PROMPT = """Пользователь написал что съел. Оцени КБЖУ.
Верни JSON:
{
  "description": "что съел (кратко)",
  "meal": "breakfast / lunch / dinner / snack",
  "kcal": число,
  "protein": число,
  "fat": число,
  "carbs": число
}
Если порция не указана — бери среднюю порцию.
Только JSON."""


async def _estimate_kbju_llama(food_text: str) -> dict:
    """Оценивает КБЖУ через Ламу."""
    from ai.llama_layer import _call, TOKENS
    import json

    messages = [
        {"role": "system", "content": PARSE_MEAL_PROMPT},
        {"role": "user", "content": food_text},
    ]
    raw = await _call(messages, 500, temperature=0.1)
    return json.loads(raw.replace("```json", "").replace("```", "").strip())


@router.message(F.text == "🍽 Что съел")
@router.message(Command("ate"))
async def cmd_ate_help(message: Message):
    await message.answer(
        "Напиши что съел, например:\n\n"
        "<code>/ate тарелка гречи с курицей</code>\n"
        "<code>/ate 2 яйца и кофе с молоком</code>\n"
        "<code>/ate творог 200г</code>",
        parse_mode="HTML"
    )


@router.message(Command("ate"))
async def cmd_log_meal(message: Message):
    text = message.text.replace("/ate", "").strip()
    if not text:
        await cmd_ate_help(message)
        return

    wait = await message.answer("⏳ Считаю КБЖУ...")
    try:
        kbju = await _estimate_kbju_llama(text)

        await add_nutrition_log(
            user_id=message.from_user.id,
            date=date.today().isoformat(),
            meal=kbju.get("meal", "snack"),
            description=kbju.get("description", text),
            kcal=kbju.get("kcal", 0),
            protein=kbju.get("protein", 0),
            fat=kbju.get("fat", 0),
            carbs=kbju.get("carbs", 0),
        )

        # Показываем итог за день
        today_total = await get_nutrition_today(message.from_user.id, date.today().isoformat())
        profile     = await get_profile(message.from_user.id)
        target_kcal = profile.get("kbju_kcal", 2000) if profile else 2000

        pct     = today_total["kcal"] / target_kcal * 100 if target_kcal else 0
        filled  = min(int(pct / 10), 10)
        bar     = "━" * filled + "╌" * (10 - filled)
        color   = "🔴" if pct > 110 else "🟡" if pct > 90 else "🟢"

        await wait.edit_text(
            f"✅ Записал: <b>{kbju.get('description', text)}</b>\n"
            f"  {kbju.get('kcal', 0):.0f} ккал | "
            f"Б: {kbju.get('protein', 0):.0f}г | "
            f"Ж: {kbju.get('fat', 0):.0f}г | "
            f"У: {kbju.get('carbs', 0):.0f}г\n\n"
            f"📊 <b>За сегодня:</b>\n"
            f"{color} {bar} {pct:.0f}%\n"
            f"{today_total['kcal']:.0f} / {target_kcal:.0f} ккал\n"
            f"Белок: {today_total['protein']:.0f}г",
            parse_mode="HTML"
        )
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}")


@router.message(Command("kbju"))
async def cmd_kbju_today(message: Message):
    """КБЖУ за сегодня."""
    user_id  = message.from_user.id
    today    = await get_nutrition_today(user_id, date.today().isoformat())
    profile  = await get_profile(user_id)

    if not profile:
        await message.answer("Сначала пройди настройку: /start")
        return

    targets = {
        "kcal":    profile.get("kbju_kcal", 2000),
        "protein": profile.get("kbju_protein", 150),
        "fat":     profile.get("kbju_fat", 60),
        "carbs":   profile.get("kbju_carbs", 200),
    }

    def bar(cur, tgt):
        pct = min(cur / tgt, 1.2) if tgt else 0
        filled = min(int(pct * 8), 8)
        return "━" * filled + "╌" * (8 - filled)

    lines = [
        f"🍽 <b>КБЖУ за {date.today().strftime('%d.%m')}</b>\n",
        f"Калории: {today['kcal']:.0f} / {targets['kcal']:.0f} ккал",
        f"{bar(today['kcal'], targets['kcal'])}",
        f"\nБелок:  {today['protein']:.0f} / {targets['protein']:.0f} г  {bar(today['protein'], targets['protein'])}",
        f"Жиры:   {today['fat']:.0f} / {targets['fat']:.0f} г  {bar(today['fat'], targets['fat'])}",
        f"Углев.: {today['carbs']:.0f} / {targets['carbs']:.0f} г  {bar(today['carbs'], targets['carbs'])}",
    ]

    gap_kcal = targets["kcal"] - today["kcal"]
    if gap_kcal > 0:
        lines.append(f"\nОсталось: <b>{gap_kcal:.0f} ккал</b>")
    else:
        lines.append(f"\nПревышение: <b>{abs(gap_kcal):.0f} ккал</b> ⚠️")

    await message.answer("\n".join(lines), parse_mode="HTML")
