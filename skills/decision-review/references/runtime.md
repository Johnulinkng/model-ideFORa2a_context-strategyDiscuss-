# 本机连接与新项目

本包提供工作流技能、Python 调用器及测试。模型网关和两个本机可执行工具是外部依赖，不随本包发布；已有工具的协议见[适配协议](adapter-contract.md)。Python 3.9+，当前锁实现适用于 macOS/Linux，Windows 尚未适配。

安装 `skills/decision-review` 后，由当前主助手执行配置；密钥留在既有网关或环境中，不写进技能，不发到聊天。

已有同协议的本机连接配置时，可以仅复用连接信息，不继承旧项目作用域：

```sh
python3 <SKILL_DIR>/scripts/configure.py --profile-file /absolute/existing-runtime.json --project /absolute/new-project
```

没有配置时指定已有工具与网关配置路径；`--search-binary` 可省略，但需要外部取证的检查点会因缺工具而失败，不会假装联网：

```sh
python3 <SKILL_DIR>/scripts/configure.py --project /absolute/project --binary /absolute/review-plan --gateway-config /absolute/private-gateway.yaml --search-binary /absolute/review-search --model verified-model-alias
```

之后在另一个已授权项目显式调用技能，由主助手用 `configure.py --project` 登记目录，沿用已有连接。每次初始化只写本技能的私有 `runtime.json`，不会修改项目 `AGENTS.md`、全局配置或 Hook 信任；文件已加入 `.gitignore`。不用手工填写送审 JSON，主助手负责准备与调用。

Stop 是可选增强：

```sh
python3 <SKILL_DIR>/scripts/configure.py --print-hooks
```

此命令只输出当前安装路径对应的配置。主助手应检查宿主已有同类 Hook，避免重复注册；用户选择启用时再合并并按宿主要求审阅信任。基础复核不依赖 Stop。新宿主只复制技能不代表 Hook 已安装或已信任。官方行为见 [Hooks](https://learn.chatgpt.com/docs/hooks)。
