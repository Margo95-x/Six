#!/usr/bin/env python3
"""
main.py - Entry point for Render deployment
Точка входа для деплоя backend на Render
"""
import sys
import os
import logging

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    """Главная функция запуска приложения"""
    try:
        # Проверяем наличие необходимых переменных окружения
        required_env_vars = ['DATABASE_URL']
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        
        if missing_vars:
            logger.error(f"Missing required environment variables: {missing_vars}")
            raise ValueError(f"Missing environment variables: {missing_vars}")
        
        # Логируем информацию о запуске
        logger.info("Starting Telegram Web App Server")
        logger.info(f"Python version: {sys.version}")
        logger.info(f"PORT: {os.getenv('PORT', 'not set')}")
        logger.info(f"DATABASE_URL: {'set' if os.getenv('DATABASE_URL') else 'not set'}")
        logger.info(f"BOT_TOKEN: {'set' if os.getenv('BOT_TOKEN') else 'not set'}")
        
        # Импортируем и запускаем основной сервер
        from server import main as server_main
        import asyncio
        
        # Запускаем сервер
        asyncio.run(server_main())
        
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application error: {e}")
        raise

if __name__ == '__main__':  # ✅ ИСПРАВЛЕНО: двойные подчеркивания
    main()
