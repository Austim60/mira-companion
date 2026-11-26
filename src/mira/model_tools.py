"""Tool descriptions offered to the conversation model."""


CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "列出当前用户与角色仍有效的长期记忆，返回可用于核对、更正、忘记的 ID。",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "可选主题筛选"}
            }, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory_source",
            "description": "用户要核对某条长期记忆的原话时，按 list_memories 给出的 ID 读取来源。",
            "parameters": {"type": "object", "properties": {
                "id": {"type": "string"}
            }, "required": ["id"], "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_memory",
            "description": "用户明确要求记住当前原话中的某件事时立即保存。content 必须是当前用户原话中的连续原文片段。",
            "parameters": {"type": "object", "properties": {
                "content": {"type": "string"},
                "kind": {"type": "string", "enum": ["daily_fragment", "interest", "user_fact", "episode", "important_event", "shared_experience", "plan"]}
            }, "required": ["content", "kind"], "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correct_memory",
            "description": "用户明确更正指定记忆时使用。content 必须是当前用户原话中的连续更正片段。",
            "parameters": {"type": "object", "properties": {
                "id": {"type": "string"}, "content": {"type": "string"}
            }, "required": ["id", "content"], "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget_memory",
            "description": "用户明确要求忘记指定记忆时使用；会阻止对应原话再次进入模型上下文。",
            "parameters": {"type": "object", "properties": {
                "id": {"type": "string"}
            }, "required": ["id"], "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_user_messages",
            "description": "读取当前用户与角色在本轮之前保存的最近 20 条用户原文及发送时间。核对用户过去说过什么时使用；不包含未发送给模型的 /help、/exit 等终端控制命令。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_preferences",
            "description": "查看当前用户对本角色仍有效的称呼、回复方式、不聊话题和更正。返回的 position 是当前有效列表中的连续编号；展示给用户时必须原样使用，不能改成数据库 ID。收到 /preferences 时调用。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_preference",
            "description": "用户明确要求时，为当前用户和角色保存称呼、回复方式、不聊话题或事实更正。/name、/style、/avoid、/correct 加完整内容属于明确请求，可直接调用而无需再确认。更正时 key 是旧说法，value 是新说法。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["nickname", "reply_style", "avoid_topic", "correction"]},
                    "value": {"type": "string", "description": "用户希望生效的新内容"},
                    "key": {"type": "string", "description": "仅更正时提供需要替代的旧说法"},
                },
                "required": ["kind", "value"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_preference",
            "description": "用户明确要求撤销某条规则时，按当前有效列表中的 position 停用该偏好。/remove 2 表示当前列表第 2 条，与数据库 ID 无关。先调用 list_preferences 核对；位置存在时在同一轮直接调用本工具，无需再问用户。只有本工具成功后才能回复已撤销。",
            "parameters": {
                "type": "object",
                "properties": {"position": {"type": "integer", "description": "list_preferences 返回的连续位置编号，从 1 开始"}},
                "required": ["position"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_contact",
            "description": "用户明确要求暂时不要主动联系时保存暂停。今天、这周分别用 today、this_week；没有期限用 indefinite。暂停只阻止主动联系，用户先发消息仍正常回复。含糊的少联系请求先询问范围，不要擅自暂停。",
            "parameters": {
                "type": "object",
                "properties": {"period": {"type": "string", "enum": ["today", "this_week", "indefinite"]}},
                "required": ["period"], "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_contact",
            "description": "用户明确要求恢复主动联系时取消当前暂停；不会补发暂停期间错过的消息。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_follow_up",
            "description": "用户明确说了未来事项且当前角色获准主动联系时，保存一条有用户原话来源的待跟进候选。用户已说结果、只是玩笑或时间含糊到无法确定合适联系窗口时不要保存，先询问。due_at 是最早适宜联系时间，expires_at 是过时后不再主动问的时间；都必须带时区。",
            "parameters": {"type": "object", "properties": {
                "topic": {"type": "string", "description": "简短且忠于用户原话的事项"},
                "due_at": {"type": "string", "description": "最早适宜联系时间，ISO 8601 带时区"},
                "expires_at": {"type": "string", "description": "事项过期时间，ISO 8601 带时区"},
            }, "required": ["topic", "due_at", "expires_at"],
                "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_follow_ups",
            "description": "查看当前用户与角色尚未处理的待跟进事项及 ID；用户要查看、取消或已说出结果时先调用。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_follow_up",
            "description": "用户明确取消某事项或已经说出结果时，按 list_follow_ups 返回的 ID 关闭，避免主动联系再提。",
            "parameters": {"type": "object", "properties": {
                "id": {"type": "string"},
                "status": {"type": "string", "enum": ["cancelled", "resolved"]},
            }, "required": ["id", "status"], "additionalProperties": False},
        },
    },
]
