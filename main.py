"""
Упрощенный продакшн Telegram бот для объявлений
Архитектура: webhook + aiohttp + PostgreSQL + Memory cache
Оптимизирован для Render.com, совместим с aiogram 2.x
"""


import asyncio
import logging
import os
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncpg
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command, CommandStart
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
import re
from functools import wraps
from collections import defaultdict
import time

# ==================== КОНФИГУРАЦИЯ ====================

class Config:
    """Централизованная конфигурация"""
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    DATABASE_URL = os.getenv("DATABASE_URL")
    
    # Telegram настройки
    TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1002827106973"))
    MODERATION_CHAT_ID = int(os.getenv("MODERATION_CHAT_ID", "0"))
    
    # Web настройки
    WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://your-app.onrender.com")
    WEBHOOK_PATH = "/webhook"
    WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
    PORT = int(os.getenv("PORT", "10000"))
    
    # Бизнес настройки
    GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/your_group")
    EXAMPLE_URL = os.getenv("EXAMPLE_URL", "https://example.com")
    DEFAULT_AD_LIMIT = 4
    RATE_LIMIT_WINDOW = 60
    RATE_LIMIT_MAX_REQUESTS = 10
    
    # Database pool настройки
    DB_MIN_SIZE = 2
    DB_MAX_SIZE = 8
    DB_COMMAND_TIMEOUT = 30
    
    @classmethod
    def validate(cls):
        """Валидация обязательных настроек"""
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN не установлен!")
        if not cls.DATABASE_URL:
            raise ValueError("DATABASE_URL не установлен!")

# ==================== МОДЕЛИ ДАННЫХ ====================

@dataclass
class UserAd:
    """Модель объявления пользователя"""
    user_id: int
    message_id: int
    message_url: str
    topic_name: str
    id: Optional[int] = None
    created_at: Optional[datetime] = None

@dataclass
class TopicInfo:
    """Модель информации о теме"""
    name: str
    id: int

@dataclass 
class ValidationResult:
    """Результат валидации текста"""
    is_valid: bool
    error_message: str = ""

# ==================== СОСТОЯНИЯ FSM ====================

class AdStates(StatesGroup):
    choosing_language = State()
    main_menu = State()
    choosing_topic = State() 
    writing_ad = State()
    my_ads = State()
    editing_ad = State()

# ==================== КОНФИГУРАЦИЯ ТЕМ ====================

TOPICS: Dict[str, TopicInfo] = {
    "topic_1": TopicInfo("💼 Работа", 27),
    "topic_2": TopicInfo("🏠 Недвижимость", 28),
    "topic_3": TopicInfo("🚗 Авто", 29),
    "topic_4": TopicInfo("🛍️ Товары", 30),
    "topic_5": TopicInfo("💡 Услуги", 31),
    "topic_6": TopicInfo("📚 Обучение", 32),
}

# ==================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ====================

bot: Bot = None
dp: Dispatcher = None
router: Router = None  # ДОБАВЛЕНО для aiogram 3.x
db_pool: asyncpg.Pool = None

# Rate limiting и кэширование в памяти
rate_limiter = defaultdict(list)
memory_cache = {}

# ==================== ЛОГИРОВАНИЕ ====================

def setup_logging():
    """Настройка логирования"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler()]
    )
    logging.getLogger('aiogram').setLevel(logging.WARNING)
    logging.getLogger('aiohttp').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ==================== ДЕКОРАТОРЫ ====================

def rate_limit(max_requests: int = Config.RATE_LIMIT_MAX_REQUESTS, 
               window: int = Config.RATE_LIMIT_WINDOW):
    """Декоратор для rate limiting"""
    def decorator(func):
        @wraps(func)
        async def wrapper(message_or_query, *args, **kwargs):
            user_id = message_or_query.from_user.id
            
            now = time.time()
            requests = rate_limiter[user_id]
            
            requests[:] = [req_time for req_time in requests if now - req_time < window]
            
            if len(requests) >= max_requests:
                if hasattr(message_or_query, 'answer'):
                    await message_or_query.answer("⏱ Слишком много запросов. Попробуйте позже.", show_alert=True)
                else:
                    await message_or_query.reply("⏱ Слишком много запросов. Попробуйте позже.")
                return
            
            requests.append(now)
            return await func(message_or_query, *args, **kwargs)
        return wrapper
    return decorator

def ban_check(func):
    """Декоратор для проверки бана"""
    @wraps(func)
    async def wrapper(message_or_query, *args, **kwargs):
        user_id = message_or_query.from_user.id
        if await is_user_banned(user_id):
            if hasattr(message_or_query, 'answer'):
                await message_or_query.answer("🚫 Вы заблокированы в этом боте.", show_alert=True)
            else:
                await message_or_query.reply("🚫 Вы заблокированы в этом боте.")
            return
        return await func(message_or_query, *args, **kwargs)
    return wrapper

# ==================== КЭШИРОВАНИЕ И БД (без изменений) ====================

class CacheService:
    """Сервис кэширования в памяти"""
    
    @staticmethod
    async def get(key: str) -> Optional[Any]:
        cache_data = memory_cache.get(key)
        if cache_data:
            value, expire_time = cache_data
            if time.time() < expire_time:
                return value
            else:
                memory_cache.pop(key, None)
        return None
    
    @staticmethod
    async def set(key: str, value: Any, ttl: int = 300) -> bool:
        expire_time = time.time() + ttl
        memory_cache[key] = (value, expire_time)
        return True
    
    @staticmethod
    async def delete(key: str) -> bool:
        memory_cache.pop(key, None)
        return True

# ==================== БАЗА ДАННЫХ ====================

@asynccontextmanager
async def get_db_connection():
    """Context manager для получения соединения с БД"""
    async with db_pool.acquire() as connection:
        try:
            yield connection
        except Exception as e:
            logger.error(f"Database error: {e}")
            raise

class DatabaseService:
    """Сервис работы с базой данных"""
    
    @staticmethod
    async def init_database():
        """Инициализация базы данных"""
        global db_pool
        
        Config.validate()
        
        try:
            db_pool = await asyncpg.create_pool(
                Config.DATABASE_URL,
                min_size=Config.DB_MIN_SIZE,
                max_size=Config.DB_MAX_SIZE,
                command_timeout=Config.DB_COMMAND_TIMEOUT
            )
            
            async with get_db_connection() as conn:
                # Создание основных таблиц
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_ads (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        message_id BIGINT NOT NULL UNIQUE,
                        message_url TEXT NOT NULL,
                        topic_name TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS banned_users (
                        user_id BIGINT PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_limits (
                        user_id BIGINT PRIMARY KEY,
                        ad_limit INTEGER NOT NULL DEFAULT 4,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Создание индексов
                indexes = [
                    "CREATE INDEX IF NOT EXISTS idx_user_ads_user_id ON user_ads(user_id)",
                    "CREATE INDEX IF NOT EXISTS idx_user_ads_message_id ON user_ads(message_id)",
                    "CREATE INDEX IF NOT EXISTS idx_user_ads_created_at ON user_ads(created_at DESC)",
                ]
                
                for index_sql in indexes:
                    try:
                        await conn.execute(index_sql)
                    except Exception as e:
                        if "already exists" not in str(e):
                            logger.warning(f"Index creation warning: {e}")
            
            logger.info("✅ База данных успешно инициализирована")
            
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации БД: {e}")
            raise
    
    @staticmethod
    async def add_user_ad(user_ad: UserAd) -> bool:
        """Добавить объявление пользователя"""
        try:
            async with get_db_connection() as conn:
                await conn.execute(
                    """INSERT INTO user_ads (user_id, message_id, message_url, topic_name) 
                       VALUES ($1, $2, $3, $4)""",
                    user_ad.user_id, user_ad.message_id, user_ad.message_url, user_ad.topic_name
                )
                
                # Инвалидируем кэш
                await CacheService.delete(f"user_ads:{user_ad.user_id}")
                await CacheService.delete(f"user_ad_count:{user_ad.user_id}")
                return True
        except Exception as e:
            logger.error(f"Ошибка добавления объявления: {e}")
            return False
    
    @staticmethod
    async def get_user_ads(user_id: int) -> List[Tuple[int, str, str]]:
        """Получить объявления пользователя с кэшированием"""
        cache_key = f"user_ads:{user_id}"
        cached = await CacheService.get(cache_key)
        if cached:
            return cached
        
        try:
            async with get_db_connection() as conn:
                rows = await conn.fetch(
                    """SELECT message_id, message_url, topic_name 
                       FROM user_ads WHERE user_id = $1 
                       ORDER BY created_at DESC""",
                    user_id
                )
                result = [(row['message_id'], row['message_url'], row['topic_name']) for row in rows]
                await CacheService.set(cache_key, result, 300)
                return result
        except Exception as e:
            logger.error(f"Ошибка получения объявлений: {e}")
            return []
    
    @staticmethod
    async def get_user_ads_with_counts(user_id: int) -> List[Tuple[int, str, str, str]]:
        """Получить объявления с нумерацией по темам"""
        ads = await DatabaseService.get_user_ads(user_id)
        
        topic_counts = defaultdict(int)
        result = []
        
        for message_id, message_url, topic_name in ads:
            topic_counts[topic_name] += 1
            topic_display = f"{topic_name} {topic_counts[topic_name]}"
            result.append((message_id, message_url, topic_display, topic_name))
        
        return result
    
    @staticmethod
    async def get_ad_by_message_id(message_id: int) -> Optional[Tuple[int, int, str, str]]:
        """Получить объявление по message_id с кэшированием"""
        cache_key = f"ad:{message_id}"
        cached = await CacheService.get(cache_key)
        if cached:
            return tuple(cached)
        
        try:
            async with get_db_connection() as conn:
                row = await conn.fetchrow(
                    """SELECT user_id, message_id, message_url, topic_name 
                       FROM user_ads WHERE message_id = $1""",
                    message_id
                )
                if row:
                    result = (row['user_id'], row['message_id'], row['message_url'], row['topic_name'])
                    await CacheService.set(cache_key, result, 600)
                    return result
                return None
        except Exception as e:
            logger.error(f"Ошибка получения объявления: {e}")
            return None
    
    @staticmethod
    async def delete_user_ad(message_id: int) -> bool:
        """Удалить объявление"""
        try:
            async with get_db_connection() as conn:
                # Получаем user_id для инвалидации кэша
                row = await conn.fetchrow(
                    "SELECT user_id FROM user_ads WHERE message_id = $1", message_id
                )
                
                if row:
                    user_id = row['user_id']
                    await conn.execute(
                        "DELETE FROM user_ads WHERE message_id = $1", message_id
                    )
                    
                    # Инвалидируем кэш
                    await CacheService.delete(f"user_ads:{user_id}")
                    await CacheService.delete(f"user_ad_count:{user_id}")
                    await CacheService.delete(f"ad:{message_id}")
                    return True
                return False
        except Exception as e:
            logger.error(f"Ошибка удаления объявления: {e}")
            return False
    
    @staticmethod
    async def get_user_ad_count(user_id: int) -> int:
        """Получить количество объявлений с кэшированием"""
        cache_key = f"user_ad_count:{user_id}"
        cached = await CacheService.get(cache_key)
        if cached is not None:
            return cached
        
        try:
            async with get_db_connection() as conn:
                result = await conn.fetchval(
                    "SELECT COUNT(*) FROM user_ads WHERE user_id = $1", user_id
                )
                count = result if result else 0
                await CacheService.set(cache_key, count, 60)
                return count
        except Exception as e:
            logger.error(f"Ошибка подсчета объявлений: {e}")
            return 0
    
    @staticmethod
    async def ban_user(user_id: int) -> bool:
        """Забанить пользователя"""
        try:
            async with get_db_connection() as conn:
                await conn.execute(
                    """INSERT INTO banned_users (user_id) 
                       VALUES ($1) ON CONFLICT (user_id) DO NOTHING""",
                    user_id
                )
                await CacheService.delete(f"banned:{user_id}")
                return True
        except Exception as e:
            logger.error(f"Ошибка бана пользователя: {e}")
            return False
    
    @staticmethod
    async def unban_user(user_id: int) -> bool:
        """Разбанить пользователя"""
        try:
            async with get_db_connection() as conn:
                await conn.execute(
                    "DELETE FROM banned_users WHERE user_id = $1", user_id
                )
                await CacheService.delete(f"banned:{user_id}")
                return True
        except Exception as e:
            logger.error(f"Ошибка разбана пользователя: {e}")
            return False

# ==================== ВАЛИДАЦИЯ ====================

class ValidationService:
    """Сервис валидации данных"""
    
    @staticmethod
    def validate_message_text(text: str) -> ValidationResult:
        """Валидация текста сообщения"""
        if not text or not text.strip():
            return ValidationResult(False, "❌ Сообщение не может быть пустым!")
        
        if len(text) > 4000:
            return ValidationResult(False, "❌ Сообщение слишком длинное (максимум 4000 символов)!")
        
        # Проверяем на @username
        if '@' in text:
            return ValidationResult(False, "❌ @username не принимаются, мы сами вставим ссылку на вас.")
        
        # Проверяем на URL
        if re.search(r'https?://', text, re.IGNORECASE):
            return ValidationResult(False, "❌ Ссылки не принимаются, мы сами вставим ссылку на вас.")
        
        # Проверяем на хэштеги
        if '#' in text:
            return ValidationResult(False, "❌ Хэштеги не принимаются, мы сами вставим ссылку на вас.")
        
        # Проверяем на домены
        domain_pattern = r'\b[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.([a-zA-Z]{2,})\b'
        if re.search(domain_pattern, text):
            return ValidationResult(False, "❌ Сайты не принимаются, мы сами вставим ссылку на вас.")
        
        return ValidationResult(True)

# ==================== УТИЛИТЫ ====================

async def is_user_banned(user_id: int) -> bool:
    """Проверить бан пользователя с кэшированием"""
    cache_key = f"banned:{user_id}"
    cached = await CacheService.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        async with get_db_connection() as conn:
            result = await conn.fetchval(
                "SELECT 1 FROM banned_users WHERE user_id = $1", user_id
            )
            is_banned = result is not None
            await CacheService.set(cache_key, is_banned, 300)
            return is_banned
    except Exception as e:
        logger.error(f"Ошибка проверки бана: {e}")
        return False

async def get_user_limit(user_id: int) -> int:
    """Получить лимит пользователя с кэшированием"""
    cache_key = f"user_limit:{user_id}"
    cached = await CacheService.get(cache_key)
    if cached is not None:
        return cached
    
    try:
        async with get_db_connection() as conn:
            result = await conn.fetchval(
                "SELECT ad_limit FROM user_limits WHERE user_id = $1", user_id
            )
            limit = result if result else Config.DEFAULT_AD_LIMIT
            await CacheService.set(cache_key, limit, 600)
            return limit
    except Exception as e:
        logger.error(f"Ошибка получения лимита: {e}")
        return Config.DEFAULT_AD_LIMIT

async def set_user_limit(user_id: int, limit: int):
    """Установить лимит объявлений для пользователя"""
    try:
        async with get_db_connection() as conn:
            await conn.execute(
                """INSERT INTO user_limits (user_id, ad_limit) 
                   VALUES ($1, $2) 
                   ON CONFLICT (user_id) 
                   DO UPDATE SET ad_limit = $2, updated_at = CURRENT_TIMESTAMP""",
                user_id, limit
            )
            # Инвалидируем кэш
            await CacheService.delete(f"user_limit:{user_id}")
    except Exception as e:
        logger.error(f"Ошибка установки лимита: {e}")

async def notify_user(user_id: int, message: str) -> bool:
    """Уведомить пользователя"""
    try:
        await bot.send_message(chat_id=user_id, text=message)
        return True
    except Exception as e:
        logger.error(f"Ошибка уведомления пользователя {user_id}: {e}")
        return False

# ==================== КЛАВИАТУРЫ ====================

def get_language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")
        ]
    ])

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Создать", callback_data="create_ad"),
            InlineKeyboardButton(text="📋 Мои объявления", callback_data="my_ads")
        ],
        [InlineKeyboardButton(text="🔗 Перейти в группу", url=Config.GROUP_LINK)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_language")]
    ])

def get_topics_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    topic_items = list(TOPICS.items())
    
    for i in range(0, len(topic_items), 2):
        row = []
        topic_key, topic_data = topic_items[i]
        row.append(InlineKeyboardButton(
            text=topic_data.name, 
            callback_data=topic_key
        ))
        
        if i + 1 < len(topic_items):
            topic_key2, topic_data2 = topic_items[i + 1]
            row.append(InlineKeyboardButton(
                text=topic_data2.name, 
                callback_data=topic_key2
            ))
        
        buttons.append(row)
    
    buttons.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== ОБРАБОТЧИКИ AIOGRAM 3.X ====================

@router.message(CommandStart())
@rate_limit()
async def start_handler(message: Message, state: FSMContext):
    """Стартовый обработчик"""
    if message.chat.id == Config.TARGET_CHAT_ID:
        return
    
    if await is_user_banned(message.from_user.id):
        await message.reply("🚫 Вы заблокированы в этом боте.")
        return
    
    await message.reply(
        "🌍 Выберите язык / Choose language:",
        reply_markup=get_language_keyboard()
    )
    await state.set_state(AdStates.choosing_language)

@router.callback_query(F.data == "lang_ru")
@rate_limit()
@ban_check
async def language_ru_handler(callback: CallbackQuery, state: FSMContext):
    """Выбор русского языка"""
    await callback.message.edit_text(
        "🏠 Главное меню:",
        reply_markup=get_main_menu_keyboard()
    )
    await state.set_state(AdStates.main_menu)
    await callback.answer()

@router.callback_query(F.data == "lang_en")
@rate_limit()
async def language_en_handler(callback: CallbackQuery, state: FSMContext):
    """Выбор английского языка (заглушка)"""
    await callback.answer("🚧 English version coming soon!", show_alert=True)

@router.callback_query(F.data == "create_ad")
@rate_limit()
@ban_check
async def create_ad_handler(callback: CallbackQuery, state: FSMContext):
    """Создание объявления"""
    await callback.message.edit_text(
        "📝 В какую тему хотите написать?",
        reply_markup=get_topics_keyboard()
    )
    await state.set_state(AdStates.choosing_topic)
    await callback.answer()

@router.callback_query(F.data == "my_ads")
@rate_limit()
@ban_check
async def my_ads_handler(callback: CallbackQuery, state: FSMContext):
    """Мои объявления"""
    user_id = callback.from_user.id
    # ads = await DatabaseService.get_user_ads(user_id)  # Раскомментируйте
    # user_limit = await get_user_limit(user_id)
    
    # Временная заглушка:
    await callback.answer("📭 У вас пока нет объявлений", show_alert=True)
    return

@router.callback_query(F.data.in_(list(TOPICS.keys())), AdStates.choosing_topic)
@rate_limit()
@ban_check
async def topic_handler(callback: CallbackQuery, state: FSMContext):
    """Выбор темы"""
    topic_key = callback.data
    
    await state.update_data(selected_topic=topic_key)
    
    topic_name = TOPICS[topic_key].name
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Пример заполнения", url=Config.EXAMPLE_URL)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_topics")]
    ])
    
    await callback.message.edit_text(
        f"✍️ Тема: {topic_name}\n\nНапишите объявление и отправьте:",
        reply_markup=keyboard
    )
    await state.set_state(AdStates.writing_ad)
    await callback.answer()

@router.message(AdStates.writing_ad, F.content_type == "text")
@rate_limit()
@ban_check
async def ad_text_handler(message: Message, state: FSMContext):
    """Обработка текста объявления"""
    if message.chat.id == Config.TARGET_CHAT_ID:
        return
    
    user_data = await state.get_data()
    selected_topic = user_data.get("selected_topic")
    
    if not selected_topic or selected_topic not in TOPICS:
        await message.reply("❌ Ошибка: тема не выбрана. Начните заново с /start")
        await state.clear()
        return
    
    # Здесь добавьте вашу логику валидации и сохранения
    await message.reply("✅ Объявление получено! (Временная заглушка)")
    await state.clear()

# ==================== НАВИГАЦИЯ ====================

@router.callback_query(F.data == "back_to_language")
async def back_to_language_handler(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору языка"""
    await callback.message.edit_text(
        "🌍 Выберите язык / Choose language:",
        reply_markup=get_language_keyboard()
    )
    await state.set_state(AdStates.choosing_language)
    await callback.answer()

@router.callback_query(F.data == "back_to_main")
async def back_to_main_handler(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await callback.message.edit_text(
        "🏠 Главное меню:",
        reply_markup=get_main_menu_keyboard()
    )
    await state.set_state(AdStates.main_menu)
    await callback.answer()

@router.callback_query(F.data == "back_to_topics")
async def back_to_topics_handler(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору тем"""
    await callback.message.edit_text(
        "📝 В какую тему хотите написать?",
        reply_markup=get_topics_keyboard()
    )
    await state.set_state(AdStates.choosing_topic)
    await callback.answer()

# ==================== ИНИЦИАЛИЗАЦИЯ И ЗАПУСК ====================

async def init_database():
    """Заглушка для инициализации БД"""
    # global db_pool
    # Config.validate()
    # db_pool = await asyncpg.create_pool(Config.DATABASE_URL, ...)
    logger.info("✅ База данных инициализирована (заглушка)")

async def is_user_banned(user_id: int) -> bool:
    """Заглушка для проверки бана"""
    return False

async def main():
    """Главная функция запуска"""
    global bot, dp, router
    
    # Инициализация компонентов
    Config.validate()
    setup_logging()
    
    bot = Bot(token=Config.BOT_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    router = Router()
    
    # Регистрация роутера
    dp.include_router(router)
    
    # Инициализация сервисов
    await init_database()
    
    # Установка команд
    await bot.set_my_commands([
        BotCommand(command="start", description="🚀 Начать работу с ботом")
    ])
    
    logger.info("🚀 Бот запущен!")
    
    # Webhook режим для Render.com
    if Config.WEBHOOK_HOST and "onrender.com" in Config.WEBHOOK_HOST:
        app = web.Application()
        
        # Настройка webhook
        webhook_requests_handler = SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
        )
        webhook_requests_handler.register(app, path=Config.WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        
        # Health check
        async def health(request):
            return web.json_response({"status": "ok"})
        app.router.add_get('/health', health)
        
        # Установка webhook
        await bot.set_webhook(Config.WEBHOOK_URL)
        logger.info(f"✅ Webhook установлен: {Config.WEBHOOK_URL}")
        
        # Запуск веб-сервера
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', Config.PORT)
        await site.start()
        logger.info(f"🌐 Веб-сервер запущен на порту {Config.PORT}")
        
        # Держим бота живым
        try:
            await asyncio.Future()  # run forever
        finally:
            await bot.delete_webhook()
            await runner.cleanup()
    else:
        # Polling режим для локальной разработки
        logger.info("🔄 Запуск в режиме polling...")
        await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())