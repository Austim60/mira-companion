"""Tool descriptions offered to the conversation model."""


CHAT_TOOLS = [
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
]
