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
    filters,
    ContextTypes
)
from dotenv import load_dotenv
import os


load_dotenv()

TOKEN = os.getenv("TOKEN")
YOUR_USERNAME = os.getenv("YOUR_USERNAME")  
DRIVERS = list(map(int, os.getenv("DRIVERS").split(",")))

active_orders = set()


def main_menu():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🚕 Заказать такси")]],
        resize_keyboard=True
    )


def order_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Отправить локацию", request_location=True)],
            [KeyboardButton("❌ Отменить заказ")]
        ],
        resize_keyboard=True
    )


def cancel_menu():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("❌ Отменить заказ")]],
        resize_keyboard=True
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚕 Добро пожаловать!\nНажмите кнопку чтобы заказать такси.",
        reply_markup=main_menu()
    )



async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text


    if text == "🚕 Заказать такси":
        active_orders.add(user.id)

        msg = f"""
🚨 НОВЫЙ ЗАКАЗ

Имя: {user.first_name}
Username: @{user.username if user.username else "нет"}
ID: {user.id}
"""

        for d in DRIVERS:
            await context.bot.send_message(d, msg)


        if not user.username:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📞 Связаться с водителем", url=f"https://t.me/{YOUR_USERNAME}")]
            ])

            await update.message.reply_text(
                "⚠️ У вас нет username в Telegram.\n"
                "Свяжитесь с водителем напрямую:",
                reply_markup=kb
            )

        await update.message.reply_text(
            "✅ Заказ принят!\nОтправьте локацию или отмените заказ.",
            reply_markup=order_menu()
        )
        return


    if text == "❌ Отменить заказ":
        if user.id in active_orders:
            active_orders.remove(user.id)

            for d in DRIVERS:
                await context.bot.send_message(
                    d,
                    f"❌ ЗАКАЗ ОТМЕНЁН\nИмя: {user.first_name}\nID: {user.id}"
                )

        await update.message.reply_text(
            "❌ Заказ отменён.\nВы можете заказать снова.",
            reply_markup=main_menu()
        )
        return


    if user.id in DRIVERS:
        if not active_orders:
            await update.message.reply_text("Нет активных заказов.")
            return

        client_id = list(active_orders)[0]

        await context.bot.send_message(
            client_id,
            f"🚖 Водитель:\n{text}"
        )



async def location_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    loc = update.message.location

    if user.id not in active_orders:
        return

    msg = f"""
📍 ЛОКАЦИЯ

https://www.google.com/maps?q={loc.latitude},{loc.longitude}
"""

    for d in DRIVERS:
        await context.bot.send_message(d, msg)


    await update.message.reply_text(
        "📍 Локация отправлена.\nВы можете отменить заказ в любой момент.",
        reply_markup=cancel_menu()
    )



app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
app.add_handler(MessageHandler(filters.LOCATION, location_handler))

app.run_polling()

