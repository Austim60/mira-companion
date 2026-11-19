from datetime import datetime

from .models import MiraError


def local_time(occurred_at):
    return datetime.fromisoformat(occurred_at).astimezone().strftime('%Y-%m-%d %H:%M %z')


def build_context(character, history, current, current_at, budget, preferences=(),
                  contact_pause=None, last_contact=None):
    system = ("你是虚拟角色，以下是角色设定。尊重用户的现实生活与自主选择。\n" +
              "角色名称：" + character['display_name'] + "\n性格：" + character['personality'] +
              "\n表达习惯：" + character['speaking_style'] + "\n背景（角色设定）：" + character['background'] +
              "\n互动边界：" + character['boundaries'] +
              "\n当前消息时间：" + local_time(current_at) +
              "。历史用户消息前的发送时间由程序标注，不是用户原话。"
              "历史中的“今天”“昨天”应按各条发送时间理解，不要把不同日期的安排直接当作同一件事。"
              "只根据用户原话陈述其经历，不编造用户说过的内容或终端命令；"
              "较早助手的猜测不能作为用户事实。若只能看到近期记录，就说明范围。"
              "需要核对更早的用户原话时，可自主调用历史工具；用户明确要求保存、查看或撤销偏好时，可自主调用偏好工具。"
              "带完整内容的 /name、/style、/avoid、/correct、/preferences、/remove 是用户的明确请求；"
              "按含义选择对应工具；明确的保存或撤销请求无需再问用户确认。"
              "展示偏好时只用 list_preferences 返回的 position 编号。收到 /remove 数字时，"
              "先调用 list_preferences 核对当前位置；若该位置存在，同一轮直接调用 remove_preference，"
              "随后回复结果。仅在编号不存在或请求含糊时询问用户。"
              "历史工具只覆盖当前用户、角色和本地数据库；若结果标明覆盖全部已保存消息，不要说本库还有未展示的更早消息。"
              "工具只提供数据或执行结果，最后仍由你回复。只有工具确认成功后才能说偏好已保存或撤销；"
              "没有调用工具或工具失败时如实说明，不要承诺后续对话会遵守尚未保存的规则。"
              "明确的不要主动联系请求调用 pause_contact；明确恢复请求调用 resume_contact。"
              "暂停不影响用户主动发来的聊天。只有工具确认成功后才能说暂停或恢复已生效。"
              "模糊的少联系请求先询问范围；角色性格不能推翻明确暂停。")
    if contact_pause:
        until = contact_pause['paused_until']
        system += ('\n当前主动联系已暂停；' +
                   ('持续到用户明确恢复。' if until is None else
                    '到 ' + local_time(until) + ' 自动结束。'))
    if last_contact:
        system += ('\n你上次主动发出的消息（' + local_time(last_contact['created_at']) +
                   '）：' + last_contact['text'] + '。用户可以选择不回复。')
    if preferences:
        system += '\n以下是用户当前明确规则，优先于角色习惯及历史对话：'
    for preference in preferences:
        if preference['kind'] == 'nickname':
            system += '\n用户希望被称为：' + preference['value']
        elif preference['kind'] == 'reply_style':
            system += '\n用户希望的回复方式：' + preference['value']
        elif preference['kind'] == 'avoid_topic':
            system += '\n用户不愿讨论的话题：' + preference['value']
        elif preference['kind'] == 'correction':
            system += ('\n用户已更正旧说法「' + preference['preference_key'] +
                       '」；当前以「' + preference['value'] + '」为准。')
    size = len(system) + len(current)
    if size > budget:
        raise MiraError('context_too_large')
    selected = []
    for user, assistant, occurred_at in reversed(history):
        stamped_user = '[发送时间：' + local_time(occurred_at) + ']\n' + user
        cost = len(stamped_user) + len(assistant)
        if size + cost > budget:
            break
        selected.append((stamped_user, assistant))
        size += cost
    messages = [{'role': 'system', 'content': system}]
    for user, assistant in reversed(selected):
        messages.extend([{'role': 'user', 'content': user}, {'role': 'assistant', 'content': assistant}])
    messages.append({'role': 'user', 'content': current})
    return messages
