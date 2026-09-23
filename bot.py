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
import os

load_dotenv()

TOKEN = os.getenv("TOKEN")
YOUR_USERNAME = os.getenv("YOUR_USERNAME")
DRIVERS = list(map(int, os.getenv("DRIVERS").split(",")))

orders = {}
order_counter = 1


def start_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🚕 Заказать такси")]
        ],
        resize_keyboard=True
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


def driver_kb(order_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚗 Выехал",
                callback_data=f"onway_{order_id}"
            )
        ],
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
        ],
        [
            InlineKeyboardButton(
                "🚗 Завершить поездку",
                callback_data=f"finish_{order_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Отменить заказ",
                callback_data=f"driver_cancel_{order_id}"
            )
        ]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚕 Добро пожаловать!\n\n"
        "Нажмите кнопку ниже, чтобы заказать поездку.",
        reply_markup=start_kb()
    )


async def create_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global order_counter

    user = update.message.from_user

    for order in orders.values():
        if (
            order["client_id"] == user.id
            and order["status"] not in ["cancelled", "finished"]
        ):
            await update.message.reply_text(
                "⚠️ У вас уже есть активный заказ.",
                reply_markup=active_kb()
            )
            return

    order_id = order_counter
    order_counter += 1

    orders[order_id] = {
        "client_id": user.id,
        "status": "new",
        "driver_id": None,
        "location_sent": False,
    }

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

    if order_id not in orders:
        await query.answer(
            "⚠️ Заказ уже неактуален.",
            show_alert=True
        )
        return

    order = orders[order_id]
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

        order["driver_id"] = driver_id
        order["status"] = "accepted"

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
                f"👤 Клиент: ID {order['client_id']}\n\n"
                f"Ожидайте локацию клиента."
            ),
            reply_markup=driver_kb(order_id)
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
        "onway",
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

    if action == "onway":
        if order["status"] != "accepted":
            await query.answer(
                "⚠️ Сейчас нельзя отметить выезд.",
                show_alert=True
            )
            return

        order["status"] = "on_way"

        await context.bot.send_message(
            chat_id=order["client_id"],
            text="🚗 Водитель уже едет к вам!"
        )

        await query.edit_message_text(
            text=(
                f"🚗 Вы выехали к клиенту\n\n"
                f"Заказ #{order_id}"
            ),
            reply_markup=driver_kb(order_id)
        )

        await query.answer(
            "🚗 Статус изменён: вы в пути."
        )

        return

    if action == "eta":
        if order["status"] not in ["accepted", "on_way"]:
            await query.answer(
                "⚠️ Сейчас нельзя установить ETA.",
                show_alert=True
            )
            return

        order["status"] = "on_way"

        await context.bot.send_message(
            chat_id=order["client_id"],
            text=(
                f"⏱ Водитель приедет "
                f"примерно через {extra} минут."
            )
        )

        await query.edit_message_text(
            text=(
                f"⏱ ETA: {extra} мин.\n\n"
                f"Заказ #{order_id}"
            ),
            reply_markup=driver_kb(order_id)
        )

        await query.answer(
            f"ETA установлено: {extra} мин."
        )

        return

    if action == "finish":
        if order["status"] != "on_way":
            await query.answer(
                "⚠️ Сначала водитель должен быть в пути.",
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

        await query.edit_message_text(
            text=f"✅ Поездка завершена #{order_id}"
        )

        order["status"] = "finished"

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

        order["status"] = "cancelled"

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

    for oid, order in orders.items():

        if order["client_id"] != user.id:
            continue

        if order["status"] not in ["accepted", "on_way"]:
            continue

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
                f"🚕 Заказ #{oid}\n\n"
                f"🗺 Открыть карту:\n"
                f"{maps_url}"
            )
        )

        order["location_sent"] = True

        await update.message.reply_text(
            "✅ Локация отправлена водителю."
        )

        return

    await update.message.reply_text(
        "⚠️ У вас нет активного заказа."
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.message.from_user

    for oid, order in list(orders.items()):

        if order["client_id"] != user.id:
            continue

        if order["status"] in ["finished", "cancelled"]:
            continue

        driver_id = order["driver_id"]

        if driver_id is not None:
            try:
                await context.bot.send_message(
                    chat_id=driver_id,
                    text=f"❌ Клиент отменил заказ #{oid}."
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
                        text=f"❌ Клиент отменил заказ #{oid}."
                    )
                except Exception as e:
                    print(
                        f"Ошибка уведомления водителя "
                        f"{driver}: {e}"
                    )

        order["status"] = "cancelled"

        await update.message.reply_text(
            f"❌ Заказ #{oid} отменён.",
            reply_markup=start_kb()
        )

        return

    await update.message.reply_text(
        "ℹ️ У вас нет активного заказа.",
        reply_markup=start_kb()
    )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text

    if text == "🚕 Заказать такси":
        await create_order(
            update,
            context
        )

    elif text == "❌ Отменить заказ":
        await cancel(
            update,
            context
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