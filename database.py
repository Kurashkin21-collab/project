import aiosqlite
from config import DATABASE_PATH


async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Траты
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                date        TEXT NOT NULL,
                amount      REAL NOT NULL,
                category    TEXT,
                shop        TEXT,
                description TEXT,
                source      TEXT DEFAULT 'manual',  -- csv / push / receipt / manual
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        # Профиль пользователя
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                user_id         INTEGER PRIMARY KEY,
                -- Онбординг
                height          INTEGER,
                weight          REAL,
                age             INTEGER,
                goal            TEXT,       -- lose / maintain / gain
                budget_food     REAL,       -- текущий бюджет на еду
                budget_target   REAL,       -- целевой бюджет
                cooking_skill   TEXT,       -- basic / normal / good
                cook_time_min   INTEGER,    -- сколько минут готов тратить
                no_eat          TEXT,       -- что не ест (JSON список)
                delivery_days   TEXT,       -- дни доставки (JSON список)
                -- КБЖУ норма (считается из профиля)
                kbju_kcal       REAL,
                kbju_protein    REAL,
                kbju_fat        REAL,
                kbju_carbs      REAL,
                -- Мета
                onboarding_done INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        # Продукты с ценами и КБЖУ
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                name_normalized TEXT,       -- нормализованное для поиска
                store           TEXT,
                price_per_100g  REAL,
                kcal_100g       REAL,
                protein_100g    REAL,
                fat_100g        REAL,
                carbs_100g      REAL,
                score           REAL,       -- КБЖУ_индекс / цена × 100
                unit            TEXT,       -- г / мл / шт
                updated_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        # История цен
        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  INTEGER,
                price       REAL,
                store       TEXT,
                recorded_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Планы питания
        await db.execute("""
            CREATE TABLE IF NOT EXISTS meal_plans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                week_start  TEXT NOT NULL,
                plan_json   TEXT NOT NULL,  -- полный JSON от DeepSeek
                model_used  TEXT,           -- flash / pro
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        # Закупочные списки
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shopping_lists (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                plan_id     INTEGER,
                type        TEXT NOT NULL,  -- weekly / monthly
                items_json  TEXT NOT NULL,  -- JSON список
                bought      INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        # Лог питания
        await db.execute("""
            CREATE TABLE IF NOT EXISTS nutrition_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                date        TEXT NOT NULL,
                meal        TEXT,           -- breakfast / lunch / dinner / snack
                description TEXT,
                kcal        REAL,
                protein     REAL,
                fat         REAL,
                carbs       REAL,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()


# ── Transactions ──────────────────────────────────────────────────────────────

async def add_transaction(user_id, date, amount, category, shop, description, source="manual"):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO transactions (user_id, date, amount, category, shop, description, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, date, amount, category, shop, description, source))
        await db.commit()


async def add_transactions_bulk(user_id: int, rows: list[dict]):
    """Массовая вставка из CSV."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executemany("""
            INSERT INTO transactions (user_id, date, amount, category, shop, description, source)
            VALUES (:user_id, :date, :amount, :category, :shop, :description, :source)
        """, [{**r, "user_id": user_id} for r in rows])
        await db.commit()


async def get_transactions(user_id: int, limit: int = 500) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM transactions
            WHERE user_id = ?
            ORDER BY date DESC
            LIMIT ?
        """, (user_id, limit))
        return [dict(r) for r in await cur.fetchall()]


async def get_food_transactions(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM transactions
            WHERE user_id = ? AND category IN ('продукты', 'доставка', 'кафе', 'еда')
            ORDER BY date DESC
        """, (user_id,))
        return [dict(r) for r in await cur.fetchall()]


# ── User profile ──────────────────────────────────────────────────────────────

async def get_profile(user_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM user_profile WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def upsert_profile(user_id: int, **kwargs):
    profile = await get_profile(user_id)
    kwargs["updated_at"] = "datetime('now')"
    if not profile:
        kwargs["user_id"] = user_id
        cols = ", ".join(kwargs.keys())
        vals = ", ".join("?" for _ in kwargs)
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                f"INSERT INTO user_profile ({cols}) VALUES ({vals})",
                list(kwargs.values())
            )
            await db.commit()
    else:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                f"UPDATE user_profile SET {sets} WHERE user_id = ?",
                list(kwargs.values()) + [user_id]
            )
            await db.commit()


# ── Products ──────────────────────────────────────────────────────────────────

async def upsert_product(name, store, price_per_100g, kcal, protein, fat, carbs, score):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        existing = await (await db.execute(
            "SELECT id FROM products WHERE name_normalized = ? AND store = ?",
            (name.lower().strip(), store)
        )).fetchone()
        if existing:
            await db.execute("""
                UPDATE products SET
                    price_per_100g = ?, kcal_100g = ?, protein_100g = ?,
                    fat_100g = ?, carbs_100g = ?, score = ?,
                    updated_at = datetime('now')
                WHERE id = ?
            """, (price_per_100g, kcal, protein, fat, carbs, score, existing[0]))
            pid = existing[0]
        else:
            cur = await db.execute("""
                INSERT INTO products
                    (name, name_normalized, store, price_per_100g,
                     kcal_100g, protein_100g, fat_100g, carbs_100g, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, name.lower().strip(), store, price_per_100g, kcal, protein, fat, carbs, score))
            pid = cur.lastrowid
        # Пишем в историю цен
        await db.execute(
            "INSERT INTO price_history (product_id, price, store) VALUES (?, ?, ?)",
            (pid, price_per_100g, store)
        )
        await db.commit()
        return pid


async def get_products_by_name(name: str) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM products
            WHERE name_normalized LIKE ?
            ORDER BY score DESC
        """, (f"%{name.lower().strip()}%",))
        return [dict(r) for r in await cur.fetchall()]


# ── Meal plans ────────────────────────────────────────────────────────────────

async def save_meal_plan(user_id: int, week_start: str, plan_json: str, model: str) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("""
            INSERT INTO meal_plans (user_id, week_start, plan_json, model_used)
            VALUES (?, ?, ?, ?)
        """, (user_id, week_start, plan_json, model))
        await db.commit()
        return cur.lastrowid


async def get_last_meal_plan(user_id: int) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM meal_plans
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


# ── Shopping lists ────────────────────────────────────────────────────────────

async def save_shopping_list(user_id: int, plan_id: int, list_type: str, items_json: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO shopping_lists (user_id, plan_id, type, items_json)
            VALUES (?, ?, ?, ?)
        """, (user_id, plan_id, list_type, items_json))
        await db.commit()


async def get_shopping_list(user_id: int, list_type: str) -> dict | None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM shopping_lists
            WHERE user_id = ? AND type = ? AND bought = 0
            ORDER BY created_at DESC LIMIT 1
        """, (user_id, list_type))
        row = await cur.fetchone()
        return dict(row) if row else None


# ── Nutrition log ─────────────────────────────────────────────────────────────

async def add_nutrition_log(user_id, date, meal, description, kcal, protein, fat, carbs):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            INSERT INTO nutrition_log
                (user_id, date, meal, description, kcal, protein, fat, carbs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, date, meal, description, kcal, protein, fat, carbs))
        await db.commit()


async def get_nutrition_today(user_id: int, date: str) -> dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("""
            SELECT
                COALESCE(SUM(kcal), 0)    as kcal,
                COALESCE(SUM(protein), 0) as protein,
                COALESCE(SUM(fat), 0)     as fat,
                COALESCE(SUM(carbs), 0)   as carbs
            FROM nutrition_log
            WHERE user_id = ? AND date = ?
        """, (user_id, date))
        row = await cur.fetchone()
        return {"kcal": row[0], "protein": row[1], "fat": row[2], "carbs": row[3]}
