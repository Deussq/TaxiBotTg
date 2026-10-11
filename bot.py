import logging
import os
import sqlite3

from dotenv import load_dotenv

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)



load_dotenv()

TOKEN = os.getenv("TOKEN")
DRIVERS_RAW = os.getenv("DRIVERS", "")
DB_FILE = "bot.db"

if not TOKEN:
    raise RuntimeError("В .env не найден TOKEN")

try:
    DRIVERS = [
        int(driver_id.strip())
        for driver_id in DRIVERS_RAW.split(",")
        if driver_id.strip()
    ]
except ValueError:
    raise RuntimeError(
        "Ошибка в DRIVERS в .env. Пример: DRIVERS=123456789,987654321"
    )

if not DRIVERS:
    raise RuntimeError("В .env не указан ни один водитель в DRIVERS")


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)



ACTIVE_STATUSES = ("new", "accepted", "on_way")



def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10,
    )
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA busy_timeout = 10000")

    return conn


def init_db():
    conn = get_db()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                telegram_id INTEGER PRIMARY KEY,
                phone TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                driver_id INTEGER,
                location_sent INTEGER DEFAULT 0,
                latitude REAL,
                longitude REAL,
                eta TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.commit()

    finally:
        conn.close()


def get_client_phone(telegram_id):
    conn = get_db()

    try:
        row = conn.execute(
            """
            SELECT phone
            FROM clients
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()

        return row["phone"] if row else None

    finally:
        conn.close()


def save_client_phone(telegram_id, phone):
    conn = get_db()

    try:
        conn.execute(
            """
            INSERT INTO clients (
                telegram_id,
                phone
            )
            VALUES (?, ?)
            ON CONFLICT(telegram_id)
            DO UPDATE SET
                phone = excluded.phone,
                updated_at = CURRENT_TIMESTAMP
            """,
            (telegram_id, phone),
        )

        conn.commit()

    finally:
        conn.close()


def create_order_db(client_id, phone):
    conn = get_db()

    try:
        cursor = conn.execute(
            """
            INSERT INTO orders (
                client_id,
                phone,
                status,
                location_sent
            )
            VALUES (?, ?, 'new', 0)
            """,
            (client_id, phone),
        )

        conn.commit()

        return cursor.lastrowid

    finally:
        conn.close()


def get_order(order_id):
    conn = get_db()

    try:
        return conn.execute(
            """
            SELECT *
            FROM orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()

    finally:
        conn.close()


def get_active_order(client_id):
    conn = get_db()

    try:
        return conn.execute(
            """
            SELECT *
            FROM orders
            WHERE client_id = ?
              AND status IN ('new', 'accepted', 'on_way')
            ORDER BY id DESC
            LIMIT 1
            """,
            (client_id,),
        ).fetchone()

    finally:
        conn.close()



def start_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🚕 Заказать такси")
            ]
        ],
        resize_keyboard=True,
    )


def phone_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📱 Отправить номер телефона",
                    request_contact=True,
                )
            ],
            [
                KeyboardButton("❌ Отменить")
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def location_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📍 Отправить локацию",
                    request_location=True,
                )
            ],
            [
                KeyboardButton("❌ Отменить заказ")
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def active_order_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("❌ Отменить заказ")
            ]
        ],
        resize_keyboard=True,
    )



def driver_order_keyboard(order_id, status="new"):
    buttons = []

    if status == "new":
        buttons.append(
            [
                InlineKeyboardButton(
                    "✅ Принять",
                    callback_data=f"accept_{order_id}",
                ),
                InlineKeyboardButton(
                    "❌ Отклонить",
                    callback_data=f"reject_{order_id}",
                ),
            ]
        )

    elif status in ("accepted", "on_way"):
        buttons.append(
            [
                InlineKeyboardButton(
                    "🚗 3 мин",
                    callback_data=f"eta_3_{order_id}",
                ),
                InlineKeyboardButton(
                    "🚗 5 мин",
                    callback_data=f"eta_5_{order_id}",
                ),
                InlineKeyboardButton(
                    "🚗 10 мин",
                    callback_data=f"eta_10_{order_id}",
                ),
                InlineKeyboardButton(
                    "🚗 15+ мин",
                    callback_data=f"eta_15+_{order_id}",
                ),
            ]
        )

        if status == "on_way":
            buttons.append(
                [
                    InlineKeyboardButton(
                        "🏁 Завершить заказ",
                        callback_data=f"finish_{order_id}",
                    )
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    "❌ Отменить заказ",
                    callback_data=f"driver_cancel_{order_id}",
                )
            ]
        )

    return InlineKeyboardMarkup(buttons)


def driver_order_text(order):
    text = (
        f"🚕 <b>Новый заказ №{order['id']}</b>\n\n"
        f"📱 Телефон: <code>{order['phone']}</code>\n"
    )

    if order["latitude"] is not None and order["longitude"] is not None:
        text += (
            f"📍 Координаты: "
            f"{order['latitude']:.6f}, {order['longitude']:.6f}\n"
            f"🗺 <a href=\"https://www.google.com/maps/search/?api=1&query="
            f"{order['latitude']},{order['longitude']}\">"
            f"Открыть на карте</a>\n"
        )

    text += "\nКто первый примет заказ — тот его получает."

    return text



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user:
        return

    await update.message.reply_text(
        "🚕 <b>Добро пожаловать в такси!</b>\n\n"
        "Нажмите кнопку ниже, чтобы заказать машину.",
        parse_mode="HTML",
        reply_markup=start_keyboard(),
    )



async def request_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user:
        return

    active_order = get_active_order(user.id)

    if active_order:
        await update.message.reply_text(
            f"🚕 У вас уже есть активный заказ №{active_order['id']}.\n\n"
            f"Статус: {active_order['status']}",
            reply_markup=active_order_keyboard(),
        )
        return

    saved_phone = get_client_phone(user.id)

    if saved_phone:
        await create_order(
            update,
            context,
            saved_phone,
        )
        return

    await update.message.reply_text(
        "📱 Для заказа такси отправьте свой номер телефона.",
        reply_markup=phone_keyboard(),
    )



async def create_order(update, context, phone):
    user = update.effective_user

    if not user:
        return


    active_order = get_active_order(user.id)

    if active_order:
        await update.message.reply_text(
            f"🚕 У вас уже есть активный заказ №{active_order['id']}.",
            reply_markup=active_order_keyboard(),
        )
        return

    save_client_phone(user.id, phone)

    order_id = create_order_db(
        client_id=user.id,
        phone=phone,
    )

    await update.message.reply_text(
        f"🚕 <b>Заказ №{order_id} создан.</b>\n\n"
        "Теперь отправьте вашу локацию, чтобы водители "
        "увидели место подачи и могли принять заказ.",
        parse_mode="HTML",
        reply_markup=location_keyboard(),
    )



async def contact_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    contact = update.message.contact

    if not user or not contact:
        return


    if contact.user_id != user.id:
        await update.message.reply_text(
            "❌ Пожалуйста, отправьте именно свой номер телефона "
            "через кнопку ниже.",
            reply_markup=phone_keyboard(),
        )
        return

    await create_order(
        update,
        context,
        contact.phone_number,
    )



async def location_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    location = update.message.location

    if not user or not location:
        return

    order = get_active_order(user.id)

    if not order:
        await update.message.reply_text(
            "❌ У вас нет активного заказа.",
            reply_markup=start_keyboard(),
        )
        return

   
    if order["status"] != "new":
        await update.message.reply_text(
            "❌ Этот заказ уже принят водителем.",
            reply_markup=active_order_keyboard(),
        )
        return

    conn = get_db()

    try:
        cursor = conn.execute(
            """
            UPDATE orders
            SET
                latitude = ?,
                longitude = ?,
                location_sent = 1
            WHERE id = ?
              AND client_id = ?
              AND status = 'new'
            """,
            (
                location.latitude,
                location.longitude,
                order["id"],
                user.id,
            ),
        )

        conn.commit()

        if cursor.rowcount != 1:
            await update.message.reply_text(
                "❌ Не удалось обновить заказ. "
                "Возможно, его уже принял другой водитель.",
                reply_markup=start_keyboard(),
            )
            return

    finally:
        conn.close()

    order = get_order(order["id"])

  
    driver_text = driver_order_text(order)

    for driver_id in DRIVERS:
        try:
            await context.bot.send_message(
                chat_id=driver_id,
                text=driver_text,
                parse_mode="HTML",
                disable_web_page_preview=False,
                reply_markup=driver_order_keyboard(
                    order["id"],
                    "new",
                ),
            )

        except Exception as e:
            logger.error(
                "Не удалось отправить заказ водителю %s: %s",
                driver_id,
                e,
            )

    await update.message.reply_text(
        f"📍 Локация получена.\n\n"
        f"🚕 <b>Заказ №{order['id']} отправлен водителям.</b>\n\n"
        "Ожидайте подтверждения.",
        parse_mode="HTML",
        reply_markup=active_order_keyboard(),
    )




def parse_callback(data):
    parts = data.split("_")

    if len(parts) < 2:
        return None, None

    action = parts[0]

    try:
        if action == "eta":
            if len(parts) < 3:
                return None, None

            eta = parts[1]
            order_id = int(parts[2])

            return f"eta:{eta}", order_id

        if action == "driver":
            if len(parts) < 3:
                return None, None

            if parts[1] != "cancel":
                return None, None

            order_id = int(parts[2])

            return "driver_cancel", order_id

        order_id = int(parts[1])

        return action, order_id

    except (ValueError, IndexError):
        return None, None



async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    driver_id = query.from_user.id

 

    if driver_id not in DRIVERS:
        await query.answer(
            "❌ У вас нет доступа.",
            show_alert=True,
        )
        return

    action, order_id = parse_callback(query.data)

    if not action or not order_id:
        await query.answer(
            "❌ Некорректная команда.",
            show_alert=True,
        )
        return

  

    if action == "accept":
        conn = get_db()

        try:
        
            cursor = conn.execute(
                """
                UPDATE orders
                SET
                    status = 'accepted',
                    driver_id = ?
                WHERE id = ?
                  AND status = 'new'
                  AND driver_id IS NULL
                  AND location_sent = 1
                """,
                (
                    driver_id,
                    order_id,
                ),
            )

            conn.commit()

            accepted = cursor.rowcount == 1

        finally:
            conn.close()

        if not accepted:
            await query.answer(
                "🚫 Заказ уже принят другим водителем.",
                show_alert=True,
            )

           
            try:
                await query.edit_message_reply_markup(
                    reply_markup=None
                )
            except Exception:
                pass

            return

        order = get_order(order_id)

        if not order:
            await query.answer(
                "❌ Заказ не найден.",
                show_alert=True,
            )
            return

        await query.answer("✅ Заказ принят!")

   
        try:
            await context.bot.send_message(
                chat_id=order["client_id"],
                text=(
                    f"🚕 <b>Заказ №{order_id} принят водителем.</b>\n\n"
                    "Водитель сейчас сообщит время прибытия."
                ),
                parse_mode="HTML",
                reply_markup=active_order_keyboard(),
            )
        except Exception as e:
            logger.error(
                "Ошибка отправки клиенту %s: %s",
                order["client_id"],
                e,
            )

       
        try:
            await query.edit_message_text(
                text=(
                    f"✅ <b>Заказ №{order_id} принят вами.</b>\n\n"
                    f"📱 Телефон: <code>{order['phone']}</code>\n"
                    f"📍 Координаты: "
                    f"{order['latitude']:.6f}, "
                    f"{order['longitude']:.6f}\n\n"
                    "Теперь выберите примерное время прибытия."
                ),
                parse_mode="HTML",
                reply_markup=driver_order_keyboard(
                    order_id,
                    "accepted",
                ),
            )
        except Exception as e:
            logger.error(
                "Ошибка обновления сообщения водителя: %s",
                e,
            )

        for other_driver_id in DRIVERS:
            if other_driver_id == driver_id:
                continue

            try:
                await context.bot.send_message(
                    chat_id=other_driver_id,
                    text=(
                        f"🚫 <b>Заказ №{order_id} уже принят "
                        "другим водителем.</b>"
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(
                    "Не удалось уведомить водителя %s: %s",
                    other_driver_id,
                    e,
                )

        return


    if action == "reject":
        order = get_order(order_id)

        if not order:
            await query.answer(
                "❌ Заказ не найден.",
                show_alert=True,
            )
            return

        if order["status"] != "new":
            await query.answer(
                "🚫 Заказ уже недоступен.",
                show_alert=True,
            )

            try:
                await query.edit_message_reply_markup(
                    reply_markup=None
                )
            except Exception:
                pass

            return

  
        await query.answer("Заказ отклонён.")

        try:
            await query.edit_message_text(
                text=(
                    f"❌ <b>Заказ №{order_id} отклонён вами.</b>\n\n"
                    "Другие водители всё ещё могут его принять."
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(
                "Ошибка редактирования сообщения после reject: %s",
                e,
            )

        return


    if action.startswith("eta:"):
        eta = action.split(":", 1)[1]

        order = get_order(order_id)

        if not order:
            await query.answer(
                "❌ Заказ не найден.",
                show_alert=True,
            )
            return

    
        if order["driver_id"] != driver_id:
            await query.answer(
                "❌ Этот заказ назначен другому водителю.",
                show_alert=True,
            )
            return

        if order["status"] == "accepted":

            conn = get_db()

            try:
                cursor = conn.execute(
                    """
                    UPDATE orders
                    SET
                        eta = ?,
                        status = 'on_way'
                    WHERE id = ?
                      AND driver_id = ?
                      AND status = 'accepted'
                    """,
                    (
                        eta,
                        order_id,
                        driver_id,
                    ),
                )

                conn.commit()

                success = cursor.rowcount == 1

            finally:
                conn.close()

            if not success:
                await query.answer(
                    "❌ Заказ уже изменился.",
                    show_alert=True,
                )
                return

            await query.answer(
                "🚗 Вы выехали!"
            )

            try:
                await context.bot.send_message(
                    chat_id=order["client_id"],
                    text=(
                        f"🚗 <b>Водитель выехал.</b>\n\n"
                        f"⏱ Примерное время прибытия: <b>{eta} мин.</b>"
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(
                    "Ошибка отправки ETA клиенту: %s",
                    e,
                )

            try:
                await query.edit_message_reply_markup(
                    reply_markup=driver_order_keyboard(
                        order_id,
                        "on_way",
                    )
                )
            except Exception:
                pass

            return

      
        if order["status"] == "on_way":

            conn = get_db()

            try:
                cursor = conn.execute(
                    """
                    UPDATE orders
                    SET eta = ?
                    WHERE id = ?
                      AND driver_id = ?
                      AND status = 'on_way'
                    """,
                    (
                        eta,
                        order_id,
                        driver_id,
                    ),
                )

                conn.commit()

                success = cursor.rowcount == 1

            finally:
                conn.close()

            if not success:
                await query.answer(
                    "❌ Не удалось изменить время.",
                    show_alert=True,
                )
                return

            await query.answer(
                f"⏱ ETA изменено: {eta} мин."
            )

            try:
                await context.bot.send_message(
                    chat_id=order["client_id"],
                    text=(
                        f"⏱ <b>Водитель обновил время прибытия.</b>\n\n"
                        f"Осталось примерно <b>{eta} мин.</b>"
                    ),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(
                    "Ошибка отправки нового ETA клиенту: %s",
                    e,
                )

            return

        await query.answer(
            "🚫 Этот заказ больше не активен.",
            show_alert=True,
        )
        return

  
    if action == "finish":
        order = get_order(order_id)

        if not order:
            await query.answer(
                "❌ Заказ не найден.",
                show_alert=True,
            )
            return

        if order["driver_id"] != driver_id:
            await query.answer(
                "❌ Этот заказ назначен другому водителю.",
                show_alert=True,
            )
            return

        conn = get_db()

        try:
            cursor = conn.execute(
                """
                UPDATE orders
                SET status = 'finished'
                WHERE id = ?
                  AND driver_id = ?
                  AND status = 'on_way'
                """,
                (
                    order_id,
                    driver_id,
                ),
            )

            conn.commit()

            success = cursor.rowcount == 1

        finally:
            conn.close()

        if not success:
            await query.answer(
                "🚫 Заказ уже завершён или недоступен.",
                show_alert=True,
            )
            return

        await query.answer("🏁 Заказ завершён.")

        try:
            await context.bot.send_message(
                chat_id=order["client_id"],
                text=(
                    f"🏁 <b>Заказ №{order_id} завершён.</b>\n\n"
                    "Спасибо за поездку! 🚕"
                ),
                parse_mode="HTML",
                reply_markup=start_keyboard(),
            )
        except Exception as e:
            logger.error(
                "Ошибка уведомления клиента о завершении: %s",
                e,
            )

        try:
            await query.edit_message_text(
                text=(
                    f"🏁 <b>Заказ №{order_id} завершён.</b>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        return

  =======================================================

    if action == "driver_cancel":
        order = get_order(order_id)

        if not order:
            await query.answer(
                "❌ Заказ не найден.",
                show_alert=True,
            )
            return

        if order["driver_id"] != driver_id:
            await query.answer(
                "❌ Этот заказ назначен другому водителю.",
                show_alert=True,
            )
            return

        conn = get_db()

        try:
            cursor = conn.execute(
                """
                UPDATE orders
                SET status = 'cancelled'
                WHERE id = ?
                  AND driver_id = ?
                  AND status IN ('accepted', 'on_way')
                """,
                (
                    order_id,
                    driver_id,
                ),
            )

            conn.commit()

            success = cursor.rowcount == 1

        finally:
            conn.close()

        if not success:
            await query.answer(
                "🚫 Заказ уже завершён или отменён.",
                show_alert=True,
            )
            return

        await query.answer("❌ Заказ отменён.")

        try:
            await context.bot.send_message(
                chat_id=order["client_id"],
                text=(
                    f"❌ <b>Водитель отменил заказ №{order_id}.</b>\n\n"
                    "Вы можете оформить новый заказ."
                ),
                parse_mode="HTML",
                reply_markup=start_keyboard(),
            )
        except Exception as e:
            logger.error(
                "Ошибка уведомления клиента об отмене: %s",
                e,
            )

        try:
            await query.edit_message_text(
                text=(
                    f"❌ <b>Заказ №{order_id} отменён.</b>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        return

    
    await query.answer(
        "❌ Неизвестная команда.",
        show_alert=True,
    )



async def cancel_order(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user:
        return

    order = get_active_order(user.id)

    if not order:
        await update.message.reply_text(
            "У вас нет активного заказа.",
            reply_markup=start_keyboard(),
        )
        return

    conn = get_db()

    try:
        cursor = conn.execute(
            """
            UPDATE orders
            SET status = 'cancelled'
            WHERE id = ?
              AND client_id = ?
              AND status IN ('new', 'accepted', 'on_way')
            """,
            (
                order["id"],
                user.id,
            ),
        )

        conn.commit()

        success = cursor.rowcount == 1

    finally:
        conn.close()

    if not success:
        await update.message.reply_text(
            "❌ Не удалось отменить заказ. "
            "Возможно, его статус уже изменился.",
            reply_markup=start_keyboard(),
        )
        return

  
    if order["driver_id"] is not None:
        try:
            await context.bot.send_message(
                chat_id=order["driver_id"],
                text=(
                    f"❌ Клиент отменил заказ №{order['id']}."
                ),
            )
        except Exception as e:
            logger.error(
                "Не удалось уведомить водителя %s: %s",
                order["driver_id"],
                e,
            )

    else:
      
        for driver_id in DRIVERS:
            try:
                await context.bot.send_message(
                    chat_id=driver_id,
                    text=(
                        f"❌ Заказ №{order['id']} отменён клиентом."
                    ),
                )
            except Exception as e:
                logger.error(
                    "Не удалось уведомить водителя %s: %s",
                    driver_id,
                    e,
                )

    await update.message.reply_text(
        f"❌ <b>Заказ №{order['id']} отменён.</b>\n\n"
        "Если понадобится машина — можете оформить новый заказ.",
        parse_mode="HTML",
        reply_markup=start_keyboard(),
    )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message:
        return

    text = update.message.text

    if text == "🚕 Заказать такси":
        await request_phone(update, context)
        return

    if text == "❌ Отменить заказ":
        await cancel_order(update, context)
        return

    if text == "❌ Отменить":
        await update.message.reply_text(
            "Заказ не создан.",
            reply_markup=start_keyboard(),
        )
        return



async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Ошибка при обработке обновления:",
        exc_info=context.error,
    )



def main():
    init_db()

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

 
    application.add_handler(
        CommandHandler("start", start)
    )

    
    application.add_handler(
        MessageHandler(
            filters.CONTACT,
            contact_handler,
        )
    )


    application.add_handler(
        MessageHandler(
            filters.LOCATION,
            location_handler,
        )
    )


    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )


    application.add_error_handler(error_handler)

    logger.info("Бот запускается...")
    logger.info("Водители: %s", DRIVERS)

    application.run_polling()


if __name__ == "__main__":
    main()