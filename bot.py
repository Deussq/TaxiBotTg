import asyncio
import logging
import math
import os
import sqlite3
from contextlib import contextmanager
from html import escape

import httpx
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TOKEN = os.getenv("TOKEN", "").strip()
DB_FILE = os.getenv("DB_FILE", "bot.db")
CURRENCY = os.getenv("CURRENCY", "€").strip() or "€"
ROUTING_URL = os.getenv(
    "ROUTING_URL",
    "https://router.project-osrm.org",
).rstrip("/")

START_PRICE = float(os.getenv("START_PRICE", "3"))
PRICE_PER_KM = float(os.getenv("PRICE_PER_KM", "1.2"))

CITIES_RAW = os.getenv(
    "CITIES",
    "Плауэн,Дрезден,Лейпциг,Берлин",
)
CITIES = [city.strip() for city in CITIES_RAW.split(",") if city.strip()]

if not CITIES:
    CITIES = ["Плауэн", "Дрезден", "Лейпциг", "Берлин"]

DEFAULT_CITY_RAW = os.getenv("CITY_NAME", "Плауэн").strip() or "Плауэн"

ADMIN_IDS = set()
for item in os.getenv("ADMIN_IDS", "").split(","):
    item = item.strip()
    if item:
        try:
            ADMIN_IDS.add(int(item))
        except ValueError:
            raise RuntimeError(f"Неверный Telegram ID администратора: {item}")

LEGACY_DRIVERS = set()
for item in os.getenv("DRIVERS", "").split(","):
    item = item.strip()
    if item:
        try:
            LEGACY_DRIVERS.add(int(item))
        except ValueError:
            raise RuntimeError(f"Неверный Telegram ID водителя: {item}")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("taxi_bot")

ACTIVE_STATUSES = (
    "awaiting_pickup",
    "awaiting_destination",
    "new",
    "accepted",
    "on_way",
    "arrived",
)

ACTIVE_SQL = ", ".join("?" for _ in ACTIVE_STATUSES)


def normalize_city(value):
    value = (value or "").strip()
    for city in CITIES:
        if city.casefold() == value.casefold():
            return city
    return value


CITY_NAME = normalize_city(DEFAULT_CITY_RAW)
if CITY_NAME not in CITIES:
    CITIES.insert(0, CITY_NAME)


if not TOKEN:
    raise RuntimeError("Не указан TOKEN в файле .env")

if START_PRICE < 0 or PRICE_PER_KM < 0:
    raise RuntimeError("Тарифы не могут быть отрицательными")


@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 20000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_columns(conn, table_name):
    return {
        row["name"]
        for row in conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def add_missing_columns(conn, table_name, columns):
    existing = table_columns(conn, table_name)
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {name} {column_type}"
            )


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                telegram_id INTEGER PRIMARY KEY,
                phone TEXT,
                city TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                driver_id INTEGER,
                pickup_lat REAL,
                pickup_lon REAL,
                destination_lat REAL,
                destination_lon REAL,
                distance_km REAL,
                price REAL,
                eta TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


        conn.execute("""
            CREATE TABLE IF NOT EXISTS drivers (
                telegram_id INTEGER PRIMARY KEY,
                phone TEXT,
                city TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS city_tariffs (
                city TEXT PRIMARY KEY,
                start_price REAL NOT NULL DEFAULT 3,
                price_per_km REAL NOT NULL DEFAULT 1.2,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        add_missing_columns(conn, "clients", {
            "phone": "TEXT",
            "city": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })

        add_missing_columns(conn, "orders", {
            "client_id": "INTEGER",
            "phone": "TEXT",
            "city": "TEXT",
            "status": "TEXT",
            "driver_id": "INTEGER",
            "pickup_lat": "REAL",
            "pickup_lon": "REAL",
            "destination_lat": "REAL",
            "destination_lon": "REAL",
            "distance_km": "REAL",
            "price": "REAL",
            "eta": "TEXT",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        })


        conn.execute("""
            UPDATE clients SET city = ?
            WHERE city IS NULL OR TRIM(city) = ''
        """, (CITY_NAME,))

        conn.execute("""
            UPDATE orders SET city = ?
            WHERE city IS NULL OR TRIM(city) = ''
        """, (CITY_NAME,))

        conn.execute("""
            UPDATE clients SET created_at = CURRENT_TIMESTAMP
            WHERE created_at IS NULL OR TRIM(created_at) = ''
        """)

        conn.execute("""
            UPDATE clients SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP)
            WHERE updated_at IS NULL OR TRIM(updated_at) = ''
        """)

        conn.execute("""
            UPDATE orders SET created_at = CURRENT_TIMESTAMP
            WHERE created_at IS NULL OR TRIM(created_at) = ''
        """)

        conn.execute("""
            UPDATE orders SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP)
            WHERE updated_at IS NULL OR TRIM(updated_at) = ''
        """)

        conn.execute("""
            UPDATE orders SET status = 'cancelled'
            WHERE status IS NULL OR TRIM(status) = ''
        """)

        for city in CITIES:
            conn.execute("""
                INSERT OR IGNORE INTO city_tariffs
                    (city, start_price, price_per_km)
                VALUES (?, ?, ?)
            """, (city, START_PRICE, PRICE_PER_KM))


        for driver_id in LEGACY_DRIVERS:
            conn.execute("""
                INSERT OR IGNORE INTO drivers
                    (telegram_id, city, status)
                VALUES (?, ?, 'approved')
            """, (driver_id, CITY_NAME))

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_client_status
            ON orders(client_id, status, id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_driver_status
            ON orders(driver_id, status, id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_status
            ON orders(status, id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_city_status
            ON orders(city, status, id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_drivers_city_status
            ON drivers(city, status)
        """)

    logger.info("База данных готова: %s", DB_FILE)

    if not ADMIN_IDS:
        logger.warning(
            "ADMIN_IDS не задан: одобрять новых водителей будет некому!"
        )


def get_client(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE telegram_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def ensure_client(user_id):
    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO clients
                (telegram_id, phone, city, created_at, updated_at)
            VALUES (?, NULL, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (user_id,))


def save_client(user_id, phone=None, city=None):
    ensure_client(user_id)
    old = get_client(user_id)

    new_phone = phone if phone is not None else old.get("phone")
    new_city = normalize_city(city) if city is not None else old.get("city", "")

    with db() as conn:
        conn.execute("""
            UPDATE clients
            SET phone = ?, city = ?, updated_at = CURRENT_TIMESTAMP
            WHERE telegram_id = ?
        """, (new_phone, new_city, user_id))

    return get_client(user_id)


def get_driver(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM drivers WHERE telegram_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def save_driver(user_id, phone=None, city=None, status=None):
    old = get_driver(user_id) or {}

    new_phone = phone if phone is not None else old.get("phone")
    new_city = normalize_city(city) if city is not None else old.get("city", "")
    new_status = status if status is not None else old.get("status", "pending")

    with db() as conn:
        conn.execute("""
            INSERT INTO drivers (
                telegram_id, phone, city, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_id) DO UPDATE SET
                phone = excluded.phone,
                city = excluded.city,
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, new_phone, new_city, new_status))

    return get_driver(user_id)


def get_tariff(city):
    city = normalize_city(city)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM city_tariffs WHERE city = ?",
            (city,),
        ).fetchone()

    if row:
        return float(row["start_price"]), float(row["price_per_km"])

    return START_PRICE, PRICE_PER_KM


def set_tariff(city, start_price, price_per_km):
    city = normalize_city(city)
    with db() as conn:
        conn.execute("""
            INSERT INTO city_tariffs
                (city, start_price, price_per_km, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(city) DO UPDATE SET
                start_price = excluded.start_price,
                price_per_km = excluded.price_per_km,
                updated_at = CURRENT_TIMESTAMP
        """, (city, start_price, price_per_km))


def get_active_order(user_id):
    with db() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM orders
            WHERE client_id = ?
            AND status IN ({ACTIVE_SQL})
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, *ACTIVE_STATUSES),
        ).fetchone()
        return dict(row) if row else None


def get_order(order_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        return dict(row) if row else None


def create_order(user_id, phone):
    client = get_client(user_id)
    if not client or not client.get("city"):
        return None

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")

        active = conn.execute(
            f"""
            SELECT id FROM orders
            WHERE client_id = ?
            AND status IN ({ACTIVE_SQL})
            LIMIT 1
            """,
            (user_id, *ACTIVE_STATUSES),
        ).fetchone()

        if active:
            return None

        cursor = conn.execute("""
            INSERT INTO orders (
                client_id, phone, city, status, created_at, updated_at
            )
            VALUES (?, ?, ?, 'awaiting_pickup',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (user_id, phone, client["city"]))

        return cursor.lastrowid


def update_order_if_status(
    order_id, statuses, updates, driver_id=None
):
    if not statuses or not updates:
        return False

    assignments = [f"{column} = ?" for column in updates]
    assignments.append("updated_at = CURRENT_TIMESTAMP")
    params = list(updates.values())

    where = [
        "id = ?",
        f"status IN ({', '.join('?' for _ in statuses)})",
    ]
    params.extend([order_id, *statuses])

    if driver_id is not None:
        where.append("driver_id = ?")
        params.append(driver_id)

    with db() as conn:
        cursor = conn.execute(
            f"""
            UPDATE orders
            SET {', '.join(assignments)}
            WHERE {' AND '.join(where)}
            """,
            params,
        )
        return cursor.rowcount == 1


def price_for_distance(distance_km, city):
    start_price, price_per_km = get_tariff(city)
    charged_km = math.ceil(max(0, distance_km) * 10) / 10
    return round(start_price + charged_km * price_per_km, 2)


async def route_distance_km(lat1, lon1, lat2, lon2):
    url = (
        f"{ROUTING_URL}/route/v1/driving/"
        f"{lon1},{lat1};{lon2},{lat2}"
    )
    params = {
        "overview": "false",
        "alternatives": "false",
        "steps": "false",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError("Маршрут не найден")

    distance = data["routes"][0].get("distance")
    if distance is None or distance < 0:
        raise ValueError("Сервис вернул некорректное расстояние")

    return distance / 1000


def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🚕 Заказать такси")],
            [
                KeyboardButton("🏙 Мой город"),
                KeyboardButton("📱 Мой телефон"),
            ],
            [
                KeyboardButton("⚙️ Настройки"),
                KeyboardButton("❌ Отменить заказ"),
            ],
            [KeyboardButton("ℹ️ Помощь")],
        ],
        resize_keyboard=True,
    )


def phone_menu():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📱 Отправить мой номер",
                    request_contact=True,
                )
            ],
            [KeyboardButton("⬅️ В меню")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def location_menu(button_text):
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(button_text, request_location=True)],
            [KeyboardButton("❌ Отменить заказ")],
            [KeyboardButton("⬅️ В меню")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def city_keyboard(mode):
    rows = []
    row = []

    for city in CITIES:
        row.append(
            InlineKeyboardButton(
                city,
                callback_data=f"city|{mode}|{city}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    return InlineKeyboardMarkup(rows)


def driver_keyboard(order_id, status):
    if status == "new":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "✅ Принять",
                callback_data=f"accept_{order_id}",
            ),
            InlineKeyboardButton(
                "❌ Отказаться",
                callback_data=f"reject_{order_id}",
            ),
        ]])

    rows = []


    if status in ("accepted", "on_way"):
        rows.append([
            InlineKeyboardButton("⏱ 3 мин", callback_data=f"eta_3_{order_id}"),
            InlineKeyboardButton("⏱ 5 мин", callback_data=f"eta_5_{order_id}"),
            InlineKeyboardButton("⏱ 10 мин", callback_data=f"eta_10_{order_id}"),
            InlineKeyboardButton("⏱ 15+ мин", callback_data=f"eta_15+_{order_id}"),
        ])

    if status == "accepted":
        rows.append([
            InlineKeyboardButton(
                "🚕 Выехал к клиенту",
                callback_data=f"depart_{order_id}",
            )
        ])


    if status == "on_way":
        rows.append([
            InlineKeyboardButton(
                "📍 Машина на месте",
                callback_data=f"arrived_{order_id}",
            )
        ])


    if status == "arrived":
        rows.append([
            InlineKeyboardButton(
                "🏁 Завершить поездку",
                callback_data=f"finish_{order_id}",
            )
        ])

    if status in ("accepted", "on_way", "arrived"):
        rows.append([
            InlineKeyboardButton(
                "❌ Отменить поездку",
                callback_data=f"driver_cancel_{order_id}",
            )
        ])

    return InlineKeyboardMarkup(rows) if rows else None


def maps_url(lat, lon):
    return (
        "https://www.google.com/maps/search/?api=1"
        f"&query={lat},{lon}"
    )

def driver_order_text(order):
    pickup = "Не указано"
    destination = "Не указано"

    if order.get("pickup_lat") is not None and order.get("pickup_lon") is not None:
        pickup = (
            f'<a href="{maps_url(order["pickup_lat"], order["pickup_lon"])}">'
            "Открыть место подачи на карте</a>"
        )

    if (
        order.get("destination_lat") is not None
        and order.get("destination_lon") is not None
    ):
        destination = (
            f'<a href="{maps_url(order["destination_lat"], order["destination_lon"])}">'
            "Открыть пункт назначения на карте</a>"
        )

    distance = order.get("distance_km")
    price = order.get("price")

    distance_text = f"{distance:.1f} км" if distance is not None else "Не рассчитано"
    price_text = f"{price:.2f} {escape(CURRENCY)}" if price is not None else "Не рассчитана"

    return (
        f"🚕 <b>Заказ №{order['id']}</b>\n\n"
        f"🏙 Город: {escape(str(order.get('city') or CITY_NAME))}\n"
        f"📍 Подача: {pickup}\n"
        f"🏁 Назначение: {destination}\n"
        f"📱 Телефон клиента: <code>{escape(str(order.get('phone') or 'не указан'))}</code>\n"
        f"📏 Расстояние: {distance_text}\n"
        f"💶 Стоимость: {price_text}\n"
    )


def approved_drivers_for_city(city):
    with db() as conn:
        rows = conn.execute("""
            SELECT telegram_id FROM drivers
            WHERE city = ? AND status = 'approved'
        """, (normalize_city(city),)).fetchall()
        return [row["telegram_id"] for row in rows]


async def notify_drivers(context, order):
    text = driver_order_text(order)
    delivered = 0

    for driver_id in approved_drivers_for_city(order["city"]):
        try:
            await context.bot.send_message(
                chat_id=driver_id,
                text=text,
                parse_mode="HTML",
                reply_markup=driver_keyboard(order["id"], "new"),
                disable_web_page_preview=True,
            )
            delivered += 1
        except Exception:
            logger.exception(
                "Не удалось отправить заказ №%s водителю %s",
                order["id"], driver_id,
            )

    return delivered


async def notify_client(context, user_id, text):
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
    except Exception:
        logger.exception("Не удалось отправить сообщение клиенту %s", user_id)


async def notify_admins(context, text, reply_markup=None):
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=reply_markup,
            )
            delivered += 1
        except Exception:
            logger.exception("Не удалось уведомить администратора %s", admin_id)
    return delivered


def admin_driver_keyboard(driver_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Одобрить",
            callback_data=f"admin|approve|{driver_id}",
        ),
        InlineKeyboardButton(
            "❌ Отклонить",
            callback_data=f"admin|reject|{driver_id}",
        ),
    ]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    ensure_client(user.id)
    client = get_client(user.id)

    if not client.get("city"):
        context.user_data["registration"] = "client_city"
        await message.reply_text(
            "🚕 Здравствуйте! Добро пожаловать в наш сервис такси.\n\n"
            "Для начала выберите город, в котором хотите заказывать поездки.",
            reply_markup=city_keyboard("client_register"),
        )
        return

    if not client.get("phone"):
        context.user_data["registration"] = "client_phone"
        await message.reply_text(
            "👋 Добро пожаловать!\n\n"
            f"Ваш город: {client['city']}.\n"
            "Пожалуйста, отправьте свой номер телефона для регистрации.",
            reply_markup=phone_menu(),
        )
        return

    active = get_active_order(user.id)
    if active:
        status_text = {
    "awaiting_pickup": "Ожидается ваше место подачи.",
    "awaiting_destination": "Ожидается пункт назначения.",
    "new": "Ищем свободного водителя.",
    "accepted": "Водитель принял заказ.",
    "on_way": "Водитель направляется к вам.",
    "arrived": "Машина на месте. Водитель ожидает вас.",
}

        await message.reply_text(
            f"Ваш активный заказ №{active['id']}.\n{status_text}",
            reply_markup=main_menu(),
        )
        return

    await message.reply_text(
        f"🚕 Добро пожаловать в такси {client['city']}!\n"
        "Выберите действие в меню.",
        reply_markup=main_menu(),
    )


async def driver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    driver = get_driver(user.id)

    if driver and driver["status"] == "approved":
        await message.reply_text(
            f"🚖 Вы подключены как водитель.\n"
            f"Рабочий город: {driver['city']}.\n"
            "Заказы этого города будут приходить сюда.\n\n"
            "Для смены города используйте /driver_settings.",
        )
        return

    if driver and driver["status"] == "pending":
        await message.reply_text(
            "⏳ Ваша заявка водителя ожидает подтверждения администратора.\n"
            f"Выбранный город: {driver['city']}."
        )
        return

    context.user_data["registration"] = "driver_city"
    await message.reply_text(
        "🚖 Регистрация водителя\n\n"
        "Выберите город, в котором будете принимать заказы.",
        reply_markup=city_keyboard("driver_register"),
    )


async def driver_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    driver = get_driver(user.id)
    if not driver or driver["status"] != "approved":
        await message.reply_text(
            "Настройки доступны только подтверждённым водителям."
        )
        return

    await message.reply_text(
        "🏙 Выберите новый рабочий город.\n\n"
        "Внимание: смена города отправит вашу учётную запись на повторное "
        "подтверждение. До одобрения новые заказы приходить не будут.",
        reply_markup=city_keyboard("driver_change"),
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if not user or user.id not in ADMIN_IDS:
        await message.reply_text("⛔ У вас нет прав администратора.")
        return

    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM drivers
            WHERE status = 'pending'
            ORDER BY created_at
        """).fetchall()

    if not rows:
        await message.reply_text("✅ Нет ожидающих заявок водителей.")
        return

    await message.reply_text(f"📋 Ожидают рассмотрения заявок: {len(rows)}")

    for row in rows:
        text = (
            "🚖 <b>Заявка водителя</b>\n\n"
            f"🆔 Telegram ID: <code>{row['telegram_id']}</code>\n"
            f"🏙 Город: {escape(row['city'] or 'не выбран')}\n"
            f"📱 Телефон: <code>{escape(row['phone'] or 'не указан')}</code>\n"
            f"🕒 Создана: {escape(row['created_at'] or '')}"
        )
        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=admin_driver_keyboard(row["telegram_id"]),
        )


async def begin_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    client = get_client(user.id)

    if not client or not client.get("city") or not client.get("phone"):
        await message.reply_text(
            "Сначала завершите регистрацию: выберите город и отправьте телефон."
        )
        await start(update, context)
        return

    active = get_active_order(user.id)
    if active:
        await message.reply_text(
            f"У вас уже есть активный заказ №{active['id']}.",
            reply_markup=main_menu(),
        )
        return

    order_id = create_order(user.id, client["phone"])
    if order_id is None:
        await message.reply_text(
            "Не удалось создать заказ. Проверьте активные заказы и попробуйте ещё раз.",
            reply_markup=main_menu(),
        )
        return

    await message.reply_text(
        f"🚕 Заказ №{order_id} создан.\n"
        f"Город: {client['city']}.\n\n"
        "Отправьте геолокацию места, где вас забрать.",
        reply_markup=location_menu("📍 Отправить место подачи"),
    )


async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    contact = message.contact

    if not contact or not user:
        return

    if contact.user_id != user.id:
        await message.reply_text(
            "Пожалуйста, отправьте именно свой номер через кнопку.",
            reply_markup=phone_menu(),
        )
        return

    registration = context.user_data.get("registration")

    if registration == "driver_phone":
        driver = get_driver(user.id)
        if not driver or not driver.get("city"):
            context.user_data["registration"] = "driver_city"
            await message.reply_text(
                "Сначала выберите рабочий город.",
                reply_markup=city_keyboard("driver_register"),
            )
            return

        save_driver(
            user.id,
            phone=contact.phone_number,
            city=driver["city"],
            status="pending",
        )
        context.user_data.pop("registration", None)

        await message.reply_text(
            "✅ Номер сохранён. Заявка отправлена администратору.\n"
            "До подтверждения заказы приходить не будут.",
            reply_markup=ReplyKeyboardRemove(),
        )

        username = f"@{user.username}" if user.username else "не указан"
        await notify_admins(
            context,
            "🚖 Новая заявка водителя\n\n"
            f"Имя: {escape(user.full_name)}\n"
            f"Username: {escape(username)}\n"
            f"Telegram ID: <code>{user.id}</code>\n"
            f"Город: {escape(driver['city'])}\n"
            f"Телефон: <code>{escape(contact.phone_number)}</code>",
            reply_markup=admin_driver_keyboard(user.id),
        )
        return

    ensure_client(user.id)
    save_client(user.id, phone=contact.phone_number)
    context.user_data.pop("registration", None)

    client = get_client(user.id)
    if not client.get("city"):
        context.user_data["registration"] = "client_city"
        await message.reply_text(
            "Номер сохранён. Теперь выберите город.",
            reply_markup=city_keyboard("client_register"),
        )
        return

    await message.reply_text(
        "✅ Номер телефона сохранён. Регистрация завершена!\n"
        f"Ваш город: {client['city']}.",
        reply_markup=main_menu(),
    )


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message or not user or not message.location:
        return

    client = get_client(user.id)
    if not client or not client.get("city") or not client.get("phone"):
        await message.reply_text(
            "Сначала зарегистрируйтесь с помощью команды /start."
        )
        return

    order = get_active_order(user.id)
    if not order:
        await message.reply_text(
            "Активного заказа нет. Нажмите «🚕 Заказать такси».",
            reply_markup=main_menu(),
        )
        return

    location = message.location
    order_id = order["id"]

    if order["status"] == "awaiting_pickup":
        changed = update_order_if_status(
            order_id,
            ["awaiting_pickup"],
            {
                "pickup_lat": location.latitude,
                "pickup_lon": location.longitude,
                "status": "awaiting_destination",
            },
        )

        if not changed:
            await message.reply_text(
                "Статус заказа изменился. Проверьте меню.",
                reply_markup=main_menu(),
            )
            return

        await message.reply_text(
            "📍 Место подачи сохранено.\n"
            "Теперь отправьте геолокацию пункта назначения.",
            reply_markup=location_menu("🏁 Отправить пункт назначения"),
        )
        return

    if order["status"] != "awaiting_destination":
        await message.reply_text(
            "Для текущего заказа больше не требуется отправлять геолокацию.",
            reply_markup=main_menu(),
        )
        return

    changed = update_order_if_status(
        order_id,
        ["awaiting_destination"],
        {
            "destination_lat": location.latitude,
            "destination_lon": location.longitude,
        },
    )

    if not changed:
        await message.reply_text(
            "Статус заказа изменился. Проверьте меню.",
            reply_markup=main_menu(),
        )
        return

    order = get_order(order_id)
    if order["pickup_lat"] is None or order["pickup_lon"] is None:
        await message.reply_text(
            "Не удалось найти место подачи. Отправьте его повторно.",
            reply_markup=location_menu("📍 Отправить место подачи"),
        )
        return

    await message.reply_text(
        "🗺 Рассчитываем маршрут и стоимость…",
        reply_markup=ReplyKeyboardRemove(),
    )

    try:
        distance = await route_distance_km(
            order["pickup_lat"],
            order["pickup_lon"],
            order["destination_lat"],
            order["destination_lon"],
        )
        price = price_for_distance(distance, order["city"])
    except Exception:
        logger.exception("Ошибка расчёта маршрута для заказа №%s", order_id)
        await message.reply_text(
            "Не удалось рассчитать маршрут. "
            "Пожалуйста, отправьте геолокацию пункта назначения ещё раз.",
            reply_markup=location_menu("🏁 Отправить пункт назначения"),
        )
        return

    changed = update_order_if_status(
        order_id,
        ["awaiting_destination"],
        {
            "distance_km": round(distance, 3),
            "price": price,
            "status": "new",
        },
    )

    if not changed:
        await message.reply_text(
            "Заказ уже изменён или отменён. Начните новый заказ при необходимости.",
            reply_markup=main_menu(),
        )
        return

    order = get_order(order_id)
    delivered = await notify_drivers(context, order)

    if delivered == 0:
        update_order_if_status(order_id, ["new"], {"status": "cancelled"})
        await message.reply_text(
            f"Сейчас нет доступных водителей в городе {order['city']}.\n"
            "Заказ отменён. Попробуйте позже.",
            reply_markup=main_menu(),
        )
        return

    await message.reply_text(
        f"✅ Заказ №{order_id} отправлен водителям города {order['city']}.\n\n"
        f"📏 Расстояние: {distance:.1f} км\n"
        f"💶 Расчётная стоимость: {price:.2f} {CURRENCY}\n\n"
        "Стоимость рассчитана по тарифу выбранного города: "
        "подача машины + цена за километр.\n"
        "Ожидайте подтверждения водителя.",
        reply_markup=main_menu(),
    )


async def city_callback(query, context):
    user = query.from_user
    parts = (query.data or "").split("|", 2)

    if len(parts) != 3:
        await query.answer("Некорректный выбор города.", show_alert=True)
        return

    _, mode, city = parts
    city = normalize_city(city)

    if city not in CITIES:
        await query.answer("Такого города нет в списке.", show_alert=True)
        return

    if mode in ("client_register", "client_change"):
        active = get_active_order(user.id)
        if active:
            await query.answer(
                "Нельзя менять город при активном заказе.",
                show_alert=True,
            )
            return

        ensure_client(user.id)
        save_client(user.id, city=city)
        client = get_client(user.id)

        await query.answer("Город сохранён.")
        context.user_data.pop("registration", None)

        try:
            if not client.get("phone"):
                context.user_data["registration"] = "client_phone"
                await query.edit_message_text(
                    f"🏙 Выбран город: {city}.\n\n"
                    "Теперь отправьте свой номер телефона.",
                )
                await query.message.reply_text(
                    "Нажмите кнопку ниже, чтобы поделиться номером.",
                    reply_markup=phone_menu(),
                )
            else:
                await query.edit_message_text(
                    f"✅ Город изменён на {city}."
                )
                await query.message.reply_text(
                    "Главное меню.",
                    reply_markup=main_menu(),
                )
        except Exception:
            logger.exception("Не удалось показать результат выбора города")
        return

    if mode == "driver_register":
        old = get_driver(user.id)
        if old and old.get("status") == "approved":
            await query.answer(
                "Вы уже подтверждённый водитель. Используйте /driver_settings.",
                show_alert=True,
            )
            return

        save_driver(
            user.id,
            city=city,
            status="pending",
        )
        context.user_data["registration"] = "driver_phone"

        await query.answer("Город сохранён.")
        await query.edit_message_text(
            f"🏙 Ваш рабочий город: {city}.\n\n"
            "Теперь отправьте свой номер телефона кнопкой ниже."
        )
        await query.message.reply_text(
            "Поделитесь номером телефона для заявки водителя.",
            reply_markup=phone_menu(),
        )
        return

    if mode == "driver_change":
        driver = get_driver(user.id)
        if not driver or driver["status"] != "approved":
            await query.answer(
                "Только подтверждённые водители могут менять город.",
                show_alert=True,
            )
            return

        save_driver(
            user.id,
            city=city,
            status="pending",
        )

        await query.answer("Город изменён.")
        await query.edit_message_text(
            f"🏙 Вы выбрали город: {city}.\n\n"
            "Для безопасности требуется повторное подтверждение администратора. "
            "До одобрения новые заказы приходить не будут."
        )

        username = f"@{user.username}" if user.username else "не указан"
        driver = get_driver(user.id)
        await notify_admins(
            context,
            "🔄 Водитель запросил смену города\n\n"
            f"Имя: {escape(user.full_name)}\n"
            f"Username: {escape(username)}\n"
            f"Telegram ID: <code>{user.id}</code>\n"
            f"Новый город: {escape(city)}\n"
            f"Телефон: <code>{escape(driver.get('phone') or 'не указан')}</code>",
            reply_markup=admin_driver_keyboard(user.id),
        )
        return

    await query.answer("Неизвестное действие.", show_alert=True)


async def admin_callback(query, context):
    user = query.from_user
    parts = (query.data or "").split("|")

    if user.id not in ADMIN_IDS:
        await query.answer("Нет прав администратора.", show_alert=True)
        return

    if len(parts) != 3:
        await query.answer("Некорректная команда.", show_alert=True)
        return

    _, action, driver_id_text = parts

    try:
        driver_id = int(driver_id_text)
    except ValueError:
        await query.answer("Некорректный ID водителя.", show_alert=True)
        return

    driver = get_driver(driver_id)
    if not driver:
        await query.answer("Водитель не найден.", show_alert=True)
        return

    if driver["status"] != "pending":
        await query.answer(
            f"Заявка уже обработана: {driver['status']}.",
            show_alert=True,
        )
        return

    if action == "approve":
        save_driver(
            driver_id,
            phone=driver.get("phone"),
            city=driver.get("city"),
            status="approved",
        )
        await query.answer("Водитель одобрен.")
        await notify_client(
            context,
            driver_id,
            f"✅ Ваша заявка одобрена!\n"
            f"Рабочий город: {driver['city']}.\n"
            "Теперь вы можете получать заказы этого города.",
        )
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"✅ Водитель {driver_id} одобрен для города {driver['city']}."
            )
        except Exception:
            pass
        return

    if action == "reject":
        save_driver(
            driver_id,
            phone=driver.get("phone"),
            city=driver.get("city"),
            status="rejected",
        )
        await query.answer("Заявка отклонена.")
        await notify_client(
            context,
            driver_id,
            "❌ К сожалению, ваша заявка водителя отклонена. "
            "Свяжитесь с администратором для уточнения.",
        )
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"❌ Заявка водителя {driver_id} отклонена.")
        except Exception:
            pass
        return

    await query.answer("Неизвестное действие.", show_alert=True)


def parse_callback(data):
    if not data:
        return None, None, None

    parts = data.split("_")

    try:
        if len(parts) == 3 and parts[0] == "eta":
            return "eta", int(parts[2]), parts[1]

        if len(parts) == 3 and parts[0] == "driver" and parts[1] == "cancel":
            return "driver_cancel", int(parts[2]), None

        if len(parts) == 2:
            return parts[0], int(parts[1]), None

    except ValueError:
        return None, None, None

    return None, None, None


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""

    if data.startswith("city|"):
        await city_callback(query, context)
        return

    if data.startswith("admin|"):
        await admin_callback(query, context)
        return

    driver_id = query.from_user.id
    driver = get_driver(driver_id)

    if not driver or driver["status"] != "approved":
        await query.answer(
            "Доступно только подтверждённым водителям.",
            show_alert=True,
        )
        return

    action, order_id, extra = parse_callback(data)

    if action is None or order_id is None:
        await query.answer("Неизвестная команда.", show_alert=True)
        return

    order = get_order(order_id)
    if not order:
        await query.answer("Заказ не найден.", show_alert=True)
        return

    
    if normalize_city(driver["city"]) != normalize_city(order["city"]):
        await query.answer(
            "Этот заказ относится к другому городу.",
            show_alert=True,
        )
        return

    if action == "accept":
        changed = update_order_if_status(
            order_id,
            ["new"],
            {
                "status": "accepted",
                "driver_id": driver_id,
                "eta": None,
            },
        )

        if not changed:
            current = get_order(order_id)

            if (
                current
                and current.get("driver_id") == driver_id
                and current["status"] in ("accepted", "on_way")
            ):
                await query.answer("Этот заказ уже закреплён за вами!")
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=driver_keyboard(order_id, current["status"])
                    )
                except Exception:
                    pass
                return

            await query.answer(
                "Заказ уже принят другим водителем или отменён.",
                show_alert=True,
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        order = get_order(order_id)
        await query.answer("Заказ закреплён за вами!")
        await notify_client(
            context,
            order["client_id"],
            f"✅ Водитель принял ваш заказ №{order_id}.\n"
            "Водитель скоро сообщит время прибытия.",
        )

   
        for other_id in approved_drivers_for_city(order["city"]):
            if other_id == driver_id:
                continue
            try:
                await context.bot.send_message(
                    chat_id=other_id,
                    text=f"Заказ №{order_id} уже принят другим водителем.",
                )
            except Exception:
                pass

        try:
            await query.edit_message_text(
                driver_order_text(order) + "\n\n✅ <b>Заказ закреплён за вами.</b>",
                parse_mode="HTML",
                reply_markup=driver_keyboard(order_id, "accepted"),
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Не удалось обновить сообщение водителя")
        return

    if action == "reject":
        if order["status"] != "new" or order.get("driver_id") is not None:
            await query.answer("Этот заказ больше не доступен.", show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        await query.answer("Вы отказались от заказа.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"Вы отказались от заказа №{order_id}. "
                "Другие водители по-прежнему могут его принять."
            )
        except Exception:
            logger.exception("Ошибка обновления сообщения при отказе")
        return

    if order.get("driver_id") != driver_id:
        await query.answer(
            "Этот заказ закреплён за другим водителем.",
            show_alert=True,
        )
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return


    if action == "depart":
        changed = update_order_if_status(
            order_id,
            ["accepted"],
            {"status": "on_way"},
            driver_id=driver_id,
        )

        if not changed:
            await query.answer(
                "Нельзя отметить выезд: статус заказа уже изменился.",
                show_alert=True,
            )
            return

        order = get_order(order_id)
        await query.answer("Клиент уведомлён: вы выехали.")

        await notify_client(
            context,
            order["client_id"],
            f"🚕 Водитель выехал к вам!\n"
            f"Заказ №{order_id}.\n"
            + (
                f"⏱ Ориентировочное время прибытия: {order['eta']}."
                if order.get("eta")
                else "Водитель направляется к месту подачи."
            ),
        )

        try:
            await query.edit_message_reply_markup(
                reply_markup=driver_keyboard(order_id, order["status"])
            )
        except Exception:
            logger.exception(
                "Не удалось обновить кнопки выезда для заказа №%s",
                order_id,
            )
        return

    if action == "arrived":
        changed = update_order_if_status(
            order_id,
            ["on_way"],
            {"status": "arrived"},
            driver_id=driver_id,
        )

        if not changed:
            await query.answer(
                "Нельзя отметить прибытие: статус заказа уже изменился.",
                show_alert=True,
            )
            return

        order = get_order(order_id)
        await query.answer("Клиент уведомлён: машина на месте.")

        await notify_client(
            context,
            order["client_id"],
            f"📍 Машина на месте!\n"
            f"Заказ №{order_id}.\n"
            "Водитель ожидает вас.",
        )

        try:
            await query.edit_message_reply_markup(
                reply_markup=driver_keyboard(order_id, "arrived")
            )
        except Exception:
            logger.exception(
                "Не удалось обновить кнопки прибытия для заказа №%s",
                order_id,
            )
        return

    if action == "eta":
        eta_values = {
            "3": "3 минуты",
            "5": "5 минут",
            "10": "10 минут",
            "15+": "15+ минут",
        }

        if extra not in eta_values:
            await query.answer("Некорректное время прибытия.", show_alert=True)
            return

        if order["status"] not in ("accepted", "on_way"):
            await query.answer("Нельзя изменить ETA для этого заказа.", show_alert=True)
            return

        eta_text = eta_values[extra]
        changed = update_order_if_status(
    order_id,
    ["accepted", "on_way"],
    {"eta": eta_text},
    driver_id=driver_id,
)

        if not changed:
            await query.answer("Статус заказа уже изменился.", show_alert=True)
            return

        order = get_order(order_id)
        await query.answer("Время прибытия отправлено клиенту.")
        await notify_client(
            context,
            order["client_id"],
            f"🚕 По заказу №{order_id} водитель сообщил: "
            f"примерно {eta_text} до прибытия.",
        )

        try:
            await query.edit_message_reply_markup(
                reply_markup=driver_keyboard(order_id, order["status"])
            )
        except Exception:
            pass
        return

    if action == "finish":
        changed = update_order_if_status(
            order_id,
            ["arrived"],
            {"status": "finished"},
            driver_id=driver_id,
        )

        if not changed:
            await query.answer(
                "Поездку нельзя завершить: её статус уже изменился.",
                show_alert=True,
            )
            return

        await query.answer("Поездка завершена.")
        await notify_client(
            context,
            order["client_id"],
            f"🏁 Поездка по заказу №{order_id} завершена. Спасибо!",
        )

        try:
            await query.edit_message_text(
                f"🏁 Заказ №{order_id} завершён.",
                reply_markup=None,
            )
        except Exception:
            pass
        return

    if action == "driver_cancel":
        changed = update_order_if_status(
            order_id,
            ["accepted", "on_way", "arrived"],
            {"status": "cancelled"},
            driver_id=driver_id,
        )

        if not changed:
            await query.answer("Заказ уже завершён или отменён.", show_alert=True)
            return

        await query.answer("Заказ отменён.")
        await notify_client(
            context,
            order["client_id"],
            f"⚠️ Водитель отменил заказ №{order_id}. "
            "Пожалуйста, закажите такси ещё раз.",
        )

        try:
            await query.edit_message_text(
                f"❌ Вы отменили заказ №{order_id}.",
                reply_markup=None,
            )
        except Exception:
            pass
        return

    await query.answer("Неизвестное действие.", show_alert=True)


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    order = get_active_order(user.id)

    if not order:
        await message.reply_text(
            "У вас нет активного заказа.",
            reply_markup=main_menu(),
        )
        return

    changed = update_order_if_status(
        order["id"],
        list(ACTIVE_STATUSES),
        {"status": "cancelled"},
    )

    if not changed:
        await message.reply_text(
            "Статус заказа уже изменился. Обновите меню.",
            reply_markup=main_menu(),
        )
        return

    if order.get("driver_id") is not None:
        await notify_client(
            context,
            order["driver_id"],
            f"⚠️ Клиент отменил заказ №{order['id']}.",
        )
    elif order["status"] == "new":
        for driver_id in approved_drivers_for_city(order["city"]):
            try:
                await context.bot.send_message(
                    chat_id=driver_id,
                    text=f"Заказ №{order['id']} отменён клиентом.",
                )
            except Exception:
                logger.exception("Не удалось уведомить водителя %s", driver_id)

    await message.reply_text(
        f"Заказ №{order['id']} отменён.",
        reply_markup=main_menu(),
    )


async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    text = (message.text or "").strip()
    driver = get_driver(user.id)

    if text == "🚕 Заказать такси":
        await begin_order(update, context)
        return

    if text == "❌ Отменить заказ":
        await cancel_order(update, context)
        return

    if text == "🏙 Мой город":
        client = get_client(user.id)
        if not client or not client.get("city"):
            await message.reply_text(
                "Сначала выберите город через /start.",
                reply_markup=city_keyboard("client_register"),
            )
            return

        await message.reply_text(
            f"🏙 Ваш город: {client['city']}.",
            reply_markup=main_menu(),
        )
        return

    if text == "📱 Мой телефон":
        client = get_client(user.id)
        phone = client.get("phone") if client else None

        if phone:
            await message.reply_text(
                f"📱 Ваш сохранённый номер: {phone}",
                reply_markup=main_menu(),
            )
        else:
            context.user_data["registration"] = "client_phone"
            await message.reply_text(
                "Отправьте свой номер телефона.",
                reply_markup=phone_menu(),
            )
        return

    if text == "⚙️ Настройки":
        await message.reply_text(
            "⚙️ Настройки клиента\n\n"
            "Выберите, что хотите изменить.",
            reply_markup=ReplyKeyboardMarkup(
                [
                    [KeyboardButton("🏙 Изменить город")],
                    [KeyboardButton("📱 Изменить телефон")],
                    [KeyboardButton("⬅️ В меню")],
                ],
                resize_keyboard=True,
            ),
        )
        return

    if text == "🏙 Изменить город":
        if get_active_order(user.id):
            await message.reply_text(
                "Нельзя менять город, пока есть активный заказ.",
                reply_markup=main_menu(),
            )
            return

        await message.reply_text(
            "Выберите новый город:",
            reply_markup=city_keyboard("client_change"),
        )
        return

    if text == "📱 Изменить телефон":
        context.user_data["registration"] = "client_phone"
        await message.reply_text(
            "Отправьте новый номер через кнопку ниже.",
            reply_markup=phone_menu(),
        )
        return

    if text == "ℹ️ Помощь":
        await message.reply_text(
            "🚕 Как заказать такси\n\n"
            "1. Нажмите «Заказать такси».\n"
            "2. Отправьте место подачи.\n"
            "3. Отправьте пункт назначения.\n"
            "4. Проверьте расчётную стоимость.\n"
            "5. Дождитесь подтверждения водителя.\n\n"
            "Заказ получают только подтверждённые водители вашего города.\n"
            "Отменить активный заказ можно кнопкой «Отменить заказ».\n\n"
            "Для регистрации водителя используйте /driver.",
            reply_markup=main_menu(),
        )
        return

    if text == "⬅️ В меню":
        context.user_data.pop("registration", None)
        await message.reply_text("Главное меню.", reply_markup=main_menu())
        return

    if driver and driver["status"] == "approved":
        if text == "🏙 Изменить рабочий город":
            await driver_settings(update, context)
            return

        if text == "📱 Изменить телефон водителя":
            context.user_data["registration"] = "driver_phone"
            await message.reply_text(
                "Отправьте новый номер телефона.",
                reply_markup=phone_menu(),
            )
            return

    await message.reply_text(
        "Выберите действие с помощью кнопок меню или используйте /start.",
        reply_markup=main_menu(),
    )


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Необработанная ошибка при обработке обновления",
        exc_info=(
            type(context.error),
            context.error,
            context.error.__traceback__,
        ) if context.error else None,
    )


def main():
    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("driver", driver_start))
    app.add_handler(CommandHandler("driver_settings", driver_settings))
    app.add_handler(CommandHandler("admin", admin_command))

    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_handler)
    )

    app.add_error_handler(error_handler)

    logger.info("Бот запускается. Города: %s", ", ".join(CITIES))
    logger.info("База данных: %s", DB_FILE)

    app.run_polling()


if __name__ == "__main__":
  
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    main()


