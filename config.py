import os
from zoneinfo import ZoneInfo

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")

# ── Groq (Лама) ───────────────────────────────────────────────────────────────
GROQ_API_KEY  = os.getenv("GROQ_API_KEY", "")
GROQ_URL      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL    = "meta-llama/llama-4-scout-17b-16e-instruct"

# ── DeepSeek ──────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_FLASH   = "deepseek-v4-flash"   # еженедельный пересчёт
DEEPSEEK_PRO     = "deepseek-v4-pro"     # онбординг + месячный анализ

# ── База данных ───────────────────────────────────────────────────────────────
DATABASE_PATH = os.getenv("DATABASE_PATH", "finbot.db")

# ── Настройки ─────────────────────────────────────────────────────────────────
TZ            = ZoneInfo("Europe/Moscow")

# Лимиты токенов на выход по режимам (max_tokens)
TOKENS = {
    "parse_csv":       3000,   # парсинг CSV выписки
    "build_profile":   1500,   # сборка профиля для DeepSeek
    "choose_product":  2000,   # выбор продукта по скору КБЖУ/цена
    "format_plan":     4000,   # форматирование плана для Telegram
    "parse_push":       500,   # парсинг пуша от банка
    "chat":            1500,   # обычный диалог
    "parse_receipt":   2000,   # парсинг фото чека
    # DeepSeek
    "ds_onboarding":   4000,   # первичный анализ при онбординге (Pro)
    "ds_weekly":       4000,   # еженедельный пересчёт (Flash)
    "ds_monthly":      4000,   # месячный глубокий анализ (Pro)
}

# Магазины для парсинга цен
STORES = [
    "pyaterochka",    # Пятёрочка
    "perekrestok",    # Перекрёсток
    "magnit",         # Магнит
]
