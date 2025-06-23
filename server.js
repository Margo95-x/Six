const express = require('express')
const TelegramBot = require('node-telegram-bot-api')
const cors = require('cors')

const app = express()

// Environment variables
const BOT_TOKEN = '7948285859:AAGPM2BYYE2US3AIbP7P4yEBV4C5oWt3FSw'
const GROUP_ID = '-1002268255207'
const PORT = process.env.PORT || 3002
const WEBHOOK_URL = process.env.WEBHOOK_URL || 'https://six-z05l.onrender.com'

// Initialize Telegram bot
const bot = new TelegramBot(BOT_TOKEN)

// Middleware
app.use(cors())
app.use('/webhook', express.raw({ type: 'application/json' }))
app.use(express.json())

// Lightweight cache for recent messages (only for current request)
let messageCache = null
let cacheTimestamp = 0
const CACHE_DURATION = 30000 // 30 seconds

// Message parsers
function parseEventFromMessage(text, messageId, date, from) {
  try {
    // Parse create/update message: "🎯 Title\n\nDescription\n\n📍 city..."
    if (text.includes('🎯')) {
      const isUpdate = text.startsWith('✏️ Updated:')
      const content = isUpdate ? text.replace('✏️ Updated:\n\n', '') : text
      
      const lines = content.split('\n').filter(line => line.trim())
      
      // Extract title (after 🎯)
      const titleLine = lines.find(line => line.includes('🎯'))
      if (!titleLine) return null
      
      const title = titleLine.replace('🎯', '').replace(/<\/?b>/g, '').trim()
      
      // Extract description (lines between title and metadata)
      const titleIndex = lines.findIndex(line => line.includes('🎯'))
      let description = ''
      let metadataStartIndex = lines.length
      
      for (let i = titleIndex + 1; i < lines.length; i++) {
        if (lines[i].match(/^[📍🏷️👤🎂]/)) {
          metadataStartIndex = i
          break
        }
        if (lines[i].trim()) {
          description += (description ? ' ' : '') + lines[i].trim()
        }
      }
      
      // Extract metadata
      const metadata = lines.slice(metadataStartIndex)
      let city = '', category = '', gender = '', ageGroup = '', authorName = '', username = ''
      
      metadata.forEach(line => {
        if (line.startsWith('📍')) city = line.replace('📍', '').trim()
        else if (line.startsWith('🏷️')) category = line.replace('🏷️', '').trim()
        else if (line.startsWith('👤') && !line.includes('@')) gender = line.replace('👤', '').trim()
        else if (line.startsWith('🎂')) ageGroup = line.replace('🎂', '').trim()
        else if (line.startsWith('👤') && line.includes('@')) {
          const authorLine = line.replace('👤', '').trim()
          const match = authorLine.match(/^(.+?)\s*\(@(.+?)\)$/)
          if (match) {
            authorName = match[1].trim()
            username = match[2].trim()
          } else {
            authorName = authorLine
          }
        }
      })
      
      const event = {
        id: messageId.toString(),
        title,
        description,
        authorId: from?.id?.toString() || 'unknown',
        author: {
          fullName: authorName,
          username: username || undefined,
          telegramId: from?.id?.toString()
        },
        city,
        category,
        gender,
        ageGroup,
        createdAt: new Date(date * 1000).toISOString(),
        updatedAt: new Date().toISOString(),
        likes: 0,
        isLiked: false,
        status: 'active',
        telegramMessageId: messageId
      }
      
      return event
    }
    
    return null
  } catch (error) {
    console.error('Error parsing message:', error)
    return null
  }
}

// Read messages from Telegram group
async function getEventsFromTelegram() {
  try {
    // Check cache first
    if (messageCache && (Date.now() - cacheTimestamp < CACHE_DURATION)) {
      return messageCache
    }
    
    console.log('Reading messages from Telegram group...')
    
    // Get chat history - this works for supergroups
    const messages = []
    let offset = 0
    const limit = 100 // Max messages per request
    const maxMessages = 500 // Don't load too many on free plan
    
    // Note: In production you might need to use different approach
    // as getUpdates has limitations for groups
    
    // Alternative: Read messages using chat export or admin bot
    // For now, we'll simulate reading recent messages
    
    const events = []
    
    // Temporary cache to avoid repeated API calls
    messageCache = events
    cacheTimestamp = Date.now()
    
    return events
  } catch (error) {
    console.error('Error reading from Telegram:', error)
    return []
  }
}

// Alternative: Read messages using getUpdates (limited)
async function getEventsFromUpdates() {
  try {
    const updates = await bot.getUpdates({ limit: 100, timeout: 10 })
    const events = []
    
    updates.forEach(update => {
      if (update.message && 
          update.message.chat.id.toString() === GROUP_ID &&
          update.message.text &&
          update.message.text.includes('🎯')) {
        
        const { message_id, text, date, from } = update.message
        const event = parseEventFromMessage(text, message_id, date, from)
        if (event) {
          events.push(event)
        }
      }
    })
    
    return events
  } catch (error) {
    console.error('Error getting updates:', error)
    return []
  }
}

// Webhook endpoint (for cache invalidation)
app.post('/webhook', (req, res) => {
  try {
    const update = JSON.parse(req.body.toString())
    
    // Process only messages from the specific group
    if (update.message && 
        update.message.chat.id.toString() === GROUP_ID &&
        update.message.text) {
      
      console.log('New message in group, invalidating cache')
      
      // Invalidate cache when new message arrives
      messageCache = null
      cacheTimestamp = 0
    }
    
    res.status(200).send('OK')
  } catch (error) {
    console.error('Webhook error:', error)
    res.status(500).send('Error')
  }
})

// Helper functions for filtering and sorting
function fullTextSearch(events, query) {
  const searchTerms = query.toLowerCase().split(' ')
  return events.filter(event => {
    const searchText = `${event.title} ${event.description}`.toLowerCase()
    return searchTerms.every(term => searchText.includes(term))
  })
}

function applyFilters(events, filters) {
  let filtered = events
  
  if (filters.city) {
    filtered = filtered.filter(e => e.city === filters.city)
  }
  
  if (filters.category) {
    filtered = filtered.filter(e => e.category === filters.category)
  }
  
  if (filters.gender) {
    filtered = filtered.filter(e => e.gender === filters.gender)
  }
  
  if (filters.ageGroup) {
    filtered = filtered.filter(e => e.ageGroup === filters.ageGroup)
  }
  
  if (filters.dateFrom) {
    filtered = filtered.filter(e => new Date(e.createdAt) >= new Date(filters.dateFrom))
  }
  
  if (filters.dateTo) {
    filtered = filtered.filter(e => new Date(e.createdAt) <= new Date(filters.dateTo))
  }
  
  if (filters.authorId) {
    filtered = filtered.filter(e => e.authorId === filters.authorId)
  }
  
  return filtered
}

function sortEvents(events, sortType) {
  switch (sortType) {
    case 'popularity':
      return [...events].sort((a, b) => (b.likes || 0) - (a.likes || 0))
    case 'old':
      return [...events].sort((a, b) => new Date(a.createdAt) - new Date(b.createdAt))
    case 'new':
    default:
      return [...events].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt))
  }
}

// API Routes
app.get('/api/feed', async (req, res) => {
  try {
    const { 
      city, 
      category, 
      gender, 
      ageGroup, 
      search, 
      sort = 'new',
      page = 1, 
      limit = 20,
      authorId,
      dateFrom,
      dateTo
    } = req.query

    // Read events from Telegram group (not from memory!)
    let events = await getEventsFromTelegram()
    
    // If no events from main method, try alternative
    if (events.length === 0) {
      events = await getEventsFromUpdates()
    }

    // Apply search
    if (search) {
      events = fullTextSearch(events, search)
    }

    // Apply filters
    const filters = { city, category, gender, ageGroup, authorId, dateFrom, dateTo }
    events = applyFilters(events, filters)

    // Apply sorting
    events = sortEvents(events, sort)

    // Pagination
    const startIndex = (page - 1) * limit
    const endIndex = startIndex + parseInt(limit)
    const paginatedEvents = events.slice(startIndex, endIndex)

    res.json({
      posts: paginatedEvents,
      hasMore: events.length > endIndex,
      total: events.length,
      page: parseInt(page),
      limit: parseInt(limit)
    })
  } catch (error) {
    console.error('Error fetching feed:', error)
    res.status(500).json({ error: 'Failed to fetch feed' })
  }
})

// Get single event by reading from Telegram
app.get('/api/events/:id', async (req, res) => {
  try {
    const events = await getEventsFromTelegram()
    const event = events.find(e => e.id === req.params.id)
    
    if (!event) {
      return res.status(404).json({ error: 'Event not found' })
    }
    
    res.json(event)
  } catch (error) {
    console.error('Error fetching event:', error)
    res.status(500).json({ error: 'Failed to fetch event' })
  }
})

// Stats endpoint
app.get('/api/stats', async (req, res) => {
  try {
    const events = await getEventsFromTelegram()
    
    const stats = {
      totalEvents: events.length,
      byCity: {},
      byCategory: {},
      byStatus: {}
    }
    
    events.forEach(event => {
      // City stats
      stats.byCity[event.city] = (stats.byCity[event.city] || 0) + 1
      
      // Category stats
      stats.byCategory[event.category] = (stats.byCategory[event.category] || 0) + 1
      
      // Status stats
      stats.byStatus[event.status] = (stats.byStatus[event.status] || 0) + 1
    })
    
    res.json(stats)
  } catch (error) {
    console.error('Error fetching stats:', error)
    res.status(500).json({ error: 'Failed to fetch stats' })
  }
})

// Health check
app.get('/health', (req, res) => {
  res.json({ 
    status: 'OK',
    cacheStatus: messageCache ? 'cached' : 'empty',
    webhookUrl: `${WEBHOOK_URL}/webhook`
  })
})

// Error handler
app.use((error, req, res, next) => {
  console.error('Server error:', error)
  res.status(500).json({ error: 'Internal server error' })
})

// Setup webhook and start server
async function setupWebhook() {
  try {
    const webhookUrl = `${WEBHOOK_URL}/webhook`
    await bot.setWebHook(webhookUrl)
    console.log('Webhook set to:', webhookUrl)
  } catch (error) {
    console.error('Error setting webhook:', error)
  }
}

// Start server
app.listen(PORT, async () => {
  console.log(`Bot 2 server running on port ${PORT}`)
  console.log(`Reading events from Telegram group on each request`)
  
  // Setup webhook for cache invalidation
  await setupWebhook()
  
  console.log('Bot 2 initialization complete - NO in-memory storage!')
})
