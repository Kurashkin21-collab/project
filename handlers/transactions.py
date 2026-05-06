"""
handlers/transactions.py

Добавление трат тремя способами:
1. Пересланный пуш от Т-Банка
2. Фото чека
3. Текст в свободной форме ("кофе 200р")
"""

from datetime import date

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from database import add_transaction
from ai.llama_layer import parse_push, parse_receipt_photo, chat

router = Router()


# ── Пересланный пуш от банка ──────────────────────────────────────────────────

@router.message(F.forward_from | F.forward_from_chat)
async def handle_forwarded_push(message: Message):
    """Ловим пересланное сообщение — скорее всего пуш от банка."""
    text = message.text or message.caption or ""
    if not text:
        return

    # Проверяем что похоже на пуш (есть числа и упоминание оплаты)
    keywords = ["оплата", "списание", "покупка", "₽", "руб"]
    if not any(kw in text.lower() for kw in keywords):
        return

    try:
        result = await parse_push(text)
        if result.get("skip"):
            return  # пополнение, не трата

        await add_transaction(
            user_id=message.from_user.id,
            date=date.today().isoformat(),
            amount=result["amount"],
            category=result["category"],
            shop=result["shop"],
            description=result["description"],
            source="push",
        )
        await message.answer(
            f"✅ Записал: <b>{result['shop']}</b> — {result['amount']}₽\n"
            f"Категория: {result['category']}",
            parse_mode="HTML"
        )
    except Exception as e:
        pass  # молча пропускаем если не распознали


# ── Фото чека ─────────────────────────────────────────────────────────────────

@router.message(F.photo)
async def handle_receipt_photo(message: Message):
    """Фото чека — парсим позиции через Vision."""
    wait = await message.answer("🧾 Читаю чек...")
    try:
        # Берём фото максимального качества
        photo = message.photo[-1]
        file  = await message.bot.get_file(photo.file_id)
        raw   = await message.bot.download_file(file.file_path)
        image_bytes = raw.read()

        items = await parse_receipt_photo(image_bytes)
        if not items:
            await wait.edit_text("❌ Не смог прочитать чек. Попробуй чётче.")
            return

        # Считаем общую сумму и записываем как одну транзакцию
        total = sum(item["price"] * item.get("qty", 1) for item in items)
        items_str = ", ".join(f"{i['name']} ({i['price']}₽)" for i in items[:5])

        await add_transaction(
            user_id=message.from_user.id,
            date=date.today().isoformat(),
            amount=total,
            category="продукты",
            shop="магазин",
            description=items_str,
            source="receipt",
        )

        await wait.edit_text(
            f"✅ Чек записан!\n\n"
            f"Позиций: {len(items)}\n"
            f"Сумма: <b>{total:.0f}₽</b>\n\n"
            f"{items_str}{'...' if len(items) > 5 else ''}",
            parse_mode="HTML"
        )
    except Exception as e:
        await wait.edit_text(f"❌ Ошибка: {e}")


# ── Текстовый ввод траты ──────────────────────────────────────────────────────

@router.message(Command("add"))
async def cmd_add_expense(message: Message):
    """
    /add кофе 200р
    Или просто текст если бот в режиме ввода траты.
    """
    text = message.text.replace("/add", "").strip()
    if not text:
        await message.answer(
            "Напиши трату, например:\n"
            "<code>/add кофе 200р</code>\n"
            "<code>/add Пятёрочка 850₽</code>",
            parse_mode="HTML"
        )
        return

    try:
        result = await parse_push(text)
        if result.get("skip"):
            await message.answer("Не похоже на трату. Напиши иначе, например: /add кофе 200р")
            return

        await add_transaction(
            user_id=message.from_user.id,
            date=date.today().isoformat(),
            amount=result["amount"],
            category=result["category"],
            shop=result["shop"],
            description=result["description"],
            source="manual",
        )
        await message.answer(
            f"✅ <b>{result['shop']}</b> — {result['amount']}₽ ({result['category']})",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Не смог разобрать. Попробуй: /add кофе 200р")
