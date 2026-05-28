from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
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
        [[KeyboardButton("🚕 Заказать такси")]],
        resize_keyboard=True
    )


def active_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Отправить локацию", request_location=True)],
            [KeyboardButton("❌ Отменить заказ")]
        ],
        resize_keyboard=True
    )


def cancel_only_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Отменить заказ")]],
        resize_keyboard=True
    )


def driver_kb(order_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Отменить заказ",
                callback_data=f"driver_cancel_{order_id}"
            )
        ]
    ])



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚕 Добро пожаловать!\nНажмите кнопку ниже:",
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
                "⚠️ У вас уже есть активный заказ",
                reply_markup=active_kb()
            )
            return

    order_id = order_counter
    order_counter += 1

    orders[order_id] = {
        "client_id": user.id,
        "status": "new",
        "driver_id": None,
        "location_sent": False
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

    msg = (
        f"🚨 НОВЫЙ ЗАКАЗ #{order_id}\n\n"
        f"👤 Имя: {user.first_name}\n"
        f"🆔 ID: {user.id}\n"
        f"👤 Username: "
        f"@{user.username if user.username else 'нет'}"
    )

    for driver in DRIVERS:
        await context.bot.send_message(
            driver,
            msg,
            reply_markup=keyboard
        )


    if not user.username:
        await update.message.reply_text(
            "⚠️ У вас нет username в Telegram.\n\n"
            "Пожалуйста, свяжитесь с водителем сами:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "📞 Написать водителю",
                        url=f"https://t.me/{YOUR_USERNAME}"
                    )
                ]
            ])
        )

    await update.message.reply_text(
        f"✅ Заказ #{order_id} создан.\n"
        f"Ожидайте принятия водителем.",
        reply_markup=active_kb()
    )



async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")


    if parts[0] == "driver":
        action = "driver_cancel"
        order_id = int(parts[2])
    else:
        action = parts[0]
        order_id = int(parts[1])

    if order_id not in orders:
        await query.answer(
            "⚠️ Заказ уже неактуален",
            show_alert=True
        )
        return

    order = orders[order_id]


    if (
        order["status"] == "accepted"
        and action != "driver_cancel"
    ):
        if order["driver_id"] == query.from_user.id:
            await query.answer(
                "Вы уже приняли этот заказ"
            )
        else:
            await query.answer(
                "🚫 Заказ уже принят другим водителем",
                show_alert=True
            )
        return


    if action == "accept":

        order["status"] = "accepted"
        order["driver_id"] = query.from_user.id

        await context.bot.send_message(
            order["client_id"],
            "🚗 Водитель принял заказ!\n\n"
            "📍 Теперь отправьте локацию.",
            reply_markup=active_kb()
        )

        await query.edit_message_text(
            f"✅ Вы приняли заказ #{order_id}",
            reply_markup=driver_kb(order_id)
        )


    elif action == "reject":

        await context.bot.send_message(
            order["client_id"],
            "❌ Водитель отклонил заказ.\n"
            "Пожалуйста, закажите заново.",
            reply_markup=start_kb()
        )

        del orders[order_id]

        await query.edit_message_text(
            f"❌ Заказ #{order_id} отклонён"
        )


    elif action == "driver_cancel":

        client_id = order["client_id"]

        await context.bot.send_message(
            client_id,
            "❌ Водитель отменил заказ.\n\n"
            "Пожалуйста, закажите такси заново.",
            reply_markup=start_kb()
        )

        del orders[order_id]

        await query.edit_message_text(
            f"❌ Заказ #{order_id} отменён водителем"
        )



async def location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    for oid, order in list(orders.items()):

        if order["client_id"] == user.id:

            if order["status"] != "accepted":
                await update.message.reply_text(
                    "⏳ Водитель ещё не принял заказ",
                    reply_markup=active_kb()
                )
                return

            if order["location_sent"]:
                await update.message.reply_text(
                    "📍 Вы уже отправили локацию",
                    reply_markup=cancel_only_kb()
                )
                return

            loc = update.message.location

            msg = (
                f"📍 ЛОКАЦИЯ КЛИЕНТА\n\n"
                f"Заказ #{oid}\n"
                f"https://www.google.com/maps?q="
                f"{loc.latitude},{loc.longitude}"
            )

            for driver in DRIVERS:
                await context.bot.send_message(
                    driver,
                    msg
                )

            order["location_sent"] = True

            await update.message.reply_text(
                "✅ Локация отправлена водителю",
                reply_markup=cancel_only_kb()
            )
            return

    await update.message.reply_text(
        "⚠️ У вас нет активного заказа.\n"
        "Создайте новый 👇",
        reply_markup=start_kb()
    )



async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    for oid, order in list(orders.items()):

        if order["client_id"] == user.id:

            for driver in DRIVERS:
                await context.bot.send_message(
                    driver,
                    f"❌ Клиент "
                    f"{user.first_name} "
                    f"отменил заказ #{oid}"
                )

            del orders[oid]

            await update.message.reply_text(
                "❌ Заказ отменён",
                reply_markup=start_kb()
            )
            return

    await update.message.reply_text(
        "⚠️ У вас нет активного заказа",
        reply_markup=start_kb()
    )



async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🚕 Заказать такси":
        await create_order(update, context)

    elif text == "❌ Отменить заказ":
        await cancel(update, context)



app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(callback))
app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_handler
    )
)
app.add_handler(
    MessageHandler(
        filters.LOCATION,
        location
    )
)

print("🚕 Бот запущен")
app.run_polling()