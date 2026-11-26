import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mira.config import Settings
from mira.conversation import ConversationService
from mira.memory import MemoryService
from mira.models import MessageInput
from mira.storage import ConversationRepository


class MemoryProvider:
    def __init__(self):
        self.responses = []
        self.calls = []

    def generate(self, messages, tools=None):
        self.calls.append((messages, tools))
        if tools is not None:
            return {"role": "assistant", "content": "收到"}, 1, 1
        return {"role": "assistant", "content": json.dumps(
            self.responses.pop(0), ensure_ascii=False
        )}, 1, 1


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        character_path = root / "character.json"
        character_path.write_text(json.dumps({
            "character_id": "mira", "display_name": "Mira",
            "personality": "温柔", "speaking_style": "自然",
            "background": "虚构角色", "boundaries": "尊重用户",
        }, ensure_ascii=False), encoding="utf-8")
        self.settings = Settings("https://example.test/v1", "test", "fake",
                                 str(root / "memory.sqlite3"), str(character_path))
        self.repository = ConversationRepository(self.settings.database_path)
        self.provider = MemoryProvider()
        self.conversation = ConversationService(self.settings, self.repository, self.provider)
        self.local_now = datetime.now().astimezone().replace(
            hour=2, minute=30, second=0, microsecond=0
        ) + timedelta(days=1)
        self.memory = MemoryService(self.settings, self.repository, self.provider,
                                    clock=lambda: self.local_now)

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def add_turn(self, key, text):
        return self.conversation.handle_message(MessageInput(
            "user", "mira", "test", key, text,
            datetime.now(timezone.utc).isoformat(),
        ))

    def proposal(self, turn_id, evidence, kind="important_event"):
        return {"items": [{
            "turn_id": turn_id, "kind": kind, "claim_mode": "asserted",
            "subject": "user", "content": evidence, "evidence": evidence,
            "fact_key": "exam" if kind == "important_event" else None,
            "topics": ["面试"], "sensitivity": "ordinary",
        }], "handoff": "面试结果待聊"}

    def test_idle_extract_restart_consolidate_and_recall(self):
        turn = self.add_turn("1", "明天有重要面试")
        self.provider.responses.append(self.proposal(turn.turn_id, "明天有重要面试"))
        self.assertEqual(self.memory.run_due(), 1)
        self.assertEqual(self.memory.run_due(), 0)
        item = self.memory.list_memories("user", "mira")[0]
        self.assertEqual(self.memory.source("user", "mira", item["id"])[0]["text"],
                         "明天有重要面试")
        self.assertEqual(self.memory.handoff("user", "mira"), "面试结果待聊")
        self.repository.close()
        self.repository = ConversationRepository(self.settings.database_path)
        self.memory = MemoryService(self.settings, self.repository, self.provider,
                                    clock=lambda: self.local_now)
        self.local_now += timedelta(hours=1)
        self.provider.responses.append({
            "gist": "用户有一次重要面试", "cue": "面试", "links": [],
        })
        self.assertEqual(self.memory.run_due(), 0)
        self.assertEqual(self.memory.list_memories("user", "mira")[0]["content"],
                         "用户有一次重要面试")
        self.assertEqual([
            row["stage"] for row in self.repository.connection.execute(
                "SELECT stage FROM memory_model_calls ORDER BY created_at, rowid"
            )
        ], ["extract", "consolidate"])
        self.assertEqual(self.memory.recall("user", "mira", "面试怎么样了")[0]["id"],
                         item["id"])
        self.assertEqual(self.memory.recall("other", "mira", "面试怎么样了"), [])

    def test_invalid_evidence_retries_without_memory(self):
        turn = self.add_turn("1", "普通聊天")
        self.provider.responses.append(self.proposal(turn.turn_id, "不存在的原话"))
        self.assertEqual(self.memory.run_due(), 0)
        self.assertEqual(self.memory.list_memories("user", "mira"), [])
        job = self.repository.connection.execute(
            "SELECT status,attempts FROM memory_jobs WHERE turn_id=?", (turn.turn_id,)
        ).fetchone()
        self.assertEqual((job["status"], job["attempts"]), ("pending", 1))

    def test_forget_suppresses_history_and_follow_up(self):
        turn = self.add_turn("1", "记住我的面试在下午三点")
        source_turn = self.repository.get_turn(turn.turn_id)
        memory_id = self.memory.remember(source_turn, "我的面试在下午三点", "plan")
        self.assertTrue(self.memory.list_memories("user", "mira"))
        self.add_turn("recall", "面试在下午三点吗")
        current = self.add_turn("2", "忘记那次面试")
        self.memory.forget("user", "mira", memory_id, current.turn_id)
        self.assertEqual(self.memory.list_memories("user", "mira"), [])
        self.assertEqual(self.repository.user_messages_before(
            self.repository.get_turn(current.turn_id), 20
        )[0], 0)
        self.assertEqual(self.repository.recent_completed_messages(
            "user", "mira", 5, for_context=True
        ), [])

    def test_conflicting_fact_is_not_recalled_as_current(self):
        first = self.add_turn("1", "面试在下午三点")
        second = self.add_turn("2", "面试在下午四点")
        for result, text in ((first, "面试在下午三点"), (second, "面试在下午四点")):
            row = self.repository.connection.execute(
                "SELECT t.id AS turn_id,t.user_id,t.character_id,m.text,m.occurred_at "
                "FROM turns t JOIN messages m ON m.turn_id=t.id AND m.role='user' "
                "WHERE t.id=?", (result.turn_id,)
            ).fetchone()
            self.memory.repository.save_batch([row], [{
                "turn_id": result.turn_id, "kind": "important_event",
                "claim_mode": "asserted", "status": "active", "subject": "user",
                "content": text, "evidence": text, "fact_key": "interview_time",
                "topics": ["面试"], "sensitivity": "ordinary", "importance": 0.75,
            }])
        self.assertEqual(self.memory.recall("user", "mira", "面试时间"), [])
        statuses = {row[0] for row in self.repository.connection.execute(
            "SELECT status FROM memory_items"
        )}
        self.assertEqual(statuses, {"disputed"})
        self.assertEqual(len(self.memory.list_memories("user", "mira")), 2)
        repeated = self.add_turn("3", "面试在下午三点")
        repeated_row = self.repository.connection.execute(
            "SELECT t.id AS turn_id,t.user_id,t.character_id,m.text,m.occurred_at "
            "FROM turns t JOIN messages m ON m.turn_id=t.id AND m.role='user' "
            "WHERE t.id=?", (repeated.turn_id,)
        ).fetchone()
        self.memory.repository.save_batch([repeated_row], [{
            "turn_id": repeated.turn_id, "kind": "important_event",
            "claim_mode": "asserted", "status": "active", "subject": "user",
            "content": "面试在下午三点", "evidence": "面试在下午三点",
            "fact_key": "interview_time", "topics": ["面试"],
            "sensitivity": "ordinary", "importance": 0.75,
        }])
        self.assertEqual(self.memory.recall("user", "mira", "面试时间"), [])
        self.assertEqual(self.repository.connection.execute(
            "SELECT COUNT(*) FROM memory_items"
        ).fetchone()[0], 2)

    def test_same_fact_repeated_on_another_day_gains_bounded_importance(self):
        for day in range(4):
            result = self.conversation.handle_message(MessageInput(
                "user", "mira", "test", str(day), "我在准备面试",
                (datetime.now(timezone.utc) - timedelta(days=4 - day)).isoformat(),
            ))
            row = self.repository.connection.execute(
                "SELECT t.id AS turn_id,t.user_id,t.character_id,m.text,m.occurred_at "
                "FROM turns t JOIN messages m ON m.turn_id=t.id AND m.role='user' "
                "WHERE t.id=?", (result.turn_id,)
            ).fetchone()
            self.memory.repository.save_batch([row], [{
                "turn_id": result.turn_id, "kind": "interest",
                "claim_mode": "asserted", "status": "active", "subject": "user",
                "content": "我在准备面试", "evidence": "我在准备面试",
                "fact_key": "exam_preparation", "topics": ["面试"],
                "sensitivity": "ordinary", "importance": 0.35,
            }])
        items = self.repository.connection.execute(
            "SELECT importance FROM memory_items"
        ).fetchall()
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0]["importance"], 0.55)

    def test_explicit_remember_is_idempotent(self):
        turn = self.add_turn("1", "请记住我喜欢蓝色")
        row = self.repository.get_turn(turn.turn_id)
        first = self.memory.remember(row, "我喜欢蓝色", "interest")
        self.assertEqual(self.memory.remember(row, "我喜欢蓝色", "interest"), first)
        self.assertEqual(self.repository.connection.execute(
            "SELECT COUNT(*) FROM memory_items"
        ).fetchone()[0], 1)
        self.assertEqual(self.memory.recall("user", "mira", "蓝色呢")[0]["id"], first)
        self.add_turn("2", "蓝色呢")
        self.assertIn("我喜欢蓝色", self.provider.calls[-1][0][0]["content"])

    def test_correction_replaces_old_view_and_source(self):
        old_turn = self.add_turn("1", "我喜欢蓝色")
        old_id = self.memory.remember(
            self.repository.get_turn(old_turn.turn_id), "我喜欢蓝色", "interest"
        )
        correction = self.add_turn("2", "更正，我喜欢绿色")
        new_id = self.memory.correct(
            "user", "mira", old_id, correction.turn_id, "我喜欢绿色"
        )
        self.assertEqual([item["id"] for item in self.memory.list_memories(
            "user", "mira"
        )], [new_id])
        self.assertEqual(self.memory.source("user", "mira", new_id)[0]["text"],
                         "更正，我喜欢绿色")
        self.assertEqual(self.repository.user_messages_before(
            self.repository.get_turn(correction.turn_id), 20
        )[0], 0)

    def test_old_daily_fragment_fades_but_important_event_remains_recallable(self):
        self.local_now = datetime.now().astimezone()
        for key, content, days, kind, importance in (
            ("daily", "今天买了面包", 7, "daily_fragment", 0.1),
            ("event", "我那次重要面试", 180, "important_event", 0.75),
        ):
            result = self.conversation.handle_message(MessageInput(
                "user", "mira", "test", key, content,
                (self.local_now - timedelta(days=days)).isoformat(),
            ))
            row = self.repository.connection.execute(
                "SELECT t.id AS turn_id,t.user_id,t.character_id,m.text,m.occurred_at "
                "FROM turns t JOIN messages m ON m.turn_id=t.id AND m.role='user' "
                "WHERE t.id=?", (result.turn_id,)
            ).fetchone()
            self.memory.repository.save_batch([row], [{
                "turn_id": result.turn_id, "kind": kind,
                "claim_mode": "asserted", "status": "active", "subject": "user",
                "content": content, "evidence": content, "fact_key": None,
                "topics": [], "sensitivity": "ordinary", "importance": importance,
            }])
        self.assertEqual(self.memory.recall("user", "mira", "今天买了面包"), [])
        self.assertEqual(len(self.memory.recall(
            "user", "mira", "还记得我那次重要面试吗"
        )), 1)

    def test_closing_follow_up_resolves_the_single_source_plan(self):
        turn = self.add_turn("1", "明天要去面试")
        source = self.repository.get_turn(turn.turn_id)
        memory_id = self.memory.remember(source, "明天要去面试", "plan")
        now = datetime.now(timezone.utc)
        follow_up = self.repository.add_follow_up(
            source, "面试", (now + timedelta(hours=1)).isoformat(),
            (now + timedelta(days=1)).isoformat(),
        )
        self.repository.close_follow_up("user", "mira", follow_up["id"], "resolved")
        item = self.memory.repository.get_item("user", "mira", memory_id)
        self.assertEqual(item["status"], "resolved")

    def test_conversation_memory_tools_use_current_turn_evidence(self):
        turn = self.add_turn("1", "请记住我喜欢蓝色")
        row = self.repository.get_turn(turn.turn_id)
        call = {"function": {"name": "remember_memory", "arguments": json.dumps({
            "content": "我喜欢蓝色", "kind": "interest",
        }, ensure_ascii=False)}}
        result = self.conversation._execute_tool(row, call)
        self.assertTrue(result["saved"])
        self.assertEqual(self.conversation._execute_tool(row, {"function": {
            "name": "get_memory_source", "arguments": json.dumps({"id": result["id"]}),
        }})["sources"][0]["text"], "请记住我喜欢蓝色")
        invalid = self.conversation._execute_tool(row, {"function": {
            "name": "remember_memory", "arguments": json.dumps({
                "content": "我喜欢红色", "kind": "interest",
            }, ensure_ascii=False),
        }})
        self.assertEqual(invalid, {"error": "invalid_memory_evidence"})


if __name__ == "__main__":
    unittest.main()
