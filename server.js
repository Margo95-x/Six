const TelegramBot = require('node-telegram-bot-api');

// Конфигурация
const BOT_TOKEN = process.env.BOT_TOKEN;
const RUSSIAN_GROUP_URL = process.env.RUSSIAN_GROUP_URL || 'https://t.me/russian_group';
const ENGLISH_GROUP_URL = process.env.ENGLISH_GROUP_URL || 'https://t.me/english_group';

console.log('=== ПРОСТОЙ БОТ ===');
console.log('BOT_TOKEN:', BOT_TOKEN ? 'установлен' : 'НЕ УСТАНОВЛЕН');

if (!BOT_TOKEN) {
    console.error('❌ BOT_TOKEN не установлен!');
    process.exit(1);
}

// Инициализация бота (только polling, без webhook)
const bot = new TelegramBot(BOT_TOKEN, { polling: true });

console.log('✅ Бот инициализирован');

// Команда старт - единственная функция
bot.onText(/\/start/, async (msg) => {
    const chatId = msg.chat.id;
    
    console.log(`[START] Пользователь ${msg.from.first_name} (${msg.from.id}) запустил бота`);
    
    const keyboard = {
        inline_keyboard: [[
            { text: 'Русский', url: RUSSIAN_GROUP_URL },
            { text: 'English', url: ENGLISH_GROUP_URL }
        ]]
    };
    
    try {
        await bot.sendMessage(chatId, 'Выберите язык:', { 
            reply_markup: keyboard 
        });
        console.log(`[SUCCESS] Кнопки отправлены пользователю ${msg.from.id}`);
    } catch (error) {
        console.error(`[ERROR] Ошибка отправки кнопок:`, error.message);
    }
});

// Игнорируем все остальные сообщения
bot.on('message', (msg) => {
    if (!msg.text?.startsWith('/start')) {
        // Просто игнорируем, не отвечаем
        return;
    }
});

// Обработка ошибок
bot.on('error', (error) => {
    console.error('[BOT ERROR]', error.message);
});

bot.on('polling_error', (error) => {
    console.error('[POLLING ERROR]', error.message);
    
    if (error.message.includes('401')) {
        console.error('❌ НЕПРАВИЛЬНЫЙ ТОКЕН БОТА! Проверьте BOT_TOKEN');
    }
    if (error.message.includes('409')) {
        console.error('❌ БОТ УЖЕ ЗАПУЩЕН В ДРУГОМ МЕСТЕ!');
    }
});

// Простейший HTTP сервер для Render
const http = require('http');
const PORT = process.env.PORT || 3000;

const server = http.createServer((req, res) => {
    if (req.url === '/') {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(`
            <h1>Простой Telegram Бот</h1>
            <p>✅ Бот работает</p>
            <p>🤖 Токен: ${BOT_TOKEN ? 'установлен' : 'НЕ УСТАНОВЛЕН'}</p>
            <p>🇷🇺 Русская группа: ${RUSSIAN_GROUP_URL}</p>
            <p>🇬🇧 Английская группа: ${ENGLISH_GROUP_URL}</p>
            <p>📝 Функция: При /start показывает 2 кнопки</p>
            <p>🚀 Статус: ${BOT_TOKEN ? 'Готов к работе' : 'Ошибка токена'}</p>
        `);
    } else {
        res.writeHead(404);
        res.end('Not Found');
    }
});

server.listen(PORT, () => {
    console.log(`🚀 HTTP сервер запущен на порту ${PORT}`);
    console.log(`🌐 Откройте: http://localhost:${PORT}`);
    console.log('=== БОТ ГОТОВ ===');
});

// Graceful shutdown
process.on('SIGINT', () => {
    console.log('\n💤 Остановка бота...');
    bot.stopPolling();
    server.close(() => {
        console.log('✅ Бот остановлен');
        process.exit(0);
    });
});
