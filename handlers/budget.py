"""
handlers/budget.py

Статистика трат, прогресс по бюджету, месячный анализ.
"""

import json
from datetime import date, timedelta
from collections import defaultdict

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from database import (
    get_transactions, get_profile,
    get_food_transactions,
)
from ai.llama_layer import build_profile_for_deepseek
from ai.deepseek_layer import monthly_deep_analysis

router = Router()


def _progress_bar(current: float, target: float, width: int = 10) -> str:
    pct = min(current / target, 1.0) if target else 0
    filled = int(pct * width)
    bar = "━" * filled + "╌" * (width - filled)
    color = "🔴" if pct > 1.0 else "🟡" if pct > 0.8 else "🟢"
    return f"{color} {bar} {pct*100:.0f}%"


@router.message(F.text == "💰 Бюджет")
@router.message(Command("budget"))
async def cmd_budget(message: Message):
    user_id  = message.from_user.id
    profile  = await get_profile(user_id)
    if not profile:
        await message.answer("Сначала пройди настройку: /start")
        return

    transactions = await get_transactions(user_id, limit=200)
    target = profile.get("budget_target") or profile.get("budget_food") or 5000

    # Считаем траты за текущий месяц
    today     = date.today()
    month_str = today.strftime("%Y-%m")
    by_cat    = defaultdict(float)
    total_month = 0

    for tx in transactions:
        if tx["date"].startswith(month_str):
            by_cat[tx["category"]] += tx["amount"]
            total_month += tx["amount"]

    # Только еда
    food_cats = {"продукты", "доставка", "кафе", "еда"}
    food_total = sum(v for k, v in by_cat.items() if k in food_cats)

    bar = _progress_bar(food_total, target)
    days_passed = today.day
    days_total  = 30
    daily_avg   = food_total / days_passed if days_passed else 0
    forecast    = daily_avg * days_total

    lines = [
        f"💰 <b>Бюджет на еду — {today.strftime('%B %Y')}</b>\n",
        f"Потрачено: <b>{food_total:.0f}₽</b> из {target:.0f}₽",
        f"{bar}\n",
        f"Средний день: {daily_avg:.0f}₽",
        f"Прогноз на месяц: <b>{forecast:.0f}₽</b>",
    ]

    if forecast > target:
        overrun = forecast - target
        lines.append(f"⚠️ Превысишь цель на <b>{overrun:.0f}₽</b>")
    else:
        save = target - forecast
        lines.append(f"✅ Укладываешься, останется ~<b>{save:.0f}₽</b>")

    # Разбивка по категориям еды
    lines.append("\n<b>По категориям:</b>")
    for cat in ["продукты", "доставка", "кафе"]:
        amt = by_cat.get(cat, 0)
        if amt:
            lines.append(f"  {cat}: {amt:.0f}₽")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text == "📈 Статистика")
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id      = message.from_user.id
    transactions = await get_transactions(user_id, limit=500)
    profile      = await get_profile(user_id)

    if not transactions:
        await message.answer("Ещё нет данных. Загрузи выписку или добавь трату через /add")
        return

    # Группируем по месяцам
    by_month = defaultdict(lambda: defaultdict(float))
    for tx in transactions:
        month = tx["date"][:7]
        by_month[month][tx["category"]] += tx["amount"]

    food_cats = {"продукты", "доставка", "кафе", "еда"}
    lines = ["📈 <b>Статистика трат на еду</b>\n"]

    for month in sorted(by_month.keys())[-3:]:  # последние 3 месяца
        food = sum(v for k, v in by_month[month].items() if k in food_cats)
        total = sum(by_month[month].values())
        pct = food / total * 100 if total else 0
        lines.append(f"<b>{month}:</b> еда {food:.0f}₽ ({pct:.0f}% от всех трат)")
        for cat in ["продукты", "доставка", "кафе"]:
            amt = by_month[month].get(cat, 0)
            if amt:
                lines.append(f"  └ {cat}: {amt:.0f}₽")

    target = profile.get("budget_target") if profile else None
    if target:
        # Тренд
        months = sorted(by_month.keys())[-2:]
        if len(months) == 2:
            food_prev = sum(v for k, v in by_month[months[0]].items() if k in food_cats)
            food_curr = sum(v for k, v in by_month[months[1]].items() if k in food_cats)
            diff = food_curr - food_prev
            trend = f"📉 -{abs(diff):.0f}₽" if diff < 0 else f"📈 +{diff:.0f}₽"
            lines.append(f"\nТренд за 2 месяца: {trend}")
        lines.append(f"Цель: {target:.0f}₽/мес")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("monthly"))
async def cmd_monthly_analysis(message: Message):
    """Глубокий месячный анализ через DeepSeek Pro."""
    user_id = message.from_user.id
    profile = await get_profile(user_id)
    if not profile:
        await message.answer("Сначала пройди настройку: /start")
        return

    wait = await message.answer("⏳ DeepSeek Pro анализирует месяц...")
    try:
        transactions = await get_transactions(user_id, limit=500)
        food_tx      = await get_food_transactions(user_id)

        compressed = await build_profile_for_deepseek(
            mode="budget_analysis",
            transactions=transactions,
            user_profile=profile,
        )

        # Краткая сводка по питанию для DeepSeek
        nutrition_summary = f"Транзакций на еду за месяц: {len(food_tx)}"

        result = await monthly_deep_analysis(
            compressed_profile=compressed,
            nutrition_log_summary=nutrition_summary,
            all_transactions_summary=json.dumps(
                {t["category"]: t["amount"] for t in transactions[:50]},
                ensure_ascii=False
            ),
        )

        await wait.delete()

        # Показываем инсайты
        lines = ["🔍 <b>Месячный анализ</b>\n"]

        if result.get("patterns_found"):
            lines.append("<b>Паттерны:</b>")
            for p in result["patterns_found"]:
                lines.append(f"  • {p}")

        if result.get("budget_progress"):
            bp = result["budget_progress"]
            lines.append(f"\n<b>Бюджет:</b> план {bp.get('planned')}₽ → факт {bp.get('actual')}₽")
            lines.append(f"Основной перерасход: {bp.get('main_overrun', '—')}")

        if result.get("strategy_next_month"):
            lines.append(f"\n💡 <b>Стратегия на следующий месяц:</b>\n{result['strategy_next_month']}")

        await message.answer("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        await wait.edit_text(f"❌ Ошибка анализа: {e}")
