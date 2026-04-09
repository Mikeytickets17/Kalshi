// Fetch real bot state from bot_state.json (pushed by ghpages_publisher)
// If found and recent, overrides simulated data with real bot trades

async function loadBotState() {
  try {
    const r = await fetch('bot_state.json?t=' + Date.now());
    if (!r.ok) return false;
    const state = await r.json();
    
    // Check if state is recent (< 5 minutes)
    const age = (Date.now() / 1000) - (state.last_updated || 0);
    if (age > 300) {
      console.log('Bot state is stale (' + Math.round(age) + 's old), using simulated data');
      return false;
    }
    
    console.log('LIVE BOT DATA loaded! Age: ' + Math.round(age) + 's');
    
    // Override dashboard variables with real bot data
    totalPnl = (state.portfolio_value || 10000) - (state.initial_balance || 10000);
    todayPnl = totalPnl; // TODO: track daily separately
    tradeCount = state.trade_count || 0;
    winCount = state.win_count || 0;
    portfolio = state.portfolio_value || 10000;
    pnlHistory = (state.equity_curve || []).map(v => v - (state.initial_balance || 10000));
    
    // Load closed trades
    if (state.closed_trades && state.closed_trades.length > 0) {
      closedTrades = state.closed_trades.map(t => ({
        time: new Date((t.closed_at || 0) * 1000).toLocaleTimeString(),
        strat: t.strategy || 'ARB',
        side: t.side || 'YES',
        asset: t.asset || '',
        venue: t.venue || 'Kalshi',
        size: '$' + (t.size_usd || 0).toFixed(0),
        pnl: t.pnl || 0,
        result: t.result || 'WIN'
      }));
    }
    
    // Load active positions
    if (state.active_positions) {
      activePositions = state.active_positions.map((p, i) => ({
        id: p.id || 'p' + i,
        time: new Date((p.opened_at || 0) * 1000).toLocaleTimeString(),
        strat: p.strategy || 'ARB',
        side: p.side || 'YES',
        asset: p.asset || '',
        venue: p.venue || 'Kalshi',
        entry: p.entry_price || 0,
        size: p.size_usd || 0,
        unrealizedPnl: p.unrealized_pnl || 0,
        openedAt: (p.opened_at || 0) * 1000
      }));
    }
    
    // Load signals
    if (state.signals && state.signals.length > 0) {
      signals = state.signals.slice(0, 20).map(s => ({
        time: new Date((s.time || 0) * 1000).toLocaleTimeString(),
        strat: s.strategy || 'ARB',
        side: s.side || 'YES',
        asset: s.asset || '',
        venue: s.venue || 'Kalshi',
        reason: s.reason || '',
        conf: s.confidence || 0,
        pnl: 0,
        size: 0
      }));
    }
    
    // Load trump posts
    if (state.trump_posts && state.trump_posts.length > 0) {
      const tp = state.trump_posts[0];
      currentTrump = {
        text: tp.text || '',
        dir: (tp.sentiment || 'NEUTRAL').toUpperCase(),
        conf: tp.confidence || 0,
        move: 0,
        topics: []
      };
    }
    
    // Load news
    if (state.news_items && state.news_items.length > 0) {
      newsItems = state.news_items.map(n => ({
        time: new Date((n.detected_at || 0) * 1000).toLocaleTimeString(),
        text: n.headline || '',
        source: n.source || '',
        pri: (n.priority || 'NEWS').toUpperCase(),
        col: n.priority === 'critical' ? 'var(--red)' : n.priority === 'high' ? 'var(--amber)' : 'var(--muted)'
      }));
    }
    
    // Update badge to show LIVE BOT
    const badges = document.querySelector('.badges');
    if (badges) {
      const existingLive = badges.querySelector('.b-live');
      if (!existingLive) {
        const b = document.createElement('span');
        b.className = 'badge b-live';
        b.style.cssText = 'background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.4);padding:2px 8px;border-radius:10px;font-size:8px;font-weight:700';
        b.textContent = 'BOT CONNECTED';
        badges.prepend(b);
      }
    }
    
    render();
    return true;
  } catch (e) {
    console.log('No bot_state.json found, using simulated data');
    return false;
  }
}

// Try to load real bot state every 30 seconds
setInterval(loadBotState, 30000);
// Also try on first load
setTimeout(loadBotState, 2000);
