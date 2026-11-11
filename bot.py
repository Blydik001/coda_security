import asyncio
import logging
import secrets
import string
import hashlib
import aiomysql
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

BOT_TOKEN = "9202159519:AAFXY2G_oCBLtDKkGNzQZpZqyvhEccsWRhI"

DB_CONFIG = {
    "host": "80.242.59.112",
    "user": "gs330867",
    "password": "HeZ5icJZQtXW",
    "db": "gs330867",
    "autocommit": True,
    "pool_recycle": 280
}

SERVER_NAME = "TEST SERVER"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool: aiomysql.Pool = None
active_timers: dict[int, asyncio.Task] = {}


class BotStates(StatesGroup):
    waiting_code = State()
    choosing_acc_for_reset = State()


def get_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Привязать аккаунт")],
            [KeyboardButton(text="Список аккаунтов"), KeyboardButton(text="Сброс пароля")]
        ],
        resize_keyboard=True
    )


async def expire_reg_code(account_id: int, code: int):
    await asyncio.sleep(300)
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE accounts SET tg_regcode = 0, tg_get = 0 WHERE id = %s AND tg_regcode = %s AND tg_get = 1",
                    (account_id, code)
                )
    except Exception as e:
        logging.error(f"Expire task error: {e}")
    finally:
        active_timers.pop(account_id, None)


async def monitor_pending_links():
    while True:
        try:
            async with db_pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT id, tg_regcode FROM accounts WHERE tg_get = 1 AND tg_regcode > 0")
                    rows = await cur.fetchall()
                    for row in rows:
                        acc_id = row["id"]
                        reg_code = row["tg_regcode"]
                        if acc_id not in active_timers or active_timers[acc_id].done():
                            task = asyncio.create_task(expire_reg_code(acc_id, reg_code))
                            active_timers[acc_id] = task
        except Exception as e:
            logging.error(f"DB monitor error: {e}")
        await asyncio.sleep(3)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Выберите действие в меню:", reply_markup=get_main_kb())


@dp.message(F.text == "Привязать аккаунт")
async def start_linking(message: Message, state: FSMContext):
    await state.set_state(BotStates.waiting_code)
    await message.answer("Введите код привязки, полученный в игре:", reply_markup=ReplyKeyboardRemove())


@dp.message(BotStates.waiting_code)
async def process_link_code(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("Код должен состоять только из цифр. Попробуйте еще раз или нажмите /start.")
        return

    code = int(message.text)
    if code <= 0:
        await message.answer("Неверный код.")
        return

    async with db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, name FROM accounts WHERE tg_regcode = %s AND tg_get = 1 LIMIT 1",
                (code,)
            )
            account = await cur.fetchone()

            if not account:
                await message.answer("Код не найден или его срок действия истек.", reply_markup=get_main_kb())
                await state.clear()
                return

            acc_id = account["id"]
            await cur.execute(
                "UPDATE accounts SET tg_id = %s, tg_regcode = 0, tg_get = 0 WHERE id = %s",
                (message.from_user.id, acc_id)
            )

            task = active_timers.pop(acc_id, None)
            if task and not task.done():
                task.cancel()

    await state.clear()
    await message.answer(
        f"Аккаунт {account['name']} успешно привязан к вашему Telegram.",
        reply_markup=get_main_kb()
    )


@dp.message(F.text == "Список аккаунтов")
async def list_accounts(message: Message):
    async with db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT name FROM accounts WHERE tg_id = %s", (message.from_user.id,))
            rows = await cur.fetchall()

    if not rows:
        await message.answer("У вас нет привязанных игровых аккаунтов.")
        return

    lines = [f"{row['name']} + {SERVER_NAME}" for row in rows]
    await message.answer("Привязанные аккаунты:\n" + "\n".join(lines))


@dp.message(F.text == "Сброс пароля")
async def reset_password_start(message: Message, state: FSMContext):
    async with db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT id, name FROM accounts WHERE tg_id = %s", (message.from_user.id,))
            rows = await cur.fetchall()

    if not rows:
        await message.answer("У вас нет привязанных игровых аккаунтов.")
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=row["name"])] for row in rows] + [[KeyboardButton(text="Отмена")]],
        resize_keyboard=True
    )
    await state.set_state(BotStates.choosing_acc_for_reset)
    await message.answer("Выберите аккаунт для сброса пароля:", reply_markup=kb)


@dp.message(BotStates.choosing_acc_for_reset)
async def reset_password_confirm(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=get_main_kb())
        return

    nick = message.text.strip()
    alphabet = string.ascii_letters + string.digits
    raw_password = "".join(secrets.choice(alphabet) for _ in range(10))
    hashed_password = hashlib.sha256(raw_password.encode()).hexdigest()

    async with db_pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id FROM accounts WHERE name = %s AND tg_id = %s LIMIT 1",
                (nick, message.from_user.id)
            )
            account = await cur.fetchone()

            if not account:
                await message.answer("Аккаунт не найден или не принадлежит вам.", reply_markup=get_main_kb())
                await state.clear()
                return

            await cur.execute(
                "UPDATE accounts SET password = %s WHERE id = %s",
                (hashed_password, account["id"])
            )

    await state.clear()
    await message.answer(
        f"Пароль для аккаунта {nick} успешно сброшен.\nНовый пароль: `{raw_password}`",
        parse_mode="Markdown",
        reply_markup=get_main_kb()
    )


async def main():
    global db_pool
    db_pool = await aiomysql.create_pool(**DB_CONFIG)
    asyncio.create_task(monitor_pending_links())
    try:
        await dp.start_polling(bot)
    finally:
        db_pool.close()
        await db_pool.wait_closed()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
