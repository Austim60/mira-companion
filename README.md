# mira-companion

一个以长期文字陪伴为目标的虚拟伴侣项目。项目先构建独立的单用户、单角色对话核心，输入和输出均为文字；后续通过独立的通讯适配层接入 QQ 等平台。

## 当前状态

第一版独立文字对话核心已实现：终端交互、角色配置、OpenAI 兼容模型调用、SQLite 原文与状态持久化、近期上下文、失败可见和手动重试。QQ 机器人模型回复和一次定时投递已完成初步验证；跨天投递、权限限制及断线恢复仍需实测。完整产品验收仍需记忆、主动联系、迁移和 QQ 接入。

首阶段计划涵盖角色设定、对话上下文、私有记忆与纠错、待跟进事项、受控主动联系及角色资料迁移。多角色、微信、语音与形象作为后续方向。

## 项目文档

- [产品需求说明书](docs/虚拟陪伴产品需求说明书_QQ单角色先行版_v1.6.md)：产品范围和验收标准。
- [核心技术设计](docs/虚拟女友核心技术设计.md)：对话生命周期、模块职责和实施计划。
- [第一版对话核心技术设计](docs/第一版对话核心技术设计.md)：首版代码结构、接口、数据模型和验收方案。
- [拟真记忆系统设计](docs/拟真记忆系统设计.md)：记忆机制与开源项目参考；部分多角色内容属于后续探索，以现行 PRD 和核心设计的阶段范围为准。
- [QQ 机器人接入与验证记录](docs/QQ机器人接入与验证记录.md)：已验证的消息链路和待验证事项。
- [项目阶段看板](docs/虚拟女友项目阶段看板.xlsx)：研发阶段及完成标志；进度以实际记录为准。
- [GitHub 技术调研报告](docs/research/虚拟女友_GitHub技术调研报告.md)及[调研汇报](docs/research/虚拟女友_GitHub技术调研汇报.pptx)：早期调研参考，范围以现行 PRD 为准。

## 本地运行

在 `mira` 虚拟环境中运行，Python 版本至少 3.10。复制 `.env.example` 为 `.env` 并填写模型服务信息；复制 `configs/character.example.json` 为 `configs/character.json`，按需调整角色设定。进程环境变量优先于 `.env`。模型接口需兼容 Chat Completions；当前没有配置真实模型凭据时，无法进行在线试聊。

在仓库根目录执行：

```powershell
$env:PYTHONPATH = 'src'
D:\miniconda3\envs\mira\python.exe -m mira.cli chat
```

输入 `/help` 查看命令。`/retry` 仅重试最近的失败轮次，可能再次产生模型调用及费用。角色配置每轮重新读取；更换稳定的 `character_id` 会被拒绝。聊天原文保存在本地 SQLite，`.env`、个人角色配置和数据库均被 Git 忽略。

运行自动测试：

```powershell
$env:PYTHONPATH = 'src'
D:\miniconda3\envs\mira\python.exe -m unittest discover -s tests -v
```

工作区上层的 `reference-projects/` 保存了相关开源项目的本地参考源码；实现遵循本仓库 PRD 和技术设计，没有直接复制第三方代码。
"# mira-companion" 
