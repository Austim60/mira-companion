"""SQLite records for source-backed conversation memory."""

import json
from datetime import datetime, timezone
from uuid import uuid4

from .models import MiraError


def migrate_memory(connection):
    """Add memory records without changing existing conversation rows."""
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS memory_items (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL REFERENCES characters(character_id),
            kind TEXT NOT NULL,
            claim_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            subject TEXT NOT NULL,
            content TEXT NOT NULL,
            fact_key TEXT,
            topics TEXT NOT NULL,
            sensitivity TEXT NOT NULL,
            importance REAL NOT NULL,
            occurred_at TEXT NOT NULL,
            last_user_evidence_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            consolidated_version INTEGER NOT NULL DEFAULT 0,
            consolidation_retry_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS memory_scope_status
            ON memory_items(user_id, character_id, status);
        CREATE TABLE IF NOT EXISTS memory_sources (
            memory_id TEXT NOT NULL REFERENCES memory_items(id),
            turn_id TEXT NOT NULL REFERENCES turns(id),
            evidence TEXT NOT NULL,
            PRIMARY KEY(memory_id, turn_id)
        );
        CREATE TABLE IF NOT EXISTS memory_edges (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            from_id TEXT NOT NULL REFERENCES memory_items(id),
            to_id TEXT NOT NULL REFERENCES memory_items(id),
            relation TEXT NOT NULL,
            source_turn_id TEXT NOT NULL REFERENCES turns(id),
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE(from_id, to_id, relation)
        );
        CREATE TABLE IF NOT EXISTS memory_views (
            memory_id TEXT NOT NULL REFERENCES memory_items(id),
            level TEXT NOT NULL,
            text TEXT NOT NULL,
            source_version INTEGER NOT NULL,
            generated_at TEXT NOT NULL,
            PRIMARY KEY(memory_id, level)
        );
        CREATE TABLE IF NOT EXISTS memory_jobs (
            turn_id TEXT PRIMARY KEY REFERENCES turns(id),
            status TEXT NOT NULL DEFAULT 'pending',
            queued_at TEXT NOT NULL,
            retry_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            error_code TEXT
        );
        CREATE TABLE IF NOT EXISTS memory_suppressions (
            turn_id TEXT PRIMARY KEY REFERENCES turns(id),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_handoffs (
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            source_turn_id TEXT NOT NULL REFERENCES turns(id),
            text TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY(user_id, character_id)
        );
        CREATE TABLE IF NOT EXISTS memory_recall_logs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            query_turn_id TEXT,
            memory_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            level TEXT,
            score REAL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_model_calls (
            id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            status TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            duration_ms INTEGER NOT NULL,
            error_code TEXT,
            created_at TEXT NOT NULL
        );
    """)
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations VALUES (7, ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    if "category" not in {row[1] for row in connection.execute("PRAGMA table_info(memory_items)")}:
        connection.execute("ALTER TABLE memory_items ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations VALUES (8, ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    connection.execute(
        "INSERT OR IGNORE INTO memory_jobs (turn_id,queued_at) "
        "SELECT id,updated_at FROM turns WHERE status='completed'"
    )


class MemoryRepository:
    """Keep memory changes and their source links in scoped transactions."""

    def __init__(self, conversation_repository):
        self.connection = conversation_repository.connection

    def log_model_call(self, stage, user_id, character_id, source_id, status,
                       input_tokens, output_tokens, duration_ms, error_code=None):
        with self.connection:
            self.connection.execute(
                "INSERT INTO memory_model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid4()), stage, user_id, character_id, source_id, status,
                 input_tokens, output_tokens, duration_ms, error_code,
                 datetime.now(timezone.utc).isoformat()),
            )

    def pending_turns(self, cutoff, now, limit=5):
        return self.connection.execute(
            "SELECT j.turn_id, t.user_id, t.character_id, t.sequence, "
            "m.text, m.occurred_at FROM memory_jobs j "
            "JOIN turns t ON t.id=j.turn_id AND t.status='completed' "
            "JOIN messages m ON m.turn_id=t.id AND m.role='user' "
            "WHERE j.status='pending' AND j.queued_at<=? "
            "AND (j.retry_at IS NULL OR j.retry_at<=?) "
            "AND NOT EXISTS (SELECT 1 FROM memory_suppressions s WHERE s.turn_id=t.id) "
            "ORDER BY t.sequence LIMIT ?",
            (cutoff, now, limit),
        ).fetchall()

    def recent_items(self, user_id, character_id, limit=20):
        return self.connection.execute(
            "SELECT * FROM memory_items WHERE user_id=? AND character_id=? "
            "AND status IN ('active','open','resolved','disputed') "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, character_id, limit),
        ).fetchall()

    def latest_completed_at(self, user_id, character_id):
        row = self.connection.execute(
            "SELECT MAX(updated_at) FROM turns WHERE user_id=? AND character_id=? "
            "AND status='completed' AND source<>'admin'",
            (user_id, character_id),
        ).fetchone()
        return row[0]

    def save_batch(self, turns, proposals, handoff=None):
        now = datetime.now(timezone.utc).isoformat()
        scope = (turns[0]["user_id"], turns[0]["character_id"])
        turn_by_id = {row["turn_id"]: row for row in turns}
        with self.connection:
            for item in proposals:
                source = turn_by_id[item["turn_id"]]
                duplicate = self.connection.execute(
                    "SELECT m.id FROM memory_items m JOIN memory_sources s "
                    "ON s.memory_id=m.id WHERE s.turn_id=? AND m.content=? LIMIT 1",
                    (source["turn_id"], item["content"]),
                ).fetchone()
                if duplicate:
                    continue
                existing = None
                if item["fact_key"]:
                    existing = self.connection.execute(
                        "SELECT * FROM memory_items WHERE user_id=? AND character_id=? "
                        "AND kind=? AND fact_key=? AND content=? "
                        "AND status IN ('active','open','resolved','disputed') LIMIT 1",
                        (*scope, item["kind"], item["fact_key"], item["content"]),
                    ).fetchone()
                if existing:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO memory_sources VALUES (?,?,?)",
                        (existing["id"], source["turn_id"], item["evidence"]),
                    )
                    evidence_dates = {
                        datetime.fromisoformat(row[0]).astimezone().date()
                        for row in self.connection.execute(
                        "SELECT m.occurred_at "
                        "FROM memory_sources s JOIN messages m "
                        "ON m.turn_id=s.turn_id AND m.role='user' "
                        "WHERE s.memory_id=?",
                        (existing["id"],),
                    )}
                    importance = min(1.0, item["importance"] +
                                     min(0.2, 0.1 * max(0, len(evidence_dates) - 1)))
                    self.connection.execute(
                        "UPDATE memory_items SET importance=MAX(importance,?), "
                        "last_user_evidence_at=?, version=version+1, updated_at=? WHERE id=?",
                        (importance, source["occurred_at"], now, existing["id"]),
                    )
                    continue
                if item["fact_key"] and item["status"] in {"active", "open"}:
                    conflicts = self.connection.execute(
                        "SELECT id FROM memory_items WHERE user_id=? AND character_id=? "
                        "AND kind=? AND fact_key=? AND content<>? "
                        "AND status IN ('active','open','resolved','disputed')",
                        (*scope, item["kind"], item["fact_key"], item["content"]),
                    ).fetchall()
                    if conflicts:
                        item = {**item, "status": "disputed"}
                        self.connection.executemany(
                            "UPDATE memory_items SET status='disputed',version=version+1, "
                            "updated_at=? WHERE id=?",
                            [(now, row["id"]) for row in conflicts],
                        )
                memory_id = str(uuid4())
                self.connection.execute(
                    "INSERT INTO memory_items "
                    "(id,user_id,character_id,kind,claim_mode,status,subject,content,"
                    "fact_key,topics,sensitivity,importance,occurred_at,"
                    "last_user_evidence_at,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (memory_id, *scope, item["kind"], item["claim_mode"],
                     item["status"], item["subject"], item["content"],
                     item["fact_key"], json.dumps(item["topics"], ensure_ascii=False),
                     item["sensitivity"], item["importance"], source["occurred_at"],
                     source["occurred_at"], now, now),
                )
                self.connection.execute(
                    "INSERT INTO memory_sources VALUES (?,?,?)",
                    (memory_id, source["turn_id"], item["evidence"]),
                )
            if handoff:
                latest = turns[-1]
                self.connection.execute(
                    "INSERT INTO memory_handoffs VALUES (?,?,?,?,?) "
                    "ON CONFLICT(user_id,character_id) DO UPDATE SET "
                    "source_turn_id=excluded.source_turn_id, text=excluded.text, "
                    "expires_at=excluded.expires_at",
                    (*scope, latest["turn_id"], handoff["text"], handoff["expires_at"]),
                )
            self.connection.executemany(
                "UPDATE memory_jobs SET status='completed', error_code=NULL WHERE turn_id=?",
                [(row["turn_id"],) for row in turns],
            )

    def save_explicit(self, turn, content, kind):
        """Save a direct user request in the active turn before the reply."""
        source = self.connection.execute(
            "SELECT text,occurred_at FROM messages WHERE turn_id=? AND role='user'",
            (turn["id"],),
        ).fetchone()
        if source is None or content not in source["text"]:
            raise MiraError("invalid_memory_evidence")
        row = {"turn_id": turn["id"], "user_id": turn["user_id"],
               "character_id": turn["character_id"], "text": source["text"],
               "occurred_at": source["occurred_at"]}
        self.save_batch([row], [{
            "turn_id": turn["id"], "kind": kind, "claim_mode": "asserted",
            "status": "open" if kind == "plan" else "active",
            "subject": "user", "content": content,
            "evidence": content, "fact_key": None, "topics": [],
            "sensitivity": "ordinary", "importance": 0.8,
        }])
        result = self.connection.execute(
            "SELECT id FROM memory_items WHERE user_id=? AND character_id=? "
            "AND content=? ORDER BY created_at DESC LIMIT 1",
            (turn["user_id"], turn["character_id"], content),
        ).fetchone()
        return result["id"]

    def fail_batch(self, turns, code, retry_at):
        with self.connection:
            self.connection.executemany(
                "UPDATE memory_jobs SET attempts=attempts+1, error_code=?, retry_at=? "
                "WHERE turn_id=? AND status='pending'",
                [(code, retry_at, row["turn_id"]) for row in turns],
            )

    def items_for_consolidation(self, now, limit=4):
        return self.connection.execute(
            "SELECT * FROM memory_items WHERE version>consolidated_version "
            "AND status IN ('active','open','resolved','candidate') "
            "AND (consolidation_retry_at IS NULL OR consolidation_retry_at<=?) "
            "ORDER BY updated_at, id LIMIT ?",
            (now, limit),
        ).fetchall()

    def delay_consolidation(self, memory_id, retry_at):
        with self.connection:
            self.connection.execute(
                "UPDATE memory_items SET consolidation_retry_at=? WHERE id=?",
                (retry_at, memory_id),
            )

    def consolidate(self, item, gist, cue, links):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            current = self.connection.execute(
                "SELECT version,status FROM memory_items WHERE id=?", (item["id"],)
            ).fetchone()
            if current is None or current["version"] != item["version"] or current["status"] == "forgotten":
                return False
            for level, text in (("detail", item["content"]), ("gist", gist), ("cue", cue)):
                self.connection.execute(
                    "INSERT INTO memory_views VALUES (?,?,?,?,?) "
                    "ON CONFLICT(memory_id,level) DO UPDATE SET text=excluded.text, "
                    "source_version=excluded.source_version, generated_at=excluded.generated_at",
                    (item["id"], level, text, item["version"], now),
                )
            for link in links:
                target = self.get_item(item["user_id"], item["character_id"], link["to_id"])
                if target and target["status"] in {"active", "open", "resolved"}:
                    source = self.connection.execute(
                        "SELECT turn_id FROM memory_sources WHERE memory_id=? LIMIT 1",
                        (item["id"],),
                    ).fetchone()
                    if source:
                        self.connection.execute(
                            "INSERT OR IGNORE INTO memory_edges VALUES (?,?,?,?,?,?,?,1)",
                            (str(uuid4()), item["user_id"], item["character_id"],
                             item["id"], target["id"], link["relation"], source["turn_id"]),
                        )
            self.connection.execute(
                "UPDATE memory_items SET consolidated_version=version,"
                "consolidation_retry_at=NULL WHERE id=?",
                (item["id"],),
            )
            return True

    def get_item(self, user_id, character_id, memory_id):
        return self.connection.execute(
            "SELECT * FROM memory_items WHERE id=? AND user_id=? AND character_id=?",
            (memory_id, user_id, character_id),
        ).fetchone()

    def list_for_admin(self, user_id, character_id):
        """Include candidates and disputes in the dashboard review queue."""
        return self.connection.execute(
            "SELECT m.* FROM memory_items m WHERE m.user_id=? AND m.character_id=? "
            "AND m.status IN ('active','open','resolved','candidate','disputed') "
            "AND EXISTS (SELECT 1 FROM memory_sources s WHERE s.memory_id=m.id "
            "AND NOT EXISTS (SELECT 1 FROM memory_suppressions x WHERE x.turn_id=s.turn_id)) "
            "ORDER BY m.updated_at DESC, m.id", (user_id, character_id),
        ).fetchall()

    def organize(self, user_id, character_id, memory_id, category, kind, confirm=False):
        """Apply a reviewed category or type and optionally confirm a factual candidate."""
        from .memory import KINDS
        category = category.strip()
        if len(category) > 40 or kind not in KINDS:
            raise MiraError("invalid_memory_organization")
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            item = self.get_item(user_id, character_id, memory_id)
            if item is None or item["status"] not in {"active", "open", "resolved", "candidate", "disputed"}:
                raise MiraError("memory_not_found")
            if confirm and (item["status"] not in {"candidate", "disputed"} or
                            item["claim_mode"] != "asserted" and
                            not (item["claim_mode"] == "planned" and kind == "plan")):
                raise MiraError("invalid_memory_confirmation")
            status = ("open" if kind == "plan" and item["claim_mode"] == "planned"
                      else "active") if confirm else item["status"]
            changed = kind != item["kind"] or status != item["status"]
            self.connection.execute(
                "UPDATE memory_items SET category=?,kind=?,status=?,importance=?,"
                "version=version+?,updated_at=? WHERE id=?",
                (category, kind, status,
                 KINDS[kind][0] if kind != item["kind"] else item["importance"],
                 int(changed), now, memory_id),
            )
            if changed:
                self.connection.execute("DELETE FROM memory_views WHERE memory_id=?", (memory_id,))

    def merge(self, user_id, character_id, target_id, source_id):
        """Join source links of two reviewed duplicates and retire the second item."""
        if target_id == source_id:
            raise MiraError("invalid_memory_merge")
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            target = self.get_item(user_id, character_id, target_id)
            source = self.get_item(user_id, character_id, source_id)
            allowed = {"active", "open", "resolved"}
            if target is None or source is None or target["status"] not in allowed or source["status"] not in allowed:
                raise MiraError("memory_not_found")
            if target["kind"] != source["kind"] or target["claim_mode"] != source["claim_mode"]:
                raise MiraError("invalid_memory_merge")
            self.connection.execute(
                "INSERT OR IGNORE INTO memory_sources (memory_id,turn_id,evidence) "
                "SELECT ?,s.turn_id,s.evidence FROM memory_sources s "
                "WHERE s.memory_id=? AND NOT EXISTS "
                "(SELECT 1 FROM memory_suppressions x WHERE x.turn_id=s.turn_id)",
                (target_id, source_id),
            )
            self.connection.execute(
                "UPDATE memory_items SET version=version+1,updated_at=? WHERE id=?",
                (now, target_id),
            )
            self.connection.execute(
                "UPDATE memory_items SET status='superseded',version=version+1,updated_at=? WHERE id=?",
                (now, source_id),
            )
            self.connection.execute("DELETE FROM memory_views WHERE memory_id IN (?,?)",
                                    (target_id, source_id))
            self.connection.execute(
                "UPDATE memory_edges SET active=0 WHERE from_id=? OR to_id=?",
                (source_id, source_id),
            )

    def source_texts(self, user_id, character_id, memory_id):
        return self.connection.execute(
            "SELECT m.text,m.occurred_at FROM memory_sources s "
            "JOIN turns t ON t.id=s.turn_id "
            "JOIN messages m ON m.turn_id=t.id AND m.role='user' "
            "WHERE s.memory_id=? AND t.user_id=? AND t.character_id=? "
            "AND NOT EXISTS (SELECT 1 FROM memory_suppressions x WHERE x.turn_id=t.id) "
            "ORDER BY t.sequence",
            (memory_id, user_id, character_id),
        ).fetchall()

    def list_active(self, user_id, character_id, include_disputed=False):
        statuses = ("('active','open','resolved','disputed')" if include_disputed else
                    "('active','open','resolved')")
        return self.connection.execute(
            "SELECT m.*, v.text AS gist, v.source_version AS view_version "
            "FROM memory_items m LEFT JOIN memory_views v "
            "ON v.memory_id=m.id AND v.level='gist' "
            "WHERE m.user_id=? AND m.character_id=? "
            "AND m.status IN " + statuses + " "
            "AND EXISTS (SELECT 1 FROM memory_sources s WHERE s.memory_id=m.id "
            "AND NOT EXISTS (SELECT 1 FROM memory_suppressions x WHERE x.turn_id=s.turn_id)) "
            "ORDER BY m.updated_at DESC, m.id",
            (user_id, character_id),
        ).fetchall()

    def handoff(self, user_id, character_id, now):
        return self.connection.execute(
            "SELECT h.text FROM memory_handoffs h WHERE h.user_id=? AND h.character_id=? "
            "AND h.expires_at>? AND NOT EXISTS "
            "(SELECT 1 FROM memory_suppressions s WHERE s.turn_id=h.source_turn_id)",
            (user_id, character_id, now),
        ).fetchone()

    def forget(self, user_id, character_id, memory_id):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            item = self.get_item(user_id, character_id, memory_id)
            if item is None or item["status"] == "forgotten":
                raise MiraError("memory_not_found")
            source_ids = [row[0] for row in self.connection.execute(
                "SELECT turn_id FROM memory_sources WHERE memory_id=?", (memory_id,)
            )]
            self.connection.execute(
                "UPDATE memory_items SET status='forgotten',version=version+1,updated_at=? "
                "WHERE id=?", (now, memory_id),
            )
            self.connection.execute("DELETE FROM memory_views WHERE memory_id=?", (memory_id,))
            self.connection.execute(
                "UPDATE memory_edges SET active=0 WHERE from_id=? OR to_id=?",
                (memory_id, memory_id),
            )
            for turn_id in source_ids:
                self.connection.execute(
                    "INSERT OR IGNORE INTO memory_suppressions VALUES (?,?)",
                    (turn_id, now),
                )
                self.connection.execute(
                    "UPDATE follow_ups SET status='cancelled',updated_at=? "
                    "WHERE source_turn_id=? AND status='pending'",
                    (now, turn_id),
                )
            self.connection.execute(
                "INSERT OR IGNORE INTO memory_suppressions (turn_id,created_at) "
                "SELECT query_turn_id,? FROM memory_recall_logs "
                "WHERE memory_id=? AND outcome='injected' AND query_turn_id IS NOT NULL",
                (now, memory_id),
            )
            self.connection.execute(
                "UPDATE follow_ups SET status='cancelled',updated_at=? "
                "WHERE user_id=? AND character_id=? AND status='pending' "
                "AND source_turn_id IN (SELECT query_turn_id FROM memory_recall_logs "
                "WHERE memory_id=? AND outcome='injected')",
                (now, user_id, character_id, memory_id),
            )
            self.connection.execute(
                "DELETE FROM memory_handoffs WHERE user_id=? AND character_id=? "
                "AND source_turn_id IN "
                "(SELECT turn_id FROM memory_sources WHERE memory_id=?)",
                (user_id, character_id, memory_id),
            )
            return item

    def suppress_turn(self, turn_id):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO memory_suppressions VALUES (?,?)",
                (turn_id, now),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO memory_jobs (turn_id,status,queued_at) "
                "VALUES (?,'completed',?)",
                (turn_id, now),
            )

    def correct(self, user_id, character_id, memory_id, turn, content):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            old = self.get_item(user_id, character_id, memory_id)
            if old is None or old["status"] not in {"active", "open", "resolved", "disputed", "candidate"}:
                raise MiraError("memory_not_found")
            new_id = str(uuid4())
            self.connection.execute(
                "UPDATE memory_items SET status='superseded',version=version+1,updated_at=? "
                "WHERE id=?", (now, memory_id),
            )
            self.connection.execute("DELETE FROM memory_views WHERE memory_id=?", (memory_id,))
            self.connection.execute(
                "UPDATE memory_edges SET active=0 WHERE from_id=? OR to_id=?",
                (memory_id, memory_id),
            )
            self.connection.execute(
                "INSERT INTO memory_items "
                "(id,user_id,character_id,kind,claim_mode,status,subject,content,"
                "fact_key,topics,sensitivity,importance,occurred_at,"
                "last_user_evidence_at,created_at,updated_at,category) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, user_id, character_id, old["kind"],
                 "planned" if old["kind"] == "plan" else "asserted",
                 "open" if old["kind"] == "plan" and
                 old["status"] in {"open", "disputed"} else "active",
                 old["subject"], content, old["fact_key"], old["topics"],
                 old["sensitivity"], old["importance"], turn["occurred_at"],
                 turn["occurred_at"], now, now, old["category"]),
            )
            self.connection.execute(
                "INSERT INTO memory_sources VALUES (?,?,?)",
                (new_id, turn["turn_id"], content),
            )
            self.connection.execute(
                "INSERT INTO memory_edges VALUES (?,?,?,?,?,?,?,1)",
                (str(uuid4()), user_id, character_id, memory_id, new_id,
                 "updates", turn["turn_id"]),
            )
            self.connection.execute(
                "UPDATE follow_ups SET status='cancelled',updated_at=? "
                "WHERE source_turn_id IN "
                "(SELECT turn_id FROM memory_sources WHERE memory_id=?) "
                "AND status='pending'",
                (now, memory_id),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO memory_suppressions (turn_id,created_at) "
                "SELECT s.turn_id,? FROM memory_sources s WHERE s.memory_id=? "
                "AND NOT EXISTS (SELECT 1 FROM memory_sources other "
                "JOIN memory_items m ON m.id=other.memory_id "
                "WHERE other.turn_id=s.turn_id AND other.memory_id<>? "
                "AND m.status IN ('active','open','resolved','candidate','disputed'))",
                (now, memory_id, memory_id),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO memory_suppressions (turn_id,created_at) "
                "SELECT query_turn_id,? FROM memory_recall_logs "
                "WHERE memory_id=? AND outcome='injected' AND query_turn_id IS NOT NULL "
                "AND query_turn_id<>?",
                (now, memory_id, turn["turn_id"]),
            )
            self.connection.execute(
                "UPDATE follow_ups SET status='cancelled',updated_at=? "
                "WHERE user_id=? AND character_id=? AND status='pending' "
                "AND source_turn_id IN (SELECT query_turn_id FROM memory_recall_logs "
                "WHERE memory_id=? AND outcome='injected' AND query_turn_id<>?)",
                (now, user_id, character_id, memory_id, turn["turn_id"]),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO memory_jobs (turn_id,status,queued_at) "
                "VALUES (?,'completed',?)",
                (turn["turn_id"], now),
            )
            return new_id

    def correct_from_admin(self, user_id, character_id, memory_id, content):
        """Record a dashboard correction as provenance without a chat reply."""
        if not content or len(content) > 300:
            raise MiraError("invalid_memory_correction")
        old = self.get_item(user_id, character_id, memory_id)
        if old is None or old["status"] not in {"active", "open", "resolved", "disputed", "candidate"}:
            raise MiraError("memory_not_found")
        now = datetime.now(timezone.utc).isoformat()
        turn_id = str(uuid4())
        with self.connection:
            self.connection.execute(
                "INSERT INTO turns (id,user_id,character_id,source,source_message_id,"
                "status,created_at,updated_at) VALUES (?,?,?,?,?,'completed',?,?)",
                (turn_id, user_id, character_id, "admin", turn_id, now, now),
            )
            self.connection.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?)",
                (str(uuid4()), turn_id, "user", content, now, now),
            )
            self.connection.execute(
                "INSERT INTO memory_jobs (turn_id,status,queued_at) "
                "VALUES (?,'completed',?)",
                (turn_id, now),
            )
            return self.correct(user_id, character_id, memory_id,
                                {"turn_id": turn_id, "occurred_at": now}, content)

    def log_recall(self, user_id, character_id, query_turn_id, decisions):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.executemany(
                "INSERT INTO memory_recall_logs VALUES (?,?,?,?,?,?,?,?,?)",
                [(str(uuid4()), user_id, character_id, query_turn_id,
                  item["id"], item["outcome"], item["level"], item["score"], now)
                 for item in decisions],
            )
