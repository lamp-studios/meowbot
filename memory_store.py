"""Persistent memory: chat histories, a full chat log, and saved facts, in one sqlite file.

Off by default. The bot owner turns it on with /persistent_memory, and the switch itself is
stored in the database so it survives restarts. While it's off nothing new is written,
except that /reset still deletes a user's stored data.

Tables:
    settings   key/value pairs (just "persistent_memory" for now)
    histories  each user's current conversation, the same trimmed list the bot keeps in RAM
    chat_log   every user and assistant message, never trimmed, until that user runs /reset
    memories   short facts the model chose to remember about a user (save_memory tool)
"""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")
MAX_MEMORIES_PER_USER = 50
MAX_MEMORY_CHARS = 300


class MemoryStore:
    def __init__(self, path=DB_PATH):
        self.path = path
        self._lock = threading.Lock()
        # one connection shared by the whole bot. every call is a few rows, so these run
        # inline on the event loop, guarded by the lock in case a thread ever uses it
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")   # safer if the bot gets killed mid-write
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS histories (
                user_id INTEGER PRIMARY KEY, messages TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS chat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS chat_log_user ON chat_log (user_id);
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                fact TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS memories_user ON memories (user_id);
        """)
        self._db.commit()
        self._enabled = self._get_setting("persistent_memory") == "on"

    def _run(self, sql, params=(), fetch=False):
        with self._lock:
            cur = self._db.execute(sql, params)
            rows = cur.fetchall() if fetch else None
            self._db.commit()
            return rows if fetch else cur.rowcount

    def _get_setting(self, key):
        rows = self._run("SELECT value FROM settings WHERE key = ?", (key,), fetch=True)
        return rows[0][0] if rows else None

    # ---- the switch ----

    def enabled(self):
        return self._enabled

    def set_enabled(self, on):
        self._run("INSERT INTO settings (key, value) VALUES ('persistent_memory', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("on" if on else "off",))
        self._enabled = bool(on)

    # ---- histories ----

    def load_history(self, user_id):
        rows = self._run("SELECT messages FROM histories WHERE user_id = ?", (user_id,), fetch=True)
        if not rows:
            return None
        try:
            messages = json.loads(rows[0][0])
        except ValueError:
            return None
        return messages if isinstance(messages, list) else None

    def save_history(self, user_id, history):
        if not self._enabled:
            return
        # pictures never go in: they're huge, and the bot drops them from history anyway
        clean = [{"role": m.get("role"), "content": m.get("content", "")} for m in history]
        self._run("INSERT INTO histories (user_id, messages, updated) VALUES (?, ?, ?) "
                  "ON CONFLICT(user_id) DO UPDATE SET messages = excluded.messages, updated = excluded.updated",
                  (user_id, json.dumps(clean), time.time()))

    # ---- full chat log ----

    def log(self, user_id, role, content):
        if not self._enabled or not content:
            return
        self._run("INSERT INTO chat_log (user_id, role, content, created) VALUES (?, ?, ?, ?)",
                  (user_id, role, content, time.time()))

    def log_count(self, user_id):
        return self._run("SELECT COUNT(*) FROM chat_log WHERE user_id = ?", (user_id,), fetch=True)[0][0]

    # ---- memories ----

    def memories(self, user_id):
        return self._run("SELECT id, fact FROM memories WHERE user_id = ? ORDER BY id",
                         (user_id,), fetch=True)

    def add_memory(self, user_id, fact):
        """Returns a short status string for the model."""
        if not self._enabled:
            return "persistent memory is turned off, nothing was saved"
        fact = " ".join((fact or "").split())[:MAX_MEMORY_CHARS]
        if not fact:
            return "nothing to remember"
        existing = self.memories(user_id)
        if any(f.lower() == fact.lower() for _, f in existing):
            return "already remembered that"
        if len(existing) >= MAX_MEMORIES_PER_USER:
            # drop the oldest so the newest facts always fit
            self._run("DELETE FROM memories WHERE id = ?", (existing[0][0],))
        self._run("INSERT INTO memories (user_id, fact, created) VALUES (?, ?, ?)",
                  (user_id, fact, time.time()))
        return f"remembered: {fact}"

    def forget_memory(self, user_id, text):
        text = (text or "").strip().lower()
        if not text:
            return "say what to forget"
        doomed = [(i, f) for i, f in self.memories(user_id) if text in f.lower()]
        for i, _ in doomed:
            self._run("DELETE FROM memories WHERE id = ?", (i,))
        if not doomed:
            return "no saved memory matched that"
        return "forgot: " + "; ".join(f for _, f in doomed)

    # ---- reset ----

    def wipe_user(self, user_id):
        """Delete everything stored about one user. Works even while the mode is off."""
        removed = 0
        for table in ("histories", "chat_log", "memories"):
            removed += self._run(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        return removed

    def stats(self):
        q = lambda sql: self._run(sql, fetch=True)[0][0]
        return {"users": q("SELECT COUNT(DISTINCT user_id) FROM chat_log"),
                "messages": q("SELECT COUNT(*) FROM chat_log"),
                "memories": q("SELECT COUNT(*) FROM memories"),
                "histories": q("SELECT COUNT(*) FROM histories")}
