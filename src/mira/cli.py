"""Interactive terminal entry point for the local companion."""

import argparse
import sys
from datetime import datetime, timezone
from threading import Event, Thread
from uuid import uuid4

from .config import load_character, load_settings
from .conversation import ConversationService
from .models import MessageInput, MiraError
from .provider import OpenAICompatibleProvider
from .proactive import ProactiveService
from .storage import ConversationRepository


HELP = "输入 /help 查看命令用法。"
COMMAND_USAGE = {
    "/help": "用法：/help（逐条查看命令用法）",
    "/status": "用法：/status（查看当前角色、模型和最近轮次状态）",
    "/retry": "用法：/retry（重试最近的失败轮次，可能再次产生模型费用）",
    "/name": "用法：/name 小墨（请 Mira 以后称呼你为“小墨”；不会修改 Mira 的名字）",
    "/style": "用法：/style 请简短回答（设置 Mira 回复你的方式）",
    "/avoid": "用法：/avoid 考研（设置你不希望 Mira 讨论的话题）",
    "/correct": "用法：/correct 面试在下午三点 => 面试改为下午四点（更正 Mira 使用的当前事实；不会删除原始聊天）",
    "/preferences": "用法：/preferences（请 Mira 查看已保存规则及编号）",
    "/remove": "用法：/remove 1（先用 /preferences 查看编号，再请 Mira 撤销对应规则）",
    "/proactive": "用法：/proactive on|off|preview|check（授权、预览或按实际规则检查）",
    "/exit": "用法：/exit（退出聊天）",
}
ARGUMENT_COMMANDS = {"/name", "/style", "/avoid", "/correct", "/remove", "/proactive"}
PROACTIVE_RESULTS = {
    "disabled": "尚未开启", "paused": "主动联系已暂停", "quiet_hours": "当前不在联系时段",
    "daily_limit": "今天已达到上限", "recent_conversation": "最近六小时聊过天",
    "awaiting_reply": "上一条主动消息尚未得到回复", "no_conversation": "暂无可参考的对话",
    "already_checked": "当前时段已经检查过", "skipped": "模型决定这次不联系",
    "sent": "已在当前聊天输出主动消息", "boundary_changed": "发送前的边界条件已变化",
}


def _run_proactive(settings, user_id, character, stop, wake):
    repository = ConversationRepository(settings.database_path)
    try:
        proactive = ProactiveService(
            settings, repository, OpenAICompatibleProvider(settings),
            lambda text: print(f"\n{character['display_name']}：{text}\n> ", end="", flush=True),
            should_stop=stop.is_set,
        )
        while not stop.is_set():
            try:
                result = proactive.check(user_id, character["character_id"])
                if result in {"delivery_unknown", "invalid_proactive_decision"}:
                    print("\n主动联系检查：" + result, flush=True)
            except Exception as exc:
                print("\n主动联系检查失败：" + getattr(exc, "code", type(exc).__name__), flush=True)
            wake.wait(60)
            wake.clear()
    finally:
        repository.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Mira 本地文字陪伴")
    parser.add_argument("command", choices=["chat"])
    parser.parse_args(argv)
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
    stop = Event()
    wake = Event()
    worker = Thread(target=_run_proactive, args=(settings, user_id, character, stop, wake),
                    daemon=True)
    print(f"正在和 {character['display_name']} 聊天。{HELP}")
    print("配置优先级：进程环境变量高于当前目录 .env。")
    worker.start()
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
                for command, usage in COMMAND_USAGE.items():
                    print(command)
                    print(usage)
                continue
            if line in ARGUMENT_COMMANDS:
                print(COMMAND_USAGE[line])
                continue
            if line == "/status":
                latest = service.latest_turn(user_id, character_id)
                state = "无对话" if latest is None else f"{latest['status']} ({latest['id']})"
                paused = "是" if service.is_contact_paused(user_id, character_id) else "否"
                enabled = "是" if repository.proactive_enabled(user_id, character_id) else "否"
                print(f"角色：{character['display_name']}；模型：{settings.model}；最近轮次：{state}；主动联系授权：{enabled}；暂停：{paused}")
                continue
            if line in {"/proactive on", "/proactive off"}:
                enabled = line.endswith("on")
                repository.set_proactive_enabled(user_id, character_id, enabled)
                print("日常主动联系已" + ("开启" if enabled else "关闭") + "。")
                wake.set()
                continue
            try:
                if line in {"/proactive preview", "/proactive check"}:
                    proactive = ProactiveService(
                        settings, repository, OpenAICompatibleProvider(settings),
                        lambda text: print(f"{character['display_name']}：{text}"),
                    )
                    if line.endswith("preview"):
                        choice, content, reason = proactive.preview(user_id, character_id)
                        if choice == "send":
                            print(f"主动联系预览（未发送）：{content}\n判断原因：{reason}")
                        elif choice == "skip":
                            print("模型决定跳过（未发送）：" + reason)
                        else:
                            print("预览结果：" + PROACTIVE_RESULTS.get(choice, choice))
                    else:
                        result = proactive.check(user_id, character_id)
                        print("主动联系检查：" + PROACTIVE_RESULTS.get(result, result))
                    continue
                if line == "/retry":
                    latest = service.latest_turn(user_id, character_id)
                    if latest is None:
                        raise MiraError("retry_not_allowed")
                    print("重试可能再次调用模型并产生费用；使用当前角色和模型配置。")
                    result = service.retry_turn(latest["id"], user_id, character_id)
                elif line.startswith("/") and not (
                    line == "/preferences" or
                    line.startswith(("/name ", "/style ", "/avoid ", "/correct ", "/remove "))
                ):
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
        stop.set()
        wake.set()
        worker.join(timeout=1)
        repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
