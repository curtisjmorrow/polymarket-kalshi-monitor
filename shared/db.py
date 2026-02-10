"""Shared database for all sector bots."""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "sector_arb.db"

def get_conn():
    return sqlite3.connect(str(DB_PATH))

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS opportunities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        bot TEXT,
        market TEXT,
        arb_type TEXT,
        strategy TEXT,
        profit_cents REAL,
        price_a REAL,
        price_b REAL,
        source_a TEXT,
        source_b TEXT,
        url TEXT,
        meta TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_status (
        bot TEXT PRIMARY KEY,
        status TEXT,
        last_scan REAL,
        scan_count INTEGER,
        markets_scanned INTEGER,
        matched_pairs INTEGER,
        last_opp_count INTEGER,
        meta TEXT
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_opp_ts ON opportunities(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_opp_bot ON opportunities(bot)')
    conn.commit()
    conn.close()

def log_opportunity(bot, market, arb_type, strategy, profit_cents, price_a=0, price_b=0, source_a="", source_b="", url="", meta=""):
    conn = get_conn()
    c = conn.cursor()
    c.execute('''INSERT INTO opportunities (timestamp, bot, market, arb_type, strategy, profit_cents, price_a, price_b, source_a, source_b, url, meta)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (time.time(), bot, market[:300], arb_type, strategy[:200], profit_cents, price_a, price_b, source_a, source_b, url, meta))
    conn.commit()
    conn.close()

def update_bot_status(bot, status="running", scan_count=0, markets_scanned=0, matched_pairs=0, last_opp_count=0, meta=""):
    conn = get_conn()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO bot_status (bot, status, last_scan, scan_count, markets_scanned, matched_pairs, last_opp_count, meta)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (bot, status, time.time(), scan_count, markets_scanned, matched_pairs, last_opp_count, meta))
    conn.commit()
    conn.close()

def get_all_bot_status():
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM bot_status ORDER BY bot')
    rows = c.fetchall()
    conn.close()
    cols = ['bot', 'status', 'last_scan', 'scan_count', 'markets_scanned', 'matched_pairs', 'last_opp_count', 'meta']
    return [dict(zip(cols, r)) for r in rows]

def get_recent_opportunities(limit=50, bot=None):
    conn = get_conn()
    c = conn.cursor()
    if bot:
        c.execute('SELECT * FROM opportunities WHERE bot=? ORDER BY timestamp DESC LIMIT ?', (bot, limit))
    else:
        c.execute('SELECT * FROM opportunities ORDER BY timestamp DESC LIMIT ?', (limit,))
    rows = c.fetchall()
    conn.close()
    cols = ['id', 'timestamp', 'bot', 'market', 'arb_type', 'strategy', 'profit_cents', 'price_a', 'price_b', 'source_a', 'source_b', 'url', 'meta']
    return [dict(zip(cols, r)) for r in rows]

def get_period_stats(seconds, bot=None):
    conn = get_conn()
    c = conn.cursor()
    cutoff = time.time() - seconds
    if bot:
        c.execute('SELECT COUNT(*), COALESCE(SUM(profit_cents),0) FROM opportunities WHERE timestamp>? AND bot=?', (cutoff, bot))
    else:
        c.execute('SELECT COUNT(*), COALESCE(SUM(profit_cents),0) FROM opportunities WHERE timestamp>?', (cutoff,))
    row = c.fetchone()
    conn.close()
    return {'count': row[0], 'profit_cents': round(row[1], 2)}

# Init on import
init_db()
