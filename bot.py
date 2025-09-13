#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import os
import random
import re
from typing import List, Dict, Any

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from supabase import create_client  # supabase-py

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------- КОНФИГ (из окружения) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")  # service_role для записи
TASKS_TABLE = os.getenv("TASKS_TABLE", "tasks")
CODES_TABLE = os.getenv("CODES_TABLE", "codes")

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    logger.critical("Нужны переменные окружения: BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY")
    raise SystemExit(1)

# ---------- Инициализация Supabase ----------
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- Инициализация бота ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- Утилита нормализации ответов ----------
def normalize_answer(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.lower().strip()
    # Удаляем знаки градусов, пробелы и пунктуацию
    s = re.sub(r"[°º\^•××\*\/\\\.,;:!?\-\(\)\[\]\"']", "", s)
    s = re.sub(r"\s+", "", s)
    return s

# ---------- Сессии пользователей (в памяти) ----------
user_sessions: Dict[int, Dict[str, Any]] = {}
# Формат session: {
#   "tasks": [ {...} ],
#   "current": int,
#   "correct": int,
#   "wrong": [ (question, explanation) ],
#   "waiting_text": bool
# }

# ---------- Главное меню ----------
async def main_menu(user_id: int):
    user_sessions.pop(user_id, None)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Пройти тестирование", callback_data="start_test")]
    ])
    await bot.send_message(user_id, "🏠 Главное меню", reply_markup=kb)

# ---------- Регистрация по коду (таблица CODES_TABLE) ----------
@dp.message(F.text.regexp(r"^[A-Z0-9]{4,10}$"))  # допускаем 4-10 символов (A-Z0-9)
async def register_user(message: Message):
    code = message.text.strip()
    user_id = message.from_user.id

    # попробуем найти код в Supabase
    res = supabase.table(CODES_TABLE).select("*").eq("code", code).maybe_single().execute()
    data = res.data
    err = res.error
    if err:
        logger.error("Supabase error select code: %s", err)
        await message.answer("Ошибка сервера. Попробуй позже.")
        return

    if not data:
        await message.answer("❌ Неверный код.")
        return

    # data — запись; предполагаем поля: code, telegram_id
    if data.get("telegram_id") in (None, ""):
        # записываем telegram_id
        upd = supabase.table(CODES_TABLE).update({"telegram_id": user_id}).eq("code", code).execute()
        if upd.error:
            logger.error("Supabase error update code: %s", upd.error)
            await message.answer("Ошибка при привязке кода. Напиши позже.")
            return
        await message.answer("✅ Регистрация успешна! Добро пожаловать.")
        await main_menu(user_id)
    elif data.get("telegram_id") == user_id:
        await message.answer("✅ Ты уже зарегистрирован.")
        await main_menu(user_id)
    else:
        await message.answer("❌ Этот код уже использован другим пользователем.")

# ---------- Вспомогательная: загрузить все задания из Supabase ----------
def load_tasks_from_supabase() -> List[Dict[str, Any]]:
    res = supabase.table(TASKS_TABLE).select("*").execute()
    if res.error:
        logger.error("Ошибка загрузки задач: %s", res.error)
        return []
    tasks = res.data or []
    # Преобразуем поле options, если это строка JSON
    for t in tasks:
        if "options" in t and isinstance(t["options"], str):
            try:
                t["options"] = json.loads(t["options"])
            except Exception:
                # оставляем строку как есть
                t["options"] = [t["options"]]
    return tasks

# ---------- Запуск теста ----------
@dp.message(F.text == "/test")
async def start_test_cmd(message: Message):
    await begin_test(message.from_user.id)

@dp.callback_query(F.data == "start_test")
async def start_test_callback(call: CallbackQuery):
    await begin_test(call.from_user.id)

async def begin_test(user_id: int):
    # Проверка регистрации: ищем запись в codes с telegram_id = user_id
    res = supabase.table(CODES_TABLE).select("*").eq("telegram_id", user_id).maybe_single().execute()
    if res.error:
        logger.error("Supabase error checking registration: %s", res.error)
        await bot.send_message(user_id, "Ошибка сервера. Попробуй позже.")
        return
    if not res.data:
        await bot.send_message(user_id, "❌ Сначала зарегистрируйся с помощью индивидуального кода.")
        return

    tasks = load_tasks_from_supabase()
    if not tasks:
        await bot.send_message(user_id, "В базе нет задач или произошла ошибка.")
        return

    random.shuffle(tasks)
    user_sessions[user_id] = {
        "tasks": tasks,
        "current": 0,
        "correct": 0,
        "wrong": [],
        "waiting_text": False
    }

    # Таймер 10 минут
    asyncio.create_task(test_timer(user_id, 600))
    await send_task(user_id)

async def test_timer(user_id: int, seconds: int):
    await asyncio.sleep(seconds)
    if user_id in user_sessions:
        await show_results(user_id)

# ---------- Отправка задачи ----------
async def send_task(user_id: int):
    session = user_sessions.get(user_id)
    if not session:
        return
    if session["current"] >= len(session["tasks"]):
        await show_results(user_id)
        return

    task = session["tasks"][session["current"]]
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    back_button = [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu")]

    ttype = task.get("type", "choice")
    question_text = task.get("question", "(вопрос пропущен)")

    if ttype == "choice":
        opts = task.get("options") or []
        for opt in opts:
            kb.inline_keyboard.append([InlineKeyboardButton(text=str(opt), callback_data=f"ans:{str(opt)}")])
        kb.inline_keyboard.append([InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip")])
        kb.inline_keyboard.append(back_button)
        await bot.send_message(user_id, f"❓ {question_text}", reply_markup=kb)
    elif ttype == "text":
        session["waiting_text"] = True
        kb.inline_keyboard.append([InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip")])
        kb.inline_keyboard.append(back_button)
        await bot.send_message(user_id, f"❓ {question_text}\n\n✍ Введи ответ сообщением:", reply_markup=kb)
    else:
        # fallback
        await bot.send_message(user_id, f"❓ {question_text}")
        session["current"] += 1
        await send_task(user_id)

# ---------- Обработка кликов (варианты) ----------
@dp.callback_query(F.data.startswith("ans:"))
async def process_answer(call: CallbackQuery):
    user_id = call.from_user.id
    session = user_sessions.get(user_id)
    if not session:
        await call.message.answer("Сессия не найдена. Нажми /test или вернись в меню.")
        return

    task = session["tasks"][session["current"]]
    answer = call.data.split(":", 1)[1]

    correct = task.get("answer")
    if normalize_answer(str(answer)) == normalize_answer(str(correct)):
        session["correct"] += 1
        await call.message.answer("✅ Верно!")
    else:
        session["wrong"].append((task.get("question", ""), task.get("explanation", "")))
        await call.message.answer("❌ Неверно!")

    session["current"] += 1
    session["waiting_text"] = False
    await send_task(user_id)

@dp.callback_query(F.data == "skip")
async def skip_task(call: CallbackQuery):
    user_id = call.from_user.id
    session = user_sessions.get(user_id)
    if not session:
        return
    task = session["tasks"].pop(session["current"])
    session["tasks"].append(task)
    session["waiting_text"] = False
    await send_task(user_id)

@dp.callback_query(F.data == "back_menu")
async def go_back_menu(call: CallbackQuery):
    await main_menu(call.from_user.id)

# ---------- Обработка текстовых ответов ----------
@dp.message()
async def process_text_answer(message: Message):
    user_id = message.from_user.id
    session = user_sessions.get(user_id)
    if not session or not session.get("waiting_text"):
        return

    task = session["tasks"][session["current"]]
    answer = message.text.strip()

    if normalize_answer(answer) == normalize_answer(task.get("answer", "")):
        session["correct"] += 1
        await message.answer("✅ Верно!")
    else:
        session["wrong"].append((task.get("question", ""), task.get("explanation", "")))
        await message.answer("❌ Неверно!")

    session["current"] += 1
    session["waiting_text"] = False
    await send_task(user_id)

# ---------- Итог ----------
async def show_results(user_id: int):
    session = user_sessions.pop(user_id, None)
    if not session:
        return
    total = len(session["tasks"])
    correct = session["correct"]
    wrong = session["wrong"]

    text = f"🏁 Тест завершён!\n\nПравильных ответов: {correct}/{total}\n"
    if wrong:
        text += "\nОшибки:\n"
        for q, exp in wrong:
            text += f"\n❌ {q}\nℹ️ {exp}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Пройти ещё раз", callback_data="start_test")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_menu")]
    ])
    await bot.send_message(user_id, text, reply_markup=kb)

# ---------- Запуск (polling) ----------
async def main():
    logger.info("Bot starting (polling)...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
