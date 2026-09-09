"""Durable non-financial virtual prints for public market data, isolated from accounts."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
import time

DAY_MS = 86_400_000
KEEP_MS = 25 * 3_600_000

class PublicTradeTape:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.last_prune = 0
        with self.connect() as db:
            db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS prints (trade_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, timestamp INTEGER NOT NULL, price TEXT NOT NULL, quantity TEXT NOT NULL, quote_volume TEXT NOT NULL, side TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS prints_symbol_time ON prints(symbol,timestamp)')
            db.execute('CREATE INDEX IF NOT EXISTS prints_time ON prints(timestamp)')
        self.prune(int(time.time()*1000))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA max_page_count=32768')  # 128 MiB at SQLite default 4 KiB pages.
        db.execute('PRAGMA journal_size_limit=4194304')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def prune(self, now):
        with self.lock, self.connect() as db:
            db.execute('DELETE FROM prints WHERE timestamp < ?', (now-KEEP_MS,))
            db.commit()
            db.execute('PRAGMA incremental_vacuum(256)')
        self.last_prune = now

    async def append(self, symbol, item):
        await asyncio.to_thread(self._append, symbol, item)

    def _append(self, symbol, item):
        from decimal import Decimal
        now = int(time.time()*1000)
        if now-self.last_prune >= 300_000:
            self.prune(now)
        with self.lock, self.connect() as db:
            db.execute('INSERT OR IGNORE INTO prints VALUES (?,?,?,?,?,?,?)',
                (item['trade_id'],symbol,int(item['ts']),str(item['price']),str(item['quantity']),
                 str(Decimal(str(item['price']))*Decimal(str(item['quantity']))),item['side']))

    async def snapshot(self, now=None):
        return await asyncio.to_thread(self._snapshot, int(time.time()*1000) if now is None else now)

    def _snapshot(self, now):
        if now-self.last_prune >= 300_000:
            self.prune(now)
        with self.lock, self.connect() as db:
            sums = {r[0]:r[1:] for r in db.execute('SELECT symbol,sum(CAST(quantity AS REAL)),sum(CAST(quote_volume AS REAL)),min(CAST(price AS REAL)),max(CAST(price AS REAL)) FROM prints WHERE timestamp>=? AND timestamp<=? GROUP BY symbol',(now-DAY_MS,now))}
            latest = {}
            for row in db.execute('SELECT p.* FROM prints p JOIN (SELECT symbol,max(timestamp) t FROM prints WHERE timestamp<=? GROUP BY symbol) x ON p.symbol=x.symbol AND p.timestamp=x.t ORDER BY p.trade_id',(now,)):
                latest[row[1]] = self.serialize(row)
        return sums,latest

    @staticmethod
    def serialize(row):
        return dict(trade_id=row[0],ticker_id=row[1],timestamp=row[2],price=row[3],base_volume=row[4],quote_volume=row[5],type=row[6],source='virtual_volume',is_virtual=True,financial_effect=False,is_simulated=True)

    async def recent(self, symbol, limit):
        return await asyncio.to_thread(self._recent,symbol,limit)

    def _recent(self,symbol,limit):
        now=int(time.time()*1000)
        if now-self.last_prune>=300_000:self.prune(now)
        with self.lock, self.connect() as db:
            return [self.serialize(r) for r in db.execute('SELECT * FROM prints WHERE symbol=? AND timestamp<=? ORDER BY timestamp DESC,trade_id DESC LIMIT ?',(symbol,now,limit))]
