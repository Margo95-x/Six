#!/usr/bin/env python3
"""
main.py - Telegram Web App Server
Объединенный сервер для WebSocket, HTTP и Telegram Bot
Автор: Assistant 
Версия: 1.0
"""

import asyncio
import logging
import os
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import asyncpg
import websockets
from websockets.server import WebSocketServerProtocol
from collections import defaultdict
from contextlib import asynccontextmanager
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import aiohttp
from dataclasses import dataclass

# Конфигурация
@dataclass
class Config:
    DATABASE_URL: str = os.getenv("DATABASE_URL")
    BOT_TOKEN: str = os.getenv("BOT_TOKEN")
    MODERATION_CHAT_ID: int = int(os.getenv("MODERATION_CHAT_ID", "0"))
    PORT: int = int(os.getenv("PORT", "10000"))
    DAILY_POST_LIMIT: int = 60
    DB_MIN_SIZE: int = 1
    DB_MAX_SIZE: int = 3
    DB_COMMAND_TIMEOUT: int = 30

config = Config()

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Глобальные переменные
db_pool = None
telegram_bot = None
connected_clients = set()
post_limits = defaultdict(list)  # Кеш лимитов в памяти
posts_cache = {}  # Кеш постов в памяти
user_cache = {}   # Кеш пользователей в памяти

# База данных
@asynccontextmanager
async def get_db_connection():
    async with db_pool.acquire() as connection:
        try:
            yield connection
        except Exception as e:
            logger.error(f"Database error: {e}")
            raise

class DatabaseService:
    @staticmethod
    async def init_database():
        global db_pool
        if not config.DATABASE_URL:
            raise ValueError("DATABASE_URL not set")
        
        db_pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=config.DB_MIN_SIZE,
            max_size=config.DB_MAX_SIZE,
            command_timeout=config.DB_COMMAND_TIMEOUT
        )
        
        async with get_db_connection() as conn:
            # Удаляем старые таблицы если существуют (для обновления структуры)
            await conn.execute("DROP TABLE IF EXISTS post_reports CASCADE")
            await conn.execute("DROP TABLE IF EXISTS posts CASCADE") 
            await conn.execute("DROP TABLE IF EXISTS users CASCADE")
            await conn.execute("DROP TABLE IF EXISTS accounts CASCADE")
            
            # Создаем таблицы с новой структурой
            await conn.execute("""
                CREATE TABLE posts (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    description TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tags JSONB NOT NULL DEFAULT '[]',
                    likes INTEGER DEFAULT 0,
                    reports_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    moderation_message_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    creator_telegram_id BIGINT NOT NULL,
                    creator_username TEXT,
                    creator_first_name TEXT,
                    creator_last_name TEXT,
                    creator_photo_url TEXT
                )
            """)
            
            await conn.execute("""
                CREATE TABLE post_reports (
                    id SERIAL PRIMARY KEY,
                    post_id INTEGER REFERENCES posts(id),
                    reporter_telegram_id BIGINT NOT NULL,
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await conn.execute("""
                CREATE TABLE accounts (
                    telegram_id BIGINT PRIMARY KEY,
                    liked_posts BIGINT[] DEFAULT '{}',
                    favorite_posts BIGINT[] DEFAULT '{}',
                    hidden_posts BIGINT[] DEFAULT '{}',
                    own_posts BIGINT[] DEFAULT '{}',
                    is_banned BOOLEAN DEFAULT FALSE,
                    ban_reason TEXT,
                    post_limit INTEGER DEFAULT 60,
                    last_post_count_reset DATE DEFAULT CURRENT_DATE,
                    posts_today INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Создаем индексы
            await conn.execute("CREATE INDEX idx_posts_telegram_id ON posts(telegram_id)")
            await conn.execute("CREATE INDEX idx_posts_category ON posts(category)")
            await conn.execute("CREATE INDEX idx_posts_status ON posts(status)")
            await conn.execute("CREATE INDEX idx_posts_created_at ON posts(created_at DESC)")
            await conn.execute("CREATE INDEX idx_posts_creator_telegram_id ON posts(creator_telegram_id)")
            await conn.execute("CREATE INDEX idx_accounts_telegram_id ON accounts(telegram_id)")
            await conn.execute("CREATE INDEX idx_post_reports_post_id ON post_reports(post_id)")
            await conn.execute("CREATE INDEX idx_post_reports_reporter ON post_reports(reporter_telegram_id)")
        
        logger.info("Database initialized with new structure")

    @staticmethod
    async def sync_user(telegram_id: int) -> Dict:
        async with get_db_connection() as conn:
            # Сброс счетчика постов если нужно
            await conn.execute("""
                UPDATE accounts 
                SET posts_today = 0, last_post_count_reset = CURRENT_DATE
                WHERE telegram_id = $1 AND last_post_count_reset < CURRENT_DATE
            """, telegram_id)
            
            account = await conn.fetchrow("SELECT * FROM accounts WHERE telegram_id = $1", telegram_id)
            
            if not account:
                await conn.execute("""
                    INSERT INTO accounts (telegram_id) VALUES ($1)
                """, telegram_id)
                account = await conn.fetchrow("SELECT * FROM accounts WHERE telegram_id = $1", telegram_id)
            
            # Кешируем аккаунт
            account_dict = dict(account)
            user_cache[telegram_id] = account_dict
            
            # Возвращаем только метаданные
            return {
                'user_id': telegram_id,
                'limits': {
                    'used': account_dict.get('posts_today', 0),
                    'total': account_dict.get('post_limit', config.DAILY_POST_LIMIT)
                },
                'is_banned': account_dict.get('is_banned', False),
                'ban_reason': account_dict.get('ban_reason')
            }

    @staticmethod
    async def create_post(post_data: Dict) -> Dict:
        async with get_db_connection() as conn:
            creator = post_data['creator_data']
            post_id = await conn.fetchval("""
                INSERT INTO posts (
                    telegram_id, description, category, tags, status,
                    creator_telegram_id, creator_username, creator_first_name, 
                    creator_last_name, creator_photo_url
                )
                VALUES ($1, $2, $3, $4, 'pending', $5, $6, $7, $8, $9) 
                RETURNING id
            """, post_data['telegram_id'], post_data['description'], post_data['category'],
                json.dumps(post_data['tags']), creator['telegram_id'], creator['username'],
                creator['first_name'], creator['last_name'], creator['photo_url'])
            
            # Увеличиваем счетчик постов пользователя
            await conn.execute("""
                UPDATE accounts SET posts_today = posts_today + 1, 
                own_posts = array_append(own_posts, $2)
                WHERE telegram_id = $1
            """, post_data['telegram_id'], post_id)
            
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            post_dict = dict(post)
            
            # Кешируем пост
            posts_cache[post_id] = post_dict
            return post_dict

    @staticmethod
    async def get_posts(filters: Dict, page: int, limit: int, search: str = '') -> List[Dict]:
        async with get_db_connection() as conn:
            query = """
                SELECT * FROM posts
                WHERE status = 'approved'
                AND category = $1
            """
            params = [filters.get('category', '')]
            param_count = 1
            
            # Поиск
            if search:
                param_count += 1
                query += f" AND LOWER(description) LIKE LOWER($${param_count})"
                params.append(f"%{search}%")
            
            # Фильтры по тегам
            if filters.get('filters'):
                for filter_type, values in filters['filters'].items():
                    if values and filter_type != 'sort' and isinstance(values, list):
                        for value in values:
                            param_count += 1
                            query += f" AND tags @> $${param_count}"
                            params.append(json.dumps([f"{filter_type}:{value}"]))
            
            # Сортировка
            sort_type = filters.get('filters', {}).get('sort', 'new')
            if sort_type == 'old':
                query += " ORDER BY created_at ASC"
            elif sort_type == 'rating':
                query += " ORDER BY likes DESC, created_at DESC"
            else:
                query += " ORDER BY created_at DESC"
            
            query += f" LIMIT {limit} OFFSET {(page - 1) * limit}"
            
            posts = await conn.fetch(query, *params)
            result = [dict(post) for post in posts]
            
            # Кешируем полученные посты
            for post in result:
                posts_cache[post['id']] = post
            
            return result

    @staticmethod
    async def approve_post(post_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            await conn.execute("UPDATE posts SET status = 'approved' WHERE id = $1", post_id)
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if post:
                post_dict = dict(post)
                posts_cache[post_id] = post_dict
                return post_dict
            return None

    @staticmethod
    async def reject_post(post_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            await conn.execute("UPDATE posts SET status = 'rejected' WHERE id = $1", post_id)
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if post:
                # Удаляем из кеша
                posts_cache.pop(post_id, None)
                return dict(post)
            return None

    @staticmethod
    async def delete_post(post_id: int, telegram_id: int = None) -> bool:
        async with get_db_connection() as conn:
            if telegram_id:
                result = await conn.execute("DELETE FROM posts WHERE id = $1 AND telegram_id = $2", post_id, telegram_id)
            else:
                result = await conn.execute("DELETE FROM posts WHERE id = $1", post_id)
            
            # Удаляем из кеша
            posts_cache.pop(post_id, None)
            return result.split()[-1] == '1'

    @staticmethod
    async def like_post(post_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            await conn.execute("UPDATE posts SET likes = likes + 1 WHERE id = $1 AND status = 'approved'", post_id)
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if post:
                post_dict = dict(post)
                posts_cache[post_id] = post_dict
                return post_dict
            return None

    @staticmethod
    async def report_post(post_id: int, reporter_telegram_id: int, reason: str = None) -> bool:
        async with get_db_connection() as conn:
            await conn.execute("""
                INSERT INTO post_reports (post_id, reporter_telegram_id, reason) VALUES ($1, $2, $3)
            """, post_id, reporter_telegram_id, reason)
            
            # Увеличиваем счетчик жалоб
            await conn.execute("""
                UPDATE posts SET reports_count = reports_count + 1 WHERE id = $1
            """, post_id)
            return True

    @staticmethod
    async def get_post_by_id(post_id: int) -> Optional[Dict]:
        # Сначала проверяем кеш
        if post_id in posts_cache:
            return posts_cache[post_id]
        
        async with get_db_connection() as conn:
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if post:
                post_dict = dict(post)
                posts_cache[post_id] = post_dict
                return post_dict
            return None

    @staticmethod
    async def get_account_info(telegram_id: int) -> Optional[Dict]:
        # Проверяем кеш
        if telegram_id in user_cache:
            return user_cache[telegram_id]
        
        async with get_db_connection() as conn:
            account = await conn.fetchrow("SELECT * FROM accounts WHERE telegram_id = $1", telegram_id)
            if account:
                account_dict = dict(account)
                user_cache[telegram_id] = account_dict
                return account_dict
            return None

    @staticmethod
    async def is_user_banned(telegram_id: int) -> bool:
        # Проверяем кеш
        if telegram_id in user_cache:
            return user_cache[telegram_id].get('is_banned', False)
        
        async with get_db_connection() as conn:
            result = await conn.fetchval("SELECT is_banned FROM accounts WHERE telegram_id = $1", telegram_id)
            return result or False

    @staticmethod
    async def get_user_posts_today(telegram_id: int) -> int:
        # Проверяем кеш
        if telegram_id in user_cache:
            return user_cache[telegram_id].get('posts_today', 0)
        
        async with get_db_connection() as conn:
            result = await conn.fetchval("SELECT posts_today FROM accounts WHERE telegram_id = $1", telegram_id)
            return result or 0

    @staticmethod
    async def get_user_limit(telegram_id: int) -> int:
        # Проверяем кеш
        if telegram_id in user_cache:
            return user_cache[telegram_id].get('post_limit', config.DAILY_POST_LIMIT)
        
        async with get_db_connection() as conn:
            result = await conn.fetchval("SELECT post_limit FROM accounts WHERE telegram_id = $1", telegram_id)
            return result or config.DAILY_POST_LIMIT

    @staticmethod
    async def set_user_limit(telegram_id: int, limit: int) -> bool:
        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE accounts SET post_limit = $2 WHERE telegram_id = $1",
                telegram_id, limit
            )
            # Обновляем кеш
            if telegram_id in user_cache:
                user_cache[telegram_id]['post_limit'] = limit
            return True

    @staticmethod
    async def ban_user(telegram_id: int, reason: str = None) -> bool:
        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE accounts SET is_banned = TRUE, ban_reason = $2 WHERE telegram_id = $1",
                telegram_id, reason
            )
            # Обновляем кеш
            if telegram_id in user_cache:
                user_cache[telegram_id]['is_banned'] = True
                user_cache[telegram_id]['ban_reason'] = reason
            return True

    @staticmethod
    async def unban_user(telegram_id: int) -> bool:
        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE accounts SET is_banned = FALSE, ban_reason = NULL WHERE telegram_id = $1",
                telegram_id
            )
            # Обновляем кеш
            if telegram_id in user_cache:
                user_cache[telegram_id]['is_banned'] = False
                user_cache[telegram_id]['ban_reason'] = None
            return True

# Система лимитов (в памяти)
class PostLimitService:
    @staticmethod
    async def check_user_limit(telegram_id: int) -> bool:
        posts_today = await DatabaseService.get_user_posts_today(telegram_id)
        limit = await DatabaseService.get_user_limit(telegram_id)
        return posts_today < limit

# Telegram Bot
class ModerationBot:
    def __init__(self):
        self.app = None

    async def init_bot(self):
        if not config.BOT_TOKEN:
            raise ValueError("BOT_TOKEN not set")
        
        self.app = Application.builder().token(config.BOT_TOKEN).build()
        
        # Команды
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("delete", self.delete_command))
        self.app.add_handler(CommandHandler("ban", self.ban_command))
        self.app.add_handler(CommandHandler("unban", self.unban_command))
        self.app.add_handler(CommandHandler("setlimit", self.setlimit_command))
        self.app.add_handler(CommandHandler("getlimit", self.getlimit_command))
        self.app.add_handler(CommandHandler("userinfo", self.userinfo_command))
        self.app.add_handler(CallbackQueryHandler(self.handle_moderation_callback))
        
        await self.app.initialize()
        await self.app.start()
        
        global telegram_bot
        telegram_bot = self.app.bot
        
        logger.info("Moderation bot initialized")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 Бот модерации объявлений\n\n"
            "Команды модерации:\n"
            "/delete <post_id> - Удалить объявление\n\n"
            "Команды администрирования:\n"
            "/ban <telegram_id> [причина] - Забанить пользователя\n"
            "/unban <telegram_id> - Снять бан\n"
            "/setlimit <telegram_id> <лимит> - Установить лимит постов\n"
            "/getlimit <telegram_id> - Проверить лимит\n"
            "/userinfo <telegram_id> - Информация о пользователе"
        )

    async def delete_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /delete <post_id>")
            return
        
        try:
            post_id = int(context.args[0])
            post = await DatabaseService.get_post_by_id(post_id)
            
            if not post:
                await update.message.reply_text("Объявление не найдено")
                return
            
            success = await DatabaseService.delete_post(post_id)
            if success:
                await broadcast_message({
                    'type': 'post_deleted',
                    'post_id': post_id
                })
                
                try:
                    await telegram_bot.send_message(
                        chat_id=post['creator_telegram_id'],
                        text="🗑 Ваше объявление было удалено модератором"
                    )
                except Exception as e:
                    logger.error(f"Failed to notify user: {e}")
                
                await update.message.reply_text(f"✅ Объявление {post_id} удалено")
            else:
                await update.message.reply_text("❌ Ошибка при удалении объявления")
                
        except ValueError:
            await update.message.reply_text("❌ Неверный ID объявления")
        except Exception as e:
            logger.error(f"Delete command error: {e}")
            await update.message.reply_text("❌ Произошла ошибка")

    async def ban_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /ban <telegram_id> [причина]")
            return
        
        try:
            telegram_id = int(context.args[0])
            reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Нарушение правил"
            
            await DatabaseService.ban_user(telegram_id, reason)
            
            # Уведомить пользователя
            try:
                await telegram_bot.send_message(
                    chat_id=telegram_id,
                    text=f"🚫 Ваш аккаунт заблокирован.\nПричина: {reason}"
                )
            except Exception as e:
                logger.warning(f"Failed to notify banned user {telegram_id}: {e}")
            
            await update.message.reply_text(f"✅ Пользователь {telegram_id} заблокирован")
            
        except ValueError:
            await update.message.reply_text("❌ Неверный Telegram ID")
        except Exception as e:
            logger.error(f"Ban command error: {e}")
            await update.message.reply_text("❌ Произошла ошибка")

    async def unban_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /unban <telegram_id>")
            return
        
        try:
            telegram_id = int(context.args[0])
            
            await DatabaseService.unban_user(telegram_id)
            
            # Уведомить пользователя
            try:
                await telegram_bot.send_message(
                    chat_id=telegram_id,
                    text="✅ Ваш аккаунт разблокирован"
                )
            except Exception as e:
                logger.warning(f"Failed to notify unbanned user {telegram_id}: {e}")
            
            await update.message.reply_text(f"✅ Пользователь {telegram_id} разблокирован")
            
        except ValueError:
            await update.message.reply_text("❌ Неверный Telegram ID")
        except Exception as e:
            logger.error(f"Unban command error: {e}")
            await update.message.reply_text("❌ Произошла ошибка")

    async def setlimit_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) != 2:
            await update.message.reply_text("Использование: /setlimit <telegram_id> <лимит>")
            return
        
        try:
            telegram_id = int(context.args[0])
            limit = int(context.args[1])
            
            if limit < 0:
                await update.message.reply_text("❌ Лимит не может быть отрицательным")
                return
            
            await DatabaseService.set_user_limit(telegram_id, limit)
            
            # Уведомить пользователя
            try:
                await telegram_bot.send_message(
                    chat_id=telegram_id,
                    text=f"📊 Ваш лимит объявлений изменен на {limit} в день"
                )
            except Exception as e:
                logger.warning(f"Failed to notify user {telegram_id}: {e}")
            
            await update.message.reply_text(f"✅ Лимит для пользователя {telegram_id} установлен: {limit}")
            
        except ValueError:
            await update.message.reply_text("❌ Неверные параметры")
        except Exception as e:
            logger.error(f"Setlimit command error: {e}")
            await update.message.reply_text("❌ Произошла ошибка")

    async def getlimit_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /getlimit <telegram_id>")
            return
        
        try:
            telegram_id = int(context.args[0])
            account_info = await DatabaseService.get_account_info(telegram_id)
            
            if account_info:
                text = (
                    f"📊 Информация о лимитах для {telegram_id}:\n"
                    f"Лимит в день: {account_info.get('post_limit', config.DAILY_POST_LIMIT)}\n"
                    f"Постов сегодня: {account_info.get('posts_today', 0)}\n"
                    f"Заблокирован: {'Да' if account_info.get('is_banned') else 'Нет'}"
                )
                if account_info.get('ban_reason'):
                    text += f"\nПричина бана: {account_info['ban_reason']}"
            else:
                text = f"❌ Пользователь {telegram_id} не найден"
            
            await update.message.reply_text(text)
            
        except ValueError:
            await update.message.reply_text("❌ Неверный Telegram ID")
        except Exception as e:
            logger.error(f"Getlimit command error: {e}")
            await update.message.reply_text("❌ Произошла ошибка")

    async def userinfo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /userinfo <telegram_id>")
            return
        
        try:
            telegram_id = int(context.args[0])
            account_info = await DatabaseService.get_account_info(telegram_id)
            
            if not account_info:
                await update.message.reply_text("❌ Пользователь не найден")
                return
            
            text = (
                f"👤 Информация об аккаунте {telegram_id}:\n"
                f"Лимит постов: {account_info.get('post_limit', config.DAILY_POST_LIMIT)}\n"
                f"Постов сегодня: {account_info.get('posts_today', 0)}\n"
                f"Собственных постов: {len(account_info.get('own_posts', []))}\n"
                f"Избранных: {len(account_info.get('favorite_posts', []))}\n"
                f"Скрытых: {len(account_info.get('hidden_posts', []))}\n"
                f"Лайков: {len(account_info.get('liked_posts', []))}\n"
                f"Заблокирован: {'Да' if account_info.get('is_banned') else 'Нет'}\n"
                f"Зарегистрирован: {account_info['created_at'].strftime('%Y-%m-%d %H:%M')}"
            )
            
            if account_info.get('ban_reason'):
                text += f"\nПричина бана: {account_info['ban_reason']}"
            
            await update.message.reply_text(text)
            
        except ValueError:
            await update.message.reply_text("❌ Неверный Telegram ID")
        except Exception as e:
            logger.error(f"Userinfo command error: {e}")
            await update.message.reply_text("❌ Произошла ошибка")

    async def handle_moderation_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        data = query.data.split("_")
        action = data[0]
        post_id = int(data[1])
        
        post = await DatabaseService.get_post_by_id(post_id)
        if not post:
            await query.edit_message_text("❌ Объявление не найдено")
            return
        
        if action == "approve":
            approved_post = await DatabaseService.approve_post(post_id)
            if approved_post:
                await broadcast_message({
                    'type': 'post_updated',
                    'post': approved_post
                })
                await query.edit_message_text("✅ Объявление одобрено и опубликовано")
            else:
                await query.edit_message_text("❌ Ошибка при одобрении")
                
        elif action == "reject":
            await DatabaseService.reject_post(post_id)
            
            try:
                await telegram_bot.send_message(
                    chat_id=post['creator_telegram_id'],
                    text="❌ Ваше объявление было отклонено модератором за нарушение правил"
                )
            except Exception as e:
                logger.error(f"Failed to notify user: {e}")
            
            await query.edit_message_text("❌ Объявление отклонено")

    async def send_for_moderation(self, post: Dict):
        if not config.MODERATION_CHAT_ID:
            logger.warning("MODERATION_CHAT_ID not set, auto-approving post")
            return await DatabaseService.approve_post(post['id'])
        
        try:
            text = (
                f"📝 Новое объявление #{post['id']}\n\n"
                f"👤 От: {post['creator_first_name']} {post.get('creator_last_name', '')}\n"
                f"🆔 ID: {post['creator_telegram_id']}\n"
                f"👤 Username: @{post.get('creator_username', 'нет')}\n"
                f"📂 Категория: {post['category']}\n\n"
                f"📄 Текст:\n{post['description']}\n\n"
                f"🏷 Теги: {', '.join(json.loads(post['tags']) if post['tags'] else [])}"
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Принять", callback_data=f"approve_{post['id']}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{post['id']}")
                ]
            ])
            
            await telegram_bot.send_message(
                chat_id=config.MODERATION_CHAT_ID,
                text=text,
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Failed to send moderation message: {e}")
            return await DatabaseService.approve_post(post['id'])

# WebSocket
async def broadcast_message(message: Dict):
    if connected_clients:
        message_str = json.dumps(message)
        disconnected_clients = set()
        
        for client in connected_clients.copy():
            try:
                await client.send(message_str)
            except websockets.exceptions.ConnectionClosed:
                disconnected_clients.add(client)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                disconnected_clients.add(client)
        
        connected_clients -= disconnected_clients

async def handle_websocket(websocket: WebSocketServerProtocol):
    connected_clients.add(websocket)
    logger.info(f"Client connected. Total clients: {len(connected_clients)}")
    
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                await handle_websocket_message(websocket, data)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({'type': 'error', 'message': 'Invalid JSON'}))
            except Exception as e:
                logger.error(f"WebSocket message error: {e}")
                await websocket.send(json.dumps({'type': 'error', 'message': str(e)}))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)
        logger.info(f"Client disconnected. Total clients: {len(connected_clients)}")

async def handle_websocket_message(websocket: WebSocketServerProtocol, data: Dict):
    action = data.get('type')
    telegram_id = data.get('user_id')
    
    # Проверяем бан
    if telegram_id and await DatabaseService.is_user_banned(telegram_id):
        await websocket.send(json.dumps({
            'type': 'banned',
            'message': 'Ваш аккаунт заблокирован'
        }))
        return
    
    if action == 'sync_user':
        user_data = await DatabaseService.sync_user(telegram_id)
        await websocket.send(json.dumps({
            'type': 'user_synced',
            'user_id': user_data['user_id'],
            'limits': user_data['limits'],
            'is_banned': user_data['is_banned'],
            'ban_reason': user_data.get('ban_reason')
        }))
    
    elif action == 'create_post':
        # Проверка лимита
        if not await PostLimitService.check_user_limit(telegram_id):
            await websocket.send(json.dumps({
                'type': 'limit_exceeded',
                'message': f'Достигнут дневной лимит объявлений'
            }))
            return
        
        # Создание поста
        post = await DatabaseService.create_post({
            'telegram_id': telegram_id,
            'description': data['description'],
            'category': data['category'],
            'tags': data['tags'],
            'creator_data': data['creator_data']
        })
        
        # Получаем обновленные лимиты
        account_info = await DatabaseService.get_account_info(telegram_id)
        limits = {
            'used': account_info.get('posts_today', 0) if account_info else 0,
            'total': account_info.get('post_limit', config.DAILY_POST_LIMIT) if account_info else config.DAILY_POST_LIMIT
        }
        
        # Отправляем на модерацию
        if telegram_bot:
            moderation_bot = ModerationBot()
            await moderation_bot.send_for_moderation(post)
        
        await websocket.send(json.dumps({
            'type': 'post_created',
            'message': 'Объявление отправлено на модерацию',
            'limits': limits
        }))
    
    elif action == 'get_posts':
        posts = await DatabaseService.get_posts(
            data, data['page'], data['limit'], data.get('search', '')
        )
        await websocket.send(json.dumps({
            'type': 'posts',
            'posts': posts,
            'append': data.get('append', False)
        }))
    
    elif action == 'like_post':
        post = await DatabaseService.like_post(data['post_id'])
        if post:
            await broadcast_message({'type': 'post_updated', 'post': post})
    
    elif action == 'delete_post':
        success = await DatabaseService.delete_post(data['post_id'], telegram_id)
        if success:
            await broadcast_message({'type': 'post_deleted', 'post_id': data['post_id']})
    
    elif action == 'report_post':
        await DatabaseService.report_post(
            data['post_id'], 
            telegram_id, 
            data.get('reason')
        )

# Основная функция для запуска HTTP сервера статических файлов
async def serve_static_files():
    """Обслуживание статических файлов для фронтенда"""
    from aiohttp import web
    
    async def index_handler(request):
        """Возвращает index.html для всех маршрутов"""
        try:
            with open('index.html', 'r', encoding='utf-8') as f:
                content = f.read()
            return web.Response(text=content, content_type='text/html')
        except FileNotFoundError:
            return web.Response(text="index.html not found", status=404)
    
    async def health_handler(request):
        """Health check endpoint"""
        return web.Response(text="OK", status=200)
    
    app = web.Application()
    app.router.add_get('/health', health_handler)
    app.router.add_get('/', index_handler)
    app.router.add_get('/{path:.*}', index_handler)  # Catch-all для SPA
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Используем порт на 1 больше чем WebSocket
    http_port = config.PORT + 1
    site = web.TCPSite(runner, '0.0.0.0', http_port)
    await site.start()
    logger.info(f"HTTP server started on port {http_port}")

# Основная функция
async def main():
    # Инициализация базы данных
    await DatabaseService.init_database()
    
    # Инициализация бота
    moderation_bot = ModerationBot()
    await moderation_bot.init_bot()
    
    # Запуск HTTP сервера для статических файлов
    await serve_static_files()
    
    # Запуск WebSocket сервера
    server = await websockets.serve(handle_websocket, '0.0.0.0', config.PORT)
    logger.info(f"WebSocket server started on port {config.PORT}")
    
    # Запуск бота
    await moderation_bot.app.updater.start_polling()
    
    try:
        await asyncio.Future()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await moderation_bot.app.stop()
        server.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application error: {e}")
        raise
