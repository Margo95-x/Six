import asyncio
import logging
import os
from datetime import datetime
from typing import Optional, List, Dict
import asyncpg
import websockets
from websockets.server import WebSocketServerProtocol
import json
from collections import defaultdict
from contextlib import asynccontextmanager

# Configuration
class Config:
    DATABASE_URL = os.getenv("POSTS_DATABASE_URL")
    PORT = int(os.getenv("PORT", "10001"))
    RATE_LIMIT_WINDOW = 60
    RATE_LIMIT_MAX_REQUESTS = 10
    DB_MIN_SIZE = 2
    DB_MAX_SIZE = 8
    DB_COMMAND_TIMEOUT = 30

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database pool
db_pool = None

# Rate limiting
rate_limiter = defaultdict(list)

# Database Service
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
        if not Config.DATABASE_URL:
            raise ValueError("POSTS_DATABASE_URL not set")
        
        db_pool = await asyncpg.create_pool(
            Config.DATABASE_URL,
            min_size=Config.DB_MIN_SIZE,
            max_size=Config.DB_MAX_SIZE,
            command_timeout=Config.DB_COMMAND_TIMEOUT
        )
        
        async with get_db_connection() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    description TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tags JSONB NOT NULL,
                    likes INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    creator JSONB NOT NULL
                )
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_user_id ON posts(user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_category ON posts(category)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_tags ON posts USING GIN(tags)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at DESC)")
        
        logger.info("Posts database initialized")

    @staticmethod
    async def create_post(post_data: Dict) -> Dict:
        async with get_db_connection() as conn:
            post_id = await conn.fetchval(
                """INSERT INTO posts (user_id, description, category, tags, creator)
                   VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                post_data['user_id'], post_data['description'], post_data['category'],
                json.dumps(post_data['tags']), json.dumps(post_data['creator'])
            )
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            return dict(post)

    @staticmethod
    async def get_posts(user_id: int, category: str, tags: List[str], page: int, limit: int, search: str = '') -> List[Dict]:
        async with get_db_connection() as conn:
            query = """
                SELECT * FROM posts
                WHERE category = $1
                AND ($2::text = '' OR to_tsvector('russian', description) @@ to_tsquery('russian', $2))
            """
            params = [category, search]
            if tags:
                query += " AND tags @> $3"
                params.append(json.dumps(tags))
            
            query += f" ORDER BY created_at DESC LIMIT {limit} OFFSET {(page - 1) * limit}"
            posts = await conn.fetch(query, *params)
            return [dict(post) for post in posts]

    @staticmethod
    async def like_post(post_id: int) -> Dict:
        async with get_db_connection() as conn:
            await conn.execute("UPDATE posts SET likes = likes + 1 WHERE id = $1", post_id)
            post = await conn.fetchrow("SELECT * FROM posts WHERE id = $1", post_id)
            return dict(post)

    @staticmethod
    async def delete_post(post_id: int, user_id: int) -> bool:
        async with get_db_connection() as conn:
            result = await conn.execute("DELETE FROM posts WHERE id = $1 AND user_id = $2", post_id, user_id)
            return result == 'DELETE 1'

    @staticmethod
    async def report_post(post_id: int) -> bool:
        # Implement reporting logic (e.g., log to a moderation queue)
        return True

# WebSocket Handler
async def handle_websocket(websocket: WebSocketServerProtocol):
    async for message in websocket:
        try:
            data = json.loads(message)
            action = data.get('type')
            
            if action == 'create_post':
                post = await DatabaseService.create_post({
                    'user_id': data['user_id'],
                    'description': data['description'],
                    'category': data['category'],
                    'tags': data['tags'],
                    'creator': {
                        'user_id': data['user_id'],
                        'username': data.get('username', ''),
                        'first_name': data.get('first_name', ''),
                        'last_name': data.get('last_name', ''),
                        'photo_url': data.get('photo_url', '')
                    }
                })
                await websocket.send(json.dumps({'type': 'post_updated', 'post': post}))
            
            elif action == 'get_posts':
                posts = await DatabaseService.get_posts(
                    data['user_id'], data['category'], data['tags'], data['page'], data['limit'], data.get('search', '')
                )
                await websocket.send(json.dumps({'type': 'posts', 'posts': posts, 'append': data.get('append', False)}))
            
            elif action == 'like_post':
                post = await DatabaseService.like_post(data['post_id'])
                await websocket.send(json.dumps({'type': 'post_updated', 'post': post}))
            
            elif action == 'delete_post':
                success = await DatabaseService.delete_post(data['post_id'], data['user_id'])
                if success:
                    await websocket.send(json.dumps({'type': 'post_deleted', 'post_id': data['post_id']}))
            
            elif action == 'report_post':
                await DatabaseService.report_post(data['post_id'])
        
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            await websocket.send(json.dumps({'type': 'error', 'message': str(e)}))

# Main
async def main():
    await DatabaseService.init_database()
    server = await websockets.serve(handle_websocket, '0.0.0.0', Config.PORT)
    logger.info(f"Posts WebSocket server started on port {Config.PORT}")
    await asyncio.Future()  # Run forever

if __name__ == '__main__':
    asyncio.run(main())
