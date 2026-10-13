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
CITY_NAME = os.getenv("CITY_NAME", "Plauen").strip() or "Plauen"
DRIVERS_RAW = os.getenv("DRIVERS", "").strip()
DB_FILE = os.getenv("DB_FILE", "bot.db")

START_PRICE = float(os.getenv("START_PRICE", "3"))
PRICE_PER_KM = float(os.getenv("PRICE_PER_KM", "1.2"))
CURRENCY = os.getenv("CURRENCY", "€").strip() or "€"
ROUTING_URL = os.getenv(
    "ROUTING_URL",
    "https://router.project-osrm.org",
).rstrip("/")

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
)

ACTIVE_SQL = ", ".join("?" for _ in ACTIVE_STATUSES)

DRIVERS = set()
for item in DRIVERS_RAW.split(","):
    item = item.strip()
    if item:
        try:
            DRIVERS.add(int(item))
        except ValueError:
            raise ValueError(
                f"Неверный Telegram ID водителя в DRIVERS: {item!r}"
            )

if not TOKEN:
    raise RuntimeError("Не указан TOKEN в файле .env")

if not DRIVERS:
    raise RuntimeError(
        "Не указаны Telegram ID водителей в DRIVERS в файле .env"
    )

if START_PRICE < 0 or PRICE_PER_KM < 0:
    raise RuntimeError("Тарифы START_PRICE и PRICE_PER_KM не могут быть отрицательными")



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

    for column_name, column_type in columns.items():
        if column_name not in existing:
            conn.execute(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN {column_name} {column_type}"
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

      
        add_missing_columns(
            conn,
            "clients",
            {
                "phone": "TEXT",
                "city": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
        )

        add_missing_columns(
            conn,
            "orders",
            {
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
            },
        )

        conn.execute(
            "UPDATE clients SET city = ? "
            "WHERE city IS NULL OR TRIM(city) = ''",
            (CITY_NAME,),
        )
        conn.execute(
            "UPDATE orders SET city = ? "
            "WHERE city IS NULL OR TRIM(city) = ''",
            (CITY_NAME,),
        )

        conn.execute("""
            UPDATE clients
            SET created_at = CURRENT_TIMESTAMP
            WHERE created_at IS NULL OR TRIM(created_at) = ''
        """)
        conn.execute("""
            UPDATE clients
            SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP)
            WHERE updated_at IS NULL OR TRIM(updated_at) = ''
        """)
        conn.execute("""
            UPDATE orders
            SET created_at = CURRENT_TIMESTAMP
            WHERE created_at IS NULL OR TRIM(created_at) = ''
        """)
        conn.execute("""
            UPDATE orders
            SET updated_at = COALESCE(created_at, CURRENT_TIMESTAMP)
            WHERE updated_at IS NULL OR TRIM(updated_at) = ''
        """)

  
        conn.execute("""
            UPDATE orders
            SET status = 'cancelled'
            WHERE status IS NULL OR TRIM(status) = ''
        """)

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

    logger.info("База данных готова: %s", DB_FILE)


def get_client(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM clients WHERE telegram_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def save_client(user_id, phone=None, city=None):
    old = get_client(user_id)

    if old:
        new_phone = phone if phone is not None else old.get("phone")
        new_city = city or old.get("city") or CITY_NAME

        with db() as conn:
            conn.execute("""
                UPDATE clients
                SET phone = ?,
                    city = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
            """, (new_phone, new_city, user_id))
    else:
        with db() as conn:
            conn.execute("""
                INSERT INTO clients (
                    telegram_id, phone, city, created_at, updated_at
                )
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (user_id, phone, city or CITY_NAME))

    return get_client(user_id)


def get_active_order(user_id):
    with db() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM orders
            WHERE client_id = ?
              AND status IN ({ACTIVE_SQL})
            ORDER BY id DESC
            LIMIT 1
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
                client_id, phone, city, status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, 'awaiting_pickup',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (user_id, phone, CITY_NAME))

        return cursor.lastrowid


def update_order_if_status(order_id, statuses, updates, driver_id=None):
 
    if not statuses:
        return False

    assignments = [
        f"{column} = ?" for column in updates
    ]
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


def price_for_distance(distance_km):
    # Округляем расстояние вверх до десятых километра.
    charged_km = math.ceil(max(0, distance_km) * 10) / 10
    return round(START_PRICE + charged_km * PRICE_PER_KM, 2)


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
                KeyboardButton("❌ Отменить заказ"),
                KeyboardButton("ℹ️ Помощь"),
            ],
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


def driver_keyboard(order_id, status):
    if status == "new":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Принять",
                    callback_data=f"accept_{order_id}",
                ),
                InlineKeyboardButton(
                    "❌ Отказаться",
                    callback_data=f"reject_{order_id}",
                ),
            ]
        ])

    if status in ("accepted", "on_way"):
        rows = [
            [
                InlineKeyboardButton(
                    "⏱ 3 мин",
                    callback_data=f"eta_3_{order_id}",
                ),
                InlineKeyboardButton(
                    "⏱ 5 мин",
                    callback_data=f"eta_5_{order_id}",
                ),
                InlineKeyboardButton(
                    "⏱ 10 мин",
                    callback_data=f"eta_10_{order_id}",
                ),
                InlineKeyboardButton(
                    "⏱ 15+ мин",
                    callback_data=f"eta_15+_{order_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Отменить поездку",
                    callback_data=f"driver_cancel_{order_id}",
                )
            ],
        ]

        if status == "on_way":
            rows.insert(
                1,
                [
                    InlineKeyboardButton(
                        "🏁 Завершить поездку",
                        callback_data=f"finish_{order_id}",
                    )
                ],
            )

        return InlineKeyboardMarkup(rows)

    return None



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

    distance_text = (
        f"{distance:.1f} км" if distance is not None else "Не рассчитано"
    )
    price_text = (
        f"{price:.2f} {escape(CURRENCY)}"
        if price is not None else "Не рассчитана"
    )

    return (
        f"🚕 <b>Заказ №{order['id']}</b>\n\n"
        f"🏙 Город: {escape(str(order.get('city') or CITY_NAME))}\n"
        f"📍 Подача: {pickup}\n"
        f"🏁 Назначение: {destination}\n"
        f"📱 Телефон клиента: <code>{escape(str(order.get('phone') or 'не указан'))}</code>\n"
        f"📏 Расстояние: {distance_text}\n"
        f"💶 Стоимость: {price_text}\n"
    )


async def notify_drivers(context, order):
    text = driver_order_text(order)
    delivered = 0

    for driver_id in DRIVERS:
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
                order["id"],
                driver_id,
            )

    return delivered


async def notify_client(context, user_id, text):
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
        )
    except Exception:
        logger.exception(
            "Не удалось отправить сообщение клиенту %s",
            user_id,
        )




async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    client = get_client(user.id)

    if not client:
        save_client(user.id, city=CITY_NAME)
        await update.effective_message.reply_text(
            f"Здравствуйте! Это бот заказа такси в городе {CITY_NAME}.\n\n"
            "Для начала отправьте номер телефона.",
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
        }.get(active["status"], "Заказ активен.")

        await update.effective_message.reply_text(
            f"Ваш активный заказ №{active['id']}.\n{status_text}",
            reply_markup=main_menu(),
        )
        return

    await update.effective_message.reply_text(
        f"Добро пожаловать в такси {CITY_NAME}!\n"
        "Выберите действие в меню.",
        reply_markup=main_menu(),
    )


async def begin_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    active = get_active_order(user.id)
    if active:
        await message.reply_text(
            f"У вас уже есть активный заказ №{active['id']}.",
            reply_markup=main_menu(),
        )
        return

    client = get_client(user.id)
    if not client or not client.get("phone"):
        await message.reply_text(
            "Перед заказом отправьте свой номер телефона.",
            reply_markup=phone_menu(),
        )
        return

    order_id = create_order(user.id, client["phone"])

    if order_id is None:
        active = get_active_order(user.id)
        if active:
            await message.reply_text(
                f"У вас уже есть активный заказ №{active['id']}.",
                reply_markup=main_menu(),
            )
        else:
            await message.reply_text(
                "Не удалось создать заказ. Попробуйте ещё раз.",
                reply_markup=main_menu(),
            )
        return

    await message.reply_text(
        f"Заказ №{order_id} создан.\n"
        "Отправьте свою геолокацию — место, где вас забрать.",
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

    save_client(user.id, phone=contact.phone_number)

    active = get_active_order(user.id)

    if active:
        await message.reply_text(
            f"Номер сохранён. У вас уже есть заказ №{active['id']}.",
            reply_markup=main_menu(),
        )
    else:
        await message.reply_text(
            "Номер телефона сохранён. Теперь можно заказывать такси.",
            reply_markup=main_menu(),
        )


async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user

    if not message.location:
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
            "Место подачи сохранено.\n"
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
        "Рассчитываем маршрут и стоимость…",
        reply_markup=ReplyKeyboardRemove(),
    )

    try:
        distance = await route_distance_km(
            order["pickup_lat"],
            order["pickup_lon"],
            order["destination_lat"],
            order["destination_lon"],
        )
        price = price_for_distance(distance)
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
        update_order_if_status(
            order_id,
            ["new"],
            {"status": "cancelled"},
        )
        await message.reply_text(
            "Сейчас не удалось связаться ни с одним водителем. "
            "Заказ отменён. Попробуйте позже.",
            reply_markup=main_menu(),
        )
        return

    await message.reply_text(
        f"✅ Заказ №{order_id} отправлен водителям.\n\n"
        f"📏 Расстояние: {distance:.1f} км\n"
        f"💶 Расчётная стоимость: {price:.2f} {CURRENCY}\n\n"
        "Стоимость рассчитана по тарифу: посадка + цена за километр.\n"
        "Ожидайте подтверждения водителя.",
        reply_markup=main_menu(),
    )




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

    driver_id = query.from_user.id

    if driver_id not in DRIVERS:
        await query.answer(
            "У вас нет доступа к управлению заказами.",
            show_alert=True,
        )
        return

    action, order_id, extra = parse_callback(query.data)

    if action is None or order_id is None:
        await query.answer("Неизвестная команда.", show_alert=True)
        return

    order = get_order(order_id)

    if not order:
        await query.answer("Заказ не найден.", show_alert=True)
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
                await query.answer(
                    "Этот заказ уже закреплён за вами!"
                )
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=driver_keyboard(
                            order_id,
                            current["status"],
                        )
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

        try:
            await query.edit_message_text(
                driver_order_text(order)
                + "\n\n✅ <b>Заказ закреплён за вами.</b>",
                parse_mode="HTML",
                reply_markup=driver_keyboard(order_id, "accepted"),
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception(
                "Не удалось обновить сообщение водителя по заказу №%s",
                order_id,
            )

        return


    if action == "reject":
        if order["status"] != "new" or order.get("driver_id") is not None:
            await query.answer(
                "Этот заказ больше не доступен.",
                show_alert=True,
            )
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
            await query.answer(
                "Нельзя изменить ETA для этого заказа.",
                show_alert=True,
            )
            return

        eta_text = eta_values[extra]

        changed = update_order_if_status(
            order_id,
            ["accepted", "on_way"],
            {
                "eta": eta_text,
                "status": "on_way",
            },
            driver_id=driver_id,
        )

        if not changed:
            await query.answer(
                "Статус заказа уже изменился.",
                show_alert=True,
            )
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
                reply_markup=driver_keyboard(order_id, "on_way")
            )
        except Exception:
            pass

        return


    if action == "finish":
        changed = update_order_if_status(
            order_id,
            ["on_way"],
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
            ["accepted", "on_way"],
            {"status": "cancelled"},
            driver_id=driver_id,
        )

        if not changed:
            await query.answer(
                "Заказ уже завершён или отменён.",
                show_alert=True,
            )
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
        for driver_id in DRIVERS:
            try:
                await context.bot.send_message(
                    chat_id=driver_id,
                    text=f"Заказ №{order['id']} отменён клиентом.",
                )
            except Exception:
                logger.exception(
                    "Не удалось уведомить водителя %s об отмене заказа",
                    driver_id,
                )

    await message.reply_text(
        f"Заказ №{order['id']} отменён.",
        reply_markup=main_menu(),
    )


async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    text = (message.text or "").strip()

    if text == "🚕 Заказать такси":
        await begin_order(update, context)
        return

    if text == "❌ Отменить заказ":
        await cancel_order(update, context)
        return

    if text == "📱 Мой телефон":
        client = get_client(user.id)
        phone = client.get("phone") if client else None

        if phone:
            await message.reply_text(
                f"Ваш сохранённый номер: {phone}",
                reply_markup=main_menu(),
            )
        else:
            await message.reply_text(
                "Отправьте свой номер телефона.",
                reply_markup=phone_menu(),
            )
        return

    if text == "🏙 Мой город":
        await message.reply_text(
            f"Город обслуживания: {CITY_NAME}",
            reply_markup=main_menu(),
        )
        return

    if text == "ℹ️ Помощь":
        await message.reply_text(
            "🚕 <b>Как заказать такси</b>\n\n"
            "1. Нажмите «Заказать такси».\n"
            "2. Отправьте место подачи.\n"
            "3. Отправьте пункт назначения.\n"
            "4. Проверьте расчётную стоимость.\n"
            "5. Дождитесь подтверждения водителя.\n\n"
            "Отменить активный заказ можно кнопкой "
            "«Отменить заказ».",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return

    if text == "⬅️ В меню":
        await message.reply_text(
            "Главное меню.",
            reply_markup=main_menu(),
        )
        return

    await message.reply_text(
        "Выберите действие с помощью кнопок меню.",
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
    app.add_handler(
        MessageHandler(filters.CONTACT, contact_handler)
    )
    app.add_handler(
        MessageHandler(filters.LOCATION, location_handler)
    )
    app.add_handler(
        CallbackQueryHandler(callback_handler)
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            menu_text_handler,
        )
    )

    app.add_error_handler(error_handler)

    logger.info("Бот такси запускается. Город: %s", CITY_NAME)
    app.run_polling()


if __name__ == "__main__":
    import asyncio

    asyncio.set_event_loop(asyncio.new_event_loop())
    main()

    '''Города сделать,регестрация для клинта и для водителя, кошелек проект '''