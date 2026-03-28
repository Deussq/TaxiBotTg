from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

from dotenv import load_dotenv
import os

load_dotenv()

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("TOKEN не найден в .env")
    
YOUR_USERNAME = os.getenv("YOUR_USERNAME")


DRIVERS = list(map(int, os.getenv("DRIVERS").split(",")))


last_client_id = None



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["🚕 Заказать такси"]]

    await update.message.reply_text(
        "Добро пожаловать! Нажмите кнопку ниже, чтобы заказать такси.",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )



async def order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_client_id

    text = update.message.text
    user = update.message.from_user


    if text != "🚕 Заказать такси":
        return

    last_client_id = user.id


    if user.username:
        contact_link = f"https://t.me/{user.username}"

        driver_text = f"""
🚨 Новый заказ!

Имя: {user.first_name}
Username: @{user.username}
ID: {user.id}

Связаться:
{contact_link}
"""

        for driver in DRIVERS:
            await context.bot.send_message(driver, driver_text)

        await update.message.reply_text(
            "Ваш заказ отправлен водителю. Ожидайте сообщение."
        )


    else:
        driver_text = f"""
🚨 Новый заказ!

Имя: {user.first_name}
Username: нет
ID: {user.id}

У клиента нет username.
Мы отправили ему профиль водителя.
"""

        for driver in DRIVERS:
            await context.bot.send_message(driver, driver_text)

        await update.message.reply_text(
            f"""
У вас нет username в Telegram.

Поэтому водитель не может написать вам напрямую.

Пожалуйста напишите водителю:
https://t.me/{YOUR_USERNAME}
"""
        )



async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_client_id

    user_id = update.message.from_user.id
    text = update.message.text


    if user_id in DRIVERS:
        if last_client_id is None:
            await update.message.reply_text("Нет активных заказов.")
            return

        await context.bot.send_message(
            last_client_id,
            f"🚖 Водитель:\n{text}"
        )

        await update.message.reply_text("Сообщение отправлено клиенту.")


app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, order))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler), group=1)

print("Бот запущен...")
app.run_polling()