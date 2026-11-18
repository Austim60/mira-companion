from .models import MiraError


def build_context(character, history, current, budget):
    system = ("你是虚拟角色，以下是角色设定。尊重用户的现实生活与自主选择。\n" +
              "角色名称：" + character['display_name'] + "\n性格：" + character['personality'] +
              "\n表达习惯：" + character['speaking_style'] + "\n背景（角色设定）：" + character['background'] +
              "\n互动边界：" + character['boundaries'])
    size = len(system) + len(current)
    if size > budget:
        raise MiraError('context_too_large')
    selected = []
    for user, assistant in reversed(history):
        cost = len(user) + len(assistant)
        if size + cost > budget:
            break
        selected.append((user, assistant))
        size += cost
    messages = [{'role': 'system', 'content': system}]
    for user, assistant in reversed(selected):
        messages.extend([{'role': 'user', 'content': user}, {'role': 'assistant', 'content': assistant}])
    messages.append({'role': 'user', 'content': current})
    return messages
