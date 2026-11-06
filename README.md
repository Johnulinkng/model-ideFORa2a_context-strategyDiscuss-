# Model IDE：精简上下文与顾问复核

让当前 Codex 先形成方案，再把必要上下文、实际成果和验收交给 DeepSeek。顾问意见原文回到当前 Codex，由它判断、修改和验证，继续推进任务。

需要补信息时，Codex 查事实并回传；同一检查点最多两轮。简单任务跳过。提示词沿用自然简短问句，不强制回答条数、排序或栏目。

- [真实通信往返与审查](docs/recent-roundtrip.md)：压缩背景，保留原始问题、关键回答和最终判断，也保留实际出现的错误。
- [通用技能](skills/decision-review/SKILL.md)：关键方案、阶段成果和交付复核。
- [本机配置](skills/decision-review/references/runtime.md)：登记项目并复用网关连接。
- [工具适配协议](skills/decision-review/references/adapter-contract.md)：复核与搜索命令的输入输出约定。

## 使用

用 Codex 的 `$skill-installer` 安装此仓库中的 `skills/decision-review`，或将该目录放到宿主支持的技能目录。安装后，在新项目显式调用：

> `$decision-review` 帮我完成这个开发任务，复杂决策和交付前按复核流程推进。

首次由主助手配置已有本机工具及当前项目。已有兼容网关时，可从已有 runtime JSON 导入连接信息；不会继承旧项目范围。凭据留在本机，不放到仓库或聊天中。`runtime.json` 已忽略。

本包依赖符合适配协议的复核／搜索可执行工具，提供 Python 工作流调用器和测试。它不包含模型网关、供应商凭据或编译好的网关二进制。需要外部取证时，搜索工具必须提供 `query-paragraph-v1` 回执；旧头部摘录不会被新校验器接受。

Stop 兜底需要宿主单独安装并信任配置；基础复核可以直接使用。可生成配置供审阅，不自动改全局 Hook。当前单 DS 调用，Claude 尚未作为此包的可用后端。Python 3.9+，macOS/Linux；Windows 锁适配尚未实现。

## 验证

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s skills/decision-review/scripts -p 'test_*.py' -q
```

测试使用本地模拟顾问回文，不会调用收费接口。真实 Stop 案例的搜索和 DS 回复摘录见通信记录；它验证该次关键证据保留，不代表任意网页语义完整或行业认证。
