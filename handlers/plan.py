"""
handlers/plan.py

Работа с планом питания:
- Просмотр текущего плана
- Список покупок (скоропорт / долгохран)
- Ручной пересчёт плана
- Отметка купленных продуктов
"""

import json
from datetime import date

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from database import (
    get_last_meal_plan, get_shopping_list,
    get_profile, get_food_transactions,
    save_meal_plan, save_shopping_list,
    upsert_product,
)
from ai.llama_layer import (
    build_profile_for_deepseek,
    format_plan_for_telegram,
    choose_best_product,
)
from ai.deepseek_layer import weekly_update
from parsers.price_parser import search_products_batch

router = Router()


# ── Просмотр плана ────────────────────────────────────────────────────────────

@router.message(F.text == "📊 План питания")
@router.message(Command("plan"))
async def cmd_plan(message: Message):
    plan_row = await get_last_meal_plan(message.from_user.id)
    if not plan_row:
        await message.answer(
            "У тебя ещё нет плана. Он появится после онбординга.\n"
            "Напиши /start чтобы начать."
        )
        return

    plan = json.loads(plan_row["plan_json"])
    formatted = await format_plan_for_telegram(plan)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Пересчитать план", callback_data="plan:recalc"),
        InlineKeyboardButton(text="🛒 Список покупок",   callback_data="plan:shopping"),
    ]])

    for chunk in [formatted[i:i+4000] for i in range(0, len(formatted), 4000)]:
        await message.answer(chunk, parse_mode="HTML")

    await message.answer("Действия:", reply_markup=kb)


# ── Список покупок ────────────────────────────────────────────────────────────

@router.message(F.text == "🛒 Список покупок")
@router.message(Command("shopping"))
@router.callback_query(F.data == "plan:shopping")
async def cmd_shopping(event: Message | CallbackQuery):
    user_id = event.from_user.id
    msg = event.message if isinstance(event, CallbackQuery) else event

    weekly  = await get_shopping_list(user_id, "weekly")
    monthly = await get_shopping_list(user_id, "monthly")

    if not weekly and not monthly:
        await msg.answer("Список покупок пуст. Сначала построй план: /plan")
        return

    text_parts = []

    if weekly:
        items = json.loads(weekly["items_json"])
        lines = ["🗓 <b>На эту неделю (скоропорт):</b>\n"]
        total = 0
        for i, item in enumerate(items):
            done_mark = "✅" if item.get("bought") else "▫️"
            lines.append(
                f"{done_mark} {item['name']} {item.get('amount_g', '')}г "
                f"— <b>{item.get('total_price', '?')}₽</b> ({item.get('store', '')})"
            )
            total += item.get("total_price", 0) or 0
        lines.append(f"\n<b>Итого: {total:.0f}₽</b>")
        text_parts.append("\n".join(lines))

    if monthly:
        items = json.loads(monthly["items_json"])
        lines = ["\n📦 <b>На месяц (долгохран):</b>\n"]
        total = 0
        for item in items:
            done_mark = "✅" if item.get("bought") else "▫️"
            lines.append(
                f"{done_mark} {item['name']} {item.get('amount_g', '')}г "
                f"— <b>{item.get('total_price', '?')}₽</b>"
            )
            total += item.get("total_price", 0) or 0
        lines.append(f"\n<b>Итого: {total:.0f}₽</b>")
        text_parts.append("\n".join(lines))

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Всё купил", callback_data="shopping:done_all"),
    ]])

    await msg.answer("\n".join(text_parts), parse_mode="HTML", reply_markup=kb)
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data == "shopping:done_all")
async def cb_shopping_done(callback: CallbackQuery):
    await callback.message.edit_text("✅ Отлично! Всё куплено.", reply_markup=None)
    await callback.answer("Записано!")


# ── Пересчёт плана ────────────────────────────────────────────────────────────

@router.message(Command("recalc"))
@router.callback_query(F.data == "plan:recalc")
async def cmd_recalc(event: Message | CallbackQuery):
    user_id = event.from_user.id
    msg = event.message if isinstance(event, CallbackQuery) else event

    wait = await msg.answer("⏳ Пересчитываю план...")

    try:
        profile      = await get_profile(user_id)
        transactions = await get_food_transactions(user_id)
        prev_plan_row = await get_last_meal_plan(user_id)
        prev_plan    = json.loads(prev_plan_row["plan_json"]) if prev_plan_row else {}

        # Лама сжимает профиль в режиме plan_update
        compressed = await build_profile_for_deepseek(
            mode="plan_update",
            transactions=transactions,
            user_profile=profile,
            prev_plan=prev_plan,
        )

        # Ищем актуальные цены
        price_check = prev_plan.get("price_check_needed", [])
        products_with_prices = []
        if price_check:
            await wait.edit_text("🔍 Обновляю цены в магазинах...")
            found = await search_products_batch(price_check)
            for name, variants in found.items():
                if variants:
                    best = await choose_best_product(name, variants)
                    products_with_prices.append(best)
                    await upsert_product(
                        best["name"], best["store"],
                        best["price_per_100g"],
                        best.get("kcal_100g") or 0,
                        best.get("protein_100g") or 0,
                        best.get("fat_100g") or 0,
                        best.get("carbs_100g") or 0,
                        best.get("score") or 0,
                    )

        await wait.edit_text("🧠 DeepSeek строит новый план...")

        # DeepSeek Flash пересчитывает
        new_plan = await weekly_update(compressed, prev_plan, products_with_prices)

        # Сохраняем
        week_start = date.today().strftime("%Y-%m-%d")
        plan_id = await save_meal_plan(
            user_id, week_start,
            json.dumps(new_plan, ensure_ascii=False), "flash"
        )
        if new_plan.get("shopping_weekly"):
            await save_shopping_list(
                user_id, plan_id, "weekly",
                json.dumps(new_plan["shopping_weekly"], ensure_ascii=False)
            )
        if new_plan.get("shopping_monthly"):
            await save_shopping_list(
                user_id, plan_id, "monthly",
                json.dumps(new_plan["shopping_monthly"], ensure_ascii=False)
            )

        await wait.delete()
        formatted = await format_plan_for_telegram(new_plan)
        for chunk in [formatted[i:i+4000] for i in range(0, len(formatted), 4000)]:
            await msg.answer(chunk, parse_mode="HTML")

    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}")

    if isinstance(event, CallbackQuery):
        await event.answer()
