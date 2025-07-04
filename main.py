import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Set, Dict, Optional, List

import asyncpg
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# Функция для сериализации datetime в JSON
def serialize_datetime(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

# Настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MODERATION_CHAT_ID = int(os.getenv("MODERATION_CHAT_ID"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-domain.com/webhook")

# Инициализация
app = FastAPI()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket соединения
active_connections: Set[WebSocket] = set()

# Модели данных
class UserSync(BaseModel):
    telegram_id: int
    username: str
    full_name: str
    first_name: str

class PostCreate(BaseModel):
    telegram_id: int
    description: str
    category: str
    city: str
    gender: str
    age: str
    date: str

class PostUpdate(BaseModel):
    post_id: int
    telegram_id: int
    description: str
    category: str
    city: str
    gender: str
    age: str
    date: str

class UserAction(BaseModel):
    telegram_id: int
    post_id: int
    action: str  # like, favorite, hide, report, delete

class NotificationSettings(BaseModel):
    telegram_id: int
    likes: bool
    system: bool
    filters: dict

# База данных
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    
    # Таблица пользователей
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            posts INTEGER[] DEFAULT '{}',
            hidden INTEGER[] DEFAULT '{}',
            favorites INTEGER[] DEFAULT '{}',
            likes INTEGER[] DEFAULT '{}',
            reports INTEGER[] DEFAULT '{}',
            post_limit INTEGER DEFAULT 10,
            status TEXT DEFAULT 'live',
            subscriptions JSONB DEFAULT '{}',
            notifications_likes BOOLEAN DEFAULT true,
            notifications_system BOOLEAN DEFAULT true,
            notifications_filters JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    
    # Добавляем новые колонки для существующих пользователей
    try:
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications_likes BOOLEAN DEFAULT true')
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications_system BOOLEAN DEFAULT true') 
        await conn.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications_filters JSONB DEFAULT \'{}\'')
    except:
        pass
    
    # Таблица постов
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT,
            description TEXT,
            category TEXT,
            city TEXT,
            gender TEXT,
            age TEXT,
            date_tag TEXT,
            likes_count INTEGER DEFAULT 0,
            reports_count INTEGER DEFAULT 0,
            username TEXT,
            full_name TEXT,
            avatar_url TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    
    await conn.close()

# WebSocket менеджер
async def broadcast_message(message: dict):
    """Отправка сообщения всем подключенным клиентам"""
    if active_connections:
        disconnected = set()
        for connection in active_connections:
            try:
                await connection.send_json(message)
            except:
                disconnected.add(connection)
        
        # Удаляем отключенные соединения
        active_connections.difference_update(disconnected)

# API endpoints
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    
    try:
        while True:
            data = await websocket.receive_json()
            
            if data["type"] == "sync":
                user_data = UserSync(**data["data"])
                user_info = await sync_user(user_data)
                await websocket.send_json({
                    "type": "user_synced",
                    "data": user_info
                })
                
            elif data["type"] == "create_post":
                post_data = PostCreate(**data["data"])
                new_post = await create_post(post_data)
                await broadcast_message({
                    "type": "new_post",
                    "data": new_post
                })
                
            elif data["type"] == "update_post":
                post_data = PostUpdate(**data["data"])
                updated_post = await update_post(post_data)
                await broadcast_message({
                    "type": "post_updated",
                    "data": updated_post
                })
                
            elif data["type"] == "user_action":
                action_data = UserAction(**data["data"])
                result = await handle_user_action(action_data)
                
                # Если это удаление собственного поста
                if action_data.action == "delete":
                    await broadcast_message({
                        "type": "post_deleted",
                        "data": {"post_id": action_data.post_id}
                    })
                    # Обновляем информацию о пользователе
                    user_info = await get_user_info(action_data.telegram_id)
                    await websocket.send_json({
                        "type": "user_updated",
                        "data": user_info
                    })
                else:
                    await broadcast_message({
                        "type": "post_action",
                        "data": result
                    })
                    
            elif data["type"] == "update_notifications":
                notif_data = NotificationSettings(**data["data"])
                await update_notification_settings(notif_data)
                await websocket.send_json({
                    "type": "notifications_updated",
                    "data": {"status": "success"}
                })
                
    except WebSocketDisconnect:
        active_connections.discard(websocket)

# Функции работы с БД
async def get_user_info(telegram_id: int) -> dict:
    """Получение актуальной информации о пользователе"""
    conn = await asyncpg.connect(DATABASE_URL)
    user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
    await conn.close()
    
    if user:
        user_info = dict(user)
        # Конвертируем datetime в строки
        if 'created_at' in user_info and user_info['created_at']:
            user_info['created_at'] = user_info['created_at'].isoformat()
        if 'updated_at' in user_info and user_info['updated_at']:
            user_info['updated_at'] = user_info['updated_at'].isoformat()
        return user_info
    return {}

async def update_notification_settings(notif_data: NotificationSettings):
    """Обновление настроек уведомлений"""
    conn = await asyncpg.connect(DATABASE_URL)
    
    await conn.execute(
        """UPDATE users SET 
           notifications_likes = $1, 
           notifications_system = $2, 
           notifications_filters = $3
           WHERE telegram_id = $4""",
        notif_data.likes, notif_data.system, 
        json.dumps(notif_data.filters), notif_data.telegram_id
    )
    
    await conn.close()

async def sync_user(user_data: UserSync) -> dict:
    """Синхронизация пользователя с БД"""
    conn = await asyncpg.connect(DATABASE_URL)
    
    # Проверяем существование пользователя
    user = await conn.fetchrow(
        "SELECT * FROM users WHERE telegram_id = $1", 
        user_data.telegram_id
    )
    
    if user:
        # Обновляем данные если изменились
        await conn.execute(
            """UPDATE users SET username = $1, full_name = $2, updated_at = NOW() 
               WHERE telegram_id = $3""",
            user_data.username, user_data.full_name, user_data.telegram_id
        )
        user_info = dict(user)
        # Конвертируем datetime в строки
        if 'created_at' in user_info and user_info['created_at']:
            user_info['created_at'] = user_info['created_at'].isoformat()
        if 'updated_at' in user_info and user_info['updated_at']:
            user_info['updated_at'] = user_info['updated_at'].isoformat()
    else:
        # Создаем нового пользователя
        await conn.execute(
            """INSERT INTO users (telegram_id, username, full_name) 
               VALUES ($1, $2, $3)""",
            user_data.telegram_id, user_data.username, user_data.full_name
        )
        user_info = {
            "telegram_id": user_data.telegram_id,
            "username": user_data.username,
            "full_name": user_data.full_name,
            "posts": [],
            "hidden": [],
            "favorites": [],
            "likes": [],
            "reports": [],
            "post_limit": 10,
            "status": "live",
            "subscriptions": {},
            "notifications_likes": True,
            "notifications_system": True,
            "notifications_filters": {}
        }
    
    await conn.close()
    return user_info
    """Синхронизация пользователя с БД"""
    conn = await asyncpg.connect(DATABASE_URL)
    
    # Проверяем существование пользователя
    user = await conn.fetchrow(
        "SELECT * FROM users WHERE telegram_id = $1", 
        user_data.telegram_id
    )
    
    if user:
        # Обновляем данные если изменились
        await conn.execute(
            """UPDATE users SET username = $1, full_name = $2, updated_at = NOW() 
               WHERE telegram_id = $3""",
            user_data.username, user_data.full_name, user_data.telegram_id
        )
        user_info = dict(user)
        # Конвертируем datetime в строки
        if 'created_at' in user_info and user_info['created_at']:
            user_info['created_at'] = user_info['created_at'].isoformat()
        if 'updated_at' in user_info and user_info['updated_at']:
            user_info['updated_at'] = user_info['updated_at'].isoformat()
    else:
        # Создаем нового пользователя
        await conn.execute(
            """INSERT INTO users (telegram_id, username, full_name) 
               VALUES ($1, $2, $3)""",
            user_data.telegram_id, user_data.username, user_data.full_name
        )
        user_info = {
            "telegram_id": user_data.telegram_id,
            "username": user_data.username,
            "full_name": user_data.full_name,
            "posts": [],
            "hidden": [],
            "favorites": [],
            "likes": [],
            "reports": [],
            "post_limit": 10,
            "status": "live",
            "subscriptions": {}
        }
    
    await conn.close()
    return user_info

async def create_post(post_data: PostCreate) -> dict:
    """Создание нового поста"""
    conn = await asyncpg.connect(DATABASE_URL)
    
    # Проверяем лимиты и статус пользователя
    user = await conn.fetchrow(
        "SELECT post_limit, status, posts, username, full_name FROM users WHERE telegram_id = $1",
        post_data.telegram_id
    )
    
    if not user or user["status"] == "banned":
        await conn.close()
        raise HTTPException(status_code=403, detail="User banned or not found")
    
    if len(user["posts"]) >= user["post_limit"]:
        await conn.close()
        raise HTTPException(status_code=403, detail="Post limit exceeded")
    
    # Создаем пост
    post_id = await conn.fetchval(
        """INSERT INTO posts (telegram_id, description, category, city, gender, age, date_tag, username, full_name, avatar_url)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
        post_data.telegram_id, post_data.description, post_data.category,
        post_data.city, post_data.gender, post_data.age, post_data.date,
        user["username"], user["full_name"], 
        f"https://api.telegram.org/file/bot/photos/user_{post_data.telegram_id}.jpg"
    )
    
    # Обновляем список постов пользователя
    new_posts = user["posts"] + [post_id]
    await conn.execute(
        "UPDATE users SET posts = $1 WHERE telegram_id = $2",
        new_posts, post_data.telegram_id
    )
    
    # Получаем созданный пост
    post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
    await conn.close()
    
    # Конвертируем datetime в строки
    post_dict = dict(post)
    if 'created_at' in post_dict and post_dict['created_at']:
        post_dict['created_at'] = post_dict['created_at'].isoformat()
    if 'updated_at' in post_dict and post_dict['updated_at']:
        post_dict['updated_at'] = post_dict['updated_at'].isoformat()
    
    # Отправляем в модерацию
    await send_to_moderation(post_dict, "new")
    
    # Отправляем уведомления подписчикам
    await send_notifications_to_subscribers(post_dict)
    
    return post_dict

async def update_post(post_data: PostUpdate) -> dict:
    """Обновление поста"""
    conn = await asyncpg.connect(DATABASE_URL)
    
    # Проверяем права
    post = await conn.fetchrow(
        "SELECT * FROM posts WHERE id = $1 AND telegram_id = $2",
        post_data.post_id, post_data.telegram_id
    )
    
    if not post:
        await conn.close()
        raise HTTPException(status_code=404, detail="Post not found")
    
    # Проверяем статус пользователя
    user = await conn.fetchrow(
        "SELECT status FROM users WHERE telegram_id = $1",
        post_data.telegram_id
    )
    
    if user["status"] == "banned":
        await conn.close()
        raise HTTPException(status_code=403, detail="User banned")
    
    # Обновляем пост
    await conn.execute(
        """UPDATE posts SET description = $1, category = $2, city = $3, 
           gender = $4, age = $5, date_tag = $6, updated_at = NOW()
           WHERE id = $7""",
        post_data.description, post_data.category, post_data.city,
        post_data.gender, post_data.age, post_data.date, post_data.post_id
    )
    
    # Получаем обновленный пост
    updated_post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_data.post_id)
    await conn.close()
    
    # Конвертируем datetime в строки
    post_dict = dict(updated_post)
    if 'created_at' in post_dict and post_dict['created_at']:
        post_dict['created_at'] = post_dict['created_at'].isoformat()
    if 'updated_at' in post_dict and post_dict['updated_at']:
        post_dict['updated_at'] = post_dict['updated_at'].isoformat()
    
    # Отправляем в модерацию
    await send_to_moderation(post_dict, "updated")
    
    return post_dict

async def handle_user_action(action_data: UserAction) -> dict:
    """Обработка действий пользователя"""
    conn = await asyncpg.connect(DATABASE_URL)
    
    user = await conn.fetchrow(
        "SELECT * FROM users WHERE telegram_id = $1",
        action_data.telegram_id
    )
    
    if user["status"] == "banned" and action_data.action in ["like", "report", "delete"]:
        await conn.close()
        raise HTTPException(status_code=403, detail="User banned")
    
    # Обработка удаления собственного поста
    if action_data.action == "delete":
        # Проверяем, что пост принадлежит пользователю
        post = await conn.fetchrow(
            "SELECT * FROM posts WHERE id = $1 AND telegram_id = $2",
            action_data.post_id, action_data.telegram_id
        )
        
        if not post:
            await conn.close()
            raise HTTPException(status_code=404, detail="Post not found or access denied")
        
        # Удаляем пост
        await conn.execute("DELETE FROM posts WHERE id = $1", action_data.post_id)
        
        # Удаляем из списков пользователей
        await conn.execute(
            "UPDATE users SET posts = array_remove(posts, $1), "
            "favorites = array_remove(favorites, $1), "
            "likes = array_remove(likes, $1), "
            "reports = array_remove(reports, $1), "
            "hidden = array_remove(hidden, $1)",
            action_data.post_id
        )
        
        await conn.close()
        return {"post_id": action_data.post_id, "action": "deleted"}
    
    # Обновляем пользователя для других действий
    if action_data.action == "like":
        if action_data.post_id in user["likes"]:
            new_likes = [x for x in user["likes"] if x != action_data.post_id]
            likes_change = -1
        else:
            new_likes = user["likes"] + [action_data.post_id]
            likes_change = 1
            
        await conn.execute(
            "UPDATE users SET likes = $1 WHERE telegram_id = $2",
            new_likes, action_data.telegram_id
        )
        
        # Обновляем счетчик лайков поста
        await conn.execute(
            "UPDATE posts SET likes_count = likes_count + $1 WHERE id = $2",
            likes_change, action_data.post_id
        )
        
        # Уведомление автору поста
        if likes_change > 0:
            post = await conn.fetchrow("SELECT telegram_id FROM posts WHERE id = $1", action_data.post_id)
            if post and post["telegram_id"] != action_data.telegram_id:
                # Проверяем настройки уведомлений автора
                author = await conn.fetchrow(
                    "SELECT notifications_likes FROM users WHERE telegram_id = $1",
                    post["telegram_id"]
                )
                if author and author["notifications_likes"]:
                    await send_like_notification(post["telegram_id"], action_data.post_id, user["username"])
        
    elif action_data.action == "favorite":
        if action_data.post_id in user["favorites"]:
            new_favorites = [x for x in user["favorites"] if x != action_data.post_id]
        else:
            new_favorites = user["favorites"] + [action_data.post_id]
            
        await conn.execute(
            "UPDATE users SET favorites = $1 WHERE telegram_id = $2",
            new_favorites, action_data.telegram_id
        )
        
    elif action_data.action == "hide":
        if action_data.post_id not in user["hidden"]:
            new_hidden = user["hidden"] + [action_data.post_id]
            await conn.execute(
                "UPDATE users SET hidden = $1 WHERE telegram_id = $2",
                new_hidden, action_data.telegram_id
            )
            
    elif action_data.action == "report":
        if action_data.post_id not in user["reports"]:
            new_reports = user["reports"] + [action_data.post_id]
            await conn.execute(
                "UPDATE users SET reports = $1 WHERE telegram_id = $2",
                new_reports, action_data.telegram_id
            )
            
            # Увеличиваем счетчик жалоб поста
            await conn.execute(
                "UPDATE posts SET reports_count = reports_count + 1 WHERE id = $1",
                action_data.post_id
            )
            
            # Отправляем в модерацию
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", action_data.post_id)
            post_dict = dict(post)
            if 'created_at' in post_dict and post_dict['created_at']:
                post_dict['created_at'] = post_dict['created_at'].isoformat()
            if 'updated_at' in post_dict and post_dict['updated_at']:
                post_dict['updated_at'] = post_dict['updated_at'].isoformat()
            await send_report_to_moderation(post_dict)
    
    # Получаем обновленный пост
    post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", action_data.post_id)
    await conn.close()
    
    # Конвертируем datetime в строки
    post_dict = dict(post)
    if 'created_at' in post_dict and post_dict['created_at']:
        post_dict['created_at'] = post_dict['created_at'].isoformat()
    if 'updated_at' in post_dict and post_dict['updated_at']:
        post_dict['updated_at'] = post_dict['updated_at'].isoformat()
    
    return post_dict

# Telegram бот функции
async def send_to_moderation(post: dict, action_type: str):
    """Отправка поста в модерацию"""
    text = f"🆕 Новое объявление" if action_type == "new" else f"✏️ Обновлено объявление"
    text += f"\n\nID: {post['id']}\nАвтор: {post['full_name']} (@{post['username']})\n"
    text += f"Telegram ID: {post['telegram_id']}\n"
    text += f"Описание: {post['description']}\nКатегория: {post['category']}\n"
    text += f"Теги: {post['city']}, {post['gender']}, {post['age']}, {post['date_tag']}"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{post['id']}"),
            InlineKeyboardButton(text="🚫 Бан", callback_data=f"ban_{post['telegram_id']}"),
            InlineKeyboardButton(text="💀 Хард бан", callback_data=f"hardban_{post['telegram_id']}")
        ]
    ])
    
    await bot.send_message(MODERATION_CHAT_ID, text, reply_markup=keyboard)

async def send_report_to_moderation(post: dict):
    """Отправка жалобы в модерацию"""
    text = f"⚠️ Жалоба на объявление\n\nID: {post['id']}\nАвтор: {post['full_name']} (@{post['username']})\n"
    text += f"Telegram ID: {post['telegram_id']}\n"
    text += f"Описание: {post['description']}\nЖалоб: {post['reports_count']}"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{post['id']}"),
            InlineKeyboardButton(text="🚫 Бан", callback_data=f"ban_{post['telegram_id']}"),
            InlineKeyboardButton(text="💀 Хард бан", callback_data=f"hardban_{post['telegram_id']}")
        ]
    ])
    
    await bot.send_message(MODERATION_CHAT_ID, text, reply_markup=keyboard)

async def send_notifications_to_subscribers(post: dict):
    """Отправка уведомлений о новом посте подписчикам"""
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        
        # Получаем всех пользователей с настройками уведомлений
        subscribers = await conn.fetch(
            "SELECT telegram_id, notifications_filters FROM users WHERE notifications_filters IS NOT NULL"
        )
        
        await conn.close()
        
        for subscriber in subscribers:
            try:
                # Парсим фильтры подписки
                if subscriber["notifications_filters"]:
                    filters = json.loads(subscriber["notifications_filters"]) if isinstance(subscriber["notifications_filters"], str) else subscriber["notifications_filters"]
                    
                    # Проверяем соответствие фильтрам
                    match = True
                    if filters.get("category") and filters["category"] != "Все" and filters["category"] != post["category"]:
                        match = False
                    if filters.get("city") and filters["city"] != "Все" and filters["city"] != post["city"]:
                        match = False
                    if filters.get("gender") and filters["gender"] != "Все" and filters["gender"] != post["gender"]:
                        match = False
                    if filters.get("age") and filters["age"] != "Все" and filters["age"] != post["age"]:
                        match = False
                    if filters.get("date") and filters["date"] != "Все" and filters["date"] != post["date_tag"]:
                        match = False
                    
                    # Отправляем уведомление если есть совпадение и это не автор поста
                    if match and subscriber["telegram_id"] != post["telegram_id"]:
                        text = f"🆕 Новое объявление!\n\n{post['description'][:100]}{'...' if len(post['description']) > 100 else ''}\n\nОт: {post['full_name']}"
                        await bot.send_message(subscriber["telegram_id"], text)
            except Exception as e:
                print(f"Ошибка отправки уведомления пользователю {subscriber['telegram_id']}: {e}")
                continue
                
    except Exception as e:
        print(f"Ошибка при отправке уведомлений подписчикам: {e}")

async def send_like_notification(telegram_id: int, post_id: int, liker_username: str):
    """Уведомление о лайке"""
    text = f"👍 Вам поставили лайк на объявление #{post_id}\nОт: @{liker_username}"
    try:
        await bot.send_message(telegram_id, text)
    except:
        pass

# Telegram команды модерации
@dp.message(lambda message: message.chat.id == MODERATION_CHAT_ID and message.text.startswith('/'))
async def handle_moderation_commands(message: types.Message):
    """Обработка команд модерации"""
    try:
        command_parts = message.text.split()
        command = command_parts[0]
        
        if command == "/delete" and len(command_parts) > 1:
            post_id = int(command_parts[1])
            await delete_post(post_id, message)
            
        elif command == "/ban" and len(command_parts) > 1:
            telegram_id = int(command_parts[1])
            await ban_user(telegram_id, message)
            
        elif command == "/hardban" and len(command_parts) > 1:
            telegram_id = int(command_parts[1])
            await hardban_user(telegram_id, message)
            
        elif command == "/unban" and len(command_parts) > 1:
            telegram_id = int(command_parts[1])
            await unban_user(telegram_id, message)
            
        elif command == "/setlimit" and len(command_parts) > 2:
            telegram_id = int(command_parts[1])
            limit = int(command_parts[2])
            await set_user_limit(telegram_id, limit, message)
            
        elif command == "/getlimit" and len(command_parts) > 1:
            telegram_id = int(command_parts[1])
            await get_user_limit(telegram_id, message)
            
        else:
            await message.answer("Доступные команды:\n/delete <post_id> - Удалить объявление\n/ban <telegram_id> - Забанить пользователя\n/hardban <telegram_id> - Забанить + удалить все посты\n/unban <telegram_id> - Разбанить пользователя\n/setlimit <telegram_id> <limit> - Установить лимит постов\n/getlimit <telegram_id> - Посмотреть лимит пользователя")
            
    except (ValueError, IndexError) as e:
        await message.answer("Неверный формат команды")
    except Exception as e:
        print(f"Ошибка обработки команды: {e}")
        await message.answer("Произошла ошибка при выполнении команды")

# Обработка кнопок модерации
@dp.callback_query()
async def handle_moderation_buttons(callback: types.CallbackQuery):
    """Обработка кнопок модерации"""
    try:
        if callback.message.chat.id != MODERATION_CHAT_ID:
            await callback.answer("Доступ запрещен")
            return
            
        action, value = callback.data.split("_", 1)
        
        if action == "delete":
            await delete_post(int(value), callback.message)
        elif action == "ban":
            await ban_user(int(value), callback.message)
        elif action == "hardban":
            await hardban_user(int(value), callback.message)
            
        await callback.answer()
    except Exception as e:
        print(f"Ошибка обработки callback: {e}")
        await callback.answer("Произошла ошибка")

# Функции модерации
async def delete_post(post_id: int, message):
    """Удаление поста"""
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        
        # Получаем пост
        post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
        if not post:
            await message.answer("Пост не найден")
            await conn.close()
            return
        
        # Получаем автора поста для проверки настроек уведомлений
        author = await conn.fetchrow(
            "SELECT notifications_system FROM users WHERE telegram_id = $1",
            post["telegram_id"]
        )
        
        # Удаляем пост
        await conn.execute("DELETE FROM posts WHERE id = $1", post_id)
        
        # Удаляем из списков пользователей и обновляем счетчик постов автора
        await conn.execute(
            "UPDATE users SET posts = array_remove(posts, $1), "
            "favorites = array_remove(favorites, $1), "
            "likes = array_remove(likes, $1), "
            "reports = array_remove(reports, $1), "
            "hidden = array_remove(hidden, $1)",
            post_id
        )
        
        # Получаем обновленную информацию об авторе
        updated_author = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", post["telegram_id"])
        
        await conn.close()
        
        # Уведомляем автора (проверяем настройки)
        if author and author.get("notifications_system", True):
            try:
                await bot.send_message(post["telegram_id"], "❌ Ваше объявление удалено из-за нарушения")
            except:
                pass
        
        # Обновляем фронт - удаляем пост
        await broadcast_message({
            "type": "post_deleted",
            "data": {"post_id": post_id}
        })
        
        # Обновляем информацию об авторе на фронте
        if updated_author:
            user_info = dict(updated_author)
            if 'created_at' in user_info and user_info['created_at']:
                user_info['created_at'] = user_info['created_at'].isoformat()
            if 'updated_at' in user_info and user_info['updated_at']:
                user_info['updated_at'] = user_info['updated_at'].isoformat()
            
            await broadcast_message({
                "type": "user_status_updated",
                "data": {"telegram_id": post["telegram_id"], "user_info": user_info}
            })
        
        await message.answer(f"✅ Пост {post_id} удален")
        
    except Exception as e:
        print(f"Ошибка удаления поста: {e}")
        await message.answer(f"❌ Ошибка при удалении поста: {str(e)}")

async def ban_user(telegram_id: int, message):
    """Бан пользователя"""
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        
        result = await conn.execute(
            "UPDATE users SET status = 'banned' WHERE telegram_id = $1",
            telegram_id
        )
        
        # Получаем обновленную информацию о пользователе
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
        await conn.close()
        
        if result == "UPDATE 0":
            await message.answer(f"❌ Пользователь {telegram_id} не найден")
            return
        
        # Отправляем обновление статуса на фронт
        if user:
            user_info = dict(user)
            if 'created_at' in user_info and user_info['created_at']:
                user_info['created_at'] = user_info['created_at'].isoformat()
            if 'updated_at' in user_info and user_info['updated_at']:
                user_info['updated_at'] = user_info['updated_at'].isoformat()
            
            await broadcast_message({
                "type": "user_status_updated",
                "data": {"telegram_id": telegram_id, "user_info": user_info}
            })
        
        # Уведомляем пользователя (проверяем настройки)
        if user and user.get("notifications_system", True):
            try:
                await bot.send_message(telegram_id, "🚫 Ваш аккаунт заблокирован")
            except:
                pass
        
        await message.answer(f"✅ Пользователь {telegram_id} забанен")
        
    except Exception as e:
        print(f"Ошибка бана пользователя: {e}")
        await message.answer(f"❌ Ошибка при бане пользователя: {str(e)}")

async def hardban_user(telegram_id: int, message):
    """Хард бан пользователя"""
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        
        # Получаем автора для проверки настроек уведомлений
        user = await conn.fetchrow(
            "SELECT notifications_system FROM users WHERE telegram_id = $1",
            telegram_id
        )
        
        # Получаем посты пользователя
        user_posts = await conn.fetch(
            "SELECT id FROM posts WHERE telegram_id = $1",
            telegram_id
        )
        
        # Удаляем все посты
        await conn.execute("DELETE FROM posts WHERE telegram_id = $1", telegram_id)
        
        # Банием пользователя
        await conn.execute(
            "UPDATE users SET status = 'banned', posts = '{}' WHERE telegram_id = $1",
            telegram_id
        )
        
        # Удаляем посты из списков других пользователей
        for post in user_posts:
            await conn.execute(
                "UPDATE users SET "
                "favorites = array_remove(favorites, $1), "
                "likes = array_remove(likes, $1), "
                "reports = array_remove(reports, $1), "
                "hidden = array_remove(hidden, $1)",
                post["id"]
            )
        
        # Получаем обновленную информацию о пользователе
        updated_user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
        
        await conn.close()
        
        # Уведомляем пользователя (проверяем настройки)
        if user and user.get("notifications_system", True):
            try:
                await bot.send_message(telegram_id, "💀 Ваш аккаунт заблокирован и все объявления удалены")
            except:
                pass
        
        # Обновляем фронт - удаляем посты
        for post in user_posts:
            await broadcast_message({
                "type": "post_deleted",
                "data": {"post_id": post["id"]}
            })
        
        # Обновляем информацию о пользователе на фронте
        if updated_user:
            user_info = dict(updated_user)
            if 'created_at' in user_info and user_info['created_at']:
                user_info['created_at'] = user_info['created_at'].isoformat()
            if 'updated_at' in user_info and user_info['updated_at']:
                user_info['updated_at'] = user_info['updated_at'].isoformat()
            
            await broadcast_message({
                "type": "user_status_updated",
                "data": {"telegram_id": telegram_id, "user_info": user_info}
            })
        
        await message.answer(f"✅ Пользователь {telegram_id} получил хард бан")
        
    except Exception as e:
        print(f"Ошибка хард бана: {e}")
        await message.answer(f"❌ Ошибка при хард бане: {str(e)}")

async def unban_user(telegram_id: int, message):
    """Разбан пользователя"""
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        
        result = await conn.execute(
            "UPDATE users SET status = 'live' WHERE telegram_id = $1",
            telegram_id
        )
        
        # Получаем обновленную информацию о пользователе
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
        await conn.close()
        
        if result == "UPDATE 0":
            await message.answer(f"❌ Пользователь {telegram_id} не найден")
            return
        
        # Отправляем обновление статуса на фронт
        if user:
            user_info = dict(user)
            if 'created_at' in user_info and user_info['created_at']:
                user_info['created_at'] = user_info['created_at'].isoformat()
            if 'updated_at' in user_info and user_info['updated_at']:
                user_info['updated_at'] = user_info['updated_at'].isoformat()
            
            await broadcast_message({
                "type": "user_status_updated",
                "data": {"telegram_id": telegram_id, "user_info": user_info}
            })
        
        # Уведомляем пользователя
        if user and user.get("notifications_system", True):
            try:
                await bot.send_message(telegram_id, "✅ Вы разблокированы")
            except:
                pass
        
        await message.answer(f"✅ Пользователь {telegram_id} разбанен")
        
    except Exception as e:
        print(f"Ошибка разбана: {e}")
        await message.answer(f"❌ Ошибка при разбане: {str(e)}")

async def set_user_limit(telegram_id: int, limit: int, message):
    """Установка лимита постов"""
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        
        result = await conn.execute(
            "UPDATE users SET post_limit = $1 WHERE telegram_id = $2",
            limit, telegram_id
        )
        
        # Получаем обновленную информацию о пользователе
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
        await conn.close()
        
        if result == "UPDATE 0":
            await message.answer(f"❌ Пользователь {telegram_id} не найден")
            return
        
        # Отправляем обновление лимита на фронт
        if user:
            user_info = dict(user)
            if 'created_at' in user_info and user_info['created_at']:
                user_info['created_at'] = user_info['created_at'].isoformat()
            if 'updated_at' in user_info and user_info['updated_at']:
                user_info['updated_at'] = user_info['updated_at'].isoformat()
            
            await broadcast_message({
                "type": "user_status_updated", 
                "data": {"telegram_id": telegram_id, "user_info": user_info}
            })
        
        # Уведомляем пользователя
        if user and user.get("notifications_system", True):
            try:
                await bot.send_message(telegram_id, f"📊 Новый лимит объявлений: {limit}")
            except:
                pass
        
        await message.answer(f"✅ Лимит для {telegram_id} установлен: {limit}")
        
    except Exception as e:
        print(f"Ошибка установки лимита: {e}")
        await message.answer(f"❌ Ошибка при установке лимита: {str(e)}")

async def get_user_limit(telegram_id: int, message):
    """Получение лимита пользователя"""
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        
        user = await conn.fetchrow(
            "SELECT post_limit, posts FROM users WHERE telegram_id = $1",
            telegram_id
        )
        
        await conn.close()
        
        if user:
            current_posts = len(user["posts"])
            await message.answer(f"📊 Пользователь {telegram_id}:\nЛимит: {user['post_limit']}\nИспользовано: {current_posts}")
        else:
            await message.answer("Пользователь не найден")
            
    except Exception as e:
        print(f"Ошибка получения лимита: {e}")
        await message.answer(f"❌ Ошибка при получении лимита: {str(e)}")

# API для получения всех постов
@app.get("/api/posts")
async def get_all_posts():
    """Получение всех постов"""
    conn = await asyncpg.connect(DATABASE_URL)
    posts = await conn.fetch("SELECT * FROM posts ORDER BY created_at DESC")
    await conn.close()
    
    # Конвертируем datetime в строки
    posts_list = []
    for post in posts:
        post_dict = dict(post)
        if 'created_at' in post_dict and post_dict['created_at']:
            post_dict['created_at'] = post_dict['created_at'].isoformat()
        if 'updated_at' in post_dict and post_dict['updated_at']:
            post_dict['updated_at'] = post_dict['updated_at'].isoformat()
        posts_list.append(post_dict)
    
    return posts_list

# Webhook для Telegram
@app.post("/webhook")
async def webhook(update: dict):
    """Обработка webhook от Telegram"""
    telegram_update = types.Update(**update)
    await dp.feed_webhook_update(bot, telegram_update)
    return {"ok": True}

# Запуск сервера
async def on_startup():
    """Инициализация при запуске"""
    await init_db()
    await bot.set_webhook(WEBHOOK_URL)
    print("🚀 Сервер запущен")

async def on_shutdown():
    """Очистка при завершении"""
    await bot.delete_webhook()
    await bot.session.close()

app.add_event_handler("startup", on_startup)
app.add_event_handler("shutdown", on_shutdown)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
