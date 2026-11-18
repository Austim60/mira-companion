"""Interactive terminal entry point for the local companion."""

import argparse
import sys
from datetime import datetime, timezone
from uuid import uuid4

from .config import load_character, load_settings
from .conversation import ConversationService
from .models import MessageInput, MiraError
from .provider import OpenAICompatibleProvider
from .storage import ConversationRepository


HELP = "命令：/help 帮助，/status 状态，/retry 重试最近失败轮次，/exit 退出"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mira 本地文字陪伴")
    parser.add_argument("command", choices=["chat"])
    args = parser.parse_args(argv)
    if args.command != "chat":
        return 2
    try:
        settings = load_settings()
        character = load_character(settings.character_path)
        repository = ConversationRepository(settings.database_path)
        service = ConversationService(settings, repository, OpenAICompatibleProvider(settings))
        service.prepare()
        service.recover()
    except (MiraError, OSError) as exc:
        print(f"启动失败：{getattr(exc, 'code', 'storage_error')}", file=sys.stderr)
        return 1
    user_id = "local-user"
    character_id = character["character_id"]
    print(f"正在和 {character['display_name']} 聊天。{HELP}")
    print("配置优先级：进程环境变量高于当前目录 .env。")
    try:
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line == "/exit":
                break
            if line == "/help":
                print(HELP)
                continue
            if line == "/status":
                latest = service.latest_turn(user_id, character_id)
                state = "无对话" if latest is None else f"{latest['status']} ({latest['id']})"
                print(f"角色：{character['display_name']}；模型：{settings.model}；最近轮次：{state}")
                continue
            try:
                if line == "/retry":
                    latest = service.latest_turn(user_id, character_id)
                    if latest is None:
                        raise MiraError("retry_not_allowed")
                    print("重试可能再次调用模型并产生费用；使用当前角色和模型配置。")
                    result = service.retry_turn(latest["id"], user_id, character_id)
                elif line.startswith("/"):
                    print("未知命令，输入 /help 查看帮助。")
                    continue
                else:
                    result = service.handle_message(MessageInput(
                        user_id, character_id, "cli", str(uuid4()), line,
                        datetime.now(timezone.utc).isoformat(),
                    ))
                if result.status == "completed":
                    print(f"{character['display_name']}：{result.text}")
                else:
                    print(f"本轮失败：{result.error_code}；输入 /retry 可手动重试。")
            except MiraError as exc:
                print(f"操作失败：{exc.code}")
    finally:
        repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
