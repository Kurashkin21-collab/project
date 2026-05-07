"""
handlers/onboarding.py

Онбординг при первом запуске:
1. 5 вопросов о профиле
2. Загрузка CSV выписки Т-Банка
3. Первичный анализ через DeepSeek Pro
4. Готовый план
"""

import json
import io
from datetime import date

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove,
)

from database import (
    get_profile, upsert_profile, add_transactions_bulk,
    save_meal_plan, save_shopping_list, get_food_transactions,
)
from ai.llama_layer import (
    parse_csv, parse_pdf, build_profile_for_deepseek,
    format_plan_for_telegram, calculate_kbju,
)
from ai.deepseek_layer import analyze_onboarding
from parsers.price_parser import search_products_batch
from ai.llama_layer import choose_best_product
from database import upsert_product

router = Router()


class Onboarding(StatesGroup):
    goal          = State()
    budget_food   = State()
    budget_target = State()
    body          = State()   # рост/вес/возраст одним сообщением
    cooking       = State()
    no_eat        = State()
    delivery_days = State()
    upload_csv    = State()


MAIN_KB = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📊 План питания"), KeyboardButton(text="🛒 Список покупок")],
    [KeyboardButton(text="💰 Бюджет"),       KeyboardButton(text="📈 Статистика")],
    [KeyboardButton(text="🍽 Что съел"),     KeyboardButton(text="⚙️ Настройки")],
], resize_keyboard=True)

CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    profile = await get_profile(message.from_user.id)

    if profile and profile.get("onboarding_done"):
        await message.answer(
            f"Привет! Бот уже настроен. Что хочешь?",
            reply_markup=MAIN_KB
        )
        return

    await state.set_state(Onboarding.goal)
    await message.answer(
        "👋 Привет! Я помогу тебе контролировать траты на еду и питаться правильно.\n\n"
        "Настройка займёт 2 минуты. Начнём?\n\n"
        "<b>Какая у тебя цель?</b>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📉 Похудеть")],
            [KeyboardButton(text="⚖️ Держать вес")],
            [KeyboardButton(text="💪 Набрать массу")],
        ], resize_keyboard=True)
    )


@router.message(Onboarding.goal)
async def ob_goal(message: Message, state: FSMContext):
    goals = {"📉 Похудеть": "lose", "⚖️ Держать вес": "maintain", "💪 Набрать массу": "gain"}
    goal = goals.get(message.text, "maintain")
    await state.update_data(goal=goal)
    await state.set_state(Onboarding.budget_food)
    await message.answer(
        "💸 <b>Сколько сейчас тратишь на еду в месяц?</b>\n"
        "<i>Примерно, в рублях. Можешь написать диапазон типа 5000-7000</i>",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )


@router.message(Onboarding.budget_food)
async def ob_budget_food(message: Message, state: FSMContext):
    # Парсим число или диапазон
    import re
    nums = re.findall(r"\d+", message.text.replace(" ", ""))
    budget = int(nums[0]) if nums else 6000
    if len(nums) >= 2:
        budget = (int(nums[0]) + int(nums[1])) // 2
    await state.update_data(budget_food=budget)
    await state.set_state(Onboarding.budget_target)
    await message.answer(
        f"Понял, сейчас ~<b>{budget}₽/мес</b>.\n\n"
        "🎯 <b>Сколько хочешь тратить на еду в месяц?</b>",
        parse_mode="HTML"
    )


@router.message(Onboarding.budget_target)
async def ob_budget_target(message: Message, state: FSMContext):
    import re
    nums = re.findall(r"\d+", message.text.replace(" ", ""))
    target = int(nums[0]) if nums else 4500
    await state.update_data(budget_target=target)
    await state.set_state(Onboarding.body)
    await message.answer(
        "🏃 <b>Напиши рост, вес и возраст через пробел</b>\n"
        "<i>Например: 181 74 20</i>",
        parse_mode="HTML"
    )


@router.message(Onboarding.body)
async def ob_body(message: Message, state: FSMContext):
    import re
    nums = re.findall(r"\d+(?:[.,]\d+)?", message.text)
    if len(nums) < 3:
        await message.answer("Напиши три числа через пробел: рост вес возраст\n<i>Например: 181 74 20</i>", parse_mode="HTML")
        return
    height = int(nums[0])
    weight = float(nums[1].replace(",", "."))
    age    = int(nums[2])
    await state.update_data(height=height, weight=weight, age=age)
    await state.set_state(Onboarding.cooking)
    await message.answer(
        "👨‍🍳 <b>Как ты готовишь?</b>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="🟢 Базово — яичница и паста")],
            [KeyboardButton(text="🟡 Нормально — несколько блюд")],
            [KeyboardButton(text="🔵 Хорошо — готовлю регулярно")],
        ], resize_keyboard=True)
    )


@router.message(Onboarding.cooking)
async def ob_cooking(message: Message, state: FSMContext):
    skills = {
        "🟢 Базово — яичница и паста": "basic",
        "🟡 Нормально — несколько блюд": "normal",
        "🔵 Хорошо — готовлю регулярно": "good",
    }
    skill = skills.get(message.text, "normal")
    await state.update_data(cooking_skill=skill)
    await state.set_state(Onboarding.no_eat)
    await message.answer(
        "🚫 <b>Что не ешь?</b>\n"
        "<i>Напиши через запятую или — если всё ешь</i>",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )


@router.message(Onboarding.no_eat)
async def ob_no_eat(message: Message, state: FSMContext):
    no_eat = [] if message.text.strip() == "—" else [x.strip() for x in message.text.split(",")]
    await state.update_data(no_eat=json.dumps(no_eat, ensure_ascii=False))
    await state.set_state(Onboarding.delivery_days)
    await message.answer(
        "🛵 <b>В какие дни обычно заказываешь доставку?</b>\n"
        "<i>Например: пятница, суббота. Или — если не заказываешь</i>",
        parse_mode="HTML"
    )


@router.message(Onboarding.delivery_days)
async def ob_delivery_days(message: Message, state: FSMContext):
    days = [] if message.text.strip() == "—" else [x.strip() for x in message.text.split(",")]
    await state.update_data(delivery_days=json.dumps(days, ensure_ascii=False))
    await state.set_state(Onboarding.upload_csv)
    await message.answer(
        "📂 <b>Последний шаг — загрузи выписку из Т-Банка</b>\n\n"
        "Принимаю <b>PDF</b> и <b>CSV</b>:\n\n"
        "📱 <b>Приложение</b> → карта → Детали → Выписка → PDF\n"
        "💻 <b>Веб</b> (tbank.ru) → счёт → Выписка → CSV\n\n"
        "Это нужно чтобы бот понял твои паттерны трат и сразу построил точный план.\n\n"
        "<i>Если не хочешь — напиши <b>пропустить</b></i>",
        parse_mode="HTML"
    )


@router.message(Onboarding.upload_csv, F.document)
async def ob_csv_upload(message: Message, state: FSMContext):
    wait = await message.answer("⏳ Анализирую выписку...")
    fname = (message.document.file_name or "").lower()
    try:
        file = await message.bot.get_file(message.document.file_id)
        raw  = await message.bot.download_file(file.file_path)
        data = raw.read()

        if fname.endswith(".pdf"):
            # PDF — парсим через Groq Vision
            await wait.edit_text("⏳ Читаю PDF через AI (может занять до минуты)...")
            transactions = await parse_pdf(data)
        else:
            # CSV — парсим как текст
            csv_text = data.decode("utf-8", errors="ignore")
            transactions = await parse_csv(csv_text)

        await add_transactions_bulk(message.from_user.id, transactions)
        await wait.edit_text(f"✅ Загружено {len(transactions)} операций. Строю план...")
        await _finish_onboarding(message, state, len(transactions))

    except Exception as e:
        await wait.edit_text(
            f"❌ Ошибка при чтении файла: {e}\n"
            "Попробуй ещё раз или напиши <b>пропустить</b>",
            parse_mode="HTML"
        )


@router.message(Onboarding.upload_csv, F.text.lower().contains("пропустить"))
async def ob_skip_csv(message: Message, state: FSMContext):
    await message.answer("Хорошо, пропускаем. Строю план на основе твоего профиля...")
    await _finish_onboarding(message, state, 0)


async def _finish_onboarding(message: Message, state: FSMContext, tx_count: int):
    """Финальный шаг онбординга — сохраняем профиль и строим первый план."""
    data    = await state.get_data()
    user_id = message.from_user.id

    # Считаем КБЖУ норму
    kbju = calculate_kbju(
        data["height"], data["weight"], data["age"], data["goal"]
    )

    # Сохраняем профиль
    await upsert_profile(
        user_id,
        height        = data["height"],
        weight        = data["weight"],
        age           = data["age"],
        goal          = data["goal"],
        budget_food   = data["budget_food"],
        budget_target = data["budget_target"],
        cooking_skill = data["cooking_skill"],
        no_eat        = data["no_eat"],
        delivery_days = data["delivery_days"],
        kbju_kcal     = kbju["kcal"],
        kbju_protein  = kbju["protein"],
        kbju_fat      = kbju["fat"],
        kbju_carbs    = kbju["carbs"],
        onboarding_done = 1,
    )

    await state.clear()

    # Получаем транзакции и профиль
    profile      = await get_profile(user_id)
    transactions = await get_food_transactions(user_id)

    # Лама сжимает профиль для DeepSeek
    compressed = await build_profile_for_deepseek(
        mode="meal_plan",
        transactions=transactions,
        user_profile=profile,
    )

    # DeepSeek Pro строит первый план
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Отправляем профиль в DeepSeek Pro...")
    try:
        plan = await analyze_onboarding(compressed)
        logger.info(f"DeepSeek вернул план: {str(plan)[:200]}")
    except Exception as e:
        logger.error(f"Ошибка DeepSeek: {e}")
        await message.answer(
            f"❌ DeepSeek не смог построить план: {e}\n\n"
            "Попробуй /recalc позже или напиши /reset и пройди онбординг заново."
        )
        return

    # Ищем цены на нужные продукты
    if plan.get("price_check_needed"):
        wait_msg = await message.answer("🔍 Ищу актуальные цены в магазинах...")
        products_found = await search_products_batch(plan["price_check_needed"])

        # Лама выбирает лучший вариант по скору
        for product_name, variants in products_found.items():
            if variants:
                best = await choose_best_product(product_name, variants)
                await upsert_product(
                    best["name"], best["store"],
                    best["price_per_100g"],
                    best.get("kcal_100g") or 0,
                    best.get("protein_100g") or 0,
                    best.get("fat_100g") or 0,
                    best.get("carbs_100g") or 0,
                    best.get("score") or 0,
                )
        await wait_msg.delete()

    # Сохраняем план
    week_start = date.today().strftime("%Y-%m-%d")
    plan_id = await save_meal_plan(
        user_id, week_start, json.dumps(plan, ensure_ascii=False), "pro"
    )

    # Сохраняем списки покупок
    if plan.get("shopping_weekly"):
        await save_shopping_list(
            user_id, plan_id, "weekly",
            json.dumps(plan["shopping_weekly"], ensure_ascii=False)
        )
    if plan.get("shopping_monthly"):
        await save_shopping_list(
            user_id, plan_id, "monthly",
            json.dumps(plan["shopping_monthly"], ensure_ascii=False)
        )

    # Лама форматирует для Telegram
    formatted = await format_plan_for_telegram(plan)

    # Отправляем по кускам если длинный
    for chunk in [formatted[i:i+4000] for i in range(0, len(formatted), 4000)]:
        await message.answer(chunk, parse_mode="HTML")

    await message.answer(
        "✅ <b>Готово! Первый план построен.</b>\n\n"
        f"КБЖУ норма: <b>{kbju['kcal']} ккал</b> | "
        f"Б: {kbju['protein']}г | Ж: {kbju['fat']}г | У: {kbju['carbs']}г\n\n"
        "Что дальше:",
        parse_mode="HTML",
        reply_markup=MAIN_KB
    )

@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext):
    """Сброс онбординга — для повторного прохождения."""
    import aiosqlite
    from config import DATABASE_PATH
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE user_profile SET onboarding_done=0 WHERE user_id=?",
            (message.from_user.id,)
        )
        await db.commit()
    await state.clear()
    await message.answer(
        "🔄 Онбординг сброшен. Напиши /start чтобы начать заново."
    )
