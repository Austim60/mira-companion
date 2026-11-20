"""SQLite persistence for the single-process conversation core."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import MiraError


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ConversationRepository:
    """Keep turns and their messages together in SQLite transactions."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self):
        self.connection.close()

    def _migrate(self):
        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS characters (
                    character_id TEXT PRIMARY KEY, config_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL,
                    character_id TEXT NOT NULL REFERENCES characters(character_id),
                    source TEXT NOT NULL, source_message_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    error_code TEXT,
                    UNIQUE(user_id, character_id, source, source_message_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    text TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(turn_id, role)
                );
                CREATE TABLE IF NOT EXISTS model_calls (
                    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id),
                    attempt INTEGER NOT NULL, provider TEXT NOT NULL,
                    model TEXT NOT NULL, status TEXT NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER,
                    error_code TEXT, created_at TEXT NOT NULL,
                    UNIQUE(turn_id, attempt)
                );
                CREATE INDEX IF NOT EXISTS turns_scope_sequence
                    ON turns(user_id, character_id, sequence);
                CREATE TABLE IF NOT EXISTS user_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    character_id TEXT NOT NULL REFERENCES characters(character_id),
                    kind TEXT NOT NULL CHECK(kind IN ('nickname','reply_style','avoid_topic','correction')),
                    preference_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, character_id, kind, preference_key)
                );
                CREATE INDEX IF NOT EXISTS preferences_active_scope
                    ON user_preferences(user_id, character_id, active);
                CREATE TABLE IF NOT EXISTS contact_pauses (
                    user_id TEXT NOT NULL,
                    character_id TEXT NOT NULL REFERENCES characters(character_id),
                    paused_until TEXT,
                    active INTEGER NOT NULL CHECK(active IN (0,1)),
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, character_id)
                );
                CREATE TABLE IF NOT EXISTS proactive_settings (
                    user_id TEXT NOT NULL,
                    character_id TEXT NOT NULL REFERENCES characters(character_id),
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, character_id)
                );
                CREATE TABLE IF NOT EXISTS proactive_decisions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    character_id TEXT NOT NULL REFERENCES characters(character_id),
                    opportunity TEXT NOT NULL, status TEXT NOT NULL,
                    follow_up_id TEXT,
                    text TEXT, reason TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, character_id, opportunity)
                );
                CREATE TABLE IF NOT EXISTS follow_ups (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    character_id TEXT NOT NULL REFERENCES characters(character_id),
                    source_turn_id TEXT NOT NULL REFERENCES turns(id),
                    topic TEXT NOT NULL, due_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','asked','resolved','cancelled','uncertain')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(source_turn_id, topic)
                );
                CREATE INDEX IF NOT EXISTS follow_ups_due
                    ON follow_ups(user_id, character_id, status, due_at);
            """)
            columns = {row[1] for row in self.connection.execute(
                "PRAGMA table_info(proactive_decisions)"
            )}
            if "follow_up_id" not in columns:
                self.connection.execute(
                    "ALTER TABLE proactive_decisions ADD COLUMN follow_up_id TEXT"
                )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (1, ?)", (utc_now(),)
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (2, ?)", (utc_now(),)
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (3, ?)", (utc_now(),)
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (4, ?)", (utc_now(),)
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (5, ?)", (utc_now(),)
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations VALUES (6, ?)", (utc_now(),)
            )

    def bind_character(self, character_id, config_path):
        existing = self.connection.execute("SELECT character_id FROM characters").fetchall()
        if existing and any(row[0] != character_id for row in existing):
            raise MiraError("character_id_mismatch")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO characters VALUES (?, ?, ?)",
                (character_id, str(config_path), utc_now()),
            )

    def recover_pending(self):
        with self.connection:
            self.connection.execute(
                "DELETE FROM follow_ups WHERE status='pending' AND source_turn_id IN "
                "(SELECT id FROM turns WHERE status='pending')"
            )
            self.connection.execute(
                "UPDATE model_calls SET status='failed', error_code='interrupted' "
                "WHERE status='pending'"
            )
            self.connection.execute(
                "UPDATE turns SET status='failed', error_code='interrupted', updated_at=? "
                "WHERE status='pending'", (utc_now(),)
            )
            self.connection.execute(
                "UPDATE follow_ups SET status='uncertain', updated_at=? WHERE id IN "
                "(SELECT follow_up_id FROM proactive_decisions WHERE status='sending') "
                "AND status='pending'", (utc_now(),)
            )
            self.connection.execute(
                "UPDATE proactive_decisions SET status='skipped', reason='interrupted', "
                "updated_at=? WHERE status='pending'", (utc_now(),)
            )
            self.connection.execute(
                "UPDATE proactive_decisions SET status='unknown', reason='delivery_unknown', "
                "updated_at=? WHERE status='sending'", (utc_now(),)
            )

    def create_or_get(self, message):
        key = (message.user_id, message.character_id, message.source, message.source_message_id)
        with self.connection:
            existing = self.connection.execute(
                "SELECT * FROM turns WHERE user_id=? AND character_id=? "
                "AND source=? AND source_message_id=?", key
            ).fetchone()
            if existing:
                original = self.user_message(existing["id"])
                if original["text"] != message.text:
                    raise MiraError("message_conflict")
                return existing, False
            now = utc_now()
            turn_id = str(uuid4())
            self.connection.execute(
                "INSERT INTO turns (id,user_id,character_id,source,source_message_id,"
                "status,created_at,updated_at) VALUES (?,?,?,?,?,'pending',?,?)",
                (turn_id, *key, now, now),
            )
            self.connection.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                (str(uuid4()), turn_id, "user", message.text, message.occurred_at, now),
            )
            return self.get_turn(turn_id), True

    def get_turn(self, turn_id):
        return self.connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()

    def find_turn_by_source(self, user_id, character_id, source, source_message_id):
        return self.connection.execute(
            "SELECT * FROM turns WHERE user_id=? AND character_id=? "
            "AND source=? AND source_message_id=?",
            (user_id, character_id, source, source_message_id),
        ).fetchone()

    def user_message(self, turn_id):
        return self.connection.execute(
            "SELECT * FROM messages WHERE turn_id=? AND role='user'", (turn_id,)
        ).fetchone()

    def assistant_message(self, turn_id):
        return self.connection.execute(
            "SELECT * FROM messages WHERE turn_id=? AND role='assistant'", (turn_id,)
        ).fetchone()

    def latest_turn(self, user_id, character_id):
        return self.connection.execute(
            "SELECT * FROM turns WHERE user_id=? AND character_id=? "
            "ORDER BY sequence DESC LIMIT 1", (user_id, character_id)
        ).fetchone()

    def set_preference(self, user_id, character_id, kind, key, value, source):
        now = utc_now()
        with self.connection:
            self._upsert_preference(user_id, character_id, kind, key, value, source, now)
            return self.connection.execute(
                "SELECT * FROM user_preferences WHERE user_id=? AND character_id=? "
                "AND kind=? AND preference_key=?",
                (user_id, character_id, kind, key),
            ).fetchone()

    def _upsert_preference(self, user_id, character_id, kind, key, value, source, now):
        self.connection.execute(
            "INSERT INTO user_preferences "
            "(user_id,character_id,kind,preference_key,value,active,source,created_at,updated_at) "
            "VALUES (?,?,?,?,?,1,?,?,?) "
            "ON CONFLICT(user_id,character_id,kind,preference_key) DO UPDATE SET "
            "value=excluded.value, active=1, source=excluded.source, updated_at=excluded.updated_at",
            (user_id, character_id, kind, key, value, source, now, now),
        )

    def list_preferences(self, user_id, character_id):
        return self.connection.execute(
            "SELECT * FROM user_preferences WHERE user_id=? AND character_id=? "
            "AND active=1 ORDER BY id", (user_id, character_id)
        ).fetchall()

    def remove_preference(self, user_id, character_id, preference_id):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE user_preferences SET active=0, updated_at=? "
                "WHERE id=? AND user_id=? AND character_id=? AND active=1",
                (utc_now(), preference_id, user_id, character_id),
            ).rowcount
            if changed != 1:
                raise MiraError("preference_not_found")

    def remove_preference_at(self, user_id, character_id, position):
        with self.connection:
            row = self.connection.execute(
                "SELECT id,kind,value FROM user_preferences "
                "WHERE user_id=? AND character_id=? AND active=1 "
                "ORDER BY id LIMIT 1 OFFSET ?",
                (user_id, character_id, position - 1),
            ).fetchone()
            if row is None:
                raise MiraError("preference_not_found")
            self.connection.execute(
                "UPDATE user_preferences SET active=0, updated_at=? WHERE id=?",
                (utc_now(), row["id"]),
            )
            return row

    def set_contact_pause(self, user_id, character_id, paused_until, source):
        with self.connection:
            self.connection.execute(
                "INSERT INTO contact_pauses VALUES (?,?,?,1,?,?) "
                "ON CONFLICT(user_id,character_id) DO UPDATE SET "
                "paused_until=excluded.paused_until, active=1, "
                "source=excluded.source, updated_at=excluded.updated_at",
                (user_id, character_id, paused_until, source, utc_now()),
            )

    def resume_contact(self, user_id, character_id, source):
        with self.connection:
            self.connection.execute(
                "UPDATE contact_pauses SET active=0, source=?, updated_at=? "
                "WHERE user_id=? AND character_id=?",
                (source, utc_now(), user_id, character_id),
            )

    def get_contact_pause(self, user_id, character_id, now):
        row = self.connection.execute(
            "SELECT * FROM contact_pauses WHERE user_id=? AND character_id=? AND active=1",
            (user_id, character_id),
        ).fetchone()
        if row is None or (row["paused_until"] is not None and
                           datetime.fromisoformat(row["paused_until"]) <= now):
            return None
        return row

    def set_proactive_enabled(self, user_id, character_id, enabled):
        with self.connection:
            self.connection.execute(
                "INSERT INTO proactive_settings VALUES (?,?,?,?) "
                "ON CONFLICT(user_id,character_id) DO UPDATE SET "
                "enabled=excluded.enabled, updated_at=excluded.updated_at",
                (user_id, character_id, int(enabled), utc_now()),
            )

    def proactive_enabled(self, user_id, character_id):
        row = self.connection.execute(
            "SELECT enabled FROM proactive_settings WHERE user_id=? AND character_id=?",
            (user_id, character_id),
        ).fetchone()
        return row is not None and bool(row[0])

    def reserve_opportunity(self, user_id, character_id, opportunity, follow_up_id=None,
                            now=None):
        now = now.astimezone(timezone.utc).isoformat() if now else utc_now()
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO proactive_decisions "
                "(id,user_id,character_id,opportunity,status,follow_up_id,created_at,updated_at) "
                "VALUES (?,?,?,?,'pending',?,?,?)",
                (str(uuid4()), user_id, character_id, opportunity, follow_up_id, now, now),
            )
            return cursor.rowcount == 1

    def finish_opportunity(self, user_id, character_id, opportunity, status, text=None, reason=None):
        with self.connection:
            row = self.connection.execute(
                "SELECT follow_up_id FROM proactive_decisions "
                "WHERE user_id=? AND character_id=? AND opportunity=?",
                (user_id, character_id, opportunity),
            ).fetchone()
            self.connection.execute(
                "UPDATE proactive_decisions SET status=?, text=?, reason=?, updated_at=? "
                "WHERE user_id=? AND character_id=? AND opportunity=?",
                (status, text, reason, utc_now(), user_id, character_id, opportunity),
            )
            if row and row["follow_up_id"] and status in {"sent", "unknown"}:
                self.connection.execute(
                    "UPDATE follow_ups SET status=?, updated_at=? "
                    "WHERE id=? AND status='pending'",
                    ("asked" if status == "sent" else "uncertain", utc_now(),
                     row["follow_up_id"]),
                )

    def claim_delivery(self, user_id, character_id, opportunity, now, day_start, text, reason):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE proactive_decisions SET status='sending', text=?, reason=?, updated_at=? "
                "WHERE user_id=? AND character_id=? AND opportunity=? AND status='pending'",
                (text, reason, utc_now(), user_id, character_id, opportunity),
            ).rowcount
            if changed != 1:
                return False
            row = self.connection.execute(
                "SELECT follow_up_id FROM proactive_decisions "
                "WHERE user_id=? AND character_id=? AND opportunity=?",
                (user_id, character_id, opportunity),
            ).fetchone()
            allowed = (self.proactive_enabled(user_id, character_id) and
                       self.get_contact_pause(user_id, character_id, now) is None and
                       self.proactive_sent_since(user_id, character_id, day_start) == 1 and
                       (row["follow_up_id"] is None or
                        self.follow_up_pending(row["follow_up_id"], now)))
            if not allowed:
                self.connection.execute(
                    "UPDATE proactive_decisions SET status='skipped', reason='boundary_changed' "
                    "WHERE user_id=? AND character_id=? AND opportunity=?",
                    (user_id, character_id, opportunity),
                )
            return allowed

    def recent_proactive(self, user_id, character_id):
        return self.connection.execute(
            "SELECT * FROM proactive_decisions WHERE user_id=? AND character_id=? "
            "AND status='sent' ORDER BY created_at DESC LIMIT 1",
            (user_id, character_id),
        ).fetchone()

    def proactive_sent_since(self, user_id, character_id, since):
        return self.connection.execute(
            "SELECT COUNT(*) FROM proactive_decisions WHERE user_id=? AND character_id=? "
            "AND status IN ('sending','sent','unknown') AND created_at>=?",
            (user_id, character_id, since),
        ).fetchone()[0]

    def recent_completed_messages(self, user_id, character_id, limit):
        return self.connection.execute(
            "SELECT u.text AS user_text, u.occurred_at AS user_at, a.text AS assistant_text "
            "FROM turns t JOIN messages u ON u.turn_id=t.id AND u.role='user' "
            "JOIN messages a ON a.turn_id=t.id AND a.role='assistant' "
            "WHERE t.user_id=? AND t.character_id=? AND t.status='completed' "
            "ORDER BY t.sequence DESC LIMIT ?",
            (user_id, character_id, limit),
        ).fetchall()

    def add_follow_up(self, turn, topic, due_at, expires_at):
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO follow_ups VALUES (?,?,?,?,?,?,?,'pending',?,?) "
                "ON CONFLICT(source_turn_id, topic) DO NOTHING",
                (str(uuid4()), turn["user_id"], turn["character_id"], turn["id"],
                 topic, due_at, expires_at, now, now),
            )
            return self.connection.execute(
                "SELECT * FROM follow_ups WHERE source_turn_id=? AND topic=?",
                (turn["id"], topic),
            ).fetchone()

    def list_follow_ups(self, user_id, character_id):
        return self.connection.execute(
            "SELECT * FROM follow_ups WHERE user_id=? AND character_id=? "
            "AND status='pending' AND expires_at>? ORDER BY due_at",
            (user_id, character_id, utc_now()),
        ).fetchall()

    def close_follow_up(self, user_id, character_id, follow_up_id, status):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE follow_ups SET status=?, updated_at=? "
                "WHERE id=? AND user_id=? AND character_id=? AND status='pending'",
                (status, utc_now(), follow_up_id, user_id, character_id),
            ).rowcount
            if changed != 1:
                raise MiraError("follow_up_not_found")

    def due_follow_up(self, user_id, character_id, now):
        timestamp = now.astimezone(timezone.utc).isoformat()
        return self.connection.execute(
            "SELECT f.*, m.text AS source_text FROM follow_ups f "
            "JOIN turns t ON t.id=f.source_turn_id AND t.status='completed' "
            "JOIN messages m ON m.turn_id=f.source_turn_id AND m.role='user' "
            "WHERE f.user_id=? AND f.character_id=? AND f.status='pending' "
            "AND f.due_at<=? AND f.expires_at>? ORDER BY f.due_at LIMIT 1",
            (user_id, character_id, timestamp, timestamp),
        ).fetchone()

    def follow_up_pending(self, follow_up_id, now):
        timestamp = now.astimezone(timezone.utc).isoformat()
        return self.connection.execute(
            "SELECT 1 FROM follow_ups WHERE id=? AND status='pending' "
            "AND due_at<=? AND expires_at>?",
            (follow_up_id, timestamp, timestamp),
        ).fetchone() is not None

    def history_before(self, turn, limit):
        if limit == 0:
            return []
        rows = self.connection.execute(
            "SELECT u.text AS user_text, u.occurred_at AS user_occurred_at, "
            "a.text AS assistant_text FROM turns t "
            "JOIN messages u ON u.turn_id=t.id AND u.role='user' "
            "JOIN messages a ON a.turn_id=t.id AND a.role='assistant' "
            "WHERE t.user_id=? AND t.character_id=? AND t.sequence<? "
            "AND t.status='completed' ORDER BY t.sequence DESC LIMIT ?",
            (turn["user_id"], turn["character_id"], turn["sequence"], limit),
        ).fetchall()
        return [(row["user_text"], row["assistant_text"], row["user_occurred_at"])
                for row in reversed(rows)]

    def user_messages_before(self, turn, limit):
        scope = (turn["user_id"], turn["character_id"], turn["sequence"])
        total = self.connection.execute(
            "SELECT COUNT(*) FROM turns WHERE user_id=? AND character_id=? AND sequence<?",
            scope,
        ).fetchone()[0]
        rows = self.connection.execute(
            "SELECT m.text, m.occurred_at FROM turns t "
            "JOIN messages m ON m.turn_id=t.id AND m.role='user' "
            "WHERE t.user_id=? AND t.character_id=? AND t.sequence<? "
            "ORDER BY t.sequence DESC LIMIT ?", (*scope, limit),
        ).fetchall()
        return total, list(reversed(rows))

    def begin_attempt(self, turn_id, provider, model):
        with self.connection:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 FROM model_calls WHERE turn_id=?", (turn_id,)
            ).fetchone()
            attempt = row[0]
            self.connection.execute(
                "INSERT INTO model_calls VALUES (?,?,?,?,?,'pending',NULL,NULL,NULL,?)",
                (str(uuid4()), turn_id, attempt, provider, model, utc_now()),
            )
            return attempt

    def complete_attempt(self, turn_id, attempt, input_tokens=None, output_tokens=None):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE model_calls SET status='completed', input_tokens=?, output_tokens=? "
                "WHERE turn_id=? AND attempt=? AND status='pending'",
                (input_tokens, output_tokens, turn_id, attempt),
            ).rowcount
            if changed != 1:
                raise MiraError("invalid_call_state")

    def finish(self, turn_id, status, provider, model, attempt, text=None,
               error_code=None, input_tokens=None, output_tokens=None):
        now = utc_now()
        with self.connection:
            if text is not None:
                self.connection.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                    (str(uuid4()), turn_id, "assistant", text, now, now),
                )
            changed = self.connection.execute(
                "UPDATE turns SET status=?, error_code=?, updated_at=? "
                "WHERE id=? AND status='pending'", (status, error_code, now, turn_id)
            ).rowcount
            if changed != 1:
                raise MiraError("invalid_turn_state")
            changed = self.connection.execute(
                "UPDATE model_calls SET status=?, input_tokens=?, output_tokens=?, error_code=? "
                "WHERE turn_id=? AND attempt=? AND status='pending'",
                (status, input_tokens, output_tokens, error_code, turn_id, attempt),
            ).rowcount
            if changed != 1:
                raise MiraError("invalid_call_state")
            if status == "failed":
                self.connection.execute(
                    "DELETE FROM follow_ups WHERE source_turn_id=? AND status='pending'",
                    (turn_id,),
                )

    def mark_failed(self, turn_id, error_code):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE turns SET status='failed', error_code=?, updated_at=? "
                "WHERE id=? AND status='pending'", (error_code, utc_now(), turn_id)
            ).rowcount
            if changed != 1:
                raise MiraError("invalid_turn_state")
            self.connection.execute(
                "DELETE FROM follow_ups WHERE source_turn_id=? AND status='pending'",
                (turn_id,),
            )

    def start_retry(self, turn_id, user_id, character_id):
        with self.connection:
            latest = self.latest_turn(user_id, character_id)
            if not latest or latest["id"] != turn_id or latest["status"] != "failed":
                raise MiraError("retry_not_allowed")
            self.connection.execute(
                "UPDATE turns SET status='pending', error_code=NULL, updated_at=? WHERE id=?",
                (utc_now(), turn_id),
            )
            return self.get_turn(turn_id)
