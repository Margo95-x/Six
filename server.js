// 🌊 Distributed Nostr Network - Golden Ratio Architecture
// 6-node resilient network with tetrahedral stability

const express = require('express');
const { WebSocketServer } = require('ws');
const crypto = require('crypto');
const fs = require('fs');
const WebSocket = require('ws');

// ========================= GOLDEN RATIO CONSTANTS =========================

const GOLDEN_RATIO = 1.618;
const FIBONACCI = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55];

const NETWORK_CONFIG = {
  // Tetrahedral topology (each node connects to 3 others)
  topology: 'tetrahedral',
  nodeId: process.env.NODE_ID || `node-${Date.now().toString(36)}-${Math.random().toString(36).substr(2, 5)}`,

  // Node roles based on golden ratio distribution
  roles: {
    master: 1,      // 1/6 - Main coordinator
    storage: 2,     // 2/6 - Primary storage nodes  
    cache: 2,       // 2/6 - Cache & search nodes
    gateway: 1      // 1/6 - Client gateway
  },

  // Network endpoints (configured per deployment)
  peers: [
    process.env.PEER_1, // Render node 1
    process.env.PEER_2, // Render node 2  
    process.env.PEER_3, // Render node 3
    process.env.PEER_4, // Railway node 1
    process.env.PEER_5, // Railway node 2
    process.env.PEER_6  // Railway node 3
  ].filter(Boolean),

  // Anti-sleep configuration
  keepAlive: {
    interval: 12 * 60 * 1000, // 12 minutes (before 15min sleep)
    pingUrl: process.env.PING_URL,
    healthCheck: '/heartbeat'
  },

  // Consensus parameters
  consensus: {
    quorum: 4,          // Need 4/6 nodes for consensus (golden ratio)
    syncInterval: 8000, // 8 seconds (fibonacci)
    conflictResolve: 'timestamp' // Resolve by latest timestamp
  },

  // Data distribution
  replication: {
    factor: 3,          // Each event stored on 3 nodes
    sharding: 'hash',   // Shard by event hash
    backupInterval: 21 * 60 * 1000 // 21 minutes (fibonacci)
  }
};

// ========================= DISTRIBUTED STORAGE ENGINE =========================

class DistributedStorage {
  constructor(nodeId) {
    this.nodeId = nodeId;
    this.role = this.determineRole();
    this.localData = new Map();
    this.replicatedData = new Map();
    this.metadata = new Map();
    this.peers = new Map();

    // Node state
    this.state = {
      isLeader: false,
      lastSync: Date.now(),
      version: 0,
      health: 'healthy'
    };

    // Conflict resolution
    this.conflictLog = [];
    this.lastHeartbeat = new Map();

    this.initializeNode();
  }

  determineRole() {
    // Use golden ratio to determine node role
    const hash = crypto.createHash('sha256').update(this.nodeId).digest('hex');
    const hashValue = parseInt(hash.slice(0, 8), 16);
    const position = (hashValue % 1000) / 1000; // 0-1

    if (position < 0.167) return 'master';      // 1/6
    if (position < 0.5) return 'storage';       // 2/6  
    if (position < 0.833) return 'cache';       // 2/6
    return 'gateway';                           // 1/6
  }

  async initializeNode() {
    console.log(`🌊 Node ${this.nodeId} starting as ${this.role}`);

    // Connect to peer network
    this.connectToPeers();

    // Start consensus protocol
    this.startConsensusLoop();

    // Begin anti-sleep mechanism
    this.startKeepAlive();

    // Initialize role-specific services
    this.initializeRoleServices();
  }

  async connectToPeers() {
    for (const peerUrl of NETWORK_CONFIG.peers) {
      if (!peerUrl || peerUrl.includes(this.nodeId)) continue;

      try {
        const ws = new WebSocket(peerUrl.replace('https://', 'wss://'));

        ws.on('open', () => {
          console.log(`🤝 Connected to peer: ${peerUrl}`);
          this.peers.set(peerUrl, {
            ws,
            lastSeen: Date.now(),
            health: 'connected'
          });

          // Send introduction
          this.sendToPeer(ws, {
            type: 'node_intro',
            nodeId: this.nodeId,
            role: this.role,
            timestamp: Date.now()
          });
        });

        ws.on('message', (data) => this.handlePeerMessage(peerUrl, data));
        ws.on('close', () => this.handlePeerDisconnect(peerUrl));

      } catch (error) {
        console.log(`❌ Failed to connect to ${peerUrl}:`, error.message);
      }
    }
  }

  startConsensusLoop() {
    // Golden ratio timing for consensus
    setInterval(() => {
      this.performConsensusRound();
    }, NETWORK_CONFIG.consensus.syncInterval);

    // Leader election every fibonacci interval
    setInterval(() => {
      this.electLeader();
    }, FIBONACCI[6] * 1000); // 8 seconds
  }

  startKeepAlive() {
    // Anti-sleep ping mechanism
    setInterval(async () => {
      try {
        // Self-ping to prevent sleep
        if (NETWORK_CONFIG.keepAlive.pingUrl) {
          const response = await fetch(NETWORK_CONFIG.keepAlive.pingUrl + '/heartbeat');
          console.log(`💓 Self-ping: ${response.status}`);
        }

        // Ping all peers to keep network alive
        this.pingAllPeers();

      } catch (error) {
        console.log('⚠️ Keep-alive error:', error.message);
      }
    }, NETWORK_CONFIG.keepAlive.interval);
  }

  initializeRoleServices() {
    switch (this.role) {
      case 'master':
        this.startMasterServices();
        break;
      case 'storage':
        this.startStorageServices();
        break;
      case 'cache':
        this.startCacheServices();
        break;
      case 'gateway':
        this.startGatewayServices();
        break;
    }
  }

  // ============= CONSENSUS PROTOCOL =============

  async performConsensusRound() {
    const activePeers = this.getActivePeers();

    if (activePeers.length < NETWORK_CONFIG.consensus.quorum - 1) {
      console.log(`⚠️ Insufficient peers for consensus: ${activePeers.length}`);
      return;
    }

    // Propose state updates
    const proposal = {
      type: 'consensus_proposal',
      nodeId: this.nodeId,
      version: this.state.version + 1,
      changes: this.getPendingChanges(),
      timestamp: Date.now()
    };

    // Send to all peers
    this.broadcastToPeers(proposal);
  }

  electLeader() {
    const activePeers = this.getActivePeers();

    // Use deterministic leader election based on node ID
    const candidates = [this.nodeId, ...activePeers.map(p => p.nodeId)].sort();
    const leader = candidates[0];

    const wasLeader = this.state.isLeader;
    this.state.isLeader = (leader === this.nodeId);

    if (this.state.isLeader && !wasLeader) {
      console.log(`👑 Node ${this.nodeId} became leader`);
      this.onBecomeLeader();
    }
  }

  onBecomeLeader() {
    // Leader responsibilities
    this.startDataSynchronization();
    this.startConflictResolution();
    this.coordinateReplication();
  }

  // ============= DATA OPERATIONS =============

  async addEvent(event) {
    // Determine storage nodes for this event (golden ratio distribution)
    const storageNodes = this.selectStorageNodes(event.id);

    // Store locally if this node is responsible
    if (storageNodes.includes(this.nodeId)) {
      this.localData.set(event.id, event);
      console.log(`📦 Stored event ${event.id.slice(0, 8)}... locally`);
    }

    // Replicate to other responsible nodes
    const replicationPromises = storageNodes
      .filter(nodeId => nodeId !== this.nodeId)
      .map(nodeId => this.replicateEvent(event, nodeId));

    await Promise.allSettled(replicationPromises);

    // Broadcast to network
    this.broadcastToPeers({
      type: 'event_added',
      event,
      timestamp: Date.now(),
      nodeId: this.nodeId
    });

    return true;
  }

  selectStorageNodes(eventId) {
    // Use consistent hashing with golden ratio distribution
    const hash = crypto.createHash('sha256').update(eventId).digest('hex');
    const hashValue = parseInt(hash.slice(0, 8), 16);

    const allNodes = [this.nodeId, ...this.getActivePeers().map(p => p.nodeId)];
    const storageNodes = allNodes.filter(nodeId => {
      const nodeHash = crypto.createHash('sha256').update(nodeId + eventId).digest('hex');
      const nodeValue = parseInt(nodeHash.slice(0, 8), 16);
      return nodeValue % NETWORK_CONFIG.replication.factor === 0;
    });

    // Ensure minimum replication
    if (storageNodes.length < NETWORK_CONFIG.replication.factor) {
      return allNodes.slice(0, NETWORK_CONFIG.replication.factor);
    }

    return storageNodes.slice(0, NETWORK_CONFIG.replication.factor);
  }

  async queryEvents(filter) {
    // Query local data
    let localResults = this.queryLocalData(filter);

    // If this is a gateway node, query storage nodes
    if (this.role === 'gateway' || this.role === 'master') {
      const storageResults = await this.queryStorageNodes(filter);
      localResults = this.mergeResults(localResults, storageResults);
    }

    return this.deduplicateAndSort(localResults);
  }

  // ============= PEER COMMUNICATION =============

  handlePeerMessage(peerUrl, data) {
    try {
      const message = JSON.parse(data);

      switch (message.type) {
        case 'node_intro':
          this.handleNodeIntro(peerUrl, message);
          break;
        case 'consensus_proposal':
          this.handleConsensusProposal(message);
          break;
        case 'event_added':
          this.handleEventAdded(message);
          break;
        case 'data_sync':
          this.handleDataSync(message);
          break;
        case 'heartbeat':
          this.handleHeartbeat(peerUrl, message);
          break;
      }
    } catch (error) {
      console.log('❌ Error handling peer message:', error);
    }
  }

  sendToPeer(ws, message) {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(message));
    }
  }

  broadcastToPeers(message) {
    for (const peer of this.peers.values()) {
      this.sendToPeer(peer.ws, message);
    }
  }

  pingAllPeers() {
    const heartbeat = {
      type: 'heartbeat',
      nodeId: this.nodeId,
      timestamp: Date.now(),
      health: this.state.health
    };

    this.broadcastToPeers(heartbeat);
  }

  // ============= NETWORK HEALTH =============

  getActivePeers() {
    const now = Date.now();
    const timeout = 30000; // 30 seconds

    return Array.from(this.peers.entries())
      .filter(([url, peer]) => now - peer.lastSeen < timeout)
      .map(([url, peer]) => ({ url, ...peer }));
  }

  handlePeerDisconnect(peerUrl) {
    console.log(`💔 Peer disconnected: ${peerUrl}`);

    const peer = this.peers.get(peerUrl);
    if (peer) {
      peer.health = 'disconnected';
      peer.lastSeen = Date.now();
    }

    // Trigger leader re-election if needed
    if (this.getActivePeers().length < NETWORK_CONFIG.consensus.quorum) {
      this.electLeader();
    }
  }

  // ============= ROLE-SPECIFIC SERVICES =============

  startMasterServices() {
    console.log('👑 Starting master node services');

    // Global state management
    this.state.isLeader = true;

    // Coordinate network health
    setInterval(() => {
      this.checkNetworkHealth();
    }, FIBONACCI[7] * 1000); // 13 seconds
  }

  startStorageServices() {
    console.log('📦 Starting storage node services');

    // Optimize for data persistence
    this.enableDataPersistence();

    // Regular backup to peers
    setInterval(() => {
      this.backupDataToPeers();
    }, NETWORK_CONFIG.replication.backupInterval);
  }

  startCacheServices() {
    console.log('⚡ Starting cache node services');

    // Optimize for fast queries
    this.buildSearchIndices();

    // Cache popular content
    setInterval(() => {
      this.cachePopularContent();
    }, FIBONACCI[8] * 1000); // 21 seconds
  }

  startGatewayServices() {
    console.log('🌐 Starting gateway node services');

    // Handle client connections
    this.optimizeForConnections();

    // Load balancing
    this.startLoadBalancing();
  }

  // ============= UTILITY METHODS =============

  queryLocalData(filter) {
    // Implementation for local data querying
    return Array.from(this.localData.values()).filter(event => {
      // Apply filters
      return true; // Simplified
    });
  }

  async queryStorageNodes(filter) {
    // Query distributed storage nodes
    const promises = this.getActivePeers()
      .filter(peer => peer.role === 'storage')
      .map(peer => this.queryPeerData(peer, filter));

    const results = await Promise.allSettled(promises);
    return results
      .filter(result => result.status === 'fulfilled')
      .flatMap(result => result.value);
  }

  mergeResults(local, remote) {
    // Merge and deduplicate results
    const merged = new Map();

    [...local, ...remote].forEach(event => {
      merged.set(event.id, event);
    });

    return Array.from(merged.values());
  }

  deduplicateAndSort(events) {
    return events
      .sort((a, b) => b.created_at - a.created_at)
      .slice(0, 100); // Limit results
  }

  getPendingChanges() {
    // Return pending state changes for consensus
    return [];
  }

  checkNetworkHealth() {
    const activePeers = this.getActivePeers();
    console.log(`💓 Network health: ${activePeers.length}/${NETWORK_CONFIG.peers.length} nodes active`);

    if (activePeers.length < NETWORK_CONFIG.consensus.quorum) {
      console.log('⚠️ Network partition detected - entering degraded mode');
      this.state.health = 'degraded';
    } else {
      this.state.health = 'healthy';
    }
  }

  // ============= MISSING METHODS (STUBS) =============

  buildSearchIndices() {
    console.log('🔍 Building search indices for cache node');
    // TODO: Implement search indexing
  }

  enableDataPersistence() {
    console.log('💾 Enabling data persistence for storage node');
    // TODO: Implement data persistence
  }

  backupDataToPeers() {
    console.log('🔄 Backing up data to peers');
    // TODO: Implement backup mechanism
  }

  cachePopularContent() {
    console.log('⚡ Caching popular content');
    // TODO: Implement content caching
  }

  optimizeForConnections() {
    console.log('🌐 Optimizing for gateway connections');
    // TODO: Implement connection optimization
  }

  startLoadBalancing() {
    console.log('⚖️ Starting load balancing');
    // TODO: Implement load balancing
  }

  startDataSynchronization() {
    console.log('🔄 Starting data synchronization');
    // TODO: Implement data sync
  }

  startConflictResolution() {
    console.log('⚖️ Starting conflict resolution');
    // TODO: Implement conflict resolution
  }

  coordinateReplication() {
    console.log('📡 Coordinating replication');
    // TODO: Implement replication coordination
  }
}

// ========================= EXPRESS API LAYER =========================

class DistributedAPI {
  constructor(storage) {
    this.storage = storage;
    this.app = express();
    this.setupMiddleware();
    this.setupRoutes();
  }

  setupMiddleware() {
    this.app.use(express.json({ limit: '1mb' }));
    this.app.use((req, res, next) => {
      res.header('Access-Control-Allow-Origin', '*');
      res.header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
      res.header('Access-Control-Allow-Headers', 'Content-Type');
      next();
    });
  }

  setupRoutes() {
    // Health check (anti-sleep endpoint)
    this.app.get('/heartbeat', (req, res) => {
      res.json({
        status: 'alive',
        nodeId: this.storage.nodeId,
        role: this.storage.role,
        timestamp: Date.now(),
        peers: this.storage.getActivePeers().length,
        health: this.storage.state.health
      });
    });

    // Distributed posts API
    this.app.get('/api/posts', async (req, res) => {
      try {
        const filter = {
          kinds: [1],
          limit: parseInt(req.query.limit) || 50,
          offset: parseInt(req.query.offset) || 0
        };

        const events = await this.storage.queryEvents(filter);

        res.json({
          items: events,
          node: this.storage.nodeId,
          network: {
            totalNodes: NETWORK_CONFIG.peers.length,
            activeNodes: this.storage.getActivePeers().length,
            role: this.storage.role
          }
        });
      } catch (error) {
        res.status(500).json({ error: 'Query failed', node: this.storage.nodeId });
      }
    });

    // Network status
    this.app.get('/api/network', (req, res) => {
      res.json({
        nodeId: this.storage.nodeId,
        role: this.storage.role,
        isLeader: this.storage.state.isLeader,
        peers: this.storage.getActivePeers().map(p => ({
          url: p.url,
          lastSeen: p.lastSeen,
          health: p.health
        })),
        network: {
          topology: NETWORK_CONFIG.topology,
          consensus: {
            quorum: NETWORK_CONFIG.consensus.quorum,
            version: this.storage.state.version
          }
        }
      });
    });

    // Fallback route for load balancing
    this.app.get('/', (req, res) => {
      res.json({
        service: 'Distributed Nostr Relay',
        node: this.storage.nodeId,
        role: this.storage.role,
        network: `${this.storage.getActivePeers().length}/${NETWORK_CONFIG.peers.length} nodes`,
        websocket: `wss://${req.headers.host}`,
        status: this.storage.state.health
      });
    });
  }

  start(port = 8080) {
    const server = this.app.listen(port, '0.0.0.0', () => {
      console.log(`🌊 Distributed node ${this.storage.nodeId} (${this.storage.role}) running on port ${port}`);
    });

    // WebSocket server for real-time sync
    const wss = new WebSocketServer({ server });

    wss.on('connection', (ws, req) => {
      console.log(`🔌 Client connected to ${this.storage.role} node`);

      // Send node info to client
      ws.send(JSON.stringify({
        type: 'node_info',
        nodeId: this.storage.nodeId,
        role: this.storage.role,
        network: this.storage.getActivePeers().length
      }));
    });

    return server;
  }
}

// ========================= INITIALIZATION =========================

async function initializeDistributedNode() {
  console.log('🌊 Initializing distributed Nostr node...');

  // Create storage layer
  const storage = new DistributedStorage(process.env.NODE_ID || `node-${Date.now().toString(36)}-${Math.random().toString(36).substr(2, 5)}`);

  // Create API layer
  const api = new DistributedAPI(storage);

  // Start server
  const server = api.start();

  // Graceful shutdown
  process.on('SIGTERM', () => {
    console.log('🛑 Graceful shutdown initiated');
    server.close(() => {
      console.log('✅ Node shutdown complete');
      process.exit(0);
    });
  });

  console.log('🚀 Distributed node initialization complete');
  console.log(`📊 Network topology: ${NETWORK_CONFIG.topology}`);
  console.log(`🎯 Node role: ${storage.role}`);
}

// Start the node
if (require.main === module) {
  initializeDistributedNode().catch(console.error);
}

module.exports = { DistributedStorage, DistributedAPI, NETWORK_CONFIG };
