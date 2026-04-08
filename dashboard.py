"""Kalshi Trading Bot Dashboard."""
import json
import os
import time
from flask import Flask, jsonify, Response

app = Flask(__name__)

INITIAL_BALANCE = 10000.0
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot_state.json')
DASHBOARD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard.html')

_last_good_state = None

def get_state():
    global _last_good_state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                raw = f.read()
            if len(raw) > 10:
                s = json.loads(raw)
                if s.get('trade_count', 0) > 0 or s.get('bot_running'):
                    _last_good_state = s
                    return s
    except (json.JSONDecodeError, IOError):
        pass
    if _last_good_state:
        return _last_good_state
    return None

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route('/')
def index():
    # Serve the ROOT dashboard.html — the exact same file that works at file:///
    with open(DASHBOARD_FILE, 'r', encoding='utf-8') as f:
        return Response(f.read(), mimetype='text/html')

@app.route('/api/state')
def api_state():
    s = get_state()
    if not s:
        return jsonify({'bot_running': False, 'portfolio_value': INITIAL_BALANCE, 'positions': [], 'closed_trades': [], 'signals': [], 'trump_posts': [], 'news_items': [], 'equity_curve': [INITIAL_BALANCE], 'trade_count': 0, 'win_count': 0, 'total_trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0, 'realized_pnl': 0, 'unrealized_pnl': 0, 'total_exposure': 0, 'win_rate': 0, 'peak_value': INITIAL_BALANCE, 'initial_balance': INITIAL_BALANCE, 'drawdown_pct': 0, 'risk': {}, 'uptime': '0m', 'last_scan': 'Not running'})
    pv = s.get('portfolio_value', INITIAL_BALANCE)
    pk = s.get('peak_value', INITIAL_BALANCE)
    cl = s.get('closed_trades', [])
    ac = s.get('active_positions', [])
    tc = s.get('trade_count', 0)
    wc = s.get('win_count', 0)
    tp = pv - INITIAL_BALANCE
    dd = (pk - pv) / pk if pk > 0 else 0
    ut = int(time.time() - s.get('start_time', time.time()))
    uh = ut // 3600
    um = (ut % 3600) // 60
    return jsonify({'bot_running': True, 'portfolio_value': round(pv, 2), 'initial_balance': INITIAL_BALANCE, 'total_pnl': round(tp, 2), 'peak_value': round(pk, 2), 'total_exposure': round(sum(p.get('size_usd', 0) for p in ac), 2), 'unrealized_pnl': round(sum(p.get('unrealized_pnl', 0) for p in ac), 2), 'realized_pnl': round(sum(c.get('pnl', 0) for c in cl), 2), 'equity_curve': s.get('equity_curve', [INITIAL_BALANCE])[-200:], 'positions': ac, 'closed_trades': cl, 'signals': s.get('signals', []), 'trump_posts': s.get('trump_posts', []), 'news_items': s.get('news_items', []), 'risk': s.get('risk', {}), 'trade_count': tc, 'win_count': wc, 'total_trades': len(cl), 'wins': wc, 'losses': tc - wc, 'win_rate': round(wc / max(tc, 1) * 100, 1), 'drawdown_pct': round(dd * 100, 2), 'uptime': f'{uh}h {um}m', 'last_scan': f'{int(time.time() - s.get("last_updated", time.time()))}s ago'})

if __name__ == '__main__':
    print('Dashboard: http://localhost:5050')
    app.run(host='0.0.0.0', port=5050, debug=False)
