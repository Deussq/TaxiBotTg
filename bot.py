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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚕 Добро пожаловать! Нажмите кнопку ниже:",
        reply_markup=start_kb()
    )



async def create_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    orders[user.id] = {
        "status": "new",
        "driver_id": None
    }

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Принять", callback_data=f"accept_{user.id}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{user.id}")]
    ])

    msg = f"""
🚨 НОВЫЙ ЗАКАЗ

Имя: {user.first_name}
Username: @{user.username if user.username else "нет"}
ID: {user.id}
"""

    for d in DRIVERS:
        await context.bot.send_message(d, msg, reply_markup=keyboard)


    if not user.username:
        await update.message.reply_text(
            "⚠️ У вас нет username в Telegram.\n"
            "Свяжитесь с водителем напрямую:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📞 Написать водителю", url=f"https://t.me/{YOUR_USERNAME}")]
            ])
        )

    await update.message.reply_text(
        "✅ Заказ принят! Ожидайте водителя.",
        reply_markup=active_kb()
    )



async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    if user.id in orders:
        orders.pop(user.id)

        for d in DRIVERS:
            await context.bot.send_message(
                d,
                f"❌ ЗАКАЗ ОТМЕНЁН\nИмя: {user.first_name}\nID: {user.id}"
            )

    await update.message.reply_text(
        "❌ Заказ отменён",
        reply_markup=start_kb()
    )



async def location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    if user.id not in orders:
        await update.message.reply_text("❗ Сначала сделайте заказ")
        return

    if orders[user.id]["status"] != "accepted":
        await update.message.reply_text("❗ Водитель ещё не принял заказ")
        return

    loc = update.message.location

    msg = f"📍 ЛОКАЦИЯ:\nhttps://www.google.com/maps?q={loc.latitude},{loc.longitude}"

    for d in DRIVERS:
        await context.bot.send_message(d, msg)

    orders[user.id]["status"] = "finished"

    await update.message.reply_text(
        "📍 Локация отправлена",
        reply_markup=start_kb()
    )



async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, client_id = query.data.split("_")
    client_id = int(client_id)

    if client_id not in orders:
        await query.edit_message_text("⚠️ Заказ уже неактуален")
        return


    if action == "accept":
        orders[client_id]["status"] = "accepted"
        orders[client_id]["driver_id"] = query.from_user.id

        await context.bot.send_message(
            client_id,
            f"🚗 Водитель принял заказ!\n👤 {query.from_user.first_name}"
        )

        await query.edit_message_text("✅ Принято")


    elif action == "reject":
        orders.pop(client_id)

        await context.bot.send_message(
            client_id,
            "❌ Водитель отклонил заказ.\nПопробуйте снова."
        )

        await query.edit_message_text("❌ Отклонено")



async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🚕 Заказать такси":
        await create_order(update, context)

    elif text == "❌ Отменить заказ":
        await cancel(update, context)



app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(callback))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
app.add_handler(MessageHandler(filters.LOCATION, location))

app.run_polling()