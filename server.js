#!/usr/bin/env node
/**
 * Telegram Bot - Events Platform Launcher
 * =======================================
 * 
 * Простой бот для запуска Mini App
 * Может работать на том же сервере что и Posts/Accounts
 */

const TelegramBot = require('node-telegram-bot-api')

class EventsPlatformBot {
  constructor() {
    this.token = process.env.BOT_TOKEN
    if (!this.token) {
      throw new Error('BOT_TOKEN environment variable is required')
    }

    this.miniAppUrl = process.env.MINI_APP_URL || 'https://your-mini-app.onrender.com'
    
    // Инициализация бота
    this.bot = new TelegramBot(this.token, { polling: true })
    
    this.setupCommands()
    this.setupHandlers()
    
    console.log('🤖 Events Platform Bot started!')
  }

  setupCommands() {
    // Устанавливаем команды бота
    this.bot.setMyCommands([
      { command: 'start', description: '🚀 Launch Events Platform' },
      { command: 'help', description: '❓ Show help' },
      { command: 'about', description: 'ℹ️ About Events Platform' }
    ])
  }

  setupHandlers() {
    // Команда /start
    this.bot.onText(/\/start/, (msg) => {
      this.handleStart(msg)
    })

    // Команда /help
    this.bot.onText(/\/help/, (msg) => {
      this.handleHelp(msg)
    })

    // Команда /about
    this.bot.onText(/\/about/, (msg) => {
      this.handleAbout(msg)
    })

    // Обработка ошибок
    this.bot.on('error', (error) => {
      console.error('Bot error:', error)
    })

    // Обработка polling_error
    this.bot.on('polling_error', (error) => {
      console.error('Polling error:', error)
    })

    console.log('✅ Bot handlers setup complete')
  }

  async handleStart(msg) {
    const chatId = msg.chat.id
    const user = msg.from

    console.log(`👤 User ${user.first_name} (${user.id}) started the bot`)

    const welcomeMessage = `
🎯 **Welcome to Events Platform!**

Hi ${user.first_name}! 👋

Create and discover amazing events in your city:
• 📝 Create events
• 🔍 Find interesting events
• ❤️ Like and save favorites
• 💬 Connect with people

Ready to start? Tap the button below! 👇
    `

    const keyboard = {
      inline_keyboard: [
        [
          {
            text: '🚀 Launch Events Platform',
            web_app: { url: this.miniAppUrl }
          }
        ],
        [
          {
            text: '❓ Help',
            callback_data: 'help'
          },
          {
            text: 'ℹ️ About',
            callback_data: 'about'
          }
        ]
      ]
    }

    try {
      await this.bot.sendMessage(chatId, welcomeMessage, {
        parse_mode: 'Markdown',
        reply_markup: keyboard
      })
    } catch (error) {
      console.error('Error sending start message:', error)
      
      // Fallback без Markdown
      await this.bot.sendMessage(chatId, 
        `Welcome to Events Platform!\n\nHi ${user.first_name}! Create and discover events in your city.`,
        { reply_markup: keyboard }
      )
    }
  }

  async handleHelp(msg) {
    const chatId = msg.chat.id

    const helpMessage = `
❓ **How to use Events Platform:**

**Getting Started:**
1. Tap "Launch Events Platform" button
2. Browse events in the feed
3. Use filters to find what you're looking for

**Creating Events:**
• Tap the "+" button in the app
• Fill in event details
• Share with the community!

**Interacting:**
• ❤️ Like events you're interested in
• ⭐ Save to favorites
• 💬 Contact event creators
• 👁️ Hide events you don't like

**Tips:**
• Use search to find specific events
• Filter by city and category
• Check "My Events" tab for your creations
• Check "Favorites" for saved events

Need more help? Contact @your_support_username
    `

    const keyboard = {
      inline_keyboard: [
        [
          {
            text: '🚀 Launch App',
            web_app: { url: this.miniAppUrl }
          }
        ]
      ]
    }

    try {
      await this.bot.sendMessage(chatId, helpMessage, {
        parse_mode: 'Markdown',
        reply_markup: keyboard
      })
    } catch (error) {
      console.error('Error sending help message:', error)
      await this.bot.sendMessage(chatId, helpMessage.replace(/\*\*/g, ''), {
        reply_markup: keyboard
      })
    }
  }

  async handleAbout(msg) {
    const chatId = msg.chat.id

    const aboutMessage = `
ℹ️ **About Events Platform**

Events Platform is a community-driven app for creating and discovering local events.

**Features:**
• 📝 Create any type of event
• 🔍 Smart search and filters  
• 👥 Connect with like-minded people
• 📱 Beautiful, fast interface
• 🔄 Real-time updates

**Categories:**
👥 Meetups • 🎉 Parties • ⚽ Sports
🎭 Culture • 💼 Business • 📚 Education

**Privacy:**
We only store public event information and your interaction preferences (likes, favorites). Your Telegram data stays private.

**Open Source:**
This platform is built with modern web technologies and follows best practices for performance and security.

Built with ❤️ for the community.
    `

    const keyboard = {
      inline_keyboard: [
        [
          {
            text: '🚀 Try It Now',
            web_app: { url: this.miniAppUrl }
          }
        ],
        [
          {
            text: '📱 Share App',
            switch_inline_query: 'Check out Events Platform! 🎯'
          }
        ]
      ]
    }

    try {
      await this.bot.sendMessage(chatId, aboutMessage, {
        parse_mode: 'Markdown',
        reply_markup: keyboard
      })
    } catch (error) {
      console.error('Error sending about message:', error)
      await this.bot.sendMessage(chatId, aboutMessage.replace(/\*\*/g, ''), {
        reply_markup: keyboard
      })
    }
  }

  // Callback query handler
  handleCallbackQuery() {
    this.bot.on('callback_query', async (query) => {
      const chatId = query.message.chat.id
      const data = query.data

      try {
        await this.bot.answerCallbackQuery(query.id)

        switch (data) {
          case 'help':
            await this.handleHelp({ chat: { id: chatId } })
            break
          case 'about':
            await this.handleAbout({ chat: { id: chatId } })
            break
          default:
            console.log('Unknown callback query:', data)
        }
      } catch (error) {
        console.error('Callback query error:', error)
      }
    })
  }

  // Обработка инлайн запросов (для шеринга)
  setupInlineQueries() {
    this.bot.on('inline_query', async (query) => {
      const results = [
        {
          type: 'article',
          id: '1',
          title: '🎯 Events Platform',
          description: 'Create and discover amazing events in your city!',
          input_message_content: {
            message_text: `🎯 **Events Platform** - Create and discover amazing events!\n\n🚀 Try it now: @${this.bot.options.username || 'your_bot_username'}`
          },
          reply_markup: {
            inline_keyboard: [
              [
                {
                  text: '🚀 Launch Events Platform',
                  url: `https://t.me/${this.bot.options.username || 'your_bot_username'}`
                }
              ]
            ]
          }
        }
      ]

      try {
        await this.bot.answerInlineQuery(query.id, results, {
          cache_time: 300,
          is_personal: false
        })
      } catch (error) {
        console.error('Inline query error:', error)
      }
    })
  }

  // Статистика использования
  setupStats() {
    this.stats = {
      totalUsers: new Set(),
      dailyActiveUsers: new Set(),
      commandsCount: {},
      startTime: Date.now()
    }

    // Сброс дневной статистики каждые 24 часа
    setInterval(() => {
      this.stats.dailyActiveUsers.clear()
      console.log(`📊 Daily stats reset. Total users: ${this.stats.totalUsers.size}`)
    }, 24 * 60 * 60 * 1000)

    // Обновляем статистику при каждом сообщении
    this.bot.on('message', (msg) => {
      const userId = msg.from.id
      this.stats.totalUsers.add(userId)
      this.stats.dailyActiveUsers.add(userId)

      const command = msg.text?.split(' ')[0]
      if (command) {
        this.stats.commandsCount[command] = (this.stats.commandsCount[command] || 0) + 1
      }
    })
  }

  // API для получения статистики (если нужно)
  getStats() {
    return {
      totalUsers: this.stats.totalUsers.size,
      dailyActiveUsers: this.stats.dailyActiveUsers.size,
      commandsCount: this.stats.commandsCount,
      uptime: Math.round((Date.now() - this.stats.startTime) / 1000)
    }
  }
}

// === ЗАПУСК БОТА ===

try {
  const bot = new EventsPlatformBot()
  
  // Настройка callback queries
  bot.handleCallbackQuery()
  
  // Настройка inline queries
  bot.setupInlineQueries()
  
  // Настройка статистики
  bot.setupStats()

  // Graceful shutdown
  process.on('SIGINT', () => {
    console.log('🛑 Bot shutting down...')
    bot.bot.stopPolling()
    process.exit(0)
  })

  process.on('SIGTERM', () => {
    console.log('🛑 Bot shutting down...')
    bot.bot.stopPolling()
    process.exit(0)
  })

} catch (error) {
  console.error('❌ Failed to start bot:', error)
  process.exit(1)
}
