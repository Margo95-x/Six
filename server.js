const TelegramBot = require('node-telegram-bot-api');
const express = require('express');

// Конфигурация
const BOT_TOKEN = process.env.BOT_TOKEN;
const RUSSIAN_GROUP_URL = process.env.RUSSIAN_GROUP_URL;
const ENGLISH_GROUP_URL = process.env.ENGLISH_GROUP_URL;
const PORT = process.env.PORT || 3000;
const WEBHOOK_URL = process.env.WEBHOOK_URL;

console.log('=== ОСНОВНОЙ БОТ ===');
console.log('BOT_TOKEN:', BOT_TOKEN ? 'установлен' : 'НЕ УСТАНОВЛЕН');
console.log('RUSSIAN_GROUP_URL:', RUSSIAN_GROUP_URL ? 'установлен' : 'НЕ УСТАНОВЛЕН');
console.log('ENGLISH_GROUP_URL:', ENGLISH_GROUP_URL ? 'установлен' : 'НЕ УСТАНОВЛЕН');

// Инициализация
const bot = new TelegramBot(BOT_TOKEN);
const app = express();

app.use(express.json());

// Настройка webhook или polling
if (WEBHOOK_URL) {
    bot.setWebHook(`${WEBHOOK_URL}/bot${BOT_TOKEN}`);
    app.post(`/bot${BOT_TOKEN}`, (req, res) => {
        bot.processUpdate(req.body);
        res.sendStatus(200);
    });
    console.log('✅ Webhook режим');
} else {
    bot.startPolling();
    console.log('✅ Polling режим');
}

// Единственная команда - старт
bot.onText(/\/start/, async (msg) => {
    const chatId = msg.chat.id;
    
    console.log(`[START] Пользователь ${msg.from.first_name} запустил бота`);
    
    const keyboard = {
        inline_keyboard: [[
            { text: 'Русский', url: RUSSIAN_GROUP_URL },
            { text: 'English', url: ENGLISH_GROUP_URL }
        ]]
    };
    
    await bot.sendMessage(chatId, 'Выберите язык:', { 
        reply_markup: keyboard 
    });
});

// Игнорируем все остальные сообщения - бот только показывает кнопки
bot.on('message', (msg) => {
    // Игнорируем все сообщения кроме /start
    if (!msg.text?.startsWith('/start')) {
        return;
    }
});

// Минимальная обработка ошибок
bot.on('error', (error) => {
    console.error('[BOT ERROR]', error);
});

bot.on('polling_error', (error) => {
    console.error('[POLLING ERROR]', error);
});

// Веб-статус
app.get('/', (req, res) => {
    res.send(`
        <h1>Main Bot Status</h1>
        <p>✅ Основной бот работает</p>
        <p>🤖 Bot Token: ${BOT_TOKEN ? 'установлен' : 'НЕ УСТАНОВЛЕН'}</p>
        <p>🇷🇺 Russian Group: ${RUSSIAN_GROUP_URL ? 'установлен' : 'НЕ УСТАНОВЛЕН'}</p>
        <p>🇬🇧 English Group: ${ENGLISH_GROUP_URL ? 'установлен' : 'НЕ УСТАНОВЛЕН'}</p>
        <p>🌐 Webhook: ${WEBHOOK_URL ? 'установлен' : 'polling режим'}</p>
        
        <h2>Функционал:</h2>
        <ul>
            <li>Команда /start показывает 2 кнопки</li>
            <li>Кнопки ведут в группы по ссылкам</li>
            <li>Никаких других команд нет</li>
        </ul>
    `);
});

// Запуск
app.listen(PORT, () => {
    console.log(`🚀 Основной бот запущен на порту ${PORT}`);
});
