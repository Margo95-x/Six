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
    REPORTS_THREAD_ID: int = int(os.getenv("REPORTS_THREAD_ID", "126"))
    PORT: int = int(os.getenv("PORT", "10000"))
    DAILY_POST_LIMIT: int = 10
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
            logger.error("DATABASE_URL not set!")
            raise ValueError("DATABASE_URL not set")
        
        logger.info(f"Attempting to connect to database...")
        database_url = config.DATABASE_URL
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)
        
        ssl_context = None
        if 'localhost' not in database_url and 'sslmode' not in database_url:
            import ssl
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        
        try:
            db_pool = await asyncpg.create_pool(
                database_url,
                min_size=config.DB_MIN_SIZE,
                max_size=config.DB_MAX_SIZE,
                command_timeout=config.DB_COMMAND_TIMEOUT,
                ssl=ssl_context
            )
            logger.info("Database pool created successfully")
        except Exception as e:
            logger.error(f"Failed to create database pool: {e}")
            raise
        
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
                    post_limit INTEGER DEFAULT 10,
                    last_post_count_reset DATE DEFAULT CURRENT_DATE,
                    posts_today INTEGER DEFAULT 0,
                    language TEXT DEFAULT 'ru',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notification_settings JSONB DEFAULT '{"likes": true, "system": true, "account": true, "new_posts": false, "new_posts_categories": [], "new_posts_tags": []}'
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
                    status TEXT DEFAULT 'approved',
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
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notification_settings JSONB DEFAULT '{\"likes\": true, \"system\": true, \"account\": true, \"new_posts\": false, \"new_posts_categories\": [], \"new_posts_tags\": []}'")
                await conn.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS is_edit BOOLEAN DEFAULT FALSE")
                await conn.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS original_post_id INTEGER")
                await conn.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                
                await conn.execute("UPDATE users SET liked = '{}' WHERE liked IS NULL")
                await conn.execute("UPDATE users SET reported_posts = '{}' WHERE reported_posts IS NULL")
                await conn.execute("UPDATE users SET favorites = '{}' WHERE favorites IS NULL")
                await conn.execute("UPDATE users SET hidden = '{}' WHERE hidden IS NULL")
                await conn.execute("UPDATE users SET posts = '{}' WHERE posts IS NULL")
                await conn.execute("UPDATE users SET notification_settings = '{\"likes\": true, \"system\": true, \"account\": true, \"new_posts\": false, \"new_posts_categories\": [], \"new_posts_tags\": []}' WHERE notification_settings IS NULL")
                
                # Обновляем статус всех постов на approved (новая логика)
                await conn.execute("UPDATE posts SET status = 'approved' WHERE status = 'pending'")
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
                'notification_settings': user_dict.get('notification_settings', {
                    'likes': True, 'system': True, 'account': True, 
                    'new_posts': False, 'new_posts_categories': [], 'new_posts_tags': []
                })
            }

    @staticmethod
    async def create_post(post_data: Dict) -> Dict:
        async with get_db_connection() as conn:
            # Посты теперь сразу публикуются
            post_id = await conn.fetchval("""
                INSERT INTO posts (telegram_id, description, category, tags, creator, status, is_edit, original_post_id)
                VALUES ($1, $2, $3, $4, $5, 'approved', $6, $7) RETURNING id
            """, post_data['telegram_id'], post_data['description'], post_data['category'],
                json.dumps(post_data['tags']), json.dumps(post_data['creator']),
                post_data.get('is_edit', False), post_data.get('original_post_id'))
            
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            
            # Отправляем уведомления пользователям с настроенными фильтрами
            await NotificationService.send_new_post_notifications(dict(post))
            
            return dict(post)

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
            user = await conn.fetchrow("SELECT liked FROM users WHERE telegram_id = $1", telegram_id)
            if not user:
                return None
            
            liked_posts = user['liked'] or []
            
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
                
                # Отправляем уведомление автору поста о лайке
                post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
                if post and action == 'added':
                    await NotificationService.send_like_notification(dict(post), telegram_id)
            
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
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
            
            # Отправляем уведомление пользователю
            await NotificationService.send_account_notification(telegram_id, 'ban', reason)
            return True

    @staticmethod
    async def hard_ban_user(telegram_id: int, reason: str = None) -> bool:
        async with get_db_connection() as conn:
            user_posts = await conn.fetch("SELECT id FROM posts WHERE telegram_id = $1", telegram_id)
            
            await conn.execute("""
                UPDATE users SET is_banned = TRUE, ban_reason = $2 WHERE telegram_id = $1
            """, telegram_id, reason)
            
            await conn.execute("DELETE FROM posts WHERE telegram_id = $1", telegram_id)
            
            if telegram_id in user_cache:
                user_cache[telegram_id]['is_banned'] = True
                user_cache[telegram_id]['ban_reason'] = reason
            
            # Отправляем уведомление пользователю
            await NotificationService.send_account_notification(telegram_id, 'hard_ban', reason)
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
            
            # Отправляем уведомление пользователю
            await NotificationService.send_account_notification(telegram_id, 'unban')
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
            
            # Отправляем уведомление пользователю
            await NotificationService.send_account_notification(telegram_id, 'limit_change', f'Новый лимит: {limit}')
            
            await broadcast_message({
                'type': 'user_limits_updated',
                'telegram_id': telegram_id,
                'limits': {
                    'used': published_count,
                    'total': limit
                }
            })
            
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

class NotificationService:
    @staticmethod
    async def send_like_notification(post: Dict, liker_id: int):
        if not telegram_bot:
            return
        
        try:
            creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
            author_id = creator['telegram_id']
            
            # Проверяем настройки уведомлений автора
            user_info = await DatabaseService.get_user_info(author_id)
            if not user_info or not user_info.get('notification_settings', {}).get('likes', True):
                return
            
            # Получаем информацию о том, кто поставил лайк
            liker_info = await DatabaseService.get_user_info(liker_id)
            if not liker_info:
                return
            
            liker_username = liker_info.get('username', '')
            liker_name = f"{liker_info.get('first_name', '')} {liker_info.get('last_name', '')}".strip()
            
            username_text = f"@{liker_username}" if liker_username else liker_name
            
            text = (
                f"❤️ {username_text} поставил лайк на ваше объявление #{post['id']}\n\n"
                f"📄 Текст: {post['description'][:100]}{'...' if len(post['description']) > 100 else ''}"
            )
            
            await telegram_bot.send_message(
                chat_id=author_id,
                text=text
            )
        except Exception as e:
            logger.error(f"Failed to send like notification: {e}")

    @staticmethod
    async def send_account_notification(telegram_id: int, action: str, reason: str = None):
        if not telegram_bot:
            return
        
        try:
            # Проверяем настройки уведомлений пользователя
            user_info = await DatabaseService.get_user_info(telegram_id)
            if not user_info or not user_info.get('notification_settings', {}).get('account', True):
                return
            
            messages = {
                'ban': f"🚫 Ваш аккаунт заблокирован{f'. Причина: {reason}' if reason else ''}",
                'hard_ban': f"🚫 Ваш аккаунт заблокирован, все объявления удалены{f'. Причина: {reason}' if reason else ''}",
                'unban': "✅ Ваш аккаунт разблокирован",
                'limit_change': f"📊 {reason if reason else 'Лимит объявлений изменен'}",
                'post_deleted': f"🗑 Ваше объявление удалено модератором{f'. Причина: {reason}' if reason else ''}"
            }
            
            if action in messages:
                await telegram_bot.send_message(
                    chat_id=telegram_id,
                    text=messages[action]
                )
        except Exception as e:
            logger.error(f"Failed to send account notification: {e}")

    @staticmethod
    async def send_system_notification(telegram_id: int, message: str):
        if not telegram_bot:
            return
        
        try:
            # Проверяем настройки уведомлений пользователя
            user_info = await DatabaseService.get_user_info(telegram_id)
            if not user_info or not user_info.get('notification_settings', {}).get('system', True):
                return
            
            await telegram_bot.send_message(
                chat_id=telegram_id,
                text=f"📢 {message}"
            )
        except Exception as e:
            logger.error(f"Failed to send system notification: {e}")

    @staticmethod
    async def send_new_post_notifications(post: Dict):
        if not telegram_bot:
            return
        
        try:
            async with get_db_connection() as conn:
                # Получаем пользователей с настроенными уведомлениями о новых постах
                users = await conn.fetch("""
                    SELECT telegram_id, notification_settings 
                    FROM users 
                    WHERE notification_settings->>'new_posts' = 'true'
                """)
                
                for user_row in users:
                    user = dict(user_row)
                    settings = user['notification_settings']
                    
                    # Проверяем категории
                    categories = settings.get('new_posts_categories', [])
                    if categories and post['category'] not in categories:
                        continue
                    
                    # Проверяем теги
                    tags = settings.get('new_posts_tags', [])
                    if tags:
                        post_tags = json.loads(post.get('tags', '[]'))
                        if not any(tag in post_tags for tag in tags):
                            continue
                    
                    # Отправляем уведомление
                    creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
                    text = (
                        f"🆕 Новое объявление в категории '{post['category']}'\n\n"
                        f"📄 {post['description'][:150]}{'...' if len(post['description']) > 150 else ''}\n\n"
                        f"👤 От: {creator.get('first_name', '')} {creator.get('last_name', '')}"
                    )
                    
                    await telegram_bot.send_message(
                        chat_id=user['telegram_id'],
                        text=text
                    )
        except Exception as e:
            logger.error(f"Failed to send new post notifications: {e}")

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
            
            # Команды доступны только в группе модерации
            self.app.add_handler(CommandHandler("start", self.start_command, filters=lambda update: update.effective_chat.id == config.MODERATION_CHAT_ID))
            self.app.add_handler(CommandHandler("delete", self.delete_command, filters=lambda update: update.effective_chat.id == config.MODERATION_CHAT_ID))
            self.app.add_handler(CommandHandler("ban", self.ban_command, filters=lambda update: update.effective_chat.id == config.MODERATION_CHAT_ID))
            self.app.add_handler(CommandHandler("hardban", self.hardban_command, filters=lambda update: update.effective_chat.id == config.MODERATION_CHAT_ID))
            self.app.add_handler(CommandHandler("unban", self.unban_command, filters=lambda update: update.effective_chat.id == config.MODERATION_CHAT_ID))
            self.app.add_handler(CommandHandler("setlimit", self.setlimit_command, filters=lambda update: update.effective_chat.id == config.MODERATION_CHAT_ID))
            self.app.add_handler(CommandHandler("getlimit", self.getlimit_command, filters=lambda update: update.effective_chat.id == config.MODERATION_CHAT_ID))
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
                
                try:
                    creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
                    await NotificationService.send_account_notification(
                        creator['telegram_id'], 'post_deleted', 'Нарушение правил'
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
            await update.message.reply_text("Использование: /ban <telegram_id>")
            return
        
        try:
            telegram_id = int(context.args[0])
            await DatabaseService.ban_user(telegram_id, "Забанен модератором")
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
            deleted_post_ids = await DatabaseService.hard_ban_user(telegram_id, "Хард-бан модератором")
            
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
            creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
            
            if action == "delete":
                success = await DatabaseService.delete_post(post_id)
                if success:
                    await broadcast_message({
                        'type': 'post_deleted',
                        'post_id': post_id
                    })
                    
                    await NotificationService.send_account_notification(
                        creator['telegram_id'], 'post_deleted', 'Нарушение правил'
                    )
                    
                    new_text = query.message.text + f"\n\n🗑 Объявление удалено"
                    await query.edit_message_text(new_text)
                    
            elif action == "ban":
                await DatabaseService.ban_user(creator['telegram_id'], "Забанен модератором")
                new_text = query.message.text + f"\n\n🚫 Пользователь забанен"
                await query.edit_message_text(new_text)
                
            elif action == "hardban":
                deleted_post_ids = await DatabaseService.hard_ban_user(creator['telegram_id'], "Хард-бан модератором")
                for deleted_id in deleted_post_ids:
                    await broadcast_message({
                        'type': 'post_deleted',
                        'post_id': deleted_id
                    })
                new_text = query.message.text + f"\n\n🚫 Пользователь забанен, все посты удалены"
                await query.edit_message_text(new_text)
                
        except Exception as e:
            logger.error(f"Moderation callback error: {e}")
            await query.edit_message_text("❌ Произошла ошибка")

    async def send_for_moderation(self, post: Dict):
        if not config.MODERATION_CHAT_ID:
            return
        
        try:
            creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
            
            text = (
                f"📝 Новое объявление #{post['id']}\n\n"
                f"👤 От: {creator['first_name']} {creator.get('last_name', '')}\n"
                f"🆔 ID: {creator['telegram_id']}\n"
                f"👤 Username: @{creator.get('username', 'нет')}\n"
                f"📂 Категория: {post['category']}\n\n"
                f"📄 Текст:\n{post['description']}\n\n"
                f"🏷 Теги: {', '.join(json.loads(post['tags']) if post['tags'] else [])}"
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{post['id']}"),
                    InlineKeyboardButton("🚫 Бан", callback_data=f"ban_{post['id']}"),
                    InlineKeyboardButton("💥 Хард-бан", callback_data=f"hardban_{post['id']}")
                ]
            ])
            
            await telegram_bot.send_message(
                chat_id=config.MODERATION_CHAT_ID,
                text=text,
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Failed to send moderation message: {e}")

    async def send_report_for_moderation(self, post: Dict, reporter_data: Dict, reason: str = None):
        if not config.MODERATION_CHAT_ID:
            return
        
        try:
            creator = json.loads(post['creator']) if isinstance(post['creator'], str) else post['creator']
            
            text = (
                f"🚨 ЖАЛОБА НА ОБЪЯВЛЕНИЕ #{post['id']}\n\n"
                f"👤 Автор объявления: {creator['first_name']} {creator.get('last_name', '')}\n"
                f"🆔 ID автора: {creator['telegram_id']}\n"
                f"👤 Username автора: @{creator.get('username', 'нет')}\n\n"
                f"🚨 Жалобу подал: {reporter_data['first_name']} {reporter_data.get('last_name', '')}\n"
                f"🆔 ID жалобщика: {reporter_data['telegram_id']}\n"
                f"👤 Username жалобщика: @{reporter_data.get('username', 'нет')}\n\n"
                f"📂 Категория: {post['category']}\n"
                f"📄 Текст объявления:\n{post['description']}\n\n"
                f"🏷 Теги: {', '.join(json.loads(post['tags']) if post['tags'] else [])}\n\n"
                f"💬 Причина жалобы: {reason or 'Не указана'}"
            )
            
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🗑 Удалить объявление", callback_data=f"delete_{post['id']}"),
                    InlineKeyboardButton("🚫 Бан автора", callback_data=f"ban_{post['id']}"),
                    InlineKeyboardButton("💥 Хард-бан", callback_data=f"hardban_{post['id']}")
                ]
            ])
            
            # Отправляем в отдельную тему для жалоб
            await telegram_bot.send_message(
                chat_id=config.MODERATION_CHAT_ID,
                message_thread_id=config.REPORTS_THREAD_ID,
                text=text,
                reply_markup=keyboard
            )
            
        except Exception as e:
            logger.error(f"Failed to send report message: {e}")

async def broadcast_message(message: Dict, filter_data: Dict = None):
    global connected_clients
    if connected_clients:
        message_str = json.dumps(message, default=str)
        disconnected_clients = set()
        
        for ws in connected_clients.copy():
            try:
                await ws.send_str(message_str)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                disconnected_clients.add(ws)
        
        connected_clients -= disconnected_clients

async def handle_websocket_message(ws, data: Dict):
    action = data.get('type')
    telegram_id = data.get('telegram_id')
    
    if not telegram_id:
        await ws.send_str(json.dumps({
            'type': 'error',
            'message': 'telegram_id is required'
        }, default=str))
        return
    
    is_banned = await DatabaseService.is_user_banned(telegram_id)
    banned_actions = ['create_post', 'like_post', 'report_post']
    
    if is_banned and action in banned_actions:
        await ws.send_str(json.dumps({
            'type': 'action_banned',
            'message': 'Это действие недоступно для заблокированных пользователей'
        }, default=str))
        return
    
    try:
        if action == 'sync_user':
            user_data = {
                'telegram_id': telegram_id,
                'username': data.get('username') or '',
                'first_name': data.get('first_name') or '',
                'last_name': data.get('last_name') or '',
                'photo_url': data.get('photo_url') or '',
                'language': data.get('language', 'ru')
            }
            
            user_data_result = await DatabaseService.sync_user(user_data)
            published_count = await DatabaseService.get_user_published_posts_count(telegram_id)
            await ws.send_str(json.dumps({
                'type': 'user_synced',
                'telegram_id': user_data_result['telegram_id'],
                'limits': {
                    'used': published_count,
                    'total': user_data_result.get('post_limit', config.DAILY_POST_LIMIT)
                },
                'is_banned': user_data_result.get('is_banned', False),
                'language': user_data_result.get('language', 'ru'),
                'favorites': user_data_result.get('favorites', []),
                'hidden': user_data_result.get('hidden', []),
                'liked': user_data_result.get('liked', []),
                'notification_settings': user_data_result.get('notification_settings', {})
            }, default=str))
        
        elif action == 'create_post':
            if not await PostLimitService.check_user_limit(telegram_id):
                await ws.send_str(json.dumps({
                    'type': 'limit_exceeded',
                    'message': f'Достигнут лимит объявлений'
                }, default=str))
                return
            
            post = await DatabaseService.create_post({
                'telegram_id': telegram_id,
                'description': data['description'],
                'category': data['category'],
                'tags': data['tags'],
                'creator': data['creator_data'],
                'is_edit': data.get('is_edit', False),
                'original_post_id': data.get('original_post_id')
            })
            
            # Отправляем в модерацию
            if telegram_bot:
                moderation_bot = ModerationBot()
                await moderation_bot.send_for_moderation(post)
            
            # Обновляем лимиты
            published_count = await DatabaseService.get_user_published_posts_count(telegram_id)
            limit = await DatabaseService.get_user_limit(telegram_id)
            
            await ws.send_str(json.dumps({
                'type': 'post_created',
                'message': 'Объявление опубликовано'
            }, default=str))
            
            # Отправляем обновление лимитов
            await ws.send_str(json.dumps({
                'type': 'user_limits_updated',
                'telegram_id': telegram_id,
                'limits': {
                    'used': published_count,
                    'total': limit
                }
            }, default=str))
            
            # Отправляем пост в ленту
            await broadcast_message({'type': 'post_approved', 'post': post})
        
        elif action == 'get_posts':
            posts = await DatabaseService.get_posts(
                data, data['page'], data['limit'], data.get('search', ''), telegram_id
            )
            await ws.send_str(json.dumps({
                'type': 'posts',
                'posts': posts,
                'append': data.get('append', False)
            }, default=str))
        
        elif action == 'like_post':
            post = await DatabaseService.like_post(data['post_id'], telegram_id)
            if post:
                await broadcast_message({'type': 'post_updated', 'post': post, 'action_user_id': telegram_id})
        
        elif action == 'delete_post':
            success = await DatabaseService.delete_post(data['post_id'], telegram_id)
            if success:
                published_count = await DatabaseService.get_user_published_posts_count(telegram_id)
                limit = await DatabaseService.get_user_limit(telegram_id)
                
                await broadcast_message({'type': 'post_deleted', 'post_id': data['post_id']})
                
                await ws.send_str(json.dumps({
                    'type': 'user_limits_updated',
                    'telegram_id': telegram_id,
                    'limits': {
                        'used': published_count,
                        'total': limit
                    }
                }, default=str))
        
        elif action == 'get_post_for_edit':
            post = await DatabaseService.get_post_by_id(data['post_id'])
            if post and post['telegram_id'] == telegram_id:
                await ws.send_str(json.dumps({
                    'type': 'post_for_edit',
                    'post': post
                }, default=str))
            else:
                await ws.send_str(json.dumps({
                    'type': 'error',
                    'message': 'Пост не найден или не принадлежит вам'
                }, default=str))
        
        elif action == 'report_post':
            post = await DatabaseService.get_post_by_id(data['post_id'])
            if post:
                result = await DatabaseService.report_post(
                    data['post_id'], 
                    telegram_id, 
                    data.get('reason')
                )
                
                if result['success']:
                    if result['message'] == 'already_reported':
                        await ws.send_str(json.dumps({
                            'type': 'error',
                            'message': 'Вы уже отправляли жалобу на это объявление'
                        }, default=str))
                    else:
                        if telegram_bot:
                            moderation_bot = ModerationBot()
                            reporter_data = {
                                'telegram_id': telegram_id,
                                'first_name': data.get('reporter_first_name', ''),
                                'last_name': data.get('reporter_last_name', ''),
                                'username': data.get('reporter_username', '')
                            }
                            await moderation_bot.send_report_for_moderation(post, reporter_data, data.get('reason'))
                        
                        await ws.send_str(json.dumps({
                            'type': 'report_sent',
                            'message': 'Жалоба отправлена модераторам'
                        }, default=str))
        
        elif action == 'get_user_limits':
            published_count = await DatabaseService.get_user_published_posts_count(telegram_id)
            limit = await DatabaseService.get_user_limit(telegram_id)
            
            await ws.send_str(json.dumps({
                'type': 'user_limits_updated',
                'telegram_id': telegram_id,
                'limits': {
                    'used': published_count,
                    'total': limit
                }
            }, default=str))
        
        elif action == 'add_to_favorites':
            result = await DatabaseService.add_to_favorites(data['post_id'], telegram_id)
            await ws.send_str(json.dumps({
                'type': 'favorites_updated',
                'action': result['action'],
                'message': result['message']
            }, default=str))
            if result['success']:
                await asyncio.sleep(0.1)
                posts = await DatabaseService.get_posts(
                    {'filters': {'sort': 'new'}}, 1, 20, '', telegram_id
                )
                await ws.send_str(json.dumps({
                    'type': 'posts',
                    'posts': posts,
                    'append': False
                }, default=str))

        elif action == 'hide_post':
            result = await DatabaseService.hide_post(data['post_id'], telegram_id)
            await ws.send_str(json.dumps({
                'type': 'hide_updated',
                'action': result['action'],
                'message': result['message']
            }, default=str))
            if result['success']:
                await asyncio.sleep(0.1)
                posts = await DatabaseService.get_posts(
                    {'filters': {'sort': 'new'}}, 1, 20, '', telegram_id
                )
                await ws.send_str(json.dumps({
                    'type': 'posts',
                    'posts': posts,
                    'append': False
                }, default=str))
        
        elif action == 'update_notification_settings':
            settings = data.get('settings', {})
            await DatabaseService.update_notification_settings(telegram_id, settings)
            await ws.send_str(json.dumps({
                'type': 'notification_settings_updated',
                'message': 'Настройки уведомлений обновлены'
            }, default=str))
            
    except Exception as e:
        logger.error(f"Error handling websocket message: {e}")
        await ws.send_str(json.dumps({
            'type': 'error',
            'message': 'Внутренняя ошибка сервера'
        }, default=str))

async def create_app():
    app = web.Application()
    
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*"
        )
    })
    
    async def websocket_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        connected_clients.add(ws)
        
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await handle_websocket_message(ws, data)
                    except json.JSONDecodeError:
                        await ws.send_str(json.dumps({'type': 'error', 'message': 'Invalid JSON'}))
                    except Exception as e:
                        logger.error(f"WebSocket message error: {e}")
                        await ws.send_str(json.dumps({'type': 'error', 'message': str(e)}))
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f'WebSocket error: {ws.exception()}')
        except Exception as e:
            logger.error(f"WebSocket handler error: {e}")
        finally:
            connected_clients.discard(ws)
        
        return ws
    
    async def health_handler(request):
        return web.json_response({
            'status': 'ok',
            'clients': len(connected_clients),
            'bot_active': telegram_bot is not None
        })
    
    async def info_handler(request):
        return web.json_response({
            'app': 'Telegram Web App Server',
            'version': '2.0',
            'port': config.PORT,
            'clients_connected': len(connected_clients),
            'endpoints': {
                'websocket': '/ws',
                'health': '/health',
                'info': '/info'
            }
        })
    
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/info', info_handler)
    app.router.add_get('/', info_handler)
    
    for route in list(app.router.routes()):
        cors.add(route)
    
    return app

async def main():
    await DatabaseService.init_database()
    
    moderation_bot = ModerationBot()
    await moderation_bot.init_bot()
    
    app = await create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, '0.0.0.0', config.PORT)
    await site.start()
    
    if moderation_bot.app:
        try:
            await moderation_bot.app.updater.start_polling(
                drop_pending_updates=True,
                error_callback=lambda exc: logger.error(f"Bot polling error: {exc}")
            )
        except Exception as e:
            logger.error(f"Bot polling failed: {e}")
    
    try:
        await asyncio.Future()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if moderation_bot.app:
                await moderation_bot.app.stop()
        except:
            pass
        await runner.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
