import sqlite3
import os

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("TOKEN")
YOUR_USERNAME = os.getenv("YOUR_USERNAME")
DRIVERS = list(map(int, os.getenv("DRIVERS").split(",")))

DB_FILE = "bot.db"


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            telegram_id INTEGER PRIMARY KEY,
            phone TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            status TEXT NOT NULL,
            driver_id INTEGER,
            location_sent INTEGER DEFAULT 0,
            latitude REAL,
            longitude REAL,
            eta TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def get_client_phone(client_id):
    conn = get_db()

    client = conn.execute(
        """
        SELECT phone
        FROM clients
        WHERE telegram_id = ?
        """,
        (client_id,)
    ).fetchone()

    conn.close()

    if client is None:
        return None

    return client["phone"]


def save_client_phone(client_id, phone):
    conn = get_db()

    existing = conn.execute(
        """
        SELECT telegram_id
        FROM clients
        WHERE telegram_id = ?
        """,
        (client_id,)
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE clients
            SET phone = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE telegram_id = ?
            """,
            (phone, client_id)
        )
    else:
        conn.execute(
            """
            INSERT INTO clients (
                telegram_id,
                phone
            )
            VALUES (?, ?)
            """,
            (client_id, phone)
        )

    conn.commit()
    conn.close()


def get_order(order_id):
    conn = get_db()

    order = conn.execute(
        """
        SELECT *
        FROM orders
        WHERE id = ?
        """,
        (order_id,)
    ).fetchone()

    conn.close()

    return order


def get_active_order(client_id):
    conn = get_db()

    order = conn.execute(
        """
        SELECT *
        FROM orders
        WHERE client_id = ?
        AND status NOT IN ('cancelled', 'finished')
        ORDER BY id DESC
        LIMIT 1
        """,
        (client_id,)
    ).fetchone()

    conn.close()

    return order


def create_order_db(client_id, phone):
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO orders (
            client_id,
            phone,
            status,
            driver_id,
            location_sent,
            latitude,
            longitude,
            eta
        )
        VALUES (?, ?, 'new', NULL, 0, NULL, NULL, NULL)
        """,
        (client_id, phone)
    )

    order_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return order_id


def update_order(order_id, **fields):
    if not fields:
        return

    allowed_fields = {
        "status",
        "driver_id",
        "location_sent",
        "latitude",
        "longitude",
        "eta",
    }

    fields = {
        key: value
        for key, value in fields.items()
        if key in allowed_fields
    }

    if not fields:
        return

    set_clause = ", ".join(
        f"{key} = ?"
        for key in fields
    )

    values = list(fields.values())
    values.append(order_id)

    conn = get_db()

    conn.execute(
        f"""
        UPDATE orders
        SET {set_clause}
        WHERE id = ?
        """,
        values
    )

    conn.commit()
    conn.close()


def start_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🚕 Заказать такси")]
        ],
        resize_keyboard=True
    )


def phone_kb():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📱 Отправить номер телефона",
                    request_contact=True
                )
            ],
            [
                KeyboardButton("❌ Отменить")
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def active_kb():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📍 Отправить локацию",
                    request_location=True
                )
            ],
            [
                KeyboardButton("❌ Отменить заказ")
            ]
        ],
        resize_keyboard=True
    )


def driver_kb(order_id, status):
    buttons = []

    if status in ["accepted", "on_way"]:
        buttons.extend([
            [
                InlineKeyboardButton(
                    "⏱ 3 мин",
                    callback_data=f"eta_3_{order_id}"
                ),
                InlineKeyboardButton(
                    "⏱ 5 мин",
                    callback_data=f"eta_5_{order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⏱ 10 мин",
                    callback_data=f"eta_10_{order_id}"
                ),
                InlineKeyboardButton(
                    "⏱ 15+ мин",
                    callback_data=f"eta_15+_{order_id}"
                )
            ]
        ])

    if status == "on_way":
        buttons.append([
            InlineKeyboardButton(
                "🏁 Завершить поездку",
                callback_data=f"finish_{order_id}"
            )
        ])

    if status in ["accepted", "on_way"]:
        buttons.append([
            InlineKeyboardButton(
                "❌ Отменить заказ",
                callback_data=f"driver_cancel_{order_id}"
            )
        ])

    return InlineKeyboardMarkup(buttons)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "🚕 Добро пожаловать!\n\n"
        "Нажмите кнопку ниже, чтобы заказать поездку.",
        reply_markup=start_kb()
    )


async def request_phone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.message.from_user

    active_order = get_active_order(user.id)

    if active_order:
        await update.message.reply_text(
            "⚠️ У вас уже есть активный заказ.",
            reply_markup=active_kb()
        )
        return

    phone = get_client_phone(user.id)

    if phone:
        await create_order(
            update,
            context,
            phone
        )
        return

    await update.message.reply_text(
        "📱 Пожалуйста, отправьте свой номер телефона.",
        reply_markup=phone_kb()
    )


async def create_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    phone: str
):
    user = update.message.from_user

    active_order = get_active_order(user.id)

    if active_order:
        await update.message.reply_text(
            "⚠️ У вас уже есть активный заказ.",
            reply_markup=active_kb()
        )
        return

    save_client_phone(
        user.id,
        phone
    )

    order_id = create_order_db(
        user.id,
        phone
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Принять",
                callback_data=f"accept_{order_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Отклонить",
                callback_data=f"reject_{order_id}"
            )
        ]
    ])

    username = (
        f"@{user.username}"
        if user.username
        else "нет"
    )

    msg = (
        f"🚨 НОВЫЙ ЗАКАЗ #{order_id}\n\n"
        f"👤 Имя: {user.first_name}\n"
        f"📱 Телефон: {phone}\n"
        f"🆔 ID: {user.id}\n"
        f"👤 Username: {username}"
    )

    for driver in DRIVERS:
        try:
            await context.bot.send_message(
                chat_id=driver,
                text=msg,
                reply_markup=keyboard
            )
        except Exception as e:
            print(
                f"Ошибка отправки заказа водителю {driver}: {e}"
            )

    await update.message.reply_text(
        f"✅ Заказ #{order_id} создан.\n\n"
        f"⏳ Ищем водителя...",
        reply_markup=active_kb()
    )


async def contact(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.message.from_user
    contact_data = update.message.contact

    if contact_data.user_id is not None:
        if contact_data.user_id != user.id:
            await update.message.reply_text(
                "⚠️ Пожалуйста, отправьте свой собственный номер телефона.",
                reply_markup=phone_kb()
            )
            return

    phone = contact_data.phone_number

    await create_order(
        update,
        context,
        phone
    )


def parse_callback(data: str):
    parts = data.split("_")

    if parts[0] == "eta":
        return (
            "eta",
            parts[1],
            int(parts[2])
        )

    if parts[0] == "driver":
        action = f"{parts[0]}_{parts[1]}"
        order_id = int(parts[2])
        return action, None, order_id

    action = parts[0]
    order_id = int(parts[1])

    return action, None, order_id


async def callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    data = query.data

    action, extra, order_id = parse_callback(data)

    order = get_order(order_id)

    if order is None:
        await query.answer(
            "⚠️ Заказ не найден.",
            show_alert=True
        )
        return

    driver_id = query.from_user.id

    if driver_id not in DRIVERS:
        await query.answer(
            "🚫 У вас нет прав водителя.",
            show_alert=True
        )
        return

    if action == "accept":
        if order["status"] != "new":
            await query.answer(
                "🚫 Этот заказ уже принят другим водителем.",
                show_alert=True
            )
            return

        if order["driver_id"] is not None:
            await query.answer(
                "🚫 Этот заказ уже занят.",
                show_alert=True
            )
            return

        update_order(
            order_id,
            driver_id=driver_id,
            status="accepted"
        )

        try:
            await context.bot.send_message(
                chat_id=order["client_id"],
                text=(
                    "🚗 Водитель принял заказ!\n\n"
                    "📍 Теперь отправьте вашу локацию."
                ),
                reply_markup=active_kb()
            )
        except Exception as e:
            print(
                f"Ошибка отправки сообщения клиенту: {e}"
            )

        await query.edit_message_text(
            text=(
                f"✅ Вы приняли заказ #{order_id}\n\n"
                f"👤 Клиент: ID {order['client_id']}\n"
                f"📱 Телефон: {order['phone']}\n\n"
                f"Выберите примерное время прибытия."
            ),
            reply_markup=driver_kb(
                order_id,
                "accepted"
            )
        )

        await query.answer(
            "✅ Заказ принят!"
        )

        for other_driver in DRIVERS:
            if other_driver == driver_id:
                continue

            try:
                await context.bot.send_message(
                    chat_id=other_driver,
                    text=(
                        f"🚫 Заказ #{order_id} уже принят "
                        f"другим водителем."
                    )
                )
            except Exception as e:
                print(
                    f"Ошибка уведомления водителя "
                    f"{other_driver}: {e}"
                )

        return

    protected_actions = [
        "eta",
        "finish",
        "driver_cancel",
    ]

    if action in protected_actions:
        if order["driver_id"] != driver_id:
            await query.answer(
                "🚫 Этот заказ принят другим водителем.",
                show_alert=True
            )
            return

    if action == "reject":
        if order["status"] != "new":
            await query.answer(
                "⚠️ Заказ уже неактуален.",
                show_alert=True
            )
            return

        await query.edit_message_text(
            f"❌ Вы отклонили заказ #{order_id}"
        )

        await query.answer(
            "Заказ отклонён."
        )

        return

    if action == "eta":
        if order["status"] not in ["accepted", "on_way"]:
            await query.answer(
                "⚠️ Сейчас нельзя установить ETA.",
                show_alert=True
            )
            return

        if order["eta"] == extra:
            await query.answer(
                f"ETA уже установлено: {extra} мин."
            )
            return

        previous_eta = order["eta"]

        update_order(
            order_id,
            eta=extra,
            status="on_way"
        )

        if previous_eta is None:
            client_text = (
                "🚗 Водитель выехал!\n\n"
                f"⏱ Будет примерно через {extra} минут."
            )
        else:
            client_text = (
                "🔄 Водитель обновил время прибытия.\n\n"
                f"⏱ Теперь будет примерно через {extra} минут."
            )

        await context.bot.send_message(
            chat_id=order["client_id"],
            text=client_text
        )

        await query.edit_message_text(
            text=(
                f"🚗 Вы выехали к клиенту\n\n"
                f"⏱ ETA: {extra} мин.\n\n"
                f"Заказ #{order_id}"
            ),
            reply_markup=driver_kb(
                order_id,
                "on_way"
            )
        )

        await query.answer(
            f"ETA установлено: {extra} мин."
        )

        return

    if action == "finish":
        if order["status"] != "on_way":
            await query.answer(
                "⚠️ Сначала выберите время прибытия.",
                show_alert=True
            )
            return

        await context.bot.send_message(
            chat_id=order["client_id"],
            text=(
                "🏁 Поездка завершена.\n\n"
                "Спасибо ❤️"
            ),
            reply_markup=start_kb()
        )

        update_order(
            order_id,
            status="finished"
        )

        await query.edit_message_text(
            text=f"✅ Поездка завершена #{order_id}"
        )

        await query.answer(
            "🏁 Поездка завершена."
        )

        return

    if action == "driver_cancel":
        if order["status"] not in ["accepted", "on_way"]:
            await query.answer(
                "⚠️ Этот заказ уже неактуален.",
                show_alert=True
            )
            return

        await context.bot.send_message(
            chat_id=order["client_id"],
            text=(
                "❌ Водитель отменил заказ.\n\n"
                "Вы можете заказать поездку снова."
            ),
            reply_markup=start_kb()
        )

        update_order(
            order_id,
            status="cancelled"
        )

        await query.edit_message_text(
            text=f"❌ Заказ #{order_id} отменён водителем."
        )

        await query.answer(
            "Заказ отменён."
        )

        return


async def location(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.message.from_user

    order = get_active_order(user.id)

    if order is None:
        await update.message.reply_text(
            "⚠️ У вас нет активного заказа."
        )
        return

    if order["status"] not in ["accepted", "on_way"]:
        await update.message.reply_text(
            "⚠️ Сейчас нельзя отправить локацию."
        )
        return

    if order["location_sent"]:
        await update.message.reply_text(
            "📍 Ваша локация уже была отправлена."
        )
        return

    loc = update.message.location
    driver_id = order["driver_id"]

    if driver_id is None:
        await update.message.reply_text(
            "⚠️ Водитель ещё не назначен."
        )
        return

    maps_url = (
        f"https://www.google.com/maps"
        f"?q={loc.latitude},{loc.longitude}"
    )

    await context.bot.send_location(
        chat_id=driver_id,
        latitude=loc.latitude,
        longitude=loc.longitude
    )

    await context.bot.send_message(
        chat_id=driver_id,
        text=(
            f"📍 ЛОКАЦИЯ КЛИЕНТА\n\n"
            f"🚕 Заказ #{order['id']}\n\n"
            f"🗺 Открыть карту:\n"
            f"{maps_url}"
        )
    )

    update_order(
        order["id"],
        location_sent=1,
        latitude=loc.latitude,
        longitude=loc.longitude
    )

    await update.message.reply_text(
        "✅ Локация отправлена водителю."
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.message.from_user

    order = get_active_order(user.id)

    if order is None:
        await update.message.reply_text(
            "ℹ️ У вас нет активного заказа.",
            reply_markup=start_kb()
        )
        return

    driver_id = order["driver_id"]

    if driver_id is not None:
        try:
            await context.bot.send_message(
                chat_id=driver_id,
                text=f"❌ Клиент отменил заказ #{order['id']}."
            )
        except Exception as e:
            print(
                f"Ошибка уведомления водителя: {e}"
            )

    else:
        for driver in DRIVERS:
            try:
                await context.bot.send_message(
                    chat_id=driver,
                    text=f"❌ Клиент отменил заказ #{order['id']}."
                )
            except Exception as e:
                print(
                    f"Ошибка уведомления водителя "
                    f"{driver}: {e}"
                )

    update_order(
        order["id"],
        status="cancelled"
    )

    await update.message.reply_text(
        f"❌ Заказ #{order['id']} отменён.",
        reply_markup=start_kb()
    )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text

    if text == "🚕 Заказать такси":
        await request_phone(
            update,
            context
        )

    elif text == "❌ Отменить заказ":
        await cancel(
            update,
            context
        )

    elif text == "❌ Отменить":
        await update.message.reply_text(
            "❌ Отмена.",
            reply_markup=start_kb()
        )


def main():
    if not TOKEN:
        raise ValueError(
            "❌ Не найден TOKEN в .env"
        )

    if not DRIVERS:
        raise ValueError(
            "❌ Список DRIVERS пуст"
        )

    init_db()

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    app.add_handler(
        MessageHandler(
            filters.CONTACT,
            contact
        )
    )

    app.add_handler(
        MessageHandler(
            filters.LOCATION,
            location
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    print("🚕 Бот запущен")

    app.run_polling()


if __name__ == "__main__":
    main()