"""Source-backed memory extraction, consolidation, and bounded recall."""

import json
from datetime import datetime, timedelta, timezone
from time import perf_counter

from .memory_storage import MemoryRepository
from .models import MiraError


KINDS = {
    "daily_fragment": (0.10, 2),
    "interest": (0.35, 30),
    "user_fact": (0.55, 180),
    "episode": (0.40, 14),
    "important_event": (0.75, 90),
    "shared_experience": (0.65, 90),
    "plan": (0.40, 14),
}
CLAIM_MODES = {"asserted", "planned", "hypothetical", "joke", "quote", "inferred"}
SENSITIVITIES = {"ordinary", "private", "highly_sensitive"}
RELATIONS = {"same_topic", "before", "caused_by", "updates", "shared_experience"}


class MemoryService:
    """Turn completed dialogue into scoped evidence and retrieve it on demand."""

    def __init__(self, settings, conversation_repository, provider, clock=None):
        self.settings = settings
        self.repository = MemoryRepository(conversation_repository)
        self.provider = provider
        self.clock = clock or (lambda: datetime.now().astimezone())

    @staticmethod
    def _json_reply(reply):
        try:
            data = json.loads(reply["content"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MiraError("invalid_memory_response") from exc
        if not isinstance(data, dict):
            raise MiraError("invalid_memory_response")
        return data

    def _model_reply(self, messages, stage, user_id, character_id, source_id):
        started = perf_counter()
        try:
            output_limit = max(2048, self.settings.max_output_tokens) if stage == "extract" else None
            answer, input_tokens, output_tokens = self.provider.generate(
                messages, max_output_tokens=output_limit
            )
        except MiraError as exc:
            self.repository.log_model_call(
                stage, user_id, character_id, source_id, "failed", None, None,
                int((perf_counter() - started) * 1000), exc.code,
            )
            raise
        self.repository.log_model_call(
            stage, user_id, character_id, source_id, "completed", input_tokens,
            output_tokens, int((perf_counter() - started) * 1000),
        )
        return answer

    def run_due(self):
        """Process idle dialogue and one changed memory after the nightly boundary."""
        now = self.clock().astimezone()
        cutoff = (now - timedelta(minutes=30)).astimezone(timezone.utc).isoformat()
        now_utc = now.astimezone(timezone.utc).isoformat()
        turns = self.repository.pending_turns(cutoff, now_utc)
        processed = 0
        if turns:
            scope = (turns[0]["user_id"], turns[0]["character_id"])
            turns = [row for row in turns if
                     (row["user_id"], row["character_id"]) == scope]
            latest = self.repository.latest_completed_at(*scope)
            if latest and latest <= cutoff:
                try:
                    proposals, handoff = self._extract(turns)
                    self.repository.save_batch(turns, proposals, handoff)
                    processed = len(turns)
                except (MiraError, ValueError, TypeError) as exc:
                    code = getattr(exc, "code", "invalid_memory_response")
                    retry = (now + timedelta(minutes=5)).astimezone(timezone.utc).isoformat()
                    self.repository.fail_batch(turns, code, retry)
        if now.hour >= 3:
            for item in self.repository.items_for_consolidation(now_utc, limit=1):
                try:
                    gist, cue, links = self._consolidate(item)
                    self.repository.consolidate(item, gist, cue, links)
                except (MiraError, ValueError, TypeError):
                    retry = (now + timedelta(hours=1)).astimezone(timezone.utc).isoformat()
                    self.repository.delay_consolidation(item["id"], retry)
        return processed

    def _extract(self, turns):
        scope = (turns[0]["user_id"], turns[0]["character_id"])
        existing = [
            {"id": row["id"], "kind": row["kind"], "content": row["content"],
             "fact_key": row["fact_key"], "status": row["status"]}
            for row in self.repository.recent_items(*scope)
        ]
        source = [
            {"turn_id": row["turn_id"], "occurred_at": row["occurred_at"],
             "text": row["text"][:1000]}
            for row in turns
        ]
        instructions = (
            "你是 Mira 的记忆候选提取器。只从给出的用户原话提取，不把助手说法当成用户事实。"
            "每条原话都要有一条或多条候选；普通闲聊可标为 daily_fragment。"
            "玩笑、引用、猜测必须如实标记表达性质，不能表述成事实。"
            "每条候选必须指定 turn_id，并提供该条原话中的完整连续证据片段 evidence。"
            "若与现有记忆冲突，不自行覆盖；按新事件提出，等待程序或用户确认。"
            "输出单个 JSON 对象，不要代码块，格式："
            '{"items":[{"turn_id":"...","kind":"daily_fragment|interest|user_fact|'
            'episode|important_event|shared_experience|plan",'
            '"claim_mode":"asserted|planned|hypothetical|joke|quote|inferred",'
            '"subject":"user","content":"简洁准确的事件或事实",'
            '"evidence":"用户原话的连续片段","fact_key":null,'
            '"topics":["具体主题"],"sensitivity":"ordinary|private|highly_sensitive"}],'
            '"handoff":"当前未完的话题或空字符串"}。'
            "fact_key 只给可变的同一属性或计划状态，不要用通用键；"
            "重要性根据用户证据，不因私密或语气强烈自行提高。"
        )
        answer = self._model_reply([
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(
                {"messages": source, "existing": existing}, ensure_ascii=False)},
        ], "extract", *scope, turns[0]["turn_id"])
        data = self._json_reply(answer)
        items = data.get("items")
        if not isinstance(items, list) or len(items) > 12:
            raise MiraError("invalid_memory_response")
        source_by_id = {row["turn_id"]: row for row in turns}
        proposals = []
        covered = set()
        for raw in items:
            if not isinstance(raw, dict):
                raise MiraError("invalid_memory_response")
            turn_id = raw.get("turn_id")
            source_row = source_by_id.get(turn_id)
            kind = raw.get("kind")
            claim_mode = raw.get("claim_mode")
            sensitivity = raw.get("sensitivity")
            content = raw.get("content")
            evidence = raw.get("evidence")
            subject = raw.get("subject")
            topics = raw.get("topics")
            fact_key = raw.get("fact_key")
            if (source_row is None or kind not in KINDS or
                claim_mode not in CLAIM_MODES or sensitivity not in SENSITIVITIES or
                not isinstance(content, str) or not 0 < len(content.strip()) <= 300 or
                not isinstance(evidence, str) or not evidence or
                evidence not in source_row["text"] or
                not isinstance(subject, str) or not 0 < len(subject.strip()) <= 80 or
                not isinstance(topics, list) or len(topics) > 8 or
                any(not isinstance(tag, str) or not 1 < len(tag.strip()) <= 40
                    for tag in topics) or
                (fact_key is not None and
                 (not isinstance(fact_key, str) or not 2 <= len(fact_key) <= 80))):
                raise MiraError("invalid_memory_response")
            if fact_key in {"内容", "相关", "其他", "content", "other"}:
                raise MiraError("invalid_memory_response")
            status = "active"
            if claim_mode in {"hypothetical", "joke", "quote", "inferred"} or sensitivity == "highly_sensitive":
                status = "candidate"
            elif kind == "plan" and claim_mode == "planned":
                status = "open"
            base = KINDS[kind][0]
            user_text = source_row["text"]
            importance = min(
                1.0, max(base, 0.8 if "记住" in user_text else base)
                + (0.15 if "重要" in user_text else 0)
                + (0.10 if "影响" in user_text else 0),
            )
            proposals.append({
                "turn_id": turn_id, "kind": kind, "claim_mode": claim_mode,
                "status": status, "subject": subject.strip(),
                "content": content.strip(), "evidence": evidence,
                "fact_key": fact_key, "topics": [tag.strip() for tag in topics],
                "sensitivity": sensitivity, "importance": importance,
            })
            covered.add(turn_id)
        for row in turns:
            if row["turn_id"] not in covered:
                proposals.append({
                    "turn_id": row["turn_id"], "kind": "daily_fragment",
                    "claim_mode": "quote", "status": "candidate",
                    "subject": "user", "content": row["text"][:200],
                    "evidence": row["text"][:200], "fact_key": None,
                    "topics": [], "sensitivity": "private",
                    "importance": KINDS["daily_fragment"][0],
                })
        handoff_text = data.get("handoff", "")
        handoff = None
        if isinstance(handoff_text, str) and 0 < len(handoff_text.strip()) <= 200:
            expiry = (self.clock().astimezone(timezone.utc) +
                      timedelta(days=1)).isoformat()
            handoff = {"text": handoff_text.strip(), "expires_at": expiry}
        return proposals, handoff

    def _consolidate(self, item):
        recent_cutoff = (self.clock().astimezone(timezone.utc) -
                         timedelta(days=3)).isoformat()
        siblings = [
            {"id": row["id"], "content": row["content"], "kind": row["kind"],
             "sources": [source["text"][:500] for source in
                         self.repository.source_texts(
                             row["user_id"], row["character_id"], row["id"]
                         )[-2:]]}
            for row in self.repository.recent_items(
                item["user_id"], item["character_id"], 12
            ) if row["id"] != item["id"] and
            row["last_user_evidence_at"] >= recent_cutoff
        ]
        sources = [row["text"][:500] for row in self.repository.source_texts(
            item["user_id"], item["character_id"], item["id"]
        )[-3:]]
        answer = self._model_reply([
            {"role": "system", "content": (
                "只依据给定的权威事件制作可重建的检索投影。"
                "gist 保留能识别该事件的事实和当前/历史状态，最多 100 字；"
                "cue 是 2 到 20 字的具体主题线索。"
                "links 仅列有直接证据的同一主题、先后、更新或共同经历关系；"
                "不要从其他记忆推断新事实。输出 JSON 对象，不要代码块："
                '{"gist":"...","cue":"...","links":[{"to_id":"...",'
                '"relation":"same_topic|before|caused_by|updates|shared_experience"}]}'
            )},
            {"role": "user", "content": json.dumps(
                {"item": {"id": item["id"], "content": item["content"],
                          "kind": item["kind"], "status": item["status"],
                          "sources": sources},
                 "related": siblings}, ensure_ascii=False)},
        ], "consolidate", item["user_id"], item["character_id"], item["id"])
        data = self._json_reply(answer)
        gist, cue, links = data.get("gist"), data.get("cue"), data.get("links")
        if (not isinstance(gist, str) or not 0 < len(gist.strip()) <= 100 or
            not isinstance(cue, str) or not 1 < len(cue.strip()) <= 20 or
            not isinstance(links, list) or len(links) > 4):
            raise MiraError("invalid_memory_response")
        allowed = {row["id"] for row in siblings}
        if any(not isinstance(link, dict) or link.get("to_id") not in allowed or
               link.get("relation") not in RELATIONS for link in links):
            raise MiraError("invalid_memory_response")
        return gist.strip(), cue.strip(), links

    @staticmethod
    def _relevance(query, item):
        text = query.casefold()
        tags = json.loads(item["topics"])
        content = item["content"].casefold()
        if content and content in text:
            return 1.0
        if any(tag.casefold() in text for tag in tags if len(tag) >= 2):
            return 0.8
        if len(text) >= 4 and text in content:
            return 0.8
        if any(content[index:index + 3] in text
               for index in range(max(0, len(content) - 2))
               if len(content[index:index + 3].strip()) == 3):
            return 0.7
        if any(content[index:index + 2] in text
               for index in range(max(0, len(content) - 1))
               if len(content[index:index + 2].strip()) == 2 and
               content[index:index + 2] not in {"我的", "你的", "我们", "他们", "这个", "那个", "时候", "今天", "昨天"}):
            return 0.6
        return 0.0

    def recall(self, user_id, character_id, query, query_turn_id=None, limit=5,
               proactive=False):
        """Select relevant active views without granting model text authority."""
        now = self.clock().astimezone(timezone.utc)
        ranked = []
        decisions = []
        for item in self.repository.list_active(user_id, character_id):
            relevance = self._relevance(query, item)
            if proactive and item["sensitivity"] != "ordinary":
                continue
            if relevance < 0.35 or item["sensitivity"] == "highly_sensitive":
                decisions.append({"id": item["id"], "outcome": "irrelevant_or_private",
                                  "level": None, "score": 0.0})
                continue
            if item["sensitivity"] == "private" and relevance < 0.8:
                decisions.append({"id": item["id"], "outcome": "privacy_gate",
                                  "level": None, "score": 0.0})
                continue
            importance = item["importance"]
            days = max(0.0, (now - datetime.fromisoformat(
                item["last_user_evidence_at"]
            )).total_seconds() / 86400)
            half_life = KINDS[item["kind"]][1] * (1 + 4 * importance)
            if importance >= 0.8:
                half_life = max(half_life, 180)
            score = relevance * (0.2 + 0.8 * importance) * 2 ** (-days / half_life)
            if item["status"] == "open":
                score = max(score, relevance * 0.8)
            level = ("detail" if score >= 0.75 else "gist" if score >= 0.5 else
                     "cue" if score >= 0.25 else None)
            if level in {"detail", "gist"}:
                text = (item["gist"] if level == "gist" and
                        item["view_version"] == item["version"] and item["gist"]
                        else item["content"])
                ranked.append((score, item, level, text))
            else:
                decisions.append({"id": item["id"], "outcome": "below_threshold",
                                  "level": level, "score": score})
        ranked.sort(key=lambda value: value[0], reverse=True)
        selected = []
        used_chars = 0
        for score, item, level, content in ranked:
            if len(selected) >= limit or used_chars + len(content) > 800:
                decisions.append({"id": item["id"], "outcome": "budget",
                                  "level": level, "score": score})
                continue
            selected.append({
                "id": item["id"], "kind": item["kind"], "status": item["status"],
                "occurred_at": item["occurred_at"], "text": content,
            })
            used_chars += len(content)
            decisions.append({"id": item["id"], "outcome": "injected",
                              "level": level, "score": score})
        if query_turn_id and decisions:
            self.repository.log_recall(user_id, character_id, query_turn_id, decisions)
        return selected

    def handoff(self, user_id, character_id):
        row = self.repository.handoff(
            user_id, character_id, self.clock().astimezone(timezone.utc).isoformat()
        )
        return row["text"] if row else None

    def list_memories(self, user_id, character_id, query=None):
        rows = self.repository.list_active(user_id, character_id,
                                           include_disputed=True)
        if query:
            rows = [row for row in rows if self._relevance(query, row) >= 0.35]
        return [
            {"id": row["id"], "kind": row["kind"], "status": row["status"],
             "content": row["gist"] if row["view_version"] == row["version"] and
             row["gist"] else row["content"]}
            for row in rows[:20]
        ]

    def remember(self, turn, content, kind):
        if (kind not in KINDS or not isinstance(content, str) or
            not 0 < len(content.strip()) <= 300):
            raise MiraError("invalid_memory_request")
        return self.repository.save_explicit(turn, content.strip(), kind)

    def source(self, user_id, character_id, memory_id):
        item = self.repository.get_item(user_id, character_id, memory_id)
        if item is None or item["status"] not in {"active", "open", "resolved", "disputed"}:
            raise MiraError("memory_not_found")
        return [{"occurred_at": row["occurred_at"], "text": row["text"]}
                for row in self.repository.source_texts(
                    user_id, character_id, memory_id
                )]

    def forget(self, user_id, character_id, memory_id, current_turn_id):
        self.repository.forget(user_id, character_id, memory_id)
        self.repository.suppress_turn(current_turn_id)

    def correct(self, user_id, character_id, memory_id, current_turn_id, content):
        turn = self.repository.connection.execute(
            "SELECT m.text,m.occurred_at FROM turns t JOIN messages m "
            "ON m.turn_id=t.id AND m.role='user' "
            "WHERE t.id=? AND t.user_id=? AND t.character_id=?",
            (current_turn_id, user_id, character_id),
        ).fetchone()
        if (turn is None or not content.strip() or len(content) > 300 or
            content not in turn["text"]):
            raise MiraError("invalid_memory_correction")
        return self.repository.correct(user_id, character_id, memory_id, {
            "turn_id": current_turn_id, "occurred_at": turn["occurred_at"],
        }, content.strip())
