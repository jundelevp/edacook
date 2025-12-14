import asyncio
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from dishes import DISHES
import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === In-memory storage (no DB) ===
paid_users: set[int] = set()
free_attempts: dict[int, int] = {}
last_request_time: dict[int, datetime] = {}
last_response_cache: dict[int, tuple[str, datetime]] = {}

# === FSM States ===
class UserPreferences(StatesGroup):
    asking_health = State()
    asking_diet = State()

# === Keyboard builders ===
def build_health_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, правильное питание", callback_data="healthy_yes"),
            InlineKeyboardButton(text="😋 Нет, просто вкусно", callback_data="healthy_no")
        ],
        [InlineKeyboardButton(text="🛠 Поддержка", url="https://t.me/Oblastyle")]
    ])

def build_diet_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🥩 Мясо", callback_data="diet_meat"),
            InlineKeyboardButton(text="🐟 Рыба", callback_data="diet_fish"),
            InlineKeyboardButton(text="🥦 Без мяса", callback_data="diet_veg")
        ],
        [InlineKeyboardButton(text="🛠 Поддержка", url="https://t.me/Oblastyle")]
    ])

def build_payment_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить доступ за 299 ₽ (навсегда)", callback_data="buy_access")],
        [InlineKeyboardButton(text="🛠 Поддержка", url="https://t.me/Oblastyle")]
    ])

def build_time_suggestion_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🍳 На завтрак", callback_data="suggest_breakfast"),
            InlineKeyboardButton(text="🍲 На обед", callback_data="suggest_lunch"),
            InlineKeyboardButton(text="🥘 На ужин", callback_data="suggest_dinner")
        ],
        [InlineKeyboardButton(text="🛠 Поддержка", url="https://t.me/Oblastyle")]
    ])

# === Time parsing ===
def get_time_category(hour: int) -> str:
    if 5 <= hour < 10:
        return "breakfast"
    elif 10 <= hour < 18:
        return "lunch"
    else:
        return "dinner"

def parse_hour_from_text(text: str) -> int:
    text = text.lower()
    now = datetime.now().hour

    if any(w in text for w in ["утро", "завтрак", "утром", "с утра", "рано", "8", "9"]):
        return 8
    if any(w in text for w in ["обед", "днём", "днем", "10", "11", "12", "13", "14", "15", "16", "17"]):
        return 13
    if any(w in text for w in ["ужин", "вечер", "ночь", "сейчас", "поздно", "18", "19", "20", "21", "22", "23", "0", "1", "2", "3", "4", "5", "6", "7"]):
        return 19

    time_match = re.search(r'(\d{1,2})', text)
    if time_match:
        h = int(time_match.group(1))
        if 0 <= h <= 23:
            return h
    return now

def filter_dishes(hour: int, healthy: bool, diet: str) -> List[Dict[str, Any]]:
    time_cat = get_time_category(hour)
    filtered = [
        d for d in DISHES
        if d["time"] == time_cat
        and d["healthy"] == healthy
        and (diet == "any" or d["diet"] == diet)
    ]
    if len(filtered) < 3:
        fallback = [d for d in DISHES if d["time"] == time_cat and d not in filtered]
        filtered += fallback
    return filtered[:3]

# === Router ===
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👨‍🍳 Привет! Я помогу решить, что приготовить — с рецептами и учётом ваших предпочтений.\n\n"
        "Просто напишите:\n"
        "• «что приготовить на ужин?»\n"
        "• «рецепт на завтрак»\n"
        "• «что сделать в 19:00?»\n\n"
        "Первый раз — бесплатно. Потом — 299 ₽ навсегда.",
        reply_markup=build_time_suggestion_kb()
    )

@router.callback_query(F.data.startswith("suggest_"))
async def handle_time_suggestion(callback: CallbackQuery, state: FSMContext):
    time_map = {"suggest_breakfast": 8, "suggest_lunch": 13, "suggest_dinner": 19}
    hour = time_map[callback.data]
    await handle_cooking_internal(callback.message, hour, callback.from_user.id, state)
    await callback.answer()

async def is_rate_limited(user_id: int) -> bool:
    now = datetime.now()
    if user_id in last_request_time:
        if now - last_request_time[user_id] < timedelta(minutes=1):
            return True
    last_request_time[user_id] = now
    return False

async def handle_cooking_internal(message: Message, hour: int, user_id: int, state: FSMContext):
    if await is_rate_limited(user_id):
        await message.answer("⏳ Подождите 1 минуту между запросами.")
        return

    if user_id not in paid_users:
        if free_attempts.get(user_id, 0) >= 1:
            await message.answer("🔓 Разблокируйте полный доступ за 299 ₽ — навсегда!", reply_markup=build_payment_kb())
            return
        free_attempts[user_id] = free_attempts.get(user_id, 0) + 1

    data = await state.get_data()
    healthy = data.get("healthy")
    diet = data.get("diet")

    if healthy is None or diet is None:
        await message.answer("Вы на правильном питании?", reply_markup=build_health_kb())
        await state.set_state(UserPreferences.asking_health)
        await state.update_data(pending_hour=hour)
        return

    # Кэш для платных пользователей
    cache_key = f"{hour}_{healthy}_{diet}"
    now = datetime.now()
    if user_id in paid_users:
        if user_id in last_response_cache:
            cached_resp, cached_time = last_response_cache[user_id]
            if now - cached_time < timedelta(minutes=5) and cached_resp.startswith(f"КЭШ:{cache_key}"):
                reply = cached_resp.replace(f"КЭШ:{cache_key}||", "", 1)
                await message.answer(reply)
                return

    dishes = filter_dishes(hour, healthy, diet)
    if not dishes:
        dishes = filter_dishes(hour, True, "any")[:3]

    reply = "Вот идеи с рецептами:\n\n"
    for d in dishes:
        reply += f"🔥 {d['name']}\n{d['recipe']}\n\n"

    if user_id not in paid_users:
        reply += "✨ Больше рецептов — за 299 ₽ навсегда!"
    else:
        last_response_cache[user_id] = (f"КЭШ:{cache_key}||{reply}", now)

    await message.answer(reply)

@router.message(F.text)
async def handle_cooking_query(message: Message, state: FSMContext):
    user_id = message.from_user.id
    hour = parse_hour_from_text(message.text)
    await handle_cooking_internal(message, hour, user_id, state)

# === FSM Handlers ===
@router.callback_query(UserPreferences.asking_health, F.data.startswith("healthy_"))
async def process_health(callback: CallbackQuery, state: FSMContext):
    healthy = callback.data == "healthy_yes"
    await state.update_data(healthy=healthy)
    await callback.message.edit_text("А что предпочитаете?", reply_markup=build_diet_kb())
    await state.set_state(UserPreferences.asking_diet)
    await callback.answer()

@router.callback_query(UserPreferences.asking_diet, F.data.startswith("diet_"))
async def process_diet(callback: CallbackQuery, state: FSMContext):
    diet_map = {"diet_meat": "meat", "diet_fish": "fish", "diet_veg": "veg"}
    diet = diet_map[callback.data]
    await state.update_data(diet=diet)
    data = await state.get_data()
    hour = data.get("pending_hour", datetime.now().hour)
    await handle_cooking_internal(callback.message, hour, callback.from_user.id, state)
    await state.clear()
    await callback.answer()

# === Payment (mock for production) ===
@router.callback_query(F.data == "buy_access")
async def buy_access(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in paid_users:
        await callback.answer("Уже оплачено!", show_alert=True)
        return

    paid_users.add(user_id)
    free_attempts.pop(user_id, None)
    last_response_cache.pop(user_id, None)

    await callback.message.edit_text(
        "✅ Доступ навсегда активирован!\n\n"
        "Теперь вы можете писать мне сколько угодно. Попробуйте:",
        reply_markup=build_time_suggestion_kb()
    )
    await callback.answer("Спасибо за доверие! 🙏", show_alert=True)

# === Main ===
async def main():
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise ValueError("❌ BOT_TOKEN не найден в .env")

    bot = Bot(token=bot_token, parse_mode=ParseMode.HTML)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    logger.info("✅ Бот запущен и готов к работе.")
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("⏹ Бот остановлен вручную.")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())