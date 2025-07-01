#!/usr/bin/env python3
"""
server.py - Telegram Web App Server для Render
Backend сервер с HTTP + WebSocket на одном порту
"""

import asyncio
import logging
import os
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import asyncpg
from collections import defaultdict
from contextlib import asynccontextmanager
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from aiohttp import web, WSMsgType
import aiohttp_cors
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
user_cache = {}

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
        
        # Преобразуем postgres:// в postgresql:// для asyncpg
        database_url = config.DATABASE_URL
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)
        
        # SSL настройки для продакшена
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
            # Создаем таблицы если не существуют
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
            
            # Создаем индексы
            try:
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_telegram_id ON posts(telegram_id)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at DESC)")
            except Exception as e:
                logger.error(f"Error creating indexes: {e}")
        
        logger.info("Database initialized")

    @staticmethod
    async def sync_user(user_data: Dict) -> Dict:
        async with get_db_connection() as conn:
            # Сброс счетчика постов если нужно
            await conn.execute("""
                UPDATE users 
                SET posts_today = 0, last_post_count_reset = CURRENT_DATE
                WHERE telegram_id = $1 AND last_post_count_reset < CURRENT_DATE
            """, user_data['telegram_id'])
            
            user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_data['telegram_id'])
            
            if not user:
                await conn.execute("""
                    INSERT INTO users (telegram_id, username, first_name, last_name, photo_url, language)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """, user_data['telegram_id'], user_data['username'], user_data['first_name'],
                    user_data['last_name'], user_data['photo_url'], user_data.get('language', 'ru'))
                user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_data['telegram_id'])
            else:
                # Обновляем информацию
                await conn.execute("""
                    UPDATE users SET username = $2, first_name = $3, last_name = $4, photo_url = $5
                    WHERE telegram_id = $1
                """, user_data['telegram_id'], user_data['username'], user_data['first_name'],
                    user_data['last_name'], user_data['photo_url'])
            
            # Кешируем пользователя
            user_dict = dict(user)
            user_cache[user_data['telegram_id']] = user_dict
            return user_dict

# Telegram Bot
class ModerationBot:
    def __init__(self):
        self.app = None

    async def init_bot(self):
        if not config.BOT_TOKEN:
            logger.warning("BOT_TOKEN not set, bot will not be available")
            return
        
        try:
            self.app = Application.builder().token(config.BOT_TOKEN).build()
            
            # Команды
            self.app.add_handler(CommandHandler("start", self.start_command))
            
            await self.app.initialize()
            await self.app.start()
            
            # Очищаем webhook если установлен
            try:
                await self.app.bot.delete_webhook(drop_pending_updates=True)
                logger.info("Webhook cleared")
            except Exception as e:
                logger.warning(f"Could not clear webhook: {e}")
            
            global telegram_bot
            telegram_bot = self.app.bot
            
            logger.info("Moderation bot initialized")
            
        except Exception as e:
            logger.error(f"Failed to initialize bot: {e}")

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🤖 Telegram Web App Server is running!")

# WebSocket обработка
async def broadcast_message(message: Dict):
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
            await ws.send_str(json.dumps({
                'type': 'user_synced',
                'telegram_id': user_data_result['telegram_id'],
                'limits': {
                    'used': 0,
                    'total': user_data_result.get('post_limit', config.DAILY_POST_LIMIT)
                },
                'is_banned': user_data_result.get('is_banned', False),
                'language': user_data_result.get('language', 'ru')
            }, default=str))
            
        elif action == 'ping':
            await ws.send_str(json.dumps({
                'type': 'pong',
                'timestamp': datetime.now().isoformat()
            }, default=str))
            
        else:
            await ws.send_str(json.dumps({
                'type': 'error',
                'message': f'Unknown action: {action}'
            }, default=str))
            
    except Exception as e:
        logger.error(f"Error handling websocket message: {e}")
        await ws.send_str(json.dumps({
            'type': 'error',
            'message': 'Internal server error'
        }, default=str))

# HTTP + WebSocket Server
async def create_app():
    app = web.Application()
    
    # Настройка CORS для GitHub Pages
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*"
        )
    })
    
    # WebSocket endpoint
    async def websocket_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        connected_clients.add(ws)
        logger.info(f"WebSocket client connected. Total: {len(connected_clients)}")
        
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
            logger.info(f"WebSocket client disconnected. Total: {len(connected_clients)}")
        
        return ws
    
    # HTTP endpoints
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
    
    # Добавляем маршруты
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/info', info_handler)
    app.router.add_get('/', info_handler)
    
    # Добавляем CORS ко всем маршрутам
    for route in list(app.router.routes()):
        cors.add(route)
    
    return app

# Основная функция
async def main():
    # Инициализация базы данных
    await DatabaseService.init_database()
    
    # Инициализация бота
    moderation_bot = ModerationBot()
    await moderation_bot.init_bot()
    
    # Создание HTTP + WebSocket приложения
    app = await create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Запуск сервера на одном порту
    site = web.TCPSite(runner, '0.0.0.0', config.PORT)
    await site.start()
    logger.info(f"Server started on port {config.PORT}")
    logger.info(f"WebSocket endpoint: /ws")
    logger.info(f"Health check: /health")
    
    # Запуск бота с обработкой ошибок
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
        logger.info("Shutting down...")
    finally:
        try:
            if moderation_bot.app:
                await moderation_bot.app.stop()
        except:
            pass
        await runner.cleanup()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application error: {e}")
        raise
