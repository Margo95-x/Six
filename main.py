#!/usr/bin/env python3
"""
Telegram Web App Server
"""

import asyncio
import logging
import os
import json
from datetime import datetime
from typing import Optional, List, Dict
import asyncpg
from contextlib import asynccontextmanager
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from aiohttp import web, WSMsgType
import aiohttp_cors
from dataclasses import dataclass

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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

db_pool = None
telegram_bot = None
connected_clients = set()
user_cache = {}

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
        
        database_url = config.DATABASE_URL
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)
        
        ssl_context = None
        if 'localhost' not in database_url and 'sslmode' not in database_url:
            import ssl
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        
        db_pool = await asyncpg.create_pool(
            database_url,
            min_size=config.DB_MIN_SIZE,
            max_size=config.DB_MAX_SIZE,
            command_timeout=config.DB_COMMAND_TIMEOUT,
            ssl=ssl_context
        )
        
        async with get_db_connection() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    photo_url TEXT,
                    favorites BIGINT[] DEFAULT '{}',
                    hidden BIGINT[] DEFAULT '{}',
                    liked BIGINT[] DEFAULT '{}',
                    reported_posts BIGINT[] DEFAULT '{}',
                    posts BIGINT[] DEFAULT '{}',
                    is_banned BOOLEAN DEFAULT FALSE,
                    ban_reason TEXT,
                    post_limit INTEGER DEFAULT 60,
                    last_post_count_reset DATE DEFAULT CURRENT_DATE,
                    posts_today INTEGER DEFAULT 0,
                    language TEXT DEFAULT 'ru',
                    notification_settings JSONB DEFAULT '{"likes": true, "system": true, "account": true}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                    description TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tags JSONB NOT NULL DEFAULT '[]',
                    likes INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    moderation_message_id INTEGER,
                    is_edit BOOLEAN DEFAULT FALSE,
                    original_post_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    creator JSONB NOT NULL
                )
            """)
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS post_reports (
                    id SERIAL PRIMARY KEY,
                    post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
                    reporter_id BIGINT NOT NULL REFERENCES users(telegram_id),
                    reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT DEFAULT 'ru'")
                await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS notification_settings JSONB DEFAULT \'{"likes": true, "system": true, "account": true}\'')
                await conn.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS is_edit BOOLEAN DEFAULT FALSE")
                await conn.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS original_post_id INTEGER")
                await conn.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                
                await conn.execute("UPDATE users SET liked = '{}' WHERE liked IS NULL")
                await conn.execute("UPDATE users SET reported_posts = '{}' WHERE reported_posts IS NULL")
                await conn.execute("UPDATE users SET favorites = '{}' WHERE favorites IS NULL")
                await conn.execute("UPDATE users SET hidden = '{}' WHERE hidden IS NULL")
                await conn.execute("UPDATE users SET posts = '{}' WHERE posts IS NULL")
            except Exception as e:
                logger.warning(f"Migration warning: {e}")
            
            try:
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_telegram_id ON posts(telegram_id)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at DESC)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)")
            except Exception as e:
                logger.error(f"Error creating indexes: {e}")

    @staticmethod
    async def sync_user(user_data: Dict) -> Dict:
        async with get_db_connection() as conn:
            user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_data['telegram_id'])
            
            if not user:
                await conn.execute("""
                    INSERT INTO users (telegram_id, username, first_name, last_name, photo_url, language)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """, user_data['telegram_id'], user_data['username'], user_data['first_name'],
                    user_data['last_name'], user_data['photo_url'], user_data.get('language', 'ru'))
                user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_data['telegram_id'])
            else:
                await conn.execute("""
                    UPDATE users SET username = $2, first_name = $3, last_name = $4, photo_url = $5
                    WHERE telegram_id = $1
                """, user_data['telegram_id'], user_data['username'], user_data['first_name'],
                    user_data['last_name'], user_data['photo_url'])
            
            published_count = await conn.fetchval("""
                SELECT COUNT(*) FROM posts 
                WHERE telegram_id = $1 AND status = 'approved'
            """, user_data['telegram_id'])
            
            user_dict = dict(user)
            user_dict['published_posts'] = published_count
            user_cache[user_data['telegram_id']] = user_dict
            
            return {
                'telegram_id': user_dict['telegram_id'],
                'limits': {
                    'used': published_count,
                    'total': user_dict.get('post_limit', config.DAILY_POST_LIMIT)
                },
                'is_banned': user_dict.get('is_banned', False),
                'language': user_dict.get('language', 'ru'),
                'favorites': user_dict.get('favorites', []),
                'hidden': user_dict.get('hidden', []),
                'liked': user_dict.get('liked', []),
                'notification_settings': user_dict.get('notification_settings', {"likes": True, "system": True, "account": True})
            }

    @staticmethod
    async def create_post(post_data: Dict) -> Dict:
        description = post_data.get('description', '').strip()
        if len(description) < 10:
            raise ValueError("Описание поста слишком короткое (менее 10 символов)")
        
        # Проверка лимита постов
        if not await PostLimitService.check_user_limit(post_data['telegram_id']):
            raise ValueError("Достигнут лимит объявлений")
        
        async with get_db_connection() as conn:
            # Создаём пост сразу со статусом 'approved'
            post_id = await conn.fetchval("""
                INSERT INTO posts (telegram_id, description, category, tags, creator, status, is_edit, original_post_id)
                VALUES ($1, $2, $3, $4, $5, 'approved', $6, $7) RETURNING id
            """, post_data['telegram_id'], post_data['description'], post_data['category'],
                json.dumps(post_data['tags']), json.dumps(post_data['creator']),
                post_data.get('is_edit', False), post_data.get('original_post_id'))
            
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            post_dict = dict(post)
            
            # Обновляем счётчик опубликованных постов
            published_count = await DatabaseService.get_user_published_posts_count(post_data['telegram_id'])
            limit = await DatabaseService.get_user_limit(post_data['telegram_id'])
            await broadcast_message({
                'type': 'user_limits_updated',
                'telegram_id': post_data['telegram_id'],
                'limits': {
                    'used': published_count,
                    'total': limit
                }
            })
            
            # Отправляем уведомление о публикации всем клиентам
            await broadcast_message({
                'type': 'post_approved',
                'post': post_dict
            })
            
            # Уведомляем создателя, если включены системные уведомления
            creator_lang = (await DatabaseService.get_user_info(post_data['telegram_id']))['language'] or 'ru'
            notification_settings = (await DatabaseService.get_user_info(post_data['telegram_id']))['notification_settings']
            if notification_settings.get('system'):
                msg = "Your post has been published!" if creator_lang == 'en' else "Ваше объявление опубликовано!"
                await telegram_bot.send_message(chat_id=post_data['telegram_id'], text=msg)
            
            return post_dict

    @staticmethod
    async def get_posts(filters: Dict, page: int, limit: int, search: str = '', telegram_id: int = None) -> List[Dict]:
        async with get_db_connection() as conn:
            if telegram_id:
                query = """
                    SELECT p.*, 
                        (CASE WHEN p.id = ANY(COALESCE(u.liked, ARRAY[]::BIGINT[])) THEN TRUE ELSE FALSE END) as user_liked,
                        (CASE WHEN p.id = ANY(COALESCE(u.favorites, ARRAY[]::BIGINT[])) THEN TRUE ELSE FALSE END) as user_favorited,
                        (CASE WHEN p.id = ANY(COALESCE(u.hidden, ARRAY[]::BIGINT[])) THEN TRUE ELSE FALSE END) as user_hidden
                    FROM posts p
                    LEFT JOIN users u ON u.telegram_id = $1
                    WHERE p.status = 'approved'
                """
                params = [telegram_id]
            else:
                query = """
                    SELECT p.*, FALSE as user_liked, FALSE as user_favorited, FALSE as user_hidden
                    FROM posts p
                    WHERE p.status = 'approved'
                """
                params = []
            
            if filters.get('filters', {}).get('sort') != 'hidden' and telegram_id:
                query += " AND (p.id <> ALL(COALESCE(u.hidden, ARRAY[]::BIGINT[])))"
            
            if filters.get('category'):
                params.append(filters['category'])
                query += f" AND p.category = ${len(params)}"
            
            if search:
                params.append(f"%{search}%")
                query += f" AND LOWER(p.description) LIKE LOWER(${len(params)})"
            
            if filters.get('filters'):
                for filter_type, values in filters['filters'].items():
                    if values and filter_type != 'sort' and isinstance(values, list):
                        for value in values:
                            params.append(json.dumps([f"{filter_type}:{value}"]))
                            query += f" AND p.tags @> ${len(params)}::JSONB"
            
            if filters.get('filters', {}).get('sort'):
                sort_type = filters['filters']['sort']
                if sort_type == 'my' and telegram_id:
                    params.append(telegram_id)
                    query += f" AND p.telegram_id = ${len(params)}"
                elif sort_type == 'favorites' and telegram_id:
                    query += " AND p.id = ANY(COALESCE(u.favorites, ARRAY[]::BIGINT[]))"
                elif sort_type == 'hidden' and telegram_id:
                    query += " AND p.id = ANY(COALESCE(u.hidden, ARRAY[]::BIGINT[]))"
            
            sort_type = filters.get('filters', {}).get('sort', 'new')
            if sort_type == 'old':
                query += " ORDER BY p.created_at ASC"
            elif sort_type == 'rating':
                query += " ORDER BY p.likes DESC, p.created_at DESC"
            else:
                query += " ORDER BY p.created_at DESC"
            
            query += f" LIMIT {limit} OFFSET {(page - 1) * limit}"
            
            posts = await conn.fetch(query, *params)
            return [dict(post) for post in posts]

    @staticmethod
    async def approve_post(post_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if not post:
                return None
            
            if post['is_edit'] and post['original_post_id']:
                await conn.execute("""
                    UPDATE posts SET 
                        description = $2,
                        category = $3,
                        tags = $4,
                        updated_at = CURRENT_TIMESTAMP,
                        status = 'approved'
                    WHERE id = $1
                """, post['original_post_id'], post['description'], post['category'], post['tags'])
                
                await conn.execute("DELETE FROM posts WHERE id = $1", post_id)
                
                updated_post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post['original_post_id'])
                return dict(updated_post) if updated_post else None
            else:
                await conn.execute("UPDATE posts SET status = 'approved' WHERE id = $1", post_id)
                updated_post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
                return dict(updated_post) if updated_post else None

    @staticmethod
    async def reject_post(post_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if not post:
                return None
            await conn.execute("UPDATE posts SET status = 'rejected' WHERE id = $1", post_id)
            return dict(post)

    @staticmethod
    async def delete_post(post_id: int, telegram_id: int = None) -> bool:
        async with get_db_connection() as conn:
            if telegram_id:
                result = await conn.execute("DELETE FROM posts WHERE id = $1 AND telegram_id = $2", post_id, telegram_id)
            else:
                result = await conn.execute("DELETE FROM posts WHERE id = $1", post_id)
            return result.split()[-1] == '1'

    @staticmethod
    async def like_post(post_id: int, telegram_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            user = await conn.fetchrow("SELECT liked, username, notification_settings FROM users WHERE telegram_id = $1", telegram_id)
            if not user:
                return None
            
            liked_posts = user['liked'] or []
            notification_settings = user['notification_settings'] or {"likes": True, "system": True, "account": True}
            
            if post_id in liked_posts:
                await conn.execute("""
                    UPDATE users SET liked = array_remove(liked, $1) WHERE telegram_id = $2
                """, post_id, telegram_id)
                await conn.execute("""
                    UPDATE posts SET likes = likes - 1 WHERE id = $1 AND status = 'approved'
                """, post_id)
                action = 'removed'
            else:
                await conn.execute("""
                    UPDATE users SET liked = array_append(COALESCE(liked, ARRAY[]::BIGINT[]), $1) WHERE telegram_id = $2
                """, post_id, telegram_id)
                await conn.execute("""
                    UPDATE posts SET likes = likes + 1 WHERE id = $1 AND status = 'approved'
                """, post_id)
                action = 'added'
            
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            if post and notification_settings.get('likes') and action == 'added':
                creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
                creator_lang = (await conn.fetchval("SELECT language FROM users WHERE telegram_id = $1", creator['telegram_id'])) or 'ru'
                msg = f"@{user['username']} liked your post #{post_id}" if creator_lang == 'en' else f"@{user['username']} поставил лайк на пост #{post_id}"
                await telegram_bot.send_message(chat_id=creator['telegram_id'], text=msg)
            
            if post:
                post_dict = dict(post)
                post_dict['like_action'] = action
                post_dict['user_liked'] = action == 'added'
                return post_dict
            return None

    @staticmethod
    async def report_post(post_id: int, reporter_id: int, reason: str = None) -> Dict:
        async with get_db_connection() as conn:
            user = await conn.fetchrow("SELECT reported_posts FROM users WHERE telegram_id = $1", reporter_id)
            if user and user['reported_posts'] and post_id in user['reported_posts']:
                return {'success': False, 'message': 'already_reported'}
            
            await conn.execute("""
                INSERT INTO post_reports (post_id, reporter_id, reason) VALUES ($1, $2, $3)
            """, post_id, reporter_id, reason)
            
            await conn.execute("""
                UPDATE users SET reported_posts = array_append(COALESCE(reported_posts, ARRAY[]::BIGINT[]), $1) WHERE telegram_id = $2
            """, post_id, reporter_id)
            
            return {'success': True, 'message': 'reported'}

    @staticmethod
    async def get_post_by_id(post_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            return dict(post) if post else None

    @staticmethod
    async def add_to_favorites(post_id: int, telegram_id: int) -> Dict:
        async with get_db_connection() as conn:
            user = await conn.fetchrow("SELECT favorites FROM users WHERE telegram_id = $1", telegram_id)
            if not user:
                return {'success': False, 'message': 'user_not_found'}
            
            favorites = user['favorites'] or []
            
            if post_id in favorites:
                await conn.execute("""
                    UPDATE users SET favorites = array_remove(COALESCE(favorites, ARRAY[]::BIGINT[]), $1) WHERE telegram_id = $2
                """, post_id, telegram_id)
                return {'success': True, 'action': 'removed', 'message': 'removed_from_favorites'}
            else:
                await conn.execute("""
                    UPDATE users SET favorites = array_append(COALESCE(favorites, ARRAY[]::BIGINT[]), $1) WHERE telegram_id = $2
                """, post_id, telegram_id)
                return {'success': True, 'action': 'added', 'message': 'added_to_favorites'}

    @staticmethod
    async def hide_post(post_id: int, telegram_id: int) -> Dict:
        async with get_db_connection() as conn:
            user = await conn.fetchrow("SELECT hidden FROM users WHERE telegram_id = $1", telegram_id)
            if not user:
                return {'success': False, 'message': 'user_not_found'}
            
            hidden = user['hidden'] or []
            
            if post_id in hidden:
                await conn.execute("""
                    UPDATE users SET hidden = array_remove(COALESCE(hidden, ARRAY[]::BIGINT[]), $1) WHERE telegram_id = $2
                """, post_id, telegram_id)
                return {'success': True, 'action': 'shown', 'message': 'post_shown'}
            else:
                await conn.execute("""
                    UPDATE users SET hidden = array_append(COALESCE(hidden, ARRAY[]::BIGINT[]), $1) WHERE telegram_id = $2
                """, post_id, telegram_id)
                return {'success': True, 'action': 'hidden', 'message': 'post_hidden'}

    @staticmethod
    async def is_user_banned(telegram_id: int) -> bool:
        if telegram_id in user_cache:
            return user_cache[telegram_id].get('is_banned', False)
        
        async with get_db_connection() as conn:
            result = await conn.fetchval("SELECT is_banned FROM users WHERE telegram_id = $1", telegram_id)
            return result or False

    @staticmethod
    async def get_user_limit(telegram_id: int) -> int:
        if telegram_id in user_cache:
            return user_cache[telegram_id].get('post_limit', config.DAILY_POST_LIMIT)
        
        async with get_db_connection() as conn:
            result = await conn.fetchval("SELECT post_limit FROM users WHERE telegram_id = $1", telegram_id)
            return result or config.DAILY_POST_LIMIT

    @staticmethod
    async def get_user_published_posts_count(telegram_id: int) -> int:
        async with get_db_connection() as conn:
            result = await conn.fetchval("""
                SELECT COUNT(*) FROM posts 
                WHERE telegram_id = $1 AND status = 'approved'
            """, telegram_id)
            return result or 0

    @staticmethod
    async def ban_user(telegram_id: int, reason: str = None) -> bool:
        async with get_db_connection() as conn:
            await conn.execute("""
                UPDATE users SET is_banned = TRUE, ban_reason = $2 WHERE telegram_id = $1
            """, telegram_id, reason)
            if telegram_id in user_cache:
                user_cache[telegram_id]['is_banned'] = True
                user_cache[telegram_id]['ban_reason'] = reason
            
            user = await conn.fetchrow("SELECT language, notification_settings FROM users WHERE telegram_id = $1", telegram_id)
            if user and user['notification_settings'].get('account'):
                msg = f"Your account has been banned. Reason: {reason}" if user['language'] == 'en' else f"Ваш аккаунт заблокирован. Причина: {reason}"
                await telegram_bot.send_message(chat_id=telegram_id, text=msg)
            return True

    @staticmethod
    async def hard_ban_user(telegram_id: int, reason: str = None) -> List[int]:
        async with get_db_connection() as conn:
            user_posts = await conn.fetch("SELECT id FROM posts WHERE telegram_id = $1", telegram_id)
            
            await conn.execute("""
                UPDATE users SET is_banned = TRUE, ban_reason = $2 WHERE telegram_id = $1
            """, telegram_id, reason)
            
            await conn.execute("DELETE FROM posts WHERE telegram_id = $1", telegram_id)
            
            if telegram_id in user_cache:
                user_cache[telegram_id]['is_banned'] = True
                user_cache[telegram_id]['ban_reason'] = reason
            
            user = await conn.fetchrow("SELECT language, notification_settings FROM users WHERE telegram_id = $1", telegram_id)
            if user and user['notification_settings'].get('account'):
                msg = f"Your account has been hard banned, all posts removed. Reason: {reason}" if user['language'] == 'en' else f"Ваш аккаунт жестко заблокирован, все посты удалены. Причина: {reason}"
                await telegram_bot.send_message(chat_id=telegram_id, text=msg)
            
            return [dict(post)['id'] for post in user_posts]

    @staticmethod
    async def unban_user(telegram_id: int) -> bool:
        async with get_db_connection() as conn:
            await conn.execute("""
                UPDATE users SET is_banned = FALSE, ban_reason = NULL WHERE telegram_id = $1
            """, telegram_id)
            if telegram_id in user_cache:
                user_cache[telegram_id]['is_banned'] = False
                user_cache[telegram_id]['ban_reason'] = None
            
            user = await conn.fetchrow("SELECT language, notification_settings FROM users WHERE telegram_id = $1", telegram_id)
            if user and user['notification_settings'].get('account'):
                msg = "Your account has been unbanned" if user['language'] == 'en' else "Ваш аккаунт разблокирован"
                await telegram_bot.send_message(chat_id=telegram_id, text=msg)
            return True

    @staticmethod
    async def set_user_limit(telegram_id: int, limit: int) -> bool:
        async with get_db_connection() as conn:
            await conn.execute("""
                UPDATE users SET post_limit = $2 WHERE telegram_id = $1
            """, telegram_id, limit)
            if telegram_id in user_cache:
                user_cache[telegram_id]['post_limit'] = limit
            
            published_count = await DatabaseService.get_user_published_posts_count(telegram_id)
            await broadcast_message({
                'type': 'user_limits_updated',
                'telegram_id': telegram_id,
                'limits': {
                    'used': published_count,
                    'total': limit
                }
            })
            
            user = await conn.fetchrow("SELECT language, notification_settings FROM users WHERE telegram_id = $1", telegram_id)
            if user and user['notification_settings'].get('account'):
                msg = f"Your post limit has been set to {limit}" if user['language'] == 'en' else f"Ваш лимит постов установлен на {limit}"
                await telegram_bot.send_message(chat_id=telegram_id, text=msg)
            return True

    @staticmethod
    async def get_user_info(telegram_id: int) -> Optional[Dict]:
        async with get_db_connection() as conn:
            user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
            return dict(user) if user else None

    @staticmethod
    async def update_notification_settings(telegram_id: int, settings: Dict) -> bool:
        async with get_db_connection() as conn:
            await conn.execute("""
                UPDATE users SET notification_settings = $2 WHERE telegram_id = $1
            """, telegram_id, json.dumps(settings))
            if telegram_id in user_cache:
                user_cache[telegram_id]['notification_settings'] = settings
            return True

class PostLimitService:
    @staticmethod
    async def check_user_limit(telegram_id: int) -> bool:
        published_count = await DatabaseService.get_user_published_posts_count(telegram_id)
        limit = await DatabaseService.get_user_limit(telegram_id)
        return published_count < limit

class ModerationBot:
    def __init__(self):
        self.app = None

    async def init_bot(self):
        if not config.BOT_TOKEN:
            logger.warning("BOT_TOKEN not set, bot will not be available")
            return
        
        try:
            self.app = Application.builder().token(config.BOT_TOKEN).build()
            
            self.app.add_handler(CommandHandler("start", self.start_command))
            self.app.add_handler(CommandHandler("delete", self.delete_command))
            self.app.add_handler(CommandHandler("ban", self.ban_command))
            self.app.add_handler(CommandHandler("hardban", self.hardban_command))
            self.app.add_handler(CommandHandler("unban", self.unban_command))
            self.app.add_handler(CommandHandler("setlimit", self.setlimit_command))
            self.app.add_handler(CommandHandler("getlimit", self.getlimit_command))
            self.app.add_handler(CallbackQueryHandler(self.handle_moderation_callback))
            
            await self.app.initialize()
            await self.app.start()
            
            try:
                await self.app.bot.delete_webhook(drop_pending_updates=True)
            except Exception as e:
                logger.warning(f"Could not clear webhook: {e}")
            
            global telegram_bot
            telegram_bot = self.app.bot
            
        except Exception as e:
            logger.error(f"Failed to initialize bot: {e}")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 Бот модерации объявлений\n\n"
            "Команды:\n"
            "/delete <post_id> - Удалить объявление\n"
            "/ban <telegram_id> - Забанить пользователя\n"
            "/hardban <telegram_id> - Забанить + удалить все посты\n"
            "/unban <telegram_id> - Разбанить пользователя\n"
            "/setlimit <telegram_id> <limit> - Установить лимит постов\n"
            "/getlimit <telegram_id> - Посмотреть лимит пользователя"
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
                
                creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
                creator_lang = (await DatabaseService.get_user_info(creator['telegram_id']))['language'] or 'ru'
                msg = "Your post was deleted by a moderator" if creator_lang == 'en' else "Ваше объявление удалено модератором"
                await telegram_bot.send_message(chat_id=creator['telegram_id'], text=msg)
                
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
            await update.message.reply_text("Использование: /ban <telegram_id>")
            return
        
        try:
            telegram_id = int(context.args[0])
            await DatabaseService.ban_user(telegram_id, "Banned by moderator")
            await update.message.reply_text(f"✅ Пользователь {telegram_id} забанен")
        except ValueError:
            await update.message.reply_text("❌ Неверный Telegram ID")
        except Exception as e:
            logger.error(f"Ban command error: {e}")
            await update.message.reply_text("❌ Произошла ошибка")

    async def hardban_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /hardban <telegram_id>")
            return
        
        try:
            telegram_id = int(context.args[0])
            deleted_post_ids = await DatabaseService.hard_ban_user(telegram_id, "Hard banned by moderator")
            
            for post_id in deleted_post_ids:
                await broadcast_message({
                    'type': 'post_deleted',
                    'post_id': post_id
                })
            
            await broadcast_message({
                'type': 'user_banned',
                'telegram_id': telegram_id
            })
            await update.message.reply_text(f"✅ Пользователь {telegram_id} забанен, все посты удалены")
        except ValueError:
            await update.message.reply_text("❌ Неверный Telegram ID")
        except Exception as e:
            logger.error(f"Hardban command error: {e}")
            await update.message.reply_text("❌ Произошла ошибка")

    async def unban_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /unban <telegram_id>")
            return
        
        try:
            telegram_id = int(context.args[0])
            await DatabaseService.unban_user(telegram_id)
            await update.message.reply_text(f"✅ Пользователь {telegram_id} разбанен")
        except ValueError:
            await update.message.reply_text("❌ Неверный Telegram ID")
        except Exception as e:
            logger.error(f"Unban command error: {e}")
            await update.message.reply_text("❌ Произошла ошибка")

    async def setlimit_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) != 2:
            await update.message.reply_text("Использование: /setlimit <telegram_id> <limit>")
            return
        
        try:
            telegram_id = int(context.args[0])
            limit = int(context.args[1])
            await DatabaseService.set_user_limit(telegram_id, limit)
            await update.message.reply_text(f"✅ Лимит пользователя {telegram_id} установлен: {limit}")
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
            user_info = await DatabaseService.get_user_info(telegram_id)
            if user_info:
                published_count = await DatabaseService.get_user_published_posts_count(telegram_id)
                await update.message.reply_text(
                    f"👤 Пользователь: {user_info['first_name']} {user_info['last_name']}\n"
                    f"🆔 ID: {telegram_id}\n"
                    f"📊 Лимит: {user_info['post_limit']}\n"
                    f"📝 Опубликовано постов: {published_count}\n"
                    f"🚫 Забанен: {'Да' if user_info['is_banned'] else 'Нет'}"
                )
            else:
                await update.message.reply_text("❌ Пользователь не найден")
        except ValueError:
            await update.message.reply_text("❌ Неверный Telegram ID")
        except Exception as e:
            logger.error(f"Getlimit command error: {e}")
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
        
        try:
            if action == "approve":
                approved_post = await DatabaseService.approve_post(post_id)
                if approved_post:
                    approved_post['is_edit'] = post.get('is_edit', False)
                    approved_post['original_post_id'] = post.get('original_post_id')
                    
                    await broadcast_message({
                        'type': 'post_approved',
                        'post': approved_post
                    })
                    
                    creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
                    creator_lang = (await DatabaseService.get_user_info(creator['telegram_id']))['language'] or 'ru'
                    notification_settings = (await DatabaseService.get_user_info(creator['telegram_id']))['notification_settings']
                    if notification_settings.get('system'):
                        msg = "Your post has been approved and published!" if creator_lang == 'en' else "Ваше объявление одобрено и опубликовано!"
                        await telegram_bot.send_message(chat_id=creator['telegram_id'], text=msg)
                    
                    new_text = query.message.text + f"\n\n✅ Объявление опубликовано\n🆔 ID объявления: {approved_post['id']}"
                    if new_text != query.message.text:
                        await query.edit_message_text(new_text)
                    else:
                        await query.answer("Действие выполнено")
                else:
                    await query.edit_message_text("❌ Ошибка при одобрении")
                    
            elif action == "reject":
                rejected_post = await DatabaseService.reject_post(post_id)
                if rejected_post:
                    creator = json.loads(rejected_post['creator']) if isinstance(rejected_post['creator'], str) else rejected_post['creator']
                    creator_lang = (await DatabaseService.get_user_info(creator['telegram_id']))['language'] or 'ru'
                    notification_settings = (await DatabaseService.get_user_info(creator['telegram_id']))['notification_settings']
                    if notification_settings.get('system'):
                        msg = "Your post has been rejected." if creator_lang == 'en' else "Ваше объявление отклонено."
                        await telegram_bot.send_message(chat_id=creator['telegram_id'], text=msg)

                    new_text = query.message.text + "\n\n❌ Объявление отклонено"
                    await query.edit_message_text(new_text)
                    
            elif action == "delete":
                success = await DatabaseService.delete_post(post_id)
                if success:
                    await broadcast_message({
                        'type': 'post_deleted',
                        'post_id': post_id
                    })
                    
                    creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
                    creator_lang = (await DatabaseService.get_user_info(creator['telegram_id']))['language'] or 'ru'
                    notification_settings = (await DatabaseService.get_user_info(creator['telegram_id']))['notification_settings']
                    if notification_settings.get('system'):
                        msg = "Your post has been deleted by a moderator." if creator_lang == 'en' else "Ваше объявление удалено модератором."
                        await telegram_bot.send_message(chat_id=creator['telegram_id'], text=msg)
                    
                    new_text = query.message.text + "\n\n🗑 Объявление удалено"
                    await query.edit_message_text(new_text)
                    
            elif action == "keep":
                new_text = query.message.text + "\n\n✅ Объявление проверено, все в порядке"
                await query.edit_message_text(new_text)
                    
        except Exception as e:
            logger.error(f"Moderation callback error: {e}")
            await query.edit_message_text("❌ Произошла ошибка")
