# 本机连接与新项目

本机需要 macOS 或 Linux、Python 3.9+、兼容的 `review-plan` 可执行文件和网关配置。需要公开取证时再配置 `review-search`。凭据由既有网关或环境提供。[适配协议](adapter-contract.md)

在要复核的项目根目录打开终端，先输入技能安装目录的绝对路径。

```sh
printf 'decision-review 安装目录的绝对路径：'
IFS= read -r REVIEW_SKILL_DIR
```

## 复用已有连接

导入已有 `runtime.json` 时会沿用其中的工具和连接路径，并把本次项目作为新的作用域。

```sh
printf '已有 runtime.json 的绝对路径：'
IFS= read -r REVIEW_PROFILE
python3 "$REVIEW_SKILL_DIR/scripts/configure.py" \
  --profile-file "$REVIEW_PROFILE" \
  --project "$PWD"
```

## 直接绑定工具

第一次配置时，按提示输入已有可执行文件和网关配置的绝对路径。

```sh
printf 'review-plan 可执行文件的绝对路径：'
IFS= read -r REVIEW_BINARY
printf '网关配置文件的绝对路径：'
IFS= read -r REVIEW_GATEWAY_CONFIG
python3 "$REVIEW_SKILL_DIR/scripts/configure.py" \
  --binary "$REVIEW_BINARY" \
  --gateway-config "$REVIEW_GATEWAY_CONFIG" \
  --project "$PWD"
```

网关要求指定模型别名时，可在配置命令中加 `--model` 和实际别名。需要公开取证时，用下面的命令补上搜索工具。

```sh
printf 'review-search 可执行文件的绝对路径：'
IFS= read -r REVIEW_SEARCH_BINARY
python3 "$REVIEW_SKILL_DIR/scripts/configure.py" \
  --search-binary "$REVIEW_SEARCH_BINARY" \
  --project "$PWD"
```

配置成功会返回 `"status": "configured"`、项目路径和 `search_available`。`search_available` 表示已经配置搜索工具路径。随后在 Codex 打开该项目并显式调用 `$decision-review`，由主助手准备送审资料和处理回文。

另一个项目也需要复用这份连接时，在新项目根目录运行下面的命令。配置保存在技能目录的私有 `runtime.json`，仓库已忽略此文件。

```sh
python3 "$REVIEW_SKILL_DIR/scripts/configure.py" --project "$PWD"
```

## 可选 Stop 兜底

生成与安装位置对应的 Hook 配置后，在宿主中合并、审阅并信任。已配置同类 Hook 时，先核对事件和命令，避免重复注册。[官方 Hook 说明](https://learn.chatgpt.com/docs/hooks)

```sh
python3 "$REVIEW_SKILL_DIR/scripts/configure.py" --print-hooks
```
