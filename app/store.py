"""Single-instance durable inbox. Run with one worker and a persistent disk."""
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS leads (
          phone TEXT PRIMARY KEY, name TEXT, stage TEXT DEFAULT 'New', paused INTEGER DEFAULT 0,
          opted_out INTEGER DEFAULT 0, updated REAL, last_inbound REAL);
        CREATE TABLE IF NOT EXISTS inbox (
          id TEXT PRIMARY KEY, phone TEXT, body TEXT, timestamp REAL, state TEXT DEFAULT 'pending',
          attempts INTEGER DEFAULT 0, available REAL DEFAULT 0, reply TEXT, outbound_id TEXT,
          delivery TEXT, error TEXT);
        ''')
        # A crash between API send and database acknowledgement needs manual reconciliation,
        # rather than automatically risking a duplicate outbound message.
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(inbox)')}
        if 'kind' not in columns:
            self.db.execute("ALTER TABLE inbox ADD COLUMN kind TEXT DEFAULT 'incoming'")
        self.db.execute("UPDATE inbox SET state='uncertain',error='Worker restarted during send; reconcile with Meta' WHERE state='sending'")
        self.db.commit()

    def ingest(self, message, name):
        timestamp = float(message['timestamp'])
        phone = message['from']
        body = message.get('text', {}).get('body', '') if message.get('type') == 'text' else '[Unsupported message]'
        cur = self.db.execute('INSERT OR IGNORE INTO inbox(id,phone,body,timestamp) VALUES(?,?,?,?)',
                              (message['id'], phone, body[:4000], timestamp))
        if cur.rowcount:
            self.db.execute('''INSERT INTO leads(phone,name,updated,last_inbound) VALUES(?,?,?,?)
            ON CONFLICT(phone) DO UPDATE SET name=excluded.name,updated=excluded.updated,
            last_inbound=MAX(leads.last_inbound,excluded.last_inbound)''', (phone, name[:100], time.time(), timestamp))
        self.db.commit()

    def next_message(self):
        return self.db.execute("SELECT * FROM inbox WHERE state='pending' AND available<=? ORDER BY timestamp,id LIMIT 1", (time.time(),)).fetchone()

    def lead(self, phone):
        return self.db.execute('SELECT * FROM leads WHERE phone=?', (phone,)).fetchone()

    def update_lead(self, phone, **values):
        allowed = {'paused', 'opted_out', 'stage'}
        if not values or not set(values) <= allowed:
            raise ValueError('Unsupported lead fields')
        assignments = ','.join(f'{field}=?' for field in values)
        self.db.execute(f'UPDATE leads SET {assignments},updated=? WHERE phone=?', (*values.values(), time.time(), phone))
        self.db.commit()

    def update_message(self, identifier, **values):
        allowed = {'state', 'attempts', 'available', 'reply', 'outbound_id', 'delivery', 'error'}
        if not values or not set(values) <= allowed:
            raise ValueError('Unsupported message fields')
        assignments = ','.join(f'{field}=?' for field in values)
        self.db.execute(f'UPDATE inbox SET {assignments} WHERE id=?', (*values.values(), identifier))
        self.db.commit()

    def status(self, identifier, status):
        self.db.execute('UPDATE inbox SET delivery=? WHERE outbound_id=?', (status, identifier))
        self.db.commit()

    def purge(self, days):
        cutoff = time.time() - max(days, 1) * 86400
        self.db.execute('DELETE FROM inbox WHERE timestamp<?', (cutoff,))
        # Retain only the minimum suppression state for opted-out people.
        self.db.execute("UPDATE leads SET name='' WHERE updated<? AND opted_out=1", (cutoff,))
        self.db.execute('DELETE FROM leads WHERE updated<? AND opted_out=0', (cutoff,))
        self.db.commit()

    def history(self, phone):
        rows = self.db.execute("SELECT body,reply FROM inbox WHERE phone=? AND state='sent' AND kind='incoming' ORDER BY timestamp DESC LIMIT 4", (phone,)).fetchall()
        return list(reversed([dict(row) for row in rows]))

    def leads(self, limit=100):
        return [dict(row) for row in self.db.execute('''SELECT leads.*,
          (SELECT body FROM inbox WHERE inbox.phone=leads.phone AND kind='incoming' ORDER BY timestamp DESC LIMIT 1) AS last_message
          FROM leads ORDER BY updated DESC LIMIT ?''', (limit,))]

    def issues(self):
        return [dict(row) for row in self.db.execute("SELECT id,phone,state,error FROM inbox WHERE state IN ('failed','uncertain','expired') ORDER BY timestamp DESC LIMIT 100")]

    def delete_lead(self, phone):
        self.db.execute('DELETE FROM inbox WHERE phone=?', (phone,))
        self.db.execute('DELETE FROM leads WHERE phone=?', (phone,))
        self.db.commit()

    def queue_manual(self, identifier, phone, text):
        lead = self.lead(phone)
        existing = self.db.execute('SELECT * FROM inbox WHERE id=?', (identifier,)).fetchone()
        if existing:
            if existing['phone'] != phone or existing['body'] != text or existing['kind'] != 'manual':
                raise ValueError('Idempotency key already used for another message')
            return False
        self.db.execute("INSERT INTO inbox(id,phone,body,timestamp,kind,reply) VALUES(?,?,?,?,'manual',?)",
                        (identifier,phone,text,lead['last_inbound'],text))
        self.db.execute("UPDATE leads SET paused=1,stage='Needs human',updated=? WHERE phone=?",(time.time(),phone))
        self.db.commit()
        return True
