
import asyncio
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from dotenv import load_dotenv
import os

load_dotenv()

TOKEN = os.getenv("TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

async def main():
    bot = Bot(TOKEN)
    keyboard = [
        [InlineKeyboardButton("🚕 Заказать такси", url="https://t.me/taxivezubot")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await bot.send_message(
        chat_id=CHANNEL_ID,
        text="Хотите заказать такси? Нажмите кнопку ниже! 🚕",
        reply_markup=reply_markup
    )


asyncio.run(main())